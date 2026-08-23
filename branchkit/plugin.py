"""Plugin core: bidirectional JSON-RPC 2.0 over stdin/stdout (NDJSON).

asyncio single-threaded core. Handlers registered with `handle` / `on` /
`handle_action` may be `async def` (run on the loop) or plain `def`
(auto-offloaded via `asyncio.to_thread` so a blocking body cannot freeze
every other handler). Registration must happen before `run()`; `call()`
may be made from any task once `run()` is underway.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import sys
import threading
from typing import Any, Callable

from .closed_vocab_gen import ERROR_KIND_RECORDING_DISABLED
from .contracts_gen import API_VERSION as _COMPILED_API_VERSION
from .contracts_gen import HOOK_ON_ACTION
from .actor import get_current_actor
from .correlation import get_current_correlation, reset_correlation, set_correlation
from .log import log


def matches_topic(pattern: str, event_type: str) -> bool:
    """Does `event_type` match `pattern`, where `*` is exactly one
    dot-separated segment? Mirrors the actuator's `event_bus::matches_topic`,
    which is what actually gates delivery — the two must agree or a plugin's
    own routing disagrees with what it receives."""
    if pattern == event_type:
        return True
    pat = pattern.split(".")
    evt = event_type.split(".")
    if len(pat) != len(evt):
        return False
    return all(p == "*" or p == e for p, e in zip(pat, evt))

# Mirrors the Go SDK's `oversizedFrameBytes` — keep the SDKs in step so the
# tripwire fires at the same size whichever SDK a plugin uses.
_OVERSIZED_FRAME_BYTES = 1024 * 1024

# StreamReader line limit. A limit, unlike the tripwire above: readline
# refuses lines beyond it, so it sits far above any frame the platform
# ships (the 1 MB tripwire logs long before this truncates).
_READ_LIMIT = 64 * 1024 * 1024


class RpcCallError(Exception):
    """An error returned by the actuator in response to a plugin call.

    Branch on `kind`, never on the message prose — the wording is not part
    of the contract. Version skew: an actuator predating structured errors
    sends no `data`, so `kind` is None and only `code` and the message are
    meaningful.
    """

    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = data
        self.kind: str | None = data.get("kind") if isinstance(data, dict) else None


class RecordingDisabledError(RpcCallError):
    """Sentinel subclass for the recording-disabled refusal: a log
    collection's recording flag is off, so the append was refused.

    Constructed centrally by `rpc_error_for`, so `isinstance` works for ANY
    call that hits this condition — parity with Go's
    `errors.Is(err, ErrRecordingDisabled)` and TS's `instanceof`."""


def rpc_error_for(code: int, message: str, data: dict | None = None) -> RpcCallError:
    """Build the right error class for a wire error. Kind-driven, so a new
    sentinel subclass is a line here rather than a wrapper per call site."""
    if isinstance(data, dict) and data.get("kind") == ERROR_KIND_RECORDING_DISABLED:
        return RecordingDisabledError(code, message, data)
    return RpcCallError(code, message, data)


def error_kind_of(e: object) -> str | None:
    """The error kind off any raised value, or None when the value is not
    an RpcCallError or carries no structured data."""
    return e.kind if isinstance(e, RpcCallError) else None


class PluginCore:
    """Transport + dispatch. The public `Plugin` class (see `__init__.py`)
    layers the generated method wrappers and the façade mixins on top."""

    def __init__(self) -> None:
        self._plugin_id: str = os.environ.get("BRANCHKIT_PLUGIN_ID", "unknown")
        self._handlers: dict[str, Callable] = {}
        self._listeners: dict[str, list[Callable]] = {}
        # on_pattern registrations, in registration order. A list rather than
        # a dict: the key is a pattern, so lookup is a scan either way, and
        # order is what makes delivery deterministic.
        self._pattern_listeners: list[tuple[str, Callable]] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._action_handlers: dict[str, Callable] | None = None
        self._next_id = 1
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        # Inbound notifications drain through one pump so listeners observe
        # them in wire order. See notes/DESIGN_SDK_EVENT_ORDERING.md.
        self._notify_queue: asyncio.Queue = asyncio.Queue()
        # Stdout writes can come from the loop AND from offloaded sync
        # handlers calling notify(); one lock keeps frames unfragmented.
        self._write_lock = threading.Lock()

        # Built-in introspection: the actuator calls list_action_types after
        # readiness to validate handlers against the manifest's
        # `action_types`; list_methods feeds the settings-HTML validator.
        async def _list_action_types(params):
            return {"action_types": self.registered_action_types() or []}

        async def _list_methods(params):
            return {"methods": sorted(self._handlers.keys())}

        self._handlers["list_action_types"] = _list_action_types
        self._handlers["list_methods"] = _list_methods

    @property
    def id(self) -> str:
        """This plugin's own id, as the actuator assigned it
        (BRANCHKIT_PLUGIN_ID), or "unknown" outside the actuator."""
        return self._plugin_id

    # --- Registration ---

    def handle(self, method: str, fn: Callable | None = None):
        """Register a handler for actuator→plugin requests. Usable directly
        (`plugin.handle("m", fn)`) or as a decorator (`@plugin.handle("m")`).

        `handle("on_action", ...)` and `handle_action(...)` are mutually
        exclusive — both install a handler for the same RPC method."""
        if fn is None:
            def deco(f):
                self.handle(method, f)
                return f
            return deco
        if method == HOOK_ON_ACTION and self._action_handlers is not None:
            raise RuntimeError(
                'plugin-sdk-py: cannot mix handle("on_action", ...) and handle_action(...) — pick one'
            )
        self._handlers[method] = fn
        return fn

    def handle_action(self, action: str, fn: Callable | None = None):
        """Register a handler for a single dispatched action type. The SDK
        installs an internal on_action handler that demuxes by
        `req["action"]`. Return-value semantics:

          - None → ``{"status": "ok"}``
          - anything else → sent back as the JSON-RPC result verbatim
          - raise → translated to a JSON-RPC error response
        """
        if fn is None:
            def deco(f):
                self.handle_action(action, f)
                return f
            return deco
        if self._action_handlers is None:
            if HOOK_ON_ACTION in self._handlers:
                raise RuntimeError(
                    'plugin-sdk-py: cannot mix handle("on_action", ...) and handle_action(...) — pick one'
                )
            self._action_handlers = {}
            self._handlers[HOOK_ON_ACTION] = self._dispatch_action
        self._action_handlers[action] = fn
        return fn

    # The design-doc registration idiom (`@plugin.action("…")`) — an alias
    # of handle_action, which keeps the Go/TS name greppable too.
    action = handle_action

    def registered_action_types(self) -> list[str] | None:
        """Action types registered via handle_action, or None if none."""
        if self._action_handlers is None:
            return None
        return list(self._action_handlers.keys())

    async def _dispatch_action(self, params):
        req = params if isinstance(params, dict) else {}
        handler = (self._action_handlers or {}).get(req.get("action", ""))
        if handler is None:
            return {"status": "not_handled"}
        result = await self._invoke(handler, req)
        if result is None:
            return {"status": "ok"}
        return result

    def on(self, method: str, fn: Callable | None = None):
        """Register a listener for actuator→plugin notifications
        (fire-and-forget). Multiple listeners may share a method. The
        ordered pump awaits each listener before delivering the next
        notification, so listeners observe wire order."""
        if fn is None:
            def deco(f):
                self.on(method, f)
                return f
            return deco
        self._listeners.setdefault(method, []).append(fn)
        return fn

    def on_pattern(self, pattern: str, fn: Callable | None = None):
        """Register a listener for every notification whose method matches
        `pattern`, where `*` stands for exactly one dot-separated segment —
        the same language `consumes.events` uses in the manifest.

        Needed whenever a plugin subscribes to a namespace instead of a name:
        `on` keys listeners by exact method, so a manifest subscription like
        `scripts.*.*` or `browser.tab.*` had events delivered to the process
        and then silently dropped by the SDK. That shape is the norm for host
        plugins, whose hosted things name their events at runtime.

        The callback takes `(event_type, params)` — a pattern listener by
        definition does not know which event arrived. The manifest still
        bounds delivery: a pattern here can only ever see events the plugin's
        `consumes.events` already admits."""
        if fn is None:
            def deco(f):
                self.on_pattern(pattern, f)
                return f
            return deco
        self._pattern_listeners.append((pattern, fn))
        return fn

    def on_ready(self, fn: Callable | None = None):
        """Register a callback fired when all plugins are ready — the safe
        place to read other plugins' collections."""
        if fn is None:
            def deco(f):
                self.on_ready(f)
                return f
            return deco
        self.on("on_ready", lambda params: fn())
        return fn

    # --- Outbound ---

    async def call(self, method: str, params: Any = None, timeout: float | None = None) -> Any:
        """Send a request to the actuator and wait for the response.
        Default timeout 10s (T1); override per call (T3). Raises
        RpcCallError for wire errors, TimeoutError on expiry."""
        if self._closed:
            raise RuntimeError("plugin shutting down")
        loop = asyncio.get_running_loop()
        call_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = loop.create_future()
        self._pending[call_id] = fut
        # The outbound frame inherits the ambient inbound correlation so the
        # call joins the upstream causal chain.
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": call_id, "method": method}
        if params is not None:
            msg["params"] = params
        corr = get_current_correlation()
        if corr:
            msg["correlation_id"] = corr
        actor = get_current_actor()
        if actor:
            msg["on_behalf_of"] = actor
        self._write(msg)
        try:
            return await asyncio.wait_for(fut, 10.0 if timeout is None else timeout)
        except asyncio.TimeoutError:
            self._pending.pop(call_id, None)
            raise TimeoutError(
                f'rpc call "{method}" timed out after {10.0 if timeout is None else timeout}s'
            ) from None

    def call_sync(self, method: str, params: Any = None, timeout: float | None = None) -> Any:
        """`call` for plain-`def` handlers, which run off the loop in a
        worker thread and cannot await. Blocks the worker thread only."""
        if self._loop is None:
            raise RuntimeError("call_sync before run()")
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError("call_sync would deadlock the event loop — use `await call(...)` in async handlers")
        cfut = asyncio.run_coroutine_threadsafe(self.call(method, params, timeout), self._loop)
        return cfut.result()

    def notify(self, method: str, params: Any = None) -> None:
        """Send a fire-and-forget notification to the actuator."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        corr = get_current_correlation()
        if corr:
            msg["correlation_id"] = corr
        actor = get_current_actor()
        if actor:
            msg["on_behalf_of"] = actor
        self._write(msg)

    def current_correlation(self) -> str:
        """The inbound correlation id for the request or notification
        currently being handled, or "" if none is in flight. Outbound calls
        inherit it automatically."""
        return get_current_correlation()

    def current_actor(self) -> str:
        """The actor label outbound calls currently carry, or "" if none.
        Hosts read it to tag their own logs with the name the platform
        records."""
        return get_current_actor()

    # --- Lifecycle ---

    async def run(self) -> None:
        """Signal that all handlers are registered, start the transport,
        and block until shutdown. Incoming requests are held until run()
        is called (L4)."""
        self._loop = asyncio.get_running_loop()
        log(self._plugin_id, "started (JSON-RPC over stdio)")

        # Graceful SIGTERM/SIGINT (L3).
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(sig, self._on_signal)
            except (NotImplementedError, RuntimeError):
                pass  # non-Unix loop

        reader = asyncio.StreamReader(limit=_READ_LIMIT)
        protocol = asyncio.StreamReaderProtocol(reader)
        await self._loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

        read_task = asyncio.ensure_future(self._read_loop(reader))
        pump_task = asyncio.ensure_future(self._drain_notifications())

        self._ready.set()
        self.notify("plugin.initialized")

        await self._shutdown_event.wait()
        read_task.cancel()
        pump_task.cancel()

    def _on_signal(self) -> None:
        log(self._plugin_id, "shutting down (signal)")
        self._do_shutdown()

    def _do_shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Reject all pending calls (L2).
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("plugin shutting down"))
        self._pending.clear()
        self._shutdown_event.set()

    # --- Internal ---

    def _write(self, msg: dict) -> None:
        if self._closed:
            return
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                sys.stdout.buffer.write(line.encode("utf-8"))
                sys.stdout.buffer.flush()
            except (BrokenPipeError, ValueError):
                # The actuator side went away mid-write; shutdown follows
                # from the read loop's EOF.
                pass

    def _send_error(self, msg_id, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                raw = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                log(self._plugin_id, "stdin frame exceeded the read limit, dropping")
                continue
            if not raw:
                # Exit when stdin closes (L1).
                log(self._plugin_id, "stdin closed, exiting")
                self._do_shutdown()
                return
            line = raw.strip()
            if not line:
                continue
            # Tripwire, not a limit — same posture and threshold as the
            # Go/TS read loops: dispatched anyway, but a frame this large
            # means the platform is shipping something it probably did not
            # intend to.
            if len(line) > _OVERSIZED_FRAME_BYTES:
                log(self._plugin_id, f"large stdin frame: {len(line)} bytes (dispatching anyway)")
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                log(self._plugin_id, f"failed to parse message: {line[:200]!r}")
                continue
            if isinstance(msg, dict):
                self._route_message(msg)

    def _route_message(self, msg: dict) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")
        # Response to a pending call — id + (result or error), no method.
        if msg_id is not None and not method:
            fut = self._pending.pop(msg_id, None)
            if fut is not None and not fut.done():
                err = msg.get("error")
                if err:
                    fut.set_exception(
                        rpc_error_for(err.get("code", -1), err.get("message", ""), err.get("data"))
                    )
                else:
                    fut.set_result(msg.get("result"))
            return
        # Request from actuator — id + method. Fire a task; never block the
        # read loop (C1).
        if msg_id is not None and method:
            asyncio.ensure_future(
                self._handle_request(msg_id, method, msg.get("params"), msg.get("correlation_id"))
            )
            return
        # Notification — method, no id (W5: no response). Enqueue for the
        # single ordered pump.
        if msg_id is None and method:
            self._notify_queue.put_nowait((method, msg.get("params"), msg.get("correlation_id")))

    async def _invoke2(self, fn: Callable, event_type: str, params: Any) -> Any:
        """`_invoke` for the two-argument pattern-listener shape."""
        if inspect.iscoroutinefunction(fn):
            return await fn(event_type, params)
        result = await asyncio.to_thread(fn, event_type, params)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _invoke(self, fn: Callable, params: Any) -> Any:
        """Dual-handler dispatch: `async def` runs on the loop; plain `def`
        is offloaded to a thread so a blocking body cannot freeze every
        other handler (contextvars — including the ambient correlation —
        propagate into the thread)."""
        if inspect.iscoroutinefunction(fn):
            return await fn(params)
        result = await asyncio.to_thread(fn, params)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _wait_ready_or_shutdown(self) -> None:
        ready = asyncio.ensure_future(self._ready.wait())
        stop = asyncio.ensure_future(self._shutdown_event.wait())
        try:
            await asyncio.wait({ready, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
            stop.cancel()

    async def _handle_request(self, msg_id, method: str, params: Any, correlation_id) -> None:
        # Hold until run() (L4) — or shutdown, so a signalled process
        # doesn't strand this task.
        await self._wait_ready_or_shutdown()
        if self._closed:
            self._send_error(msg_id, -1, "plugin shutting down")
            return
        handler = self._handlers.get(method)
        if handler is None:
            self._send_error(msg_id, -32601, f"method not found: {method}")
            return
        token = set_correlation(correlation_id)
        try:
            result = await self._invoke(handler, params)
            self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except Exception as e:  # exception recovery (C3)
            log(self._plugin_id, f"handler error for {method}: {e}")
            self._send_error(msg_id, -1, str(e))
        finally:
            reset_correlation(token)

    async def _drain_notifications(self) -> None:
        # Hold delivery until run() — same gate as requests, and the one
        # the Go SDK applies to both lanes. The queue is unbounded and
        # enqueue never blocks, so holding here is free.
        await self._wait_ready_or_shutdown()
        if self._closed:
            return
        while True:
            method, params, correlation_id = await self._notify_queue.get()
            listeners = self._listeners.get(method) or []
            patterned = [fn for pat, fn in self._pattern_listeners if matches_topic(pat, method)]
            if not listeners and not patterned:
                continue
            token = set_correlation(correlation_id)
            try:
                for fn in list(listeners):
                    try:
                        await self._invoke(fn, params)
                    except Exception as e:
                        log(self._plugin_id, f"listener error for {method}: {e}")
                # Exact listeners first, then pattern ones — a plugin with
                # both registered for the same event sees the specific handler
                # run before the catch-all, which is the order that reads
                # correctly.
                for fn in patterned:
                    try:
                        await self._invoke2(fn, method, params)
                    except Exception as e:
                        log(self._plugin_id, f"pattern listener error for {method}: {e}")
            finally:
                reset_correlation(token)


def api_version() -> str:
    """The BranchKit API version from the actuator (env var), falling back
    to the version this SDK was generated against."""
    return os.environ.get("BRANCHKIT_API_VERSION") or _COMPILED_API_VERSION


def plugin_dir() -> str:
    """The plugin's installation directory (BRANCHKIT_PLUGIN_DIR), falling
    back to "." when unset — what a plugin run by hand outside the actuator
    sees. Launch-contract surface, same class as api_version().

    The command loaders deliberately do NOT route through this: they
    distinguish unset ("not actuator-launched, load nothing") from a
    directory, and a "." fallback would have them scan the working
    directory for commands.json."""
    return os.environ.get("BRANCHKIT_PLUGIN_DIR") or "."


def plugin_data_dir() -> str:
    """The directory this plugin may read and write freely
    (BRANCHKIT_PLUGIN_DATA) — its OWN data namespace, shared with the
    stages it ships. Returns "" when unset (not launched by the actuator):
    a "." fallback would have a hand-run plugin write into its own source
    tree, silently."""
    return os.environ.get("BRANCHKIT_PLUGIN_DATA", "")


def models_dir() -> str:
    """The plugin's model namespace (BRANCHKIT_MODELS_DIR) — where the CLI
    provisions the models this plugin declares in `provides.models`.
    READ-ONLY; "" when unset."""
    return os.environ.get("BRANCHKIT_MODELS_DIR", "")

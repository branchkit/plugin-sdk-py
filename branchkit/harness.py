"""Plugin-author test harness client — drives `branchkit-test-harness`
(a real actuator core in test mode) over stdio. Python twin of
harness.{go,ts}. Synchronous by design: it exists to be called from
plain `unittest` test bodies, which are not async.

    from branchkit.harness import Harness, harness_binary_available

    h = Harness.start("path/to/plugin-dir")
    try:
        result = h.must_simulate_command("click refresh")
    finally:
        h.stop()

Harness-backed integration tests should skip when
`harness_binary_available()` is false — they run where the binary is
built (the app-repo conformance context) and skip cleanly on a fresh
checkout."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Any


class HarnessError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class SimulateResult:
    """One simulate_command outcome. `matched`, `action`, `args`,
    `consumed_count`, `sets_tags`, `clears_tags`, `owner_plugin`,
    `action_response`, and `tied_candidates` (populated when the matcher
    declined because 2+ equally-eligible commands tied)."""

    def __init__(self, data: dict):
        self.matched: bool = bool(data.get("matched"))
        self.action = data.get("action")
        self.args: dict = data.get("args") or {}
        self.consumed_count = data.get("consumed_count")
        self.sets_tags: list = data.get("sets_tags") or []
        self.clears_tags: list = data.get("clears_tags") or []
        self.owner_plugin = data.get("owner_plugin")
        self.action_response = data.get("action_response")
        self.tied_candidates: list = data.get("tied_candidates") or []

    def action_type(self) -> str:
        if not isinstance(self.action, dict):
            return ""
        return self.action.get("action_type") or ""

    def action_params(self) -> Any:
        if not isinstance(self.action, dict):
            raise RuntimeError("no action in result")
        return self.action.get("params")


class Harness:
    def __init__(self, proc: subprocess.Popen):
        # Internal — use Harness.start().
        self._proc = proc
        self._next_id = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._responses: dict[int, dict] = {}
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @classmethod
    def start(cls, dir: str) -> "Harness":
        binary = find_harness_binary()
        proc = subprocess.Popen(
            [binary, "--server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
        h = cls(proc)
        h._call("test.start", {"dir": os.path.abspath(dir)})
        return h

    def __enter__(self) -> "Harness":
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.stop()
        except Exception:
            self._cleanup()

    # --- Lifecycle ---

    def stop(self) -> None:
        self._call("test.stop", {})
        self._cleanup()

    def reset(self) -> None:
        self._call("test.reset", {})

    def reload(self) -> None:
        self._call("test.reload", {})

    def load_manifest(self, dir_or_name: str) -> None:
        """Load a dependency plugin's manifest without spawning its binary.
        Accepts a local directory path or a plugin name."""
        abs_dir = os.path.abspath(dir_or_name)
        if os.path.exists(os.path.join(abs_dir, "plugin.json")):
            self._call("test.load_manifest", {"dir": abs_dir})
        else:
            self._call("test.load_manifest", {"name": dir_or_name})

    def load_plugin(self, dir_or_name: str) -> None:
        """Resolve, spawn, and run a dependency plugin in the harness; it
        shares state with the primary plugin."""
        abs_dir = os.path.abspath(dir_or_name)
        if os.path.exists(os.path.join(abs_dir, "plugin.json")):
            self._call("test.load_plugin", {"dir": abs_dir})
        else:
            self._call("test.load_plugin", {"name": dir_or_name})

    def resolve_deps(self) -> list[dict]:
        """Resolve all depends_on entries and report status."""
        return self._call("test.resolve_deps", {}).get("deps") or []

    # --- Tags ---

    def set_tag(self, tag: str) -> None:
        self._call("test.set_tag", {"tag": tag})

    def clear_tag(self, tag: str) -> None:
        self._call("test.clear_tag", {"tag": tag})

    def get_tags(self, pattern: str) -> list[str]:
        return self._call("test.get_tags", {"pattern": pattern}).get("tags") or []

    def require_tag(self, tag: str) -> None:
        if tag not in self.get_tags(tag):
            raise AssertionError(f'expected tag "{tag}" to be active, but it was not')

    def require_no_tag(self, tag: str) -> None:
        if tag in self.get_tags(tag):
            raise AssertionError(f'expected tag "{tag}" to NOT be active, but it was')

    # --- Commands / collections / events ---

    def simulate_command(self, phrase: str) -> SimulateResult:
        return SimulateResult(self._call("test.simulate_command", {"phrase": phrase}))

    def must_simulate_command(self, phrase: str) -> SimulateResult:
        result = self.simulate_command(phrase)
        if not result.matched:
            raise AssertionError(f'expected "{phrase}" to match a command, but it didn\'t')
        return result

    def get_collection(self, name: str) -> dict:
        return self._call("test.get_collection", {"name": name})

    def write_collection(self, name: str, data: Any, contributor: str | None = None) -> None:
        params: dict[str, Any] = {"name": name, "data": data}
        if contributor:
            params["contributor"] = contributor
        self._call("test.write_collection", params)

    def call_plugin(self, method: str, params: Any) -> Any:
        return self._call("test.call_plugin_method", {"method": method, "params": params})

    def get_plugin_state(self) -> dict:
        return self._call("test.get_plugin_state", {})

    def set_world(self, world: Any) -> None:
        self._call("test.set_world", world)

    def inject_event(self, event_type: str, data: Any) -> None:
        self._call("test.inject_event", {"event_type": event_type, "data": data})

    def get_hud(self, channel: str) -> dict:
        return self._call("test.get_hud", {"channel": channel})

    def list_hud_channels(self) -> list[dict]:
        return self._call("test.get_hud", {}).get("channels") or []

    def get_rpc_log(self) -> list[dict]:
        return self._call("test.get_rpc_log", {}).get("entries") or []

    # --- Conformance phases ---

    def run_static_analysis(self) -> dict:
        return self._call("test.run_static_analysis", {})

    def run_startup_check(self) -> dict:
        return self._call("test.run_startup_check", {})

    def run_rpc_contract(self) -> dict:
        return self._call("test.run_rpc_contract", {})

    def run_settings_check(self) -> dict:
        return self._call("test.run_settings_check", {})

    def run_dependency_check(self) -> dict:
        return self._call("test.run_dependency_check", {})

    def run_all(self) -> dict:
        return self._call("test.run_all", {})

    # --- Internal ---

    def _read_loop(self) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        for raw in stdout:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            if msg_id is None:
                continue
            with self._cond:
                self._responses[msg_id] = msg
                self._cond.notify_all()
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _call(self, method: str, params: Any, timeout: float = 30.0) -> Any:
        with self._lock:
            self._next_id += 1
            call_id = self._next_id
        line = json.dumps({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params})
        stdin = self._proc.stdin
        assert stdin is not None
        stdin.write((line + "\n").encode("utf-8"))
        stdin.flush()
        with self._cond:
            ok = self._cond.wait_for(
                lambda: call_id in self._responses or self._closed, timeout
            )
            if call_id in self._responses:
                msg = self._responses.pop(call_id)
            elif self._closed:
                raise HarnessError(-32000, "harness closed")
            elif not ok:
                raise HarnessError(-32000, f"timeout: {method} did not respond within {timeout:g}s")
        err = msg.get("error")
        if err:
            raise HarnessError(err.get("code", -1), err.get("message", ""))
        return msg.get("result")

    def _cleanup(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        self._proc.kill()
        self._proc.wait(timeout=5)


def harness_binary_available() -> bool:
    """True when the `branchkit-test-harness` binary can be located."""
    try:
        find_harness_binary()
        return True
    except OSError:
        return False


def find_harness_binary() -> str:
    env = os.environ.get("BRANCHKIT_TEST_HARNESS")
    if env:
        return env
    candidates = [
        "target/debug/branchkit-test-harness",
        "target/release/branchkit-test-harness",
        "../target/debug/branchkit-test-harness",
        "../target/release/branchkit-test-harness",
        "../../target/debug/branchkit-test-harness",
        "../../target/release/branchkit-test-harness",
    ]
    for c in candidates:
        abs_path = os.path.abspath(c)
        if os.path.exists(abs_path):
            return abs_path
    found = shutil.which("branchkit-test-harness")
    if found:
        return found
    raise OSError(
        "harness: cannot find branchkit-test-harness binary. "
        "Set BRANCHKIT_TEST_HARNESS or run 'cargo build -p branchkit-test-harness'"
    )

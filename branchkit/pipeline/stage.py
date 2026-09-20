"""The stage runtime — the layer between wire framing and a working stage.

``Reader``/``Writer`` get bytes on and off the pipe. This owns the
obligations above them that every stage otherwise hand-rolls, and that fail
SILENTLY when hand-rolled wrong:

- the capability handshake goes out first and unprompted,
- receiver-side flow credit follows a declared policy rather than a counter
  each stage maintains itself,
- unknown events are tolerated (wire leniency is contract, not courtesy),
- a fatal error becomes one BKLOG1 line and exit 1, the same way in every
  stage.

Which entry point
-----------------
Two, because there are two loop shapes:

- ``serve_audio_consumer`` — read-driven. The stage's work is a reaction to
  an inbound audio session. VAD gates, STT engines, command recognizers.
- ``serve_source`` — notifier-driven. The stage produces spontaneously from
  a device, OS notification, or timer, and may never read stdin at all.
  Power/display/location monitors, microphones.

An audio source is the second shape plus ``SourceOptions.listen_for_stop``.

Why one is media-neutral and the other is not
---------------------------------------------
``serve_source`` assumes nothing about what you emit. ``serve_audio_consumer``
is audio-bound because audio is the only STREAM the wire has — audio_start /
audio_chunk / audio_stop are the streaming events, everything else is
discrete, and flow credit counts audio frames. A runtime cannot be more
general than the protocol it speaks.

Flow credit: which side are you on
-----------------------------------
- Consuming audio → you must grant credit. ``CreditPolicy`` drives it.
- Producing audio → you must not implement credit at all. The platform holds
  the sender-side window; a producer that outruns it blocks on the pipe.
  There is deliberately no sender-side helper here.

Threads, not asyncio
--------------------
The rest of this SDK is asyncio at its core, because a plugin multiplexes
JSON-RPC. A stage does not: it is a subprocess with one blocking pipe in and
one out, and the Go and TS runtimes it must behave identically to are shaped
that way. Threads keep the port a port. A stage that wants asyncio internally
is free to run its own loop inside the body.

Port of ``stage.go`` / ``stage.ts``.
"""

import signal
import sys
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, BinaryIO, Callable, Optional

# The runtime references the audio session types in two places: the
# AudioConsumer callbacks, and the stop request a listen_for_stop source
# reads. Both are audio assumptions sitting in an otherwise domain-free
# runtime — visible here on purpose rather than hidden by a flat package.
from .audio import (
    EVENT_AUDIO_CHUNK,
    EVENT_AUDIO_START,
    EVENT_AUDIO_STOP,
    AudioChunk,
    AudioStart,
    AudioStop,
)
from .credit import CreditGranter
from .events_gen import EVENT_CAPABILITY, Capability
from .stagelog import log_error
from .wire import Event, Reader, Writer


def run(body: Callable[[], None]) -> None:
    """Execute a stage body as main, mapping a fatal error to one structured
    log line and exit code 1::

        if __name__ == "__main__":
            pipeline.run(main)
    """
    try:
        body()
    except Exception as exc:  # noqa: BLE001 — a stage's last line of defence
        log_error(f"fatal: {exc}")
        sys.exit(1)


class InitialGrant(Enum):
    """When the runtime emits the initial credit window.

    The NUMBERS are receiver-chosen buffering policy and stay at your call
    site; only the mechanism is branchkit. This captures the one structural
    difference between stages: whether the window opens before any session
    exists.
    """

    #: Emit the initial window right after the handshake with an empty
    #: session id — the stage buffers freely and wants upstream moving before
    #: any session exists.
    ON_START = "on_start"
    #: Emit it on every audio_start, stamped with that session (which also
    #: resets the cadence counter). Shallow-queue stages.
    ON_SESSION_START = "on_session_start"
    #: Emit nothing automatically — for a window that depends on runtime
    #: state, or a stage that consumes no audio and must never grant.
    MANUAL = "manual"


@dataclass
class CreditPolicy:
    """Receiver-side credit configuration."""

    initial: int = 0            # frames in the unconditional initial window
    every: int = 0              # grant again after every N processed chunks
    grant: int = 0              # frames per cadence grant
    when: InitialGrant = InitialGrant.ON_START


#: The policy for a stage that consumes no audio and must never grant.
NO_CREDIT = CreditPolicy(when=InitialGrant.MANUAL)


class Chunk(Enum):
    """Whether a delivered audio_chunk counts toward the credit cadence."""

    #: Processed — count it. The normal answer.
    COUNTED = "counted"
    #: Discarded without processing (a stale session id, an unsupported
    #: format), so it does not count.
    #:
    #: Note the open question this preserves rather than settles: a dropped
    #: chunk still spent a frame of the sender's window, so never counting it
    #: shrinks that window for the rest of the run.
    DROPPED = "dropped"


class Flow(Enum):
    """Whether the consumer loop continues after a callback."""

    CONTINUE = "continue"
    STOP = "stop"


class AudioCtx:
    """What a consumer callback is handed: the outbound writer, plus the
    credit granter wired to this stage's policy."""

    def __init__(self, w: Writer, credit: CreditGranter):
        self._lock = threading.Lock()
        self._w = w
        self._credit = credit

    def emit(self, event_type: str, data: Any) -> None:
        """Write one framed event."""
        with self._lock:
            self._w.write_typed(event_type, data)

    def emit_raw(self, event_type: str, data: Any, payload: bytes) -> None:
        """Write one framed event with a binary payload."""
        with self._lock:
            self._w.write_typed(event_type, data, payload)

    def grant_now(self, session_id: str, frames: int) -> None:
        """Emit credit unconditionally and reset the cadence counter, for
        windows the policy cannot express — a re-grant at an utterance
        boundary."""
        with self._lock:
            self._credit.grant_now(self._w, session_id, frames)


class AudioConsumer:
    """A read-driven stage. Subclass and override only what you care about.

    Go embeds a BaseConsumer for the same reason; in Python a base class with
    conformant defaults IS that idiom, so there is one class rather than an
    interface and a base.
    """

    def on_audio_start(self, ev: AudioStart, ctx: AudioCtx) -> None:
        """A session begins upstream."""

    def on_audio_chunk(self, ev: AudioChunk, payload: bytes,
                       ctx: AudioCtx) -> Chunk:
        """One frame of audio. Return Chunk.DROPPED to keep it out of the
        credit cadence."""
        return Chunk.COUNTED

    def on_audio_stop(self, ev: AudioStop, ctx: AudioCtx) -> Flow:
        """The session ends. A per_run stage emits its final result here and
        returns Flow.STOP."""
        return Flow.CONTINUE

    def on_other(self, ev: Event, ctx: AudioCtx) -> Flow:
        """Any event the runtime did not decode, including unknown types.
        Ignoring them is the default because wire leniency is contract."""
        return Flow.CONTINUE

    def on_eof(self, ctx: AudioCtx) -> None:
        """Clean EOF — upstream closed."""


def serve_audio_consumer(cap: Capability, policy: CreditPolicy,
                         handler: AudioConsumer) -> None:
    """Serve a read-driven stage on stdin/stdout."""
    serve_audio_consumer_on(sys.stdin.buffer, sys.stdout.buffer,
                            cap, policy, handler)


def serve_audio_consumer_on(r: BinaryIO, w: BinaryIO, cap: Capability,
                            policy: CreditPolicy,
                            handler: AudioConsumer) -> None:
    """``serve_audio_consumer`` over explicit transports. The stdio wrapper is
    what stages use; this exists so the runtime itself is testable over a
    pipe."""
    every = policy.every or 1
    reader = Reader(r)
    ctx = AudioCtx(Writer(w), CreditGranter(every, policy.grant))

    ctx.emit(EVENT_CAPABILITY, cap)
    if policy.when is InitialGrant.ON_START and policy.initial > 0:
        ctx.grant_now("", policy.initial)

    while True:
        ev = reader.read_event()
        if ev is None:
            handler.on_eof(ctx)
            return

        if ev.type == EVENT_AUDIO_START:
            start: AudioStart = ev.data or {}
            if policy.when is InitialGrant.ON_SESSION_START and policy.initial > 0:
                ctx.grant_now(start.get("session_id", ""), policy.initial)
            handler.on_audio_start(start, ctx)

        elif ev.type == EVENT_AUDIO_CHUNK:
            chunk: AudioChunk = ev.data or {}
            outcome = handler.on_audio_chunk(chunk, ev.payload, ctx)
            if outcome is Chunk.COUNTED and policy.every > 0:
                # Same lock the ctx emitters take: the cadence grant is an
                # emission, and a callback may be emitting from another thread.
                with ctx._lock:  # noqa: SLF001 — same object, not a reach-in
                    ctx._credit.on_chunk(ctx._w, chunk.get("session_id", ""))

        elif ev.type == EVENT_AUDIO_STOP:
            stop: AudioStop = ev.data or {}
            if handler.on_audio_stop(stop, ctx) is Flow.STOP:
                return

        else:
            if handler.on_other(ev, ctx) is Flow.STOP:
                return


@dataclass
class SourceOptions:
    """Configures ``serve_source``."""

    #: Watch stdin for the platform's audio_stop stop request and stop when it
    #: arrives (or on EOF, which means the platform is gone).
    #:
    #: This is what makes an audio source out of an event source: the runner
    #: ends a session by writing audio_stop to the source's stdin, and that
    #: stop may carry a cutoff_ms the source must forward verbatim on its own
    #: downstream audio_stop. Read it back with ``SourceCtx.stop_request()``.
    #:
    #: Off by default: an event source that never opens stdin is the common
    #: case, and turning this on for one would be a behavior change.
    listen_for_stop: bool = False


class SourceCtx:
    """A running source stage's handle: the outbound writer plus the stop
    signal."""

    def __init__(self, w: Writer):
        self._lock = threading.Lock()
        self._w = w
        self._done = threading.Event()
        self._stop_request: Optional[AudioStop] = None

    def emit(self, event_type: str, data: Any) -> None:
        with self._lock:
            self._w.write_typed(event_type, data)

    def emit_raw(self, event_type: str, data: Any, payload: bytes) -> None:
        with self._lock:
            self._w.write_typed(event_type, data, payload)

    def done(self) -> threading.Event:
        """Set when a stop is requested. Wait on it against your own event
        source — the Python counterpart of Go's ``<-ctx.Done()``."""
        return self._done

    def stopped(self) -> bool:
        """Has a stop been requested — for a producer loop that polls."""
        return self._done.is_set()

    def request_stop(self) -> None:
        """Request a stop from inside the stage."""
        self._done.set()

    def stop_request(self) -> Optional[AudioStop]:
        """The audio_stop that requested this stop, when the platform sent one
        and ``listen_for_stop`` is on. None if the stop came from a signal,
        from stdin EOF, or from ``request_stop()``.

        An audio source forwards this event's cutoff_ms verbatim on its own
        downstream audio_stop.
        """
        with self._lock:
            return self._stop_request


def serve_source(cap: Capability, opts: SourceOptions,
                 body: Callable[[SourceCtx], None]) -> None:
    """Serve a notifier-driven stage on stdout.

    Emits the capability handshake, installs a SIGTERM/SIGINT stop watcher
    (and the stop listener, per opts), then hands control to body. The stage
    owns its own loop — that is the point of this shape.
    """
    serve_source_on(sys.stdin.buffer, sys.stdout.buffer, cap, opts, body)


def serve_source_on(r: BinaryIO, w: BinaryIO, cap: Capability,
                    opts: SourceOptions,
                    body: Callable[[SourceCtx], None]) -> None:
    """``serve_source`` over explicit transports, for tests."""
    sc = SourceCtx(Writer(w))

    previous: list = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous.append((sig, signal.signal(sig, lambda *_: sc.request_stop())))
        except ValueError:
            # Not the main thread — a test driving the runtime directly. The
            # stop signal still works; only the OS handler is unavailable.
            pass

    try:
        sc.emit(EVENT_CAPABILITY, cap)
        if opts.listen_for_stop:
            threading.Thread(target=_watch_for_stop, args=(r, sc),
                             daemon=True).start()
        body(sc)
    finally:
        for sig, handler in previous:
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass


def _watch_for_stop(r: BinaryIO, sc: SourceCtx) -> None:
    """Read stdin until the platform's audio_stop arrives, or until EOF/error
    — which mean the platform is gone.

    Every other inbound event is ignored rather than fatal: a source stage
    must tolerate inbound bytes without dying, which the conformance source
    suite tests directly.
    """
    try:
        reader = Reader(r)
        while True:
            try:
                ev = reader.read_event()
            except Exception:  # noqa: BLE001 — malformed or dead pipe
                return
            if ev is None:
                return
            if ev.type != EVENT_AUDIO_STOP:
                continue
            if isinstance(ev.data, dict):
                with sc._lock:  # noqa: SLF001 — same module's own state
                    sc._stop_request = ev.data
            return
    finally:
        sc.request_stop()

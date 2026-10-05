"""The speech engine runtime — the third loop shape, request-driven.

Each ``speak`` request becomes an audio session the stage produces,
streaming, and stops the moment the platform cancels it. Text-to-speech
engines: the pipeline run the other way, from words to the speakers.

A speech engine produces audio, so it implements no flow credit (see
``stage.py``): the platform holds the window.

Port of ``speech.go`` and the Rust ``stage::serve_speech_engine``.
"""

import sys
import threading
import time
from collections import deque
from typing import Any, BinaryIO, Deque, Optional

from .audio import (
    EVENT_AUDIO_CHUNK,
    EVENT_AUDIO_START,
    EVENT_AUDIO_STOP,
    EVENT_SPEAK,
    AudioFormat,
    Speak,
)
from .events_gen import EVENT_CAPABILITY, EVENT_ERROR, Capability
from .stage import Flow
from .stagelog import log_warn
from .wire import Reader, Writer


def shared_clock_ms() -> int:
    """Milliseconds on the shared clock: the clock the platform stamps
    microphone audio with (``AudioChunk.timestamp_ms``), and so the clock a
    recognizer's word onsets are on.

    ``CLOCK_UPTIME_RAW`` on macOS (the uptime clock the macOS app stamps its
    microphone with), ``CLOCK_MONOTONIC`` on other unixes, wall time on
    Windows, where every producer on the machine uses it. Stamps from
    different processes on one machine are comparable.

    An audio sink stamps playback_started / playback_ended with it, which is
    how the platform drops BranchKit's own voice coming back through the
    microphone.
    """
    if sys.platform == "darwin":
        return time.clock_gettime_ns(time.CLOCK_UPTIME_RAW) // 1_000_000
    if hasattr(time, "CLOCK_MONOTONIC") and sys.platform != "win32":
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1_000_000
    return time.time_ns() // 1_000_000


class SpeakCtx:
    """What ``SpeechEngine.speak`` is handed: where the utterance's audio
    goes, and whether it has been cancelled."""

    def __init__(self, w: Writer, lock: threading.Lock, session_id: str,
                 cancelled: threading.Event):
        self._w = w
        self._lock = lock
        self._session_id = session_id
        self._cancelled = cancelled
        self._started = False

    @property
    def session_id(self) -> str:
        """The utterance this context belongs to."""
        return self._session_id

    def cancelled(self) -> bool:
        """Has the platform cancelled this utterance? Stop synthesizing when
        it has: nothing more of it will be sent."""
        return self._cancelled.is_set()

    def done(self) -> threading.Event:
        """Set when the utterance is cancelled — wait on it, or hand it to
        synthesis running on another thread."""
        return self._cancelled

    def start(self, fmt: AudioFormat) -> None:
        """Open the utterance's audio in ``fmt``. Call it once, before the
        first ``audio``. Engines usually know their format only once the model
        is loaded, which is why it is given here and not in the capability."""
        if self._started:
            raise RuntimeError("speech engine: start called twice for one utterance")
        self._started = True
        if self.cancelled():
            return
        with self._lock:
            self._w.write_typed(EVENT_AUDIO_START,
                                {"session_id": self._session_id, "format": fmt})

    def audio(self, pcm: bytes) -> Flow:
        """Send one chunk of audio, in the format given to ``start``, as soon
        as it is synthesized. Send it in pieces as the engine makes them,
        never the whole utterance at the end, so the first words play while
        the rest are made.

        Returns ``Flow.STOP`` once the utterance is cancelled, without sending
        anything: stop synthesizing and return. The runtime closes the
        utterance either way.
        """
        if not self._started:
            raise RuntimeError("speech engine: audio before start")
        if self.cancelled():
            return Flow.STOP
        with self._lock:
            self._w.write_typed(
                EVENT_AUDIO_CHUNK,
                {"session_id": self._session_id, "timestamp_ms": shared_clock_ms()},
                pcm,
            )
        return Flow.CONTINUE


class SpeechEngine:
    """A text-to-speech stage (stage_type "tts"). Subclass and implement
    ``speak``.

    The engine implements one thing, how to say one request; the runtime owns
    the rest of the contract — the handshake, the order requests are spoken
    in, cancellation, and closing every utterance with exactly one
    audio_stop, including one that failed or was cancelled before it began.
    """

    def speak(self, req: Speak, ctx: SpeakCtx) -> None:
        """Say ``req``: call ``ctx.start`` once with the audio format, then
        ``ctx.audio`` for each piece as it is synthesized, and return when the
        utterance is done or ``audio`` says ``Flow.STOP``.

        An exception fails this utterance only: the runtime sends an error
        event for it, closes it, and goes on to the next. A failure that makes
        the engine unusable belongs before ``serve_speech_engine`` (a model
        that does not load), where ``run`` turns it into exit 1.
        """
        raise NotImplementedError


def serve_speech_engine(cap: Capability, engine: SpeechEngine) -> None:
    """Serve a speech engine on stdin/stdout."""
    serve_speech_engine_on(sys.stdin.buffer, sys.stdout.buffer, cap, engine)


def serve_speech_engine_on(r: BinaryIO, w: BinaryIO, cap: Capability,
                           engine: SpeechEngine) -> None:
    """``serve_speech_engine`` over explicit transports, for tests."""
    lock = threading.Lock()
    writer = Writer(w)
    with lock:
        writer.write_typed(EVENT_CAPABILITY, cap)

    # The utterance being spoken, so a cancel for it reaches the engine while
    # speak is still running rather than after it returns.
    current: list = [None]  # (session_id, threading.Event) or None
    cur_lock = threading.Lock()

    # Unbounded on purpose: the reader must never wait on a busy engine, or a
    # cancel for the utterance being spoken would wait behind the requests
    # queued after it.
    inbox: Deque[tuple] = deque()
    wake = threading.Condition()
    gone = [False]

    def post(item: tuple) -> None:
        with wake:
            inbox.append(item)
            wake.notify()

    def read_loop() -> None:
        reader = Reader(r)
        try:
            while True:
                try:
                    ev = reader.read_event()
                except Exception:  # noqa: BLE001 — malformed or dead pipe
                    return
                if ev is None:
                    return
                data = ev.data if isinstance(ev.data, dict) else None
                if ev.type == EVENT_SPEAK:
                    if (data is None or not isinstance(data.get("session_id"), str)
                            or not isinstance(data.get("text"), str)):
                        log_warn("speak: undecodable request")
                        continue
                    post(("speak", data))
                elif ev.type == EVENT_AUDIO_STOP and data is not None:
                    sid = data.get("session_id")
                    with cur_lock:
                        cur = current[0]
                        if cur is not None and cur[0] == sid:
                            cur[1].set()
                    post(("cancel", sid))
        finally:
            # EOF or a broken pipe: the platform is gone, and whatever is
            # being said will not be heard.
            with cur_lock:
                if current[0] is not None:
                    current[0][1].set()
            with wake:
                gone[0] = True
                wake.notify()

    threading.Thread(target=read_loop, daemon=True).start()

    def close_utterance(session_id: str) -> None:
        with lock:
            writer.write_typed(EVENT_AUDIO_STOP, {"session_id": session_id})

    queue: Deque[Any] = deque()

    def handle(item: tuple) -> None:
        kind, value = item
        if kind == "speak":
            queue.append(value)
            return
        # Queued and not yet begun: close it now. A cancel for the utterance
        # being spoken already went through its event, and one for an id no
        # longer known is moot.
        for req in list(queue):
            if req.get("session_id") == value:
                queue.remove(req)
                close_utterance(value)
                return

    while True:
        # Take everything that has arrived before choosing what to say next,
        # so a cancel already sent for a queued utterance is honored before
        # it starts.
        with wake:
            while not inbox and not gone[0] and not queue:
                wake.wait()
            items = list(inbox)
            inbox.clear()
            if gone[0]:
                return
        for item in items:
            handle(item)
        if not queue:
            continue

        req = queue.popleft()
        sid = req["session_id"]
        cancelled = threading.Event()
        with cur_lock:
            current[0] = (sid, cancelled)
        failure: Optional[BaseException] = None
        try:
            engine.speak(req, SpeakCtx(writer, lock, sid, cancelled))
        except Exception as e:  # noqa: BLE001 — fails this utterance only
            failure = e
        finally:
            with cur_lock:
                current[0] = None
        if failure is not None:
            with lock:
                writer.write_typed(EVENT_ERROR, {
                    "session_id": sid,
                    "code": "speak_failed",
                    "message": str(failure),
                    "fatal": False,
                })
        close_utterance(sid)

"""The speech engine runtime: one speak request in, one audio session out,
closed by exactly one audio_stop whatever happened to it."""

import io
import os
import threading
import time
import unittest

from branchkit.pipeline import (
    Event,
    Flow,
    Reader,
    SpeechEngine,
    Writer,
    serve_speech_engine_on,
    shared_clock_ms,
)

PCM16 = {"rate": 16000, "width": 2, "channels": 1}
CAP = {"stage_type": "tts", "stage_name": "test", "lifecycle_modes": ["persistent"]}


class WordEngine(SpeechEngine):
    """Speaks one 4-byte chunk per word, pausing between chunks the way
    synthesis would. A word "fail" fails the utterance after its chunk."""

    def __init__(self):
        self.spoken = []

    def speak(self, req, ctx):
        self.spoken.append(req["session_id"])
        ctx.start(PCM16)
        for word in req["text"].split():
            if ctx.audio(b"\0\0\0\0") is Flow.STOP:
                return
            if word == "fail":
                raise RuntimeError("engine broke")
            time.sleep(0.005)


def speak(session, text):
    return Event("speak", {"session_id": session, "text": text})


def stop(session):
    return Event("audio_stop", {"session_id": session})


def run_engine(engine, events, hold_open=0.2):
    """Serve engine with events written up front; stdin stays open for
    hold_open seconds (so queued utterances get spoken), then EOF."""
    rfd, wfd = os.pipe()
    r = os.fdopen(rfd, "rb")
    w = os.fdopen(wfd, "wb")

    def feed():
        writer = Writer(w)
        for ev in events:
            writer.write_event(ev)
        time.sleep(hold_open)
        w.close()

    threading.Thread(target=feed, daemon=True).start()
    out = io.BytesIO()
    serve_speech_engine_on(r, out, CAP, engine)
    out.seek(0)
    reader = Reader(out)
    evs = []
    while True:
        ev = reader.read_event()
        if ev is None:
            return evs
        evs.append(ev)


def for_session(evs, session):
    return [e.type for e in evs
            if isinstance(e.data, dict) and e.data.get("session_id") == session]


class TestSpeechEngine(unittest.TestCase):
    def test_utterance_is_start_chunks_stop_and_never_credit(self):
        out = run_engine(WordEngine(), [speak("u1", "snap left now")])
        self.assertEqual(out[0].type, "capability")
        self.assertEqual(for_session(out, "u1"), [
            "audio_start", "audio_chunk", "audio_chunk", "audio_chunk", "audio_stop"])
        self.assertNotIn("flow_credit", [e.type for e in out])
        chunk = next(e for e in out if e.type == "audio_chunk")
        self.assertEqual(len(chunk.payload), 4)

    def test_spoken_in_arrival_order(self):
        e = WordEngine()
        run_engine(e, [speak("a", "one"), speak("b", "two")])
        self.assertEqual(e.spoken, ["a", "b"])

    def test_cancel_stops_the_utterance_in_progress(self):
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        stage_in = os.fdopen(in_r, "rb")
        stage_out = os.fdopen(out_w, "wb")

        def serve():
            serve_speech_engine_on(stage_in, stage_out, CAP, WordEngine())
            stage_out.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        w = Writer(os.fdopen(in_w, "wb"))
        reader = Reader(os.fdopen(out_r, "rb"))
        w.write_event(speak("long", " ".join(["word"] * 200)))
        while reader.read_event().type != "audio_chunk":
            pass
        w.write_event(stop("long"))
        after = []
        while True:
            ev = reader.read_event()
            after.append(ev.type)
            if ev.type == "audio_stop":
                break
        self.assertLess(len(after), 10, after)
        w._s.close()  # noqa: SLF001 — EOF ends the stage
        rest = []
        while True:
            ev = reader.read_event()
            if ev is None:
                break
            rest.append(ev)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(for_session(rest, "long"), [])

    def test_queued_utterance_cancelled_before_it_begins_is_closed_unspoken(self):
        e = WordEngine()
        out = run_engine(e, [speak("a", "one two three four"), speak("b", "never"),
                             stop("b")], hold_open=0.3)
        self.assertEqual(e.spoken, ["a"])
        self.assertEqual(for_session(out, "b"), ["audio_stop"])

    def test_failed_utterance_reports_closes_and_the_next_plays(self):
        out = run_engine(WordEngine(), [speak("bad", "fail here"), speak("good", "fine")])
        self.assertEqual(for_session(out, "bad"),
                         ["audio_start", "audio_chunk", "error", "audio_stop"])
        self.assertEqual(for_session(out, "good"),
                         ["audio_start", "audio_chunk", "audio_stop"])

    def test_unknown_and_malformed_inbound_is_ignored(self):
        e = WordEngine()
        out = run_engine(e, [Event("ext.acme.thing", {"a": 1}),
                             Event("speak", {"no": "text"}), speak("ok", "hello")])
        self.assertEqual(e.spoken, ["ok"])
        self.assertEqual(for_session(out, "ok"),
                         ["audio_start", "audio_chunk", "audio_stop"])

    def test_shared_clock_moves_forward(self):
        a = shared_clock_ms()
        time.sleep(0.005)
        self.assertGreaterEqual(shared_clock_ms(), a + 4)


if __name__ == "__main__":
    unittest.main()

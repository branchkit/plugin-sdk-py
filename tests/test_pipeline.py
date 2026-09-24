"""Pipeline port: framing, credit cadence, structured logging.

The framing assertions are byte-exact on purpose. The wire contract is a
byte contract — the conformance suite compares the Go, TS and Python ports
against shared echo fixtures — so a test that only checked round-tripping
would pass on a port no other implementation could read.
"""

import io
import unittest

from branchkit.pipeline import CreditGranter, Event, Reader, WireError, Writer
from branchkit.pipeline import stagelog


def write_all(events):
    buf = io.BytesIO()
    w = Writer(buf)
    for ev in events:
        w.write_event(ev)
    return buf.getvalue()


class TestFraming(unittest.TestCase):
    def test_header_is_byte_exact(self):
        # Verified identical to the Go port's output for these same events.
        self.assertEqual(
            write_all([Event("audio_chunk", {"session_id": "s1"}, b"\x00\x01\x02")]),
            b'{"type":"audio_chunk","data":{"session_id":"s1"},'
            b'"payload_length":3}\n\x00\x01\x02',
        )

    def test_empty_data_is_omitted(self):
        # None, {} and a missing field must all produce the same header, or
        # an event decoded from a lenient peer re-serializes non-canonically.
        for data in (None, {}):
            self.assertEqual(write_all([Event("audio_stop", data)]),
                             b'{"type":"audio_stop"}\n')

    def test_no_payload_length_when_empty(self):
        self.assertNotIn(b"payload_length", write_all([Event("ping")]))

    def test_non_ascii_is_utf8_not_escaped(self):
        # Go emits UTF-8 directly; \u-escaping here would differ byte-wise.
        self.assertEqual(write_all([Event("t", {"s": "héllo → 世界"})]),
                         '{"type":"t","data":{"s":"héllo → 世界"}}\n'.encode())

    def test_round_trip_with_payload(self):
        raw = write_all([
            Event("a", {"x": 1}, b"\xde\xad"),
            Event("b"),
        ])
        got = list(Reader(io.BytesIO(raw)))
        self.assertEqual([e.type for e in got], ["a", "b"])
        self.assertEqual(got[0].payload, b"\xde\xad")
        self.assertEqual(got[0].data, {"x": 1})
        self.assertEqual(got[1].payload, b"")

    def test_clean_eof_is_none(self):
        self.assertIsNone(Reader(io.BytesIO(b"")).read_event())

    def test_truncated_header_is_not_eof(self):
        # Truncation must not read as an orderly close.
        with self.assertRaises(WireError):
            Reader(io.BytesIO(b'{"type":"a"}')).read_event()

    def test_short_payload_is_refused(self):
        with self.assertRaises(WireError):
            Reader(io.BytesIO(
                b'{"type":"a","payload_length":8}\nshort')).read_event()

    def test_payload_cap(self):
        with self.assertRaises(WireError):
            Reader(io.BytesIO(
                b'{"type":"a","payload_length":99999999}\n')).read_event()

    def test_bad_header_is_wire_error(self):
        with self.assertRaises(WireError):
            Reader(io.BytesIO(b"not json\n")).read_event()


def echo(raw: bytes) -> bytes:
    """Read every frame in ``raw`` and re-emit it: what a pass-through stage
    and the framing conformance fixture both do."""
    return write_all(list(Reader(io.BytesIO(raw))))


class TestDataBytePreservation(unittest.TestCase):
    """Go keeps ``data`` as json.RawMessage, so an echoed frame keeps its
    exact bytes. Python parses, and re-serialising changed these values."""

    CASES = [
        b'{"type":"t","data":{"v":1e-7}}\n',        # was 1e-07
        b'{"type":"t","data":{"v":1e16}}\n',        # was 1e+16
        b'{"type":"t","data":{"v":1.10}}\n',        # was 1.1
        b'{"type":"t","data":{"s":"\\u00e9"}}\n',   # was raw \xc3\xa9
        b'{"type":"t","data":{"s":"\\ud800"}}\n',   # raised UnicodeEncodeError
        b'{"type":"t","data":{"v":[1E+2,-0,0.0,"<&>"]}}\n',
        b'{"type":"t","data":{"v":1e-7},"payload_length":2}\nab',
    ]

    def test_echo_is_byte_identical(self):
        for raw in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(echo(raw), raw)

    def test_parsed_value_still_exposed(self):
        ev = Reader(io.BytesIO(self.CASES[0])).read_event()
        self.assertEqual(ev.data, {"v": 1e-7})
        self.assertEqual(ev.raw_data, b'{"v":1e-7}')

    def test_whitespace_compacted_like_go(self):
        # Go's encoder compacts a RawMessage; string contents are untouched.
        self.assertEqual(
            echo(b'{ "type" : "t" , "data" : { "v" : 1e-7 , "s" : "a b" } }\n'),
            b'{"type":"t","data":{"v":1e-7,"s":"a b"}}\n')

    def test_edited_data_is_reserialised(self):
        ev = Reader(io.BytesIO(b'{"type":"t","data":{"v":1e-7}}\n')).read_event()
        ev.data["v"] = 2
        self.assertEqual(write_all([ev]), b'{"type":"t","data":{"v":2}}\n')

    def test_type_swap_is_an_edit(self):
        # True == 1 in Python; the raw bytes must not mask that swap.
        ev = Reader(io.BytesIO(b'{"type":"t","data":{"v":1}}\n')).read_event()
        ev.data["v"] = True
        self.assertEqual(write_all([ev]), b'{"type":"t","data":{"v":true}}\n')

    def test_constructed_lone_surrogate_does_not_crash(self):
        # Go's decoder maps a lone surrogate to U+FFFD; the writer does the
        # same instead of raising UnicodeEncodeError.
        self.assertEqual(write_all([Event("t", {"s": "a\ud800b"})]),
                         '{"type":"t","data":{"s":"a\ufffdb"}}\n'.encode())

    def test_invalid_utf8_in_data_passes_through(self):
        # Go copies a RawMessage without validating UTF-8.
        raw = b'{"type":"t","data":{"s":"\xff"}}\n'
        self.assertEqual(echo(raw), raw)

    def test_empty_data_still_omitted(self):
        self.assertEqual(echo(b'{"type":"t","data":{ }}\n'), b'{"type":"t"}\n')

    def test_malformed_still_rejected(self):
        for raw in (b'{"type":"t",}\n', b'{"type":"t"} x\n', b'[1]\n',
                    b'{"type":"t","data":{"v":01}}\n'):
            with self.subTest(raw=raw), self.assertRaises(WireError):
                Reader(io.BytesIO(raw)).read_event()


class TestCredit(unittest.TestCase):
    def test_cadence(self):
        buf = io.BytesIO()
        w = Writer(buf)
        g = CreditGranter(every=3, grant=10)
        g.grant_now(w, "s1", 5)
        for _ in range(7):
            g.on_chunk(w, "s1")
        frames = [int(line.split(b'"frames":')[1].rstrip(b"}"))
                  for line in buf.getvalue().splitlines()]
        # initial window, then one grant per three chunks (7 -> 2)
        self.assertEqual(frames, [5, 10, 10])

    def test_every_zero_never_grants(self):
        buf = io.BytesIO()
        g = CreditGranter(every=0, grant=10)
        for _ in range(100):
            g.on_chunk(Writer(buf), "s1")
        self.assertEqual(buf.getvalue(), b"")

    def test_counter_survives_sessions(self):
        # Deliberate: a per-session reset comes from grant_now, not from here.
        buf = io.BytesIO()
        w = Writer(buf)
        g = CreditGranter(every=3, grant=1)
        g.on_chunk(w, "s1")
        g.on_chunk(w, "s1")
        g.on_chunk(w, "s2")
        self.assertIn(b'"session_id":"s2"', buf.getvalue())


class TestStageLog(unittest.TestCase):
    def setUp(self):
        stagelog.clear_log_session()
        self.addCleanup(stagelog.clear_log_session)

    def capture(self, fn):
        import sys
        buf = io.StringIO()
        old, sys.stderr = sys.stderr, buf
        try:
            fn()
        finally:
            sys.stderr = old
        return buf.getvalue()

    def test_prefix_and_session(self):
        stagelog.set_log_session("sess-42")
        self.assertEqual(self.capture(lambda: stagelog.log_warn("hi")),
                         "BKLOG1\twarn\tsess-42\thi\n")

    def test_no_session_leaves_field_empty(self):
        self.assertEqual(self.capture(lambda: stagelog.log_info("hi")),
                         "BKLOG1\tinfo\t\thi\n")

    def test_newlines_flattened_to_one_line(self):
        out = self.capture(lambda: stagelog.log_error("a\nb\rc"))
        self.assertEqual(out, "BKLOG1\terror\t\ta b c\n")
        self.assertEqual(out.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()


class TestAudioConsumerRuntime(unittest.TestCase):
    """The obligations the runtime owns, each of which fails silently when a
    stage hand-rolls it."""

    def drive(self, events, policy, handler):
        from branchkit.pipeline.stage import serve_audio_consumer_on
        inb = io.BytesIO()
        w = Writer(inb)
        for e in events:
            w.write_event(e)
        inb.seek(0)
        out = io.BytesIO()
        serve_audio_consumer_on(inb, out, {"stage_type": "filter"},
                                policy, handler)
        out.seek(0)
        return list(Reader(out))

    def test_handshake_goes_out_first_and_unprompted(self):
        from branchkit.pipeline.stage import AudioConsumer, NO_CREDIT
        out = self.drive([], NO_CREDIT, AudioConsumer())
        self.assertEqual(out[0].type, "capability")
        self.assertEqual(out[0].data, {"stage_type": "filter"})

    def test_initial_window_then_cadence(self):
        from branchkit.pipeline.stage import (
            AudioConsumer, CreditPolicy, InitialGrant)
        out = self.drive(
            [Event("audio_chunk", {"session_id": "s1"}, b"ab")] * 4,
            CreditPolicy(initial=4, every=2, grant=8,
                         when=InitialGrant.ON_START),
            AudioConsumer())
        self.assertEqual([e.data["frames"] for e in out[1:]], [4, 8, 8])

    def test_session_start_grant_is_stamped_with_the_session(self):
        from branchkit.pipeline.stage import (
            AudioConsumer, CreditPolicy, InitialGrant)
        out = self.drive([Event("audio_start", {"session_id": "s9"})],
                         CreditPolicy(initial=3,
                                      when=InitialGrant.ON_SESSION_START),
                         AudioConsumer())
        self.assertEqual(out[1].data, {"session_id": "s9", "frames": 3})

    def test_no_credit_never_grants(self):
        from branchkit.pipeline.stage import AudioConsumer, NO_CREDIT
        out = self.drive([Event("audio_chunk", {"session_id": "s1"}, b"x")],
                         NO_CREDIT, AudioConsumer())
        self.assertEqual([e.type for e in out[1:]], [])

    def test_dropped_chunk_does_not_count(self):
        from branchkit.pipeline.stage import (
            AudioConsumer, Chunk, CreditPolicy, InitialGrant)

        class Dropper(AudioConsumer):
            def on_audio_chunk(self, ev, payload, ctx):
                return Chunk.DROPPED

        out = self.drive(
            [Event("audio_chunk", {"session_id": "s1"}, b"x")] * 5,
            CreditPolicy(every=2, grant=8, when=InitialGrant.MANUAL),
            Dropper())
        self.assertEqual([e.type for e in out[1:]], [])

    def test_unknown_events_are_tolerated(self):
        # Wire leniency is contract, not courtesy.
        from branchkit.pipeline.stage import AudioConsumer, NO_CREDIT

        class Rec(AudioConsumer):
            def __init__(self):
                self.other = []

            def on_other(self, ev, ctx):
                from branchkit.pipeline.stage import Flow
                self.other.append(ev.type)
                return Flow.CONTINUE

        h = Rec()
        self.drive([Event("ext.vendor.weird", {"x": 1})], NO_CREDIT, h)
        self.assertEqual(h.other, ["ext.vendor.weird"])

    def test_flow_stop_ends_the_loop(self):
        from branchkit.pipeline.stage import AudioConsumer, Flow, NO_CREDIT

        class Stopper(AudioConsumer):
            def __init__(self):
                self.eof = False

            def on_audio_stop(self, ev, ctx):
                return Flow.STOP

            def on_eof(self, ctx):
                self.eof = True

        h = Stopper()
        self.drive([Event("audio_stop", {"session_id": "s1"}),
                    Event("audio_chunk", {"session_id": "s1"}, b"never")],
                   NO_CREDIT, h)
        self.assertFalse(h.eof, "loop should have returned before EOF")


class TestSourceRuntime(unittest.TestCase):
    def serve(self, inbound_events, opts, body):
        from branchkit.pipeline.stage import serve_source_on
        inb = io.BytesIO()
        w = Writer(inb)
        for e in inbound_events:
            w.write_event(e)
        inb.seek(0)
        out = io.BytesIO()
        serve_source_on(inb, out, {"stage_type": "source"}, opts, body)
        out.seek(0)
        return list(Reader(out))

    def test_handshake_then_body_owns_the_loop(self):
        from branchkit.pipeline.stage import SourceOptions
        ran = []
        out = self.serve([], SourceOptions(), lambda sc: ran.append(True))
        self.assertEqual(out[0].type, "capability")
        self.assertTrue(ran)

    def test_stop_request_carries_cutoff_for_verbatim_forwarding(self):
        from branchkit.pipeline.stage import SourceOptions
        got = {}

        def body(sc):
            sc.done().wait(timeout=2)
            got["req"] = sc.stop_request()

        self.serve([Event("audio_stop", {"session_id": "s1",
                                         "cutoff_ms": 250})],
                   SourceOptions(listen_for_stop=True), body)
        self.assertEqual(got["req"]["cutoff_ms"], 250)

    def test_listen_for_stop_off_ignores_the_same_bytes(self):
        from branchkit.pipeline.stage import SourceOptions
        got = {}

        def body(sc):
            got["stopped"] = sc.stopped()

        self.serve([Event("audio_stop", {"session_id": "s1"})],
                   SourceOptions(), body)
        self.assertFalse(got["stopped"])

    def test_internal_stop_records_no_request(self):
        from branchkit.pipeline.stage import SourceOptions
        got = {}

        def body(sc):
            sc.request_stop()
            got["stopped"] = sc.stopped()
            got["req"] = sc.stop_request()

        self.serve([], SourceOptions(), body)
        self.assertTrue(got["stopped"])
        self.assertIsNone(got["req"])

    def test_unrelated_inbound_events_do_not_stop_a_source(self):
        # Needs a pipe that STAYS OPEN: EOF legitimately stops a source (the
        # platform is gone), so a BytesIO of noise would stop it for the right
        # reason and prove nothing about tolerance.
        import os
        import time

        from branchkit.pipeline.stage import SourceOptions, serve_source_on

        rfd, wfd = os.pipe()
        r = os.fdopen(rfd, "rb", buffering=0)
        w = os.fdopen(wfd, "wb", buffering=0)
        self.addCleanup(r.close)
        Writer(w).write_event(Event("ext.v.noise", {"a": 1}))

        got = {}

        def body(sc):
            time.sleep(0.15)
            got["running"] = not sc.stopped()
            Writer(w).write_event(Event("audio_stop", {"session_id": "s1"}))
            sc.done().wait(timeout=2)
            got["stopped_after"] = sc.stopped()

        serve_source_on(r, io.BytesIO(), {"stage_type": "source"},
                        SourceOptions(listen_for_stop=True), body)
        w.close()
        self.assertTrue(got["running"], "noise must not stop a source")
        self.assertTrue(got["stopped_after"], "the real stop must still land")

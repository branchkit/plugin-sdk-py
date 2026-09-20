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

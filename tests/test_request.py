"""The request stage runtime: one reply per request, with its id, in
arrival order; an unreadable request is an error event and serving goes on."""

import io
import unittest

from branchkit.pipeline import Event, Reader, Writer, serve_requests_on

CAP = {"stage_type": "request", "stage_name": "test", "lifecycle_modes": ["persistent"]}


async def upper(body):
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise ValueError("no text")
    return {"text": text.upper()}


def run_requests(events):
    """Serve with the whole script written up front, then EOF."""
    inbound = io.BytesIO()
    w = Writer(inbound)
    for ev in events:
        w.write_event(ev)
    inbound.seek(0)
    out = io.BytesIO()
    serve_requests_on(inbound, out, CAP, upper)
    out.seek(0)
    reader = Reader(out)
    evs = []
    while True:
        ev = reader.read_event()
        if ev is None:
            return evs
        evs.append(ev)


def request(data):
    return Event("request", data)


class TestRequestStage(unittest.TestCase):
    def test_every_request_gets_one_reply_with_its_id_in_order(self):
        out = run_requests([
            request({"request_id": "a", "body": {"text": "one"}}),
            Event("vocabulary_update", {}),
            request({"request_id": "b", "body": {}}),
            request({"request_id": "c", "body": {"text": "three"}}),
        ])
        self.assertEqual(out[0].type, "capability")
        self.assertEqual([e.type for e in out[1:]], ["reply"] * 3)
        replies = [e.data for e in out[1:]]
        self.assertEqual(replies[0], {"request_id": "a", "body": {"text": "ONE"}})
        self.assertEqual(replies[1]["request_id"], "b")
        self.assertEqual(replies[1]["error"], "no text")
        self.assertNotIn("body", replies[1])
        self.assertEqual(replies[2]["body"], {"text": "THREE"})

    def test_an_unreadable_request_is_an_error_event_and_serving_continues(self):
        out = run_requests([
            request({"body": {"text": "no id"}}),
            request({"request_id": "z", "body": {"text": "ok"}}),
        ])
        self.assertEqual(out[1].type, "error")
        self.assertEqual(out[1].data["code"], "bad_request")
        self.assertFalse(out[1].data["fatal"])
        self.assertEqual(out[2].type, "reply")
        self.assertEqual(out[2].data["request_id"], "z")


if __name__ == "__main__":
    unittest.main()

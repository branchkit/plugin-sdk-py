"""The platform's topic conformance table, subscription column.

The copy in testdata/ is byte-identical to the platform's (a platform-side
gate holds it there), so these are the same answers the delivery gate is
tested against: a pattern listener routes exactly what delivery sends."""

import json
import pathlib
import unittest

from branchkit.topic import matches_topic

TABLE = pathlib.Path(__file__).parent / "testdata" / "topic-match-conformance.json"


class TestTopicConformance(unittest.TestCase):
    def test_subscription_column(self):
        cases = json.loads(TABLE.read_text(encoding="utf-8"))["cases"]
        failures = [
            f"{c['pattern']!r} vs {c['topic']!r} should be {c.get('subscription')}"
            for c in cases
            if not isinstance(c.get("subscription"), bool)
            or matches_topic(c["pattern"], c["topic"]) != c["subscription"]
        ]
        self.assertEqual(failures, [])
        # A truncated or stale copy must not pass by having nothing to say.
        self.assertGreaterEqual(len(cases), 40)
        self.assertGreaterEqual(sum("**" in c["pattern"] for c in cases), 10)

    def test_many_globstars_stay_polynomial(self):
        pattern = "**." * 40 + "z"
        topic = ".".join(["a"] * 400)
        self.assertFalse(matches_topic(pattern, topic))
        self.assertTrue(matches_topic(pattern, topic + ".z"))


if __name__ == "__main__":
    unittest.main()

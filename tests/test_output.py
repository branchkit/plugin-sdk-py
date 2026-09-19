import asyncio
import json
import unittest

import branchkit
from branchkit import (
    KNOWN_OUTPUT_KINDS,
    OUTPUT_KIND_CHOICES,
    OUTPUT_URGENCY_AMBIENT,
    dispatch_action,
    say_action,
)


class TestOutputHelpers(unittest.TestCase):
    def test_say_action_shape(self):
        self.assertEqual(say_action("snap left"), {"say": "snap left"})

    def test_dispatch_action_carries_params_only_when_given(self):
        self.assertEqual(
            dispatch_action("windows.desk", {"n": 2}),
            {"dispatch": "windows.desk", "params": {"n": 2}},
        )
        # `params` must be ABSENT, not None-valued, so JSON carries no key.
        self.assertEqual(json.dumps(dispatch_action("windows.close")), '{"dispatch": "windows.close"}')

    def test_vocabulary_is_generated_in_platform_order(self):
        self.assertEqual(KNOWN_OUTPUT_KINDS, ("choices", "mode", "outcome", "problem", "progress"))

    def test_output_state_carries_the_document_and_decodes_the_answer(self):
        plugin = branchkit.Plugin()
        sent = {}

        async def fake_call(method, params=None, timeout=None):
            sent["method"] = method
            sent["params"] = params
            return {"ok": True, "generation": 7, "meaning_changed": True}

        plugin.call = fake_call  # type: ignore[assignment]
        doc = {
            "channel": "discovery",
            "kind": OUTPUT_KIND_CHOICES,
            "title": "Commands",
            "phrase": "twelve commands",
            "sections": [
                {
                    "title": "Windows",
                    "items": [
                        {"id": "snap_left", "title": "snap left", "phrase": "snap left", "action": say_action("snap left")}
                    ],
                }
            ],
            "urgency": OUTPUT_URGENCY_AMBIENT,
            "locale": "en",
            "v": 1,
        }
        res = asyncio.run(plugin.output_state(doc))
        self.assertEqual(sent["method"], "output.state")
        self.assertEqual(sent["params"], {"state": doc})
        self.assertEqual(res, {"ok": True, "generation": 7, "meaning_changed": True})


if __name__ == "__main__":
    unittest.main()

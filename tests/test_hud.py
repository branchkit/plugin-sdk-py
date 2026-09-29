"""HUD push sugar: the exact `hud.push` request each helper sends — the
same assertions the TS SDK's hud suite makes. The fragment shape (a
morph into a target id) is what sizes a HUD window from its content; the
raw shape replaces the whole window and must carry `raw: true` with an
empty target."""

import unittest

import branchkit


class TestHudPush(unittest.IsolatedAsyncioTestCase):
    def _capturing_plugin(self):
        p = branchkit.Plugin()
        sent = []

        async def call(method, params=None, timeout=None):
            sent.append((method, params))
            return {}

        p.call = call
        return p, sent

    async def test_fragment_sends_the_morph_shape(self):
        p, sent = self._capturing_plugin()
        await p.hud_push_fragment("ch", "content", "<b>x</b>")
        self.assertEqual(
            sent,
            [("hud.push", {"channel": "ch", "fragments": [{"target_id": "content", "html": "<b>x</b>"}]})],
        )

    async def test_raw_sends_the_raw_shape(self):
        p, sent = self._capturing_plugin()
        await p.hud_push_raw("ch", "<div>y</div>")
        self.assertEqual(
            sent,
            [("hud.push", {"channel": "ch", "fragments": [{"target_id": "", "html": "<div>y</div>", "raw": True}]})],
        )


if __name__ == "__main__":
    unittest.main()

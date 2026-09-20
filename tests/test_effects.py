"""Effects: the displaced-owner filter.

`on_effect_displaced` is the only effects surface with logic of its own —
the rest are thin wrappers over generated calls. It filters on
`displaced_owner == plugin.id`, so the failure mode is silent in both
directions: a plugin never told it lost an effect, or one told about
somebody else's.

Written 2026-09-19, when the §4.6 parity table gained an Effects row and the
parity gate reported that only the TS SDK tested this. The cases mirror
plugin-sdk-ts/src/__tests__/effects.test.ts one for one.
"""

import os
import unittest

from branchkit import Plugin
from branchkit.contracts_gen import EVENT_EFFECT_DISPLACED

SELF = "test-plugin"


class TestOnEffectDisplaced(unittest.IsolatedAsyncioTestCase):
    def plugin_as_self(self):
        # BRANCHKIT_PLUGIN_ID is what `.id` reads, the way a real plugin is
        # told who it is; set it before construction as the platform does.
        os.environ["BRANCHKIT_PLUGIN_ID"] = SELF
        self.addCleanup(os.environ.pop, "BRANCHKIT_PLUGIN_ID", None)
        p = Plugin()
        seen = []
        p.on_effect_displaced(lambda evt: seen.append(evt))
        return p, seen

    async def deliver(self, p, params):
        for h in p._listeners.get(EVENT_EFFECT_DISPLACED, []):
            await h(params)

    async def test_delivers_when_this_plugin_is_displaced(self):
        p, seen = self.plugin_as_self()
        await self.deliver(p, {"effect": "audio.capture",
                               "new_owner": "other-plugin",
                               "displaced_owner": SELF})
        self.assertEqual(seen, [{"effect": "audio.capture",
                                 "new_owner": "other-plugin",
                                 "displaced_owner": SELF}])

    async def test_ignores_another_plugins_effect(self):
        p, seen = self.plugin_as_self()
        await self.deliver(p, {"effect": "audio.capture", "new_owner": "a",
                               "displaced_owner": "somebody-else"})
        self.assertEqual(seen, [])

    async def test_absent_new_owner_becomes_empty_string(self):
        # new_owner is Option<String> on the wire. Requiring a string dropped
        # real displacement events in TS — the plugin was never told it had
        # lost the effect.
        p, seen = self.plugin_as_self()
        await self.deliver(p, {"effect": "audio.capture", "new_owner": None,
                               "displaced_owner": SELF})
        await self.deliver(p, {"effect": "audio.capture",
                               "displaced_owner": SELF})
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(e["new_owner"] == "" for e in seen))

    async def test_drops_uninterpretable_payloads(self):
        # effect and displaced_owner are load-bearing: one says what was lost,
        # the other is what the filter keys on.
        #
        # NOTE a live cross-SDK divergence: Go DELIVERS the no-effect payload
        # with an empty Effect (its struct decode yields the zero value),
        # while Python and TypeScript both drop it. Two of three drop, so Go
        # is the outlier; recorded in DESIGN_PLUGIN_SDK_SPEC.md §4.6 rather
        # than settled here, because which behaviour is correct is a contract
        # question, not a porting one.
        p, seen = self.plugin_as_self()
        await self.deliver(p, {"new_owner": "a", "displaced_owner": SELF})
        await self.deliver(p, {"effect": "audio.capture", "new_owner": "a"})
        await self.deliver(p, None)
        await self.deliver(p, "not an object")
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()

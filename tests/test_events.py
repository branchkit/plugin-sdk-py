"""The typed event helpers (events_gen.py): one per platform event, tying
its name to its generated payload type. Delivered through the plugin's own
`_invoke`, so a sync listener is offloaded and an async one awaited, as the
pump does."""

import unittest

from branchkit import Plugin
from branchkit.contracts_gen import (
    EVENT_COLLECTION_UPDATED,
    EVENT_POWER_CHANGED,
    EVENT_SYSTEM_WILL_SLEEP,
)
from branchkit.events_gen import EventsMixin


class TestTypedEvents(unittest.IsolatedAsyncioTestCase):
    async def deliver(self, p, event, params):
        for h in p._listeners.get(event, []):
            await p._invoke(h, params)

    async def test_data_event_hands_over_its_payload(self):
        p = Plugin()
        seen = []
        p.on_collection_updated(lambda evt: seen.append(evt))
        await self.deliver(p, EVENT_COLLECTION_UPDATED, {"collection": "keyboard.keys"})
        self.assertEqual(seen, [{"collection": "keyboard.keys"}])

    async def test_decorator_form(self):
        p = Plugin()
        seen = []

        @p.on_collection_updated
        async def listener(evt):
            seen.append(evt["collection"])

        await self.deliver(p, EVENT_COLLECTION_UPDATED, {"collection": "a"})
        self.assertEqual(seen, ["a"])

    async def test_signal_and_empty_event_take_no_argument(self):
        p = Plugin()
        calls = []
        p.on_power_changed(lambda: calls.append("sync"))

        async def asleep():
            calls.append("async")

        p.on_system_will_sleep(asleep)
        await self.deliver(p, EVENT_POWER_CHANGED, {"on_battery": True})
        await self.deliver(p, EVENT_SYSTEM_WILL_SLEEP, None)
        self.assertEqual(calls, ["sync", "async"])

    def test_no_helper_shadows_another_plugin_member(self):
        # Go and TS refuse a duplicate member at compile time; a Python
        # mixin would silently shadow one (or be shadowed), so check here.
        helpers = {n for n in vars(EventsMixin) if n.startswith("on_")}
        self.assertGreater(len(helpers), 40)
        for base in Plugin.__mro__:
            if base in (EventsMixin, Plugin, object):
                continue
            clash = helpers & set(vars(base))
            self.assertFalse(clash, f"{base.__name__} also defines {sorted(clash)}")


if __name__ == "__main__":
    unittest.main()

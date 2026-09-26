"""CollectionMirror: on_change reports changes, not refetches.

A refetch that returns the data already held must not fire on_change, or a
consumer that rebuilds a view on every change does that work for nothing.
Populated, emptied, and a real change are each pinned here."""

import asyncio
import unittest

from branchkit.mirror import CollectionMirror


class FakePlugin:
    """Just enough of a Plugin for CollectionMirror.refresh."""

    def __init__(self):
        self.data = [{"letter": "a", "codeword": "arch"}]

    async def collection_get(self, name):
        return {"name": name, "data": self.data}


class TestMirrorChanges(unittest.TestCase):
    def test_identical_refetch_does_not_fire_on_change(self):
        plugin = FakePlugin()
        mirror = CollectionMirror(plugin, "alphabet")
        changes = []
        mirror.on_change(lambda: changes.append(1))

        async def run():
            await mirror.refresh()
            await mirror.refresh()
            self.assertEqual(len(changes), 1, "an identical refetch is not a change")
            plugin.data = [{"letter": "a", "codeword": "alpha"}]
            await mirror.refresh()
            self.assertEqual(len(changes), 2, "a real change fires")
            plugin.data = []
            await mirror.refresh()
            await mirror.refresh()
            self.assertEqual(len(changes), 3, "emptying fires once")
            self.assertEqual(mirror.raw(), [])

        asyncio.run(run())

    def test_a_type_change_is_a_change(self):
        # `1 == True` in Python; the mirror compares JSON text, so a value
        # changing from 1 to true is still reported.
        plugin = FakePlugin()
        plugin.data = [{"v": 1}]
        mirror = CollectionMirror(plugin, "flags")
        changes = []
        mirror.on_change(lambda: changes.append(1))

        async def run():
            await mirror.refresh()
            plugin.data = [{"v": True}]
            await mirror.refresh()
            self.assertEqual(len(changes), 2)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()

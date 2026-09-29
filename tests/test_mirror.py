"""CollectionMirror: on_change reports changes, not refetches.

A refetch that returns the data already held must not fire on_change, or a
consumer that rebuilds a view on every change does that work for nothing.
Populated, emptied, and a real change are each pinned here."""

import asyncio
import unittest

import branchkit
from branchkit.mirror import CollectionMirror
from branchkit.plugin import RpcCallError


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


def _stub_plugin(respond):
    """A real Plugin whose outbound `call` is answered by `respond(method,
    params)` — so the mirror runs through the real generated wrappers,
    listener registration and notification pump. `respond` may be async; an
    Exception it returns is raised."""
    p = branchkit.Plugin()

    async def call(method, params=None, timeout=None):
        result = respond(method, params)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, Exception):
            raise result
        return result

    p.call = call
    return p


def _get_reply(data):
    return {"name": "alphabet", "introducer": "voice", "merge": "authoritative", "data": data}


async def _until(pred, timeout=2.0):
    """Poll `pred` until true; fail loudly rather than sleep and hope."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition not reached within timeout")
        await asyncio.sleep(0.001)


class TestMirrorContract(unittest.IsolatedAsyncioTestCase):
    """The same contract the Go and TS mirror suites pin, case for case."""

    async def test_rpc_error_preserves_snapshot(self):
        fail = False
        p = _stub_plugin(lambda m, _: RpcCallError(-1, "backend down") if fail else _get_reply({"k": "v1"}))
        mirror = p.mirror_collection("alphabet")
        await mirror.refresh()
        fail = True
        with self.assertRaisesRegex(RpcCallError, "backend down"):
            await mirror.refresh()
        self.assertEqual(mirror.raw(), {"k": "v1"}, "a failed refetch must not clobber the snapshot")
        self.assertTrue(mirror.ready)

    async def test_empty_after_population_commits_and_notifies(self):
        # A populated mirror cannot be racing boot, so an empty read is the
        # source saying "I am now empty": commit it and tell on_change.
        # Swallowing it is how derived projections orphan.
        populated = True
        p = _stub_plugin(lambda m, _: _get_reply([{"letter": "a"}] if populated else []))
        mirror = p.mirror_collection("alphabet")
        changes = []
        mirror.on_change(lambda: changes.append(1))
        await mirror.refresh()
        self.assertEqual(len(changes), 1)
        populated = False
        await mirror.refresh()
        self.assertEqual(len(changes), 2)
        self.assertTrue(mirror.ready, "empty is a real state, not un-readiness")
        self.assertEqual(mirror.raw(), [])

    async def test_unpopulated_stays_not_ready(self):
        # The boot race: collection.get before the owner's first put returns
        # the empty sentinel ([], null, or no data at all). Not an error.
        for reply in (_get_reply([]), _get_reply(None), {"name": "alphabet"}):
            p = _stub_plugin(lambda m, _, r=reply: r)
            mirror = p.mirror_collection("alphabet")
            changes = []
            mirror.on_change(lambda: changes.append(1))
            await mirror.refresh()
            self.assertFalse(mirror.ready, reply)
            self.assertIsNone(mirror.raw(), reply)
            self.assertEqual(changes, [], reply)

    async def test_rapid_updates_refresh_in_wire_order(self):
        # Drives the REAL notification pump, because the thing under test is
        # the pump's await. Inverted latency is the discriminator: the FIRST
        # update's fetch is slow, the SECOND's instant. Serialized, v1 lands
        # then v2 (final v2); concurrent, the stale v1 lands last (final v1).
        gets = 0
        done = 0

        async def respond(method, _params):
            nonlocal gets, done
            self.assertEqual(method, "collection.get")
            gets += 1
            version = f"v{gets}"
            await asyncio.sleep(0.05 if gets == 1 else 0)
            done += 1
            return _get_reply({"k": version})

        p = _stub_plugin(respond)
        mirror = p.mirror_collection("alphabet")
        for _ in range(2):
            p._route_message(
                {"jsonrpc": "2.0", "method": "_platform.collection.updated", "params": {"collection": "alphabet"}}
            )
        p._ready.set()
        pump = asyncio.ensure_future(p._drain_notifications())
        try:
            await _until(lambda: done == 2)
        finally:
            pump.cancel()
        self.assertEqual(gets, 2)
        self.assertEqual(mirror.raw(), {"k": "v2"})

    async def test_on_ready_fetches_and_matching_update_refetches(self):
        version = "v1"
        gets = 0

        def respond(method, _params):
            nonlocal gets
            gets += 1
            return _get_reply({"k": version})

        p = _stub_plugin(respond)
        mirror = p.mirror_collection("alphabet")
        p._ready.set()
        pump = asyncio.ensure_future(p._drain_notifications())

        def send(method, params):
            p._route_message({"jsonrpc": "2.0", "method": method, "params": params})

        try:
            send("on_ready", {})
            await _until(lambda: mirror.raw() == {"k": "v1"})

            version = "v2"
            # A non-matching update is ignored; the matching one after it
            # refetches. Wire order means once the second has been handled,
            # the first has too — so gets == 2 proves the first fetched
            # nothing.
            send("_platform.collection.updated", {"collection": "other"})
            send("_platform.collection.updated", {"collection": "alphabet"})
            await _until(lambda: mirror.raw() == {"k": "v2"})
        finally:
            pump.cancel()
        self.assertEqual(gets, 2)


if __name__ == "__main__":
    unittest.main()

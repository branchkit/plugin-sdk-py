"""platform() and supports(): the profile on_ready carries, and the
question a plugin asks before calling."""

import unittest

import branchkit

PROFILE = {
    "os": "linux",
    "session": "sway",
    "host": "h",
    "unavailable": [{"op": "native.dock_position", "reason": "platform_no_analogue"}],
    "unobservable_events": [],
}


def deliver(p, method, params):
    """Every listener for `method`, in order — as the notification pump does."""
    for fn in p._listeners.get(method, []):
        fn(params)


class PlatformTest(unittest.IsolatedAsyncioTestCase):
    async def test_on_ready_profile_is_kept_and_seen_by_the_plugins_on_ready(self):
        p = branchkit.Plugin(detached=True)
        seen = []
        p.on_ready(lambda: seen.append((p.platform() or {}).get("session")))
        self.assertIsNone(p.platform())
        deliver(p, "on_ready", {"platform": PROFILE})
        self.assertEqual(seen, ["sway"])
        self.assertFalse(await p.supports("native.dock_position"))
        self.assertTrue(await p.supports("native.cpu_usage"))
        self.assertTrue(await p.supports("vendor.never_heard_of_it"))

    async def test_an_old_actuators_bare_on_ready_leaves_no_profile(self):
        p = branchkit.Plugin(detached=True)
        deliver(p, "on_ready", {})
        self.assertIsNone(p.platform())
        # Detached, so the fetch raises: the call itself will say.
        self.assertTrue(await p.supports("native.dock_position"))

    async def test_before_on_ready_supports_fetches_once(self):
        p = branchkit.Plugin(detached=True)
        fetches = []

        async def call(method, params=None, **kw):
            fetches.append(method)
            return PROFILE

        p.call = call
        self.assertFalse(await p.supports("native.dock_position"))
        self.assertTrue(await p.supports("native.cpu_usage"))
        self.assertEqual(fetches, ["platform.profile"])
        self.assertEqual(p.platform()["os"], "linux")


if __name__ == "__main__":
    unittest.main()

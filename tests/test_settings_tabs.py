"""The SDK-owned render_settings hook: one renderer per tab key, the
registered stylesheet on every response, an error for a key nobody
registered, and every settings mirror refreshed before the tab draws."""

import asyncio
import unittest

import branchkit


def fake_plugin(store: dict) -> branchkit.Plugin:
    """A Plugin whose actuator calls are answered in-process: the composed
    settings read serves `store`."""
    p = branchkit.Plugin()

    async def call(method, params=None, *args, **kwargs):
        if method == "collection.get":
            return {"name": params["name"], "data": store}
        raise AssertionError(f"unexpected method {method}")

    p.call = call  # type: ignore[method-assign]
    return p


class TestSettingsTabs(unittest.IsolatedAsyncioTestCase):
    async def render(self, p, tab_key):
        fn = p._handlers["render_settings"]
        return await fn({"tab_key": tab_key, "search": ""})

    async def test_dispatch_css_and_read_through(self):
        store = {"editor": "stale"}
        p = fake_plugin(store)
        mirror = p.settings("plugin.test.config")
        p.settings_css(".alpha{}")

        @p.settings_tab("alpha")
        def alpha(req):  # plain def — offloaded like any handler
            return f'<p id="alpha">{(mirror.get() or {}).get("editor", "")}</p>'

        @p.settings_tab("beta")
        async def beta(req):
            return f'<p id="beta">{req["tab_key"]}</p>'

        # The store moved behind the mirror's back (no collection.updated);
        # the render must still see the current value.
        store["editor"] = "fresh"
        resp = await self.render(p, "alpha")
        self.assertEqual(resp, {"html": '<p id="alpha">fresh</p>', "css": ".alpha{}"})
        resp = await self.render(p, "beta")
        self.assertEqual(resp["html"], '<p id="beta">beta</p>')
        with self.assertRaisesRegex(RuntimeError, '"nope"'):
            await self.render(p, "nope")

    async def test_renderer_error_propagates(self):
        p = fake_plugin({})

        @p.settings_tab("broken")
        def broken(req):
            raise ValueError("cannot draw")

        with self.assertRaisesRegex(ValueError, "cannot draw"):
            await self.render(p, "broken")

    # The tab API is the only way in: a hand-written render_settings handler
    # raises at registration whether or not a tab was registered first.
    def test_handle_rejects_render_settings(self):
        a = fake_plugin({})
        with self.assertRaisesRegex(RuntimeError, "settings_tab"):
            a.handle("render_settings", lambda params: {})
        b = fake_plugin({})
        b.settings_tab("x", lambda req: "")
        with self.assertRaisesRegex(RuntimeError, "settings_tab"):
            b.handle("render_settings", lambda params: {})

    # A command answers with no result however its handler is written — the
    # proxy refuses anything else with 422; this is the SDK's half.
    def test_handle_command_drops_the_return_value(self):
        p = fake_plugin({})
        seen = {}

        @p.handle_command("set_volume")
        def set_volume(req):
            seen["volume"] = req["volume"]
            return {"leak": True}

        fn = p._handlers["set_volume"]
        self.assertIsNone(asyncio.run(fn({"volume": 3})))
        self.assertEqual(seen, {"volume": 3})


if __name__ == "__main__":
    unittest.main()

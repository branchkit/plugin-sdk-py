"""Kernel unit tests — the SDK's own suite, separate from the
language-agnostic conformance harness (which is the cross-SDK floor).
These cover in-process behavior the harness can't see: builder wire
shapes, loaders, URL/route spelling, proxy parsing, and the
dual-handler dispatch contract."""

import asyncio
import json
import os
import tempfile
import unittest

import branchkit
from branchkit import commands as cmds
from branchkit import proxy, ui
from branchkit.actor import acting_for, get_current_actor
from branchkit.correlation import get_current_correlation, reset_correlation, set_correlation
from branchkit.plugin import (
    PluginCore,
    RecordingDisabledError,
    RpcCallError,
    error_kind_of,
    matches_topic,
    rpc_error_for,
)


class TestCommandBuilder(unittest.TestCase):
    def test_full_builder_wire_shape(self):
        spec = (
            cmds.command(cmds.word("open"), cmds.one_of("tab", "window"), cmds.capture("t", "ts"), cmds.text("n"))
            .action("browser.open", {"force": True})
            .requires_tags("a")
            .sets_tags("b")
            .clears_tags("c")
            .display_source("t", "menu")
            .sets_on_partial("d")
            .cancels_bridge()
            .discovery("exclusive")
            .category("Nav")
            .description("Open")
            .build()
        )
        self.assertEqual(spec["pattern"], ["open", ["tab", "window"], "<t:ts>", "<n:text>"])
        self.assertEqual(spec["action"], {"type": "browser.open", "force": True})
        self.assertEqual(spec["requires_tags"], ["a"])
        self.assertTrue(spec["cancels_bridge"])
        self.assertEqual(spec["discovery"], "exclusive")

    def test_capture_without_name_uses_collection(self):
        self.assertEqual(cmds.capture("", "apps"), "<apps>")
        self.assertEqual(cmds.text(), "<text>")

    def test_normalize_coerces_null_array_fields(self):
        spec = {"pattern": ["x"], "requires_tags": None, "variants": None}
        out = cmds._normalize_command_spec(spec)
        self.assertEqual(out["requires_tags"], [])
        self.assertEqual(out["variants"], [])


class TestCommandLoaders(unittest.TestCase):
    def test_context_file_merges_tags_and_absent_base_is_ok(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "commands"))
            with open(os.path.join(d, "commands", "warp.json"), "w") as f:
                json.dump(
                    {
                        "context": {"requires_tags": ["app.warp"]},
                        "commands": [{"pattern": ["go"], "requires_tags": ["own"]}],
                    },
                    f,
                )
            old = os.environ.get("BRANCHKIT_PLUGIN_DIR")
            os.environ["BRANCHKIT_PLUGIN_DIR"] = d
            try:
                specs = cmds.load_commands()
            finally:
                if old is None:
                    del os.environ["BRANCHKIT_PLUGIN_DIR"]
                else:
                    os.environ["BRANCHKIT_PLUGIN_DIR"] = old
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0]["requires_tags"], ["app.warp", "own"])

    def test_unset_plugin_dir_loads_nothing(self):
        old = os.environ.pop("BRANCHKIT_PLUGIN_DIR", None)
        try:
            self.assertEqual(cmds.load_commands(), [])
        finally:
            if old is not None:
                os.environ["BRANCHKIT_PLUGIN_DIR"] = old

    def test_missing_context_tags_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ctx.json")
            with open(path, "w") as f:
                json.dump({"context": {}, "commands": [{"pattern": ["x"]}]}, f)
            with self.assertRaises(RuntimeError):
                cmds._load_context_file(path)


class TestErrors(unittest.TestCase):
    def test_kind_driven_sentinel(self):
        e = rpc_error_for(-32006, "refused", {"kind": "recording_disabled"})
        self.assertIsInstance(e, RecordingDisabledError)
        self.assertEqual(error_kind_of(e), "recording_disabled")
        plain = rpc_error_for(-32001, "gone", {"kind": "not_found"})
        self.assertIsInstance(plain, RpcCallError)
        self.assertNotIsInstance(plain, RecordingDisabledError)
        self.assertIsNone(error_kind_of(ValueError("x")))

    def test_missing_data_leaves_kind_none(self):
        e = rpc_error_for(-1, "old actuator")
        self.assertIsNone(e.kind)
        self.assertEqual(e.code, -1)


class TestProxyParsing(unittest.TestCase):
    def test_unix_and_tcp_forms(self):
        self.assertEqual(proxy.parse_proxy_url("unix:///a/b.sock"), ("unix", "/a/b.sock"))
        self.assertEqual(proxy.parse_proxy_url("http://127.0.0.1:8080"), ("tcp", "127.0.0.1", 8080))

    def test_rejects_bad_forms(self):
        for bad in ("unix://", "http://127.0.0.1", "socks5://x:1", ""):
            with self.assertRaises(ValueError):
                proxy.parse_proxy_url(bad)


class TestSettingsRoute(unittest.TestCase):
    def test_method_url_uses_plugin_id(self):
        os.environ["BRANCHKIT_PLUGIN_ID"] = "demo"
        try:
            self.assertEqual(branchkit.method_url("set_gap"), "/v1/plugins/demo/methods/set_gap")
            self.assertEqual(branchkit.method_post("set_gap"), "@post('/v1/plugins/demo/methods/set_gap')")
            self.assertIn("{payload: {x: 1}}", branchkit.method_post("set_gap", "{x: 1}"))
        finally:
            del os.environ["BRANCHKIT_PLUGIN_ID"]


class TestUi(unittest.TestCase):
    def test_args_marshals_values_and_passes_expr_raw(self):
        s = ui.args({"a": "x\"y", "b": 1, "c": ui.expr("el.value")})
        self.assertIn('"a":"x\\"y"', s)
        self.assertIn('"c":el.value', s)

    def test_signal_name_distinct_for_colliding_seeds(self):
        self.assertNotEqual(ui.signal_name("a.b"), ui.signal_name("a_b"))

    def test_confirm_button_declares_ifmissing_signal(self):
        html = ui.confirm_button("Delete", "del_item", payload={"id": "x"})
        self.assertIn("__ifmissing", html)
        self.assertIn("Really delete?", html)


class TestListOpts(unittest.TestCase):
    def test_only_set_fields_serialize(self):
        self.assertEqual(branchkit.list_opts(limit=5, writer="me"), {"limit": 5, "writer": "me"})
        self.assertEqual(branchkit.log_list_opts(), {})


class TestCorrelation(unittest.TestCase):
    def test_set_reset_roundtrip(self):
        self.assertEqual(get_current_correlation(), "")
        token = set_correlation("tr_x")
        self.assertEqual(get_current_correlation(), "tr_x")
        reset_correlation(token)
        self.assertEqual(get_current_correlation(), "")


class TestActor(unittest.TestCase):
    def test_acting_for_sets_and_restores(self):
        self.assertEqual(get_current_actor(), "")
        with acting_for("headphones.py"):
            self.assertEqual(get_current_actor(), "headphones.py")
        self.assertEqual(get_current_actor(), "")

    def test_nested_scopes_restore_the_outer_label(self):
        with acting_for("headphones.py"):
            with acting_for("notes.py"):
                self.assertEqual(get_current_actor(), "notes.py")
            self.assertEqual(get_current_actor(), "headphones.py")
        self.assertEqual(get_current_actor(), "")

    def test_empty_actor_is_no_label(self):
        with acting_for(""):
            self.assertEqual(get_current_actor(), "")
        with acting_for(None):
            self.assertEqual(get_current_actor(), "")


class TestPatternListeners(unittest.TestCase):
    def test_matches_topic(self):
        for pattern, event, want in [
            ("scripts.headphones.charged", "scripts.headphones.charged", True),
            ("scripts.*.*", "scripts.headphones.charged", True),
            ("scripts.*.charged", "scripts.headphones.charged", True),
            ("*.headphones.charged", "scripts.headphones.charged", True),
            # `*` is ONE segment. A pattern that swallowed trailing segments
            # would route events the actuator's own gate never delivered.
            ("scripts.*", "scripts.headphones.charged", False),
            ("scripts.*.*", "scripts.headphones", False),
            ("scripts.*.*", "browser.tab.opened", False),
        ]:
            self.assertEqual(matches_topic(pattern, event), want, f"{pattern} vs {event}")

    def test_on_pattern_registers_in_order(self):
        core = PluginCore()
        core.on_pattern("scripts.*.*", lambda t, p: None)
        core.on_pattern("browser.*.*", lambda t, p: None)
        self.assertEqual(
            [pat for pat, _ in core._pattern_listeners], ["scripts.*.*", "browser.*.*"]
        )


class TestPatternDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_pattern_listener_sees_the_concrete_event_type(self):
        core = PluginCore()
        seen = []
        core.on_pattern("scripts.*.*", lambda t, p: seen.append(t))
        await core._invoke2(core._pattern_listeners[0][1], "scripts.headphones.charged", {})
        self.assertEqual(seen, ["scripts.headphones.charged"])

    async def test_async_pattern_listener_is_awaited(self):
        core = PluginCore()
        seen = []

        async def handler(event_type, params):
            seen.append((event_type, params))

        core.on_pattern("scripts.*.*", handler)
        await core._invoke2(handler, "scripts.notes.saved", {"k": 1})
        self.assertEqual(seen, [("scripts.notes.saved", {"k": 1})])


class TestDualHandlerDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_sync_handler_offloads_and_sees_correlation(self):
        core = PluginCore()
        seen = {}

        def sync_handler(params):
            import threading

            seen["thread_is_main"] = threading.current_thread() is threading.main_thread()
            seen["correlation"] = get_current_correlation()
            seen["actor"] = get_current_actor()
            return {"ok": True}

        token = set_correlation("tr_sync")
        try:
            with acting_for("headphones.py"):
                result = await core._invoke(sync_handler, {})
        finally:
            reset_correlation(token)
        self.assertEqual(result, {"ok": True})
        self.assertFalse(seen["thread_is_main"], "plain-def handler must run off the loop thread")
        self.assertEqual(seen["correlation"], "tr_sync", "contextvars must propagate into the thread")
        self.assertEqual(seen["actor"], "headphones.py", "the actor label rides the same contextvars")

    async def test_async_handler_runs_on_loop(self):
        core = PluginCore()

        async def h(params):
            return params

        self.assertEqual(await core._invoke(h, {"a": 1}), {"a": 1})

    async def test_handle_decorator_and_mutual_exclusion(self):
        core = PluginCore()

        @core.handle("m.x")
        async def mx(params):
            return 1

        self.assertIn("m.x", core._handlers)

        @core.handle_action("p.a")
        async def pa(req):
            return None

        with self.assertRaises(RuntimeError):
            core.handle("on_action", mx)


class TestListenerHelpers(unittest.TestCase):
    def test_inherited_listener_count_parses(self):
        from branchkit.listen import inherited_listener_count

        os.environ["LISTEN_FDS"] = "2"
        try:
            self.assertEqual(inherited_listener_count(), 2)
        finally:
            del os.environ["LISTEN_FDS"]
        os.environ["LISTEN_FDS"] = "junk"
        try:
            self.assertEqual(inherited_listener_count(), 0)
        finally:
            del os.environ["LISTEN_FDS"]


if __name__ == "__main__":
    unittest.main()

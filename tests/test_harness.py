"""Harness integration tests — the Python twin of the Go and TS SDKs'
harness suites. They drive the real `branchkit-test-harness` binary
(a real matcher, event bus and HUD registry) against the app repo's
plugins/helloworld (the Go one). Two things must exist first:

  1. the harness binary — `cargo build -p branchkit-test-harness` in the
     app repo, or BRANCHKIT_TEST_HARNESS=/path/to/branchkit-test-harness;
  2. the helloworld plugin's binary, which the harness spawns —
     `cd plugins/helloworld/src && go build -o ../helloworld-plugin .`

Either one missing skips with a message saying which. Set
BRANCHKIT_REQUIRE_HARNESS=1 (any CI lane that builds them) to make a
missing piece FAIL instead: a skip reports green, so a lookup that quietly
stops finding the binary would otherwise go unnoticed."""

import os
import unittest

from branchkit.harness import Harness, harness_binary_available, harness_required

_HERE = os.path.dirname(os.path.abspath(__file__))
HELLOWORLD_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "plugins", "helloworld"))
APPS_PROVIDER_DIR = os.path.join(_HERE, "testdata", "apps-provider")
HELLOWORLD_BIN = os.path.join(HELLOWORLD_DIR, "helloworld-plugin")

_HARNESS_SKIP = (
    "branchkit-test-harness not found; build it with "
    "`cargo build -p branchkit-test-harness` or set BRANCHKIT_TEST_HARNESS "
    "(BRANCHKIT_REQUIRE_HARNESS=1 fails instead of skipping)"
)
_HELLOWORLD_MISSING = (
    f"helloworld plugin not built ({HELLOWORLD_BIN}); build it with "
    "`cd plugins/helloworld/src && go build -o ../helloworld-plugin .`"
)


@unittest.skipUnless(harness_binary_available(), _HARNESS_SKIP)
class HarnessTests(unittest.TestCase):
    def start(self) -> Harness:
        if not os.path.exists(HELLOWORLD_BIN):
            if harness_required():
                self.fail(f"BRANCHKIT_REQUIRE_HARNESS is set: {_HELLOWORLD_MISSING}")
            self.skipTest(_HELLOWORLD_MISSING)
        h = Harness.start(HELLOWORLD_DIR)
        self.addCleanup(h.stop)
        return h

    def test_start_stop(self):
        h = self.start()
        state = h.get_plugin_state()
        self.assertTrue(state.get("alive"), "plugin should be alive after start")
        self.assertEqual(state.get("plugin_id"), "helloworld")

    def test_simulate_command_tie(self):
        h = self.start()
        # Seed the consumed `apps` vocabulary so the capture branch is live —
        # helloworld only consumes it; in production the apps plugin
        # provides it (the stub carries the same schema, and the writer must
        # be the introducer because named_entities pins introducer_only).
        # With "branchkit" seeded, "hello branchkit" completes BOTH
        # helloworld commands at the same length (the ["hello","branchkit"]
        # literal and the ["hello","<apps>"] capture). Equally-eligible
        # same-length candidates are a genuine tie: the matcher declines to
        # act and surfaces the tied set rather than guessing.
        h.load_manifest(APPS_PROVIDER_DIR)
        h.write_collection(
            "apps", {"spoken": "branchkit", "app_id": "com.test.branchkit"}, "apps-provider-stub"
        )
        result = h.simulate_command("hello branchkit")
        self.assertFalse(result.matched, "expected a surfaced tie, got a single winner")
        self.assertEqual(len(result.tied_candidates), 2, result.tied_candidates)
        for c in result.tied_candidates:
            self.assertEqual(c.get("owner_plugin"), "helloworld")

    def test_simulate_command_no_match(self):
        h = self.start()
        result = h.simulate_command("this will not match anything")
        self.assertFalse(result.matched)

    def test_parameterized_command(self):
        h = self.start()
        # With the provider stub's schema loaded, the `<apps>` capture
        # resolves the spoken key to the collection's value field, so the
        # action's "{apps}" placeholder carries the bundle id.
        h.load_manifest(APPS_PROVIDER_DIR)
        h.write_collection(
            "apps", {"spoken": "finder", "app_id": "com.apple.finder"}, "apps-provider-stub"
        )
        result = h.must_simulate_command("hello finder")
        self.assertEqual(result.action_params().get("name"), "com.apple.finder")

    def test_tag_set_get_clear(self):
        h = self.start()
        h.set_tag("test.example.tag")
        h.require_tag("test.example.tag")
        self.assertEqual(h.get_tags("test.example.*"), ["test.example.tag"])
        h.clear_tag("test.example.tag")
        h.require_no_tag("test.example.tag")

    def test_reset(self):
        h = self.start()
        h.set_tag("test.before.reset")
        h.require_tag("test.before.reset")
        h.reset()
        h.require_no_tag("test.before.reset")
        self.assertTrue(h.get_plugin_state().get("alive"), "plugin should be alive after reset")


if __name__ == "__main__":
    unittest.main()

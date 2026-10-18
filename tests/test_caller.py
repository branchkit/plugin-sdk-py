"""The generated wrappers run over any Caller, and a timeout override."""

import asyncio
import unittest

from branchkit import Client, Plugin
from branchkit.contracts_gen import METHOD_COLLECTION_GET, METHOD_NATIVE_DARK_MODE
from branchkit.methods_gen import MethodsMixin


class FakeCaller:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def call(self, method, params=None, timeout=None):
        self.calls.append((method, params, timeout))
        return self.answer


class CallerTests(unittest.TestCase):
    def test_wrappers_run_over_any_caller(self):
        fake = FakeCaller({"record": {"id": "a"}})
        got = asyncio.run(Client(fake).collection_get(name="notes"))
        self.assertEqual(fake.calls, [(METHOD_COLLECTION_GET, {"name": "notes"}, None)])
        self.assertEqual(got, {"record": {"id": "a"}})

    def test_with_timeout_passes_its_timeout_to_the_plugins_call(self):
        p = Plugin(detached=True)
        fake = FakeCaller({"dark_mode": True})
        p.call = fake.call
        asyncio.run(p.with_timeout(42).native_dark_mode())
        asyncio.run(p.native_dark_mode())
        self.assertEqual([(m, t) for m, _, t in fake.calls],
                         [(METHOD_NATIVE_DARK_MODE, 42), (METHOD_NATIVE_DARK_MODE, None)])

    def test_no_plugin_member_shadows_a_generated_wrapper(self):
        # Plugin lists MethodsMixin after the hand-written mixins, so a
        # same-named method there would win, silently.
        generated = {n for n, v in vars(MethodsMixin).items()
                     if callable(v) and not n.startswith("_") and n != "call"}
        self.assertGreater(len(generated), 400)
        shadowing = []
        for cls in Plugin.__mro__:
            if cls in (MethodsMixin, object):
                continue
            shadowing += [f"{cls.__name__}.{n}" for n in vars(cls) if n in generated]
        self.assertEqual(shadowing, [])


if __name__ == "__main__":
    unittest.main()

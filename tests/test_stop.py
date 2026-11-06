"""on_stop: the plugin's cleanup, run with the connection open before the
platform stops it."""

import asyncio
import unittest

import branchkit
from branchkit.contracts_gen import HOOK_LIST_METHODS, HOOK_ON_STOP


async def request(p, method, params=None):
    """Run a registered request handler the way the read loop does."""
    return await p._handlers[method](params if params is not None else {})


class OnStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_registering_it_names_on_stop_in_list_methods(self):
        p = branchkit.Plugin(detached=True)
        self.assertNotIn(HOOK_ON_STOP, (await request(p, HOOK_LIST_METHODS))["methods"])
        p.on_stop(lambda: None)
        self.assertIn(HOOK_ON_STOP, (await request(p, HOOK_LIST_METHODS))["methods"])

    async def test_cleanup_calls_the_platform_and_is_answered_after(self):
        p = branchkit.Plugin(detached=True)
        order = []

        async def call(method, params=None, **kw):
            await asyncio.sleep(0.01)
            order.append(f"call {method}")
            return {"data": None}

        p.call = call

        @p.on_stop
        async def cleanup():
            await p.call("collection.get", {"name": "on_stop_probe"})
            order.append("cleanup done")

        answer = await request(p, HOOK_ON_STOP)
        order.append("answered")
        self.assertEqual(answer, {})
        self.assertEqual(order, ["call collection.get", "cleanup done", "answered"])

    async def test_a_plain_def_runs_off_the_loop(self):
        p = branchkit.Plugin(detached=True)
        ran = []
        p.on_stop(lambda: ran.append("sync"))
        self.assertEqual(await request(p, HOOK_ON_STOP), {})
        self.assertEqual(ran, ["sync"])

    async def test_an_exception_is_the_answer(self):
        p = branchkit.Plugin(detached=True)

        @p.on_stop()
        async def cleanup():
            raise RuntimeError("windows not restored")

        with self.assertRaisesRegex(RuntimeError, "windows not restored"):
            await request(p, HOOK_ON_STOP)


if __name__ == "__main__":
    unittest.main()

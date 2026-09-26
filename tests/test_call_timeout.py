"""A call that gets no answer raises CallTimeoutError.

Typed, so a caller can tell "no answer" (the call may have happened) from a
definite refusal (RpcCallError) without reading the message, and still a
TimeoutError so existing `except TimeoutError` handlers keep working."""

import unittest

from branchkit.plugin import CallTimeoutError, PluginCore, RpcCallError


class TestCallTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_unanswered_call_raises_typed_timeout(self):
        core = PluginCore()
        core._write = lambda msg: None  # nothing answers the request
        with self.assertRaises(CallTimeoutError) as ctx:
            await core.call("collection.get", {}, timeout=0.02)
        err = ctx.exception
        self.assertIsInstance(err, TimeoutError)
        self.assertNotIsInstance(err, RpcCallError)
        self.assertEqual((err.method, err.timeout), ("collection.get", 0.02))
        self.assertEqual(core._pending, {}, "the abandoned call is forgotten")


if __name__ == "__main__":
    unittest.main()

"""Collection read helpers — the exhaustive-read pair.

`list` returns one page and discards `total`, so it cannot distinguish a
complete read from one the platform capped. `list_all` exists so a caller
that needs completeness (clearing a collection, reconciling against it,
mirroring it) can have it. These pin the behavior that makes it worth
having.
"""

import asyncio
import unittest

from branchkit.collection import CollectionMixin


class FakePlugin(CollectionMixin):
    """Answers `collection_list` from a fixed corpus, recording each read's opts."""

    def __init__(self, total: int):
        self.total = total
        self.calls: list[dict] = []

    async def collection_list(self, name: str, opts: dict | None = None):  # type: ignore[override]
        opts = opts or {}
        self.calls.append(opts)
        limit = opts.get("limit")
        assert limit is not None, "the probe read must carry an explicit limit"
        n = min(limit, self.total)
        return {"records": [{"id": "k", "payload": {}} for _ in range(n)], "total": self.total}


class TestListAll(unittest.TestCase):
    def test_reads_past_the_first_page(self):
        """Bounded by `total`, not a cursor walk — `cursor` is a no-op on
        contribution-keyed storage, so paging would never terminate."""
        p = FakePlugin(total=1500)
        records = asyncio.run(p.list_all("things"))
        self.assertEqual(len(records), 1500)
        self.assertEqual(len(p.calls), 2, "want exactly two reads")
        self.assertEqual(p.calls[1]["limit"], 1500, "second read should be bounded by total")

    def test_stops_at_one_read_when_the_first_page_is_whole(self):
        """Always paying two round trips would be a regression on every
        mirror refresh."""
        p = FakePlugin(total=1)
        asyncio.run(p.list_all("things"))
        self.assertEqual(len(p.calls), 1)

    def test_re_reads_when_the_collection_grows_mid_read(self):
        """`total` is observed on the read that returns it, so a write landing
        between the probe and the second read leaves the second read short.
        Returning it would report a truncated set as complete."""

        class Growing(FakePlugin):
            def __init__(self):
                super().__init__(total=1500)
                self.totals = [1500, 1600, 1600]

            async def collection_list(self, name, opts=None):
                opts = opts or {}
                self.calls.append(opts)
                total = self.totals[min(len(self.calls) - 1, len(self.totals) - 1)]
                n = min(opts["limit"], total)
                return {
                    "records": [{"id": "k", "payload": {}} for _ in range(n)],
                    "total": total,
                }

        p = Growing()
        self.assertEqual(len(asyncio.run(p.list_all("things"))), 1600)

    def test_gives_up_on_an_endlessly_growing_collection(self):
        """A collection written faster than it can be read is not something to
        spin on. Say so rather than returning a short read as though whole."""

        class Endless(FakePlugin):
            async def collection_list(self, name, opts=None):
                opts = opts or {}
                self.calls.append(opts)
                n = min(opts["limit"], self.total)
                self.total += 100
                return {
                    "records": [{"id": "k", "payload": {}} for _ in range(n)],
                    "total": self.total,
                }

        p = Endless(total=1500)
        with self.assertRaises(RuntimeError):
            asyncio.run(p.list_all("things"))

    def test_never_probes_without_a_limit(self):
        """Reading with no limit to discover `total` would fire the
        platform's default-limit diagnostic on every call, burying real
        occurrences from other callers under this helper's own noise. The
        assertion lives in FakePlugin.call."""
        p = FakePlugin(total=0)
        asyncio.run(p.list_all("things"))
        self.assertTrue(all(c.get("limit") is not None for c in p.calls))

    def test_compacted_variant_asks_for_the_fold(self):
        """Without compacted=True this silently returns raw append history
        and the caller has no way to tell."""
        p = FakePlugin(total=0)
        asyncio.run(p.list_all_compacted("things"))
        self.assertTrue(p.calls[0].get("compacted"))


if __name__ == "__main__":
    unittest.main()

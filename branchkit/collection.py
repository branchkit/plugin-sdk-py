"""State uniform helpers — the collection façade over the generated
`collection.*` wrappers. Python twin of plugin-sdk-go/collection.go and
plugin-sdk-ts/src/collection.ts."""

from __future__ import annotations

from typing import Any, Callable

from .contracts_gen import EVENT_COLLECTION_UPDATED


def scope_collection() -> dict:
    """Every other record THIS PLUGIN owns in the collection is the
    complement: after the call, the records you own here are exactly the
    ones you passed. Other plugins' records, and any the user added through
    Settings, are untouched and invisible to the diff."""
    return {"kind": "collection"}


def scope_group(group: str) -> dict:
    """Narrows further, to this plugin's records carrying the named group
    label — and stamps that label on every entry written. The label must be
    non-empty; an empty one is indistinguishable from "ungrouped" and the
    platform refuses it."""
    return {"kind": "group", "value": group}


def list_opts(
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    writer: str | None = None,
) -> dict:
    """Build a ListOpts with typed scalar values. `writer` filters to
    records owned by that writer — pair with `plugin.id` to ask for your
    own records, the read half of a scoped write."""
    out: dict[str, Any] = {}
    if since_ms is not None:
        out["since_ms"] = since_ms
    if until_ms is not None:
        out["until_ms"] = until_ms
    if limit is not None:
        out["limit"] = limit
    if cursor is not None:
        out["cursor"] = cursor
    if writer is not None:
        out["writer"] = writer
    return out


class CollectionMixin:
    """The uniform state verbs. Composed into `Plugin`."""

    async def get(self, name: str, id: str) -> dict | None:
        """The record with that id, or None. On a keyed (compacted-changelog)
        log this is the RAW entry — use `get_compacted` for the folded
        current state."""
        res = await self.collection_fetch(id, name)
        rec = (res or {}).get("record")
        return rec if isinstance(rec, dict) else None

    async def get_compacted(self, name: str, key: str) -> dict | None:
        """A keyed log's folded CURRENT state for one key — the point-read
        half of the compacted-changelog projection (pairs with
        `list_compacted`)."""
        res = await self.collection_fetch_compacted(key, name)
        rec = (res or {}).get("record")
        return rec if isinstance(rec, dict) else None

    async def list(self, name: str, opts: dict | None = None) -> list[dict]:
        """Read ONE PAGE of records.

        Omitting `opts` does NOT mean "every record": the platform applies a
        default limit when the caller supplies none, so a large collection
        comes back truncated, and this method discards `total` so it cannot
        tell you that happened.

        Choose deliberately: some records -> `list` with an explicit `limit`;
        every record -> `list_all`; a page plus the real count -> `list_page`.
        """
        res = await self.collection_list(name, opts)
        return (res or {}).get("records") or []

    async def list_all(self, name: str) -> list[dict]:
        """Read EVERY record, defeating the platform's default list limit.

        Use this when the read has to be exhaustive - clearing a collection,
        reconciling against it, counting it. `list` returns one page and
        discards `total`, so a caller using it cannot tell a complete read
        from a capped one; that is a quiet correctness bug wherever
        completeness was assumed.

        Prefer `list` with an explicit `limit` when you only need some
        records: this one is deliberately unbounded.
        """
        return await self._list_exhaustive(name, compacted=False)

    async def list_all_compacted(self, name: str) -> list[dict]:
        """`list_all` over the compacted-changelog projection - every folded
        record, one per key. A keyed log with more live keys than the cap
        otherwise folds to a view its reader believes is whole."""
        return await self._list_exhaustive(name, compacted=True)

    async def _list_exhaustive(self, name: str, compacted: bool) -> list[dict]:
        """Reads a collection completely, re-reading while it grows.

        Normally two round trips, not a cursor walk: `total` comes back with
        the first page, so the second read is bounded exactly. Cursor paging
        would be wrong anyway on contribution-keyed storage, where `cursor` is
        a no-op.

        The probe passes an EXPLICIT limit rather than omitting one. Reading
        with no limit to discover `total` would trip the platform's
        default-limit warning on every call - this helper would manufacture
        the exact noise that warning exists to surface, burying real
        occurrences underneath it.

        `total` is the FOLDED count when compacted, so the short-circuit is a
        real one on both projections rather than a guaranteed miss.
        """
        first_page = 1000

        async def page(limit: int) -> tuple[list[dict], int]:
            opts: dict = {"limit": limit}
            if compacted:
                opts["compacted"] = True
            return await self.list_page(name, opts)

        # Re-reads until the page covers `total`, because `total` is
        # observed on the read that returns it and the collection can grow
        # between reads. Taking the first `total` on faith would hand back a
        # short result reported as complete - the same "mirror declares
        # itself Ready over a truncated read" this helper exists to prevent,
        # just with a narrower window: one write landing mid-refresh is
        # enough.
        #
        # Bounded rather than unbounded: a collection written faster than it
        # can be read is not a condition to spin on, and a caller waiting on
        # a mirror refresh should get an answer. Practically it settles on
        # the second read.
        max_passes = 5
        limit = first_page
        for _ in range(max_passes):
            records, total = await page(limit)
            if len(records) >= total:
                return records
            limit = total
        raise RuntimeError(
            f"collection {name!r} kept growing across {max_passes} exhaustive "
            "reads; read it with an explicit limit and page with cursor instead"
        )

    async def list_compacted(self, name: str, opts: dict | None = None) -> list[dict]:
        """The compacted-changelog projection of a keyed log — one folded
        record per key instead of the raw append history."""
        merged = dict(opts or {})
        merged["compacted"] = True
        res = await self.collection_list(name, merged)
        return (res or {}).get("records") or []

    async def list_page(self, name: str, opts: dict | None = None) -> tuple[list[dict], int]:
        """Like `list` but also returns the unfiltered total."""
        res = await self.collection_list(name, opts)
        res = res or {}
        return res.get("records") or [], res.get("total") or 0

    async def count(self, name: str) -> int:
        res = await self.collection_count(name)
        return (res or {}).get("count") or 0

    async def put(self, name: str, id: str, payload: Any) -> None:
        """Single-record upsert. An unregistered name auto-registers as a
        record-keyed dynamic collection — memory-only and EPHEMERAL; declare
        the collection in the manifest for durable storage."""
        await self.collection_put(name, [{"id": id, "payload": payload}])

    async def put_many(self, name: str, entries: list[dict]) -> int:
        """Bulk upsert. Validation runs across all entries before any
        commit, so a partial batch with one invalid entry leaves the
        backend untouched."""
        if not entries:
            return 0
        res = await self.collection_put(name, entries)
        return (res or {}).get("count") or 0

    async def replace(
        self,
        name: str,
        entries: list[dict],
        scope: dict,
        *,
        roles: dict | None = None,
        label: str | None = None,
    ) -> dict:
        """Make the records in scope exactly `entries`: upsert what changed,
        delete what is absent, skip what is byte-identical. Returns
        ``{"put": n, "deleted": n, "skipped": n}``.

        Scope is required and never inferred — pass `scope_collection()` or
        `scope_group(...)`. See notes/DESIGN_COLLECTION_REPLACE.md."""
        # Refused locally rather than sent: guessing between "everything I
        # own here" and "the subset under this key space" is how a refresh
        # silently becomes a wipe.
        if not isinstance(scope, dict) or scope.get("kind") not in ("collection", "group"):
            raise ValueError(
                "replace: scope is required — use scope_collection() or scope_group(name)"
            )
        # No early return on empty `entries`: replacing with the empty set
        # is how a caller CLEARS its scope.
        res = await self.collection_replace(name, scope, entries, label, roles)
        res = res or {}
        return {
            "put": res.get("put") or 0,
            "deleted": res.get("deleted") or 0,
            "skipped": res.get("skipped") or 0,
        }

    async def put_many_with_roles(self, name: str, entries: list[dict], roles: dict) -> int:
        """Bulk upsert with per-payload-field display roles (field → role).
        Roles persist on the collection — pass them on the first put to a
        new name, then omit."""
        return await self.put_many_with_display(name, entries, roles, "")

    async def put_many_with_display(
        self, name: str, entries: list[dict], roles: dict | None, label: str
    ) -> int:
        """Bulk upsert that also sets the collection's human-readable label.
        Pass "" to leave the label unchanged; like roles, it persists."""
        if not entries:
            return 0
        res = await self.collection_put(name, entries, None, label or None, roles)
        return (res or {}).get("count") or 0

    async def patch(self, name: str, id: str, fields: Any) -> None:
        """Errors NOT_FOUND if no record with that id exists, or
        OPERATION_NOT_PERMITTED on collections the state forbids patching."""
        await self.collection_patch(fields, id, name)

    async def delete(self, name: str, id: str) -> bool:
        """Single-record delete. Returns whether the record existed."""
        res = await self.collection_delete_records(name, [id])
        return ((res or {}).get("deleted") or 0) > 0

    async def delete_many(self, name: str, ids: list[str]) -> tuple[int, int]:
        """Bulk delete. Returns (deleted, already_absent) so callers can
        detect drift between their view and the platform's."""
        if not ids:
            return 0, 0
        res = await self.collection_delete_records(name, ids)
        res = res or {}
        return res.get("deleted") or 0, res.get("already_absent") or 0

    def subscribe(self, name: str, fn: Callable) -> None:
        """Run `fn(evt)` whenever `_platform.collection.updated` fires for
        this collection. The ordered pump awaits an async `fn`, so two rapid
        updates cannot race. Subscriptions live for the process lifetime."""

        async def _on_updated(params):
            evt = params if isinstance(params, dict) else {}
            if evt.get("collection") == name:
                # Await, don't discard: this is what keeps wire order. Dual
                # dispatch, same as any handler — a plain-`def` fn offloads.
                await self._invoke(fn, evt)

        self.on(EVENT_COLLECTION_UPDATED, _on_updated)

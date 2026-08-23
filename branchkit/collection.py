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
        res = await self.collection_list(name, opts)
        return (res or {}).get("records") or []

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

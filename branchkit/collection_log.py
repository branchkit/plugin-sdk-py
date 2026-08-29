"""Helpers for log-kind collections — append-only record stores declared
in the plugin manifest as `kind: "log"`. Sugar over the unified verbs
(the wire surface is collection.list / collection.fetch /
collection.delete_records; log-shaped reads are the same list with
time-window opts). Python twin of collection_log.{go,ts}."""

from __future__ import annotations

from typing import Any


def log_list_opts(
    *,
    since_ms: int | None = None,
    until_ms: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict:
    """Build a LogListOpts with typed scalar values — field-identical to
    the unified list opts by design."""
    out: dict[str, Any] = {}
    if since_ms is not None:
        out["since_ms"] = since_ms
    if until_ms is not None:
        out["until_ms"] = until_ms
    if limit is not None:
        out["limit"] = limit
    if cursor is not None:
        out["cursor"] = cursor
    return out


def _record_to_log_entry(r: dict) -> dict:
    """Project the unified record envelope onto the log view. Lossless: log
    records carry their append time in timestamp_ms and owner in writer."""
    return {
        "id": r.get("id"),
        "timestamp_ms": r.get("timestamp_ms"),
        "payload": r.get("payload"),
        "writer": r.get("writer"),
    }


class CollectionLogMixin:
    async def append(self, name: str, payload: Any) -> str:
        """Append an entry to a log-kind collection; the actuator assigns a
        ULID and timestamp. Returns the assigned entry id. Raises
        RecordingDisabledError if the collection's recording flag is off."""
        entry = await self.collection_append(name, payload)
        if not entry:
            raise RuntimeError("collection.append: actuator returned no entry")
        return entry["id"]

    async def append_entry(self, name: str, payload: Any) -> dict:
        """Like `append` but returns the full LogEntry."""
        entry = await self.collection_append(name, payload)
        if not entry:
            raise RuntimeError("collection.append: actuator returned no entry")
        return entry

    async def append_keyed(self, name: str, key: str, payload: Any) -> None:
        """Annotate a keyed log (`log` preset, `id_strategy: by_field`):
        appends `payload` with `key` stamped into the key field, as a fresh
        append; same-key appends fold. Read the merged view with
        `list_compacted`. See docs/design/DESIGN_LOG_ANNOTATION_PROJECTION.md."""
        await self.collection_append_keyed(key, name, payload)

    async def list_log(self, name: str, opts: dict | None = None) -> list[dict]:
        """List log entries newest-first."""
        entries, _ = await self.list_log_page(name, opts)
        return entries

    async def list_log_page(self, name: str, opts: dict | None = None) -> tuple[list[dict], int]:
        """Like `list_log` but also returns the unfiltered total."""
        records, total = await self.list_page(name, opts)
        return [_record_to_log_entry(r) for r in records], total

    async def get_log_entry(self, name: str, id: str) -> dict | None:
        """One entry by id, or None. The RAW entry — on a keyed log use
        `get_compacted` for a key's current state."""
        rec = await self.get(name, id)
        return _record_to_log_entry(rec) if rec else None

    async def delete_log_entry(self, name: str, id: str) -> bool:
        """Delete one entry by id. Returns whether it existed."""
        return await self.delete(name, id)

    async def set_collection_recording(self, name: str, enabled: bool) -> None:
        """Toggle the recording flag on a log-kind collection. When False,
        subsequent `append` calls raise RecordingDisabledError."""
        await self.privacy_set_recording(enabled, name)

    async def get_collection_recording(self, name: str) -> bool:
        """The effective recording flag — the user override if set,
        otherwise the manifest's `default_recording_enabled`."""
        res = await self.privacy_get_recording(name)
        return bool((res or {}).get("enabled", False))

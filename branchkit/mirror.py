"""Consumed-collection mirror — a local, always-fresh copy of a collection
this plugin consumes. Python twin of mirror.{go,ts}; see
notes/DESIGN_COLLECTION_MIRROR.md.

Freshness model: fetches once at on_ready (the documented earliest safe
point to read other plugins' collections); refetches on
`_platform.collection.updated` for this collection (the manifest must
subscribe to that event pattern in `consumes.events`); an unpopulated
collection (the boot race) is NOT an error — the mirror stays not-ready
and the update event completes it."""

from __future__ import annotations

from typing import Callable

from .log import log


def _unpopulated(data) -> bool:
    """The empty sentinel an unwritten collection returns (`[]`, null, or
    absent) — including for singleton schemas, which only unwrap to their
    object shape once the owner has put."""
    return data is None or (isinstance(data, list) and len(data) == 0)


class CollectionMirror:
    def __init__(self, plugin, name: str, compacted: bool = False):
        # Internal — use plugin.mirror_collection / plugin.mirror_compacted.
        self._plugin = plugin
        self._name = name
        self._compacted = compacted
        self._data = None
        self._ready = False
        self._on_change: list[Callable] = []

    @property
    def ready(self) -> bool:
        """True once the mirror has fetched a populated snapshot."""
        return self._ready

    def raw(self):
        """The last populated `data` payload, or None before the first
        populated fetch — the same shapes `collection.get` returns."""
        return self._data

    def on_change(self, fn: Callable) -> None:
        """Register a callback fired after every successful refresh."""
        self._on_change.append(fn)

    async def refresh(self) -> None:
        """Refetch the collection. A populated response updates the
        snapshot, marks the mirror ready, and fires on_change callbacks.
        An RPC error propagates and leaves the previous snapshot intact.

        An EMPTY response means one of two things, and readiness tells them
        apart: a never-populated mirror is in the boot race (silent no-op —
        the update event completes it); a populated mirror reading empty is
        the source saying "I am now empty", so the snapshot empties and
        on_change fires — swallowing it is how derived projections orphan."""
        if self._compacted:
            recs = await self._plugin.list_compacted(self._name)
            empty = len(recs) == 0
            data = recs
        else:
            res = await self._plugin.collection_get(self._name)
            data = (res or {}).get("data")
            empty = _unpopulated(data)
        if empty:
            if not self._ready:
                return  # boot race — the update event will complete the mirror
            data = []
        self._data = data
        self._ready = True
        for fn in list(self._on_change):
            fn()

    def attach(self) -> None:
        # Internal — wires the on_ready fetch and update-event refetch. The
        # ordered notification pump awaits each refresh, so refreshes run
        # one at a time in wire order (the Go SDK gets this from its single
        # notify goroutine). Cannot deadlock: refresh blocks on an outbound
        # call whose response the read loop delivers independently.
        self_id = self._plugin.id

        async def _initial(_params=None):
            try:
                await self.refresh()
            except Exception as e:
                log(self_id, f'mirror "{self._name}": initial fetch failed: {e}')

        async def _on_update(_evt):
            try:
                await self.refresh()
            except Exception as e:
                log(self_id, f'mirror "{self._name}": refresh failed: {e}')

        self._plugin.on("on_ready", _initial)
        self._plugin.subscribe(self._name, _on_update)


class MirrorMixin:
    def mirror_collection(self, name: str) -> CollectionMirror:
        """Create a CollectionMirror of `name` and wire its freshness
        hooks. Must be called before `run()` so the on_ready fetch lands."""
        mirror = CollectionMirror(self, name)
        mirror.attach()
        return mirror

    def mirror_compacted(self, name: str) -> CollectionMirror:
        """Mirror the FOLDED current-state view of a keyed log — the records
        `list_compacted` returns, one per key. Use instead of
        `mirror_collection` for a keyed log (which would mirror the raw
        append history). The mirrored collection must declare
        `emits_on_change: true` for the refetch to fire."""
        mirror = CollectionMirror(self, name, compacted=True)
        mirror.attach()
        return mirror

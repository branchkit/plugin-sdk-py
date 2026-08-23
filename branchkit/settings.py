"""Typed settings access for `preset: settings` collections
(DESIGN_PLUGIN_SETTINGS_STORAGE.md). The platform materializes the
composed view — every manifest-declared field at its shipped default,
with the user's sparse changes applied last — so the plugin never loads,
caches, or defaults anything itself. Settings are read-only from the
plugin (`writers: platform_only`); there is deliberately no save."""

from __future__ import annotations

from typing import Any, Callable

from .log import log


class SettingsMirror:
    def __init__(self, plugin, name: str):
        # Internal — use plugin.settings(name).
        self._plugin = plugin
        self._name = name
        self._mirror = plugin.mirror_collection(name)
        self._val: dict | None = None
        self._on_change: list[Callable] = []
        self_id = plugin.id

        def _decode():
            raw = self._mirror.raw()
            if not isinstance(raw, dict):
                log(self_id, f'settings "{name}": composed read is not an object')
                return
            self._val = raw
            for fn in list(self._on_change):
                fn(self._val)

        self._mirror.on_change(_decode)

    @property
    def ready(self) -> bool:
        """True once a decoded snapshot exists. Unlike domain mirrors there
        is no boot race: the composed read is materialized from manifest
        defaults, so the first fetch always populates."""
        return self._val is not None

    def get(self) -> dict | None:
        """The current settings, or None before the first fetch."""
        return self._val

    def on_change(self, fn: Callable) -> None:
        """Run `fn(settings)` after every successful fetch — the initial
        one and every user edit."""
        self._on_change.append(fn)

    async def refresh(self) -> None:
        """Force a refetch. Rarely needed — the update-event path keeps the
        mirror fresh."""
        await self._mirror.refresh()

    async def set_user(self, key: str, value: Any) -> None:
        """Relay ONE user gesture into the settings collection. Settings
        are `writers: platform_only` — a plugin never saves settings on its
        own initiative — so this writes tenant `_user`: the choice is the
        user's and this plugin is the transport. The write and the mirror
        refresh are ONE operation on purpose: the actuator re-renders the
        settings tab the moment your handler returns, and a re-render that
        reads a stale mirror draws the stale value. After set_user
        resolves, get() observes the write."""
        await self.set_user_fields({key: value})

    async def set_user_fields(self, fields: dict) -> None:
        """set_user for a form submit: every field in one patch, one
        refresh. Same contract."""
        await self._plugin.overrides_apply(
            "patch", self._name, None, fields, self._name, None, "_user"
        )
        await self.refresh()

    async def unpatch_user(self, field: str) -> None:
        """Remove the user's override for one field so it resumes tracking
        the plugin's shipped default (a change back to the default must not
        pin a copy of it)."""
        await self._plugin.overrides_apply(
            "unpatch", self._name, field, None, self._name, None, "_user"
        )
        await self.refresh()

    async def load(self) -> dict | None:
        """The composed settings via a synchronous read-through. Use at the
        top of render paths: a render must read state at least as fresh as
        whatever triggered it."""
        await self.refresh()
        return self.get()


class SettingsMixin:
    def settings(self, name: str) -> SettingsMirror:
        """Typed mirror of a `preset: settings` collection. Must be called
        before `run()` so the initial fetch lands."""
        return SettingsMirror(self, name)

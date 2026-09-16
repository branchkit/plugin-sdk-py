"""Typed settings access for `preset: settings` collections
(DESIGN_PLUGIN_SETTINGS_STORAGE.md). The platform materializes the
composed view — every manifest-declared field at its shipped default,
with the user's sparse changes applied last — so the plugin never loads,
caches, or defaults anything itself. Settings are read-only from the
plugin (`writers: platform_only`); there is deliberately no save."""

from __future__ import annotations

from typing import Any, Callable

from .contracts_gen import HOOK_RENDER_SETTINGS
from .log import log



class SettingsMirror:
    def __init__(self, plugin, name: str):
        # Internal — use plugin.settings(name).
        self._plugin = plugin
        self._name = name
        self._mirror = plugin.mirror_collection(name)
        # The SDK's render_settings hook refreshes every settings mirror
        # before a tab draws (settings_tab) — the read-through render paths
        # used to hand-roll.
        plugin._settings_mirrors.append(self)
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


class SettingsMixin:
    def settings(self, name: str) -> SettingsMirror:
        """Typed mirror of a `preset: settings` collection. Must be called
        before `run()` so the initial fetch lands."""
        return SettingsMirror(self, name)

    def settings_tab(self, key: str, fn: Callable | None = None):
        """Register the renderer for the manifest-declared settings tab
        `key`. Usable directly (`plugin.settings_tab("k", fn)`) or as a
        decorator (`@plugin.settings_tab("k")`). The renderer takes the
        render_settings params and returns the tab's HTML FRAGMENT — the
        platform's frame owns the container it is morphed into — and the
        SDK attaches the stylesheet registered with `settings_css`. Plain
        `def` or `async def`, as with `handle`.

        The first call installs the SDK's own `render_settings` handler,
        which on every render (1) refreshes every settings mirror created
        with `settings()`, so the render reads state at least as fresh as
        whatever woke it; (2) dispatches on `tab_key` — a key with no
        renderer is an error, which the platform shows as the tab's error
        state instead of a blank body; (3) returns the fragment with the
        registered stylesheet.

        The platform's method proxy discards a settings method's result
        and answers 204: a method that changed something returns nothing
        and lets the re-render that follows draw it.

        This is the only way to install a render_settings handler:
        `handle("render_settings", ...)` raises, so every tab goes through
        this dispatch."""
        if fn is None:
            def deco(f):
                self.settings_tab(key, f)
                return f
            return deco
        if self._settings_tabs is None:
            self._settings_tabs = {}
            self._handlers[HOOK_RENDER_SETTINGS] = self._render_settings_tab
        self._settings_tabs[key] = fn
        return fn

    def settings_css(self, css: str) -> None:
        """Register the stylesheet returned with every tab this plugin
        renders. One sheet per plugin: the platform places it in a
        `<style>` element it owns, outside the morph target."""
        self._settings_css = css

    async def _render_settings_tab(self, params: Any) -> dict:
        req = params if isinstance(params, dict) else {}
        key = req.get("tab_key", "")
        fn = (self._settings_tabs or {}).get(key)
        if fn is None:
            raise RuntimeError(f'no renderer registered for settings tab "{key}"')
        # Read through before drawing. A refresh failure is logged, not
        # fatal: the mirror keeps its last snapshot and the tab still draws.
        for m in list(self._settings_mirrors):
            try:
                await m.refresh()
            except Exception as e:
                log(self.id, f"settings read-through failed: {e}")
        html = await self._invoke(fn, req)
        resp = {"html": html}
        if self._settings_css:
            resp["css"] = self._settings_css
        return resp


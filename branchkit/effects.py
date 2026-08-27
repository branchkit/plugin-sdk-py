"""Capability-mechanism helpers (effects). Python twin of effects.{go,ts}.
See notes/DESIGN_CAPABILITY_MECHANISM.md."""

from __future__ import annotations

from typing import Callable

from .contracts_gen import EVENT_EFFECT_DISPLACED


def _optional_str(v) -> str | None:
    return v if isinstance(v, str) else None


class EffectsMixin:
    async def assert_effect(self, name: str) -> dict:
        """Declare this plugin is asserting `name` (which must be declared
        in the manifest's `consumes.effects[*].asserts`). Returns
        ``{"granted", "already_held", "displaced", "enforced"}``."""
        res = await self.effects_assert(name)
        return {
            "granted": res["granted"],
            "already_held": res["already_held"],
            "displaced": _optional_str(res.get("displaced")),
            "enforced": res["enforced"],
        }

    async def retract_effect(self, name: str) -> dict:
        """Release this plugin's assertion of `name`. Idempotent. Returns
        ``{"retracted", "new_owner"}``."""
        res = await self.effects_retract(name)
        return {
            "retracted": res["retracted"],
            "new_owner": _optional_str(res.get("new_owner")),
        }

    async def is_effect_active(self, name: str) -> dict:
        """``{"active", "current_owner"}`` — active is True when this plugin
        holds top-of-stack. Unknown names return active=False rather than
        raising."""
        res = await self.effects_is_active(name)
        return {
            "active": res["active"],
            "current_owner": _optional_str(res.get("current_owner")),
        }

    def on_effect_displaced(self, handler: Callable) -> None:
        """Register `handler(evt)` fired when THIS plugin's assertion is
        overridden by a later asserter. Delivery of the underlying event is
        broadcast; this helper filters on `displaced_owner == plugin.id`.
        Subscribe directly via `on(EVENT_EFFECT_DISPLACED, ...)` to observe
        all displacements."""
        self_id = self.id

        async def _on_displaced(params):
            if not isinstance(params, dict):
                return
            effect = _optional_str(params.get("effect"))
            displaced_owner = _optional_str(params.get("displaced_owner"))
            # Only these two are load-bearing; their absence means a payload
            # this SDK cannot interpret, so drop it.
            if effect is None or displaced_owner is None:
                return
            # `new_owner` is Option<String> on the wire and may be null.
            new_owner = _optional_str(params.get("new_owner")) or ""
            if displaced_owner != self_id:
                return
            await self._invoke(
                handler,
                {"effect": effect, "new_owner": new_owner, "displaced_owner": displaced_owner},
            )

        self.on(EVENT_EFFECT_DISPLACED, _on_displaced)

"""HUD push sugar — parity with hud.{go,ts}. The generated hud_push takes
raw fragments, which proved awkward enough that callers hand-rolled the
envelope; these cover the two real shapes."""

from __future__ import annotations


class HudMixin:
    async def hud_push_fragment(self, channel: str, target_id: str, html: str) -> None:
        """Morph `html` into the element with id `target_id` inside the
        named HUD window — the shape that sizes the window from its content
        (raw replacement with an empty target leaves it 1px tall)."""
        await self.hud_push(channel, [{"target_id": target_id, "html": html}])

    async def hud_push_raw(self, channel: str, html: str) -> None:
        """Replace the HUD window's entire content (`raw: True`) — for
        windows whose markup carries its own container."""
        await self.hud_push(channel, [{"target_id": "", "html": html, "raw": True}])

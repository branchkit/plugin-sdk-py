"""Per-plugin leveled log façade over `plugin.debug` (the RPC method).
Python twin of debug.{go,ts}. Lines land in
`<app_support>/plugin-logs/<pluginID>.log`; warn/error cross-post to
actuator.log. See docs/design/DESIGN_PLUGIN_LOG_LEVELS.md."""

from __future__ import annotations

from typing import Any

from .contracts_gen import METHOD_PLUGIN_DEBUG

_LEVELS = ("trace", "debug", "info", "warn", "error")


class DebugMixin:
    async def trace(self, tag: str, data: Any) -> None:
        """Per-record diagnostic at trace level. Dropped by default
        (threshold defaults to `info`)."""
        await self.call(METHOD_PLUGIN_DEBUG, {"tag": tag, "data": data, "level": "trace"})

    async def debug(self, tag: str, data: Any) -> None:
        """Tagged structured payload at debug level. With the default
        threshold of `info` these are dropped — use `info` for
        per-operation diagnostics you want visible by default."""
        await self.call(METHOD_PLUGIN_DEBUG, {"tag": tag, "data": data, "level": "debug"})

    async def info(self, tag: str, data: Any) -> None:
        """Per-operation diagnostic at info level. Visible by default."""
        await self.call(METHOD_PLUGIN_DEBUG, {"tag": tag, "data": data, "level": "info"})

    async def warn(self, tag: str, data: Any) -> None:
        """Warning line, cross-posted to actuator.log."""
        await self.call(METHOD_PLUGIN_DEBUG, {"tag": tag, "data": data, "level": "warn"})

    async def error(self, tag: str, data: Any) -> None:
        """Error line, cross-posted to actuator.log."""
        await self.call(METHOD_PLUGIN_DEBUG, {"tag": tag, "data": data, "level": "error"})

    async def log_at(self, level: str, tag: str, data: Any) -> None:
        """Level-by-string helper for bridges forwarding requests with a
        `level` field. Unknown levels fall through to `debug`."""
        normalized = level if level in _LEVELS else "debug"
        await self.call(METHOD_PLUGIN_DEBUG, {"tag": tag, "data": data, "level": normalized})

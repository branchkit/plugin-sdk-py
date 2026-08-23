"""The one route the actuator serves for settings-UI → plugin method
calls (`/v1/plugins/{plugin_id}/methods/{*method_path}`). Hand-writing
this shape is how four plugins ended up with dead settings tabs — the
segment lives here so there is one spelling of it. Python twin of the
Go SDK's MethodURL / MethodPost."""

from __future__ import annotations

import os

_METHOD_ROUTE_PREFIX = "/v1/plugins/"


def method_url(method: str) -> str:
    """The settings-UI route that invokes `method` on this plugin. The
    plugin id comes from BRANCHKIT_PLUGIN_ID, so a renamed plugin cannot
    desync its own URLs. The actuator normalizes `-` and `/` to `_` before
    dispatch."""
    plugin_id = os.environ.get("BRANCHKIT_PLUGIN_ID") or "unknown"
    return f"{_METHOD_ROUTE_PREFIX}{plugin_id}/methods/{method.lstrip('/')}"


def method_post(method: str, payload_js: str = "") -> str:
    """The Datastar `@post(...)` expression that invokes `method` on this
    plugin, for a `data-on:click` attribute. `payload_js` is a JavaScript
    object literal embedded verbatim — escape user-controlled strings
    before building it."""
    if not payload_js:
        return f"@post('{method_url(method)}')"
    return f"@post('{method_url(method)}', {{payload: {payload_js}}})"

"""Settings-tab interaction idioms — the layer where every silent dead
button so far was born (DESIGN_SETTINGS_UI_ROBUSTNESS.md, leg 2). Parity
with plugin-sdk-go/ui and plugin-sdk-ts/src/ui.ts. Each helper encodes a
contract the platform cannot check at runtime:

- the element in an expression is `el`, never `$el` ($ reads a signal)
- page-local interaction state is a Datastar signal declared with
  __ifmissing (survives morphs) and CONSUMED by the action that uses it
- method URLs come from method_post, never spelled by hand
- payload values are marshaled, never quote-spliced

Plugins own look and layout: helpers accept class/style options and
return fragments. Hand-written Datastar remains a full escape hatch."""

from __future__ import annotations

import html as _html
import json
from typing import Any

from .settings_route import method_post


class Expr:
    """Marks a raw Datastar/JS expression inside a payload — the
    deliberate escape hatch from value marshaling."""

    def __init__(self, js: str):
        self.js = js


def expr(js: str) -> Expr:
    return Expr(js)


# "The value of the input immediately before this button" — the input+Save
# pairing. `el` is the element; `$el` silently reads an undefined signal.
input_value = Expr("el.previousElementSibling.value")


def signal_name(seed: str) -> str:
    """Sanitize an arbitrary seed for use inside a Datastar signal
    identifier: alnum+underscore, with an fnv32 suffix so distinct seeds
    that sanitize alike cannot share state."""
    clean = "".join(c if c.isalnum() else "_" for c in seed)
    h = 0x811C9DC5
    for ch in seed:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{clean}_{h:x}"


def args(payload: dict[str, Any]) -> str:
    """Build a JS object-literal payload string — the expression-context
    form of a payload dict, for hand-composed data-on expressions calling
    method_post directly. Values marshaled; Expr raw."""
    parts = [
        f"{json.dumps(k)}:{v.js if isinstance(v, Expr) else json.dumps(v)}"
        for k, v in payload.items()
    ]
    return "{" + ",".join(parts) + "}"


def _build_payload(payload: dict | None, payload_js: str | None) -> str:
    if payload_js is not None:
        return payload_js
    if not payload:
        return ""
    return args(payload)


def _attrs(class_: str | None, style: str | None) -> str:
    out = ""
    if class_:
        out += f' class="{_html.escape(class_, quote=True)}"'
    if style:
        out += f' style="{_html.escape(style, quote=True)}"'
    return out


def _button(label: str, click: str, class_: str | None, style: str | None) -> str:
    return (
        f"<button{_attrs(class_, style)} "
        f'data-on:click="{_html.escape(click, quote=True)}">'
        f"{_html.escape(label)}</button>"
    )


def post_button(
    label: str,
    method: str,
    *,
    payload: dict | None = None,
    payload_js: str | None = None,
    then: str | None = None,
    class_: str | None = None,
    style: str | None = None,
) -> str:
    """Post to one of this plugin's methods on click."""
    click = method_post(method, _build_payload(payload, payload_js))
    if then:
        click += f"; {then}"
    return _button(label, click, class_, style)


def signal_button(
    label: str,
    expr_js: str,
    *,
    class_: str | None = None,
    style: str | None = None,
) -> str:
    """Run a signal expression on click ("$renaming = true") — page-local
    UI state, no server round-trip."""
    return _button(label, expr_js, class_, style)


def confirm_button(
    label: str,
    method: str,
    *,
    payload: dict | None = None,
    payload_js: str | None = None,
    then: str | None = None,
    confirm_label: str | None = None,
    key: str | None = None,
    class_: str | None = None,
    style: str | None = None,
) -> str:
    """Two-click destructive action: arm (page-local signal — one window's
    half-finished delete never appears in another), confirm posts and
    consumes the signal in one expression, Cancel disarms."""
    payload_str = _build_payload(payload, payload_js)
    sig_key = key or signal_name(f"{method}|{payload_str}")
    confirm_text = confirm_label or f"Really {label.lower()}?"
    sig = f"$c_{sig_key}"
    arm = _button(label, f"{sig} = true", class_, style)
    confirm_click = f"{method_post(method, payload_str)}; {sig} = false"
    if then:
        confirm_click += f"; {then}"
    danger_style = f"{style or ''}color:#c44;border-color:#c44;"
    confirm = _button(confirm_text, confirm_click, class_, danger_style)
    cancel = _button("Cancel", f"{sig} = false", class_, style)
    return (
        f'<span data-signals:c_{sig_key}__ifmissing="false">'
        f'<span data-show="!{sig}">{arm}</span>'
        f'<span data-show="{sig}" style="display:none;">{confirm}{cancel}</span>'
        f"</span>"
    )

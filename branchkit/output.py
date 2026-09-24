"""Semantic output helpers — parity with output.{go,ts}.

The generated ``plugin.output_state(state)`` wrapper is the whole call; a
plugin states what is true for the person (``OutputState``,
``OutputSection``, ``OutputItem`` in types_gen.py, ``OUTPUT_KIND_*`` /
``OUTPUT_URGENCY_*`` in closed_vocab_gen.py) and never sees a renderer. An
item's ``action`` is one of exactly two shapes; these build them so no
producer hand-writes the envelope, and so the three SDKs read the same.

Producers state what is true (a kind, human-language phrases, an urgency)
and never choose how or whether it is shown or spoken; every renderer reads
the same document.
"""

from __future__ import annotations

from typing import Any

from .types_gen import OutputAction


def say_action(words: str) -> OutputAction:
    """The action that injects ``words`` as if the person had spoken them —
    routed through the same matcher their voice reaches, so confirming the
    item is indistinguishable from saying it. The common case for a command."""
    return {"say": words}


def dispatch_action(action_type: str, params: Any = None) -> OutputAction:
    """The action that dispatches ``action_type`` directly with ``params``
    (omitted when ``None``) — for items that are not commands."""
    if params is None:
        return {"dispatch": action_type}
    return {"dispatch": action_type, "params": params}

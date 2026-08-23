"""Command loading and authoring. Python twin of commands.{go,ts}.

File layout:

    $BRANCHKIT_PLUGIN_DIR/
      commands.json     ← base commands (no context)
      commands/         ← optional directory of context files
        warp.json       ← context-scoped commands

Commands in a context file inherit the context's requires_tags (merged
with any on the command itself)."""

from __future__ import annotations

import json
import os
from typing import Any

# The actuator's command parser rejects an explicit JSON `null` for these
# array fields (it accepts an array or an absent field). The builder
# defaults them to [], but a spec from load_commands carries whatever the
# file had — coerce None to [] before the wire. Mirrors Go's
# normalizeCommandSpec.
_COMMAND_SPEC_ARRAY_FIELDS = (
    "requires_tags",
    "sets_tags",
    "clears_tags",
    "sets_on_partial",
    "variants",
)


def word(w: str) -> str:
    """A literal spoken word."""
    return w


def one_of(*alts: str) -> list[str]:
    """An alternatives slot: any of the given words matches, sharing one
    action."""
    return list(alts)


def capture(name: str, collection: str) -> str:
    """A list-capture token `<name:collection>` whose matched value binds
    to `name`. An empty name uses the collection as the binding name."""
    return f"<{name}:{collection}>" if name else f"<{collection}>"


def text(name: str = "") -> str:
    """A free-text capture token `<name:text>` (or `<text>`)."""
    return f"<{name}:text>" if name else "<text>"


class CommandBuilder:
    """Accumulates a CommandSpec via chained setters; finish with build()."""

    def __init__(self, slots: list):
        self._spec: dict[str, Any] = {
            "pattern": list(slots),
            "cancels_bridge": False,
            "requires_tags": [],
            "sets_tags": [],
            "clears_tags": [],
            "sets_on_partial": [],
            "display_sources": {},
            "variants": [],
        }

    def action(self, type: str, params: dict | None = None) -> "CommandBuilder":
        """Set the action fired on match. `type` is the action's type (a
        built-in like "key" or a dotted plugin action); `params` are merged
        into the action object."""
        self._spec["action"] = {"type": type, **(params or {})}
        return self

    def requires_tags(self, *tags: str) -> "CommandBuilder":
        self._spec["requires_tags"].extend(tags)
        return self

    def sets_tags(self, *tags: str) -> "CommandBuilder":
        self._spec["sets_tags"].extend(tags)
        return self

    def clears_tags(self, *tags: str) -> "CommandBuilder":
        self._spec["clears_tags"].extend(tags)
        return self

    def display_source(self, capture: str, collection: str) -> "CommandBuilder":
        """Discovery-HUD display override for one capture: enumerate
        `collection` in the HUD instead of the capture's matching
        collection. Matching is untouched."""
        self._spec["display_sources"][capture] = collection
        return self

    def sets_on_partial(self, *tags: str) -> "CommandBuilder":
        self._spec["sets_on_partial"].extend(tags)
        return self

    def cancels_bridge(self) -> "CommandBuilder":
        self._spec["cancels_bridge"] = True
        return self

    def discovery(self, mode: str) -> "CommandBuilder":
        """Declare the command's prefix-discovery affordance ("prefix" or
        "exclusive"). Valid only on a literal-prefix + single-tail-capture
        pattern. See notes/DESIGN_DISCOVERABLE_PREFIX.md."""
        self._spec["discovery"] = mode
        return self

    def category(self, c: str) -> "CommandBuilder":
        self._spec["category"] = c
        return self

    def description(self, d: str) -> "CommandBuilder":
        self._spec["description"] = d
        return self

    def build(self) -> dict:
        return self._spec


def command(*slots) -> CommandBuilder:
    """Start a command builder with the given pattern slots."""
    return CommandBuilder(list(slots))


def _load_command_file(path: str) -> list[dict]:
    """An ABSENT file is not an error — a plugin may ship only context
    files, or none at all. Any other read failure propagates: swallowing
    them is how an unreadable commands.json silently pushed zero
    commands."""
    try:
        with open(path, encoding="utf-8") as f:
            data = f.read()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise RuntimeError(f"{path}: {e}") from e
    return json.loads(data)


def _load_context_file(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        cf = json.load(f)
    context_tags = ((cf.get("context") or {}).get("requires_tags")) or []
    if not context_tags:
        raise RuntimeError(f"{path}: missing or empty context.requires_tags")
    commands = cf.get("commands") or []
    return [_merge_requires_tags(cmd, context_tags) for cmd in commands]


def _merge_requires_tags(cmd: dict, context_tags: list[str]) -> dict:
    existing = cmd.get("requires_tags") or []
    return {**cmd, "requires_tags": [*context_tags, *existing]}


def load_commands() -> list[dict]:
    """Load commands.json and any context files from commands/ WITHOUT
    pushing — lets a plugin union file-authored static commands with built
    dynamic ones and push them in a single push_command_specs call."""
    plugin_dir = os.environ.get("BRANCHKIT_PLUGIN_DIR")
    if not plugin_dir:
        return []
    raw: list[dict] = []
    raw.extend(_load_command_file(os.path.join(plugin_dir, "commands.json")))
    context_dir = os.path.join(plugin_dir, "commands")
    try:
        entries = sorted(e for e in os.listdir(context_dir) if e.endswith(".json"))
    except OSError:
        entries = []
    for entry in entries:
        raw.extend(_load_context_file(os.path.join(context_dir, entry)))
    return raw


def _normalize_command_spec(spec: dict) -> dict:
    out = dict(spec)
    for field in _COMMAND_SPEC_ARRAY_FIELDS:
        if out.get(field) is None:
            out[field] = []
    return out


async def push_commands(plugin) -> int:
    """Load commands.json + context files and push them all via
    commands.push. Returns the number of command variants registered."""
    specs = load_commands()
    if not specs:
        return 0
    return await push_command_specs(plugin, specs)


async def push_command_specs(plugin, specs: list[dict]) -> int:
    """Register a built/loaded set of commands via commands.push
    (replace-per-plugin semantics). Returns the number of command variants
    registered."""
    resp = await plugin.call(
        "commands.push", {"commands": [_normalize_command_spec(s) for s in specs]}
    )
    return (resp or {}).get("count") or 0


async def push_command_group(plugin, group: str, specs: list[dict]) -> int:
    """Register `specs` as a NAMED GROUP within this plugin's command set,
    replacing only that group. Use whenever a plugin has more than one
    command source — plain push_command_specs replaces the ENTIRE set, so
    two sources pushing independently race. Returns the number of command
    variants now active for the whole plugin."""
    # Refused locally rather than sent: an empty group name is
    # indistinguishable on the wire from an ungrouped push, which replaces
    # EVERY group. A caller must not reach whole-set semantics by accident.
    if not group:
        raise ValueError(
            "push_command_group: group name is required (use push_command_specs to replace the whole set)"
        )
    resp = await plugin.call(
        "commands.push",
        {"commands": [_normalize_command_spec(s) for s in specs], "group": group},
    )
    return (resp or {}).get("count") or 0

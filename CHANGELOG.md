# Changelog

Versions before this file predate it; their contents are in the repo's
git history.

## Unreleased

## 0.15.0 — 2026-10-25

- New `branchkit.wrap_tls(conn, host, context=None)` runs TLS over a
  connection `dial()` returned, including the named-pipe object `dial()`
  returns on Windows, which `ssl`'s `wrap_socket` refuses.
- `log()` and `stage_log()` write UTF-8 to stderr whatever the locale's
  encoding (cp1252 on Windows).
- The package's project URLs name branchkit.com.
- New log-event constants: `plugin.restarted` (cause, backoff and restart
  count), `plugin.restart_budget_exhausted`, and `stage.respawned` (a
  pipeline stage started afresh instead of reused, with the reason).
  `state.ttl_swept` is now emitted at Info, once per collection a TTL sweep
  removed records from.
- A window's frame source can now be `assumed`: the platform cannot see the
  window (the focused window on GNOME Wayland) and gives its display's
  visible bounds, so its geometry is not a fact.
- Generated docs: `commands.resolve`'s `collections` narrows completions to
  commands stored in the named command-set collections; Hyprland is named
  among the desktop sessions an operation cannot run on, and the platform
  profile's `session` field lists `hyprland`.

## 0.14.0 — 2026-10-21

Pages, part two: the old names are gone.

- Breaking: the deprecated `settings_tab` is removed; register a page with
  `plugin.page(key, fn)` or `@plugin.page(key)`.
- The manifest declares a plugin's pages as `implements.pages` (was
  `implements.settings_tabs`, now refused with the rename named).

## 0.13.0 — 2026-10-21

Pages: a plugin's own screens are registered as pages.

- `plugin.page(key, fn)` (or `@plugin.page(key)`) registers a renderer for
  one of the plugin's own pages. `settings_tab` is a deprecated alias and
  will be removed in the next release, when the manifest's
  `implements.settings_tabs` becomes `implements.pages`.

## 0.12.0 — 2026-10-20

Re-decodes can target the dictation stages, and a run can record its audio.

- `recognition_redecode(language=...)`: the language a dictation stage's
  probe decodes in, as the live stage's `--language`. A command stage
  ignores it. The dictation stages (WhisperKit, whisper.cpp) can now be
  re-decoded through `recognition.redecode` too.
- `pipelines_run(record_to=...)`: record a run's input audio into a folder
  of the caller's own data dir, one WAV per span plus a `manifest.jsonl`
  line each.

## 0.11.0 — 2026-10-20

Re-decodes can match the live recognizer, and report time, confidence and
grammar.

- `RedecodeItem["structured"]`: decode the item through the live
  structured grammar (the pattern DAG and union a recognition pipeline is
  seeded with) instead of the flat word list. The grammar is the
  platform's; set the flag to opt in.
- `RedecodeLine["decode_ms"]` and `RedecodeLine["word_margins"]`: the
  recognizer's time on the item and each decoded word's confidence margin.
  Both are absent on a failed line.
- `grammar_version` on the `recognition.redecode` and `pipelines.grammar`
  results: a content version of the structured grammar. They are equal for
  the same grammar, so a plugin caching re-decode results can key them by
  the grammar they were decoded under.

## 0.10.0 — 2026-10-19

Breaking: plugins no longer send raw messages to the host.

- Removed `control_signal`. Use the operation for the effect: `hud_show` /
  `hud_hide` for windows, `indicator_set` for the recording indicator,
  `artifact_download` for the plugin's own artifacts.
- Removed `control_message` from `OnActionResponse`. A handler that opens
  or closes a window calls `hud_show` / `hud_hide` itself; return data in
  `result`. The platform ignores (and logs) a `control_message` from an
  older SDK.
- Removed `control_message` from `DispatchResult`, `SelectionPickResult`
  and `SessionEndCleanupResult`.

## 0.9.0 — 2026-10-19

New surface; nothing removed.

- `indicator_set(state=...)` shows what input is doing on the recording
  indicator (the menu-bar or tray icon, and the floating pill).
  `IndicatorState` names what the input becomes, not any one plugin's modes:
  `"idle"`, `"input_text"`, `"input_action"`, `"listening"`, `"standby"`,
  `"processing"`, `"speaking"`, `"loading"`. It needs the `microphone`
  privilege, and a host that draws no indicator refuses it.
- `artifact_download(ref="<plugin>/<artifact>")` downloads one of the
  caller's own declared artifacts on the platform's downloader; progress is
  published to `_platform.model_download`. A ref naming another plugin's
  artifact, or a missing downloader, is an error.
- Prefer these to the raw `control_signal` forms (`shell:icon:...`,
  `download_model:...`): the platform now refuses a raw icon state, and a
  later release removes `control_signal`.

## 0.8.1 — 2026-10-18

Documentation only; no behaviour or type changes.

- `ResolveResult.sets_tags` and `clears_tags` are documented: they are the tags the
  resolve's commit applied, so a write the platform's policy refused, or
  one a hold ended before, is absent; a preview reports what committing
  would set and clear.
- `cancels_bridge` is documented as the platform now reads it: a command of two or
  more words that opens with a literal word interrupts a held part-way
  match without it; it matters for one-word cancel words like "over".

## 0.8.0 — 2026-10-18

### Transcript payload

- `PipelineTranscriptEventParams` gains `focused_app`, `focused_app_now` and
  `partial`. The platform always sent them; the declared type lacked them, so
  a plugin on the typed helper could not read them. The platform now builds
  the payload from the declared type, so the two cannot drift again.

### Handshake declared

- The handshake every SDK answers itself is now declared in the platform's
  protocol and so generated: `HOOK_LIST_METHODS`, `HOOK_LIST_ACTION_TYPES`,
  `HOOK_ON_READY` constants, and the `ListMethodsResponse`,
  `ListActionTypesResponse` and `OnReadyRequest` types. The SDK registers
  its own handshake through those constants. `OnReadyRequest.platform` is
  the same `PlatformProfileResponse` type `platform.profile` returns.

## 0.7.0 — 2026-10-18

### Typed event subscription

- One generated helper per platform event, beside the call wrappers:
  `plugin.on_app_focused(fn)` or `@plugin.on_app_focused`, with `fn` typed
  to take `AppFocusedEventParams`. An event with no payload, or a signal
  (its payload is advisory: re-read the state it announces), takes `fn()`,
  sync or async. The helpers are a generated `EventsMixin` that `Plugin`
  mixes in. `on_effect_displaced` stays the hand-written helper for its
  event.
- `on` and `on_pattern` are unchanged, for other plugins' events and
  patterns.

## 0.6.0 — 2026-10-17

### Typed wrappers

- `Client(caller)` gives the generated wrappers (`MethodsMixin`) over any
  `Caller` (anything with the plugin's `call`), such as a test's fake,
  with no transport.
- `plugin.with_timeout(seconds)` returns the wrappers with that timeout
  for each call, in place of `call`'s 10 s default. Before, only a raw
  `call(method, params, timeout=...)` could wait longer.

### Network

- Over a handed-off proxy channel (`BRANCHKIT_PROXY=fd://N`, Linux), a `dial()`
  whose timeout passed while waiting for the channel's answer no longer
  leaves the next dial one answer behind. The late answer is read and
  discarded before the next ask, so every dial gets the connection that
  answers its own ask; before, it got the previous dial's, and so did every
  dial after it.

### World

- `DisplayInfo.is_primary`: whether a display is the system's main one (the
  menu bar's on macOS, the primary monitor on Windows, RandR's primary
  output on X11). False for every display where the system cannot say.

### Native

- `native_caret_bounds`: where the caret is on screen in the focused text
  field (`text.caret`). Nothing for a password field or a field that
  cannot say; on Linux, X11 only.

### HUD

- `relative_to="caret"`: a window above or below the line being typed,
  lined up with the text caret where it was when the window opened. Needs
  the `text.caret` privilege; otherwise, or where the caret cannot be
  read, on the display.
- Anchors cover the whole three-by-three grid: `"center-left"` and
  `"center-right"` join the seven there were.
- `hud_create_channel` takes `relative_to` (`"display"`, the default,
  `"focused_window"` or `"pointer"`) and `offset` (`{"x": ..., "y": ...}`
  in pixels), as the manifest's `hud_windows` do. Needs a platform that
  knows them.

### Listen

- `ListenRequest.remote_addr`: the client's `"host:port"`, on an
  inherited listener and through the platform's listener relay (Windows)
  alike. The relay header is now `BKRELAY/2`, whose answer carries the
  peer (`OK <addr>`); a bare `OK` from an older platform is still accepted.
  Needs a platform that speaks version 2.

### Output

- `output_state(..., draws_own_window=True)`: publish a state for every
  other renderer (speech, the window's accessibility element) while the
  plugin keeps drawing its own window with `hud.push`; the platform draws
  nothing.

### Request stages (stage runtime)

- Added `branchkit.pipeline.serve_requests` / `serve_requests_on`: the fourth
  stage shape, one answer per request (a language model, a translator, a
  classifier). The capability declares `stage_type: "request"`; each
  `request {request_id, body}` gets exactly one `reply` with the same id, in
  arrival order, carrying the handler's answer as `body` or its exception as
  `error`. A request with no id gets a non-fatal `bad_request` error event.

### Speech engines (stage runtime)

- Added `branchkit.pipeline.serve_speech_engine` /
  `serve_speech_engine_on` with the `SpeechEngine` base class and `SpeakCtx`:
  the third stage shape, a text-to-speech engine. Each `speak` request
  becomes an audio session the stage streams back (`start`, then `audio` per
  piece); the runtime keeps requests in order, cancels the one in progress
  the moment the platform sends its `audio_stop` (`cancelled()`, `done()`),
  closes a queued one that is cancelled before it starts, and ends every
  utterance with exactly one `audio_stop`, after an `error` when `speak`
  raises.
- Added `branchkit.pipeline.shared_clock_ms`, the clock the platform stamps
  microphone audio with (`CLOCK_UPTIME_RAW` on macOS, `CLOCK_MONOTONIC` on
  other unixes, wall time on Windows).
- Generated: `Speak`, `PlaybackStarted`, `PlaybackEnded` and their event
  tags; `Capability.voices` with `VoiceInfo`.

### Device triggers

- Added `bindings_set_triggers` (`TriggerDecl`, `TriggerKind`): a device plugin publishes
  the triggers it offers, each with a label, an optional heading (a layer)
  and whether it is a button or momentary. Settings lists them, bound or not,
  and binds any of them from a command picker as the user's own binding.
  Publish again when a device connects or goes away; the list is dropped when
  the plugin stops.
- Added `bindings_report` and `bindings_set` (`BindingsReportRequest`, `BindingsSetRequest`, `BindingEdit`, `BindingEdge`):
  a plugin that owns an input device makes its controls binding triggers,
  bound in the platform's table like hotkeys. It reports each press of one of
  its own triggers and the platform runs what it is bound to; it may set what
  its own triggers are bound to, and those edits run on its authority. The
  caller is always the source: neither call can name another plugin's
  triggers. Trigger names are the plugin's own (`"g2/button3"`), with the
  hotkey event words after them, plus the new `repeat`.

### Key bindings (breaking)

- Removed `keybinds_register` and its `KeybindsRegisterRequest` / `KeybindsRegisterResult` /
  `RegistrySnapshot` / `RegistryEntry` types. The `keybinds.register` operation is gone: it let any plugin
  replace every hotkey on the machine. The platform now builds the hotkey
  table itself from the `_platform.bindings` collection and the user's edits
  in `_platform.binding_overrides`, and publishes the result as
  `_platform.bindings.active`. A plugin contributes a hotkey under
  `collection_data["_platform.bindings"]` (previously
  `collection_data.keybinds`, same shape).

### Pipeline

- `DisplayInfo` gains `is_asleep` (`NotRequired[bool]`): the display is
  connected but in display sleep. Absent means awake.

## 0.5.0 — 2026-09-28

0.4.0 was tagged but never published to PyPI; 0.5.0 carries its changes too
(listed under 0.4.0 below), so upgrading from 0.3.0 means reading both.

### D-Bus calls (Linux)

- `await plugin.native_dbus_call(service=..., path=..., interface=..., method=..., args=...)`
  calls one D-Bus method the plugin declared in `requires.dbus.methods` and the
  user switched on. Linux only; elsewhere the call fails with "D-Bus exists
  only on Linux".

### Breaking: generated method parameters are keyword-only

- Every generated method's parameters are now keyword-only:
  `await plugin.collection_fetch(id=i, name=n)`, not
  `await plugin.collection_fetch(i, n)`. Positional order came from the
  schema, which is alphabetical, so it said nothing a caller could guess,
  and 62 methods had two or more parameters of one type that ran just as
  well swapped. A positional call now fails at once with a `TypeError`
  naming the method. A new optional parameter can be added later without
  breaking any caller. Methods with no parameters are unchanged.

### `**` in pattern listeners

- `on_pattern` now takes `**`, zero or more whole segments, matching the
  platform's subscription grammar: `ext.acme.**` hears every depth under the
  vendor (the bare `ext.acme` included), and `_platform.**` every platform
  event. `*` is unchanged — exactly one segment. Wildcards are whole segments
  only, so `a**` stays literal text. Before this, a `**` pattern matched
  nothing but its own literal text. The matcher runs the platform's topic
  conformance table (a copy ships beside the tests), so it answers exactly
  what delivery does.

### Who sent this event

- `plugin.current_event_origin()` (and `get_current_event_origin()`) returns
  an `EventOrigin(source, on_behalf_of)` for the event notification an `on`
  or `on_pattern` listener is handling. The platform delivers every event a
  subscription matches, whoever emitted it, and until now a listener could
  not tell two senders of one event type apart. `source` is the emitter the
  platform authenticated (a plugin id, `_platform`, or a stage's name) and
  can be trusted; `on_behalf_of` is that emitter's own actor label, a claim
  by `source`. Same ambient shape as `current_correlation()`, and carried
  into a plain-`def` listener's thread; empty in a request handler. An
  platform that does not send the sender leaves it empty.

## 0.4.0 — 2026-09-19

### The platform's `Action` is a type

`dispatch` — the call a plugin makes most — took raw JSON. It takes the
generated `Action` now, as do `commands.resolve`'s winner and tied
candidates, and the actions a delegated pipeline returns from
`on_transcript`. `Action` is an internally tagged union with a
self-recursive `sequence` variant, and it is rendered in full: no field
that carries an action is raw JSON any more.

Two things stay deliberately open, and say so in their own doc comments:
an action's `params` (per-plugin — `branchkit-gen` types those from the
receiving plugin's `action_types`) and `commands.push`'s `action` (the
authored dialect is a superset of the wire shape, so one type would lie
about it).

### Closed shapes stopped hiding as JSON

Every remaining untyped member of the generated surface was audited
against the code that produces it, and either declared or defended in
writing. Newly typed here:

- `hud.push` takes `HudFragment[]`
- `hud.create_channel` takes the `Anchor` enum (an unrecognised value is
  now an error instead of a silent fall back to the default)
- `selection.set` takes `HUDItem[]`
- `keybinds.register` takes `RegistrySnapshot`
- `commands.enumerate` returns a typed `binding`
- `commands.push` takes `CommandSpec[]`
- `on_commands_changed` carries typed command snapshots — and
  `disabled_plugins`, which the schema had been omitting entirely

Untyped members across the three SDKs: 193 → 147. What is left is listed
field by field, each with a written reason, in the generator's fidelity
ledger (every remaining member is opaque by declaration in the Rust source).

### Known gap

A command `pattern` token is `string | string[]`, an untagged union. The
generator refuses to emit one rather than degrade it to raw JSON, so
`CommandSpec.pattern` and `.variants` stay open and the hand-written
command builder remains the way to construct a pattern.

### Also

`dispatch` takes a typed action dict; `push_command_specs` and
`push_command_group` go through the generated wrapper.

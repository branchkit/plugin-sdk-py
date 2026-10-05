# BranchKit Plugin SDK (Python)

BranchKit is an accessibility plugin platform for the desktop. The platform
loads plugins, confines each one to what it declared, tracks what is true right
now (which app has focus, which mode is active), and routes voice commands,
hotkeys and other input to whichever plugin claims them. This SDK is how a
Python program becomes one of those plugins. MIT licensed, standard library
only.

**Status:** BranchKit is pre-launch; the application is in private
development, and this SDK is published and usable today. Versions are 0.x, so a
minor release can break callers — [CHANGELOG.md](CHANGELOG.md) says what
changed and how to migrate. You can write and unit-test a plugin today;
loading it needs a BranchKit install, which is not yet publicly available.

## Install

Python 3.11 or later. A plugin carries its own copy of the SDK — a plugin runs
sandboxed, with no network and no pip — so install it into the plugin
directory:

```sh
python3 -m pip install --target . branchkit                                  # latest on PyPI
python3 -m pip install --target . git+https://github.com/branchkit/plugin-sdk-py  # this repository's main
branchkit-cli runtime install python                                         # once per machine
```

PyPI releases can lag this repository; the changelog says what each version
contains. The last command installs the CPython that BranchKit runs Python
plugins with, which the manifest asks for with `requires.runtimes`.

The fastest start is the scaffold, which writes a working plugin and vendors
the SDK for you:

```sh
branchkit-cli dev init --name my-plugin --template py
```

## A minimal plugin

A plugin is a directory with a manifest, the commands it contributes, and a
program. This is what `dev init` writes, trimmed.

`plugin.json` declares who the plugin is, what it may do, and what it offers:

```json
{
  "id": "my-plugin",
  "name": "My Plugin",
  "version": "0.1.0",
  "min_api_version": "0.2.0",
  "requires": { "privileges": ["input"], "runtimes": ["python"] },
  "run": "python3 main.py",
  "action_prefix": "myplugin",
  "action_types": {
    "greet": { "label": "Greet", "fields": [{ "key": "name", "label": "Name", "field_type": "string" }] }
  },
  "collection_data": { "voice_commands": "commands.json" },
  "implements": { "on_action": true }
}
```

`commands.json` maps a spoken phrase to an action:

```json
[
  {
    "pattern": ["hello", "branchkit"],
    "action": { "type": "myplugin.greet", "params": { "name": "BranchKit" } },
    "description": "Say Hello BranchKit"
  }
]
```

`main.py` handles the action:

```python
import asyncio

import branchkit
from actions_gen import GreetParams  # generated from plugin.json by branchkit-gen

plugin = branchkit.Plugin()


@plugin.handle_action("myplugin.greet")
async def greet(req):
    p: GreetParams = req["params"] or {}
    name = p.get("name") or "BranchKit"
    await plugin.input_type_text(text=f"Hello, {name}!")


asyncio.run(plugin.run())  # returns when BranchKit stops the plugin
```

Say "hello branchkit" and the plugin types `Hello, BranchKit!` at the cursor.
Typing needs the `input` privilege, which is why the manifest asks for it.

`actions_gen.py` comes from
[branchkit-gen](https://github.com/branchkit/branchkit-gen)
(`go install github.com/branchkit/branchkit-gen@latest`). It writes a
`TypedDict` for each entry in `action_types` and a `handle_<action>`
registrar, so the action string need not be spelled by hand; re-run
`branchkit-gen --plugin .` after editing the manifest.

Handlers may be `async def`, run on the event loop, or plain `def`, run on a
worker thread so a blocking body cannot stall the plugin. The generated
platform methods are coroutines, so a handler that calls the platform should
be `async def`; keep plain `def` for blocking work that does not.

## Calling the platform

Every platform method has a generated method on `Plugin`. Its arguments are
keyword-only, and it returns the typed result:

```python
rec = await plugin.collection_fetch(id=record_id, name="notes")

await plugin.hud_create_channel(channel="status", accepts_input=True)
```

Arguments are always named, never positional, so each one says what it is; a
positional call fails at once with a `TypeError`. Leave an optional argument
out to mean "absent". The generated methods are in
[branchkit/methods_gen.py](branchkit/methods_gen.py) and their result types in
[branchkit/types_gen.py](branchkit/types_gen.py), with the platform's own
description on every parameter.

**Errors** from the platform are `branchkit.RpcCallError` (`code`, `message`,
`kind`, `data`), and `branchkit.error_kind_of(e)` reads the kind from any
exception:

```python
try:
    await plugin.input_type_text(text="hi")
except branchkit.RpcCallError as e:
    if branchkit.error_kind_of(e) == "forbidden":
        ...  # declare the privilege
```

`UnsupportedError` (a method this OS does not provide) and
`RecordingDisabledError` are subclasses of `RpcCallError` you can catch
directly. A call that gets no answer in time raises `CallTimeoutError` (a
`TimeoutError`) instead; it is not a refusal, since the platform may have
carried the call out, so re-read before retrying a write.

`await plugin.call(method, params)` is the untyped escape hatch, for a method
too new to have a generated wrapper (`plugin.call_sync` is its blocking form
for a plain `def` handler). Prefer the wrapper whenever one exists.

## Permissions and the sandbox

Every plugin runs confined to what its manifest declares, and the platform,
not the SDK, enforces it. A plugin that cannot be sandboxed on the machine
does not start.

- **Privileges.** A call that needs a privilege not listed under
  `requires.privileges` is refused before it runs, with an error of kind
  `forbidden` naming the operation. Some privileges also ask the user the
  first time, and the user can switch any grant off later.
- **Files.** The plugin reads its own directory (`branchkit.plugin_dir()`) and
  reads and writes its own data directory (`branchkit.plugin_data_dir()`). The
  home directory and other plugins' data are out of reach.
- **Network.** None unless `requires.network` asks for it: `"localhost"`, or
  `{"hosts": ["api.example.com"]}`. Connections go through a per-plugin proxy
  that checks each host, and the SDK routes `urllib.request` and
  `branchkit.dial` through it for you. A host the manifest does not list, or
  one the user has switched off, is refused with a `branchkit.HostRefusedError`.

## What the SDK covers

| Need | API |
|---|---|
| Handle an action | `@plugin.handle_action("prefix.name")` (alias `@plugin.action`), registrars from `actions_gen.py` |
| Serve your own method | `@plugin.handle("method")`, `plugin.handle_command` |
| React to events | `@plugin.on(event)`, `@plugin.on_pattern("ext.acme.**")`, `plugin.current_event_origin()`; emit with `events_emit` |
| Store state | `get` / `list` / `list_page` / `count` / `put` / `put_many` / `patch` / `delete` / `replace`, `subscribe` |
| Append-only logs | `append`, `append_keyed`, `list_log`, `get_log_entry`, `delete_log_entry` |
| Keep a live copy | `plugin.mirror_collection(name)`, `plugin.settings(name)` |
| Contribute commands | `branchkit.command(branchkit.word("open"), branchkit.capture("app", "apps")).action(…).build()`, `push_command_specs`, `push_command_group` |
| Bind keys and device buttons | manifest `collection_data["_platform.bindings"]`; a device plugin lists its triggers with `bindings_set_triggers`, reports presses with `bindings_report` and proposes settings from its own screen with `bindings_propose` (guides: *Triggers and authority*, *Make a device a binding source*) |
| A settings tab | `@plugin.settings_tab(key)` + `implements.settings_tabs` in the manifest; `post_button` / `signal_button` / `confirm_button` |
| Show something | `output_state(state=…)` with `say_action` / `dispatch_action`, `hud_push` |
| Hold a system effect | `assert_effect`, `retract_effect`, `is_effect_active`, `on_effect_displaced` |
| Trace a request | `plugin.current_correlation()` |
| Label calls made for something you host (a script, an extension) | `with branchkit.acting_for(actor):` |
| Find your files | `branchkit.plugin_dir()`, `branchkit.plugin_data_dir()`, `branchkit.api_version()` |
| Log | `await plugin.info` / `warn` / `error` / `debug` / `trace(tag, data)` to your plugin's log (debug and trace are off by default) |
| Outbound HTTP | `urllib.request.urlopen` (routed through the platform's proxy), `branchkit.UpstreamClient` |
| Raw TCP (MQTT, a local daemon) | `branchkit.dial(host, port)` |
| Accept local connections | `branchkit.listen_local(plugin)` with `requires.sockets.listen` |
| Test a plugin | `branchkit.harness.Harness` |

Package `branchkit.pipeline` is for pipeline stages (audio and monitor
processes on a separate wire), not for ordinary plugins.

## Testing a plugin

`Harness` loads your plugin against a simulated platform and matches phrases
the way the real matcher does, without audio:

```python
import unittest

from branchkit.harness import Harness, harness_binary_available


@unittest.skipUnless(harness_binary_available(), "branchkit-test-harness not found")
class GreetTests(unittest.TestCase):
    def test_greet_matches(self):
        with Harness.start(".") as h:
            result = h.must_simulate_command("hello branchkit")
            self.assertEqual(result.action_type(), "myplugin.greet")
```

It runs the `branchkit-test-harness` binary, which ships with the BranchKit app
(on macOS, inside `BranchKit.app/Contents/Resources`); set
`BRANCHKIT_TEST_HARNESS` to its path anywhere else. Because the app is not yet
publicly available, the guard above makes harness tests skip outside a
BranchKit install (`Harness.start` raises without the binary); set
`BRANCHKIT_REQUIRE_HARNESS=1` (in CI, say) to make a missing binary a failure
instead. `python3 -m unittest` runs your tests; `branchkit-cli dev test .`
checks the manifest and runs the platform's own conformance checks against the
plugin.

Against a running BranchKit:

```sh
branchkit-cli plugin install . --build            # install it
branchkit-cli dev watch .                          # reload on save
branchkit-cli dev say "hello branchkit" --simulate # match and report, execute nothing
branchkit-cli dev plog my-plugin --since 30s       # read its log
```

## Learn more

- **API reference:** the docstrings on every export (`help(branchkit)`), and
  the type hints your editor reads.
- **Local docs:** `branchkit-cli docs path` prints the documentation bundled
  with your installed BranchKit, for reading or grepping offline.
- **Worked examples:** [helloworld-py](https://github.com/branchkit/branchkit-plugin-helloworld-py)
  (exactly what `dev init` writes);
  [snippets](https://github.com/branchkit/branchkit-plugin-snippets), the
  teaching plugin; and real plugins built on the Go SDK with the same surface:
  [keyboard](https://github.com/branchkit/branchkit-plugin-keyboard),
  [system](https://github.com/branchkit/branchkit-plugin-system),
  [placement](https://github.com/branchkit/branchkit-plugin-placement).
- **Tools:** [branchkit-cli](https://github.com/branchkit/branchkit-cli)
  (scaffold, install, test, inspect, managed runtimes) and
  [branchkit-gen](https://github.com/branchkit/branchkit-gen) (typed action
  params, manifest validation).

## Versioning

Tags follow semver, 0.x for now: a minor release may break callers, and
CHANGELOG.md names every break with its migration. The SDK version is separate
from the platform contract version: the platform refuses to load a plugin whose
manifest `min_api_version` is newer than the contract it speaks, and
`branchkit.api_version()` reports that contract version at run time. The
contract itself changes without deprecation cycles until the first release. The
Go ([plugin-sdk-go](https://github.com/branchkit/plugin-sdk-go)), TypeScript
([plugin-sdk-ts](https://github.com/branchkit/plugin-sdk-ts)) and Python SDKs
implement the same surface and are held to it by one cross-language conformance
suite.

## Contributing

[Issues](https://github.com/branchkit/plugin-sdk-py/issues/new/choose) are welcome:
a bug in the SDK or its docs, or, most useful, something you tried to build
and couldn't, with what you needed from the platform. You don't need to know
how BranchKit is built to tell us that.

We don't take pull requests for code yet. Much of each SDK is generated from
BranchKit's platform contracts, which aren't public, and the three are kept in
step across Go, TypeScript and Python, so the maintainers make each change in
all three at once. Files ending in `_gen.py` are generated; don't edit them by hand.

Found a security problem, such as a way around the sandbox or a permission
check? Please [report it privately](https://github.com/branchkit/plugin-sdk-py/security/advisories/new),
not in a public issue.

To run this SDK's own tests:

```sh
python3 -m unittest discover -s tests
```

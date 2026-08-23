# BranchKit Plugin SDK (Python)

The Python SDK for building [BranchKit](https://branchkit.dev)
plugins — processes (run under BranchKit's managed CPython) that add
voice commands, window management, browser integration, or anything
else to the BranchKit platform. MIT licensed. Feature-parity with the
Go and TypeScript SDKs, verified by a shared conformance harness.
Stdlib-only: importing the SDK forces no third-party dependency on any
plugin.

## Start here

- **[Your First Plugin](https://branchkit.dev/guide/getting-started/your-first-plugin)** —
  working plugin in ~10 minutes
- **[Plugin Anatomy](https://branchkit.dev/guide/getting-started/plugin-anatomy)** —
  manifest, lifecycle, methods
- **[Plugin API Reference](https://branchkit.dev/reference/specs/plugin-api)** —
  every wire method, generated from the OpenRPC spec

## Minimal plugin

```python
import asyncio
import branchkit

plugin = branchkit.Plugin()

@plugin.handle_action("myplugin.greet")
async def greet(req):
    await plugin.input_type_text("Hello!")
    return {"status": "ok"}

asyncio.run(plugin.run())
```

Pair with a `plugin.json` manifest declaring the action and
`"runtimes": ["python"]` (`run: "python3 main.py"`) — see the tutorial.
`branchkit-gen` generates typed params (`TypedDict`) and registrars from
your manifest's `action_types`.

Handlers may be `async def` (run on the asyncio loop) or plain `def`
(auto-offloaded to a worker thread, so a blocking body cannot freeze the
plugin — use `plugin.call_sync(...)` there instead of `await`).

## Key surfaces

| Need | API |
|---|---|
| Handle an action | `@plugin.handle_action("prefix.name")` (alias `@plugin.action`) |
| Handle an RPC method | `@plugin.handle("my_method")` |
| Listen for events | `@plugin.on(EVENT_COLLECTION_UPDATED)` |
| Call the actuator | generated wrappers (`await plugin.collection_get(...)`, 600+) or raw `plugin.call` |
| State verbs | `plugin.get/list/put/patch/delete/replace/...` |
| Logs (append-only) | `plugin.append/append_keyed/list_log/...` |
| Mirrors | `plugin.mirror_collection(name)`, `plugin.settings(name)` |
| Commands | `branchkit.command(...)` builder, `push_command_group` |
| Leveled logging | `await plugin.info(tag, data)` (trace/debug/info/warn/error) |
| Local listener | `branchkit.listen_local(plugin)` (serves inherited fds) |
| Outbound HTTP | `branchkit.UpstreamClient` / `urllib.request.urlopen` (both proxy-aware) |
| Author tests | `branchkit.harness.Harness` |

## Development

```sh
python3 -m unittest discover -s tests     # the SDK's own suite
```

The cross-language conformance suite lives in the app workspace
(`branchkit-sdk-test` against `sdk-test/testplugin-py`). Generated
files (`*_gen.py`) are emitted by `emit-sdk` — edit the inventory, not
the files.

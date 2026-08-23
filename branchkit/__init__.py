"""BranchKit plugin SDK for Python.

    import asyncio
    import branchkit

    plugin = branchkit.Plugin()

    @plugin.handle("my.method")
    async def my_method(params):
        return {"ok": True}

    asyncio.run(plugin.run())

Handlers may be `async def` (run on the loop) or plain `def`
(auto-offloaded to a thread, so a blocking body cannot freeze the
plugin). Stdlib-only by design: importing the SDK forces no third-party
dependency on any plugin."""

from . import proxy as _proxy

# Route stdlib HTTP through BRANCHKIT_PROXY when sandboxed (per-host
# tier) — same import-time side effect as the TS SDK's entry module.
_proxy.install_proxy_from_env()

from .plugin import (
    PluginCore,
    RecordingDisabledError,
    RpcCallError,
    api_version,
    error_kind_of,
    models_dir,
    plugin_data_dir,
    plugin_dir,
)
from .actor import acting_for, get_current_actor
from .collection import CollectionMixin, list_opts, scope_collection, scope_group
from .collection_log import CollectionLogMixin, log_list_opts
from .debug import DebugMixin
from .effects import EffectsMixin
from .hud import HudMixin
from .log import log
from .methods_gen import MethodsMixin
from .mirror import CollectionMirror, MirrorMixin
from .settings import SettingsMirror, SettingsMixin
from .commands import (
    CommandBuilder,
    capture,
    command,
    load_commands,
    one_of,
    push_command_group,
    push_command_specs,
    push_commands,
    text,
    word,
)
from .listen import Listener, inherited_listener_count, listen_local
from .settings_route import method_post, method_url
from .ui import (
    Expr,
    args,
    confirm_button,
    expr,
    input_value,
    post_button,
    signal_button,
    signal_name,
)
from .upstream import UpstreamClient, UpstreamResponse
from .closed_vocab_gen import *  # noqa: F401,F403 — error kinds, directives, effects
from .contracts_gen import *  # noqa: F401,F403 — method/hook/event/tag constants
from .log_events_gen import *  # noqa: F401,F403 — observability log-event names


class Plugin(
    CollectionMixin,
    CollectionLogMixin,
    EffectsMixin,
    DebugMixin,
    HudMixin,
    MirrorMixin,
    SettingsMixin,
    MethodsMixin,
    PluginCore,
):
    """The plugin: JSON-RPC transport plus the generated method wrappers
    and the state/log/effects/debug/HUD/mirror/settings façades, one
    class. See PluginCore for the transport contract."""

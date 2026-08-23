import json
import sys


def log(plugin_id: str, *args) -> None:
    """Write a log message to stderr with a [pluginId] prefix (W7).
    Stdout is reserved for JSON-RPC protocol messages."""
    parts = [a if isinstance(a, str) else json.dumps(a) for a in args]
    sys.stderr.write(f"[{plugin_id}] {' '.join(parts)}\n")
    sys.stderr.flush()

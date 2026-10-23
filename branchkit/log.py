import json
import sys


def _write_stderr(text: str) -> None:
    """Write ``text`` to stderr as UTF-8, whatever the locale's encoding.

    Python encodes its text streams in the locale's encoding, which on
    Windows is the ANSI code page: an "é" left the process as one cp1252
    byte, no UTF-8 reader took it, and a character the code page lacks was
    escaped away. The platform reads plugin and stage stderr as UTF-8, so
    the SDK writes those bytes itself. Text already buffered on the stream
    goes out first, keeping the order."""
    err = sys.stderr
    if err is None:
        return
    buf = getattr(err, "buffer", None)
    if buf is None:
        # Replaced by something with no byte layer (a test's StringIO):
        # there is no encoding to get wrong.
        err.write(text)
        err.flush()
        return
    err.flush()
    buf.write(text.encode("utf-8", "replace"))
    buf.flush()


def log(plugin_id: str, *args) -> None:
    """Write a log message to stderr with a [pluginId] prefix (W7).
    Stdout is reserved for JSON-RPC protocol messages."""
    parts = [a if isinstance(a, str) else json.dumps(a) for a in args]
    _write_stderr(f"[{plugin_id}] {' '.join(parts)}\n")

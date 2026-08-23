"""Localhost listener for external-service connections. Python twin of
listen.{go,ts}.

When the actuator granted listener sockets (manifest `sockets.listen`,
delivered per the LISTEN_FDS convention at fds 3+), the FIRST granted
listener (fd 3) is used instead of self-binding. This is not an
optimization: inside the Linux sandbox the plugin runs in an empty
network namespace, where a self-bound 127.0.0.1 is a private dead
loopback — the inherited host-loopback listener is the only reachable
surface. CPython serves an inherited fd natively
(`socket.socket(fileno=...)`), so unlike TS there is no two-runtime
dance. See the actuator's notes/DESIGN_SANDBOX_LOOPBACK_FDPASS.md."""

from __future__ import annotations

import hmac
import http.server
import json
import os
import secrets
import socket
import socketserver
import threading
from typing import Callable


def inherited_listener_count() -> int:
    """Number of actuator-granted listener sockets (0 when none). Unlike
    systemd's convention, LISTEN_PID is deliberately not set or checked:
    the actuator cannot know the child pid before spawn, and plugin
    identity is already established by fd ownership."""
    raw = os.environ.get("LISTEN_FDS", "")
    try:
        n = int(raw)
    except ValueError:
        return 0
    return n if n > 0 else 0


def _granted_ports() -> list[int]:
    """Ports of the actuator-granted listeners, parsed from
    BRANCHKIT_LISTEN_PORTS ("id=port" pairs, comma-separated)."""
    raw = os.environ.get("BRANCHKIT_LISTEN_PORTS", "")
    out = []
    for pair in raw.split(","):
        _, _, port_s = pair.partition("=")
        if port_s.isdigit() and int(port_s) > 0:
            out.append(int(port_s))
    return out


class ListenRequest:
    """The request a route handler receives: `method`, `path`, `headers`,
    and `body()` for the raw payload bytes."""

    def __init__(self, handler: http.server.BaseHTTPRequestHandler, path: str):
        self.method = handler.command
        self.path = path
        self.headers = handler.headers
        self._handler = handler

    def body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self._handler.rfile.read(length) if length > 0 else b""


class Listener:
    """Accepts inbound HTTP connections from an external service. Route
    handlers take a ListenRequest and return the response body (str or
    bytes, sent as 200) or a (status, body) tuple."""

    def __init__(self, server, token: str, addr: str, plugin):
        # Internal — use listen_local() to create.
        self._server = server
        self._token = token
        self._addr = addr
        self._serving = False
        self._routes: dict[str, Callable] = {}
        self.plugin = plugin

    def handle_func(self, method: str, path: str, handler: Callable) -> None:
        """Register a handler for an exact method + path. Matching is on
        the path only — a query string on the request is ignored."""
        self._routes[f"{method} {path}"] = handler

    def addr(self) -> str:
        """The listener's address (e.g. "127.0.0.1:52431")."""
        return self._addr

    def token(self) -> str:
        """The pairing token external services must present."""
        return self._token

    def serve(self) -> None:
        """Begin dispatching to registered handlers. Non-blocking. The
        socket accepts from listen_local() on — requests arriving before
        serve() get a 503, which is distinguishable from a genuinely wrong
        path's 404."""
        self._serving = True

    def shutdown(self) -> None:
        """Stop the listener and remove the discovery file."""
        self._server.shutdown()
        self._server.server_close()
        _remove_discovery()

    def _dispatch(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        auth = handler.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            _respond(handler, 401, b"unauthorized")
            return
        # Constant-time comparison — `==` short-circuits at the first
        # differing byte, which is enough to recover the token one
        # character at a time through response timing.
        if not hmac.compare_digest(auth[7:], self._token):
            _respond(handler, 403, b"forbidden")
            return
        if not self._serving:
            _respond(handler, 503, b"listener not serving yet")
            return
        path = handler.path.split("?", 1)[0]
        route = self._routes.get(f"{handler.command} {path}")
        if route is None:
            _respond(handler, 404, b"not found")
            return
        try:
            result = route(ListenRequest(handler, path))
        except Exception as e:
            _respond(handler, 500, str(e).encode("utf-8"))
            return
        if isinstance(result, tuple):
            status, body = result
        else:
            status, body = 200, result
        if body is None:
            body = b""
        if isinstance(body, str):
            body = body.encode("utf-8")
        _respond(handler, status, body)


def _respond(handler: http.server.BaseHTTPRequestHandler, status: int, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    listener: Listener | None = None

    def __init__(self, sock: socket.socket):
        # Adopt an already-bound (and possibly already-listening) socket
        # instead of binding a fresh one.
        super().__init__(sock.getsockname()[:2], _RequestHandler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()[:2]


class _RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # stdout is the RPC channel
        pass

    def _handle(self):
        listener = self.server.listener
        if listener is None:
            _respond(self, 503, b"listener not ready")
            return
        listener._dispatch(self)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle
    do_HEAD = _handle


def listen_local(plugin) -> Listener:
    """Bind a localhost TCP port for an external service to connect to
    (or serve the actuator-granted inherited listener at fd 3 when
    present). Generates a pairing token and writes a connect.json
    discovery file to BRANCHKIT_PLUGIN_DIR."""
    token = secrets.token_hex(32)

    if inherited_listener_count() > 0:
        sock = socket.socket(fileno=3)
        # Tripwire for runtimes that accept an fd but silently bind a fresh
        # socket: when the actuator published the granted ports, serving
        # anywhere else means the inherited listener was dropped.
        granted = _granted_ports()
        port = sock.getsockname()[1]
        if granted and port not in granted:
            sock.close()
            raise OSError(
                f"runtime silently rebound the inherited listener "
                f"(serving :{port}, granted :{', :'.join(map(str, granted))})"
            )
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)

    server = _Server(sock)
    addr = f"127.0.0.1:{sock.getsockname()[1]}"
    listener = Listener(server, token, addr, plugin)
    server.listener = listener

    # A failed discovery write is fatal, not cosmetic: the external service
    # finds the port and token ONLY through connect.json.
    try:
        _write_discovery({"port": str(sock.getsockname()[1]), "token": token})
    except OSError as e:
        server.server_close()
        raise OSError(f"failed to write connect.json discovery file: {e}") from e

    # Accept immediately (pre-serve requests draw 503, matching TS); the
    # thread dies with the process.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return listener


def _write_discovery(info: dict) -> None:
    plugin_dir = os.environ.get("BRANCHKIT_PLUGIN_DIR")
    if not plugin_dir:
        return
    path = os.path.join(plugin_dir, "connect.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(info, indent=2))


def _remove_discovery() -> None:
    plugin_dir = os.environ.get("BRANCHKIT_PLUGIN_DIR")
    if not plugin_dir:
        return
    try:
        os.unlink(os.path.join(plugin_dir, "connect.json"))
    except OSError:
        pass

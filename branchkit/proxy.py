"""Transparent outbound proxy (the actuator's per-host network
enforcement: the sandbox allows no other egress than the actuator's
per-plugin CONNECT proxy).

When a plugin declares `"network": {"hosts": [...]}`, platforms without an
in-kernel per-host primitive run the plugin in a no-network sandbox whose
only egress is an actuator-run HTTP CONNECT proxy enforcing the declared
hostname allowlist. The actuator advertises the endpoint in
BRANCHKIT_PROXY:

    unix:///path/to/endpoint.sock  — UNIX socket (Linux, bind-mounted into
                                     the sandbox at the same path; and
                                     macOS, whose Seatbelt has no per-host
                                     primitive either)
    http://127.0.0.1:<port>        — localhost TCP (legacy Windows path)
    npipe://<pipe name>            — a named pipe ACLd to the container (Windows)

The SDK installs a `urllib.request` opener at import time, so a plugin
author writes ordinary `urllib.request.urlopen()` calls (and everything
built on them — UpstreamClient included) and the platform routes and
enforces. TLS tunnels opaquely (CONNECT, then a normal client-side
handshake). The target hostname travels BY NAME — inside the sandbox
there is no DNS. When BRANCHKIT_PROXY is unset (no `hosts` policy, or an
unsandboxed dev run), nothing is installed and requests go direct."""

from __future__ import annotations

import functools
import http.client
import os
import socket
import ssl
import sys
import urllib.request


def parse_proxy_url(v: str) -> tuple:
    """Parse a BRANCHKIT_PROXY value into ("unix", path) or
    ("tcp", host, port). Raises on unsupported schemes."""
    if v.startswith("unix://"):
        path = v[len("unix://"):]
        if not path:
            raise ValueError(f"empty proxy socket path in {v!r}")
        return ("unix", path)
    if v.startswith("http://"):
        rest = v[len("http://"):].rstrip("/")
        host, sep, port_s = rest.rpartition(":")
        if not sep or not port_s.isdigit() or int(port_s) <= 0:
            raise ValueError(f"proxy url {v!r} needs an explicit port")
        return ("tcp", host, int(port_s))
    if v.startswith("npipe://"):
        # Windows: a named pipe ACL'd to this container (no loopback exemption).
        path = v[len("npipe://"):]
        if not path:
            raise ValueError(f"empty proxy pipe name in {v!r}")
        return ("npipe", path)
    raise ValueError(f"unsupported BRANCHKIT_PROXY {v!r} (want unix://, http:// or npipe://)")


class HostRefusedError(OSError):
    """The platform proxy's refusal of a connection: the target is not a
    host the plugin's manifest declares. Raised by `dial()` and by the
    installed `urllib` opener for the same case. Branch on the class, not
    on the message::

        try:
            conn = branchkit.dial("homeassistant.local", 1883)
        except branchkit.HostRefusedError as e:
            ...  # the manifest does not declare e.host — tell the user

    A refusal is by-name and happens before any dial, so it is not a
    reachability failure — a declared host that is down is an ordinary
    OSError, not this."""

    def __init__(self, host: str, port: int, status_line: str):
        super().__init__(
            f"branchkit proxy refused CONNECT {host}:{port}: {status_line} "
            "(host not in the plugin's declared allowlist)"
        )
        self.host = host
        self.port = port
        #: The proxy's status line as received, e.g. "HTTP/1.1 403 Forbidden".
        self.status = status_line


def dial(host: str, port: int, timeout: float | None = None):
    """Open a raw TCP connection to ``host:port`` — for a protocol that is
    not HTTP: MQTT, a telnet-controlled receiver, a Redis-like local daemon.
    It is the same CONNECT tunnel the HTTP transport uses, so it is enforced
    and recorded exactly like HTTP.

    Inside the sandbox the plugin has no direct egress; the platform's
    filtering proxy is the only route and it enforces the manifest's
    declared host allowlist. ``dial`` is that route: when BRANCHKIT_PROXY
    is set the connection is a CONNECT tunnel through the proxy (unix:// on
    Linux and macOS, npipe:// on Windows — the same dial the installed
    ``urllib`` opener uses), and the proxy records every attempt as
    ``plugin.network_connect``. When BRANCHKIT_PROXY is unset (an
    unsandboxed dev run) the dial is direct.

    Returns a ``socket.socket``; on Windows, where the proxy is a named
    pipe, a socket-like object over the pipe (``sendall``, ``recv``,
    ``makefile``, ``close`` — the same surface ``http.client`` uses). A host
    the manifest does not declare is refused by the proxy and raises
    :class:`HostRefusedError`.

    ``timeout`` bounds the connect (the proxy dial and the CONNECT
    handshake included) and stays set on the socket for later reads and
    writes; ``None`` means blocking. Blocking either way — from an
    ``async def`` handler, call it through ``asyncio.to_thread``.

    TLS is the caller's: ``ssl.create_default_context().wrap_socket(conn,
    server_hostname=host)`` — the proxy tunnels bytes opaquely and never
    terminates TLS, so the allowlist decides which NAME you may dial, not
    who answers."""
    if not host:
        raise ValueError("dial: empty host")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"dial: port {port!r} out of range 1-65535")
    v = os.environ.get("BRANCHKIT_PROXY")
    if not v:
        return socket.create_connection((host, port), timeout=timeout)
    # Unlike the opener install (which prints and goes direct at import
    # time, where there is no caller to tell), a raw dial has one: a
    # malformed endpoint is a ValueError here, not a silent direct dial
    # that dies in the sandbox anyway.
    return _connect_tunnel(parse_proxy_url(v), host, port, timeout)


def _connect_tunnel(endpoint: tuple, host: str, port: int, timeout) -> socket.socket:
    """Dial the proxy endpoint and complete the CONNECT handshake to
    host:port. Returns a socket that is an opaque tunnel to the target.
    The proxy resolves the hostname host-side and refuses hosts outside
    the allowlist."""
    if endpoint[0] == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(endpoint[1])
    elif endpoint[0] == "npipe":
        from . import _pipe
        sock = _pipe.PipeConn(endpoint[1])
        # Opening a pipe cannot hang (a busy pipe fails at once), but the
        # CONNECT handshake below can: bound it like the unix/tcp branches.
        sock.settimeout(timeout)
    else:
        sock = socket.create_connection((endpoint[1], endpoint[2]), timeout=timeout)
    try:
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
        sock.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("proxy closed the connection during CONNECT")
            head += chunk
            if len(head) > 4096:
                raise OSError("oversized CONNECT response")
        status_line = head.split(b"\r\n", 1)[0].decode("latin1")
        parts = status_line.split()
        if len(parts) >= 2 and parts[1] == "403":
            # The allowlist refusal (host_proxy's RESP_FORBIDDEN) — typed, so
            # a caller can tell "not declared" from "declared but unreachable"
            # (a 400, below) without reading prose.
            raise HostRefusedError(host, port, status_line)
        if len(parts) < 2 or parts[1] != "200":
            raise OSError(f"branchkit proxy could not connect {host}:{port}: {status_line}")
        # Nothing follows the 200 head until we speak, so no residual bytes.
        return sock
    except BaseException:
        sock.close()
        raise


class _TunnelHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, branchkit_endpoint, **kwargs):
        super().__init__(host, **kwargs)
        self._branchkit_endpoint = branchkit_endpoint

    def connect(self):
        self.sock = _connect_tunnel(
            self._branchkit_endpoint, self.host, self.port, self.timeout
        )


class _TunnelHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, branchkit_endpoint, context=None, **kwargs):
        super().__init__(host, context=context, **kwargs)
        self._branchkit_endpoint = branchkit_endpoint
        self._branchkit_context = context or ssl.create_default_context()

    def connect(self):
        raw = _connect_tunnel(
            self._branchkit_endpoint, self.host, self.port, self.timeout
        )
        if self._branchkit_endpoint[0] == "npipe":
            # ssl.wrap_socket needs a real socket; a pipe is not one, and the
            # socketpair trick would use loopback the sandbox blocks. TLS runs
            # over the pipe through a MemoryBIO instead.
            from . import _pipe
            self.sock = _pipe.TlsPipe(raw, self.host, self._branchkit_context)
        else:
            self.sock = self._branchkit_context.wrap_socket(raw, server_hostname=self.host)


class _ProxyHTTPHandler(urllib.request.HTTPHandler):
    # Run before the default HTTPHandler (order 500) so ours wins.
    handler_order = 490

    def __init__(self, endpoint: tuple):
        super().__init__()
        self._endpoint = endpoint

    def http_open(self, req):
        return self.do_open(
            functools.partial(_TunnelHTTPConnection, branchkit_endpoint=self._endpoint),
            req,
        )


class _ProxyHTTPSHandler(urllib.request.HTTPSHandler):
    handler_order = 490

    def __init__(self, endpoint: tuple):
        super().__init__()
        self._endpoint = endpoint

    def https_open(self, req):
        return self.do_open(
            functools.partial(_TunnelHTTPSConnection, branchkit_endpoint=self._endpoint),
            req,
        )


def install_proxy_from_env() -> None:
    """Install a `urllib.request` opener routing through BRANCHKIT_PROXY.
    No-op when the env var is unset. Called once from the SDK entry
    module. Redirect following comes from urllib's own handlers, so
    proxied and direct requests behave alike."""
    v = os.environ.get("BRANCHKIT_PROXY")
    if not v:
        return
    try:
        endpoint = parse_proxy_url(v)
        opener = urllib.request.build_opener(
            _ProxyHTTPHandler(endpoint), _ProxyHTTPSHandler(endpoint)
        )
        urllib.request.install_opener(opener)
    except Exception as e:
        # A malformed value must not take the plugin down at import time —
        # requests will go direct and die in the sandbox, which is visible.
        print(f"[branchkit-sdk] ignoring invalid BRANCHKIT_PROXY: {e}", file=sys.stderr)

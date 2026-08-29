"""Transparent outbound proxy (the actuator's per-host network
enforcement — docs/design/DESIGN_SANDBOX_HOST_PROXY.md).

When a plugin declares `"network": {"hosts": [...]}`, platforms without an
in-kernel per-host primitive run the plugin in a no-network sandbox whose
only egress is an actuator-run HTTP CONNECT proxy enforcing the declared
hostname allowlist. The actuator advertises the endpoint in
BRANCHKIT_PROXY:

    unix:///path/to/endpoint.sock  — UNIX socket (Linux; bind-mounted into
                                     the sandbox at the same path)
    http://127.0.0.1:<port>        — localhost TCP (Windows)

The SDK installs a `urllib.request` opener at import time, so a plugin
author writes ordinary `urllib.request.urlopen()` calls (and everything
built on them — UpstreamClient included) and the platform routes and
enforces. TLS tunnels opaquely (CONNECT, then a normal client-side
handshake). The target hostname travels BY NAME — inside the sandbox
there is no DNS. When BRANCHKIT_PROXY is unset (macOS in-kernel
enforcement, unsandboxed dev), nothing is installed and requests go
direct."""

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
    raise ValueError(f"unsupported BRANCHKIT_PROXY {v!r} (want unix:// or http://)")


def _connect_tunnel(endpoint: tuple, host: str, port: int, timeout) -> socket.socket:
    """Dial the proxy endpoint and complete the CONNECT handshake to
    host:port. Returns a socket that is an opaque tunnel to the target.
    The proxy resolves the hostname host-side and refuses hosts outside
    the allowlist."""
    if endpoint[0] == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(endpoint[1])
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
        if len(parts) < 2 or parts[1] != "200":
            raise OSError(
                f"branchkit proxy refused CONNECT {host}:{port}: {status_line} "
                "(host not in the plugin's declared allowlist?)"
            )
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

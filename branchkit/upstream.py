"""UpstreamClient — outbound HTTP calls to an external service, with
configurable timeouts and a cached health check. Python twin of
upstream.{go,ts}. Requests go through `urllib.request.urlopen`, whose
installed opener the SDK routes through BRANCHKIT_PROXY when sandboxed
(see proxy.py) — the same transparent enforcement the other SDKs apply
to their default transports."""

from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request


class UpstreamResponse:
    """A fully-read response: `status`, `headers`, `body` (bytes), and
    `text()` for the decoded form."""

    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class UpstreamClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._health_ok = False
        self._health_at = 0.0

    def _do_blocking(self, method: str, path: str, body, timeout: float) -> UpstreamResponse:
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(self._base_url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return UpstreamResponse(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as e:
            # A status >= 400 is still a response, not a transport failure —
            # match fetch()/Go, which hand the caller the response.
            return UpstreamResponse(e.code, dict(e.headers or {}), e.read())

    async def do(self, method: str, path: str, body: bytes | str | None = None) -> UpstreamResponse:
        """Send an HTTP request to the upstream service. The body is read
        eagerly; transport failures raise (urllib.error.URLError)."""
        return await asyncio.to_thread(self._do_blocking, method, path, body, self._timeout)

    async def healthy(self) -> bool:
        """Whether the upstream is reachable. Cached for 2 seconds."""
        now = time.monotonic()
        if now - self._health_at < 2.0:
            return self._health_ok
        try:
            await asyncio.to_thread(self._do_blocking, "GET", "/", None, 2.0)
            self._health_ok = True
        except Exception:
            self._health_ok = False
        self._health_at = now
        return self._health_ok

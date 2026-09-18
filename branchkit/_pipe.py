r"""Windows named-pipe transport for the sandbox proxy and listener relay.

On Windows a plugin runs in an AppContainer with no loopback exemption, so the
actuator hands it the filtering proxy and the relay rendezvous over NAMED PIPES
ACL'd to the plugin's container SID (``npipe://\\.\pipe\...``), not loopback TCP
(the actuator's docs/design/DESIGN_WINDOWS_LOOPBACK_EXEMPTION.md).

A named pipe opens as a FILE, not a socket, and the stdlib's ``http.server`` and
``http.client`` want a socket. :class:`PipeConn` wraps one pipe file as enough
of a socket for both the relay (``http.server`` request handling) and the
plain-HTTP proxy path (``http.client``). It is deliberately NOT TLS-wrappable:
``ssl`` needs a real socket, and the socketpair adapter that would give it one
uses loopback — which is exactly what the sandbox blocks here. The HTTPS proxy
path over a pipe therefore needs an ``ssl.MemoryBIO`` layer, tracked separately.
"""

from __future__ import annotations

import io


class _Unclosable(io.RawIOBase):
    """A raw stream over a shared pipe file that never closes it.

    ``http.server`` makes a read file and a write file from one connection and
    closes each; the underlying duplex pipe must outlive both, so these
    forward to the shared :class:`io.FileIO` and no-op on close. Reads and
    writes are independent directions of the duplex pipe, so a BufferedReader
    over one and a BufferedWriter over another (both wrapping the same file)
    do not fight over a position — a pipe has none.
    """

    def __init__(self, f: io.FileIO):
        self._f = f

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._f.read(len(b))
        if not data:
            return 0
        n = len(data)
        b[:n] = data
        return n

    def write(self, b) -> int:
        return self._f.write(bytes(b))

    def close(self) -> None:
        # The shared pipe is owned by PipeConn.close(), not by makefile wrappers.
        pass


class PipeConn:
    """A minimal socket-like wrapper over a Windows named-pipe file (duplex).

    Enough of the socket surface for ``http.server`` (``makefile``, ``close``,
    ``getpeername``, ``shutdown``) and the plain-HTTP proxy CONNECT handshake
    (``sendall``, ``recv``, ``settimeout``). Construction opens the pipe and
    raises ``OSError`` if it cannot (busy or gone), so a caller can back off and
    retry exactly as it would a refused ``socket.create_connection``.
    """

    def __init__(self, path: str):
        # buffering=0 -> a raw io.FileIO; the pipe is opened read+write (duplex).
        # open() on Windows accepts a \\.\pipe\ path.
        self._f = open(path, "r+b", buffering=0)
        self._closed = False

    def sendall(self, data) -> None:
        mv = memoryview(data if isinstance(data, (bytes, bytearray)) else bytes(data))
        while mv:
            n = self._f.write(mv)
            if not n:
                continue
            mv = mv[n:]

    def recv(self, n: int) -> bytes:
        data = self._f.read(n)
        return data if data is not None else b""

    def settimeout(self, _timeout) -> None:
        # A pipe has no socket-level timeout; the relay pairs within a round
        # trip and the proxy answers immediately, so a blocking read is fine.
        pass

    def gettimeout(self):
        return None

    def getpeername(self):
        return ("pipe", 0)

    def getsockname(self):
        return ("pipe", 0)

    def makefile(self, mode: str = "rb", buffering: int = -1):
        raw = _Unclosable(self._f)
        if "w" in mode:
            return raw if buffering == 0 else io.BufferedWriter(raw)
        return io.BufferedReader(raw)

    def shutdown(self, _how=0) -> None:
        pass

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._f.close()
            except OSError:
                pass


class TlsPipe:
    """TLS over a :class:`PipeConn` (or any sendall/recv/close transport) via
    ``ssl.MemoryBIO`` — for an HTTPS request through the proxy PIPE, where
    ``ssl.wrap_socket`` cannot be used because there is no real socket. Exposes
    the socket surface ``http.client`` needs (``sendall``, ``recv``,
    ``makefile``, ``close``).
    """

    def __init__(self, transport, server_hostname, context):
        import ssl

        self._ssl = ssl
        self._t = transport
        self._inb = ssl.MemoryBIO()
        self._outb = ssl.MemoryBIO()
        self._obj = context.wrap_bio(self._inb, self._outb, server_hostname=server_hostname)
        self._closed = False
        # Socket-style makefile reference counting. ``http.client`` hands the
        # connection to the response on a ``Connection: close`` reply: right
        # after the headers it calls ``sock.close()`` and then keeps reading the
        # body through the ``makefile()`` object, trusting a real socket to stay
        # open until that file is closed too. We must honour the same contract —
        # tearing the TLS session down here (``unwrap()``) would poison reads of
        # the body already buffered in the incoming BIO. So ``close()`` only
        # requests teardown; it happens once the last makefile stream closes.
        self._io_refs = 0
        self._close_requested = False
        self._drive(self._obj.do_handshake)

    def _flush(self):
        data = self._outb.read()
        if data:
            self._t.sendall(data)

    def _feed(self):
        chunk = self._t.recv(16384)
        if not chunk:
            self._inb.write_eof()
        else:
            self._inb.write(chunk)

    def _drive(self, fn, *args):
        while True:
            try:
                result = fn(*args)
                self._flush()
                return result
            except self._ssl.SSLWantReadError:
                self._flush()
                self._feed()
            except self._ssl.SSLWantWriteError:
                self._flush()

    def sendall(self, data) -> None:
        mv = memoryview(data if isinstance(data, (bytes, bytearray)) else bytes(data))
        while mv:
            n = self._drive(self._obj.write, bytes(mv))
            mv = mv[n:]

    def recv(self, n: int) -> bytes:
        try:
            return self._drive(self._obj.read, n)
        except self._ssl.SSLZeroReturnError:
            return b""
        except self._ssl.SSLEOFError:
            # The peer closed without a TLS close_notify — normal for a
            # `Connection: close` response, where EOF delimits the body. Treat
            # it as end-of-stream (a plain socket read returns b"" here too);
            # http.client then decides truncation from Content-Length/chunking.
            return b""

    def settimeout(self, _t) -> None:
        pass

    def getpeername(self):
        return ("pipe-tls", 0)

    def makefile(self, mode: str = "rb", buffering: int = -1):
        self._io_refs += 1
        raw = _TlsRaw(self)
        if "w" in mode:
            return raw if buffering == 0 else io.BufferedWriter(raw)
        return io.BufferedReader(raw)

    def _decref(self) -> None:
        # A makefile stream closed. Once none remain, honour a deferred close.
        if self._io_refs > 0:
            self._io_refs -= 1
        if self._io_refs <= 0 and self._close_requested:
            self._real_close()

    def close(self) -> None:
        # Defer the real teardown while a makefile stream is still reading (the
        # response body). With no live streams, close immediately.
        self._close_requested = True
        if self._io_refs <= 0:
            self._real_close()

    def _real_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._obj.unwrap()
            self._flush()
        except (self._ssl.SSLError, OSError):
            pass
        self._t.close()


class _TlsRaw(io.RawIOBase):
    """Raw stream mapping http.client's rfile/wfile onto a :class:`TlsPipe`."""

    def __init__(self, tls: "TlsPipe"):
        self._tls = tls
        self._decref_done = False

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._tls.recv(len(b))
        if not data:
            return 0
        n = len(data)
        b[:n] = data
        return n

    def write(self, b) -> int:
        self._tls.sendall(bytes(b))
        return len(b)

    def close(self) -> None:
        # Drop this stream's reference exactly once; the pipe closes when the
        # last live stream does. close() may be called more than once (explicit
        # close plus __del__), so guard the decrement.
        if not self._decref_done:
            self._decref_done = True
            self._tls._decref()

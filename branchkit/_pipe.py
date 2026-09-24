r"""Windows named-pipe transport for the sandbox proxy and listener relay.

On Windows a plugin runs in an AppContainer with no loopback exemption, so the
actuator hands it the filtering proxy and the relay rendezvous over NAMED PIPES
ACL'd to the plugin's container SID (``npipe://\\.\pipe\...``), not loopback TCP
(a loopback exemption would open every loopback port on the machine).

A named pipe opens as a FILE, not a socket, and the stdlib's ``http.server`` and
``http.client`` want a socket. :class:`PipeConn` wraps one pipe file as enough
of a socket for both the relay (``http.server`` request handling) and the
plain-HTTP proxy path (``http.client``). It is deliberately NOT TLS-wrappable:
``ssl`` needs a real socket, and the socketpair adapter that would give it one
uses loopback — which is exactly what the sandbox blocks here. The HTTPS proxy
path over a pipe therefore needs an ``ssl.MemoryBIO`` layer (:class:`TlsPipe`).

Timeouts. A socket gets deadlines from the kernel; a pipe opened with plain
``open()`` has none, so ``settimeout`` used to be a no-op and a proxy that
accepted the pipe and never answered hung the plugin forever (Go gets real
deadlines from go-winio). The pipe is now opened OVERLAPPED and every read and
write waits on its completion event with a bound, cancelling the I/O when the
bound passes — the same stdlib ``_winapi`` primitives
``multiprocessing.connection.PipeConnection`` uses, so no ctypes. ``settimeout``
/ ``gettimeout`` follow socket semantics: ``None`` blocks, ``0`` polls, a
positive number bounds each ``recv`` and the whole of ``sendall``, and expiry
raises ``TimeoutError`` (``socket.timeout``).
"""

from __future__ import annotations

import io
import math
import socket
import time

# Win32 codes. Spelled out: _winapi does not export all of them everywhere.
_ERROR_BROKEN_PIPE = 109
_ERROR_OPERATION_ABORTED = 995
_ERROR_IO_PENDING = 997
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF


def _timeout_ms(timeout) -> int:
    """Seconds (None = forever) -> a WaitForSingleObject bound. Rounded UP so
    a small positive timeout never degrades to a 0 ms poll."""
    if timeout is None:
        return _INFINITE
    return min(int(math.ceil(timeout * 1000)), _INFINITE - 1)


def _is_broken_pipe(exc: OSError) -> bool:
    return isinstance(exc, BrokenPipeError) or getattr(exc, "winerror", None) == _ERROR_BROKEN_PIPE


class _OverlappedPipe:
    """One named-pipe HANDLE opened for overlapped I/O, with bounded waits.

    ``winapi`` is the stdlib ``_winapi`` module in production and a fake in
    the tests (which run on every OS); nothing here reaches Windows another way.
    """

    def __init__(self, handle, winapi):
        self._h = handle
        self._w = winapi
        self._closed = False

    @classmethod
    def open(cls, path: str) -> "_OverlappedPipe":
        import _winapi  # Windows-only stdlib module

        w = _winapi
        # Raises OSError (busy / gone / access denied) exactly as open() did.
        h = w.CreateFile(
            path,
            w.GENERIC_READ | w.GENERIC_WRITE,
            0,
            w.NULL,
            w.OPEN_EXISTING,
            w.FILE_FLAG_OVERLAPPED,
            w.NULL,
        )
        return cls(h, w)

    def _finish(self, ov, timeout) -> tuple[int, bool]:
        """Wait for a started overlapped op: (transferred, timed_out).

        On expiry the op is cancelled and then waited for, because the kernel
        may have completed it in the race — a read that did complete must hand
        its bytes back rather than drop them.
        """
        res = self._w.WaitForSingleObject(ov.event, _timeout_ms(timeout))
        timed_out = res == _WAIT_TIMEOUT
        if timed_out:
            try:
                ov.cancel()
            except OSError:
                pass  # already completed
        elif res != _WAIT_OBJECT_0:
            ov.cancel()
            ov.GetOverlappedResult(True)
            raise OSError(f"pipe: WaitForSingleObject returned {res}")
        n, err = ov.GetOverlappedResult(True)
        if err == _ERROR_OPERATION_ABORTED and not timed_out:
            raise OSError("pipe: I/O aborted")
        return n, timed_out and n == 0

    def read(self, n: int, timeout) -> bytes:
        """Up to ``n`` bytes; b"" at EOF (the server end closed)."""
        try:
            ov, err = self._w.ReadFile(self._h, n, overlapped=True)
            if err == _ERROR_IO_PENDING:
                got, timed_out = self._finish(ov, timeout)
                if timed_out:
                    raise socket.timeout("timed out")
            else:
                # Completed synchronously (0), or ERROR_MORE_DATA on a message-
                # mode pipe (the remainder comes with the next read).
                got, _ = ov.GetOverlappedResult(True)
        except OSError as exc:
            if not isinstance(exc, TimeoutError) and _is_broken_pipe(exc):
                return b""
            raise
        return bytes(ov.getbuffer()[:got]) if got else b""

    def write(self, data, timeout) -> int:
        ov, err = self._w.WriteFile(self._h, bytes(data), overlapped=True)
        if err == _ERROR_IO_PENDING:
            n, timed_out = self._finish(ov, timeout)
            if timed_out:
                raise socket.timeout("timed out")
            return n
        n, _ = ov.GetOverlappedResult(True)
        return n

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._w.CloseHandle(self._h)


class _Unclosable(io.RawIOBase):
    """A raw stream over a shared pipe file that outlives the makefile wrappers.

    ``http.server`` makes a read file and a write file from one connection and
    closes each; the underlying duplex pipe must outlive both. Reads and writes
    are independent directions of the duplex pipe, so a BufferedReader over one
    and a BufferedWriter over another (both wrapping the same file) do not fight
    over a position — a pipe has none.

    Closing a makefile stream does not close the pipe; it drops one reference on
    the owning :class:`PipeConn`, which (like a real socket) closes the pipe only
    once every makefile stream is closed AND :meth:`PipeConn.close` was called.
    That is what lets ``http.client`` close the connection right after the
    headers (a ``Connection: close`` reply) and still read the body through this
    stream.
    """

    def __init__(self, pc: "PipeConn"):
        self._pc = pc

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        # Through recv/_send, not the file, so the connection's timeout
        # applies to makefile reads and writes as it does on a socket.
        data = self._pc.recv(len(b))
        if not data:
            return 0
        n = len(data)
        b[:n] = data
        return n

    def write(self, b) -> int:
        return self._pc._send(b)

    def close(self) -> None:
        # socket.SocketIO's shape: mark THIS stream closed (so ``.closed`` is
        # honest and a finalizer never re-flushes into it), then drop its one
        # reference. IOBase.close is a no-op once closed, and __del__ skips a
        # closed stream, so the early return is the exactly-once guard.
        if self.closed:
            return
        super().close()
        self._pc._decref()


class PipeConn:
    """A minimal socket-like wrapper over a Windows named-pipe file (duplex).

    Enough of the socket surface for ``http.server`` (``makefile``, ``close``,
    ``getpeername``, ``shutdown``) and the plain-HTTP proxy CONNECT handshake
    (``sendall``, ``recv``, ``settimeout``). Construction opens the pipe and
    raises ``OSError`` if it cannot (busy or gone), so a caller can back off and
    retry exactly as it would a refused ``socket.create_connection``.
    """

    def __init__(self, path: str):
        # Duplex, overlapped: see the module docstring on timeouts.
        self._f = _OverlappedPipe.open(path)
        self._closed = False
        # Like a new socket: the process-wide default (None unless changed).
        self._timeout = socket.getdefaulttimeout()
        # Socket-style makefile reference counting (see _Unclosable): close()
        # defers the real teardown while a makefile stream is still reading.
        self._io_refs = 0
        self._close_requested = False

    def _send(self, data, timeout=...) -> int:
        return self._f.write(data, self._timeout if timeout is ... else timeout)

    def sendall(self, data) -> None:
        # Like socket.sendall since 3.5, the timeout bounds the WHOLE call, not
        # each chunk — a peer draining one byte per timeout can't stretch it.
        mv = memoryview(data if isinstance(data, (bytes, bytearray)) else bytes(data))
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while mv:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            n = self._send(mv, left)
            if not n:
                if deadline is not None and time.monotonic() >= deadline:
                    raise socket.timeout("timed out")
                continue
            mv = mv[n:]

    def recv(self, n: int) -> bytes:
        return self._f.read(n, self._timeout)

    def settimeout(self, timeout) -> None:
        if timeout is not None:
            timeout = float(timeout)
            if timeout < 0:
                raise ValueError("Timeout value out of range")
        self._timeout = timeout

    def gettimeout(self):
        return self._timeout

    def getpeername(self):
        return ("pipe", 0)

    def getsockname(self):
        return ("pipe", 0)

    def makefile(self, mode: str = "rb", buffering: int = -1):
        self._io_refs += 1
        raw = _Unclosable(self)
        if "w" in mode:
            return raw if buffering == 0 else io.BufferedWriter(raw)
        return io.BufferedReader(raw)

    def shutdown(self, _how=0) -> None:
        pass

    def _decref(self) -> None:
        if self._io_refs > 0:
            self._io_refs -= 1
        if self._io_refs <= 0 and self._close_requested:
            self._real_close()

    def close(self) -> None:
        # Defer the real teardown while a makefile stream is still reading (a
        # response body). With no live streams, close immediately.
        self._close_requested = True
        if self._io_refs <= 0:
            self._real_close()

    def _real_close(self) -> None:
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

    def settimeout(self, timeout) -> None:
        # The deadline lives on the transport: every byte TLS moves goes
        # through its recv/sendall, so a stalled peer times out mid-handshake
        # or mid-record exactly as a plain read would.
        self._t.settimeout(timeout)

    def gettimeout(self):
        return self._t.gettimeout()

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
        # last live stream does. Same shape as _Unclosable.close: ``.closed``
        # goes True, and a second close (explicit plus __del__) returns early.
        if self.closed:
            return
        super().close()
        self._tls._decref()

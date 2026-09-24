"""Tests for the Windows named-pipe transport shims (`branchkit._pipe`).

The classes are Windows-only in production, but the bug they carried was in a
transport-agnostic state machine — socket-style ``makefile()`` reference
counting — so these run anywhere over a ``socketpair`` (no named pipe, no
Windows). The regression they guard: ``http.client`` closes the connection
right after the headers on a ``Connection: close`` reply, then keeps reading the
body through the ``makefile()`` stream, trusting a real socket to stay open
until that stream is closed too. A shim that tore its transport down on
``close()`` truncated every such body — for ``TlsPipe`` by calling
``SSLObject.unwrap()``, which poisons decryption of the body already buffered in
the incoming BIO.
"""

import http.client
import socket
import ssl
import threading
import unittest

from branchkit import _pipe


class _SockFile:
    """A read/write/close file-like adapter over one end of a socketpair, so a
    PipeConn (which speaks ``.read``/``.write``/``.close`` on ``self._f``) can
    run over a real duplex stream on any OS."""

    def __init__(self, sock: socket.socket):
        self._s = sock

    def read(self, n: int, timeout=None):
        self._s.settimeout(timeout)
        return self._s.recv(n)

    def write(self, b, timeout=None) -> int:
        self._s.settimeout(timeout)
        return self._s.send(b)

    def close(self) -> None:
        self._s.close()


def _pipeconn_over(sock: socket.socket) -> "_pipe.PipeConn":
    """A PipeConn wired to `sock` without opening a named pipe path."""
    pc = _pipe.PipeConn.__new__(_pipe.PipeConn)
    pc._f = _SockFile(sock)
    pc._closed = False
    pc._timeout = None
    pc._io_refs = 0
    pc._close_requested = False
    return pc


class _PipeHTTPConnection(http.client.HTTPConnection):
    """An HTTPConnection whose socket is a given PipeConn — the exact wiring the
    real plain-HTTP proxy path uses (`self.sock = PipeConn(...)`)."""

    def __init__(self, pc: "_pipe.PipeConn"):
        super().__init__("pipe.invalid")
        self._pc = pc

    def connect(self):  # noqa: D401 - override
        self.sock = self._pc


def _serve_once(sock: socket.socket, raw_response: bytes) -> threading.Thread:
    """Read one HTTP request off `sock`, write `raw_response`, then close —
    a one-shot origin server on the far end of the socketpair."""

    def run():
        sock.settimeout(5)
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                d = sock.recv(4096)
                if not d:
                    break
                buf += d
            sock.sendall(raw_response)
        except OSError:
            pass
        finally:
            sock.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _chunked(body: bytes, connection_close: bool = True) -> bytes:
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
    if connection_close:
        head += b"Connection: close\r\n"
    head += b"\r\n"
    out = head
    for i in range(0, len(body), 4096):
        part = body[i : i + 4096]
        out += (b"%x\r\n" % len(part)) + part + b"\r\n"
    return out + b"0\r\n\r\n"


class TestPipeConnHTTPEndToEnd(unittest.TestCase):
    """The real regression, driven through http.client over a socketpair."""

    def _round_trip(self, raw_response: bytes) -> "http.client.HTTPResponse":
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client_sock.close)
        _serve_once(server_sock, raw_response)
        pc = _pipeconn_over(client_sock)
        conn = _PipeHTTPConnection(pc)
        self.addCleanup(conn.close)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.addCleanup(resp.close)
        return resp

    def test_chunked_connection_close_body_arrives_whole(self):
        # Connection: close makes getresponse() close the socket after the
        # headers — the trigger. Before the fix this truncated the body.
        payload = bytes((65 + i % 26) for i in range(128 * 1024))
        resp = self._round_trip(_chunked(payload, connection_close=True))
        self.assertEqual(resp.status, 200)
        body = resp.read()
        self.assertEqual(len(body), len(payload))
        self.assertEqual(body, payload)

    def test_content_length_body_arrives_whole(self):
        payload = b'{"ok":true}'
        raw = (
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%b"
            % (len(payload), payload)
        )
        resp = self._round_trip(raw)
        self.assertEqual(resp.read(), payload)


class TestPipeConnRefcount(unittest.TestCase):
    """The makefile reference-counting state machine, directly."""

    def _pc(self):
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client_sock.close)
        self.addCleanup(server_sock.close)
        return _pipeconn_over(client_sock)

    def test_close_defers_while_a_stream_is_open(self):
        pc = self._pc()
        f = pc.makefile("rb")
        self.assertEqual(pc._io_refs, 1)
        pc.close()  # request teardown, but a stream is live
        self.assertFalse(pc._closed, "close must defer while a makefile stream is open")
        f.close()
        self.assertTrue(pc._closed, "real close fires when the last stream closes")

    def test_relay_pattern_two_streams_then_explicit_close(self):
        # http.server makes rfile + wfile, closes both at finish, then the relay
        # closes the connection. The pipe must survive until that final close.
        pc = self._pc()
        r = pc.makefile("rb")
        # buffering=0 -> the raw stream, so no BufferedWriter finalizer tries to
        # re-flush into the shared pipe (which never reports itself closed). The
        # reference count is what this asserts, and it is the same either way.
        w = pc.makefile("wb", buffering=0)
        self.assertEqual(pc._io_refs, 2)
        w.close()
        r.close()
        self.assertFalse(pc._closed, "streams closed but no explicit close yet -> still open")
        pc.close()
        self.assertTrue(pc._closed)

    def test_close_with_no_streams_is_immediate(self):
        pc = self._pc()
        pc.close()
        self.assertTrue(pc._closed)

    def test_stream_close_without_teardown_request_keeps_pipe_open(self):
        # The relay invariant: makefile streams closing on their own must NOT
        # tear down the shared pipe — only an explicit PipeConn.close() may.
        pc = self._pc()
        f = pc.makefile("rb")
        f.close()
        self.assertFalse(pc._closed, "no close() requested -> pipe stays open")

    def test_double_stream_close_after_teardown_is_safe(self):
        pc = self._pc()
        f = pc.makefile("rb")
        pc.close()  # teardown requested, deferred behind the live stream
        f.close()  # last stream -> real close
        f.close()  # double close: no underflow, no reopen
        self.assertTrue(pc._closed)
        self.assertLessEqual(pc._io_refs, 0)


class _FakeSSLObj:
    """Records unwrap() so the test can prove teardown is deferred."""

    def __init__(self):
        self.unwrapped = 0

    def unwrap(self):
        self.unwrapped += 1
        return None


class _RecordingTransport:
    def __init__(self):
        self.closed = False

    def sendall(self, _data):
        pass

    def recv(self, _n):
        return b""

    def close(self):
        self.closed = True


class TestTlsPipeRefcount(unittest.TestCase):
    """TlsPipe carries the same contract, and its teardown is the poison:
    unwrap() must not run until the last makefile stream is closed."""

    def _tls(self):
        t = _pipe.TlsPipe.__new__(_pipe.TlsPipe)
        t._ssl = ssl
        t._t = _RecordingTransport()
        t._obj = _FakeSSLObj()
        t._outb = ssl.MemoryBIO()
        t._closed = False
        t._io_refs = 0
        t._close_requested = False
        return t

    def test_unwrap_and_transport_close_deferred_until_stream_closes(self):
        t = self._tls()
        f = t.makefile("rb")
        t.close()
        self.assertEqual(t._obj.unwrapped, 0, "unwrap() must not run while a stream is open")
        self.assertFalse(t._t.closed, "transport must stay open while a stream is open")
        f.close()
        self.assertEqual(t._obj.unwrapped, 1, "unwrap() runs exactly once, at real close")
        self.assertTrue(t._t.closed)

    def test_close_with_no_streams_tears_down_immediately(self):
        t = self._tls()
        t.close()
        self.assertEqual(t._obj.unwrapped, 1)
        self.assertTrue(t._t.closed)

    def test_double_stream_close_after_teardown_unwraps_once(self):
        t = self._tls()
        f = t.makefile("rb")
        t.close()  # teardown requested, deferred behind the live stream
        f.close()  # last stream -> real close, unwrap once
        f.close()  # double close: no second unwrap
        self.assertEqual(t._obj.unwrapped, 1, "unwrap() must run exactly once")


class _FakeOv:
    """A _winapi.Overlapped stand-in: completes with ``data`` when (if ever)
    the fake kernel finishes it, or with ERROR_OPERATION_ABORTED on cancel."""

    def __init__(self, data=b"", completes=True, sync=False, raise_on_result=None):
        self.event = object()
        self.data = data
        self.completes = completes
        self.sync = sync
        self.cancelled = False
        self.raise_on_result = raise_on_result

    def cancel(self):
        self.cancelled = True

    def GetOverlappedResult(self, wait):
        assert wait, "must wait for the op to settle before touching its buffer"
        if self.raise_on_result:
            raise self.raise_on_result
        if self.completes:
            return len(self.data), 0
        assert self.cancelled, "an unfinished op must be cancelled before waiting"
        return 0, _pipe._ERROR_OPERATION_ABORTED

    def getbuffer(self):
        return self.data


class _FakeWinapi:
    """Only the calls _OverlappedPipe makes. ``ov`` is the op the next
    ReadFile/WriteFile starts; ``waits`` records every bound passed."""

    def __init__(self, ov):
        self.ov = ov
        self.waits = []
        self.closed = 0

    def _start(self):
        if self.ov.sync:
            return self.ov, 0
        return self.ov, _pipe._ERROR_IO_PENDING

    def ReadFile(self, _h, _n, overlapped):
        assert overlapped
        return self._start()

    def WriteFile(self, _h, _buf, overlapped):
        assert overlapped
        return self._start()

    def WaitForSingleObject(self, _event, ms):
        self.waits.append(ms)
        return _pipe._WAIT_OBJECT_0 if self.ov.completes else _pipe._WAIT_TIMEOUT

    def CloseHandle(self, _h):
        self.closed += 1


def _ovpipe(ov):
    w = _FakeWinapi(ov)
    return _pipe._OverlappedPipe(object(), w), w


class TestOverlappedPipe(unittest.TestCase):
    """The Windows I/O path against a fake _winapi. This proves the control
    flow (bounds, cancel-then-settle, EOF mapping), not Windows itself."""

    def test_read_completes(self):
        p, w = _ovpipe(_FakeOv(b"hello"))
        self.assertEqual(p.read(16, 2.5), b"hello")
        self.assertEqual(w.waits, [2500])

    def test_none_waits_forever(self):
        p, w = _ovpipe(_FakeOv(b"x"))
        p.read(1, None)
        self.assertEqual(w.waits, [_pipe._INFINITE])

    def test_tiny_timeout_rounds_up_not_to_a_poll(self):
        p, w = _ovpipe(_FakeOv(b"x"))
        p.read(1, 0.0001)
        self.assertEqual(w.waits, [1])

    def test_read_timeout_cancels_and_raises(self):
        ov = _FakeOv(completes=False)
        p, _ = _ovpipe(ov)
        with self.assertRaises(socket.timeout):
            p.read(16, 0.5)
        self.assertTrue(ov.cancelled)

    def test_read_that_won_the_cancel_race_keeps_its_bytes(self):
        # The wait expired, but the kernel completed the read before cancel.
        ov = _FakeOv(b"late")
        p, w = _ovpipe(ov)
        w.WaitForSingleObject = lambda _e, ms: _pipe._WAIT_TIMEOUT
        self.assertEqual(p.read(16, 0.5), b"late")
        self.assertTrue(ov.cancelled)

    def test_sync_completion(self):
        p, w = _ovpipe(_FakeOv(b"now", sync=True))
        self.assertEqual(p.read(16, 1), b"now")
        self.assertEqual(w.waits, [])

    def test_broken_pipe_is_eof(self):
        p, _ = _ovpipe(_FakeOv(raise_on_result=BrokenPipeError()))
        self.assertEqual(p.read(16, 1), b"")

    def test_write_timeout_cancels_and_raises(self):
        ov = _FakeOv(completes=False)
        p, _ = _ovpipe(ov)
        with self.assertRaises(socket.timeout):
            p.write(b"abc", 0.5)
        self.assertTrue(ov.cancelled)

    def test_write_returns_count(self):
        p, _ = _ovpipe(_FakeOv(b"abc"))
        self.assertEqual(p.write(b"abc", None), 3)

    def test_close_once(self):
        p, w = _ovpipe(_FakeOv())
        p.close()
        p.close()
        self.assertEqual(w.closed, 1)


class TestPipeConnTimeout(unittest.TestCase):
    """settimeout is real now: a peer that never answers raises instead of
    hanging the plugin (the hung-proxy case)."""

    def _pc(self):
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client_sock.close)
        self.addCleanup(server_sock.close)
        return _pipeconn_over(client_sock), server_sock

    def test_settimeout_gettimeout_socket_semantics(self):
        pc, _ = self._pc()
        self.assertIsNone(pc.gettimeout())
        pc.settimeout(3)
        self.assertEqual(pc.gettimeout(), 3.0)
        pc.settimeout(None)
        self.assertIsNone(pc.gettimeout())
        with self.assertRaises(ValueError):
            pc.settimeout(-1)

    def test_recv_times_out_on_silent_peer(self):
        pc, _ = self._pc()
        pc.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            pc.recv(1)

    def test_makefile_read_times_out_on_silent_peer(self):
        pc, _ = self._pc()
        pc.settimeout(0.05)
        f = pc.makefile("rb")
        self.addCleanup(f.close)
        with self.assertRaises(socket.timeout):
            f.readline()

    def test_timeout_passed_to_transport(self):
        seen = []

        class Rec:
            def read(self, n, timeout):
                seen.append(timeout)
                return b"x"

            def write(self, b, timeout):
                seen.append(timeout)
                return len(b)

        pc = _pipe.PipeConn.__new__(_pipe.PipeConn)
        pc._f, pc._timeout = Rec(), 2.0
        pc.recv(1)
        pc.sendall(b"abc")
        self.assertEqual(seen[0], 2.0)
        self.assertTrue(0 < seen[1] <= 2.0, "sendall passes the REMAINING budget")

    def test_tls_pipe_delegates_timeout_to_transport(self):
        pc, _ = self._pc()
        t = _pipe.TlsPipe.__new__(_pipe.TlsPipe)
        t._t = pc
        t.settimeout(1.5)
        self.assertEqual(pc.gettimeout(), 1.5)
        self.assertEqual(t.gettimeout(), 1.5)


class TestStreamClosedFlag(unittest.TestCase):
    """close() now marks the makefile stream closed (it used to stay
    ``closed == False``) without disturbing the reference count."""

    def test_pipeconn_stream_reports_closed(self):
        client_sock, server_sock = socket.socketpair()
        self.addCleanup(client_sock.close)
        self.addCleanup(server_sock.close)
        pc = _pipeconn_over(client_sock)
        r = pc.makefile("rb")
        w = pc.makefile("wb", buffering=0)
        r.close()
        w.close()
        self.assertTrue(r.closed)
        self.assertTrue(w.closed)
        self.assertEqual(pc._io_refs, 0)
        self.assertFalse(pc._closed, "stream close alone must not close the pipe")
        del r, w  # finalizers must not decref again
        self.assertEqual(pc._io_refs, 0)

    def test_tls_stream_reports_closed(self):
        t = TestTlsPipeRefcount._tls(None)
        f = t.makefile("rb")
        t.close()
        f.close()
        self.assertTrue(f.closed)
        self.assertEqual(t._obj.unwrapped, 1)


if __name__ == "__main__":
    unittest.main()

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

    def read(self, n: int):
        return self._s.recv(n)

    def write(self, b) -> int:
        return self._s.send(b)

    def close(self) -> None:
        self._s.close()


def _pipeconn_over(sock: socket.socket) -> "_pipe.PipeConn":
    """A PipeConn wired to `sock` without opening a named pipe path."""
    pc = _pipe.PipeConn.__new__(_pipe.PipeConn)
    pc._f = _SockFile(sock)
    pc._closed = False
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


if __name__ == "__main__":
    unittest.main()

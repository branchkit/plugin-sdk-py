"""The relay branch of listen_local, with a stand-in for the actuator's
relay (listener_relay.rs): parked plugin connections presenting the right
header, OK written on one per client, bytes pumped both ways."""

import http.client
import os
import socket
import threading
import unittest

from branchkit import listen

TOKEN = "0123456789abcdef0123456789abcdef"


class FakeRelay:
    def __init__(self, with_peer: bool = True):
        # with_peer: answer "OK <client>" (the current protocol), else the
        # bare "OK" of version 1.
        self.with_peer = with_peer
        # Every socket the relay opens, accepted or listening, so stop() can
        # close them all: left to the GC they surface as `ResourceWarning:
        # unclosed socket`, which hides a real leak in the SDK.
        self._socks: list[socket.socket] = []
        self._socks_lock = threading.Lock()
        self.alive = True
        self.rv = self._track(socket.socket()); self.rv.bind(("127.0.0.1", 0)); self.rv.listen(16)
        self.pub = self._track(socket.socket()); self.pub.bind(("127.0.0.1", 0)); self.pub.listen(16)
        self.parked: list[socket.socket] = []
        self.cv = threading.Condition()
        threading.Thread(target=self._rendezvous, daemon=True).start()
        threading.Thread(target=self._public, daemon=True).start()

    def _track(self, sock):
        with self._socks_lock:
            if self.alive:
                self._socks.append(sock)
                return sock
        sock.close()  # accepted after stop(): close at once
        return sock

    def _rendezvous(self):
        while self.alive:
            try:
                s, _ = self.rv.accept()
            except OSError:
                return
            self._track(s)
            line = b""
            while not line.endswith(b"\n"):
                b = s.recv(1)
                if not b:
                    break
                line += b
            if line == f"{listen._RELAY_HEADER_PREFIX}trial {TOKEN}\n".encode():
                with self.cv:
                    self.parked.append(s); self.cv.notify_all()
            else:
                s.close()

    def _public(self):
        while self.alive:
            try:
                c, client_addr = self.pub.accept()
            except OSError:
                return
            self._track(c)
            with self.cv:
                self.cv.wait_for(lambda: self.parked, timeout=5)
                p = self.parked.pop(0) if self.parked else None
            if p is None:
                c.close(); continue
            if self.with_peer:
                p.sendall(("OK %s:%d\n" % client_addr[:2]).encode())
            else:
                p.sendall(b"OK\n")
            threading.Thread(target=self._pump, args=(c, p), daemon=True).start()
            threading.Thread(target=self._pump, args=(p, c), daemon=True).start()

    @staticmethod
    def _pump(a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except OSError:
            pass
        finally:
            try: b.shutdown(socket.SHUT_WR)
            except OSError: pass

    def stop(self):
        with self._socks_lock:
            self.alive = False
            socks, self._socks = self._socks, []
        for s in socks:
            # shutdown first: it wakes a pump thread blocked in recv on this
            # socket, which a bare close() does not reliably do.
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            s.close()


class ListenRelayTest(unittest.TestCase):
    def test_listen_local_serves_through_the_actuators_relay(self):
        relay = FakeRelay()
        env = dict(os.environ)
        os.environ["LISTEN_FDS"] = ""
        os.environ["BRANCHKIT_LISTEN_RELAY"] = "127.0.0.1:%d" % relay.rv.getsockname()[1]
        os.environ["BRANCHKIT_LISTEN_RELAY_TOKEN"] = TOKEN
        os.environ["BRANCHKIT_LISTEN_PORTS"] = "trial=%d" % relay.pub.getsockname()[1]
        os.environ.pop("BRANCHKIT_PLUGIN_DIR", None)
        try:
            ln = listen.listen_local(plugin=None)
            self.assertEqual(ln.addr(), "127.0.0.1:%d" % relay.pub.getsockname()[1])
            ln.handle_func("GET", "/ping", lambda req: "pong")
            ln.serve()
            for _ in range(3):  # the pool refills after each pairing
                conn = http.client.HTTPConnection("127.0.0.1", relay.pub.getsockname()[1], timeout=5)
                conn.request("GET", "/ping", headers={"Authorization": "Bearer " + ln.token()})
                r = conn.getresponse()
                self.assertEqual((r.status, r.read()), (200, b"pong"))
                conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", relay.pub.getsockname()[1], timeout=5)
            conn.request("GET", "/ping")
            self.assertEqual(conn.getresponse().status, 401)
            conn.close()
            ln.shutdown()  # must not block: relay mode never ran serve_forever
        finally:
            os.environ.clear(); os.environ.update(env)
            relay.stop()

    def test_a_relayed_request_carries_the_clients_address(self):
        # The plugin holds only the rendezvous connection; the actuator's
        # answer carries who the client is, and remote_addr reports it.
        for with_peer in (True, False):
            relay = FakeRelay(with_peer)
            env = dict(os.environ)
            os.environ["LISTEN_FDS"] = ""
            os.environ["BRANCHKIT_LISTEN_RELAY"] = "127.0.0.1:%d" % relay.rv.getsockname()[1]
            os.environ["BRANCHKIT_LISTEN_RELAY_TOKEN"] = TOKEN
            os.environ["BRANCHKIT_LISTEN_PORTS"] = "trial=%d" % relay.pub.getsockname()[1]
            os.environ.pop("BRANCHKIT_PLUGIN_DIR", None)
            try:
                ln = listen.listen_local(plugin=None)
                ln.handle_func("GET", "/peer", lambda req: req.remote_addr)
                ln.serve()
                conn = http.client.HTTPConnection("127.0.0.1", relay.pub.getsockname()[1], timeout=5)
                conn.connect()
                mine = "127.0.0.1:%d" % conn.sock.getsockname()[1]
                conn.request("GET", "/peer", headers={"Authorization": "Bearer " + ln.token()})
                r = conn.getresponse()
                body = r.read().decode()
                conn.close()
                self.assertEqual(r.status, 200)
                if with_peer:
                    self.assertEqual(body, mine)
                else:
                    self.assertNotEqual(body, mine)
                ln.shutdown()
            finally:
                os.environ.clear(); os.environ.update(env)
                relay.stop()


class ParseRelayAnswerTest(unittest.TestCase):
    def test_answers(self):
        cases = [
            (b"OK 127.0.0.1:50741", (True, ("127.0.0.1", 50741))),
            (b"OK [::1]:50741", (True, ("::1", 50741))),
            (b"OK", (True, None)),
            (b"OK ", (False, None)),
            (b"OK nonsense", (False, None)),
            (b"OK 127.0.0.1:0", (False, None)),
            (b"OK 127.0.0.1:70000", (False, None)),
            (b"OK [127.0.0.1]:5", (False, None)),
            (b"NO 127.0.0.1:1", (False, None)),
            (b"", (False, None)),
        ]
        for line, want in cases:
            self.assertEqual(listen._parse_relay_answer(line), want, line)

    def test_format_addr(self):
        self.assertEqual(listen._format_addr(("127.0.0.1", 5)), "127.0.0.1:5")
        self.assertEqual(listen._format_addr(("::1", 5, 0, 0)), "[::1]:5")
        self.assertEqual(listen._format_addr(None), "")


if __name__ == "__main__":
    unittest.main()

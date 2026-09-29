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
    def __init__(self):
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
                c, _ = self.pub.accept()
            except OSError:
                return
            self._track(c)
            with self.cv:
                self.cv.wait_for(lambda: self.parked, timeout=5)
                p = self.parked.pop(0) if self.parked else None
            if p is None:
                c.close(); continue
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


if __name__ == "__main__":
    unittest.main()

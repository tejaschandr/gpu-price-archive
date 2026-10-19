"""Fail any test that tries to reach a real host. Loaded at discovery, before any test runs,
so it covers the whole suite. Loopback stays allowed for tests/test_http.py."""

import socket
import unittest
import urllib.request

_connect = socket.socket.connect


def _guarded(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError(f"test tried to reach the network: {address}")
    return _connect(self, address)


socket.socket.connect = _guarded


class TestGuard(unittest.TestCase):
    def test_external_connection_refused(self):
        with self.assertRaises(Exception) as cm:
            urllib.request.urlopen("http://example.com/", timeout=2)
        self.assertIn("reach the network", str(cm.exception))

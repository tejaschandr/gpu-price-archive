"""cim.http against real loopback servers (no external network)."""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from cim import http


def serve(handler) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TestBodyCap(unittest.TestCase):
    def test_oversized_response_refused(self):
        class Big(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * (http.MAX_BYTES + 10))

            def log_message(self, *a):
                pass

        srv = serve(Big)
        try:
            with self.assertRaises(http.FetchError) as cm:
                http.get(f"http://127.0.0.1:{srv.server_port}/", retries=1)
            self.assertIn("response over", str(cm.exception))
        finally:
            srv.shutdown()
            srv.server_close()


class TestRedirects(unittest.TestCase):
    def setUp(self):
        self.seen = seen = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self.target = serve(Target)
        loc = f"http://127.0.0.1:{self.target.server_port}/"

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", loc)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.redir = serve(Redirector)
        self.url = f"http://127.0.0.1:{self.redir.server_port}/"

    def tearDown(self):
        for s in (self.target, self.redir):
            s.shutdown()
            s.server_close()

    def test_authorized_request_does_not_follow_redirect(self):
        # urllib used to resend the key to the redirect target.
        with self.assertRaises(http.FetchError) as cm:
            http.get(self.url, headers={"Authorization": "Bearer FAKE"}, retries=1)
        self.assertIn("HTTP 302", str(cm.exception))
        self.assertEqual(self.seen, [])

    def test_unauthenticated_request_follows_redirect(self):
        status, body, _ = http.get(self.url, retries=1)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(self.seen, [None])


if __name__ == "__main__":
    unittest.main()

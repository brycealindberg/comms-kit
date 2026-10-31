"""The API key must never reach a server that fails the certificate check.

The failure that matters: someone on the same network answers as the bridge, and the tool hands
over the key that reads and sends Slack as the owner. These tests use a real local TLS server.

Run: python3 -m unittest test_slack_tls   (from this directory)
"""
import contextlib
import http.server
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slack_bridge as S  # noqa: E402

FAKE_KEY = "fake-api-key-for-this-test"


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
class BridgeCertificate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cert = os.path.join(cls.tmp.name, "cert.pem")
        cls.key = os.path.join(cls.tmp.name, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", cls.key,
                        "-out", cls.cert, "-days", "2", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("X-Api-Key"))
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", 'attachment; filename="note.txt"')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class QuietServer(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a refused handshake is the point of these tests

        self.server = QuietServer(("localhost", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True,
                                                 do_handshake_on_connect=False)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = S.Config(None, Path(self.tmp.name), "https://localhost:%d" % self.server.server_port, FAKE_KEY)
        env = {k: v for k, v in os.environ.items()
               if k not in ("SLACK_BRIDGE_VERIFY_TLS", "SLACK_BRIDGE_INSECURE_TLS", "SSL_CERT_FILE")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_certificate_nobody_trusts_is_refused_and_the_key_stays_home(self):
        with self.assertRaises(SystemExit) as caught:
            S.request_json(self.config, "health")
        self.assertIn("unreachable", str(caught.exception))
        self.assertEqual(self.seen, [])

    def test_a_download_is_refused_the_same_way(self):
        with tempfile.TemporaryDirectory() as out, self.assertRaises(SystemExit):
            S.download_file(self.config, "F1", None, Path(out))
        self.assertEqual(self.seen, [])

    def test_a_certificate_the_machine_trusts_is_accepted(self):
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": self.cert}):
            self.assertEqual(S.request_json(self.config, "health"), {"ok": True})
        self.assertEqual(self.seen, [FAKE_KEY])

    def test_turning_the_check_off_is_explicit_and_says_the_risk(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"SLACK_BRIDGE_INSECURE_TLS": "1"}), contextlib.redirect_stderr(err):
            self.assertEqual(S.request_json(self.config, "health"), {"ok": True})
        self.assertIn("TLS check is OFF", err.getvalue())


if __name__ == "__main__":
    unittest.main()

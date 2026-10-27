"""cardinal-connect against a Cardinal behind a self-signed certificate.

The stub maestro from test_cardinal_plugin served over TLS with the core's
fixture certificate (CN=cardinal.test; it does not name 127.0.0.1), the way
a customer's self-hosted Cardinal behind an ALB with a self-signed
certificate looks. docs/specs/tls-trust.md.
"""

from __future__ import annotations

import json
import ssl
import sys
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cardinal_plugin as base  # noqa: E402
from test_cardinal_plugin import CONNECT, DISCONNECT, STATUS, read_json, run_plugin, settings_env  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from cardinal_core import tls  # noqa: E402

TESTDATA = Path(__file__).resolve().parents[3] / "core" / "tests" / "testdata" / "tls"
CERT, KEY = TESTDATA / "cert.pem", TESTDATA / "key.pem"
OTHER_CERT = TESTDATA / "other-cert.pem"
FINGERPRINT = tls.fingerprint(ssl.PEM_cert_to_DER_cert(CERT.read_text()))
OTHER_FINGERPRINT = tls.fingerprint(ssl.PEM_cert_to_DER_cert(OTHER_CERT.read_text()))


class _TlsThreadingServer(ThreadingHTTPServer):
    def server_bind(self):
        super().server_bind()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(CERT), str(KEY))
        self.socket = ctx.wrap_socket(self.socket, server_side=True)

    def handle_error(self, request, client_address):
        pass  # a client refusing the handshake is expected here


class TlsStubMaestro(base.StubMaestro):
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def start(self):
        original = base.ThreadingHTTPServer
        base.ThreadingHTTPServer = _TlsThreadingServer
        try:
            super().start()
        finally:
            base.ThreadingHTTPServer = original


class TlsConnectTests(unittest.TestCase):
    def setUp(self):
        self.stub = TlsStubMaestro()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = self.home / ".claude" / "cardinal.json"
        self.pin = self.home / ".cardinal" / "tls" / "127.0.0.1.pem"
        self.bundle = self.home / ".cardinal" / "tls" / "ca-bundle.pem"

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def connect(self, *extra):
        return run_plugin(CONNECT, ["--host", self.stub.url(), *extra], self.home, hermetic=True)

    def test_untrusted_certificate_stops_connect_and_explains(self):
        res = self.connect()
        self.assertEqual(res.returncode, tls.EXIT_UNTRUSTED_CERT, res.stdout + res.stderr)
        self.assertIn("does not trust", res.stderr)
        self.assertIn("CN=cardinal.test", res.stderr)
        self.assertIn("does not name 127.0.0.1", res.stderr)
        self.assertIn(f"--trust-cert {FINGERPRINT}", res.stderr)
        self.assertIn(f"--host {self.stub.url()}", res.stderr)
        # Nothing was asked of the server, nothing written.
        self.assertEqual(self.stub.device_code_calls, [])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.pin.exists())

    def test_wrong_fingerprint_is_refused(self):
        res = self.connect("--trust-cert", OTHER_FINGERPRINT)
        self.assertEqual(res.returncode, tls.EXIT_UNTRUSTED_CERT)
        self.assertIn("does not match --trust-cert", res.stderr)
        self.assertFalse(self.pin.exists())
        self.assertEqual(self.stub.device_code_calls, [])

    def test_trust_cert_pins_connects_and_wires_node_ca(self):
        res = self.connect("--trust-cert", FINGERPRINT.lower().replace(":", ""))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("Pinned the certificate for 127.0.0.1", res.stdout)
        self.assertTrue(self.pin.exists())
        self.assertIn("BEGIN CERTIFICATE", self.bundle.read_text())
        # The device flow and both reachability probes went over the pinned TLS.
        self.assertEqual(len(self.stub.device_code_calls), 1)
        self.assertGreaterEqual(self.stub.ingest_probe_count, 1)
        self.assertIn("Ingest endpoint reachable", res.stdout)
        self.assertIn("MCP endpoint reachable", res.stdout)
        env = settings_env(self.home)
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"], str(self.bundle))
        self.assertNotIn("NODE_TLS_REJECT_UNAUTHORIZED", env)
        self.assertTrue(env["CARDINAL_MCP_URL"].startswith("https://127.0.0.1:"))
        state = read_json(self.state)
        self.assertEqual(state["tls"], {"pinned_hosts": ["127.0.0.1"], "node_extra_ca_certs": True,
                                        "runtime_insecure": False})
        # The certificate does not name the host: connect says what that means
        # for Claude Code's own clients.
        self.assertIn("does not name the host", res.stdout)
        self.assertIn("--insecure-runtime-tls", res.stdout)

    def test_pinned_host_connects_silently_next_time(self):
        self.assertEqual(self.connect("--trust-cert", FINGERPRINT).returncode, 0)
        self.stub.token_calls = 0
        res = self.connect("--rotate")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("Pinned", res.stdout)
        self.assertEqual(settings_env(self.home)["NODE_EXTRA_CA_CERTS"], str(self.bundle))

    def test_status_and_disconnect_handle_the_pin(self):
        self.assertEqual(self.connect("--trust-cert", FINGERPRINT, "--insecure-runtime-tls").returncode, 0)
        env = settings_env(self.home)
        self.assertEqual(env["NODE_TLS_REJECT_UNAUTHORIZED"], "0")
        self.assertTrue(read_json(self.state)["tls"]["runtime_insecure"])

        res = run_plugin(STATUS, [], self.home, hermetic=True)
        self.assertIn("pinned certificate for 127.0.0.1", res.stdout)
        self.assertIn("NODE_TLS_REJECT_UNAUTHORIZED=0", res.stdout)

        res = run_plugin(DISCONNECT, [], self.home, hermetic=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        env = settings_env(self.home)
        self.assertNotIn("NODE_EXTRA_CA_CERTS", env)
        self.assertNotIn("NODE_TLS_REJECT_UNAUTHORIZED", env)
        # The pin itself stays (like a known_hosts entry); the user deletes it to forget.
        self.assertTrue(self.pin.exists())
        # The revokes reached the server over the pinned TLS.
        self.assertTrue(self.stub.revoke_calls)

    def test_users_own_node_extra_ca_certs_is_left_alone(self):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"env": {"NODE_EXTRA_CA_CERTS": "/corp/ca.pem"}}))
        res = self.connect("--trust-cert", FINGERPRINT)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(settings_env(self.home)["NODE_EXTRA_CA_CERTS"], "/corp/ca.pem")
        self.assertIn("already sets NODE_EXTRA_CA_CERTS=/corp/ca.pem", res.stdout)
        self.assertFalse(read_json(self.state)["tls"]["node_extra_ca_certs"])
        run_plugin(DISCONNECT, [], self.home, hermetic=True)
        self.assertEqual(settings_env(self.home)["NODE_EXTRA_CA_CERTS"], "/corp/ca.pem")

    def test_insecure_runtime_flag_is_not_owned_when_user_set_it(self):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"env": {"NODE_TLS_REJECT_UNAUTHORIZED": "0"}}))
        self.assertEqual(self.connect("--trust-cert", FINGERPRINT).returncode, 0)
        self.assertFalse(read_json(self.state)["tls"]["runtime_insecure"])
        run_plugin(DISCONNECT, [], self.home, hermetic=True)
        self.assertEqual(settings_env(self.home)["NODE_TLS_REJECT_UNAUTHORIZED"], "0")


if __name__ == "__main__":
    unittest.main()

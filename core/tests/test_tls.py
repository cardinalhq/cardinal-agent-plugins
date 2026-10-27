"""cardinal_core.tls: pinning a self-signed Cardinal certificate.

A local HTTPS server serves the fixture certificate (CN=cardinal.test, SANs
cardinal.test and *.cardinal.internal, valid for a century) on 127.0.0.1: the
system never trusts it and it never names the host, exactly like a
self-hosted Cardinal behind a self-signed certificate.
"""

from __future__ import annotations

import os
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from cardinal_core import tls
from cardinal_core.evidence_promote import no_redirect_opener

TESTDATA = Path(__file__).resolve().parent / "testdata" / "tls"
CERT = TESTDATA / "cert.pem"
KEY = TESTDATA / "key.pem"
OTHER_CERT = TESTDATA / "other-cert.pem"
OTHER_KEY = TESTDATA / "other-key.pem"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET  # noqa: N815

    def log_message(self, format, *args):  # noqa: A002
        pass


class _QuietServer(HTTPServer):
    def handle_error(self, request, client_address):
        pass  # a client that aborts the handshake is the point of these tests


class _TlsServer:
    def __init__(self, cert: Path, key: Path):
        self.server = _QuietServer(("127.0.0.1", 0), _Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/"


def _der(path: Path) -> bytes:
    return ssl.PEM_cert_to_DER_cert(path.read_text())


class TlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp.name})
        self.env.start()
        tls._context_cache.clear()
        tls.install(force=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        tls.install(force=True)

    # -- fingerprints ------------------------------------------------------

    def test_fingerprint_matches_openssl_format_and_normalizes(self):
        fp = tls.fingerprint(_der(CERT))
        self.assertRegex(fp, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertEqual(tls.normalize_fingerprint(fp), fp)
        self.assertEqual(tls.normalize_fingerprint(fp.replace(":", "").lower()), fp)
        self.assertEqual(tls.normalize_fingerprint("sha256 Fingerprint=" + fp), fp)
        self.assertEqual(tls.normalize_fingerprint("SHA256:" + fp.lower()), fp)
        with self.assertRaises(ValueError):
            tls.normalize_fingerprint("abc")

    def test_names_host(self):
        info = tls.CertInfo(pem="", der=b"", fingerprint="", names=["cardinal.test", "*.cardinal.internal"])
        self.assertTrue(info.names_host("cardinal.test"))
        self.assertTrue(info.names_host("CARDINAL.TEST"))
        self.assertTrue(info.names_host("app.cardinal.internal"))
        self.assertFalse(info.names_host("a.b.cardinal.internal"))
        self.assertFalse(info.names_host("cardinal.internal"))
        self.assertFalse(info.names_host("elb.amazonaws.com"))

    # -- probe --------------------------------------------------------------

    def test_probe_describes_an_untrusted_self_signed_certificate(self):
        with _TlsServer(CERT, KEY) as srv:
            p = tls.probe(srv.url)
        self.assertFalse(p.trusted)
        self.assertEqual(p.hostname, "127.0.0.1")
        self.assertEqual(p.error, "")
        self.assertIn("self", p.reason.lower())
        self.assertIsNotNone(p.cert)
        self.assertEqual(p.cert.fingerprint, tls.fingerprint(_der(CERT)))
        self.assertEqual(p.cert.subject, "CN=cardinal.test")
        self.assertTrue(p.cert.self_signed)
        self.assertEqual(p.cert.names, ["cardinal.test", "*.cardinal.internal"])
        self.assertFalse(p.cert.names_host("127.0.0.1"))
        self.assertIn("2126", p.cert.not_after)

    def test_probe_trusts_a_pinned_host(self):
        with _TlsServer(CERT, KEY) as srv:
            tls.save_pin("127.0.0.1", CERT.read_text())
            p = tls.probe(srv.url)
        self.assertTrue(p.trusted)
        self.assertEqual(p.via, "pin")

    def test_probe_reports_a_rotated_certificate_as_untrusted(self):
        tls.save_pin("127.0.0.1", OTHER_CERT.read_text())
        with _TlsServer(CERT, KEY) as srv:
            p = tls.probe(srv.url)
        self.assertFalse(p.trusted)
        self.assertIn("changed since it was pinned", p.reason)
        self.assertEqual(p.cert.fingerprint, tls.fingerprint(_der(CERT)))

    def test_probe_non_tls_failure_has_no_certificate(self):
        p = tls.probe("https://127.0.0.1:1/", timeout=2)
        self.assertFalse(p.trusted)
        self.assertIsNone(p.cert)
        self.assertNotEqual(p.error, "")
        # Nothing to decide: ensure_trusted steps aside and the real request fails itself.
        self.assertIsNone(tls.ensure_trusted("https://127.0.0.1:1/", interactive=False, timeout=2))

    def test_probe_ignores_plain_http(self):
        p = tls.probe("http://127.0.0.1:1/")
        self.assertTrue(p.trusted)
        self.assertEqual(p.via, "plain")

    # -- urllib ----------------------------------------------------------------

    def test_urlopen_fails_without_a_pin_and_succeeds_with_one(self):
        with _TlsServer(CERT, KEY) as srv:
            with self.assertRaises(urllib.error.URLError) as ctx:
                urllib.request.urlopen(srv.url, timeout=5)
            self.assertIn("CERTIFICATE_VERIFY_FAILED", str(ctx.exception))
            tls.save_pin("127.0.0.1", CERT.read_text())
            with urllib.request.urlopen(srv.url, timeout=5) as resp:
                self.assertEqual(resp.read(), b"ok")

    def test_pin_for_another_host_does_not_weaken_this_one(self):
        tls.save_pin("other.example", CERT.read_text())
        with _TlsServer(CERT, KEY) as srv:
            with self.assertRaises(urllib.error.URLError):
                urllib.request.urlopen(srv.url, timeout=5)

    def test_pin_of_a_different_certificate_is_refused(self):
        tls.save_pin("127.0.0.1", OTHER_CERT.read_text())
        with _TlsServer(CERT, KEY) as srv:
            with self.assertRaises(urllib.error.URLError) as ctx:
                urllib.request.urlopen(srv.url, timeout=5)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(ctx.exception))

    def test_build_opener_sites_honor_the_pin(self):
        tls.save_pin("127.0.0.1", CERT.read_text())
        with _TlsServer(CERT, KEY) as srv:
            with no_redirect_opener().open(srv.url, timeout=5) as resp:
                self.assertEqual(resp.read(), b"ok")

    def test_pin_saved_mid_process_takes_effect_at_once(self):
        with _TlsServer(CERT, KEY) as srv:
            with self.assertRaises(urllib.error.URLError):
                urllib.request.urlopen(srv.url, timeout=5)
            tls.save_pin("127.0.0.1", CERT.read_text())
            urllib.request.urlopen(srv.url, timeout=5).close()
            tls.forget_pin("127.0.0.1")
            with self.assertRaises(urllib.error.URLError):
                urllib.request.urlopen(srv.url, timeout=5)

    # -- pin store ---------------------------------------------------------------

    def test_pin_store_layout_and_bundle(self):
        path = tls.save_pin("Cardinal.Example", CERT.read_text())
        self.assertEqual(path, self.home / ".cardinal" / "tls" / "cardinal.example.pem")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o644")
        self.assertEqual(tls.pinned_hosts(), {"cardinal.example": path})
        bundle = tls.bundle_path()
        self.assertTrue(bundle.exists())
        self.assertIn("BEGIN CERTIFICATE", bundle.read_text())
        tls.save_pin("other.example", OTHER_CERT.read_text())
        self.assertEqual(bundle.read_text().count("BEGIN CERTIFICATE"), 2)
        self.assertEqual(tls.pins_for(["https://cardinal.example/api", "https://x.example/"]), ["cardinal.example"])
        self.assertTrue(tls.forget_pin("cardinal.example"))
        self.assertFalse(tls.forget_pin("cardinal.example"))
        self.assertEqual(bundle.read_text().count("BEGIN CERTIFICATE"), 1)
        tls.forget_pin("other.example")
        self.assertFalse(bundle.exists())

    def test_pinned_fingerprint_reads_through_the_header_comment(self):
        tls.save_pin("h.example", CERT.read_text())
        self.assertEqual(tls.pinned_fingerprint("h.example"), tls.fingerprint(_der(CERT)))
        self.assertIsNone(tls.pinned_fingerprint("nope.example"))
        self.assertIsNone(tls.pem_block("no certificate here"))

    def test_save_pin_rejects_junk(self):
        with self.assertRaises(ValueError):
            tls.save_pin("h", "not a certificate")
        with self.assertRaises(ValueError):
            tls.save_pin("", CERT.read_text())

    # -- ensure_trusted ------------------------------------------------------------

    def test_ensure_trusted_is_silent_for_a_trusted_host(self):
        logged = []
        with _TlsServer(CERT, KEY) as srv:
            tls.save_pin("127.0.0.1", CERT.read_text())
            self.assertIsNone(tls.ensure_trusted(srv.url, interactive=False, log=logged.append))
        self.assertEqual(logged, [])

    def test_ensure_trusted_without_a_terminal_explains_and_raises(self):
        with _TlsServer(CERT, KEY) as srv:
            with self.assertRaises(tls.CertificateError) as ctx:
                tls.ensure_trusted(srv.url, interactive=False, command="cardinal-connect --host X")
        msg = str(ctx.exception)
        fp = tls.fingerprint(_der(CERT))
        self.assertIn("does not trust", msg)
        self.assertIn("CN=cardinal.test", msg)
        self.assertIn("(self-signed)", msg)
        self.assertIn("does not name 127.0.0.1", msg)
        self.assertIn(f"cardinal-connect --host X --trust-cert {fp}", msg)
        self.assertEqual(tls.pinned_hosts(), {})

    def test_ensure_trusted_with_matching_fingerprint_pins(self):
        logged = []
        fp = tls.fingerprint(_der(CERT))
        with _TlsServer(CERT, KEY) as srv:
            info = tls.ensure_trusted(srv.url, trust=[fp.lower().replace(":", "")],
                                      interactive=False, log=logged.append)
            self.assertEqual(info.fingerprint, fp)
            self.assertIn("127.0.0.1", tls.pinned_hosts())
            urllib.request.urlopen(srv.url, timeout=5).close()
        self.assertTrue(any("Pinned" in line for line in logged))

    def test_ensure_trusted_with_wrong_fingerprint_refuses(self):
        other = tls.fingerprint(_der(OTHER_CERT))
        with _TlsServer(CERT, KEY) as srv:
            with self.assertRaises(tls.CertificateError) as ctx:
                tls.ensure_trusted(srv.url, trust=[other], interactive=False)
        self.assertIn("does not match --trust-cert", str(ctx.exception))
        self.assertEqual(tls.pinned_hosts(), {})

    def test_ensure_trusted_rejects_a_malformed_fingerprint(self):
        with _TlsServer(CERT, KEY) as srv:
            with self.assertRaises(tls.CertificateError) as ctx:
                tls.ensure_trusted(srv.url, trust=["nope"], interactive=False)
        self.assertIn("--trust-cert", str(ctx.exception))

    def test_ensure_trusted_interactive_yes_and_no(self):
        with _TlsServer(CERT, KEY) as srv:
            with self.assertRaises(tls.CertificateError):
                tls.ensure_trusted(srv.url, interactive=True, log=lambda _: None, ask=lambda _: "n")
            self.assertEqual(tls.pinned_hosts(), {})
            info = tls.ensure_trusted(srv.url, interactive=True, log=lambda _: None, ask=lambda _: "y")
            self.assertIsNotNone(info)
            self.assertIn("127.0.0.1", tls.pinned_hosts())

    def test_ensure_hosts_trusted_dedupes_and_skips_http(self):
        fp = tls.fingerprint(_der(CERT))
        with _TlsServer(CERT, KEY) as srv:
            pinned = tls.ensure_hosts_trusted(
                [srv.url + "v1/metrics", srv.url + "api/orgs/x/mcp", "http://plain.example/", None],
                trust=[fp], interactive=False, log=lambda _: None)
        self.assertEqual([c.fingerprint for c in pinned], [fp])

    def test_runtime_note_mentions_name_mismatch(self):
        lines = tls.runtime_note("claude", ["h.example"], certs_name_hosts=False)
        self.assertTrue(any("NODE_EXTRA_CA_CERTS" in line for line in lines))
        self.assertTrue(any("does not name the host" in line for line in lines))
        self.assertFalse(any("does not name" in line for line in tls.runtime_note("codex", ["h"], True)))


if __name__ == "__main__":
    unittest.main()

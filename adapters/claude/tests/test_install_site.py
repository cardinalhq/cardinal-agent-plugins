#!/usr/bin/env python3
"""Tests for cardinal-install-site against a stub Maestro + a fake `helm`.

The bin is subcommand + JSON-out, so every test runs the real script in a
subprocess with HOME pointed at a temp dir holding a 0600 cardinal-secrets.json,
and asserts on the parsed JSON. A fake `helm` on PATH records its argv so we can
prove the install key reaches helm but never the bin's stdout.
"""

import http.server
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

BIN = Path(__file__).resolve().parent.parent / "bin" / "cardinal-install-site"

PSK = "psk_supersecretinstallkey000000000000"


class StubMaestro:
    """Minimal maestro control-plane for the install flow. State is per-instance
    so tests can flip `phoned_home` etc."""

    def __init__(self):
        self.phoned_home = False
        self.site_name_taken = False
        self.license_decision = "license"  # or "contact_sales" / 403
        self.lakerunner_reason = None      # e.g. "operator_not_phoned_home"
        self.last_create_body = None
        self.last_lakerunner_body = None
        self._server = None
        self._thread = None

    def url(self):
        host, port = self._server.server_address
        return f"http://127.0.0.1:{port}"

    def start(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def _read(self):
                n = int(self.headers.get("content-length") or "0")
                raw = self.rfile.read(n) if n else b""
                return json.loads(raw) if raw else {}

            def do_GET(self):
                p = self.path
                if p == "/api/me":
                    self._send(200, {
                        "user": {"id": "u1", "email": "owner@example.com"},
                        "orgs": [
                            {"orgId": "org-1", "name": "Acme", "slug": "acme", "role": "owner"},
                            {"orgId": "org-2", "name": "Other", "slug": "other", "role": "member"},
                        ],
                    })
                elif p == "/api/orgs/org-1/sites":
                    self._send(200, {"sites": [
                        {"siteId": "site-existing", "name": "existing", "status": "registered",
                         "workloadNamespace": "cardinal", "mode": "operator_managed"},
                    ]})
                elif p.endswith("/bootstrap-step"):
                    if outer.phoned_home:
                        self._send(200, {"step": "done", "site": {}})
                    else:
                        self._send(200, {"step": "install", "site": {},
                                         "apiKey": {"plaintext": PSK, "prefix": "psk_"}})
                elif p.endswith("/bootstrap-status"):
                    self._send(200, {"phonedHome": outer.phoned_home,
                                     "agentVersion": "v1.0.0" if outer.phoned_home else None,
                                     "lastPhonedHomeAt": None})
                elif p.endswith("/workloads"):
                    self._send(200, {"workloads": [{"workloadId": "wl-1", "kind": "lakerunner"}]})
                elif p.endswith("/workloads/maestro/credentials"):
                    self._send(200, {
                        "email": "owner@example.com",
                        "baseUrl": "http://localhost:4200/",
                        "service": "maestro-maestro",
                        "namespace": "cardinal",
                        "port": 4200,
                        "passwordSecret": "maestro-owner-password",
                        "passwordKey": "password",
                    })
                else:
                    self._send(404, {"error": "not_found"})

            def do_POST(self):
                p = self.path
                body = self._read()
                if p == "/api/orgs/org-1/sites":
                    if outer.site_name_taken:
                        self._send(409, {"error": "site_name_taken"})
                        return
                    outer.last_create_body = body
                    self._send(201, {
                        "site": {"siteId": "site-new", "name": body.get("name")},
                        "apiKey": {"plaintext": PSK, "prefix": "psk_"},
                    })
                elif p.endswith("/workloads/license/resolve"):
                    if outer.license_decision == "contact_sales":
                        self._send(200, {"decision": "contact_sales"})
                    elif outer.license_decision == "not_eligible":
                        self._send(403, {"error": "trial_not_eligible"})
                    else:
                        self._send(200, {"decision": "trial",
                                         "license": {"id": "lic-1", "isTrial": True}})
                elif p.endswith("/workloads/lakerunner"):
                    if outer.lakerunner_reason:
                        self._send(409, {"reason": outer.lakerunner_reason})
                        return
                    outer.last_lakerunner_body = body
                    self._send(201, {"workload": {"workloadId": "wl-1"},
                                     "license": {"tier": "trial", "expiresAt": "2027-01-01T00:00:00Z"}})
                elif p.endswith("/bootstrap-key"):
                    self._send(200, {"apiKey": {"plaintext": PSK, "prefix": "psk_"}})
                else:
                    self._send(404, {"error": "not_found"})

            def do_DELETE(self):
                self._send(204, {})

        self._server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()


def run(home: Path, args, extra_path=None, timeout=30):
    env = dict(os.environ)
    env["HOME"] = str(home)
    if extra_path:
        env["PATH"] = f"{extra_path}:{env['PATH']}"
    res = subprocess.run(
        [sys.executable, str(BIN), *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )
    try:
        parsed = json.loads(res.stdout)
    except json.JSONDecodeError:
        parsed = None
    return res, parsed


def write_secrets(home: Path, endpoint: str):
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cardinal-secrets.json").write_text(
        json.dumps({"act_api_key": "act-token-xyz", "act_endpoint": endpoint}) + "\n"
    )


def make_fake_helm(dir_: Path, record: Path, exit_code=0, echo_stderr=False):
    """Fake helm: records argv to `record`. When echo_stderr, it also writes
    argv to stderr (as real helm does on a --set error) so the key-scrub path
    is actually exercised."""
    helm = dir_ / "helm"
    stderr_line = 'printf "%s\\n" "$@" >&2\n' if echo_stderr else ""
    helm.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{record}"\n'
        f"{stderr_line}"
        f"exit {exit_code}\n"
    )
    helm.chmod(0o755)


class InstallSiteTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubMaestro()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        write_secrets(self.home, self.stub.url())

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def test_no_token_fails_with_connect_hint(self):
        empty = TemporaryDirectory()
        try:
            res, out = run(Path(empty.name), ["whoami"])
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual(out["error"], "no_act_token")
        finally:
            empty.cleanup()

    def test_whoami_returns_only_owner_orgs(self):
        res, out = run(self.home, ["whoami"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(out["ok"])
        self.assertEqual([o["orgId"] for o in out["owner_orgs"]], ["org-1"])
        self.assertEqual(out["non_owner_count"], 1)

    def test_create_site_always_sends_operator_managed(self):
        res, out = run(self.home, ["create-site", "--org", "org-1", "--name", "new", "--namespace", "cardinal"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(out["siteId"], "site-new")
        self.assertEqual(self.stub.last_create_body["mode"], "operator_managed")
        self.assertEqual(self.stub.last_create_body["workloadNamespace"], "cardinal")

    def test_create_site_name_collision_is_actionable(self):
        self.stub.site_name_taken = True
        res, out = run(self.home, ["create-site", "--org", "org-1", "--name", "dupe", "--namespace", "cardinal"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "name_taken")

    def test_install_perch_dry_run_redacts_key_and_never_prints_it(self):
        res, out = run(self.home, ["install-perch", "--org", "org-1", "--site", "site-new",
                                   "--namespace", "cardinal", "--dry-run"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(out["dry_run"])
        self.assertIn("REDACTED", out["command"])
        self.assertNotIn(PSK, res.stdout)

    def test_install_perch_runs_helm_with_key_but_never_prints_it(self):
        bindir = self.home / "fakebin"
        bindir.mkdir()
        record = self.home / "helm-args.txt"
        make_fake_helm(bindir, record)

        res, out = run(self.home, ["install-perch", "--org", "org-1", "--site", "site-new",
                                   "--namespace", "cardinal"], extra_path=str(bindir))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(out["ok"])
        # helm received the real key…
        helm_args = record.read_text()
        self.assertIn(f"site.apiKey={PSK}", helm_args)
        # …but it never appeared in the bin's stdout.
        self.assertNotIn(PSK, res.stdout)
        self.assertIn("REDACTED", out["command"])

    def test_install_perch_helm_failure_scrubs_key_from_echoed_stderr(self):
        # Real helm echoes --set values back on a failure. The fake helm does
        # too (echo_stderr=True), so the scrub in cardinal-install-site is
        # actually exercised — without the scrub, PSK would appear in out.stderr.
        bindir = self.home / "fakebin"
        bindir.mkdir()
        record = self.home / "helm-args.txt"
        make_fake_helm(bindir, record, exit_code=1, echo_stderr=True)

        res, out = run(self.home, ["install-perch", "--org", "org-1", "--site", "site-new",
                                   "--namespace", "cardinal"], extra_path=str(bindir))
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "helm_failed")
        # helm really did receive the key (so it really was in its stderr)…
        self.assertIn(f"site.apiKey={PSK}", record.read_text())
        # …but the surfaced stderr — and all stdout — is scrubbed.
        self.assertIn("REDACTED", out["stderr"])
        self.assertNotIn(PSK, out["stderr"])
        self.assertNotIn(PSK, res.stdout)

    def test_install_perch_missing_helm_is_actionable(self):
        # Empty PATH-prefix dir → helm not found. (PATH still has system dirs,
        # but none named `helm` in CI/test envs is not guaranteed, so point PATH
        # at an isolated dir only.)
        bindir = self.home / "emptybin"
        bindir.mkdir()
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["PATH"] = str(bindir)  # only this dir; no helm anywhere
        res = subprocess.run(
            [sys.executable, str(BIN), "install-perch", "--org", "org-1",
             "--site", "site-new", "--namespace", "cardinal"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        out = json.loads(res.stdout)
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "helm_missing")

    def test_install_perch_after_phone_home_reports_key_gone(self):
        self.stub.phoned_home = True
        res, out = run(self.home, ["install-perch", "--org", "org-1", "--site", "site-new",
                                   "--namespace", "cardinal", "--dry-run"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "install_key_unavailable")

    def test_wait_perch_returns_when_phoned_home(self):
        self.stub.phoned_home = True
        res, out = run(self.home, ["wait-perch", "--org", "org-1", "--site", "site-new", "--timeout", "5"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(out["phonedHome"])

    def test_wait_perch_times_out_with_debug_hint(self):
        res, out = run(self.home, ["wait-perch", "--org", "org-1", "--site", "site-new", "--timeout", "1"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "timeout")

    def test_add_lakerunner_sends_managed_poc_and_persists_reservation_key(self):
        res, out = run(self.home, ["add-lakerunner", "--org", "org-1", "--site", "site-new",
                                   "--name", "lr", "--namespace", "cardinal"])
        self.assertEqual(res.returncode, 0, res.stderr)
        body = self.stub.last_lakerunner_body
        self.assertEqual(body["licenseId"], "lic-1")
        self.assertEqual(body["lakerunner"]["profile"], "poc")
        self.assertEqual(body["lakerunner"]["objectStore"]["mode"], "managed")
        self.assertEqual(body["lakerunner"]["lrdb"]["mode"], "managed")
        self.assertEqual(body["lakerunner"]["configdb"]["mode"], "managed")
        # Reservation key persisted + stable across a retry.
        installs = json.loads((self.home / ".claude" / "cardinal-installs.json").read_text())
        first_key = installs["site-new"]["reservation_key"]
        self.assertEqual(body["reservationKey"], first_key)
        run(self.home, ["add-lakerunner", "--org", "org-1", "--site", "site-new",
                        "--name", "lr", "--namespace", "cardinal"])
        self.assertEqual(self.stub.last_lakerunner_body["reservationKey"], first_key)

    def test_add_lakerunner_maps_not_phoned_home(self):
        self.stub.lakerunner_reason = "operator_not_phoned_home"
        res, out = run(self.home, ["add-lakerunner", "--org", "org-1", "--site", "site-new",
                                   "--name", "lr", "--namespace", "cardinal"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "operator_not_phoned_home")

    def test_add_lakerunner_no_license_is_actionable(self):
        self.stub.license_decision = "contact_sales"
        res, out = run(self.home, ["add-lakerunner", "--org", "org-1", "--site", "site-new",
                                   "--name", "lr", "--namespace", "cardinal"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "contact_sales")

    def test_add_lakerunner_not_eligible_is_actionable(self):
        self.stub.license_decision = "not_eligible"  # → 403 from license/resolve
        res, out = run(self.home, ["add-lakerunner", "--org", "org-1", "--site", "site-new",
                                   "--name", "lr", "--namespace", "cardinal"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "trial_not_eligible")

    def test_add_lakerunner_maps_not_operator_managed(self):
        # The invariant this skill is built around: create-site always sends
        # operator_managed, so this 409 should never happen — but if it does,
        # it must be an actionable message, not a raw 409.
        self.stub.lakerunner_reason = "site_not_operator_managed"
        res, out = run(self.home, ["add-lakerunner", "--org", "org-1", "--site", "site-new",
                                   "--name", "lr", "--namespace", "cardinal"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "not_operator_managed")

    def test_reservation_file_unreadable_does_not_clobber_other_sites(self):
        # An unreadable installs file must fail loudly, NOT rewrite the file with
        # only the current site (which would wipe every other site's key).
        installs = self.home / ".claude" / "cardinal-installs.json"
        installs.parent.mkdir(parents=True, exist_ok=True)
        # A directory at that path makes read_text raise OSError deterministically
        # (no permission-bit flakiness).
        installs.mkdir()
        res, out = run(self.home, ["add-lakerunner", "--org", "org-1", "--site", "site-new",
                                   "--name", "lr", "--namespace", "cardinal"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out["error"], "installs_unreadable")
        # The path was not overwritten — still the directory we made.
        self.assertTrue(installs.is_dir())
        # And the lakerunner install never fired.
        self.assertIsNone(self.stub.last_lakerunner_body)

    def test_connect_info_renders_port_forward_and_password_commands(self):
        res, out = run(self.home, ["connect-info", "--org", "org-1", "--site", "site-new"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(out["login_email"], "owner@example.com")
        self.assertIn("port-forward -n cardinal svc/maestro-maestro 4200:4200", out["port_forward"])
        self.assertIn("get secret maestro-owner-password", out["read_password"])


if __name__ == "__main__":
    unittest.main()

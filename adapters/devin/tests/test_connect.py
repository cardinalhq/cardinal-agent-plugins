from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support
from support import ADAPTER_DIR, FakeCardinal

from cardinal_core import otlp
from cardinal_core.paths import AgentPaths
from cardinal_devin import cli
from cardinal_devin.connect import run_connect, run_status

LAUNCHER = ADAPTER_DIR / "bin" / "cardinal-devin"


class ConnectTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cardinal-devin-connect-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.paths = AgentPaths(home=Path(tmp) / "home")
        self.server = FakeCardinal()
        self.addCleanup(self.server.close)
        self.lines: list = []

    def connect(self, **kw) -> int:
        return run_connect(self.paths, host=self.server.url, out=self.lines.append, probe_sleeps=(), **kw)

    def test_connect_writes_state_and_secret(self) -> None:
        self.assertEqual(self.connect(), 0)
        state = self.paths.read_state()
        self.assertEqual(state["ingest_endpoint"], self.server.url)
        self.assertEqual(state["org_slug"], "acme")
        self.assertEqual(state["user_email"], "admin@example.com")
        self.assertEqual(state["mode"], "telemetry-only")
        self.assertEqual(state["deployment_environment"], "customer")
        self.assertNotIn("ingest-secret-key-123", self.paths.state_path.read_text())
        self.assertEqual(self.paths.read_secrets()["ingest_api_key"], "ingest-secret-key-123")
        self.assertEqual(stat.S_IMODE(self.paths.secrets_path.stat().st_mode), 0o600)
        self.assertFalse(self.paths.pending_path.exists())
        conn = otlp.connection_from_paths(self.paths)
        self.assertEqual((conn.endpoint, conn.api_key), (self.server.url, "ingest-secret-key-123"))
        code_req = next(r for r in self.server.requests if r.path == "/api/auth/device/code")
        self.assertEqual(code_req.json()["client"], "cardinal-devin-poller")
        self.assertEqual(code_req.json()["scopes"], ["ingest:write"])
        self.assertTrue(any(r.path == "/v1/metrics" for r in self.server.requests))

    def test_already_connected_needs_rotate(self) -> None:
        self.assertEqual(self.connect(), 0)
        self.server.requests.clear()
        self.assertEqual(self.connect(), 2)
        self.assertEqual(self.server.requests, [])
        self.assertIn("--rotate", self.lines[-1])
        self.assertEqual(self.connect(rotate=True), 0)

    def test_dry_run_writes_nothing(self) -> None:
        self.assertEqual(self.connect(dry_run=True), 0)
        self.assertFalse(self.paths.home.exists())
        self.assertTrue(any("would_write" in line for line in self.lines))

    def test_denied_consent(self) -> None:
        self.server.token_error = "access_denied"
        self.assertEqual(self.connect(), 1)
        self.assertFalse(self.paths.state_path.exists())
        self.assertFalse(self.paths.pending_path.exists())
        self.assertIn("denied", self.lines[-1])

    def test_status(self) -> None:
        kwargs = dict(devin_key_source="$DEVIN_API_KEY", devin_org_id=None,
                      devin_base_url="https://api.devin.ai", api_version="auto", github_token_set=False)
        self.assertEqual(run_status(self.paths, out=self.lines.append, **kwargs), 1)
        self.assertIn("Not connected", "\n".join(self.lines))
        self.connect()
        self.lines.clear()
        self.assertEqual(run_status(self.paths, out=self.lines.append, **kwargs), 0)
        text = "\n".join(self.lines)
        self.assertIn("connected to", text)
        self.assertIn("API v1", text)
        self.assertIn("no GITHUB_TOKEN", text)
        self.assertIn("0 tracked", text)
        self.assertNotIn("ingest-secret-key-123", text)


class CliTests(unittest.TestCase):
    def test_decision_schema_command(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["decision-schema"]), 0)
        self.assertIn("decisions", json.loads(buf.getvalue())["properties"])

    def test_poll_requires_connection_and_key(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {k: v for k, v in os.environ.items() if k not in ("DEVIN_API_KEY", "DEVIN_ORG_ID", "GITHUB_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(cli.main(["poll", "--once", "--state-dir", tmp]), 1)
            self.assertIn("Not connected", err.getvalue())
            self.assertEqual(cli.main(["poll", "--once", "--dry-run", "--state-dir", tmp]), 2)
            self.assertIn("no Devin key", err.getvalue())

    def test_launcher_help_and_schema(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        for args in (["--help"], ["decision-schema"]):
            proc = subprocess.run([sys.executable, str(LAUNCHER), *args], capture_output=True, text=True,
                                  env=env, timeout=60, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("draft-07", proc.stdout)


if __name__ == "__main__":
    unittest.main()

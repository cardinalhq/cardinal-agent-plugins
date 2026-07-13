"""Parity + behavioral tests for the migrated Codex adapter.

Golden parity: runs the migrated hook against the SAME fixtures used by
capture_goldens.py (which ran the pre-migration shipped hook) and asserts
normalized OTLP/stdout output is byte-equal to tests/goldens/.

Behavioral tests are ported from the source repo's test_cardinal_plugin.py.

Run from adapters/codex/:
    python3 -m unittest tests.test_parity -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent   # adapters/codex
REPO_ROOT = ROOT.parent.parent
HOOK = ROOT / "hooks" / "cardinal-codex-telemetry.py"
SCRIPTS = ROOT / "scripts"
CONNECT = SCRIPTS / "cardinal-connect"
STATUS = SCRIPTS / "cardinal-status"
DISCONNECT = SCRIPTS / "cardinal-disconnect"
GOLDENS = Path(__file__).resolve().parent / "goldens"


def setUpModule() -> None:  # noqa: N802
    """Vendor cardinal_core next to the hooks (build output, gitignored)."""
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "codex"],
        check=True, capture_output=True,
    )


def golden(name: str):
    return json.loads((GOLDENS / f"{name}.json").read_text())


class StubCardinal:
    """HTTP stub for the full connect/status/disconnect + ingest surface.
    Ported verbatim from the source repo's test_cardinal_plugin.py."""

    def __init__(self):
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.token_pending_count = 1
        self.token_calls = 0
        self.last_scopes: list[str] = []
        self.mcp_status = 405
        self.ingest_status = 400
        self.revoke_status = 204
        self.revoke_calls: list[tuple[str, str | None]] = []
        self.log_batches: list[dict] = []
        self.limits_verdict: dict = {
            "decision": "allow",
            "band": 0,
            "ttl_seconds": 120,
            "evaluations": [
                {
                    "scope": "session",
                    "spent_usd": 1.25,
                    "limit_usd": 100.0,
                    "fraction": 0.0125,
                    "set_by": {"self": True},
                }
            ],
        }

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send_json(self, status: int, body: dict | None):
                payload = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def do_POST(self):
                length = int(self.headers.get("content-length") or "0")
                raw = self.rfile.read(length)
                if self.path == "/api/auth/device/code":
                    try:
                        body = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        body = {}
                    outer.last_scopes = list(body.get("scopes") or [])
                    self._send_json(201, {
                        "device_code": "dc-xyz",
                        "user_code": "ABCD-EFGH",
                        "verification_uri": f"{outer.url()}/connect?code=ABCD-EFGH",
                        "expires_in": 30,
                        "interval": 1,
                    })
                    return
                if self.path == "/api/auth/device/token":
                    outer.token_calls += 1
                    if outer.token_calls <= outer.token_pending_count:
                        self._send_json(400, {"error": "authorization_pending"})
                        return
                    self._send_json(200, {
                        "org": {
                            "id": "org-uuid-1",
                            "slug": "test-org",
                            "name": "Test Org",
                        },
                        "user": {
                            "id": "user-uuid-1",
                            "email": "rj@example.com",
                        },
                        "ingest": {
                            "endpoint": outer.url(),
                            "api_key": "INGESTPLAINTEXT" + "y" * 48,
                            "api_header": "x-cardinalhq-api-key",
                            "key_id": "ingest-key-uuid-1",
                        },
                        "mcp": {
                            "url": f"{outer.url()}/api/orgs/org-uuid-1/mcp",
                            "api_key": "MCPPLAINTEXT" + "x" * 52,
                            "key_id": "mcp-key-uuid-1",
                            "key_prefix": "MCPPLAIN",
                            "created_at": "2026-06-05T00:00:00Z",
                        },
                        "limits": {
                            "status_url": f"{outer.url()}/api/agent-limits/status",
                            "enabled": True,
                        },
                    })
                    return
                if self.path == "/v1/metrics":
                    if self.headers.get("x-cardinalhq-api-key", "").startswith("INGESTPLAINTEXT"):
                        self.send_response(outer.ingest_status)
                    else:
                        self.send_response(401)
                    self.end_headers()
                    return
                if self.path == "/v1/logs":
                    try:
                        body = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        body = {}
                    outer.log_batches.append(body)
                    if self.headers.get("x-cardinalhq-api-key", "").startswith("INGESTPLAINTEXT"):
                        self.send_response(200)
                    else:
                        self.send_response(401)
                    self.end_headers()
                    return
                if self.path.startswith("/api/maestro-keys/") and self.path.endswith("/revoke"):
                    key_id = self.path.split("/")[-2]
                    outer.revoke_calls.append((key_id, self.headers.get("X-CardinalHQ-API-Key")))
                    self.send_response(outer.revoke_status)
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def do_GET(self):
                if self.path.startswith("/api/orgs/") and self.path.endswith("/mcp"):
                    self.send_response(outer.mcp_status)
                    self.end_headers()
                    return
                if self.path.startswith("/api/agent-limits/status"):
                    self._send_json(200, outer.limits_verdict)
                    return
                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def run_script(path: Path, args: list[str], home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(path), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_hook(args: list[str], home: Path, stdin: dict,
             env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    assert args[0] == "--event"
    return fixtures.run_hook(HOOK, args[1], home, stdin, env_extra=env_extra)


def read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text()) if path.exists() else {}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def event_names(batch: dict) -> list[str]:
    records = batch["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    return [
        next(a["value"]["stringValue"] for a in r["attributes"] if a["key"] == "event_name")
        for r in records
    ]


def records_named(batch: dict, name: str) -> list[dict]:
    records = batch["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    out = []
    for r in records:
        attrs = {a["key"]: next(iter(a["value"].values())) for a in r["attributes"]}
        if attrs.get("event_name") == name:
            out.append(attrs)
    return out


def raw_records_named(batch: dict, name: str) -> list[dict]:
    """Full OTLP logRecords (not flattened attrs) — for privacy scans
    that must inspect the exact emitted bytes."""
    records = batch["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    out = []
    for r in records:
        attrs = {a["key"]: next(iter(a["value"].values())) for a in r["attributes"]}
        if attrs.get("event_name") == name:
            out.append(r)
    return out


class GoldenSmokeTests(unittest.TestCase):
    def test_goldens_exist(self) -> None:
        names = {p.stem for p in GOLDENS.glob("*.json")}
        self.assertEqual(len(names), 10, names)


class GoldenParityTests(unittest.TestCase):
    """The migrated hook must emit byte-equal normalized output to the
    goldens captured from the pre-migration shipped hook."""

    def assert_matches(self, results: dict) -> None:
        for name, data in sorted(results.items()):
            with self.subTest(golden=name):
                self.assertEqual(data, golden(name))

    def test_telemetry_scenarios(self) -> None:
        # stop_first, stop_second, user_prompt_submit, subagent_stop
        self.assert_matches(fixtures.scenario_telemetry(HOOK))

    def test_session_start_scenarios(self) -> None:
        self.assert_matches(fixtures.scenario_session_start(HOOK))

    def test_gate_scenarios(self) -> None:
        self.assert_matches(fixtures.scenario_gate(HOOK))


if __name__ == "__main__":
    unittest.main()

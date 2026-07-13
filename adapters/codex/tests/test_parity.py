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


class ManifestTests(unittest.TestCase):
    def test_mcp_json_has_disabled_template(self):
        data = json.loads((ROOT / ".mcp.json").read_text())
        entry = data["mcpServers"]["cardinal"]
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "${CARDINAL_MCP_URL}")
        self.assertEqual(entry["headers"]["X-CardinalHQ-API-Key"], "${CARDINAL_MCP_API_KEY}")
        self.assertFalse(entry["enabled"])

    def test_plugin_version_is_loaded_at_runtime(self):
        # Both cardinal-connect and the telemetry hook resolve
        # PLUGIN_VERSION from plugin.json at import time — no hardcoded
        # constants.
        import importlib.util
        from importlib.machinery import SourceFileLoader
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())

        def _load(name, path):
            loader = SourceFileLoader(name, str(path))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod

        connect = _load("cardinal_connect_module", CONNECT)
        self.assertEqual(connect.PLUGIN_VERSION, manifest["version"])
        self.assertNotEqual(connect.PLUGIN_VERSION, "unknown")

        version_helper = _load(
            "cardinal_codex_plugin_version",
            ROOT / "hooks" / "_plugin_version.py",
        )
        self.assertEqual(version_helper.plugin_version(), manifest["version"])


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubCardinal()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.config = self.home / ".codex" / "config.toml"
        self.hooks = self.home / ".codex" / "hooks.json"
        self.state = self.home / ".codex" / "cardinal.json"
        self.secrets = self.home / ".codex" / "cardinal-secrets.json"

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def test_happy_path_writes_managed_config_and_state(self):
        result = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.stub.last_scopes, ["ingest:write", "mcp:invoke"])

        config = read_toml(self.config)
        entry = config["mcp_servers"]["cardinal"]
        self.assertTrue(entry["url"].endswith("/api/orgs/org-uuid-1/mcp"))
        self.assertTrue(entry["http_headers"]["X-CardinalHQ-API-Key"].startswith("MCPPLAINTEXT"))
        raw_config = self.config.read_text()
        self.assertIn("# BEGIN cardinal-codex-plugin managed MCP server", raw_config)

        state = read_json(self.state)
        self.assertEqual(state["mode"], "telemetry-and-mcp")
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["org_slug"], "test-org")
        self.assertEqual(state["ingest_key_id"], "ingest-key-uuid-1")
        self.assertEqual(state["mcp_key_id"], "mcp-key-uuid-1")
        self.assertNotIn("MCPPLAINTEXT", self.state.read_text())
        self.assertNotIn("INGESTPLAINTEXT", self.state.read_text())

        secrets = read_json(self.secrets)
        self.assertTrue(secrets["ingest_api_key"].startswith("INGESTPLAINTEXT"))
        self.assertTrue(secrets["mcp_api_key"].startswith("MCPPLAINTEXT"))

        hooks = read_json(self.hooks)
        self.assertIn("SessionStart", hooks["hooks"])
        self.assertIn("UserPromptSubmit", hooks["hooks"])
        self.assertIn("Stop", hooks["hooks"])
        self.assertIn("SubagentStop", hooks["hooks"])
        self.assertIn("cardinal-codex-plugin", json.dumps(hooks))

    def test_already_connected_guard_without_rotate(self):
        first = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(first.returncode, 0)
        second = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(second.returncode, 2)
        self.assertIn("already connected", second.stderr.lower())

    def test_rotate_replaces_existing_managed_block(self):
        first = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(first.returncode, 0)
        self.stub.token_calls = 0
        second = run_script(CONNECT, ["--host", self.stub.url(), "--rotate"], self.home)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual(self.config.read_text().count("[mcp_servers.cardinal]"), 1)

    def test_dry_run_writes_nothing(self):
        result = run_script(CONNECT, ["--host", self.stub.url(), "--dry-run"], self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would_write", result.stdout)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.state.exists())

    def test_unmanaged_cardinal_table_is_not_overwritten(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text('[mcp_servers.cardinal]\nurl = "https://example.invalid/mcp"\n')
        result = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unmanaged", (result.stderr + result.stdout).lower())
        self.assertFalse(self.state.exists())

    def inject_drift_into_managed_block(self):
        """Simulate the issue-#10 drift observed on a live install: Codex
        rewrites config.toml and lands its own sections INSIDE the plugin's
        BEGIN/END marker span."""
        foreign = (
            "[desktop]\n"
            'followUpQueueMode = "queue"\n'
            "\n"
            "[hooks.state]\n"
            'trusted_hash = "sha256:abc"\n'
        )
        end_marker = "# END cardinal-codex-plugin managed MCP server"
        text = self.config.read_text()
        self.assertIn(end_marker, text)
        self.config.write_text(text.replace(end_marker, foreign + end_marker))

    def test_rotate_preserves_foreign_sections_inside_managed_block(self):
        first = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.inject_drift_into_managed_block()

        self.stub.token_calls = 0
        result = run_script(CONNECT, ["--host", self.stub.url(), "--rotate"], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        text = self.config.read_text()
        self.assertEqual(text.count("[mcp_servers.cardinal]"), 1)
        config = read_toml(self.config)
        self.assertIn("cardinal", config["mcp_servers"])
        self.assertEqual(config["desktop"]["followUpQueueMode"], "queue")
        self.assertEqual(config["hooks"]["state"]["trusted_hash"], "sha256:abc")

    def test_disconnect_preserves_foreign_sections_inside_managed_block(self):
        first = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.inject_drift_into_managed_block()

        result = run_script(DISCONNECT, [], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        text = self.config.read_text()
        self.assertNotIn("[mcp_servers.cardinal]", text)
        self.assertNotIn("cardinal-codex-plugin managed MCP server", text)
        config = read_toml(self.config)
        self.assertEqual(config["desktop"]["followUpQueueMode"], "queue")
        self.assertEqual(config["hooks"]["state"]["trusted_hash"], "sha256:abc")

    def test_mcp_reachability_failure_aborts(self):
        self.stub.mcp_status = 401
        result = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mcp reachability failed", (result.stderr + result.stdout).lower())
        self.assertFalse(self.state.exists())


class StatusAndDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubCardinal()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        connected = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(connected.returncode, 0, connected.stderr + connected.stdout)
        self.config = self.home / ".codex" / "config.toml"
        self.hooks = self.home / ".codex" / "hooks.json"
        self.state = self.home / ".codex" / "cardinal.json"
        self.secrets = self.home / ".codex" / "cardinal-secrets.json"

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def test_status_reports_reachable(self):
        result = run_script(STATUS, [], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Telemetry:", result.stdout)
        self.assertIn("OK ingest reachable", result.stdout)
        self.assertIn("MCP endpoint:", result.stdout)
        self.assertIn("OK MCP reachable", result.stdout)

    def test_disconnect_revokes_key_and_removes_managed_block(self):
        result = run_script(DISCONNECT, [], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.stub.revoke_calls, [
            ("ingest-key-uuid-1", "INGESTPLAINTEXT" + "y" * 48),
            ("mcp-key-uuid-1", "MCPPLAINTEXT" + "x" * 52),
        ])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.secrets.exists())
        self.assertNotIn("cardinal-codex-plugin managed", self.config.read_text())
        config = read_toml(self.config)
        self.assertNotIn("cardinal", config.get("mcp_servers", {}))
        self.assertNotIn("cardinal-codex-plugin", self.hooks.read_text())


class TelemetryHookTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubCardinal()
        self.stub.start()
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        connected = run_script(CONNECT, ["--host", self.stub.url()], self.home)
        self.assertEqual(connected.returncode, 0, connected.stderr + connected.stdout)

    def tearDown(self):
        self.stub.stop()
        self.tmp.cleanup()

    def test_stop_hook_posts_codex_events_from_transcript(self):
        session_id = "sess-test-1"
        transcript = self.home / "session.jsonl"
        rows = [
            {
                "timestamp": "2026-07-01T00:00:00.000Z",
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(self.home)},
            },
            {
                "timestamp": "2026-07-01T00:00:01.000Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.5", "cwd": str(self.home)},
            },
            {
                "timestamp": "2026-07-01T00:00:02.000Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "run tests"},
            },
            {
                "timestamp": "2026-07-01T00:00:03.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "go test ./..."}),
                    "call_id": "call-1",
                },
            },
            {
                "timestamp": "2026-07-01T00:00:04.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "Process exited with code 0\n",
                },
            },
            {
                "timestamp": "2026-07-01T00:00:05.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 12,
                            "cached_input_tokens": 7,
                            "output_tokens": 5,
                            "total_tokens": 17,
                        }
                    },
                    "rate_limits": {
                        "limit_id": "codex",
                        "plan_type": "team",
                        "primary": {"used_percent": 3, "resets_at": 1780000000},
                        "secondary": {"used_percent": 8, "resets_at": 1780001000},
                    },
                },
            },
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

        result = run_hook(
            ["--event", "Stop"],
            self.home,
            {"session_id": session_id, "transcript_path": str(transcript)},
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(self.stub.log_batches), 1)

        records = self.stub.log_batches[0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        names = [
            next(a["value"]["stringValue"] for a in r["attributes"] if a["key"] == "event_name")
            for r in records
        ]
        self.assertIn("cardinal.turn_tool", names)
        self.assertIn("tool_result", names)
        self.assertIn("api_request", names)
        self.assertIn("cardinal.turn_usage", names)
        self.assertIn("cardinal.plan_usage", names)

        # api_request must carry cost_usd — codex has no native cost
        # emitter, so the plugin computes it from usage + a model price
        # table.
        api_req = next(
            r for r in records
            if next(a["value"]["stringValue"] for a in r["attributes"] if a["key"] == "event_name") == "api_request"
        )
        cost_kv = next(a for a in api_req["attributes"] if a["key"] == "cost_usd")
        # gpt-5.5 falls back to gpt-5 pricing via longest-prefix match:
        #   (12-7) input * $1.25/M + 7 cached * $0.125/M + 5 output * $10/M
        #   → 0.000057 rounded to 6 places.
        self.assertAlmostEqual(cost_kv["value"]["doubleValue"], 0.000057, places=6)

        resource_attrs = {
            a["key"]: next(iter(a["value"].values()))
            for a in self.stub.log_batches[0]["resourceLogs"][0]["resource"]["attributes"]
        }
        self.assertEqual(resource_attrs["service.name"], "codex")
        self.assertEqual(resource_attrs["agent.runtime"], "codex")


if __name__ == "__main__":
    unittest.main()

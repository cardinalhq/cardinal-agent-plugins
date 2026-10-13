"""What cardinal-connect writes into ~/.gemini/settings.json must load in
Gemini CLI, and cardinal-disconnect must remove exactly that.

Plugin 0.22.1 and earlier wrote `telemetry: {target: "otlp", otlpHeaders:
{<header>: <ingest key>}, cardinalManaged: true, ...}`. Gemini CLI 0.63.0
(and every version since 0.26.0) rejects target "otlp" with a
FatalConfigError and refuses to start; its settings schema rejects unknown
keys in `telemetry` and in each `mcpServers` entry. These tests pin the
0.63.0 schema shape, the repair of the old shape, ownership kept in
cardinal.json, and the ingest key never landing in settings.json.

GeminiBinaryParseTest runs the real `gemini` (skipped when it is not on
PATH, or with CARDINAL_SKIP_GEMINI_BINARY=1) against a temporary
GEMINI_CLI_HOME: it never reads or writes the real ~/.gemini, and
`--list-extensions` exits right after configuration loads, before any
model call (outbound HTTP is sent to a dead local proxy).
No other network: the device flow and probes are stubbed.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ADAPTER = Path(__file__).resolve().parent.parent
MONOREPO_ROOT = ADAPTER.parents[1]
CONNECT = ADAPTER / "scripts" / "cardinal-connect"
DISCONNECT = ADAPTER / "scripts" / "cardinal-disconnect"
STATUS = ADAPTER / "scripts" / "cardinal-status"

# gemini-cli 0.63.0 settings schema (bundle chunk settingsSchema):
# TelemetrySettings and MCPServerConfig are additionalProperties: false.
TELEMETRY_KEYS_063 = {"enabled", "target", "otlpEndpoint", "otlpProtocol", "traces",
                      "logPrompts", "outfile", "useCollector", "useCliAuth"}
TELEMETRY_TARGETS_063 = {"local", "gcp"}
OTLP_PROTOCOLS_063 = {"grpc", "http"}
MCP_SERVER_KEYS_063 = {"command", "args", "env", "cwd", "url", "httpUrl", "headers", "tcp",
                       "type", "timeout", "trust", "description", "includeTools", "excludeTools",
                       "extension", "oauth", "authProviderType", "targetAudience",
                       "targetServiceAccount"}

INGEST_KEY = "INGESTPLAINTEXT" + "q" * 49
MCP_KEY = "MCPPLAINTEXT" + "x" * 52
INGEST = {"endpoint": "https://ingest.example", "api_key": INGEST_KEY,
          "api_header": "x-cardinalhq-api-key", "key_id": "ing-1"}
MCP = {"url": "https://cardinal.example/api/orgs/org-1/mcp", "api_key": MCP_KEY,
       "key_id": "mcp-key-1", "key_prefix": "MCPPLAIN"}

LEGACY_TELEMETRY = {
    "enabled": True,
    "target": "otlp",
    "otlpEndpoint": "https://ingest.example/v1/logs",
    "otlpHeaders": {"x-cardinalhq-api-key": INGEST_KEY},
    "logPrompts": False,
    "cardinalManaged": True,
}


def schema_problems(settings: dict) -> list[str]:
    """Where `settings` fails the 0.63.0 shape for the blocks Gemini CLI
    validates strictly (or fatally)."""
    problems = []
    tel = settings.get("telemetry")
    if isinstance(tel, dict):
        extra = set(tel) - TELEMETRY_KEYS_063
        if extra:
            problems.append(f"telemetry has unknown keys {sorted(extra)}")
        if "target" in tel and tel["target"] not in TELEMETRY_TARGETS_063:
            problems.append(f"telemetry.target {tel['target']!r} is fatal")
        if "otlpProtocol" in tel and tel["otlpProtocol"] not in OTLP_PROTOCOLS_063:
            problems.append(f"telemetry.otlpProtocol {tel['otlpProtocol']!r} is fatal")
    elif tel is not None and not isinstance(tel, bool):
        problems.append("telemetry is neither a boolean nor an object")
    for name, entry in (settings.get("mcpServers") or {}).items():
        extra = set(entry) - MCP_SERVER_KEYS_063
        if extra:
            problems.append(f"mcpServers.{name} has unknown keys {sorted(extra)}")
    return problems


def _ensure_vendored() -> None:
    if (ADAPTER / "hooks" / "cardinal_core" / "__init__.py").exists():
        return
    subprocess.run(
        [sys.executable, str(MONOREPO_ROOT / "build" / "vendor.py"), "gemini"],
        check=True, capture_output=True,
    )


class _Home(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_vendored()

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.gemini = self.home / ".gemini"
        self.gemini.mkdir()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)   # cardinal-connect resolves ~/.gemini at import
        name = f"cardinal_connect_schema_{id(self)}"
        loader = importlib.machinery.SourceFileLoader(name, str(CONNECT))
        spec = importlib.util.spec_from_loader(name, loader)
        self.connect = importlib.util.module_from_spec(spec)
        loader.exec_module(self.connect)
        # The installed CLI's version decides the endpoint path; pin "current".
        patcher = mock.patch.object(self.connect, "gemini_cli_version", return_value=(0, 63, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self.tmp.cleanup()

    # --- paths / helpers ---------------------------------------------------

    @property
    def settings_path(self) -> Path:
        return self.gemini / "settings.json"

    @property
    def env_path(self) -> Path:
        return self.gemini / ".env"

    @property
    def state_path(self) -> Path:
        return self.gemini / "cardinal.json"

    def settings(self) -> dict:
        return json.loads(self.settings_path.read_text())

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def write_settings(self, data: dict) -> None:
        self.settings_path.write_text(json.dumps(data, indent=2))

    def write_connected(self, endpoints: bool = True, **extra) -> None:
        """A connection's state and secrets, without key ids (disconnect then
        makes no revoke calls); without endpoints, status probes nothing."""
        state = {"schema_version": 1, "host": "https://cardinal.example", "mode": "telemetry-and-mcp",
                 **({"ingest_endpoint": INGEST["endpoint"], "mcp_url": MCP["url"]} if endpoints else {}),
                 **extra}
        self.state_path.write_text(json.dumps(state))
        (self.gemini / "cardinal-secrets.json").write_text(json.dumps({
            "ingest_api_key": INGEST_KEY, "ingest_api_header": "x-cardinalhq-api-key",
            "mcp_api_key": MCP_KEY}))

    def connect_run(self, native=None, no_extension=False, ingest=True) -> str:
        args = argparse.Namespace(
            host="https://cardinal.example", telemetry_only=False, no_extension=no_extension,
            deployment_env=None, dry_run=False, native_telemetry=native,
        )
        bundle = {"org": {"id": "org-1", "slug": "acme"}, "user": {"email": "dev@example.com"},
                  "mcp": dict(MCP), "ingest": dict(INGEST) if ingest else None, "limits": None}
        df = self.connect.deviceflow
        out = io.StringIO()
        with mock.patch.object(df, "poll_device_token", return_value=bundle), \
                mock.patch.object(df, "verify_mcp_reachable", return_value=(True, "HTTP 405")), \
                mock.patch.object(df, "verify_ingest_reachable", return_value=(True, "HTTP 400")), \
                contextlib.redirect_stdout(out):
            rc = self.connect.continue_after_grant(args, {"device_code": "dc", "interval": 1})
        self.assertEqual(rc, 0, out.getvalue())
        return out.getvalue()

    def drop_key_ids(self) -> None:
        """No revoke calls from cardinal-disconnect."""
        state = self.state()
        for key in ("ingest_key_id", "mcp_key_id", "act_key_id"):
            state.pop(key, None)
        self.state_path.write_text(json.dumps(state))

    def run_script(self, script: Path, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(script), *argv], capture_output=True, text=True, timeout=60,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, cwd=str(self.home),
        )

    def assert_valid_and_keyless(self) -> None:
        if not self.settings_path.exists():
            return
        settings = self.settings()
        self.assertEqual(schema_problems(settings), [], settings)
        text = self.settings_path.read_text()
        self.assertNotIn(INGEST_KEY, text)
        self.assertNotIn("otlpHeaders", text)
        for block in ("telemetry", "mcpServers"):
            self.assertNotIn("cardinalManaged", json.dumps(settings.get(block)), block)


class ConnectWritesValidSettingsTest(_Home):
    def test_default_connect_writes_no_telemetry_block_and_no_env(self):
        self.write_settings({"general": {"vimMode": True}})
        out = self.connect_run()
        self.assertEqual(self.settings(), {"general": {"vimMode": True}})
        self.assertFalse(self.env_path.exists())
        self.assertEqual(self.state()["native_telemetry"], {"requested": False, "enabled": False})
        self.assertIn("own OTLP exporter: off", out)

    def test_native_telemetry_block_is_schema_valid_and_keyless(self):
        out = self.connect_run(native=True)
        self.assert_valid_and_keyless()
        self.assertEqual(self.settings()["telemetry"], {
            "enabled": True, "target": "local", "otlpEndpoint": "https://ingest.example",
            "otlpProtocol": "http", "logPrompts": False,
        })
        # The header reaches Gemini through ~/.gemini/.env, 0600.
        env = self.env_path.read_text()
        self.assertIn(f"OTEL_EXPORTER_OTLP_HEADERS='x-cardinalhq-api-key={INGEST_KEY}'", env)
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)
        state = self.state()
        self.assertEqual(state["native_telemetry"], {"requested": True, "enabled": True})
        self.assertEqual(state["settings_owned"]["telemetry"], self.settings()["telemetry"])
        self.assertNotIn(INGEST_KEY, self.state_path.read_text())
        self.assertIn("own OTLP exporter: on", out)

    def test_cli_before_0_34_is_pointed_at_v1_logs(self):
        # Before 0.34.0 Gemini CLI posts every signal to the endpoint verbatim.
        with mock.patch.object(self.connect, "gemini_cli_version", return_value=(0, 33, 0)):
            self.connect_run(native=True)
        self.assertEqual(self.settings()["telemetry"]["otlpEndpoint"], "https://ingest.example/v1/logs")

    def test_no_extension_install_entries_carry_no_marker(self):
        self.connect_run(no_extension=True)
        self.assert_valid_and_keyless()
        settings = self.settings()
        self.assertEqual(settings["mcpServers"]["cardinal"],
                         {"url": MCP["url"], "headers": {"X-CardinalHQ-API-Key": MCP_KEY}})
        self.assertEqual(settings["mcpServers"]["cardinal-behavior"], self.connect.behavior_server())
        for groups in settings["hooks"].values():
            for group in groups:
                self.assertNotIn("cardinalManaged", group)
        owned = self.state()["settings_owned"]["mcp_servers"]
        self.assertEqual(owned["cardinal"], {"url": MCP["url"]})
        self.assertNotIn(MCP_KEY, self.state_path.read_text())

    def test_user_env_lines_are_kept(self):
        self.env_path.write_text("GEMINI_API_KEY=abc\n")
        self.connect_run(native=True)
        env = self.env_path.read_text()
        self.assertTrue(env.startswith("GEMINI_API_KEY=abc\n"), env)
        self.connect_run(native=True)   # a re-connect keeps a single block
        self.assertEqual(self.env_path.read_text().count("OTEL_EXPORTER_OTLP_HEADERS="), 1)

    def test_user_headers_in_env_leave_native_off(self):
        self.env_path.write_text("OTEL_EXPORTER_OTLP_HEADERS=authorization=mine\n")
        out = self.connect_run(native=True)
        self.assertNotIn("telemetry", self.settings() if self.settings_path.exists() else {})
        self.assertEqual(self.env_path.read_text(), "OTEL_EXPORTER_OTLP_HEADERS=authorization=mine\n")
        self.assertIn("already sets OTEL_EXPORTER_OTLP_HEADERS", out)

    def test_user_telemetry_block_is_never_replaced(self):
        mine = {"enabled": True, "target": "gcp"}
        self.write_settings({"telemetry": mine})
        out = self.connect_run(native=True)
        self.assertEqual(self.settings()["telemetry"], mine)
        self.assertFalse(self.env_path.exists())
        self.assertEqual(self.state()["native_telemetry"], {"requested": True, "enabled": False})
        self.assertIn("Cardinal did not write", out)

    def test_native_choice_is_kept_on_rotate_and_can_be_turned_off(self):
        self.connect_run(native=True)
        self.connect_run()   # --rotate without the flag: keeps it on
        self.assertIn("telemetry", self.settings())
        self.connect_run(native=False)
        self.assertNotIn("telemetry", self.settings())
        self.assertFalse(self.env_path.exists())


class RepairLegacySettingsTest(_Home):
    """Installs from plugin 0.22.1 and earlier: Gemini CLI does not start."""

    def legacy_settings(self) -> dict:
        return {
            "general": {"vimMode": True},
            "telemetry": dict(LEGACY_TELEMETRY),
            "mcpServers": {
                "cardinal": {"url": MCP["url"], "headers": {"X-CardinalHQ-API-Key": MCP_KEY},
                             "cardinalManaged": True},
                "cardinal-behavior": {**self.connect.behavior_server(), "cardinalManaged": True},
                "mine": {"command": "my-server"},
            },
        }

    def test_rerun_while_connected_repairs_the_legacy_block(self):
        self.write_connected()
        self.write_settings(self.legacy_settings())
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["cardinal-connect"]), contextlib.redirect_stdout(out):
            rc = self.connect.main()
        self.assertEqual(rc, 0, out.getvalue())
        self.assert_valid_and_keyless()
        settings = self.settings()
        self.assertNotIn("telemetry", settings)   # native exporter is opt-in
        self.assertEqual(settings["general"], {"vimMode": True})
        self.assertEqual(settings["mcpServers"]["mine"], {"command": "my-server"})
        self.assertEqual(settings["mcpServers"]["cardinal"]["url"], MCP["url"])
        owned = self.state()["settings_owned"]["mcp_servers"]
        self.assertEqual(set(owned), {"cardinal", "cardinal-behavior"})
        # The backup of the broken file does not keep the key either.
        for bak in self.gemini.glob("settings.json.bak.*"):
            self.assertNotIn(INGEST_KEY, bak.read_text(), bak)
        # Idempotent: nothing left to repair.
        self.assertEqual(self.connect.repair_settings(), ([], []))

    def test_repair_flag_with_native_rewrites_to_the_valid_block(self):
        self.write_connected()
        self.write_settings(self.legacy_settings())
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["cardinal-connect", "--repair", "--native-telemetry"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.connect.main(), 0)
        self.assert_valid_and_keyless()
        self.assertEqual(self.settings()["telemetry"]["target"], "local")
        self.assertEqual(self.settings()["telemetry"]["otlpEndpoint"], "https://ingest.example")
        self.assertIn(INGEST_KEY, self.env_path.read_text())
        self.assertEqual(self.state()["native_telemetry"], {"requested": True, "enabled": True})

    def test_repair_flag_works_without_a_connection(self):
        self.write_settings(self.legacy_settings())
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["cardinal-connect", "--repair"]), contextlib.redirect_stdout(out):
            self.assertEqual(self.connect.main(), 0)
        self.assert_valid_and_keyless()
        self.assertNotIn("telemetry", self.settings())
        self.assertIn(str(self.settings_path), out.getvalue())

    def test_old_backups_lose_the_plaintext_key(self):
        bak = self.gemini / "settings.json.bak.20261001-120000"
        bak.write_text(json.dumps({"telemetry": dict(LEGACY_TELEMETRY)}))
        self.write_settings({"telemetry": dict(LEGACY_TELEMETRY)})
        changed, _ = self.connect.repair_settings()
        self.assertIn(str(bak), changed)
        self.assertNotIn(INGEST_KEY, bak.read_text())

    def test_a_users_legacy_shaped_block_is_made_valid_not_removed(self):
        self.write_settings({"telemetry": {"enabled": True, "target": "otlp",
                                           "otlpEndpoint": "http://collector:4318",
                                           "otlpHeaders": {"authorization": "Bearer x"}}})
        changed, notes = self.connect.repair_settings()
        self.assertEqual(changed, [str(self.settings_path)])
        self.assertEqual(self.settings()["telemetry"],
                         {"enabled": True, "target": "local", "otlpEndpoint": "http://collector:4318"})
        self.assertTrue(notes)

    def test_status_reports_the_legacy_block(self):
        self.write_connected(endpoints=False)
        self.write_settings(self.legacy_settings())
        proc = self.run_script(STATUS)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("refuses to start", proc.stdout)
        self.assertIn("cardinal-connect --repair", proc.stdout)
        self.assertNotIn(INGEST_KEY, proc.stdout)

    def test_status_is_clean_after_repair(self):
        self.write_connected(endpoints=False)
        self.write_settings(self.legacy_settings())
        self.connect.repair_settings()
        proc = self.run_script(STATUS)
        self.assertNotIn("--repair", proc.stdout)


class DisconnectRemovesExactlyCardinalsTest(_Home):
    def test_disconnect_removes_native_block_servers_and_env_block(self):
        self.write_settings({"general": {"vimMode": True},
                             "mcpServers": {"mine": {"command": "my-server"}}})
        self.env_path.write_text("GEMINI_API_KEY=abc\n")
        self.connect_run(native=True, no_extension=True)
        self.drop_key_ids()
        proc = self.run_script(DISCONNECT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        settings = self.settings()
        self.assertEqual(settings, {"general": {"vimMode": True},
                                    "mcpServers": {"mine": {"command": "my-server"}}})
        self.assertEqual(self.env_path.read_text(), "GEMINI_API_KEY=abc\n")

    def test_disconnect_leaves_an_edited_block_and_a_users_server(self):
        self.connect_run(native=True)
        self.drop_key_ids()
        self.env_path.unlink()
        settings = self.settings()
        settings["telemetry"]["otlpEndpoint"] = "http://my-collector:4318"   # now the user's
        settings["mcpServers"] = {"cardinal": {"url": "https://other.example/mcp"}}
        self.write_settings(settings)
        proc = self.run_script(DISCONNECT, "--force")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        after = self.settings()
        self.assertEqual(after["telemetry"]["otlpEndpoint"], "http://my-collector:4318")
        self.assertEqual(after["mcpServers"], {"cardinal": {"url": "https://other.example/mcp"}})

    def test_disconnect_cleans_the_old_broken_shape(self):
        self.write_connected()
        self.write_settings({
            "telemetry": dict(LEGACY_TELEMETRY),
            "mcpServers": {"cardinal": {"url": MCP["url"], "cardinalManaged": True},
                           "mine": {"command": "my-server"}},
            "hooks": {"AfterTool": [{"matcher": "", "cardinalManaged": True, "hooks": [
                {"type": "command", "command": "python3 /x --event AfterTool # cardinal-gemini-plugin"}]}]},
        })
        proc = self.run_script(DISCONNECT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.settings(), {"mcpServers": {"mine": {"command": "my-server"}}})
        for bak in self.gemini.glob("settings.json.bak.*"):
            self.assertNotIn(INGEST_KEY, bak.read_text(), bak)

    def test_disconnect_makes_a_users_legacy_shaped_block_valid(self):
        self.write_connected()
        self.write_settings({"telemetry": {"enabled": True, "target": "otlp", "otlpHeaders": {"a": "b"}}})
        proc = self.run_script(DISCONNECT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.settings(), {"telemetry": {"enabled": True, "target": "local"}})


GEMINI = shutil.which("gemini")


@unittest.skipUnless(GEMINI and os.environ.get("CARDINAL_SKIP_GEMINI_BINARY") != "1",
                     "the gemini CLI is not installed")
class GeminiBinaryParseTest(_Home):
    """The real CLI loads what cardinal-connect writes. GEMINI_CLI_HOME points
    Gemini at the temporary home (never ~/.gemini); --list-extensions exits
    after configuration loads, before any model call."""

    def run_gemini(self) -> subprocess.CompletedProcess:
        cwd = self.home / "project"
        cwd.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OTEL_", "GEMINI_", "GOOGLE_"))}
        # HOME too: any hook Gemini runs (SessionEnd at exit) sees the temp home.
        env.update(HOME=str(self.home), GEMINI_CLI_HOME=str(self.home), GEMINI_CLI_TRUST_WORKSPACE="true",
                   GEMINI_CLI_NO_RELAUNCH="true", GEMINI_API_KEY="not-a-real-key",
                   HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
                   NO_PROXY="127.0.0.1,localhost")
        return subprocess.run([GEMINI, "--list-extensions"], cwd=str(cwd), env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def prepare(self, settings: dict) -> None:
        settings.setdefault("privacy", {"usageStatisticsEnabled": False})
        settings.setdefault("general", {"enableAutoUpdate": False,
                                        "enableAutoUpdateNotification": False})
        # Validated like any other key; no Cardinal hook runs from this test.
        settings.setdefault("hooksConfig", {"enabled": False})
        self.write_settings(settings)

    def test_legacy_block_is_rejected(self):
        # The bug: proves this harness reaches Gemini's telemetry validation.
        legacy = dict(LEGACY_TELEMETRY, otlpEndpoint="http://127.0.0.1:9/v1/logs")
        self.prepare({"telemetry": legacy})
        proc = self.run_gemini()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Invalid telemetry target: otlp", proc.stdout + proc.stderr)

    def test_connect_output_loads(self):
        self.prepare({})
        with mock.patch.dict(INGEST, endpoint="http://127.0.0.1:9"):
            self.connect_run(native=True, no_extension=True)
        proc = self.run_gemini()
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertNotIn("Invalid configuration", output)
        self.assertNotIn("Invalid telemetry", output)

    def test_repaired_legacy_install_loads(self):
        self.write_connected(ingest_endpoint="http://127.0.0.1:9")
        self.prepare({"telemetry": dict(LEGACY_TELEMETRY),
                      "mcpServers": {"cardinal": {"url": "http://127.0.0.1:9/mcp",
                                                  "cardinalManaged": True}}})
        self.connect.repair_settings(native=True)
        proc = self.run_gemini()
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertNotIn("Invalid configuration", output)


if __name__ == "__main__":
    unittest.main()

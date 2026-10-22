"""Copilot CLI's user MCP config (hooks/_copilot_config.py) as
cardinal-connect / -disconnect / -status manage it, and the connect-time
owner-input consent.

mcp-config.json gets two entries, `cardinal` (remote gateway, the MCP key in
`headers`) and `cardinal-behavior` (the plugin's stdio server). Copilot may
skip an entry with keys it does not know, so nothing Cardinal-specific is
written into an entry: ownership is recorded as fingerprints in the state
file's `managed_mcp`, and disconnect removes an entry only while it is still
the one recorded there.

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_connect.py -v
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fixtures import ADAPTER, CONNECT, DISCONNECT, STATUS, FakeMaestro, clean_env, load_script

sys.path.insert(0, str(ADAPTER / "hooks"))
from cardinal_core import owner_input  # noqa: E402

config = load_script(ADAPTER / "hooks" / "_copilot_config.py", "copilot_config_under_test")

BEHAVIOR = ADAPTER / "scripts" / "cardinal-behavior"
GATEWAY_KEYS = {"type", "url", "headers", "tools"}
BEHAVIOR_KEYS = {"type", "command", "args", "tools"}
OTHER = {"other": {"type": "http", "url": "https://foreign.example/mcp", "tools": ["*"]}}


class TTYIn(io.StringIO):
    def isatty(self) -> bool:
        return True


class ConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "mcp-config.json"

    def write(self, managed=None, url="https://app.example/api/orgs/o/mcp", key="ck_test"):
        return config.write_entries(self.path, url, key, "9.9.9", BEHAVIOR, managed)

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data))

    def test_write_into_a_missing_file(self):
        data, warnings = self.write()
        self.assertEqual(warnings, [])
        servers = data["mcpServers"]
        self.assertEqual(set(servers), {"cardinal", "cardinal-behavior"})
        self.assertEqual(servers["cardinal"], {
            "type": "http", "url": "https://app.example/api/orgs/o/mcp",
            "headers": {"X-CardinalHQ-API-Key": "ck_test", "X-Cardinal-Client": "copilot/9.9.9"},
            "tools": ["*"]})
        self.assertEqual(servers["cardinal-behavior"], {
            "type": "stdio", "command": "python3", "args": [str(BEHAVIOR), "--runtime", "copilot"],
            "tools": ["*"]})
        self.assertFalse(self.path.exists(), "write_entries returns the config; the caller saves it")

    def test_no_cardinal_specific_keys_in_any_entry(self):
        data, _ = self.write()
        servers = data["mcpServers"]
        self.assertEqual(set(servers["cardinal"]), GATEWAY_KEYS)
        self.assertEqual(set(servers["cardinal-behavior"]), BEHAVIOR_KEYS)
        self.assertNotIn("cardinalManaged", json.dumps(data))
        self.assertEqual(set(data), {"mcpServers"})
        # The key never lands in the stdio entry's argv.
        self.assertNotIn("ck_test", json.dumps(servers["cardinal-behavior"]))

    def test_unrelated_servers_and_top_level_keys_are_kept(self):
        self.save({"mcpServers": dict(OTHER), "someSetting": True})
        data, _ = self.write()
        self.assertEqual(data["mcpServers"]["other"], OTHER["other"])
        self.assertTrue(data["someSetting"])

    def test_an_unmanaged_cardinal_entry_refuses_the_write(self):
        mine = {"type": "http", "url": "https://mine.example/mcp"}
        self.save({"mcpServers": {"cardinal": mine}})
        with self.assertRaises(config.ConfigError) as cm:
            self.write()
        self.assertIn("mcpServers.cardinal", str(cm.exception))
        # A managed record for a different URL does not make it Cardinal's.
        with self.assertRaises(config.ConfigError):
            self.write(managed={"cardinal": {"url": "https://other.example/mcp"}})

    def test_a_managed_entry_may_be_rewritten(self):
        first, _ = self.write()
        managed = config.managed_record(first, BEHAVIOR)
        self.assertEqual(managed, {"cardinal": {"url": "https://app.example/api/orgs/o/mcp"},
                                   "cardinal-behavior": {"command": "python3",
                                                         "args": [str(BEHAVIOR), "--runtime", "copilot"]}})
        self.save(first)
        second, warnings = self.write(managed=managed, key="ck_rotated")
        self.assertEqual(warnings, [])
        self.assertEqual(second["mcpServers"]["cardinal"]["headers"]["X-CardinalHQ-API-Key"], "ck_rotated")

    def test_an_unmanaged_behavior_entry_is_left_with_a_warning(self):
        mine = {"type": "stdio", "command": "/opt/mine"}
        self.save({"mcpServers": {"cardinal-behavior": mine}})
        data, warnings = self.write()
        self.assertEqual(data["mcpServers"]["cardinal-behavior"], mine)
        self.assertEqual(len(warnings), 1)
        self.assertIn("cardinal-behavior", warnings[0])
        self.assertEqual(set(config.managed_record(data, BEHAVIOR)), {"cardinal"})

    def test_an_identical_behavior_entry_is_adopted(self):
        self.save({"mcpServers": {"cardinal-behavior": config.build_behavior_entry(BEHAVIOR)}})
        data, warnings = self.write()
        self.assertEqual(warnings, [])
        self.assertIn("cardinal-behavior", config.managed_record(data, BEHAVIOR))

    def test_remove_entries_removes_only_managed_unchanged_entries(self):
        data, _ = self.write()
        managed = config.managed_record(data, BEHAVIOR)
        data["mcpServers"].update(OTHER)
        self.save(data)
        out, removed, kept = config.remove_entries(self.path, managed)
        self.assertEqual((removed, kept), (["cardinal", "cardinal-behavior"], []))
        self.assertEqual(out, {"mcpServers": OTHER})
        # Changed by the user since: kept.
        data["mcpServers"]["cardinal"]["url"] = "https://edited.example/mcp"
        self.save(data)
        out, removed, kept = config.remove_entries(self.path, managed)
        self.assertEqual((removed, kept), (["cardinal-behavior"], ["cardinal"]))
        self.assertIn("cardinal", out["mcpServers"])
        # No record at all: nothing removed, nothing to write.
        self.assertEqual(config.remove_entries(self.path, None), (None, [], ["cardinal", "cardinal-behavior"]))
        self.assertEqual(config.remove_entries(self.path, "garbage")[0], None)

    def test_remove_entries_drops_an_emptied_mcp_servers(self):
        data, _ = self.write()
        self.save(data)
        out, removed, _ = config.remove_entries(self.path, config.managed_record(data, BEHAVIOR))
        self.assertEqual(out, {})
        self.assertEqual(len(removed), 2)

    def test_missing_file_and_bad_files(self):
        self.assertEqual(config.remove_entries(self.path, {}), (None, [], []))
        for text in ("{not json", "[1]"):
            self.path.write_text(text)
            with self.assertRaises(config.ConfigError):
                config.load(self.path)
            self.assertIsNone(config.gateway_key(self.path))

    def test_gateway_key(self):
        self.assertIsNone(config.gateway_key(self.path))
        data, _ = self.write(key="ck_gateway")
        self.save(data)
        self.assertEqual(config.gateway_key(self.path), "ck_gateway")
        self.save({"mcpServers": {"cardinal": {"url": "u", "headers": {"X-CardinalHQ-API-Key": ""}}}})
        self.assertIsNone(config.gateway_key(self.path))

    def test_is_managed(self):
        entry = config.build_mcp_entry("https://u", "k", "1")
        self.assertTrue(config.is_managed("cardinal", entry, {"cardinal": {"url": "https://u"}}))
        self.assertFalse(config.is_managed("cardinal", entry, {"cardinal": {"url": "https://v"}}))
        self.assertFalse(config.is_managed("cardinal", entry, {}))
        self.assertFalse(config.is_managed("cardinal", entry, None))
        self.assertFalse(config.is_managed("cardinal", "not a dict", {"cardinal": {"url": "https://u"}}))
        # An absent entry is never Cardinal's, even against a null record.
        self.assertFalse(config.is_managed("cardinal", None, {"cardinal": None}))
        self.assertFalse(config.is_managed("cardinal", None, {"cardinal": {"url": "https://u"}}))
        self.assertFalse(config.is_managed("cardinal", entry, {"cardinal": None}))

    def test_copilot_home(self):
        self.assertEqual(config.copilot_home({"COPILOT_HOME": "/opt/ch"}), Path("/opt/ch"))
        self.assertEqual(config.mcp_config_path({"COPILOT_HOME": "/opt/ch"}), Path("/opt/ch/mcp-config.json"))
        self.assertEqual(config.copilot_home({"COPILOT_HOME": "relative"}), Path.home() / ".copilot")
        self.assertEqual(config.copilot_home({}), Path.home() / ".copilot")


class ScriptCase(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(os.path.realpath(tmp.name))
        self.copilot = self.home / ".copilot"
        self.copilot.mkdir()
        self.mcp = self.copilot / "mcp-config.json"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("COPILOT_HOME", None)

    def env(self, **extra: str) -> dict:
        return clean_env(self.home, **extra)


class ConnectScriptTests(ScriptCase):
    def setUp(self):
        super().setUp()
        self.connect = load_script(CONNECT, f"copilot_connect_{id(self)}")

    def test_paths_live_under_the_sandboxed_home(self):
        self.assertEqual(self.connect.STATE_PATH, self.copilot / "cardinal.json")
        self.assertEqual(self.connect.SECRETS_PATH, self.copilot / "cardinal-secrets.json")
        self.assertEqual(self.connect.mcp_path(), self.mcp)
        self.assertEqual(self.connect.CLIENT_ID, "cardinal-copilot-plugin")

    def test_write_mcp_config_is_0600_and_keeps_other_servers(self):
        self.mcp.write_text(json.dumps({"mcpServers": OTHER}))
        self.mcp.chmod(0o644)
        backup, managed = self.connect.write_mcp_config(self.mcp, "https://app.example/mcp", "ck_1", None)
        self.assertEqual(stat.S_IMODE(self.mcp.stat().st_mode), 0o600)
        self.assertIsNotNone(backup)
        self.assertEqual(json.loads(Path(backup).read_text()), {"mcpServers": OTHER})
        servers = json.loads(self.mcp.read_text())["mcpServers"]
        self.assertEqual(set(servers), {"other", "cardinal", "cardinal-behavior"})
        self.assertEqual(servers["cardinal-behavior"]["args"],
                         [str(ADAPTER.resolve() / "scripts" / "cardinal-behavior"), "--runtime", "copilot"])
        self.assertEqual(servers["cardinal"]["headers"]["X-Cardinal-Client"], f"copilot/{self.connect.PLUGIN_VERSION}")
        self.assertEqual(managed["cardinal"], {"url": "https://app.example/mcp"})
        self.assertNotIn("cardinalManaged", self.mcp.read_text())

    def test_write_into_a_fresh_home(self):
        self.copilot.rmdir()
        backup, _ = self.connect.write_mcp_config(self.mcp, "https://app.example/mcp", "ck_1", None)
        self.assertIsNone(backup)
        self.assertEqual(stat.S_IMODE(self.mcp.stat().st_mode), 0o600)

    def test_write_mcp_config_refuses_an_unmanaged_cardinal_entry(self):
        mine = {"mcpServers": {"cardinal": {"type": "http", "url": "https://mine.example"}}}
        self.mcp.write_text(json.dumps(mine))
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stdout", new=io.StringIO()):
            self.connect.write_mcp_config(self.mcp, "https://app.example/mcp", "ck_1", None)
        self.assertIn("Refusing to write", str(cm.exception.code))
        self.assertEqual(json.loads(self.mcp.read_text()), mine)

    def test_repair_registration_restores_the_behavior_entry(self):
        _, managed = self.connect.write_mcp_config(self.mcp, "https://app.example/mcp", "ck_1", None)
        (self.copilot / "cardinal.json").write_text(json.dumps({"user_email": "u@example.com",
                                                                "managed_mcp": managed,
                                                                "mcp_config_path": str(self.mcp)}))
        data = json.loads(self.mcp.read_text())
        del data["mcpServers"]["cardinal-behavior"]
        self.mcp.write_text(json.dumps(data))
        self.assertEqual(self.connect.repair_registration(), [str(self.mcp)])
        self.assertIn("cardinal-behavior", json.loads(self.mcp.read_text())["mcpServers"])
        self.assertEqual(self.connect.repair_registration(), [])

    def test_repair_without_a_managed_cardinal_entry_registers_nothing(self):
        self.mcp.write_text(json.dumps({"mcpServers": {"cardinal": {"url": "mine"}}}))
        (self.copilot / "cardinal.json").write_text(json.dumps({"user_email": "u@example.com"}))
        self.assertEqual(self.connect.repair_registration(), [])
        self.assertNotIn("cardinal-behavior", json.loads(self.mcp.read_text())["mcpServers"])

    def test_rerun_without_rotate_when_up_to_date_exits_2(self):
        _, managed = self.connect.write_mcp_config(self.mcp, "https://app.example/mcp", "ck_1", None)
        (self.copilot / "cardinal.json").write_text(json.dumps({"user_email": "u@example.com",
                                                                "managed_mcp": managed}))
        res = subprocess.run([sys.executable, str(CONNECT)], capture_output=True, text=True, env=self.env(),
                             timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("--rotate", res.stderr)


class OwnerInputConsentTests(ScriptCase):
    def setUp(self):
        super().setUp()
        self.connect = load_script(CONNECT, f"copilot_connect_oi_{id(self)}")

    def test_on_needs_a_typed_yes_in_a_terminal(self):
        out = io.StringIO()
        self.assertEqual(self.connect.owner_input_consent(True, home=self.home, stdin=io.StringIO("yes\n"),
                                                          stdout=out), 2)
        self.assertIn(owner_input.consent_text("copilot"), out.getvalue())
        self.assertIn("Not recorded", out.getvalue())
        self.assertEqual(self.connect.owner_input_consent(True, home=self.home, stdin=TTYIn("no\n"),
                                                          stdout=io.StringIO()), 1)
        self.assertFalse(owner_input.consent("copilot", self.home))
        self.assertEqual(self.connect.owner_input_consent(True, home=self.home, stdin=TTYIn("yes\n"),
                                                          stdout=io.StringIO()), 0)
        self.assertTrue(owner_input.consent("copilot", self.home))
        path = owner_input.consent_path("copilot", self.home)
        self.assertEqual(path, self.copilot / "cardinal" / "owner-input-consent.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_cli_on_refuses_without_a_tty_and_off_records_consent_off(self):
        piped = subprocess.run([sys.executable, str(CONNECT), "--owner-input", "on"], input="yes\n",
                               capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(piped.returncode, 2, piped.stdout + piped.stderr)
        self.assertIn(owner_input.consent_text("copilot"), piped.stdout)
        self.assertFalse(owner_input.consent_path("copilot", self.home).exists())
        self.assertEqual(self.connect.owner_input_consent(True, home=self.home, stdin=TTYIn("yes\n"),
                                                          stdout=io.StringIO()), 0)
        off = subprocess.run([sys.executable, str(CONNECT), "--owner-input", "off"], capture_output=True,
                             text=True, env=self.env(), timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertIn("Owner input is OFF for Copilot CLI", off.stdout)
        self.assertNotIn("device", off.stdout.lower())
        self.assertFalse(owner_input.consent("copilot", self.home))
        self.assertTrue(owner_input.consent_path("copilot", self.home).exists())
        self.assertFalse((self.copilot / "cardinal.json").exists())


class DisconnectTests(ScriptCase):
    def setUp(self):
        super().setUp()
        self.maestro = FakeMaestro().start()
        self.addCleanup(self.maestro.stop)
        self.maestro.routes["revoke"] = (200, {})
        connect = load_script(CONNECT, f"copilot_connect_dc_{id(self)}")
        self.mcp.write_text(json.dumps({"mcpServers": OTHER}))
        with mock.patch("sys.stdout", new=io.StringIO()):
            _, managed = connect.write_mcp_config(self.mcp, "https://app.example/mcp", "ck_gateway", None)
        self.managed = managed
        (self.copilot / "cardinal.json").write_text(json.dumps({
            "host": self.maestro.origin, "user_email": "u@example.com", "mcp_config_path": str(self.mcp),
            "managed_mcp": managed, "ingest_key_id": "ik-1", "ingest_key_prefix": "ing_",
            "mcp_key_id": "mk-1", "mcp_key_prefix": "ck_g", "act_key_id": "ak-1", "act_key_prefix": "act_"}))
        (self.copilot / "cardinal-secrets.json").write_text(json.dumps({
            "ingest_api_key": "ingest-secret", "mcp_api_key": "stale-mcp-key", "act_api_key": "act-secret"}))
        runtime = self.copilot / "cardinal"
        (runtime / "limits").mkdir(parents=True)
        owner_input.set_consent("copilot", self.home, True)

    def disconnect(self) -> subprocess.CompletedProcess:
        res = subprocess.run([sys.executable, str(DISCONNECT)], capture_output=True, text=True, env=self.env(),
                             timeout=30)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res

    def test_disconnect_revokes_and_removes_only_managed_entries(self):
        res = self.disconnect()
        self.assertEqual(json.loads(self.mcp.read_text()), {"mcpServers": OTHER})
        self.assertFalse((self.copilot / "cardinal.json").exists())
        self.assertFalse((self.copilot / "cardinal-secrets.json").exists())
        self.assertFalse((self.copilot / "cardinal").exists())
        self.assertIn("Cleared the owner-input consent", res.stdout)
        self.assertFalse(owner_input.consent("copilot", self.home))
        revokes = {r["path"]: r["headers"].get("x-cardinalhq-api-key") for r in self.maestro.paths("/revoke")}
        self.assertEqual(revokes, {"/api/maestro-keys/ik-1/revoke": "ingest-secret",
                                   "/api/maestro-keys/mk-1/revoke": "ck_gateway",
                                   "/api/maestro-keys/ak-1/revoke": "act-secret"})
        self.assertEqual(stat.S_IMODE(self.mcp.stat().st_mode), 0o600)
        self.assertTrue(list(self.copilot.glob("mcp-config.json.bak.*")))

    def test_an_edited_entry_is_kept(self):
        data = json.loads(self.mcp.read_text())
        data["mcpServers"]["cardinal"]["url"] = "https://edited.example/mcp"
        self.mcp.write_text(json.dumps(data))
        res = self.disconnect()
        servers = json.loads(self.mcp.read_text())["mcpServers"]
        self.assertEqual(set(servers), {"other", "cardinal"})
        self.assertIn("Skipping mcpServers.cardinal", res.stdout)

    def test_revoke_failure_still_disconnects(self):
        self.maestro.routes["revoke"] = (500, {})
        res = self.disconnect()
        self.assertIn("Could not revoke", res.stdout)
        self.assertFalse((self.copilot / "cardinal.json").exists())
        self.assertEqual(json.loads(self.mcp.read_text()), {"mcpServers": OTHER})

    def test_not_connected_is_a_no_op(self):
        (self.copilot / "cardinal.json").unlink()
        res = self.disconnect()
        self.assertIn("not connected", res.stdout)
        self.assertEqual(self.maestro.requests, [])
        self.assertIn("cardinal", json.loads(self.mcp.read_text())["mcpServers"])


class StatusTests(ScriptCase):
    def status(self, rc: int = 0, **env: str) -> str:
        res = subprocess.run([sys.executable, str(STATUS)], capture_output=True, text=True, env=self.env(**env),
                             timeout=30)
        self.assertEqual(res.returncode, rc, res.stdout + res.stderr)
        return res.stdout

    def test_not_connected(self):
        out = self.status(rc=1)
        self.assertIn("Not connected. Run cardinal-connect", out)
        self.assertIn(str(ADAPTER / "hooks" / "hooks.json"), out)

    def test_owner_input_line(self):
        (self.copilot / "cardinal.json").write_text(json.dumps({"user_email": "dev@example.com"}))

        def line(**env: str) -> str:
            return next(x for x in self.status(**env).splitlines() if x.startswith("Owner input:"))

        self.assertIn("off (cardinal-connect --owner-input on", line())
        owner_input.set_consent("copilot", self.home, True)
        self.assertIn("on (you consented", line())
        self.assertIn("off (kill switch", line(CARDINAL_OWNER_INPUT="0"))


if __name__ == "__main__":
    unittest.main()

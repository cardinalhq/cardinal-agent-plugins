"""Checks preview + the cardinal-behavior MCP server on Cursor
(docs/plans/cross-agent-parity.md, Lane G). Fixture-only: Cursor is not
installed on the machine these were written on.

- cardinal-connect writes a managed `cardinal-behavior` stdio entry
  (--runtime cursor) into ~/.cursor/mcp.json next to `cardinal` (not into a
  project's .cursor/mcp.json: cloud agents have no local plugin), keeps an
  unmanaged one, and cardinal-disconnect removes the managed entries.
- scripts/cardinal-checks installs the skill into the plugin's skills/;
  sessionStart reconciles it and adds the routing rule while on and
  connected. No skill reload or skill-tool deny on Cursor: a change takes
  effect at the next session.

Run: cd adapters/cursor && python3 -m unittest discover -s tests -p test_cursor_checks.py -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ADAPTER = Path(__file__).resolve().parent.parent
REPO = ADAPTER.parents[1]
ORG = "12345678-1234-1234-1234-123456789abc"
ROUTING = "Cardinal Checks preview is on"


def load(path: Path, name: str):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent.parent / "hooks"))
    try:
        loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent.parent / "hooks"))
    return module


class PluginCopy(unittest.TestCase):
    def setUp(self):
        if not (ADAPTER / "hooks" / "cardinal_core" / "checks_install.py").exists():
            subprocess.run([sys.executable, str(REPO / "build" / "vendor.py"), "cursor"], check=True,
                           capture_output=True)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        (self.home / ".cursor").mkdir(parents=True)
        self.plugin = Path(tmp.name) / "plugin"
        for part in ("hooks", "scripts", "checks", ".cursor-plugin"):
            shutil.copytree(ADAPTER / part, self.plugin / part, ignore=shutil.ignore_patterns("__pycache__"))
        (self.plugin / "skills").mkdir()
        shutil.copytree(REPO / "common" / "behavior", self.plugin / "lib" / "behavior",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mcp_json = self.home / ".cursor" / "mcp.json"

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
        env.update(HOME=str(self.home), CARDINAL_STORYBOARD_SESSION_START="0")
        return env


class RegistrationTests(PluginCopy):
    def setUp(self):
        super().setUp()
        self.connect = load(self.plugin / "scripts" / "cardinal-connect", f"cursor_connect_{id(self)}")
        self.disconnect = load(self.plugin / "scripts" / "cardinal-disconnect", f"cursor_disconnect_{id(self)}")

    def test_user_mcp_json_gets_a_managed_behavior_entry_that_disconnect_removes(self):
        self.mcp_json.write_text(json.dumps({"mcpServers": {"other": {"url": "https://foreign"}}}))
        self.connect.write_mcp_config(self.mcp_json, "https://mcp.example", "key")
        servers = json.loads(self.mcp_json.read_text())["mcpServers"]
        entry = servers["cardinal-behavior"]
        self.assertEqual(entry["command"], "python3")
        self.assertEqual(entry["args"], [str(self.plugin.resolve() / "scripts" / "cardinal-behavior"), "--runtime", "cursor"])
        self.assertTrue(entry["cardinalManaged"])
        self.assertNotIn("key", json.dumps(entry["args"]))
        self.assertTrue(self.disconnect.remove_mcp_entry(self.mcp_json))
        self.assertEqual(json.loads(self.mcp_json.read_text())["mcpServers"], {"other": {"url": "https://foreign"}})

    def test_project_mcp_json_has_no_behavior_entry(self):
        project = self.home / "repo" / ".cursor" / "mcp.json"
        self.connect.write_mcp_config(project, "https://mcp.example", "key", behavior=False)
        self.assertEqual(set(json.loads(project.read_text())["mcpServers"]), {"cardinal"})

    def test_an_unmanaged_behavior_entry_is_kept_by_connect_and_disconnect(self):
        mine = {"command": "/opt/mine"}
        self.mcp_json.write_text(json.dumps({"mcpServers": {"cardinal-behavior": mine}}))
        self.connect.write_mcp_config(self.mcp_json, "https://mcp.example", "key")
        self.assertEqual(json.loads(self.mcp_json.read_text())["mcpServers"]["cardinal-behavior"], mine)
        self.disconnect.remove_mcp_entry(self.mcp_json)
        self.assertEqual(json.loads(self.mcp_json.read_text())["mcpServers"], {"cardinal-behavior": mine})

    def test_a_managed_behavior_entry_alone_is_still_removed(self):
        self.connect.write_mcp_config(self.mcp_json, "https://mcp.example", "key")
        data = json.loads(self.mcp_json.read_text())
        del data["mcpServers"]["cardinal"]
        self.mcp_json.write_text(json.dumps(data))
        self.assertTrue(self.disconnect.remove_mcp_entry(self.mcp_json))
        self.assertFalse(self.mcp_json.exists())

    def test_the_registered_server_starts_for_cursor(self):
        self.connect.write_mcp_config(self.mcp_json, "https://mcp.example", "key")
        entry = json.loads(self.mcp_json.read_text())["mcpServers"]["cardinal-behavior"]
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        result = subprocess.run([sys.executable, *entry["args"]], input="".join(json.dumps(r) + "\n" for r in requests),
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        init, tools = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(init["result"]["serverInfo"]["name"], "cardinal-behavior")
        self.assertIn("set_check_example", [t["name"] for t in tools["result"]["tools"]])


class ChecksSkillTests(PluginCopy):
    def checks(self, *args):
        return subprocess.run([sys.executable, str(self.plugin / "scripts" / "cardinal-checks"), *args],
                              capture_output=True, text=True, env=self.env(), timeout=30)

    def session_start(self):
        result = subprocess.run([sys.executable, str(self.plugin / "hooks" / "cardinal-cursor-telemetry.py"),
                                 "--event", "sessionStart"],
                                input=json.dumps({"conversation_id": "c1", "hook_event_name": "sessionStart",
                                                  "workspace_roots": [str(self.home)]}),
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.strip()
        return json.loads(out).get("additional_context", "") if out else ""

    def connect_files(self):
        (self.home / ".cursor" / "cardinal.json").write_text(
            json.dumps({"mcp_url": f"https://app.example/api/orgs/{ORG}/mcp"}))
        (self.home / ".cursor" / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "k"}))

    def test_on_off_installs_into_the_plugin_skills(self):
        on = self.checks("on")
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertIn("Start a new Cursor session", on.stdout)
        installed = self.plugin / "skills" / "checks"
        self.assertEqual((installed / "SKILL.md").read_text(), (ADAPTER / "checks" / "skill" / "SKILL.md").read_text())
        self.assertTrue((self.home / ".cursor" / "cardinal" / "checks" / "enabled.json").is_file())
        self.assertEqual(self.checks("off").returncode, 0)
        self.assertFalse(installed.exists())

    def test_session_start_reconciles_and_routes_only_while_on_and_connected(self):
        installed = self.plugin / "skills" / "checks"
        self.connect_files()
        self.assertNotIn(ROUTING, self.session_start())
        self.checks("on")
        shutil.rmtree(installed)
        self.assertIn(ROUTING, self.session_start())
        self.assertTrue((installed / "SKILL.md").is_file())
        (self.home / ".cursor" / "cardinal" / "checks" / "enabled.json").write_text('{"enabled": false}')
        self.assertNotIn(ROUTING, self.session_start())
        self.assertFalse(installed.exists())

    def test_skill_names_the_behavior_server_and_polling(self):
        skill = (ADAPTER / "checks" / "skill" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: cardinal-checks\n"))
        self.assertIn("`cardinal-behavior` server", skill)
        self.assertIn("get_behavior_range", skill)
        self.assertIn("next Cursor session", skill)
        self.assertNotIn("Monitor", skill)


if __name__ == "__main__":
    unittest.main()

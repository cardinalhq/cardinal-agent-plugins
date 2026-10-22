"""Checks preview + the cardinal-behavior MCP server on Copilot CLI.

- cardinal-connect registers a `cardinal-behavior` stdio entry
  (--runtime copilot) in ~/.copilot/mcp-config.json next to `cardinal`
  (test_copilot_connect.py covers the ownership rules); the registered
  command starts the server.
- scripts/cardinal-checks installs the skill into the plugin's skills/
  (.plugin/plugin.json "skills": "skills"); sessionStart reconciles it and
  adds the routing rule while on and connected. A change takes effect at the
  next Copilot CLI session.

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_checks.py -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fixtures import ADAPTER, REPO_ROOT, clean_env, load_script

sys.path.insert(0, str(ADAPTER / "hooks"))
from cardinal_core import checks_install  # noqa: E402

ORG = "12345678-1234-1234-1234-123456789abc"
ROUTING = "Cardinal Checks preview is on"


class PluginCopy(unittest.TestCase):
    """A copy of the plugin (cardinal-checks writes into <plugin>/skills/)
    laid out as installed: lib/behavior instead of the checkout's common/."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(os.path.realpath(tmp.name))
        self.home = base / "home"
        (self.home / ".copilot").mkdir(parents=True)
        self.plugin = base / "plugin"
        for part in ("hooks", "scripts", "checks", ".plugin"):
            shutil.copytree(ADAPTER / part, self.plugin / part, ignore=shutil.ignore_patterns("__pycache__"))
        (self.plugin / "skills").mkdir()
        shutil.copytree(REPO_ROOT / "common" / "behavior", self.plugin / "lib" / "behavior",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mcp = self.home / ".copilot" / "mcp-config.json"
        self.installed = self.plugin / "skills" / "checks"

    def env(self) -> dict:
        return clean_env(self.home, CARDINAL_STORYBOARD_SESSION_START="0", CARDINAL_INVESTIGATION_POLLER="0")

    def connect_files(self) -> None:
        (self.home / ".copilot" / "cardinal.json").write_text(
            json.dumps({"mcp_url": f"https://app.example/api/orgs/{ORG}/mcp"}))
        (self.home / ".copilot" / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "k"}))


class BehaviorServerTests(PluginCopy):
    def test_the_registered_server_starts_for_copilot(self):
        connect = load_script(self.plugin / "scripts" / "cardinal-connect", f"copilot_connect_ck_{id(self)}")
        with mock.patch("sys.stdout", new=io.StringIO()):
            connect.write_mcp_config(self.mcp, "https://mcp.example", "key", None)
        entry = json.loads(self.mcp.read_text())["mcpServers"]["cardinal-behavior"]
        self.assertEqual(entry["args"], [str(self.plugin.resolve() / "scripts" / "cardinal-behavior"),
                                         "--runtime", "copilot"])
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        result = subprocess.run([sys.executable, *entry["args"]], input="".join(json.dumps(r) + "\n" for r in requests),
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        init, tools = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(init["result"]["serverInfo"]["name"], "cardinal-behavior")
        self.assertIn("set_check_example", [t["name"] for t in tools["result"]["tools"]])

    def test_launcher_adds_the_runtime_when_missing(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"}]
        result = subprocess.run([sys.executable, str(self.plugin / "scripts" / "cardinal-behavior")],
                                input=json.dumps(requests[0]) + "\n", capture_output=True, text=True,
                                env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])["result"]["serverInfo"]["name"],
                         "cardinal-behavior")


class ChecksSkillTests(PluginCopy):
    def checks(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.plugin / "scripts" / "cardinal-checks"), *args],
                              capture_output=True, text=True, env=self.env(), timeout=30)

    def session_start(self) -> str:
        result = subprocess.run([sys.executable, str(self.plugin / "hooks" / "cardinal-copilot-telemetry.py"),
                                 "--event", "sessionStart"],
                                input=json.dumps({"sessionId": "s1", "cwd": str(self.home), "source": "startup"}),
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.strip()
        return json.loads(out).get("additionalContext", "") if out else ""

    def test_on_off_installs_into_the_plugin_skills(self):
        on = self.checks("on")
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertIn("Start a new Copilot CLI session", on.stdout)
        self.assertEqual((self.installed / "SKILL.md").read_text(),
                         (ADAPTER / "checks" / "skill" / "SKILL.md").read_text())
        self.assertTrue((self.home / ".copilot" / "cardinal" / "checks" / "enabled.json").is_file())
        off = self.checks("off")
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertFalse(self.installed.exists())

    def test_install_dir_is_the_plugins_skills(self):
        self.assertEqual(checks_install.install_dir("copilot", self.plugin), self.plugin / "skills" / "checks")
        manifest = json.loads((self.plugin / ".plugin" / "plugin.json").read_text())
        self.assertEqual(self.plugin / manifest["skills"] / "checks", self.installed)

    def test_session_start_reconciles_and_routes_only_while_on_and_connected(self):
        self.connect_files()
        self.assertNotIn(ROUTING, self.session_start())
        self.checks("on")
        shutil.rmtree(self.installed)
        self.assertIn(ROUTING, self.session_start())
        self.assertTrue((self.installed / "SKILL.md").is_file())
        (self.home / ".copilot" / "cardinal" / "checks" / "enabled.json").write_text('{"enabled": false}')
        self.assertNotIn(ROUTING, self.session_start())
        self.assertFalse(self.installed.exists())

    def test_unconnected_session_start_installs_but_does_not_route(self):
        self.checks("on")
        ctx = self.session_start()
        self.assertNotIn(ROUTING, ctx)
        self.assertIn("Cardinal is not connected", ctx)

    def test_disconnect_removes_the_installed_skill_and_the_setting(self):
        self.connect_files()
        self.assertEqual(self.checks("on").returncode, 0)
        self.assertTrue((self.installed / "SKILL.md").is_file())
        result = subprocess.run([sys.executable, str(self.plugin / "scripts" / "cardinal-disconnect")],
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Removed the Checks preview skill", result.stdout)
        self.assertFalse(self.installed.exists())
        self.assertFalse((self.home / ".copilot" / "cardinal" / "checks").exists())

    def test_skill_names_the_behavior_server_and_copilot(self):
        skill = (ADAPTER / "checks" / "skill" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: cardinal-checks\n"))
        self.assertIn("`cardinal-behavior` server", skill)
        self.assertIn("get_behavior_range", skill)
        self.assertIn("next Copilot CLI session", skill)
        self.assertNotIn("Monitor", skill)


if __name__ == "__main__":
    unittest.main()

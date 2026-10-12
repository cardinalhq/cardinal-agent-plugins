"""Checks preview + the cardinal-behavior MCP server on Gemini CLI
(docs/plans/cross-agent-parity.md, Lane G).

- cardinal-connect merges a `cardinal-behavior` stdio server (--runtime
  gemini) into the extension's mcpServers next to `cardinal`, keeps other
  servers, and (no-extension install) writes a cardinalManaged entry into
  settings.json that cardinal-disconnect strips.
- The checks skill lives in the extension (~/.gemini/extensions/cardinal/
  skills/checks/): `cardinal-checks on` installs it there, a rebuilt
  extension keeps it, and SessionStart reconciles it and adds the routing
  rule while on and connected. No skill reload or skill-tool deny on Gemini
  CLI: a change takes effect at the next session.

Run: cd adapters/gemini && python3 -m unittest discover -s tests -p test_gemini_checks.py -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ADAPTER = Path(__file__).resolve().parent.parent
REPO = ADAPTER.parents[1]
ORG = "12345678-1234-1234-1234-123456789abc"
ROUTING = "Cardinal Checks preview is on"


def _ensure_vendored() -> None:
    if not (ADAPTER / "hooks" / "cardinal_core" / "checks_install.py").exists():
        subprocess.run([sys.executable, str(REPO / "build" / "vendor.py"), "gemini"], check=True, capture_output=True)


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class PluginCopy(unittest.TestCase):
    """A copy of the plugin and a scratch HOME (connect resolves ~ at import)."""

    @classmethod
    def setUpClass(cls):
        _ensure_vendored()

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        (self.home / ".gemini").mkdir(parents=True)
        self.plugin = Path(tmp.name) / "plugin"
        for part in ("hooks", "scripts", "checks", "extension", "skills", "agents", ".gemini-plugin"):
            shutil.copytree(ADAPTER / part, self.plugin / part, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO / "common" / "behavior", self.plugin / "lib" / "behavior",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old is not None else os.environ.pop("HOME"))
        self.connect = load(self.plugin / "scripts" / "cardinal-connect", f"gemini_connect_{id(self)}")
        self.ext = self.home / ".gemini" / "extensions" / "cardinal"

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
        env.update(HOME=str(self.home), CARDINAL_STORYBOARD_SESSION_START="0")
        return env

    def checks(self, *args):
        return subprocess.run([sys.executable, str(self.plugin / "scripts" / "cardinal-checks"), *args],
                              capture_output=True, text=True, env=self.env(), timeout=30)

    def manifest(self) -> dict:
        return json.loads((self.ext / "gemini-extension.json").read_text())


class RegistrationTests(PluginCopy):
    def test_extension_merges_the_behavior_server_and_drops_it_without_credentials(self):
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        servers = self.manifest()["mcpServers"]
        self.assertEqual(servers["cardinal-behavior"], {
            "command": "python3", "args": [str(self.plugin.resolve() / "scripts" / "cardinal-behavior"), "--runtime", "gemini"]})
        self.assertNotIn("ck_1", json.dumps(servers["cardinal-behavior"]))
        m = self.manifest()
        m["mcpServers"]["other"] = {"command": "/opt/other"}
        m["mcpServers"]["cardinal-behavior"] = {"command": "stale"}
        (self.ext / "gemini-extension.json").write_text(json.dumps(m))
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        servers = self.manifest()["mcpServers"]
        self.assertEqual(servers["other"], {"command": "/opt/other"})
        self.assertEqual(servers["cardinal-behavior"]["args"][-1], "gemini")
        self.connect.install_extension_bundle(None, None)
        self.assertEqual(self.manifest()["mcpServers"], {"other": {"command": "/opt/other"}})

    def test_no_extension_settings_entry_is_managed_and_stripped(self):
        settings = self.home / ".gemini" / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"mine": {"command": "x"}}}))
        self.connect.apply_settings(None, "https://c.example/mcp", "ck_1", include_hooks=True)
        servers = json.loads(settings.read_text())["mcpServers"]
        self.assertTrue(servers["cardinal-behavior"]["cardinalManaged"])
        self.assertEqual(servers["cardinal-behavior"]["args"][1:], ["--runtime", "gemini"])
        disconnect = load(self.plugin / "scripts" / "cardinal-disconnect", f"gemini_disconnect_{id(self)}")
        stripped, changed = disconnect.strip_managed_settings(json.loads(settings.read_text()))
        self.assertTrue(changed)
        self.assertEqual(stripped["mcpServers"], {"mine": {"command": "x"}})

    def test_repair_registers_behavior_on_a_connection_made_before_it(self):
        # The extension: its manifest has only `cardinal`.
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        manifest_path = self.ext / "gemini-extension.json"
        manifest = json.loads(manifest_path.read_text())
        del manifest["mcpServers"]["cardinal-behavior"]
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        self.assertEqual(self.connect.repair_hook_registration(), [str(self.ext)])
        servers = json.loads(manifest_path.read_text())["mcpServers"]
        self.assertEqual(servers["cardinal-behavior"]["args"][1:], ["--runtime", "gemini"])
        self.assertEqual(servers["cardinal"]["headers"], {"X-CardinalHQ-API-Key": "ck_1"})
        # --no-extension: settings.json.
        shutil.rmtree(self.ext)
        settings = self.home / ".gemini" / "settings.json"
        self.connect.apply_settings(None, "https://c.example/mcp", "ck_1", include_hooks=True)
        data = json.loads(settings.read_text())
        del data["mcpServers"]["cardinal-behavior"]
        settings.write_text(json.dumps(data))
        self.assertEqual(self.connect.repair_hook_registration(), [str(settings)])
        entry = json.loads(settings.read_text())["mcpServers"]["cardinal-behavior"]
        self.assertTrue(entry["cardinalManaged"])
        self.assertEqual(self.connect.repair_hook_registration(), [], "repair must be idempotent")

    def test_a_users_own_settings_behavior_entry_is_kept(self):
        settings = self.home / ".gemini" / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"cardinal-behavior": {"command": "mine"}}}))
        self.connect.apply_settings(None, "https://c.example/mcp", "ck_1", include_hooks=True)
        self.assertEqual(json.loads(settings.read_text())["mcpServers"]["cardinal-behavior"], {"command": "mine"})

    def test_the_registered_server_starts_for_gemini(self):
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        entry = self.manifest()["mcpServers"]["cardinal-behavior"]
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        result = subprocess.run([sys.executable, *entry["args"]], input="".join(json.dumps(r) + "\n" for r in requests),
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        init, tools = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(init["result"]["serverInfo"]["name"], "cardinal-behavior")
        names = [t["name"] for t in tools["result"]["tools"]]
        self.assertIn("get_behavior_range", names)
        # Gemini CLI's tool names are mcp_<server>_<tool>, truncated past 63 characters.
        self.assertTrue(all(len(f"mcp_cardinal-behavior_{n}") <= 63 for n in names))


class ChecksSkillTests(PluginCopy):
    def session_start(self):
        result = subprocess.run([sys.executable, str(self.plugin / "hooks" / "cardinal-gemini-telemetry.py"),
                                 "--event", "SessionStart"],
                                input=json.dumps({"session_id": "s1", "source": "startup", "cwd": str(self.home)}),
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.strip()
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def connect_files(self):
        (self.home / ".gemini" / "cardinal.json").write_text(
            json.dumps({"mcp_url": f"https://app.example/api/orgs/{ORG}/mcp"}))
        (self.home / ".gemini" / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "k"}))

    def test_on_needs_the_extension_then_installs_into_it(self):
        refused = self.checks("on")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("extension", refused.stderr)
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        on = self.checks("on")
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertIn("Start a new Gemini CLI session", on.stdout)
        skill = self.ext / "skills" / "checks" / "SKILL.md"
        self.assertEqual(skill.read_text(), (ADAPTER / "checks" / "skill" / "SKILL.md").read_text())
        self.assertFalse((self.plugin / "skills" / "checks").exists())
        # A rebuilt extension (connect --repair, an upgrade) keeps the opted-in skill.
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        self.assertTrue(skill.is_file())
        self.assertEqual(self.connect.repair_hook_registration(), [])
        self.assertEqual(self.checks("off").returncode, 0)
        self.assertFalse(skill.exists())

    def gate(self, name, tool_name="activate_skill"):
        result = subprocess.run([sys.executable, str(self.plugin / "hooks" / "cardinal-gemini-telemetry.py"),
                                 "--event", "BeforeTool", "--role", "checks-gate"],
                                input=json.dumps({"session_id": "s1", "hook_event_name": "BeforeTool",
                                                  "tool_name": tool_name, "tool_input": {"name": name},
                                                  "cwd": str(self.home)}),
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_checks_gate_denies_activate_skill_for_the_checks_skill_while_off(self):
        denied = self.gate("cardinal-checks")
        self.assertEqual(denied["decision"], "deny")
        self.assertIn("Checks preview is off", denied["reason"])
        self.assertIn("cardinal-checks on", denied["reason"])
        # Other skills, and other tools, pass untouched.
        self.assertIsNone(self.gate("cardinal-storyboard"))
        self.assertIsNone(self.gate("cardinal-checks", tool_name="run_shell_command"))

    def test_checks_gate_allows_the_checks_skill_while_on(self):
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        self.assertEqual(self.checks("on").returncode, 0)
        self.assertIsNone(self.gate("cardinal-checks"))
        self.assertEqual(self.checks("off").returncode, 0)
        self.assertEqual(self.gate("cardinal-checks")["decision"], "deny")

    def test_checks_gate_is_registered_in_hooks_json_and_hook_groups(self):
        group = ("BeforeTool", "^activate_skill$", "checks-gate", self.connect.HOOK_TIMEOUT_MS)
        self.assertIn(group, self.connect.HOOK_GROUPS)
        source = json.loads((ADAPTER / "extension" / "hooks" / "hooks.json").read_text())
        commands = {g["matcher"]: g["hooks"][0]["command"] for g in source["hooks"]["BeforeTool"]}
        self.assertEqual(commands["^activate_skill$"],
                         "python3 __HOOK_SCRIPT__ --event BeforeTool --role checks-gate # cardinal-gemini-plugin")
        self.assertEqual(len(set(commands.values())), len(commands), "Gemini dedupes hooks by name:command")

    def test_session_start_reconciles_and_routes_only_while_on_and_connected(self):
        self.connect_files()
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        self.assertNotIn(ROUTING, self.session_start())
        self.checks("on")
        shutil.rmtree(self.ext / "skills" / "checks")
        self.assertIn(ROUTING, self.session_start())
        self.assertTrue((self.ext / "skills" / "checks" / "SKILL.md").is_file())
        (self.home / ".gemini" / "cardinal" / "checks" / "enabled.json").write_text('{"enabled": false}')
        self.assertNotIn(ROUTING, self.session_start())
        self.assertFalse((self.ext / "skills" / "checks").exists())

    def test_skill_names_gemini_tools_and_polling(self):
        skill = (ADAPTER / "checks" / "skill" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: cardinal-checks\n"))
        self.assertIn("mcp_cardinal-behavior_", skill)
        self.assertIn("get_behavior_range", skill)
        self.assertIn("next Gemini CLI session", skill)
        self.assertIn("the user's label approval\n  (step 7) is still required", skill)
        self.assertNotIn("without any shell command or approval", skill)
        self.assertNotIn("Monitor", skill)
        self.assertNotIn("__CARDINAL_PLUGIN_ROOT__", skill)


if __name__ == "__main__":
    unittest.main()

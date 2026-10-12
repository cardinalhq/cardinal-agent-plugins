"""Checks preview + the cardinal-behavior MCP server on Codex
(docs/plans/cross-agent-parity.md, Lane G).

- cardinal-connect registers [mcp_servers.cardinal-behavior] (a stdio server
  run through a stable launcher with --runtime codex); disconnect removes it.
- scripts/cardinal-behavior and scripts/cardinal-checks are thin launchers
  over common/behavior (lib/behavior in a release) and cardinal_core.checks_cli.
- SessionStart reconciles the installed skill with `cardinal-checks on|off`
  and adds the routing rule while on and connected. No skill reload, no
  Skill-tool deny on Codex: a change takes effect at the next session.

Run: cd adapters/codex && python3 -m unittest discover -s tests -p test_codex_checks.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_parity import CONNECT, DISCONNECT, StubCardinal, read_toml, run_script  # noqa: E402

ADAPTER = Path(__file__).resolve().parent.parent
REPO = ADAPTER.parent.parent
ORG = "12345678-1234-1234-1234-123456789abc"
ROUTING = "Cardinal Checks preview is on"


def mcp(command: list, home: Path, *requests: dict) -> list:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
    env["HOME"] = str(home)
    result = subprocess.run(command, input="".join(json.dumps(r) + "\n" for r in requests),
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


class ConnectRegistersBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubCardinal()
        self.stub.start()
        self.addCleanup(self.stub.stop)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.config = self.home / ".codex" / "config.toml"
        self.launcher = self.home / ".codex" / "cardinal" / "cardinal-behavior.py"

    def connect(self, *extra):
        result = run_script(CONNECT, ["--host", self.stub.url(), *extra], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def test_connect_writes_a_managed_stdio_entry_and_disconnect_removes_it(self):
        self.connect()
        entry = read_toml(self.config)["mcp_servers"]["cardinal-behavior"]
        self.assertEqual(entry, {"command": "python3", "args": [str(self.launcher), "--runtime", "codex"]})
        self.assertNotIn("MCPPLAINTEXT", json.dumps(entry))
        self.assertTrue(os.access(self.launcher, os.X_OK))
        # Inside the managed span, so rotate / disconnect handle it with the cardinal table.
        text = self.config.read_text()
        begin, end = text.index("# BEGIN cardinal-codex-plugin"), text.index("# END cardinal-codex-plugin")
        self.assertTrue(begin < text.index("[mcp_servers.cardinal-behavior]") < end)
        rotated = self.connect("--rotate")
        self.assertIn("behavior MCP launcher", rotated.stdout)
        self.assertEqual(self.config.read_text().count("[mcp_servers.cardinal-behavior]"), 1)
        result = run_script(DISCONNECT, [], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("cardinal-behavior", self.config.read_text())
        self.assertFalse(self.launcher.exists())

    def test_the_registered_command_starts_the_server_for_codex(self):
        self.connect()
        entry = read_toml(self.config)["mcp_servers"]["cardinal-behavior"]
        init, tools = mcp([sys.executable, *entry["args"]], self.home,
                          {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                          {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "cardinal-behavior")
        names = {t["name"] for t in tools["result"]["tools"]}
        self.assertTrue({"set_check_example", "lock_check_examples", "get_behavior_range",
                         "execute_behavior_range"} <= names)

    def test_an_unmanaged_behavior_table_is_left_alone(self):
        self.config.parent.mkdir(parents=True)
        mine = '[mcp_servers.cardinal-behavior]\ncommand = "/opt/mine"\n'
        self.config.write_text(mine)
        result = self.connect()
        self.assertIn("unmanaged [mcp_servers.cardinal-behavior]", result.stdout)
        config = read_toml(self.config)
        self.assertEqual(config["mcp_servers"]["cardinal-behavior"], {"command": "/opt/mine"})
        self.assertIn("cardinal", config["mcp_servers"])
        self.assertEqual(self.config.read_text().count("[mcp_servers.cardinal-behavior]"), 1)

    def test_a_users_behavior_table_outside_the_markers_survives_rotate_and_disconnect(self):
        mine = '[mcp_servers.cardinal-behavior]\ncommand = "/opt/mine"\n'
        user_entry = {"command": "/opt/mine"}
        # Before the managed block (the file it was connected into) ...
        self.config.parent.mkdir(parents=True)
        self.config.write_text(mine)
        self.connect()
        self.assertEqual(read_toml(self.config)["mcp_servers"]["cardinal-behavior"], user_entry)
        for _ in range(2):
            self.connect("--rotate")
            self.assertEqual(read_toml(self.config)["mcp_servers"]["cardinal-behavior"], user_entry)
            self.assertEqual(self.config.read_text().count("[mcp_servers.cardinal-behavior]"), 1)
        result = run_script(DISCONNECT, [], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(read_toml(self.config), {"mcp_servers": {"cardinal-behavior": user_entry}})

    def test_a_users_behavior_table_added_after_the_managed_block_survives(self):
        self.connect()
        # The user replaces the managed entry with their own, after the END marker.
        text = self.config.read_text()
        begin = text.index("[mcp_servers.cardinal-behavior]")
        end = text.index("# END cardinal-codex-plugin")
        text = text[:begin] + text[end:] + '\n[mcp_servers.cardinal-behavior]\ncommand = "/opt/mine"\n'
        self.config.write_text(text)
        self.connect("--rotate")
        config = read_toml(self.config)
        self.assertEqual(config["mcp_servers"]["cardinal-behavior"], {"command": "/opt/mine"})
        self.assertEqual(self.config.read_text().count("[mcp_servers.cardinal-behavior]"), 1)
        result = run_script(DISCONNECT, [], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(read_toml(self.config), {"mcp_servers": {"cardinal-behavior": {"command": "/opt/mine"}}})

    def test_repair_hooks_registers_behavior_on_a_connection_made_before_it(self):
        self.connect()
        # A connection from before the Checks preview: the managed block holds
        # only [mcp_servers.cardinal].
        text = self.config.read_text()
        start = text.index("[mcp_servers.cardinal-behavior]")
        end = text.index("# END cardinal-codex-plugin")
        old = 'model = "o3"\n\n' + text[:start].rstrip("\n") + "\n" + text[end:] + '\n[desktop]\nx = 1\n'
        self.config.write_text(old)
        self.assertNotIn("cardinal-behavior", read_toml(self.config)["mcp_servers"])
        result = run_script(CONNECT, ["--repair-hooks"], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Registered the cardinal-behavior MCP server", result.stdout)
        config = read_toml(self.config)
        self.assertEqual(config["mcp_servers"]["cardinal-behavior"],
                         {"command": "python3", "args": [str(self.launcher), "--runtime", "codex"]})
        self.assertIn("X-CardinalHQ-API-Key", config["mcp_servers"]["cardinal"]["http_headers"])
        self.assertEqual((config["model"], config["desktop"]), ("o3", {"x": 1}))
        text = self.config.read_text()
        self.assertTrue(text.index("# BEGIN cardinal-codex-plugin") < text.index("[mcp_servers.cardinal-behavior]")
                        < text.index("# END cardinal-codex-plugin"))
        # Idempotent; disconnect still removes it.
        again = run_script(CONNECT, ["--repair-hooks"], self.home)
        self.assertNotIn("Registered the cardinal-behavior", again.stdout)
        self.assertEqual(self.config.read_text(), text)
        run_script(DISCONNECT, [], self.home)
        self.assertNotIn("cardinal-behavior", self.config.read_text())

    def test_repair_hooks_without_a_managed_block_says_so(self):
        result = run_script(CONNECT, ["--repair-hooks"], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("No managed Cardinal MCP block", result.stdout)
        self.assertFalse(self.config.exists())

    def test_repair_hooks_restores_the_behavior_launcher(self):
        self.connect()
        self.launcher.unlink()
        result = run_script(CONNECT, ["--repair-hooks"], self.home)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(self.launcher.is_file())
        self.assertIn('Path("scripts") / "cardinal-behavior"', self.launcher.read_text())


class LaunchersAndSessionStartTests(unittest.TestCase):
    """A copy of the plugin, so installing the skill never touches the checkout."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.plugin = Path(tmp.name) / "plugin"
        for part in ("hooks", "scripts", "checks", ".codex-plugin"):
            shutil.copytree(ADAPTER / part, self.plugin / part, ignore=shutil.ignore_patterns("__pycache__"))
        (self.plugin / "skills").mkdir()
        shutil.copytree(REPO / "common" / "behavior", self.plugin / "lib" / "behavior",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.installed = self.plugin / "skills" / "checks"

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
        env.update(HOME=str(self.home), CARDINAL_STORYBOARD_SESSION_START="0")
        return env

    def checks(self, *args):
        return subprocess.run([sys.executable, str(self.plugin / "scripts" / "cardinal-checks"), *args],
                              capture_output=True, text=True, env=self.env(), timeout=30)

    def session_start(self):
        result = subprocess.run([sys.executable, str(self.plugin / "hooks" / "cardinal-codex-telemetry.py"),
                                 "--event", "SessionStart"],
                                input=json.dumps({"session_id": "s1", "source": "startup", "cwd": str(self.home)}),
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return ""
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def connect_files(self):
        agent = self.home / ".codex"
        agent.mkdir(exist_ok=True)
        (agent / "cardinal.json").write_text(json.dumps({"mcp_url": f"https://app.example/api/orgs/{ORG}/mcp"}))
        (agent / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "k"}))

    def test_cardinal_checks_on_off_installs_into_the_plugin_skills(self):
        on = self.checks("on")
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertIn("Start a new Codex session", on.stdout)
        self.assertEqual((self.installed / "SKILL.md").read_text(),
                         (ADAPTER / "checks" / "skill" / "SKILL.md").read_text())
        self.assertEqual((self.installed / "CORE.md").read_text(), (REPO / "common" / "checks" / "CORE.md").read_text())
        self.assertTrue((self.home / ".codex" / "cardinal" / "checks" / "enabled.json").is_file())
        off = self.checks("off")
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertFalse(self.installed.exists())

    def test_on_without_the_behavior_registration_prints_the_repair_command(self):
        self.connect_files()   # connected, but config.toml has no cardinal-behavior table
        on = self.checks("on")
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertIn("not registered for Codex", on.stdout)
        self.assertIn(f"python3 {self.plugin.resolve() / 'scripts' / 'cardinal-connect'} --repair-hooks", on.stdout)

    def test_disconnect_removes_the_installed_skill_and_the_setting(self):
        self.connect_files()
        self.assertEqual(self.checks("on").returncode, 0)
        self.assertTrue((self.installed / "SKILL.md").is_file())
        result = subprocess.run([sys.executable, str(self.plugin / "scripts" / "cardinal-disconnect")],
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Removed the Checks preview skill", result.stdout)
        self.assertFalse(self.installed.exists())
        self.assertFalse((self.home / ".codex" / "cardinal" / "checks").exists())

    def test_disconnect_keeps_a_skills_checks_it_did_not_install(self):
        self.connect_files()
        (self.installed).mkdir()
        (self.installed / "SKILL.md").write_text("mine\n")
        result = subprocess.run([sys.executable, str(self.plugin / "scripts" / "cardinal-disconnect")],
                                capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.installed / "SKILL.md").read_text(), "mine\n")

    def test_session_start_reconciles_and_adds_the_rule_only_when_on_and_connected(self):
        self.connect_files()
        self.assertNotIn(ROUTING, self.session_start())
        self.checks("on")
        shutil.rmtree(self.installed)   # a plugin update: a fresh plugin dir without the copy
        self.assertIn(ROUTING, self.session_start())
        self.assertTrue((self.installed / "SKILL.md").is_file())
        (self.home / ".codex" / "cardinal" / "checks" / "enabled.json").write_text('{"enabled": false}')
        self.assertNotIn(ROUTING, self.session_start())
        self.assertFalse(self.installed.exists())

    def test_session_start_unconnected_installs_but_adds_no_rule(self):
        self.checks("on")
        shutil.rmtree(self.installed)
        self.assertNotIn(ROUTING, self.session_start())
        self.assertTrue((self.installed / "SKILL.md").is_file())

    def test_behavior_launcher_defaults_to_codex_and_uses_the_codex_connection(self):
        self.connect_files()
        launcher = self.plugin / "scripts" / "cardinal-behavior"
        (call,) = mcp([sys.executable, str(launcher)], self.home,
                      {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                          "name": "set_check_example", "arguments": {
                              "draft": "d1", "trace_id": "a" * 32, "evidence": "sufficient",
                              "proposed": "MATCH", "reason": "r"}}})
        self.assertNotIn("isError", call["result"])
        self.assertTrue((self.home / ".codex" / "cardinal" / "checks" / "drafts" / "d1.json").is_file())
        help_out = subprocess.run([sys.executable, str(launcher), "--help"], capture_output=True, text=True,
                                  env=self.env(), timeout=30)
        self.assertEqual(help_out.returncode, 0, help_out.stderr)
        self.assertIn("--runtime", help_out.stdout)


class SkillTemplateTests(unittest.TestCase):
    def test_skill_names_codex_tools_and_polling(self):
        skill = (ADAPTER / "checks" / "skill" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: cardinal-checks\n"))
        self.assertIn("mcp__cardinal-behavior__", skill)
        self.assertIn("get_behavior_range", skill)
        self.assertIn("next Codex session", skill)
        self.assertIn("the user's label approval\n  (step 7) is still required", skill)
        self.assertNotIn("without any shell command or approval", skill)
        self.assertNotIn("Monitor", skill)
        self.assertNotIn("mcp__plugin_cardinal", skill)
        tracked = subprocess.run(["git", "ls-files", "adapters/codex/skills/checks"], cwd=REPO,
                                 capture_output=True, text=True).stdout
        self.assertEqual(tracked.strip(), "")


if __name__ == "__main__":
    unittest.main()

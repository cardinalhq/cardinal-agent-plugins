"""The Copilot plugin's skills and agents.

Copilot CLI plugin layout (.plugin/plugin.json "skills": "skills", "agents":
"agents"): skills/<dir>/SKILL.md with YAML frontmatter `name` (lowercase,
hyphens) and `description`, and agents/<name>.agent.md custom agents
(frontmatter `name`, `description`, optional `tools`).

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_skills.py -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from fixtures import ADAPTER, REPO_ROOT

SKILLS = ADAPTER / "skills"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CLAUDE_TOOL_NAMES = ("Read", "Grep", "Glob", "Bash", "Write", "Edit")


def frontmatter(path: Path) -> tuple:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, f"{path} has no frontmatter"
    meta = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, m.group(2)


class SkillTests(unittest.TestCase):
    def skill_dirs(self) -> list:
        return sorted(p for p in SKILLS.iterdir() if p.is_dir() and p.name != "checks")

    def test_every_skill_has_a_kebab_name_and_description(self):
        dirs = self.skill_dirs()
        self.assertTrue(dirs)
        names = []
        for d in dirs:
            meta, body = frontmatter(d / "SKILL.md")
            self.assertRegex(meta.get("name", ""), NAME_RE, d)
            self.assertTrue(meta.get("description"), d)
            self.assertLessEqual(len(meta["description"]), 1024, d)
            self.assertTrue(body.strip(), d)
            names.append(meta["name"])
        self.assertEqual(len(names), len(set(names)), names)

    def test_expected_skills_exist(self):
        for name in ("connect", "disconnect", "status", "decision", "canvas", "storyboard", "supervise",
                     "install-site", "migrate-from-grafana", "optimize-toolkit"):
            self.assertTrue((SKILLS / name / "SKILL.md").is_file(), name)

    def test_script_skills_name_existing_scripts_and_the_sandbox(self):
        for skill, script in (("connect", "cardinal-connect"), ("disconnect", "cardinal-disconnect"),
                              ("status", "cardinal-status"), ("decision", "cardinal-decision")):
            with self.subTest(skill=skill):
                body = frontmatter(SKILLS / skill / "SKILL.md")[1]
                self.assertIn(f"scripts/{script}", body)
                self.assertTrue((ADAPTER / "scripts" / script).is_file(), script)

    def test_connect_skill_leaves_owner_input_consent_to_the_user(self):
        body = frontmatter(SKILLS / "connect" / "SKILL.md")[1]
        self.assertIn("--owner-input on", body)
        self.assertIn("Never run `--owner-input on` yourself", body)
        self.assertIn("CARDINAL_OWNER_INPUT=0", body)
        self.assertIn("sandbox", body)

    def test_decision_skill_is_status_only(self):
        body = frontmatter(SKILLS / "decision" / "SKILL.md")[1]
        self.assertIn("cardinal-decision status", body)

    def test_skills_name_copilot_paths_not_other_agents(self):
        for d in self.skill_dirs():
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            with self.subTest(skill=d.name):
                for other in ("~/.claude", "~/.cursor", "~/.codex", "~/.gemini", "CLAUDE_PLUGIN_ROOT"):
                    self.assertNotIn(other, text)

    def test_canvas_skill_is_thin_and_the_shared_skills_are_synced(self):
        meta, body = frontmatter(SKILLS / "canvas" / "SKILL.md")
        self.assertEqual(meta["name"], "canvas")
        self.assertIn("CORE.md", body)
        self.assertIn("--runtime copilot", body)
        self.assertIn("~/.copilot/cardinal/storyboards/", body)
        self.assertTrue((SKILLS / "canvas" / "CORE.md").is_file())
        self.assertTrue((SKILLS / "canvas" / "exemplars").is_dir())
        self.assertTrue((SKILLS / "storyboard" / "scripts" / "render_preview.py").is_file())
        res = subprocess.run([sys.executable, str(REPO_ROOT / "build" / "sync_skills.py"), "--check", "copilot"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


class AgentTests(unittest.TestCase):
    def test_brainstorm_agent_mirrors_claudes(self):
        meta, body = frontmatter(ADAPTER / "agents" / "brainstorm.agent.md")
        claude_meta, claude_body = frontmatter(REPO_ROOT / "adapters" / "claude" / "agents" / "brainstorm.md")
        self.assertEqual(meta["name"], "brainstorm")
        self.assertRegex(meta["name"], NAME_RE)
        self.assertEqual(meta["description"], claude_meta["description"])
        self.assertEqual(body, claude_body)
        # Copilot's own read-only tool names, not Claude Code's.
        tools = json.loads(meta["tools"])
        self.assertEqual(tools, ["view", "grep", "glob"])
        for name in CLAUDE_TOOL_NAMES:
            self.assertNotIn(name, tools)
        self.assertNotIn("model", meta)

    def test_every_agent_file_uses_the_agent_md_suffix(self):
        files = sorted((ADAPTER / "agents").iterdir())
        self.assertTrue(files)
        for f in files:
            self.assertTrue(f.name.endswith(".agent.md"), f.name)

    def test_manifest_ships_skills_and_agents(self):
        manifest = json.loads((ADAPTER / ".plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["skills"], "skills")
        self.assertEqual(manifest["agents"], "agents")


if __name__ == "__main__":
    unittest.main()

"""The Cursor plugin's skills and agents (docs/plans/cross-agent-parity.md,
Lane D): connect / disconnect / status / decision / canvas SKILL.md files
over the existing scripts, and the brainstorm subagent shipped in the
plugin's agents/ directory.

Cursor plugin layout: skills/<name>/SKILL.md and agents/*.md, each with
YAML frontmatter `name` (lowercase kebab-case) and `description`
(cursor.com/docs/reference/plugins). Subagent frontmatter also takes
`model` (`inherit` or a model id) and `readonly` (cursor.com/docs/agent/subagents).

Run: cd adapters/cursor && python3 -m unittest discover tests -p test_cursor_skills.py -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent.parent
REPO_ROOT = ADAPTER.parent.parent
SKILLS = ADAPTER / "skills"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


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
    def test_every_skill_has_kebab_name_and_description(self):
        dirs = sorted(p for p in SKILLS.iterdir() if p.is_dir())
        self.assertTrue(dirs)
        for d in dirs:
            meta, body = frontmatter(d / "SKILL.md")
            self.assertRegex(meta.get("name", ""), NAME_RE, d)
            self.assertTrue(meta.get("description"), d)
            self.assertTrue(body.strip(), d)

    def test_new_skills_exist(self):
        for name in ("connect", "disconnect", "status", "decision", "canvas"):
            self.assertTrue((SKILLS / name / "SKILL.md").is_file(), name)

    def test_script_skills_name_existing_scripts_and_the_sandbox(self):
        for skill, script in (("connect", "cardinal-connect"), ("disconnect", "cardinal-disconnect"),
                              ("status", "cardinal-status"), ("decision", "cardinal-decision")):
            body = frontmatter(SKILLS / skill / "SKILL.md")[1]
            self.assertIn(f"scripts/{script}", body)
            self.assertTrue((ADAPTER / "scripts" / script).is_file(), script)
            self.assertIn("Auto-review", body, skill)
            self.assertIn("sandbox", body, skill)

    def test_connect_skill_leaves_owner_input_consent_to_the_user(self):
        body = frontmatter(SKILLS / "connect" / "SKILL.md")[1]
        self.assertIn("--owner-input on", body)
        self.assertIn("Never run `--owner-input on` yourself", body)
        self.assertIn("CARDINAL_OWNER_INPUT=0", body)

    def test_decision_skill_is_status_only(self):
        body = frontmatter(SKILLS / "decision" / "SKILL.md")[1]
        self.assertIn("cardinal-decision status", body)
        self.assertIn("Do not run `on` or `off` yourself", body)

    def test_canvas_skill_is_thin_and_its_core_is_synced(self):
        meta, body = frontmatter(SKILLS / "canvas" / "SKILL.md")
        self.assertEqual(meta["name"], "canvas")
        self.assertIn("CORE.md", body)
        self.assertIn("--runtime cursor", body)
        self.assertIn("~/.cursor/cardinal/storyboards/", body)
        self.assertNotIn("~/.claude", body)
        self.assertTrue((SKILLS / "canvas" / "CORE.md").is_file())
        self.assertTrue((SKILLS / "canvas" / "exemplars").is_dir())
        self.assertTrue((SKILLS / "storyboard" / "scripts" / "render_preview.py").is_file())
        res = subprocess.run([sys.executable, str(REPO_ROOT / "build" / "sync_skills.py"), "--check", "cursor"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


class AgentTests(unittest.TestCase):
    def test_brainstorm_agent_mirrors_claudes(self):
        cursor_meta, cursor_body = frontmatter(ADAPTER / "agents" / "brainstorm.md")
        claude_meta, claude_body = frontmatter(REPO_ROOT / "adapters" / "claude" / "agents" / "brainstorm.md")
        self.assertEqual(cursor_meta["name"], "brainstorm")
        self.assertRegex(cursor_meta["name"], NAME_RE)
        self.assertEqual(cursor_meta["description"], claude_meta["description"])
        self.assertEqual(cursor_body, claude_body)
        # Cursor takes `inherit` or a Cursor model id (Claude's alias means nothing there).
        self.assertEqual(cursor_meta["model"], "inherit")
        self.assertEqual(cursor_meta["readonly"], "true")

    def test_manifest_ships_skills_and_agents(self):
        manifest = json.loads((ADAPTER / ".cursor-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["agents"], "./agents/")


if __name__ == "__main__":
    unittest.main()

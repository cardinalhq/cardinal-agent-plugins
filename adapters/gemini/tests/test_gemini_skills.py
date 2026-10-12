"""The skills and agents the Cardinal extension ships to Gemini CLI.

gemini-cli 0.50.0 loads an extension's skills from <ext>/skills/*/SKILL.md
(frontmatter `name` and `description` required: skillLoader.loadSkillFromFile)
and its agents from <ext>/agents/*.md (agentLoader: frontmatter validated by a
STRICT schema — unknown keys fail the agent). cardinal-connect copies both
into ~/.gemini/extensions/cardinal/ (tests/test_connect.py).

Run: cd adapters/gemini && python3 -m unittest tests.test_gemini_skills -v
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent.parent
REPO_ROOT = ADAPTER.parents[1]
SKILLS = ADAPTER / "skills"
AGENTS = ADAPTER / "agents"

# localAgentSchema keys (agentLoader, chunk-W4YMYD53.js: .strict()).
AGENT_KEYS = {"kind", "name", "description", "display_name", "tools", "mcp_servers", "model", "temperature",
              "max_turns", "timeout_mins"}
# Read-only built-in tool names of gemini-cli 0.50.0 (tool-names constants).
READ_ONLY_TOOLS = {"read_file", "read_many_files", "list_directory", "glob", "grep_search"}


def frontmatter(path: Path) -> tuple:
    """({key: value or [items]}, body) of a `---` YAML-ish frontmatter
    (scalars and one level of `- item` lists: all these files use)."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n([\s\S]*))?", text)
    if not m:
        return {}, text
    data: dict = {}
    key = None
    for line in m.group(1).splitlines():
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and key:
            data.setdefault(key, [])
            data[key].append(item.group(1).strip())
            continue
        kv = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if kv:
            key = kv.group(1)
            data[key] = kv.group(2).strip() or []
    return data, m.group(2) or ""


class SkillTests(unittest.TestCase):
    def test_every_skill_has_name_and_description(self):
        dirs = sorted(p for p in SKILLS.iterdir() if p.is_dir())
        self.assertTrue(dirs)
        for d in dirs:
            with self.subTest(skill=d.name):
                fm, _ = frontmatter(d / "SKILL.md")
                self.assertIsInstance(fm.get("name"), str)
                self.assertTrue(fm["name"])
                self.assertIsInstance(fm.get("description"), str)
                self.assertTrue(fm["description"])

    def test_skills_name_the_plugin_scripts_absolutely(self):
        # A skill runs wherever Gemini runs, not in the plugin: script paths
        # use the placeholder cardinal-connect replaces at install.
        for md in SKILLS.rglob("SKILL.md"):
            with self.subTest(skill=md.parent.name):
                self.assertNotRegex(md.read_text(), r"python3 scripts/")

    def test_canvas_is_thin_over_the_synced_core(self):
        skill = (SKILLS / "canvas" / "SKILL.md").read_text()
        self.assertIn("[CORE.md](CORE.md)", skill)
        self.assertIn("~/.gemini/cardinal/storyboards/", skill)
        self.assertIn("--runtime gemini", skill)
        self.assertNotIn("~/.claude", skill)
        self.assertEqual((SKILLS / "canvas" / "CORE.md").read_bytes(),
                         (REPO_ROOT / "common" / "canvas" / "CORE.md").read_bytes())
        check = subprocess.run([sys.executable, str(REPO_ROOT / "build" / "sync_skills.py"), "--check", "gemini"],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_decision_is_status_only(self):
        fm, body = frontmatter(SKILLS / "decision" / "SKILL.md")
        self.assertEqual(fm["name"], "cardinal-decision")
        self.assertIn('scripts/cardinal-decision" status', body)
        self.assertIn("Do not run `on` or `off` yourself", body)
        self.assertIn("~/.gemini/cardinal", body)


class AgentTests(unittest.TestCase):
    def test_brainstorm_is_a_valid_gemini_local_agent(self):
        fm, body = frontmatter(AGENTS / "brainstorm.md")
        self.assertLessEqual(set(fm), AGENT_KEYS, "Gemini's agent schema is strict")
        self.assertEqual(fm["name"], "brainstorm")
        self.assertRegex(fm["name"], r"^[a-z0-9-_]+$")
        self.assertEqual(fm.get("kind"), "local")
        self.assertTrue(fm["description"])
        self.assertNotIn("model", fm, "inherit the session's Gemini model")
        self.assertTrue(set(fm["tools"]) <= READ_ONLY_TOOLS, fm["tools"])
        self.assertIn("Recommendation", body)

    def test_brainstorm_matches_the_claude_agent(self):
        claude = (REPO_ROOT / "adapters" / "claude" / "agents" / "brainstorm.md")
        _, claude_body = frontmatter(claude)
        _, body = frontmatter(AGENTS / "brainstorm.md")
        self.assertEqual(body.strip(), claude_body.strip())
        self.assertEqual(frontmatter(AGENTS / "brainstorm.md")[0]["description"],
                         frontmatter(claude)[0]["description"])


if __name__ == "__main__":
    unittest.main()

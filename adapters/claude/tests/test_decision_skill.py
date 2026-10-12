"""The decision skill (skills/decision/SKILL.md): routes "is decision capture
on?" / "turn it on|off" to bin/cardinal-decision, which Claude can run
itself (unlike Codex, whose sandbox blocks the toggle).

The skill quotes the CLI's output lines, so these tests run the real CLI
under a temp HOME and check that what the skill quotes is what it prints.
Requires cardinal_core vendored: python3 build/vendor.py claude

Run with: python3 -m unittest tests.test_decision_skill -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SKILL = PLUGIN_ROOT / "skills" / "decision" / "SKILL.md"
CLI = PLUGIN_ROOT / "bin" / "cardinal-decision"
HOOK = PLUGIN_ROOT / "hooks" / "decision-prompt.py"


def frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must start with YAML frontmatter"
    out = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def _flat(text: str) -> str:
    return " ".join(text.split())


class DecisionSkillTextTests(unittest.TestCase):
    def setUp(self):
        self.raw = SKILL.read_text()
        self.meta = frontmatter(self.raw)
        self.text = _flat(self.raw)

    def test_frontmatter_is_model_invoked_and_parses(self):
        self.assertEqual(self.meta["name"], "decision")
        self.assertNotIn("disable-model-invocation", self.meta)
        self.assertNotIn("user-invocable", self.meta)
        # A plain YAML scalar may not contain ": " or " #"; Claude Code drops a
        # skill whose frontmatter fails to parse from the model's listing.
        for value in self.meta.values():
            self.assertNotIn(": ", value)
            self.assertNotIn(" #", value)

    def test_description_triggers_only_on_decision_capture(self):
        description = self.meta["description"]
        self.assertLessEqual(len(description), 300)
        for needle in ("Cardinal decision capture", "on or off", "cardinal-decision",
                       "Use only when the user asks about Cardinal decision capture",
                       "not for making or recording a decision"):
            self.assertIn(needle, description)

    def test_routes_status_and_the_toggle_to_the_cli(self):
        for needle in (
            "cardinal-decision status",
            "`cardinal-decision on` or `cardinal-decision off`",
            "Run `status` afterwards and report the effective state.",
            "run `status`. Do not toggle it.",
            "Never run `on` or `off` unless the user asked for it in this conversation.",
            "`/cardinal:decision on|off`",
        ):
            self.assertIn(needle, self.text)
        # Unlike the Codex skill, Claude runs the toggle itself.
        self.assertNotIn("Do not run `on` or `off` yourself", self.text)

    def test_fallback_path_reaches_the_cli(self):
        self.assertIn('"<this skill\'s base directory>/../../bin/cardinal-decision"', self.text)
        self.assertTrue((SKILL.parent / ".." / ".." / "bin" / "cardinal-decision").resolve().is_file())

    def test_explains_the_override_and_the_connection(self):
        for needle in (
            "`(CARDINAL_DECISIONS=1)` means that variable overrides the toggle",
            "While it is set, `on` and `off` have no effect.",
            "Do not edit it yourself.",
            "`Cardinal telemetry: not connected`",
            "never sent to Cardinal",
            "`/cardinal:connect`",
            "`--session <session id>`",
        ):
            self.assertIn(needle, self.text)

    def test_recording_stays_with_the_prompt_hook(self):
        self.assertIn("`cardinal-decision record --session …`", self.text)
        self.assertIn("You do not need this skill to record a decision.", self.text)


class DecisionSkillMatchesTheCliTests(unittest.TestCase):
    """What the skill quotes is what bin/cardinal-decision prints."""

    def setUp(self):
        if not (PLUGIN_ROOT / "hooks" / "cardinal_core" / "decisions.py").exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        (self.home / ".claude").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args: str, **env: str) -> str:
        environ = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CARDINAL_", "CLAUDE_", "OTEL_"))}
        environ.update(HOME=str(self.home), **env)
        out = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                             env=environ, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_status_on_off_lines(self):
        status = self.cli("status")
        self.assertIn("Decision capture: off", status)
        self.assertIn("Cardinal telemetry: not connected", status)
        self.assertIn("Decision capture is on.", self.cli("on"))
        self.assertIn("Decision capture: on", self.cli("status"))
        self.assertIn("Decision capture is off.", self.cli("off"))
        self.assertIn("Decision capture: off", self.cli("status"))

    def test_override_suffix_beats_the_toggle(self):
        self.cli("off")
        status = self.cli("status", CARDINAL_DECISIONS="1")
        self.assertIn("Decision capture: on (CARDINAL_DECISIONS=1)", status)

    def test_session_flag_lists_the_ledger(self):
        self.assertIn("Decisions in session s-1:", self.cli("status", "--session", "s-1"))

    def test_hook_supplies_the_record_command(self):
        # The skill says the prompt hook hands over `record --session …`.
        self.assertIn('record --session', HOOK.read_text())


if __name__ == "__main__":
    unittest.main()

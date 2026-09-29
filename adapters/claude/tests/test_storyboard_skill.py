"""Storyboard skills: what they must carry, and what they must not.

Cardinal serves the storyboard craft itself (conductor #1975:
storyboard__describe_grammar sections authoring, evidence and canvas), so every
Claude client authors the same way. The plugin skills point at those guides
and keep only what is specific to Claude Code with the Cardinal plugin: the
session id hook, captured evidence and `cardinal-evidence promote`, and the
local Chromium preview loop. The evidence contract from the design doc (conductor
docs/specs/investigation-storyboards.md, section 9) stays verbatim.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STORYBOARD_SKILL = PLUGIN_ROOT / "skills" / "storyboard" / "SKILL.md"
CANVAS_SKILL = PLUGIN_ROOT / "skills" / "canvas" / "SKILL.md"

EVIDENCE_CONTRACT = (
    "> Rendered pixels may suggest a hypothesis but never establish a factual claim. Any quantitative or "
    "population-level statement must resolve to evidence from a receipt or deterministic derivation over receipts.",
    "> Preview every scene before publishing. Inspect whether the intended point is visually obvious without "
    "reading the investigation transcript. Revise the presentation when it is not.",
)

# conductor packages/maestro/src/storyboard/grammar.ts GUIDE_SECTIONS (#1975):
# the guides storyboard__describe_grammar serves by name. The skills point at
# them instead of restating them.
GUIDE_SECTIONS = ("authoring", "evidence", "canvas")

# Text the server's guides own (grammar.ts AUTHORING_GUIDE, EVIDENCE_GUIDE,
# CANVAS_DESIGN, RULES_OUT_DIRECTION). A copy in a skill drifts from the server,
# so none of it may come back.
SERVER_OWNED = (
    # canvas design doctrine (CANVAS_DESIGN.doctrine / the design doc, section 9)
    "Act like an exceptional UI and information designer",
    "latency on the path, failure on the component",
    "Prefer visualizing the conclusion or reasoning over merely visualizing the raw telemetry",
    "## Canvas design doctrine",
    # reveal-step rules and rules that bite
    "Every reveal step is a complete, correct picture",
    "No numbers in source",
    "Past a few thousand marks, draw on a `<canvas>`, not in SVG",
    # authoring guide
    "Titles are findings, not topics",
    "ruled out as a cause of",
    "Declare `unit` in a usual spelling",
    "{select: {receiptId",
    "`reduce.where` takes a literal",
    "prose_number_unreconciled",
    "sparse_groups_at_timestamp",
    "Can the reader get from each important claim to the evidence behind it?",
    "latest_only: true",
    # evidence guide
    "model_result_omitted",
    '{receiptId, selector: "/error/message"}',
    "Receipts expire after",
)


def _flat(path: Path) -> str:
    return " ".join(path.read_text().split())


class StoryboardSkillTextTests(unittest.TestCase):
    def test_storyboard_skill_carries_the_evidence_contract_verbatim(self):
        lines = STORYBOARD_SKILL.read_text().splitlines()
        for quote in EVIDENCE_CONTRACT:
            self.assertIn(quote, lines)

    def test_storyboard_skill_points_at_the_authoring_and_evidence_guides(self):
        text = _flat(STORYBOARD_SKILL)
        self.assertIn(
            'call `storyboard__describe_grammar` with `{section: "authoring"}` and `{section: "evidence"}`',
            text,
        )
        self.assertIn('`{section: "canvas"}` before drawing', text)
        self.assertIn("Follow them; do not work from memory.", text)

    def test_canvas_skill_points_at_the_canvas_guide(self):
        # Replaces the verbatim design-doctrine pin: the doctrine is served by
        # the grammar's canvas section as canvas.design.
        text = _flat(CANVAS_SKILL)
        self.assertIn('call `storyboard__describe_grammar {section: "canvas"}`', text)
        self.assertIn("`canvas.design`", text)
        self.assertIn("Follow them; do not work from memory.", text)

    def test_skills_name_only_sections_the_server_serves(self):
        reference = {"overview", "schema", "bindings", "canvas", "prefabs", "libraries", "rules"}
        for path in (STORYBOARD_SKILL, CANVAS_SKILL):
            named = set(re.findall(r'section:? "([a-z_]+)"', _flat(path)))
            self.assertTrue(named, path.name)
            self.assertLessEqual(named, reference | set(GUIDE_SECTIONS), path.name)

    def test_skills_do_not_restate_what_the_server_guides_own(self):
        for path in (STORYBOARD_SKILL, CANVAS_SKILL):
            text = _flat(path)
            for needle in SERVER_OWNED:
                self.assertNotIn(needle, text, f"{path.parent.name}/SKILL.md restates {needle!r}")

    def test_canvas_skill_lists_every_binding_shape(self):
        # conductor mcp-gateway storyboard/tools/authoring.go describe_grammar description.
        self.assertIn("source · select · ref · derive · reduce · extract", CANVAS_SKILL.read_text())

    def test_skills_pin_the_maestro_version_that_serves_the_guides(self):
        # The guides (conductor #1975) are in no tag up to maestro v1.97.16;
        # an older maestro's describe_grammar rejects section "authoring".
        for path in (STORYBOARD_SKILL, STORYBOARD_SKILL.parent / "README.md"):
            text = " ".join(path.read_text().split())
            self.assertIn("newer than v1.97.16", text, path.name)
            self.assertNotIn("v1.97.15 or newer", text, path.name)
            self.assertNotIn("v1.97.12", text, path.name)

    def test_storyboard_skill_keeps_the_claude_code_specifics(self):
        text = _flat(STORYBOARD_SKILL)
        for needle in (
            # SessionStart hook (hooks/storyboard-session.py)
            '"Cardinal session id for this session: …"',
            "Pass it to `storyboard__create`",
            # PostToolUse preview hook (hooks/storyboard-preview.py)
            "a plugin hook renders each scene with your local Chromium and reports the PNG paths",
            "Read every PNG",
            "never a publish blocker",
        ):
            self.assertIn(needle, text)

    def test_storyboard_skill_teaches_promoting_captured_evidence(self):
        # bin/cardinal-evidence promote: any tool result (built-in tools and
        # other MCP servers alike) becomes a captured receipt only once
        # promoted into the draft.
        text = _flat(STORYBOARD_SKILL)
        for needle in (
            "Any tool result from this session can be cited",
            "shell commands (tests, `git`, `make`), file reads and edits",
            "`cardinal-evidence find <text>`",
            "**Withheld.**",
            "`[evidence:ev_… withheld: …]`",
            "it cannot be cited: say so plainly",
            "**Never claim more than the output shows.**",
            "reported by <client>",
            "`cardinal-evidence show ev_…`",
            "`[evidence:ev_…]`",
            "promote the ones you cite before binding them",
            "`cardinal-evidence promote --storyboard <id> ev_… [ev_…]`",
            "`ev_… -> rcpt_…`",
            "Nothing is uploaded until a storyboard cites it",
        ):
            self.assertIn(needle, text)

    def test_canvas_skill_keeps_the_local_preview_loop(self):
        text = _flat(CANVAS_SKILL)
        for needle in (
            "`~/.claude/cardinal/storyboards/<storyboard_id>/r<revision>/`",
            "Read every PNG the hook reported",
            'python3 -I "$RENDER" --from-json <preview.json> [--scene <id>]… [--theme dark]',
            "Keep the `-I`",
            "**Skip the preview, tell the user once, keep authoring.**",
        ):
            self.assertIn(needle, text)
        self.assertTrue((CANVAS_SKILL.parent / "scripts" / "render_preview.py").is_file())

    def test_canvas_skill_exemplars_exist(self):
        named = set(re.findall(r"`exemplars/([a-z0-9-]+\.js)`", CANVAS_SKILL.read_text()))
        self.assertEqual(named, {"scalar-and-series.js", "roof-and-timeline.js", "cohort-rows.js"})
        for name in named:
            self.assertTrue((CANVAS_SKILL.parent / "exemplars" / name).is_file(), name)

    def test_combined_skill_length_stays_slim(self):
        # The craft moved to the server (conductor #1975); the skills keep only
        # the Claude Code parts. Before: 477 lines / 4909 words (plugin 0.34.0).
        # After the slim: 202 lines / 1932 words. The caps leave a little room,
        # not enough to paste a guide back in.
        paths = (STORYBOARD_SKILL, CANVAS_SKILL)
        total = sum(len(p.read_text().splitlines()) for p in paths)
        self.assertLessEqual(total, 220)
        words = sum(len(p.read_text().split()) for p in paths)
        self.assertLessEqual(words, 2100)


if __name__ == "__main__":
    unittest.main()

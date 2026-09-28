"""Storyboard skills: the load-bearing text the design doc says the skills carry
verbatim (conductor docs/specs/investigation-storyboards.md, section 9).

The doctrine is edited for judgment and tightened over time; these pins keep the
evidence contract and the canvas design doctrine from being paraphrased away
while that happens. Everything else in the skills is free to change.
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

# Design doc section 9, "Canvas design doctrine", line for line.
CANVAS_DOCTRINE = [
    '> **Canvas**',
    '>',
    '> Canvas is a blank, interactive visual surface. When stock components are insufficient, use it freely.',
    '>',
    '> Act like an exceptional UI and information designer. Do not default to charts, boxes, tables, or generic node graphs',
    '> just because they are easy to generate.',
    '>',
    '> Ask: *If I had complete control over the pixels, what would make this finding obvious in five seconds?*',
    '>',
    '> Build that.',
    '>',
    '> You may draw physical objects, architecture, topology, timelines, flows, code, infrastructure, annotations, overlays,',
    '> animations, or entirely novel visualizations. Combine representations when useful.',
    '>',
    '> Prefer direct visual explanation over legends and prose. Put information where it belongs: status on the object,',
    '> latency on the path, failure on the component, change at the point it occurred.',
    '>',
    '> Use hierarchy aggressively. Make the important thing visually dominant and supporting context quiet. Remove anything',
    "> that does not help establish the scene's statement.",
    '>',
    '> Preserve useful spatial context across scenes and progressively reveal, highlight, annotate, or transform it when that',
    '> makes the story easier to follow.',
    '>',
    '> The Canvas receives evidence-backed data. You have complete freedom over its presentation, but no freedom to invent or',
    '> alter the underlying evidence.',
    '>',
    '> Always preview the Canvas. Judge the rendered result, not the source code. Iterate until a viewer can understand the',
    "> scene's point without reading the investigation transcript.",
]


# Source units conductor's prose-number check scales from a binding's declared
# `unit` (packages/maestro/src/storyboard/validation/numbers.ts: TIME_NS, BYTES,
# BYTE_UNITS, "%"). A declared unit outside this set switches off the fallbacks an
# undeclared value gets, so the skill must never recommend declaring one.
# k/M/G/T are written suffixes (MULTIPLIER), never declared units.
RECOGNISED_DECLARED_UNITS = {
    "ns", "us", "µs", "ms", "msec", "s", "sec", "secs", "second", "seconds",
    "m", "min", "mins", "minute", "minutes", "h", "hr", "hrs", "hour", "hours",
    "d", "day", "days",
    "B", "b", "byte", "bytes", "kB", "KB", "MB", "GB", "TB", "PB",
    "KiB", "MiB", "GiB", "TiB", "PiB",
    "%",
}


def _doctrine_blockquote(text: str) -> list:
    m = re.search(r"^## Canvas design doctrine\n\n((?:>[^\n]*\n)+)", text, re.M)
    return m.group(1).splitlines() if m else []


class StoryboardSkillTextTests(unittest.TestCase):
    def test_storyboard_skill_carries_the_evidence_contract_verbatim(self):
        lines = STORYBOARD_SKILL.read_text().splitlines()
        for quote in EVIDENCE_CONTRACT:
            self.assertIn(quote, lines)

    def test_canvas_skill_carries_the_design_doctrine_verbatim(self):
        self.assertEqual(_doctrine_blockquote(CANVAS_SKILL.read_text()), CANVAS_DOCTRINE)

    def test_canvas_skill_prefers_the_finding_over_the_telemetry(self):
        text = " ".join(CANVAS_SKILL.read_text().split())
        self.assertIn(
            "Prefer visualizing the conclusion or reasoning over merely visualizing the raw telemetry. "
            "The underlying telemetry should remain inspectable as evidence.",
            text,
        )


    def test_declared_unit_spellings_are_ones_the_prose_check_scales(self):
        text = " ".join(STORYBOARD_SKILL.read_text().split())
        m = re.search(r"Declare `unit` in a usual spelling \(([^)]*)\)", text)
        self.assertIsNotNone(m, "declared-unit list not found in the storyboard skill")
        spellings = [u.strip().rstrip("…").strip() for u in re.split(r"[,;]", m.group(1))]
        spellings = [u for u in spellings if u]
        self.assertTrue(spellings)
        for unit in spellings:
            self.assertIn(unit, RECOGNISED_DECLARED_UNITS, f"skill recommends declaring unit {unit!r}")

    def test_rules_out_direction_matches_the_grammar(self):
        # conductor packages/maestro/src/storyboard/grammar.ts RULES_OUT_DIRECTION
        # (same text in design doc section 5 and the viewer's CLAIM_VERB.rules_out).
        text = " ".join(STORYBOARD_SKILL.read_text().split())
        self.assertIn(
            "from = the candidate cause/hypothesis being ruled out, to = the outcome it is ruled out for; "
            'reads "from — ruled out as a cause of — to"',
            text,
        )

    def test_storyboard_skill_teaches_the_evidence_tools(self):
        # Shapes shipped in conductor #1958 (select, ref, reduce.where literals)
        # and #1959 (get_receipt navigation, failed-call receipts, compact preview).
        text = " ".join(STORYBOARD_SKILL.read_text().split())
        for needle in (
            "{select: {receiptId, representation?, in, match, pointer?, unit?}}",
            "{ref: <binding key>, pointer?}",
            "`reduce.where` takes a literal",
            "{param, label}",
            "outline: true",
            "`pointer` + `rows: {offset, limit ≤200}`",
            "model_result_omitted",
            '{receiptId, selector: "/error/message"}',
            "verbose: true",
        ):
            self.assertIn(needle, text)
        # Superseded text must not come back: failed reads now get receipts,
        # and positional selectors are no longer the only row-addressing recipe.
        self.assertNotIn("Writes, failed calls and kube Secret reads get no", text)
        self.assertNotIn("Positional selectors need `expect` guards", text)
        self.assertNotIn("value_preview_rule", text)

    def test_skills_match_conductor_1961(self):
        # conductor #1961: SQL execute_sql reads get receipts; one duration rule
        # (PROSE_NUMBER_RULES); select/ref address rows, expect only guards;
        # cv.embed options are {id, height} and unknown keys are frame errors.
        sb = " ".join(STORYBOARD_SKILL.read_text().split())
        cv = " ".join(CANVAS_SKILL.read_text().split())
        for needle in (
            "read-only SQL `execute_sql`",
            "A duration in any form (24h, 24-hour, 1h30m, 90m, 7d) is one value",
            "a value in a time unit (converted) or none, never a count",
            '"last 24h" needs the window bound',
            "`expect` only guards equality",
        ):
            self.assertIn(needle, sb)
        for needle in (
            "`cv.embed(prefab, props, {id, height})`",
            "its settings are props",
            "a prop or option it does not accept (the error names the accepted keys)",
        ):
            self.assertIn(needle, cv)
        # Superseded by #1961: compact durations were skipped; positional
        # selectors were taught as needing expect.
        self.assertNotIn("(p99, 1h30m)", sb)
        self.assertNotIn("still does", sb)
        self.assertNotIn("needs no `expect` guard", sb)

    def test_canvas_skill_lists_every_binding_shape(self):
        # conductor mcp-gateway storyboard/tools/authoring.go describe_grammar description.
        self.assertIn("source · select · ref · derive · reduce · extract", CANVAS_SKILL.read_text())

    def test_skills_pin_the_maestro_version_the_evidence_tools_need(self):
        # v1.97.15 is the first maestro tag carrying conductor #1958, #1959 and #1961.
        for path in (STORYBOARD_SKILL, STORYBOARD_SKILL.parent / "README.md"):
            text = path.read_text()
            self.assertIn("v1.97.15 or newer", text, path.name)
            self.assertNotIn("v1.97.12", text, path.name)

    def test_semantic_fixes_stay_pinned(self):
        # Quality-pass fixes (b), (c), (d), (f); deleting any of them must fail.
        text = " ".join(STORYBOARD_SKILL.read_text().split())
        for needle in (
            # (b) the viewer owns state and open questions; the canvas may still
            # draw the relationship a claim names.
            "The viewer renders each scene's state and its `openQuestions` from the spec; "
            'do not draw state pills or a "still open" list in the Canvas',
            # (c)
            "An attribution that is only `correlates_with`",
            "never inside another claim's from/to",
            # (d)
            "an established cause with an open impact is `supported`, with open questions",
            # (f)
            "`rules.prose_numbers`",
            "the check never reads Canvas source, so bind every number a canvas draws",
        ):
            self.assertIn(needle, text)
        # (b) must not tell the canvas to leave out the claims it visualizes.
        self.assertNotIn("the claims (as sentences)", text)

    def test_titles_carry_no_fixed_prefix(self):
        text = " ".join(STORYBOARD_SKILL.read_text().split())
        self.assertIn('**Titles are findings, not topics**, in about one clause, with no fixed prefix', text)

    def test_combined_skill_length_does_not_grow(self):
        # rjha, 2026-09-28: no new visual-style doctrine, and the skills must not
        # grow; new tool shapes replace superseded text. Baseline: plugin 0.33.1,
        # storyboard 209 + canvas 268 lines.
        paths = (STORYBOARD_SKILL, CANVAS_SKILL)
        total = sum(len(p.read_text().splitlines()) for p in paths)
        self.assertLessEqual(total, 477)
        # Lines alone can be gamed by re-flowing, so words are capped too. 0.33.1
        # had 2262 + 2592 = 4854 words; 0.34.0's tool reference is 55 words longer
        # (4909). The cap stops further growth; it does not claim a word-neutral 0.34.0.
        words = sum(len(p.read_text().split()) for p in paths)
        self.assertLessEqual(words, 4909)


if __name__ == "__main__":
    unittest.main()

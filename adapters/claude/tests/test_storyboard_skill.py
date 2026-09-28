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


if __name__ == "__main__":
    unittest.main()

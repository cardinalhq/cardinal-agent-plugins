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
REPO = PLUGIN_ROOT.parent.parent
STORYBOARD_SKILL = PLUGIN_ROOT / "skills" / "storyboard" / "SKILL.md"
CANVAS_SKILL = PLUGIN_ROOT / "skills" / "canvas" / "SKILL.md"

# Everything that tells Claude or the user how Storyboards come about.
GUIDANCE = (
    STORYBOARD_SKILL, CANVAS_SKILL, STORYBOARD_SKILL.parent / "README.md", PLUGIN_ROOT / "README.md",
    REPO / "README.md", PLUGIN_ROOT / "bin" / "cardinal-storyboard", PLUGIN_ROOT / "hooks" / "storyboard-session.py",
)
# The old lifecycle (SPEC §4): the storyboard as the end-of-investigation
# write-up, and the Investigation as something to create by hand first.
OLD_LIFECYCLE = (
    "finished investigation", "finished (or stalled)", "turn a finished",
    "write-up", "write up", "write this up", "show me how we got here", "after investigating",
    "after using cardinal tools", "turns an investigation into", "turns a cardinal investigation into",
    "before any storyboard: `investigation create`", "investigation create`, then",
    "`investigation create` binds the session", "create an investigation manually",
    "invoke the skill at the end", "run the storyboard skill as usual",
)

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
            "Pass it to `storyboard__find` and `storyboard__add_act`",
            # PreToolUse context hook (hooks/storyboard-context.py)
            "the context hook adds it if you forget",
            # PostToolUse preview hook (hooks/storyboard-preview.py)
            "a plugin hook renders each scene with your local Chromium and reports the PNG paths",
            "Read every PNG",
            "leave the storyboard in draft",
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
            "Promote the ones you cite before binding them",
            "`cardinal-evidence promote --storyboard <id> ev_… [ev_…]`",
            "`ev_… -> rcpt_…`",
            "Nothing is uploaded until a storyboard cites it",
        ):
            self.assertIn(needle, text)

    def test_storyboard_skill_keeps_evidence_hygiene(self):
        # From the release E2E: the agent promoted entries it never cited,
        # re-promoted instead of reusing receipts, and called redacted
        # (citable) results "withheld".
        text = _flat(STORYBOARD_SKILL)
        for needle in (
            "only those, as you write the scene that cites them",
            "prints its existing receipt (`already promoted`): reuse that id",
            "**Redacted is not withheld.**",
            "cite it and call it redacted, never withheld; claim nothing about masked values",
        ):
            self.assertIn(needle, text)

    def test_storyboard_skill_updates_instead_of_duplicating(self):
        # find -> continue / add_act / create (conductor storyboard__find and
        # storyboard__add_act; plan v2 C5). Feature-detected: an older Cardinal
        # without the tools gets a new storyboard, never an unknown argument.
        text = _flat(STORYBOARD_SKILL)
        for needle in (
            "## Update, don't duplicate",
            "cardinal-storyboard context",
            "storyboard__find",
            "before `storyboard__add_act` or `storyboard__create`",
            "storyboard__find {session_id, context}",
            "follow its `rule`",
            # Associations (plugin 0.40): context is stamped by the hook, the
            # CLI is the fallback; continue only on may_continue; `about` is
            # declared only when the server lists it, checkout only for this
            # branch's / PR's change, never for an incident; link after the PR.
            "A plugin hook adds `session_id` and `context` (where you write from)",
            "pass `cardinal-storyboard context`'s `{\"context\": {…}}`",
            "continue without asking only on `may_continue` true",
            "If storyboard__create lists `about`",
            "`{checkout: true}` only when it explains this branch's or PR's change",
            "ask if unsure; never for an incident",
            "`storyboard__link {storyboard_id, add: {prs: [N]}}` (or `{commits: [<merge sha>]}`)",
            "same_session",
            "`yours` both true",
            "storyboard__add_act",
            "`storyboard__add_act {storyboard_id, title, session_id, context}`",
            "no `storyboard__add_act` listed",
            "No `storyboard__find` (an older Cardinal): create without `context`",
            # Public links: ask the person unless they already said to update what they shared.
            "public_links",
            "public_links_decision_required",
            "unless they already said to update what they shared",
            '`public_links: "extend"` or `"keep"`',
            # The "already said to update what they shared" exemption covers
            # only the public_links choice; raw evidence always needs a fresh
            # yes from the person (plan v2 C4).
            "(that covers only this choice)",
            "raw_evidence_confirmation_required",
            "confirm_raw_evidence",
            "always ask the person, listing the bindings it names",
            "never set `confirm_raw_evidence` yourself, even if they said to update what they shared",
            "Only their yes sends `confirm_raw_evidence: true`",
            "published acts are immutable; storyboard__add_act adds the next act to the same storyboard and link",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("a published storyboard is immutable", text)

    def test_storyboard_skill_asks_before_adding_to_a_match(self):
        # rjha 2026-09-29 (plan v2 P9, "Ask before adding to an existing
        # storyboard"): offer strong matches (<=3) or one recent weak match,
        # always with a way out, and fall back to a new storyboard when
        # nobody can be asked.
        text = _flat(STORYBOARD_SKILL)
        for needle in (
            "### Ask before adding to an existing storyboard",
            "AskUserQuestion",
            "numbered question",
            "Start a new storyboard",
            "Continue open act <n> of \"<question>\"",
            'label `Add to "<question, first 40 chars>"`',
            "description `<why> · <act_count> acts · updated <relative time>`",
            # <why>, exactly one of:
            "`same session`",
            "`<why>` names the role",
            "`about <kind> <value>`",
            "`written from branch <branch>`",
            "`written from the checkout of PR <repo>#<pr_number>`",
            "Never call a written_from match the same PR",
            "`same repo <repo>`",
            "`your storyboard`",
            # which matches: strong <= 3, else one weak one from the last 7 days
            "strong matches (`match` session, or `match_role` about), then written_from ones, at most 3",
            "at most 1 weak match (repo, workdir, actor)",
            "7 days",
            "create without asking",
            # skip the prompt: a named target, or find's silent-continue case
            "Skip it if the person named a target",
            "continue silently only an `open_act` with `same_session` and `yours` both true",
            # non-interactive
            "`claude -p`",
            "name the best match in your final message",
            "say: add this to <storyboard>",
        ):
            self.assertIn(needle, text)

    def test_ask_before_adding_comes_before_storyboard_create(self):
        # The skill's flow block asks before its first storyboard__create, and
        # the section that says how sits above the flow block.
        raw = STORYBOARD_SKILL.read_text()
        self.assertLess(raw.index("### Ask before adding to an existing storyboard"), raw.index("```"))
        flow = raw.split("```")[1]
        self.assertIn("storyboard__create", flow)
        self.assertLess(flow.index("find"), flow.index("ask before adding"))
        self.assertLess(flow.index("ask before adding"), flow.index("storyboard__create"))

    def test_skills_teach_the_live_storyboard_not_a_finished_write_up(self):
        # SPEC §4: Cardinal creates the Investigation and its live Storyboard
        # for every connected session; the skill improves that projection.
        # No guidance may say the storyboard comes at the end, is a write-up
        # of a finished investigation, or that an Investigation must be
        # created first. The checkpoint guidance's "right then, not as a
        # summary at the end" says the opposite (the record is kept live), so
        # that exact negation is not the old lifecycle.
        for path in GUIDANCE:
            text = _flat(path).lower().replace("not as a summary at the end", "")
            for phrase in OLD_LIFECYCLE:
                self.assertNotIn(phrase.lower(), text, f"{path.relative_to(REPO)} still says {phrase!r}")

    def test_the_control_log_is_never_evidence(self):
        # Conductor refuses it too (control_log_not_evidence); the plugin never
        # captures `cardinal-storyboard investigation` calls.
        self.assertIn("**The control log is never evidence** and never public: never cite, promote or record "
                      "investigation events or `cardinal-storyboard investigation …` calls.", _flat(STORYBOARD_SKILL))

    def test_the_live_storyboard_is_the_default_target(self):
        text = _flat(STORYBOARD_SKILL)
        for needle in (
            "## Which storyboard",
            "This session's live storyboard (`sb_…` in the session-start context, or "
            "`cardinal-storyboard investigation link`): author it directly, no find, no question, never "
            "`storyboard__create` a second one.",
            "when `storyboard__set_frame` is listed, frame it",
            "`cardinal-storyboard investigation question \"<text>\"`",
            "not owner authority",
            "## Publish and share",
            "never published or shared on its own",
            "Publishing freezes a reviewed version",
            "Share only a published act, only when asked",
            "state init --investigation <inv_…>",
        ):
            self.assertIn(needle, text)
        raw = STORYBOARD_SKILL.read_text()
        self.assertLess(raw.index("## Which storyboard"), raw.index("## Update, don't duplicate"))

    def test_do_i_start_the_storyboard_first_is_answered_no(self):
        # The bug: asked "before I ask the question do I invoke the storyboard
        # skill?", Claude answered "No. Ask your question first and invoke
        # the skill at the end". The frontmatter description is in Claude's
        # context in every session, so the answer has to be there too.
        description = re.search(r"^description: (.*)$", STORYBOARD_SKILL.read_text(), re.M).group(1)
        for needle in ("Publish a Cardinal storyboard", "updating its evidence-bound scenes",
                       "reviewing previews", "changing the draft status to published"):
            self.assertIn(needle, description)
        for phrase in ("finished", "stalled", "at the end", "after using", "write"):
            self.assertNotIn(phrase, description.lower())
        self.assertIn('"Do I invoke the storyboard skill before I ask my question?" No. You never need to start '
                      "Storyboards. This session already has an Investigation and Storyboard. Work normally, and ask "
                      "for the Storyboard whenever you want to see or share the investigation.",
                      _flat(STORYBOARD_SKILL))
        readme = _flat(STORYBOARD_SKILL.parent / "README.md")
        self.assertIn("You never need to start a storyboard.", readme)
        self.assertIn("the answer is no: work normally", readme)
        self.assertIn("`cardinal-storyboard investigation link`", readme)

    def test_storyboard_readme_explains_acts(self):
        readme = _flat(STORYBOARD_SKILL.parent / "README.md")
        self.assertNotIn("To change one, ask for a new storyboard", readme)
        for needle in ("Published acts are immutable", "cardinal-storyboard context",
                       "An update adds an act to the same storyboard, so the id and link stay the same",
                       "never an absolute path"):
            self.assertIn(needle, readme)

    def test_canvas_skill_keeps_the_local_preview_loop(self):
        text = _flat(CANVAS_SKILL)
        for needle in (
            "`~/.claude/cardinal/storyboards/<storyboard_id>/r<revision>/`",
            "Read every PNG the hook reported",
            'python3 -I "$RENDER" --from-json <preview.json> [--scene <id>]… [--theme dark]',
            "Keep the `-I`",
            "**Tell the user visual review is unavailable and keep authoring the draft.**",
        ):
            self.assertIn(needle, text)
        self.assertTrue((CANVAS_SKILL.parent / "scripts" / "render_preview.py").is_file())

    def test_canvas_skill_exemplars_exist(self):
        named = set(re.findall(r"`exemplars/([a-z0-9-]+\.js)`", CANVAS_SKILL.read_text()))
        self.assertEqual(named, {"scalar-and-series.js", "roof-and-timeline.js", "cohort-rows.js"})
        for name in named:
            self.assertTrue((CANVAS_SKILL.parent / "exemplars" / name).is_file(), name)

    def test_storyboard_skill_teaches_the_card_behind_the_schema_gate(self):
        # Gateway tool schemas are additionalProperties:false: `card` or
        # `link_preview` sent to an older gateway is a tool error, so every new
        # arg is conditional on the tool listing it (as the associations track
        # gates `about`).
        text = _flat(STORYBOARD_SKILL)
        for needle in (
            "## The card",
            "When `storyboard__publish` lists `card`",
            "when `storyboard__publish` lists `link_preview`",
            "`storyboard__share` when its action lists `link_preview`",
            "`headline`: the conclusion, one sentence, at most 140 characters.",
            "must be bound in this act, or publish refuses.",
            "`headline_figure`: one bound number.",
            "`cover_scene`: the scene that shows the conclusion; avoid scenes that bind raw rows.",
            "Preview with `card`, Read the cover and the link-preview mock the hook reports, then publish with "
            "the same `card`.",
            "After publish a hook uploads your render of the cover (or hero) scene and says which image link "
            "previews use.",
            "Member link previews are on by default. Tell the person what a posted link shows",
            "`link_preview: false`",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("Slack app", text)

    def test_canvas_skill_judges_the_cover_and_the_thumbnail(self):
        text = _flat(CANVAS_SKILL)
        for needle in ("`<scene>-cover.png`", "1200x630", "`unfurl-mock.png`", "240 px thumbnail",
                       "pick another `cover_scene`"):
            self.assertIn(needle, text)



if __name__ == "__main__":
    unittest.main()

# Spike 2 evaluation — skill-driven compilation of Session A

**Date:** 2026-08-01
**Session compiled:** `e470b9a9-8ccb-4059-a4f0-13da5373b70c` (lakerunner worklane depth investigation, uncompacted, 21 tool calls, 1 attachment)
**Evaluator:** <operator> (ran the source investigation)
**Verdict:** **close to acceptable** — proceed to ground-truth + feedback-loop iteration.

---

## Q1. Does `sentinel.yaml` look like Session A's investigation?

**Yes, with two documented losses.**

The `metric-anomaly-integrity-check` Sentinel captures the core question I was actually asking: "is this metric spike real, or is the metric itself broken?" The two-part degeneracy test (cross-dimension collapse + within-series flatness) is exactly the pattern I inferred from #19's output. `dimensionalBreakdown: [action, level]` matches the worklane-specific labels I chose.

Documented losses:
- **Secondary conclusion** (size-starved ingest / real overdue-depth signal from #18) moved to a variation extension point. Honest — this was orthogonal to the reusable question.
- **Manual code-reading** (#3, #4, #5, #6, #8, #9) collapsed. See Q7.

## Q2. Are node IDs semantic and stable?

**Yes.** All six IDs describe what the node does, not its order:
- `query-metric-timeseries`, `query-metric-by-dimensions`, `locate-metric-emission-context`, `detect-degeneracy`, `degeneracy-condition`, `emit-degeneracy-finding`.

No `tool-4` / `step-7`. Names would survive a reordering. Style question flagged in the rationale (`detect-degeneracy` vs. `check-collapse`) is real but not blocking.

## Q3. Did the skill correctly filter meta-tool calls?

**Correctly and intelligently.**

- `#10 ToolSearch` → INCIDENTAL (correct — Claude Code session-runtime tool prep).
- `#20`, `#21` (bash.jq against paths under `~/.claude/projects/`) → **not** blindly filtered as INCIDENTAL. The subagent recognized these as Claude Code's spill-to-disk pattern (tool result exceeded token budget → operator recovers with jq/cat) and treated them as semantic continuations of #19 and #18. This is a **refinement** of spike-1's F3 rule, not a violation.

This is the best possible outcome: the skill applied F3's intent (drop meta) while rejecting its literal form (drop-by-path-prefix). Feed this refinement back into F3.

## Q4. Did the skill decompose Bash into synthetic capabilities?

**Yes, per F5.** `bash.grep`, `bash.git`, `bash.kubectl`, `bash.jq` all appear in the classification table. Retained tool nodes bind to capability IDs (`observability.query-metrics`, `code.grep`) rather than vendor tool names, per §10.

## Q5. Did the skill attribute conclusion numbers back to producing tool calls?

**Partially.** The `detect-degeneracy` node's "Fixture from investigation" section cites:
- `avg=2.014e+04 min=2.014e+04 max=... p99=2.014e+04 count=... across 9 series` → from #19 via #20.
- `100% of claims fired on trigger=age` → from #18 via #21 (in the variation extension discussion).

Not line-by-line-perfect (no explicit `citation → conclusion sentence` map), but attribution IS present and traceable. Acceptable for a spike. A shipping compiler should produce an explicit citation map.

## Q6. Did the skill honestly refuse anything?

**Yes, in three places worth counting:**

1. **Rejected an initial `assess-integrity` LLM node** in favor of `detect-degeneracy` function + `degeneracy-condition` condition, per §32. Recorded in Stage 6 iteration notes so a reviewer sees the choice was made.
2. **Documented compression loss** for #3–#9 as "genuine loss of fidelity" rather than papering over it.
3. **Flagged expression-language gap** (`join()` used in tool arguments but not enumerated in §13) rather than assuming it works.

No hallucinated fixtures. No image-content-as-evidence. Attachment recorded as `disposition: replaced-by-plain-input` with a clear reason.

## Q7. Where is the largest gap vs. a human compiler?

**Code-reading compression** (rationale item #2). The six code-reading calls (#3–#9) are what let me argue "level-0 and level-7 SHOULD differ by hours, per `laneEffectiveMaxAgeExpr` and `LevelMaxAgeCap`." That semantic bridge is what turned "flat numbers" into "degenerate metric." The Sentinel pushes this judgment onto the operator via `dimensionalBreakdown` (they must know which labels ought to vary) and `withinSeriesFlatnessTolerance` (they must pick a threshold).

For a specialist re-running this against a different metric on the same codebase, that's fine. For a generalist encountering an unfamiliar metric, it's a real gap. A shipping compiler needs an answer for "how do we preserve the code-reading judgment": either an LLM node with directory access, or a summarize-the-emission-code function that runs once and feeds the degeneracy assessment.

Not a spike failure — it's a genuine unresolved compiler design question the spike surfaced. See rationale.md items 1, 2, 3, 4, 6 for the full list.

---

## Overall verdict

**Close to acceptable.** The output is what an honest compiler would produce given the current spec, and where it fell short, it said so. Highest-value refinements the spike surfaced:

- **F3 refinement:** spill-to-disk pattern (`Output has been saved to <path>` → subsequent `jq/cat/python` on `<path>`) is one logical operation, not a meta-tool call.
- **Code-reading pattern:** compiler needs a policy for interactive read-many-files exploration.
- **Expression language:** §13 governs conditions only; tool-argument expressions need their own allowlist (or must be declared as the same language).
- **Node-ID stability under iteration:** Stage 6 rename freedom conflicts with §9 stable-ID requirement.
- **Attachment-choice policy:** four options listed, no chooser guidance.

## Recommended next action (per handoff decision tree)

Hand-author ground-truth DAG for Session A + iterate skill with feedback loop. Ground truth should:

- Encode the code-reading judgment explicitly (probably a summarize-emission-code function node backed by an LLM invocation, so the level-0-vs-level-7 argument survives).
- Retain the secondary size-starved-ingest signal as a first-class node rather than a variation extension, so we can see whether the skill's "focus on one reusable question" heuristic under-scoped or the human would keep both.
- Serve as scoring rubric for a second spike-2 run.

Then re-run spike 2 with an updated SKILL.md that closes the five gaps above, and score against ground truth.

# Spike-2 v2 scored against ground truth — Session A

**Date:** 2026-08-01
**Ground truth:** `ground-truth/sentinel.yaml` (10 nodes, anomaly-triage scope, LLM-required)
**v2 output:** `out-v2/sentinel.yaml` (11 nodes, two-branch scope, LLM-optional)
**v1 output (for reference):** `out/sentinel.yaml` (6 nodes, narrow metric-integrity scope)

---

## Scoring summary

| Axis | v1 | v2 | GT | Notes |
|---|---|---|---|---|
| A. Reusable-question scope | narrow (integrity only) | expanded (integrity + workload branch) | expanded (integrity + real-driver) | v2 **converges** with GT on scope-up; framing differs (mirror-image logic) |
| B. LLM capability declared | no | yes (optional) | yes (required) | v2 **partial** — right instinct, wrong essentiality |
| C. Code-reading essential | no (inputs) | no (optional LLM) | **yes** (required LLM) | v2 **misses** — biggest single gap |
| D. Alt-driver probe first-class | no (variation) | yes (optional node) | yes (gated node) | v2 **converges** — big win vs. v1 |
| E. Second LLM node (real-driver reasoning) | no | no (used function) | yes (`explain-real-signal`) | v2 **misses** — kept deterministic where GT went qualitative |
| F. Finding shape | 1 emit | 2 emits w/ cross-guard | 1 two-part emit | v2 **misses** the exact call GT-DIFF-8 resolved |
| G. Spill-to-disk collapse | editorial | mechanical (`collapsedSteps[]`) | editorial | v2 **cleaner** than GT here |
| H. Attachment handling | outcome only | Q1 fired, procedure recorded | replaced-by-plain-input | v2 **matches** GT + adds audit trail |
| I. Emit-branch coverage | 1 (degeneracy only) | 2 (deg + workload) | 3 (deg-explained + driver-confirmed + inconclusive) | v2 **partial** — GT is more graceful |
| J. Retained tool-call count | 5 | 4 (+2 collapsed) | 12 | v2 **misses** — code-reading calls dropped even though LLM node is declared |

**Convergence rate:** 4 match / 4 partial / 3 miss = SKILL-v2 closed roughly half the gap between v1 and GT.

---

## The three misses, ranked

### Miss 1 — Code-reading not treated as essential (axes B/C/J)

Ground truth made three coupled decisions:
- `codeRepoPath: required: true`
- `summarize-driver-semantics` is a first-class REQUIRED LLM node
- Tool calls #3–#9 are all RETAINED as feeding that node

v2 made the mirror-image call:
- `codeRepoPath: required: false`
- `interpret-metric-semantics-from-code` is OPTIONAL (only runs when repo path is set)
- Tool calls #3–#9 are still OMITTED as tool nodes

**Why this happened:** SKILL-v2's Stage 2.5 chooser presents A/B/C options as tradeoffs the compiler picks between. It doesn't push the compiler to pick A ("essential LLM") when the source investigation used the judgment as load-bearing. The subagent picked A as a *hybrid* (LLM available if repo provided) rather than as *required*.

**Root cause:** the SKILL asks "which option?" but doesn't ask "was the source-investigation conclusion reachable WITHOUT this judgment?" If not, the answer defaults to A-required.

### Miss 2 — Second LLM node absent (axis E)

Ground truth has `explain-real-signal`: LLM reasoning that turns alternative-metric time series + emission semantics into a proposed real driver.

v2 has `detect-counter-label-dominance` (function) → `capacity-starvation-condition` (condition) → deterministic emit-title interpolation.

**Why this happened:** SKILL-v1 and SKILL-v2 both push "prefer function+condition over LLM per §32." That's correct as a default. But it's wrong when the reasoning is genuinely causal-across-multiple-signals — which this investigation's second half was. Detecting a single-label dominance ratio > 0.9 is deterministic; concluding "this dominance PATTERN caused the SYMPTOM on a different metric" is not.

**Root cause:** §32's "prefer function" bias is a good default but doesn't have an escape hatch for causal cross-signal reasoning. SKILL-v3 needs a rule: "if the judgment ties evidence from ≥2 tool nodes into a causal claim, it's LLM."

### Miss 3 — Two findings instead of one (axis F)

Ground truth: single `correlated-anomaly-explanation` emit with `parts.integrityAssessment` + `parts.realDriverHypothesis`.

v2: two separate emits (`emit-degeneracy-finding`, `emit-capacity-starvation-finding`) with a cross-guard preventing both from firing.

**Why this happened:** SKILL-v2 has no guidance on when to unify vs. split emits. The subagent's default when two branches produce different classifications is two emits. Ground truth argued the story coupling ("X is a red herring; Y is the real driver") requires unification.

**Root cause:** missing SKILL guidance on emit-unification. This is a spec-adjacent decision — §14 (emit-node) doesn't discuss multi-part findings.

---

## The four convergences

### Convergence A — Scope expansion (axis A)

v1 asked "is this metric degenerate?" (narrow). Both v2 and GT expanded to "why did this metric spike, given the integrity question is part of it?" This is the biggest gap-closing win — SKILL-v2's new Stage 3 (with the two-part conclusion recognition from L118) worked.

### Convergence D — Alt-driver first-class

v1 demoted the size-starved-ingest signal to a "variation extension point." Both v2 and GT promoted it back into the DAG. v2's guard is slightly different (`relatedCounterMetric != null` vs. GT's integrity-failure gate) but the promotion decision is the same.

### Convergence G — Spill-to-disk formalized

v2's `provenance.collapsedSteps[]` field is cleaner than GT (which folded it into prose). SKILL-v2's Stage 1.5 rule is doing real work here. Feed this back into F3 in `spike/dag-harvest/FINDINGS.md`.

### Convergence H — Attachment chooser

Both landed at replaced-by-plain-input; v2 recorded WHICH question (Q1) fired. Auditable in a way GT isn't (GT just names the outcome).

---

## What this means for the SKILL

Highest-ROI edits to SKILL-v3:

1. **New Stage 2.5 rule** — "If the source investigation's conclusion is not reachable without the code-reading judgment, the LLM node is REQUIRED, not optional." Codify Miss 1's root cause.

2. **New Stage 4 rule** — "If a judgment ties evidence from ≥2 tool nodes into a causal claim, it is an LLM node." Codify Miss 2's root cause.

3. **New Stage 4.6 (emit unification)** — "When two conditions classify the same underlying phenomenon and the reader needs both classifications together to understand the story, emit ONE multi-part finding. When they are independent classifications, emit two." Codify Miss 3.

4. **Adopt v2's Stage 1.5 spill-collapse rule verbatim** — it works.

5. **Adopt v2's Stage 4.5 attachment chooser verbatim** — it works.

## What this means for the sentinels initiative

Spike-2 has now given us three independent artifacts (v1, v2, GT) that agree on:
- The Sentinel shape (schema per §8)
- The scope should be bigger than v1 argued
- Alternative-driver probe belongs in the DAG
- Spill-to-disk is a compiler-formalizable pattern

They disagree on:
- Whether LLM nodes are essential or optional (the load-bearing productization question)
- Whether the emit is unified or split

The disagreement axes are more valuable than the convergences — they tell us where the phase 2 compiler design has real choices to make. **Recommendation:** move the disagreements into `sentinels.md` as explicit open questions (§30 or a new "Compiler design open questions" section) and treat them as prerequisites to phase 2 compiler work. The heuristic + skill spikes have earned out — further spike iterations without the spec-level decisions are diminishing returns.

## What's ready to commit

- `spike/dag-harvest/` — spike 1 (heuristic).
- `spike/skill-driven-harvest/SKILL.md`, `SKILL-v2.md` — both versions preserved.
- `spike/skill-driven-harvest/out/`, `out-v2/`, `ground-truth/` — three DAG artifacts.
- `spike/skill-driven-harvest/EVAL.md`, `SCORING.md` — evaluation of each round.

All safe to commit per the handoff's "commit posture" note. Recommended commit shape: one commit for spike 1, one for spike 2 (both rounds + ground truth + scoring). Do NOT co-commit the spec (`sentinels.md`) — that lands on `feat/sentinels-spec` per the handoff.

# Ground-truth decisions — Session A

**STATUS: FINALIZED 2026-08-01.** All 8 GT-DIFFs accepted. Final Sentinel at `ground-truth/sentinel.yaml` is the scoring anchor for spike-2 v2.

## Resolved decisions (summary)

| Meta / GT-DIFF | Decision |
|---|---|
| Scope | **Anomaly-triage with integrity check** (strawman shape) — "Is this reported anomaly real, and if not, what IS the real driver?" |
| Code-reading role | **Essential** — the "level-0 vs level-7 SHOULD differ" judgment was code-derived, not intuition. |
| Reuse target | **Different metrics** — general anomaly-triage tool, not "did the fix stick?" |
| GT-DIFF-1 (name) | Accept — `correlated-anomaly-explanation`. |
| GT-DIFF-2 (conclusionType) | Accept — `anomaly-explanation`. |
| GT-DIFF-3 (codeRepoPath) | Accept — REQUIRED. |
| GT-DIFF-4 (llm.reason capability) | Accept — first-class LLM capability. |
| GT-DIFF-5 (summarize-driver-semantics LLM node) | Accept — first-class LLM node with code.read access. |
| GT-DIFF-6 (detect-degeneracy depends on LLM summary) | Accept — uses `dimensionsShouldVary` from the summary. |
| GT-DIFF-7 (alternative-driver probe first-class) | Accept — `query-alternative-drivers` + `explain-real-signal` are first-class, gated on integrity failure. |
| GT-DIFF-8 (finding shape) | Accept — one two-part finding (`correlated-anomaly-explanation`), preserves story coupling. |

## What the ground truth adds beyond the resolved GT-DIFFs

- **Three emit nodes, not one.** `emit-explanation` (degeneracy + real driver), `emit-driver-confirmed` (integrity passed — correlation may be genuine), `emit-inconclusive` (insufficient data). Session A only exercised the first path; the others are added because a Sentinel should handle the branches its own condition creates.
- **`llm.reason` as a general capability** rather than a task-specific `llm.summarize-emission`. Two LLM nodes (`summarize-driver-semantics`, `explain-real-signal`) reference it. This matches §12 better and reduces capability sprawl.
- **`query-driver-by-dimensions` depends on `summarize-driver-semantics`.** The dimensions passed to the group-by come from the LLM summary's `dimensionsEmitted`, not from an operator input. This closes a gap the strawman glossed: dimensionalBreakdown was still an operator input in the strawman, contradicting the "code-reading is essential" resolution.

## What is intentionally NOT in the ground truth

- **Recursion when alternative driver ALSO fails integrity.** Real investigations can iterate; this Sentinel stops after one hop. If the alternative driver is also degenerate, `explain-real-signal` will say so with low confidence. Recursion is a phase-2 spec question.
- **A "confirm the real driver" query step.** After `explain-real-signal` proposes `claim_trigger`, a rigorous investigation would dimensionally probe it too. Omitted for scope; a variation could add it.
- **Alerting/routing metadata.** No pager keys, no escalation policy. Ground truth captures the investigation shape; routing is a Binding concern per §57.

---

## Original strawman + open questions (kept below for audit)

Framing for the strawman at `ground-truth/sentinel.yaml`. Every `GT-DIFF-N` in the YAML corresponds to a decision here.

## The load-bearing scoping call

**Spike-2 said the reusable procedure is:** "Is this metric degenerate?" (one-part answer, boolean-shaped conclusion).

**Strawman says the reusable procedure is:** "Is this reported anomaly real, and if not, what IS the real driver?" (two-part narrative conclusion).

The evidence:
- The session's opening prompt was "we just saw a spike in worklane depth for logs, figure out why that metric spiked" — a WHY question, not a "is this metric broken" question.
- The conclusion had two parts: (a) claim_wait_ms is degenerate; (b) the real story is size-starved ingest.
- The whole point of the code-reading + degeneracy detection was to reject a misleading hypothesis and get to the actual driver.

If the reusable procedure is only (a), the Sentinel becomes a metric-QA tool. If it's both, the Sentinel becomes an anomaly-triage tool that has metric-QA as an internal step. **The strawman argues the latter.** Which framing matches what you were actually doing?

---

## GT-DIFFs — one decision per item

### GT-DIFF-1: Sentinel name/scope

Strawman: `correlated-anomaly-explanation` (broader).
Spike-2: `metric-anomaly-integrity-check` (narrower).

Which describes what you were doing? Or was it neither — some third framing?

### GT-DIFF-2: `conclusionType`

Strawman: `anomaly-explanation` (narrative story).
Spike-2: `metric-integrity-classification` (boolean-shaped classification).

Related to GT-DIFF-1.

### GT-DIFF-3: `codeRepoPath` required vs. optional

Strawman: **required**.
Spike-2: **optional**.

The claim: without reading the emission code, the "dimensions should differ" judgment is unjustified — the operator would be picking `dimensionalBreakdown` values based on... vibes? For this Sentinel to work on a metric the runner hasn't investigated before, the code-reading has to happen inside the DAG, not before it.

Counter-argument: for the specific case where the operator KNOWS the metric already (e.g., re-running the Sentinel against a familiar metric), forcing a code repo path is annoying.

Your call.

### GT-DIFF-4 + GT-DIFF-5: LLM node for `summarize-driver-semantics`

Strawman: introduces an `llm.summarize-emission` capability + a first-class LLM node.
Spike-2: deliberately avoided any LLM nodes.

The claim: this is exactly the kind of judgment §12 says LLM nodes should exist for — "what does this metric measure and which dimensions should vary" is qualitative and code-semantic. Compressing it into a `dimensionalBreakdown` input is hiding the judgment, not eliminating it.

Counter-argument: LLM nodes are expensive + non-deterministic. If the operator is running this against 100 metrics, that's 100 LLM invocations per run.

Your call: LLM node, function node fed by grep output, or operator input (spike-2's choice)?

### GT-DIFF-6: `detect-degeneracy` depends on LLM summary

If GT-DIFF-5 stays, then `detect-degeneracy` uses `dimensionsShouldVary` from the LLM summary rather than trusting the operator's `dimensionalBreakdown` blindly. If GT-DIFF-5 goes, this collapses too.

### GT-DIFF-7: alternative-driver probe first-class vs. variation

Strawman: `query-alternative-drivers` + `explain-real-signal` are first-class nodes gated on integrity failure.
Spike-2: moved to a documented "variation extension point" outside the DAG.

Related to GT-DIFF-1. If the reusable question is "what's the real driver," these nodes are the payoff. If it's "is the metric degenerate," they're orthogonal.

### GT-DIFF-8: one two-part finding vs. two findings

Strawman: single `correlated-anomaly-explanation` emit.
Spike-2: single `metric-integrity-degenerate` emit; the "real driver" would be a separate emit (in the variation).

The claim: the story "X is a red herring; Y is the real driver" loses meaning if split. The reader wants both halves at once.

Counter-argument: dedupe / severity / routing may work better on separate finding types.

---

## Meta-questions

Two things I'd want to hear you answer before we finalize:

1. **Was the code-reading (calls #3–#9) essential to your conclusion, or scaffolding you could have skipped?** Spike-2's compression assumes scaffolding. Strawman assumes essential. Which is closer to your memory?

2. **Would you re-run this Sentinel against a different metric on the same codebase?** If yes, the strawman shape works. If you'd only re-run it against the SAME metric (i.e., "did the fix stick?"), the strawman is over-engineered and spike-2's narrower shape is right.

Answer these two + accept/reject/amend the 8 GT-DIFFs, and I'll produce the final ground-truth.

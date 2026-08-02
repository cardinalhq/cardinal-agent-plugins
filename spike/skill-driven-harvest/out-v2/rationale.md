# Compilation rationale — session A → `metric-anomaly-explanation`

**Capture ID:** `e470b9a9-8ccb-4059-a4f0-13da5373b70c` (lakerunner worklane depth spike investigation)
**Skill:** mechanize-compile (spike v2)
**Compiled by:** subagent execution, 2026-08-01

---

## Stage 1 — segmentation summary

| Field | Value |
|---|---|
| Objective (first substantive user text, L7) | "We just saw a spike in worklane depth for logs, and I found a correlated metric: [Image #1]" |
| Follow-up clarification (L23) | "there is clear correlation with: lakerunner_worklane_claim_wait_ms figure out why that metric spiked" |
| User course-correction (L80) | "this is in the oportun data" — investigation stayed on `prod` because the operator's data was actually there. |
| Attachments | 1 image, attached at L7 (att-1: image/png, ~343 KB, source Screenshot 2026-07-27) |
| Tool calls | 21 total |
| Conclusion (L118) | Two-branch classification: (1) `claim_wait_ms` is a **degenerate** metric (stuck histogram summary); (2) the correlated overdue-depth climb IS real, explained by **size-starved ingest** — 100% of claims fired on `trigger="age"`. |

Conclusion shape: classifying + explaining (present-tense stative verbs — "isn't measuring", "is size-starved"). Not task-execution. Compilation proceeds.

## Stage 1.5 — spill-projection detection (new v2)

Spill markers found in tool_results at L86, L104, L106.

| Spill origin | Spill path (tail) | Follow-up projection | Collapse decision |
|---|---|---|---|
| #16 Bash grep-oportun (L85→L86) | `bg7l3svd7.txt` | none — no follow-on tool call reads this path | NOT collapsed; #16 remains a standalone EXPLORATORY step (dead-end) |
| #18 mcp lakerunner query (L103→L104) | `...-1785184689334.txt` | #21 (L115) `jq -r '.summary' <path>` | COLLAPSED: #21 folded into `query-related-counter-by-labels` (the semantic node for #18) |
| #19 mcp lakerunner query (L105→L106) | `...-1785184690849.txt` | #20 (L113) `jq -r '.summary' <path>` | COLLAPSED: #20 folded into `query-metric-by-dimensions` (the semantic node for #19) |

The refined F3 rule fires: #16 is INCIDENTAL (spill exists but no follow-up projection), while #20 and #21 are COLLAPSED (spill-projections of preceding queries), not INCIDENTAL. v1's editorial call to collapse them is now the spec-level rule this v2 skill enforces.

## Stage 2 — per-tool classification

| # | Tool | Synthetic capability | Classification | Retained node | Rationale |
|---|---|---|---|---|---|
| 1 | Bash | `bash.grep` | EXPLORATORY | — | Empty result (bare-token grep for `worklane_claim_wait_ms`). Rejected hypothesis about the identifier form. |
| 2 | Bash | `bash.grep` | SUPPORTING | `locate-metric-emission-code` (OPTIONAL) | Grep for `claim_wait_ms\|ClaimWaitMs` found the emission source. Encoded as OPTIONAL tool node gated on `codeRepoPath`. |
| 3 | Read | `filesystem.read` | SUPPORTING | — (fed into LLM node) | Read `telemetry.go` for metric declaration. Judgment output absorbed into `interpret-metric-semantics-from-code`. |
| 4 | Bash | `bash.grep` | SUPPORTING | — (fed into LLM node) | Located `claimWaitHist.Record(...)` in `manager.go:202`. Same. |
| 5 | Read | `filesystem.read` | SUPPORTING | — (fed into LLM node) | Read `manager.go:150-249` (the recording site). Same. |
| 6 | Bash | `bash.grep` | SUPPORTING | — (fed into LLM node) | Located `effectiveMaxAge`/`MaxAge` config — the config that told the operator level-0 vs level-7 SHOULD differ. Same. |
| 7 | Bash | `bash.git` | EXPLORATORY | — | Recent commits check; not cited by conclusion. |
| 8 | Bash | `bash.grep` | SUPPORTING | — (fed into LLM node) | Located `phase-latency.md` spec. Same. |
| 9 | Read | `filesystem.read` | SUPPORTING | — (fed into LLM node) | Read the phase-latency.md definition. Same. |
| 10 | ToolSearch | (Claude Code meta) | INCIDENTAL | — | Session-runtime tool preparation. |
| 11 | Bash | `bash.kubectl` | FAILED | — | SSO expired. |
| 12 | mcp kube list | `kube.list-resources` | FAILED | — | Empty result (wrong labels). |
| 13 | mcp kube list | `kube.list-resources` | FAILED | — | Empty result (wrong namespace). |
| 14 | mcp kube list | `kube.list-resources` | EXPLORATORY | — | Namespace listing; not cited by conclusion. |
| 15 | lakerunner list_instances | `observability.list-instances` | EXPLORATORY | — | Instance discovery — collapsed into `instance` input. |
| 16 | Bash | `bash.grep` | EXPLORATORY | — | Follow-up on user hint "oportun"; spilled to `bg7l3svd7.txt`; NO follow-on projection — genuine dead end (per Stage 1.5 discriminator). |
| 17 | mcp lakerunner query | `observability.query-metrics` | REQUIRED | `query-metric-timeseries-baseline` | Confirmed the metric was flat over lookback (all data-points ≈ 20000). Cited in L102 conclusion. |
| 18 | mcp lakerunner query | `observability.query-metrics` | REQUIRED | `query-related-counter-by-labels` | The `claim_trigger` rate query. Summary (extracted by #21) cited in L118 as "100% of claims fired on trigger=age". |
| 19 | mcp lakerunner query | `observability.query-metrics` | REQUIRED | `query-metric-by-dimensions` | KEY evidence — avg by (action, level). Cited directly in L118. |
| 20 | Bash | `bash.jq` | COLLAPSED | (into #19's node) | Spill-projection of #19. |
| 21 | Bash | `bash.jq` | COLLAPSED | (into #18's node) | Spill-projection of #18. |

## Stage 2.5 — Code-reading option chosen

Cluster: tool calls #2, #3, #4, #5, #6, #8, #9 (7 calls). Purpose: understand what `claim_wait_ms` measures and which dimensions SHOULD vary if healthy. The critical semantic bridge — "level-7 compact SHOULD sit near 1h per `LevelMaxAgeCap`, so identical values across levels is impossible for honest data" — depended on this cluster.

**Decision procedure walk:**

- Q1 (extracting typed CONSTANTS?) — Partially. The operator noted `LevelMaxAgeCap` and `IngestMaxAge` values but didn't extract them as numbers per se; they served as qualitative anchors. Skip Option B.
- Q2 (qualitative judgment across whole functions?) — Yes. The operator read `manager.go:196-202` to understand that `claim_wait_ms` is "age of the oldest input in a batch at the moment it's claimed" (this exact phrasing appears in L53 and L77). This is a qualitative reading of a function body plus surrounding config.
- `codeRepoPath` — Realistic runtime input? For an operator investigating their own codebase, yes. For a generalist running the Sentinel across arbitrary metrics, no.

**Chosen: Option A (LLM node) as an OPTIONAL branch.**

Rationale: the code-reading judgment is generalizable to any histogram metric on any codebase, but only when a checkout is available. Making it optional lets the Sentinel run with or without the interpretation. `locate-metric-emission-code` (grep) and `interpret-metric-semantics-from-code` (llm) are both gated on `codeRepoPath != null`. When the branch runs, its output attaches to `emit-degeneracy-finding` as OPTIONAL evidence.

**Fidelity loss recorded:** When `codeRepoPath` is not supplied, the code-reading judgment falls back to the operator's declared inputs (`dimensionalBreakdown`, `withinSeriesFlatnessTolerance`). A generalist without source access must still declare these, so v1's compression cost survives in the fallback path.

**Contrast with v1:** v1 chose Option C (compression into structured inputs) and dropped the emission-code grep as OPTIONAL reviewer context only. v2 upgrades to a hybrid: the same grep node exists, but it now feeds an LLM interpretation node when a checkout is available. If reviewers judge the LLM node is not worth the runtime cost, deleting `interpret-metric-semantics-from-code` and its edge into `emit-degeneracy-finding` restores the v1 shape without other structural changes.

## Stage 3 — procedure signature

```
objectiveClass: metric-anomaly-explanation
evidencePattern:
  - metric-time-series
  - dimensional-breakdown
  - related-counter-label-distribution
  - (optional) metric-emission-source-code
transformations:
  - query-metric-current-timeseries
  - group-by-dimensions
  - detect-cross-dimension-value-collapse
  - detect-within-series-flatness
  - compute-counter-label-dominance
judgments:
  - metric-integrity-classification
  - (conditional) workload-pattern-classification
outputClass: metric-integrity-finding | workload-capacity-starvation-finding | inconclusive
```

Coherent, reusable procedure. Compilation proceeds.

## Stage 4 — DAG synthesis, per-node

### `query-metric-timeseries-baseline` (tool, from #17)
- Preserved: query shape `avg(<metric>{svc, signal})`; aggregation choice.
- Generalized: `metricName`, `instance`, `serviceName`, `signal`, `lookbackWindow` are inputs.
- Guessed: output schema `{summary, series_total, data_points_total}` — matches the payload shape captured at L94.
- Capability abstraction: bound to `observability.query-metrics` (per §10) not the vendor MCP tool name.

### `query-metric-by-dimensions` (tool, from #19 + collapsed #20)
- Preserved: `avg by (<dims>) (<metric>{...})` shape.
- Generalized: `dimensionalBreakdown` is a required array input. Original session used `[action, level]`; that becomes the input's runtime binding.
- Collapse note: this node's `output.summary` and `output.ddsketches` correspond to what #20's `jq -r '.summary'` extracted from #19's spill. The spill mechanism is a Claude Code runtime detail; the compiler treats the pair as one logical query returning a summary + per-series ddsketches.
- Guessed: `ddsketches` schema (per-series stats map). Derived from the format hint in L106.

### `query-related-counter-by-labels` (tool, from #18 + collapsed #21)
- Preserved: `sum by (<labels>) (rate(<counter>{...}[5m]))` shape.
- Generalized: `relatedCounterMetric`, `relatedCounterLabels`, `relatedCounterDominanceLabel` are OPTIONAL inputs. When absent, the node's `when` gate skips it, and the capacity-starvation branch never fires.
- Rationale for OPTIONAL: not every metric-anomaly investigation has a natural companion counter. The Sentinel remains useful for pure integrity checks. When a counter is supplied, the second branch runs.
- Collapse note: same spill-projection collapse as `query-metric-by-dimensions` (#21 folded in).

### `locate-metric-emission-code` (tool, OPTIONAL, from #2)
- Preserved: grep intent (find the metric by name in the codebase).
- Generalized: `pattern = ${inputs.metricName}`, `path = ${inputs.codeRepoPath}`, added file globs.
- Gated on `codeRepoPath != null`.
- Guessed: structured output schema `matches: [{file, line, text}]`. The raw bash grep output is text; a real compiler would need a small parser (either in-tool or an intermediate function node).

### `interpret-metric-semantics-from-code` (llm, OPTIONAL, from #3-#9's judgment output)
- Preserved: the operator's semantic understanding — what the metric measures, which dimensions SHOULD vary, expected value ranges.
- Generalized: model policy `analytical-small`, bounded evidence (grep matches only, not full file contents), structured output with an `inconclusiveFields` array so partial answers are honest.
- LLM justification (per §32): the transformation *cannot* be reduced to a deterministic parser because the interpretation depends on reading function bodies and comments to derive qualitative claims. A parser would need to know what `LevelMaxAgeCap` means semantically. Deterministic alternative (Option B function extraction) considered and rejected because the operator's judgment was qualitative, not numeric extraction.
- Guessed: output schema (definition/emissionSites/dimensionsShouldVary/expectedRangeHints/inconclusiveFields). The LLM must return an `inconclusive` marker per field per §12.

### `detect-degeneracy` (function, synthesized from operator's reasoning at L118 item 1)
- Preserved: the two-part test — cross-dimension collapse AND within-series flatness.
- Generalized: `withinSeriesFlatnessTolerance` (default 0.001) and `minSeriesForIntegrity` (default 3) are tunable.
- Node kind: **function**, not llm. Same reasoning as v1: this transformation is a deterministic stats calculation (compute per-series stat spread, compute cross-series variance of avg).
- Fixture: `avg=2.014e+04 min=2.014e+04 max=2.014e+04 p99=2.014e+04 count=160` across 9 series (from #20's spill projection).

### `detect-counter-label-dominance` (function, synthesized from operator's reasoning at L118 item 2)
- Preserved: the numeric test — one label value accounts for ≥ threshold fraction of counter samples.
- Generalized: `dominanceLabel` and `dominanceRatioThreshold` inputs.
- Node kind: function. Simple aggregation and ratio compare; no qualitative judgment needed.
- Fixture: "100% of claims in the last 3 hours fired on `trigger='age'`" (from #21's spill projection).
- When gate: only runs if `relatedCounterMetric != null`.

### `degeneracy-condition` (condition, synthesized)
- Restricted expression language per §13. Uses conjunctive rule with `sufficientData` guard so undersized samples yield `false` (not-triggered) rather than declaring degeneracy on insufficient evidence.

### `capacity-starvation-condition` (condition, synthesized)
- Uses `sampleSufficient` guard, dominance flag, AND `!degeneracy-condition.output` (do not blame workload when the metric itself is degenerate — the workload signal is ambiguous under a degenerate measurement).
- When gate matches `query-related-counter-by-labels` and `detect-counter-label-dominance`.

### `emit-degeneracy-finding` (emit, from L118 item 1)
- Preserved: finding narrative — metric appears degenerate; here is the collapse value and dimensioned evidence.
- Generalized: dedupe key includes instance + service + metric; severity ramp based on series count.
- Evidence refs point at node outputs; interpretation and grep matches are OPTIONAL refs (may be null when the code-reading branch didn't run).

### `emit-capacity-starvation-finding` (emit, from L118 item 2)
- Preserved: finding narrative — the *real* correlated signal is workload capacity starvation, characterized by one-label dominance of the trigger counter.
- Generalized: dedupe key on instance + service + counter metric + dominance label; severity ramp based on dominance ratio.

## Stage 5 — validation notes

**Schema (§8 shape):** conforms.

**Referential validity:** every `dependsOn` target exists; every `${nodes.X.output.Y}` reference targets a declared node output. Two references target OPTIONAL nodes: `interpret-metric-semantics-from-code.output` and `locate-metric-emission-code.output.matches`. Both are marked `optional: true` in the evidence array (per §17: runtime must tolerate missing outputs when the upstream was skipped by its `when` gate).

**Graph validity:** acyclic. Roots: `query-metric-timeseries-baseline`, `query-metric-by-dimensions`, `query-related-counter-by-labels`, `locate-metric-emission-code`. `interpret-metric-semantics-from-code` depends on `locate-metric-emission-code`. `detect-degeneracy` depends on `query-metric-by-dimensions`; `detect-counter-label-dominance` on `query-related-counter-by-labels`. Both feed conditions, which gate emits. `capacity-starvation-condition` depends on both `detect-counter-label-dominance` AND `degeneracy-condition` (to enforce "workload only when metric is honest") — this is not a cycle.

**Type validity:** input types declared. Expressions use only functions declared in SKILL-v2's Expression language section: `join(array, separator)` (subset B), ternary in `severityExpression` (subset B), string concatenation via `${...}` interpolation (subset B). No new functions invented. Documented as a spec-clarification-needed item below.

**Semantic drift:**
- Preserved: both load-bearing conclusions from L118 (degeneracy classification + capacity-starvation explanation).
- Preserved (via optional branch): the code-reading judgment can survive as an LLM node when `codeRepoPath` is set.
- Lost: the user's screenshot as visual evidence (correctly replaced by plain inputs per Stage 4.5 Q1).
- Lost: the operator's chat-only reasoning between tool calls (e.g., L53's exposition of "claim_wait_ms is age of the oldest input"). This appears in the LLM node's task description as guidance but is not itself a captured node.

**Honest LLM nodes:** one LLM node in the DAG (`interpret-metric-semantics-from-code`), OPTIONAL, with an explicit justification (see per-node section). All other synthesized transformations are functions.

**Attachment discipline:** applied Stage 4.5. Att-1 (the screenshot) fired Q1 (operator inference became downstream inputs). Recorded as `replaced-by-plain-input` in `provenance.omittedAttachments`. No attachment content text appears in the DAG.

**Spill-projection collapse discipline:** applied Stage 1.5. #20 and #21 collapsed into their preceding query nodes; #16 correctly identified as INCIDENTAL (spill exists, no projection). No spill-projection Bash call remains as its own tool node.

## Stage 6 — iteration

**Round 1** (structural pass with free renaming):
- Renamed `query-metric-timeseries` → `query-metric-timeseries-baseline` for clarity that this is the baseline pattern check, not the primary evidence query.
- Renamed `query-metric-related-counter` → `query-related-counter-by-labels` to say what the node does structurally (query by label breakdown) rather than name its role.
- Split what could have been one `assess-metric-integrity` LLM node into `detect-degeneracy` (function) + `degeneracy-condition` (condition) per §32.
- Added `detect-counter-label-dominance` function and its condition to preserve the L118-item-2 reasoning.
- Added OPTIONAL `interpret-metric-semantics-from-code` LLM node per Stage 2.5 Option A choice.
- **End of Round 1: node IDs FROZEN.**

**Round 2** (semantic fix-up, IDs frozen):
- Added `!degeneracy-condition.output` to `capacity-starvation-condition` so workload conclusions are not drawn from a degenerate metric.
- Added `optional: true` flags on evidence refs pointing at gated nodes.
- No node renames.

**Round 3:** not needed; no unresolved errors.

## Unresolved compiler weaknesses surfaced by this compilation

1. **Expression language spec gap (still unresolved).** SKILL-v2 codifies subset A/B/C but flags this as spec-clarification-needed. The Sentinel uses `join(array, separator)` in tool arguments and finding titles; §13 does not enumerate this. v2 assumes it works.

2. **OPTIONAL LLM branch cost bookkeeping.** The DAG's `maxCost.llmTokens: 10000` is a guess. If `codeRepoPath` is set, the `interpret-metric-semantics-from-code` node consumes budget once per execution. If reviewers judge that unacceptable, they can delete the LLM node without other structural changes — the base without-code-reading Sentinel is a subset.

3. **Two-branch reusable question.** SKILL-v2's Stage 3 guidance covers single-branch procedures well; this compilation genuinely has two coupled classifications (integrity + workload). A future SKILL revision should call out multi-branch investigations explicitly and offer a canonical shape (parallel probes → two conditions → two emits with negative cross-guards).

4. **Function-node fixture generation not addressed.** §31 requires every generated function to have at least one fixture from the original investigation. The Sentinel declares functions but does not yet ship `functions/detect-degeneracy.py` or its fixture. That's Stage 8 (§29) work, out of scope for the spike but flagged so a reviewer isn't surprised.

5. **`emissionSites` shape when the LLM returns inconclusive.** The LLM output schema lists `emissionSites` as required, but a real inconclusive case should probably allow `[]`. Left as-is because a stricter schema forces the LLM to at least emit an empty array rather than omit the field.

6. **Cache policy not declared.** LLM nodes are uncached by default per §19; tool nodes might benefit from caching within a run window. Not declared. A shipping compiler should populate cache policies from the query lookback windows.

## Skill feedback (meta) — where v2 was better, where it still gaps

Where v2 was better than v1:
- Stage 1.5's spill-projection rule made the #20/#21 handling mechanical rather than an editorial call.
- Stage 2.5's decision procedure made the code-reading choice explicit (chose Option A hybrid). v1 chose Option C without labeling that as one choice among alternatives.
- Stage 4.5's attachment chooser Q1/Q2/Q3/Q4 collapsed 30 seconds of "which of the four options is this?" into an answer in one step.
- Stage 6's ID freeze eliminated the temptation to rename mid-iteration.

Where v2 is still thin:
- Expression-language subset B is stated as a pragmatic ruling. The spec should formalize this — v2 skill can only flag, not resolve.
- Two-branch investigations (integrity + workload) don't have a canonical structural template. The DAG shape here is defensible but ad hoc.
- Function-node source and fixture generation is deferred entirely to Stage 8 (per §29), which the SKILL does not cover. A full compiler pass needs a companion skill or extension.

## Answer to the spike's success criterion

If a human reads this rationale + Sentinel, they should be able to say:
- "Yes, this captures BOTH parts of the L118 conclusion — the metric-degeneracy classification AND the workload-pattern explanation."
- "The code-reading survives as an OPTIONAL LLM branch when a checkout is available, so the level-0-vs-level-7 reasoning is not lost."
- "The screenshot is correctly handled as a plain-input replacement (Q1 of the Stage 4.5 chooser), not described as evidence."
- "The spill-projection collapse of #20 and #21 is mechanical, not editorial."

Primary risk area: the two-branch shape has more nodes than v1's single-branch shape (11 vs 6). A reviewer who prefers the "one reusable question" heuristic will argue v1's scope discipline was correct; a reviewer who values fidelity to the original investigation will argue v2 preserved more. Both are defensible; this compilation chose the second.

# Compilation rationale — session A → `metric-anomaly-integrity-check`

**Capture ID:** `e470b9a9-8ccb-4059-a4f0-13da5373b70c` (lakerunner worklane depth spike investigation)
**Skill:** mechanize-compile (spike v0)
**Compiled by:** subagent execution, 2026-08-01

---

## Stage 1 — segmentation summary

| Field | Value |
|---|---|
| Objective (first substantive user text, event L7) | "We just saw a spike in worklane depth for logs, and I found a correlated metric: [Image #1]" |
| Follow-up clarification (L23) | "there is clear correlation with: lakerunner_worklane_claim_wait_ms figure out why that metric spiked" |
| User course-correction (L80) | "this is in the oportun data" (turned out to be wrong — investigation stayed on `prod`) |
| Attachments | 1 image, referenced at event #7 (att-1: image/png, 342856B, sha256:33020f7e704a1493…) |
| Tool calls | 21 total |
| Conclusion (L118) | Classification: the metric is **degenerate** ("`claim_wait_ms` isn't measuring what it looks like it's measuring right now — the number itself is stuck") plus a secondary real-signal explanation (the correlated overdue-depth climb is size-starved ingest). |

The conclusion **classifies** ("this is a degenerate metric emission, not a real spike"). It does not report task actions. This is an investigation, so per stage 3 of the skill flow, compilation proceeds.

---

## Stage 2 — per-tool classification

Ordinals match those in `spike/dag-harvest/out/session-A-report.md`.

| # | Tool | Synthetic capability | Classification | Retained node | Rationale |
|---|---|---|---|---|---|
| 1 | Bash | `bash.grep` | EXPLORATORY | — | Empty result; hypothesis rejected (metric name isn't a bare token in the code). |
| 2 | Bash | `bash.grep` | SUPPORTING | `locate-metric-emission-context` | The broader grep found the emission site. Compiled as an OPTIONAL tool node gated on `codeRepoPath`. |
| 3 | Read | `filesystem.read` | SUPPORTING | — | Read `telemetry.go` for context. Not parameterizable without a "which files should I read" judgment (see §7 caveat below). |
| 4 | Bash | `bash.grep` | SUPPORTING | — | Located the actual recording call. Same not-parameterizable problem as #3. |
| 5 | Read | `filesystem.read` | SUPPORTING | — | Read `manager.go` lines 150–249 for context. Same. |
| 6 | Bash | `bash.grep` | SUPPORTING | — | Located `effectiveMaxAge`/`MaxAge` config; informed the "level-0 vs level-7 should differ" reasoning that the operator applied manually. Encoded implicitly in the `dimensionalBreakdown` + `withinSeriesFlatnessTolerance` inputs. |
| 7 | Bash | `bash.git` | EXPLORATORY | — | Recent commits check. Not cited by conclusion. |
| 8 | Bash | `bash.grep` | SUPPORTING | — | Located phase-latency.md spec. Not parameterizable. |
| 9 | Read | `filesystem.read` | SUPPORTING | — | Read the spec. Not parameterizable. |
| 10 | ToolSearch | (Claude Code meta) | INCIDENTAL | — | Session-runtime tool preparation. |
| 11 | Bash | `bash.kubectl` | FAILED | — | SSO expired. |
| 12 | mcp kube list | `kube.list-resources` | FAILED | — | Empty result (wrong labels). |
| 13 | mcp kube list | `kube.list-resources` | FAILED | — | Empty result (wrong namespace). |
| 14 | mcp kube list | `kube.list-resources` | EXPLORATORY | — | Namespace listing to look for the right one. Not cited by conclusion. |
| 15 | lakerunner list_instances | `observability.list-instances` | EXPLORATORY | — | Collapsed into `instance` as a plain input; explicit discovery step is not part of the reusable procedure. |
| 16 | Bash | `bash.grep` | EXPLORATORY | — | Follow-up to user hint "this is in oportun data"; dead end. |
| 17 | mcp lakerunner query | `observability.query-metrics` | REQUIRED | `query-metric-timeseries` | Confirmed the metric was flat over the lookback (all data-points = 20000). Directly cited in conclusion. |
| 18 | mcp lakerunner query | `observability.query-metrics` | REQUIRED (deferred) | — | The `claim_trigger` rate query. Its summary (extracted by #21) is cited as "100% of claims fired on trigger=age". Excluded from v0.1.0 core DAG to keep the Sentinel focused on the degeneracy detection; see extension point below. |
| 19 | mcp lakerunner query | `observability.query-metrics` | REQUIRED | `query-metric-by-dimensions` | The KEY evidence — avg by (action, level). Cited directly in conclusion. Runtime spill-to-disk is collapsed into the node output; see §6 below. |
| 20 | Bash | `bash.jq` | REQUIRED (collapsed) | — | Reads the spilled summary of #19. Absorbed into `query-metric-by-dimensions.output.summary`. |
| 21 | Bash | `bash.jq` | REQUIRED (collapsed) | — | Reads the spilled summary of #18. Absorbed into the omitted #18 node. |

**Note on F3 (from spike-1 FINDINGS):** the heuristic marks #20 and #21 as meta-tool calls because their inputs reference `~/.claude/projects/`. That rule is **over-broad**. Both calls read tool-result *spill files* Claude Code produced automatically for #18/#19 because those results exceeded the token budget. They are semantic continuations of the queries, not the operator poking the session file. In this DAG they are collapsed into the semantic query-tool node's output; a real compiler should recognize the spill-then-jq pattern and treat it as one logical operation (see §6).

---

## Stage 3 — procedure signature

```
objectiveClass: metric-anomaly-explanation
evidencePattern:
  - metric-time-series
  - dimensional-breakdown
  - (optional) metric-emission-source-code
transformations:
  - query-metric-current-timeseries
  - group-by-dimensions
  - detect-cross-dimension-value-collapse
  - detect-within-series-flatness
judgments:
  - metric-integrity-classification
outputClass: metric-integrity-finding (degenerate | real | inconclusive)
```

This is a coherent, reusable investigation procedure. Compilation proceeds.

---

## Stage 4 — DAG synthesis, per-node

### `query-metric-timeseries` (tool, from #17)

- **From:** tool-call 17 (`mcp__plugin_cardinal_cardinal__lakerunner__execute_metrics_query`).
- **Preserved verbatim:** the query expression *shape* `avg(<metric>{service_name=..., signal=...})`; the aggregation choice (avg over the raw series).
- **Generalized:** `instance`, `metricName`, `serviceName`, `signal`, `lookbackWindow` all became inputs. The literal `lakerunner_worklane_claim_wait_ms` (F9 warned about domain constants that look like inputs) IS the input in this Sentinel — the whole procedure is parameterized over "the metric under investigation".
- **Node kind:** tool. This is a direct external capability call.
- **Guessed:** the output schema (`summary`, `series_total`, `data_points_total`). The captured payload showed a `data_points` array and derived summary text, so this is a reasonable minimal schema. A future compiler should introspect the actual response schema.
- **Capability abstraction:** bound to `observability.query-metrics` rather than the vendor-specific `mcp__plugin_cardinal_cardinal__lakerunner__execute_metrics_query`, per §10.

### `query-metric-by-dimensions` (tool, from #19 with #20 collapsed in)

- **From:** tool-call 19 + tool-call 20. Node output represents the parsed summary of the (spilled) time-series result.
- **Preserved verbatim:** the query shape `avg by (<dims>) (<metric>{...})`; the semantic role — the dimensional integrity probe.
- **Generalized:** `dimensionalBreakdown` is a required array input. In the source session it was `[action, level]` because those are worklane-specific attributes; for another metric it would be different (e.g. `[endpoint, status]` for a latency histogram).
- **Node kind:** tool.
- **Collapsed with #20:** in the source session, this query's raw result exceeded the token budget and was spilled to disk. #20 then ran `jq -r '.summary' <spill-path>` to recover the summary. The Sentinel model collapses this into a single node whose output is the summary; the spill-to-disk mechanism is a Claude Code runtime artifact, not a semantic step. This is an editorial call — the alternative would be a `retrieve-spilled-summary` function node that runs only when the tool result exceeds a threshold. That model is more accurate to what happened but noisy for reuse.
- **Guessed:** the `ddsketches` output field's schema (map of series-attribute-set → summary stats). Derived from the spill file's schema note that Claude Code produced when it warned about size.

### `locate-metric-emission-context` (tool, optional, from #2)

- **From:** tool-call 2 (`grep -rn "claim_wait_ms|ClaimWaitMs|claim_wait" --include="*.go"`). Tool calls 3, 4, 5, 6, 8, 9 all served the same purpose (understand the metric's source) but the operator's exact sequence isn't reproducible without human judgment about "which files to read next." Only the initial locate step is preserved; downstream reading is dropped (see below).
- **Preserved verbatim:** the intent — locate files that mention the metric name.
- **Generalized:** `pattern = ${inputs.metricName}`, `path = ${inputs.codeRepoPath}`, added file globs to widen from Go-only. Gated on `codeRepoPath != null` — this node is optional context, not a hard dependency.
- **Node kind:** tool.
- **Guessed:** output schema of `matches: [{file, line, text}]`. The real bash grep output is raw text; the compiler would need a small parser (either in-tool or a follow-on function node) to structure it. For a spike this is elided.

**Not encoded from #3, #4, #5, #6, #8, #9:** the operator interactively read specific files (`telemetry.go`, `manager.go` lines 150-249, `phase-latency.md`). To reproduce this in a Sentinel would require either (a) an LLM node picking which files to read (LLM justification: "which of these grep matches is the definition site?"), or (b) an operator-supplied input listing files to read. Both are inferior to just providing the grep output as reviewer context and letting a human interpret it, or letting a downstream LLM node consume the grep matches directly. For v0 spike, the emission context is context-only (feeds the emitted finding, does not feed the degeneracy detection). This is a **compression loss** — the original investigation's manual code-reading is what let the operator argue "compact level 7 SHOULD be near 1h per LevelMaxAgeCap in `laneEffectiveMaxAgeExpr`". The Sentinel encodes that judgment via the `withinSeriesFlatnessTolerance` input and the assumption that all dimensions in `dimensionalBreakdown` SHOULD vary.

### `detect-degeneracy` (function, synthesized, from #20/#21 observations)

- **From:** no single tool call. Derived from the pattern the operator noticed in #20's output: "min = max = p50 = p95 = p99 = 20136 for every one of them, across 179 minute-buckets. That is impossible for real observations."
- **Preserved verbatim:** the two-part test — cross-dimension collapse (all series have the same avg) AND within-series flatness (each series has min ≈ max ≈ p50 ≈ p95 ≈ p99).
- **Generalized:** made a numeric tolerance input (`withinSeriesFlatnessTolerance`) because "identical to the microsecond" is what triggered in this case but for other metrics near-identical is enough.
- **Node kind:** **function**, not LLM. Per §32 and the skill's Stage 5 semantic check: this transformation *can* be expressed deterministically (compute per-series stat spread, compute cross-series variance of the avg). No qualitative judgment is needed. An earlier draft used an LLM node but was rejected here because deterministic rules suffice.
- **Guessed:** the tolerance default (0.001) and `minSeriesForIntegrity` default (3). Empirical values from this one session don't justify a specific default; documented as tunable.
- **Fixture from investigation:** the raw ddsketches summary from #19 (via #20) is the primary fixture. The captured `avg=2.014e+04 min=2.014e+04 max=... p99=2.014e+04 count=…` across 9 series is exactly what the function's expected output (crossDimensionCollapse=true, withinSeriesFlat=true) is calibrated against.

### `degeneracy-condition` (condition, synthesized)

- **From:** operator's inference in L118 conclusion.
- **Preserved verbatim:** the conjunctive rule — degeneracy is declared only when both cross-dimension collapse and within-series flatness hold.
- **Generalized:** wrapped in a `sufficientData` guard so the Sentinel reports `inconclusive` rather than "degenerate" when the sample is too small.
- **Node kind:** condition. Restricted expression language per §13.

### `emit-degeneracy-finding` (emit, synthesized)

- **From:** L118 conclusion structure.
- **Preserved verbatim:** the finding narrative — this metric appears degenerate; here is the collapse value and the dimensioned evidence.
- **Generalized:** dedupe key includes instance + service + metric so re-execution against the same target does not spam. Severity ramp based on series count is a placeholder; the real severity model belongs to a Binding.
- **Node kind:** emit.
- **Attachment reference:** none — the source session's screenshot is NOT emitted; only structured attributes and node output references.

### Not-included: extended-signal probe (from #18)

Tool-call 18 (rate of `claim_trigger` grouped by action/level/trigger) produced the "100% of claims fired on trigger=age" observation cited in the conclusion's secondary explanation of the *real* correlated signal (size-starved ingest). It IS required for the full original narrative but is orthogonal to the degeneracy-detection procedure. **v0.1.0 omits it.** A `related-counter-probe` Variation could re-add it as `query-metric-related-counter` (tool) → `detect-under-capacity-pattern` (function) → separate `emit-under-capacity-finding`. The extension point `before-degeneracy-condition` is declared for this Variation.

---

## Stage 5 — validation notes

Ran the four schema-plus-quality checks in-head; documented issues below.

**Schema (§8 shape):** conforms.

**Referential validity:** every `dependsOn` target exists. Every `${nodes.X.output.Y}` reference targets a declared node output. **One caveat:** the `${nodes.locate-metric-emission-context.output.matches}` reference in `emit-degeneracy-finding.evidence` is on a `when`-gated node; the emit-node's evidence carries `optional: true`. The runtime must tolerate a missing upstream output when the upstream was skipped by its `when`. This should be validated by the executor per §17 semantics; the Sentinel encodes the intent correctly.

**Graph validity:** acyclic. `query-metric-timeseries` and `query-metric-by-dimensions` are roots; both feed `detect-degeneracy`; that feeds `degeneracy-condition`; that gates `emit-degeneracy-finding`. `locate-metric-emission-context` is a parallel root feeding only the emit.

**Type validity:** input types are declared. Expressions look plausibly typed. One weakness: `join(inputs.dimensionalBreakdown, ", ")` is used in query expressions — the expression language of §13 lists `contains` and pure functions like `abs, min, max` but does not enumerate string operations. A real compiler needs `join` (and probably `format`) as declared pure functions. Documented as an unresolved spec question.

**Semantic drift:** does the DAG represent the investigation?
- Preserved: the core discovery (metric is degenerate because it collapses across dimensions that should vary).
- Lost: the manual code-reading that let the operator argue *which* dimensions SHOULD vary. The Sentinel makes the operator declare that up front via `dimensionalBreakdown`.
- Lost: the secondary conclusion about the real signal (overdue-depth / size-starved ingest). Documented as a Variation extension point.
- Judgment: the compression is honest — the DAG's answer to "is this a real spike or a degenerate metric?" is the same as the investigation's answer, given the same inputs.

**Honest LLM nodes:** none in the DAG. An earlier draft had an `assess-integrity` LLM node; rejected because the same judgment can be expressed as `crossDimensionCollapse && withinSeriesFlat` over deterministically computed stats. Recorded here so a reviewer sees the choice was considered.

**Attachment discipline:** no attachment content is inlined as evidence. The screenshot (att-1) is recorded in `provenance.omittedAttachments` with `disposition: replaced-by-plain-input` and the reason that `metricName` + `serviceName` inputs carry the equivalent information.

---

## Stage 6 — iteration

One iteration round.

- **Round 1 fixes:**
  - Initial draft had a `select-instance` tool node calling `list-instances` (from #15). Removed because in a Sentinel `instance` is just an input; the discovery step was operator-specific.
  - Initial draft had `assess-integrity` as an `llm` node. Rewritten as `detect-degeneracy` function + `degeneracy-condition` condition per §32.
  - Initial draft included `query-metric-related-counter` (from #18/#21). Moved to a documented Variation extension point to keep the core Sentinel focused on the reusable question ("is this metric anomaly real?").

No unresolved schema errors after round 1.

---

## Unresolved compiler weaknesses surfaced by this compilation

These are the places where the skill's guidance was thin and I made judgment calls that a real compiler would want to formalize:

1. **Spill-to-disk collapsing (F3 refinement).** The spike-1 FINDINGS rule "filter tool calls whose inputs reference `~/.claude/projects/`" is over-broad. Claude Code's automatic tool-result spill produces jq calls against paths under that prefix which ARE legitimate evidence retrieval. The compiler needs to recognize the pattern `tool-result includes "Output has been saved to <path>" → subsequent Bash `jq/cat/python` on `<path>` = one logical operation`. I collapsed these editorially; a real compiler should do this automatically.

2. **Code-reading compression.** Six tool calls (#3, #4, #5, #6, #8, #9) collectively did "understand the metric semantics from the source code." Compiling this to a Sentinel would require either an LLM node with directory access ("read the code and tell me what this metric measures") or an operator-supplied input listing files. I chose the third path — drop the interactive code reading, replace with an OPTIONAL grep node for reviewer context, and encode the *judgment output* (which dimensions should vary, what tolerance means "identical") as inputs. This is a genuine loss of fidelity to the original investigation. The skill's guidance doesn't address this pattern.

3. **Expression language gap.** I used `join(...)` in a query-argument expression, which is not enumerated in §13's condition-expression allowlist. §13 governs condition nodes specifically; tool-argument expressions may have a broader (or entirely different) expression language. The spec should clarify.

4. **Attachment vs. input-inference boundary.** The screenshot at event #7 was the trigger. The operator's inference — "the metric that spiked is `lakerunner_worklane_claim_wait_ms`" — is what became a Sentinel input. I chose to encode this as text inputs (metricName + serviceName) and record the attachment as "replaced-by-plain-input" per §29's second option. But another reasonable choice would have been to require the image as a Sentinel `image` input and let a future multimodal capability extract the metric name at runtime. The skill's guidance lists both options but does not help choose. For a spike I chose the cheaper option; a real compiler would want a policy.

5. **Dead ends as reviewer signal.** F8 from spike-1 said the exploratory dead ends should be preserved in the audit log. I have recorded them all in `provenance.omittedSteps` with reasons, but the skill's mandatory rationale format doesn't otherwise ask for them to be visible to reviewers. If the audit.jsonl were produced (skipped for this spike per SKILL.md), each of these would have a per-event decision record.

6. **Semantic node ID drift.** Between drafts I renamed nodes (`assess-integrity` → `detect-degeneracy`, `select-instance` removed, etc.). If any of these had been declared as variation points in an earlier Sentinel version, Variations would break. The stability guarantee in §9 requires that a real compiler track node-ID history across iteration rounds. For a spike this is moot.

---

## Skill feedback (meta)

Where the skill guided well:
- The mandate to read §52 (do NOT optimize for reuse percentage) shaped every editorial decision. Without it I would have retained #3–#9 as tool nodes to inflate the DAG.
- The concrete example YAML at the top of SKILL.md was more useful than any single §-reference; it anchored the shape.
- Stage 3's "conclusion-shape classifier" rule (past-tense action verbs vs. classifying verbs) was correct and easy to apply.

Where the skill was thin:
- No guidance on the spill-to-disk collapsing pattern (item 1 above). This is common in Claude Code sessions and a compiler will meet it constantly.
- No guidance on how much upstream context to retain (item 2 above). The choice between "encode code-reading as an LLM node" vs. "compress into structured inputs" is load-bearing and undirected.
- The attachment guidance is complete but doesn't help choose between the four options (item 4 above).
- Stage 4's node-ID stability rule is stated but the iteration loop in Stage 6 encourages renaming; the tension between "keep IDs stable across iteration" and "iterate freely" is unresolved (item 6 above).
- SKILL.md does not tell the subagent whether it can/should read `spike/dag-harvest/out/session-A-report.md` (the prior heuristic output). I did read it as background; a future run should either explicitly permit this or require the subagent to work only from the raw JSONL.

Where the skill's guidance is ambiguous:
- "Meaningful IDs > pretty IDs" (§ MUST NOT list, line 249). What counts as meaningful? `detect-degeneracy` vs. `check-collapse` — both describe what the node does. This is style guidance more than a rule.
- "Function nodes represent deterministic transformations that do not already exist as tools" (§11). What if a transformation exists as a tool in some environments but not others? For `detect-degeneracy`, one could imagine a `stats.summary-collapse-detector` tool; I chose function because that tool doesn't exist in this repo's capability inventory today.

---

## Answer to the spike's success criterion

If a human reads this rationale and looks at the Sentinel, they should be able to say:
- "Yes, this is the reusable procedure from the worklane investigation — detect when a histogram's summary is collapsed across dimensions that should vary."
- "The manual code-reading dropped out, and the secondary size-starved-ingest signal moved to a Variation. Both losses are documented."
- "The screenshot was correctly handled as a plain-input replacement rather than being described as evidence."

If instead they say "this DAG doesn't match what I did" — the primary suspect is item 2 above (code-reading compression), where I sacrificed fidelity for parameterizability.

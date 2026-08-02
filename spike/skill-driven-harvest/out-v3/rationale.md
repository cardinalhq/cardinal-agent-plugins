# Rationale — worklane-metric-degeneracy-and-cause (v3)

Compiled from Claude Code session
`e470b9a9-8ccb-4059-a4f0-13da5373b70c` (lakerunner project). Session is
Session A from the spike-1 fixture set (~21 tool calls, one PNG
attachment, no compaction).

This compilation is v3 — first-principles from the JSONL against the
amended `sentinels.md` (§9 kind union + §14a ask_human + §32 amended)
under `SKILL-v2.md`. No previously-emitted output was read before this
document was written.

## Objective (Stage 1)

User pasted a screenshot showing worklane depth for `logs` starting to
climb around 12:00, then said in free text that the correlated metric
is `lakerunner_worklane_claim_wait_ms`, running "in the oportun data",
and asked *why* that metric spiked.

## Conclusion (Stage 1)

The operator concluded, verbatim, two things:

1. **The metric is emitting a degenerate/collapsed histogram**: every
   dimension (`action="ingest"` vs `action="compact"`, levels 0..7)
   shows identical `avg=min=max=p50=p95=p99=20136 ms` for 179+
   minute-buckets. Level-7 compaction should sit near
   `LevelMaxAgeCap = 1h`, not on the same value as level-0 ingest.
   Therefore the "spike" is the metric emission itself, not a
   workload event.
2. **The real correlated signal** is the overdue-depth climb, and
   the trigger-rate breakdown explains why: 100% of ingest claims in
   the window fire on `trigger="age"`, 0% on `trigger="size"` —
   classic age-starved / low-arrival-density.

The conclusion further calls these **two separate phenomena** — the
metric bug does not cause the depth climb; the age-starvation does.
This synthesis is a claim in its own right and is mechanized as its
own analytical node (see `assess-findings-relationship`).

## Procedure signature (Stage 3)

```
objectiveClass: metric-anomaly-explanation
evidencePattern:
  - metric-emission-source-code
  - metric-aggregate-timeseries
  - metric-per-dimension-timeseries
  - sibling-cause-hypothesis-timeseries
transformations:
  - locate-emission-code
  - interpret-metric-semantics
  - detect-histogram-degeneracy
  - classify-trigger-pattern
judgments:
  - metric-semantic-interpretation
  - findings-causal-relationship
outputClass: metric-integrity-plus-workload-cause-finding
```

Conclusion shape passes the SKILL-v2 Stage 3 investigation-vs-task
check: it **classifies** the metric ("this is a degenerate emission,
not a workload event") — present-tense stative form, not "Done, X, Y,
Z." Compilation is appropriate.

## Node inventory

Thirteen nodes: 3 tool (metric queries), 2 tool (code navigation),
2 llm, 2 function, 2 condition, 2 emit.

| Node id | Kind | Derived from ord | Notes |
|---|---|---|---|
| `locate-metric-emission-site` | tool | 26 | `bash.grep -l` for metric name in Go |
| `read-emission-code` | tool | 30, 35 | Read files at the emission sites; two Read calls collapsed into one capability call |
| `interpret-metric-semantics` | llm | 33, 43 (code-reading judgments) | See §32 justification below |
| `query-metric-aggregate` | tool | 94 | `avg(metric{...})` |
| `query-metric-by-dimension` | tool | 106 (+114 collapsed) | `avg by (action, level) (metric{...})` |
| `query-trigger-rate-by-dimension` | tool | 104 (+116 collapsed) | `sum by (action, level, trigger) (rate(trigger[5m]))` |
| `detect-histogram-degeneracy` | function | derived transformation | Deterministic; §32 rule 1 |
| `classify-trigger-pattern` | function | derived transformation | Deterministic; §32 rule 1 |
| `assess-findings-relationship` | llm | operator's synthesis | See §32 justification below |
| `degeneracy-finding-condition` | condition | inferred | Gate on `isDegenerate == true` |
| `starvation-finding-condition` | condition | inferred | Gate on trigger pattern + sample sufficiency |
| `emit-degeneracy-finding` | emit | inferred from conclusion | Type `metric-histogram-degeneracy` |
| `emit-starvation-finding` | emit | inferred from conclusion | Type `worklane-age-starvation` |

## Code-reading option chosen (Stage 2.5)

**Option A — LLM node with directory access.** The code-reading here
was not extracting a single constant (though `LevelMaxAgeCap = 1h` and
`IngestMaxAge = 30s` are named). The operator was making a
**qualitative claim** about *which* dimensions should legitimately
vary (level-0 ingest vs level-7 compact sit on different age gates,
per `laneEffectiveMaxAgeExpr` in `internal/worklane/queries.go`;
`claimWaitHist.Record` in `internal/worklane/manager.go`). A generalist
running this Sentinel against a *different* metric would need to redo
this analysis. Option A preserves that fidelity mechanically.

Fidelity lost vs the source investigation: none material — the LLM
node has to be given the same code the operator read; the emission-code
tool node makes that explicit. A future operator on an unfamiliar
metric gets an LLM-derived semantic claim rather than having to hand-
read the code themselves.

Option B rejected: the code reading is qualitative, not
constant-extraction. Option C rejected: this Sentinel is designed to
generalize across metrics, so encoding the operator's specific
conclusion as an input defeats reuse.

## Analytical-node decisions per amended §32

Every judgment step was evaluated against the three-way rule.

### `detect-histogram-degeneracy` — function

- Deterministic transformation? YES. Given `{avg, min, max, p50,
  p90, p95, p99, count}` per dimension, compute
  `crossDimensionalVariance = stdev(avgs) / mean(avgs)` and
  `identicalStatistics = (min == max == p50 == p95 == p99)` per
  series. Flatness tolerance is a numeric threshold input. All
  arithmetic; no interpretive step.
- Rule 1 fires → **function**. No llm/ask_human justification needed.

### `classify-trigger-pattern` — function

- Deterministic transformation? YES. For each `(action, level)` in
  the trigger breakdown, compute fraction by `trigger` label; the
  lane is age-starved if `ageFraction >= ageStarvationTriggerFraction`
  (input threshold). Sample sufficiency: `count >= N`.
- Rule 1 fires → **function**.

### `interpret-metric-semantics` — llm

- Deterministic transformation? NO. Deciding "which dimensions
  should this metric legitimately vary across, and what tolerance
  reads as flat" requires reading Go function bodies
  (`laneEffectiveMaxAgeExpr`, the `claimWaitHist.Record` call site
  and its attribute set) and interpreting semantic intent from
  identifier names, comments, and control flow. A regex extractor
  is not honest here — it would either invent structure or fail
  silently on renamed identifiers.
  - `deterministicAlternativeConsidered: true`
  - `reasonRejected: >`
      Requires cross-file interpretive reading of Go code
      (`laneEffectiveMaxAgeExpr`, `claimWaitHist.Record` attribute
      derivation) that cannot be reduced to a deterministic
      extraction over the source text.
- LLM-safe?
  - Qualitative in nature? YES — semantic classification of metric
    dimensionality and expected variance from source code.
  - Autonomous delegation acceptable? YES — the downstream consumer
    is a function node that produces a text finding. A wrong
    interpretation produces a wrong `metric-histogram-degeneracy`
    finding, not a destructive action. Findings in v0 emit to
    stdout / JSON file / webhook (§14); no external actuation.
  - `autonomousDelegationAcceptable: true`
  - `reason: >`
      Output flows into a deterministic degeneracy classifier and
      then into a finding-emit path. No destructive downstream.
- Rule 2 fires → **llm**.
- ask_human considered and rejected: the source operator did this
  work by reading code, not by exercising accountability or
  ratifying a high-stakes choice. §14a's "steps where the source
  investigation had a human-in-the-loop moment that a re-run must
  reproduce" applies to *decisions*, not to *information work*. An
  operator running this Sentinel does not need to hand-read the
  metric's emission code themselves — that is exactly the work being
  mechanized.

### `assess-findings-relationship` — llm

- Deterministic transformation? NO. Deciding "are the histogram-
  degeneracy finding and the age-starvation finding two views of
  one root cause, or two independent phenomena that co-occur, or
  is one an emission artifact that should be discounted" is
  qualitative synthesis. A rule ("degenerate AND age-starved →
  same cause" or vice versa) is not honest — the operator's actual
  reasoning was "the metric emission looks broken because
  cross-dimensional identity is impossible for these labels; the
  workload signal is separate because it comes from a live
  counter, not the broken histogram." That chain requires
  interpretive judgment.
  - `deterministicAlternativeConsidered: true`
  - `reasonRejected: >`
      Cross-finding causal-relationship classification requires
      qualitative reasoning about which of two co-occurring
      observations is real vs. artifact; no threshold expresses it.
- LLM-safe?
  - Qualitative? YES.
  - Autonomous delegation acceptable? YES — output annotates the
    two findings' `relationship` field; no destructive downstream.
  - `autonomousDelegationAcceptable: true`
  - `reason: >`
      Downstream is emit-only; misclassification produces a
      mislabeled relationship attribute on findings, not action.
- Rule 2 fires → **llm**.
- ask_human considered and rejected: same reasoning. This is a
  claim, not an accountability call. If a future deployment used
  the Sentinel's output to *auto-file a bug against the emission
  path*, that emit path would be a destructive downstream and the
  §32 rule 2 safety test would fail — at that point, a variant of
  this Sentinel should insert an ask_human ratification node
  before the file-a-bug emit. In v0 it stays llm.

### Cross-source causal LLM-safety test (amended §32)

Per §32's third rule, a judgment that ties evidence from ≥2 tool
nodes into a causal claim must pass both qualitative-nature AND
autonomous-delegation-acceptable. `assess-findings-relationship`
ties evidence from `detect-histogram-degeneracy` (which consumes
two metric-query tool nodes and one llm node) and
`classify-trigger-pattern` (which consumes one metric-query tool
node). Both conditions pass. Recorded above.

## Ask-human enumeration (v3-specific)

The compiler enumerated candidate `ask_human` sites and rejected each:

| Candidate | Rejection reason |
|---|---|
| `interpret-metric-semantics` | LLM-safe (see above); operator's original work was information gathering, not ratification |
| `assess-findings-relationship` | LLM-safe; produces a claim, not an action |
| Instance selection (from ord 83's `list_instances`) | Better encoded as required input `instance`; runtime confirmation would slow every execution for no accountability gain |
| Metric selection | Attachment chooser Q1 fired; encoded as `metricName` input |
| Whether to emit the degeneracy finding | v0 emits are non-destructive (§14: stdout / JSON / webhook). ratification not required |

No `ask_human` node materialized. This is honest for v0-shape emits.
The rationale flags: **when this Sentinel is adapted to a variant
whose emit path is destructive (file-a-bug, page-oncall,
disable-alerting), a Variation MUST insert an `ask_human` node
between the analytical LLM nodes and the destructive emit.** That is
the §14a-honest use of ask_human for this procedure.

## Non-existence of optional analytical nodes (amended §32)

The compiler did not gate any `llm` node with `when:`. Specifically:

- `interpret-metric-semantics` is required. If a future operator
  wants to skip it (they already know which dimensions should
  vary), that is a **Variation**: remove the llm node and add an
  input `dimensionsExpectedToVary: [string]` that feeds
  `detect-histogram-degeneracy` directly. This is exactly §32's
  "operator-supplied input at Binding time" pattern.
- `assess-findings-relationship` is required. If a Variation wants
  to emit both findings without the relationship claim, remove the
  node and any emit-body `${nodes.assess-findings-relationship...}`
  references.
- The two `function` nodes (`detect-histogram-degeneracy` and
  `classify-trigger-pattern`) are also both required, but §32
  permits function-level optionality via `when:` if a future
  variant needs it. Not applied here.

## Attachment handling (Stage 4.5)

One PNG attachment at capture-event ord-8. mimeType `image/png`, size
~342 KB (reported by spike-1 as `sha256:33020f7e704a1493...`).

Applied Q1 first: **did the operator's inference from the attachment
become downstream inputs?** YES. The screenshot's role was to convey
"which metric to investigate" and "the shape of the anomaly (flat
plateau at ~21.5k)." Both extracted claims are now typed inputs:
`metricName`, and the flat-plateau shape is exactly what
`detect-histogram-degeneracy` looks for. `provenance.omittedAttachments[]`
records `disposition: replaced-by-plain-input` with `replacedByInputs:
[metricName, service]`.

Q2 (would a runtime executor genuinely need the image to reproduce
the judgment?) does not fire — no downstream node requires visual
comparison; the degeneracy classifier works from numeric time-series
alone.

Q3 (nice-to-have context?) does not fire — text-based reproducibility
is complete without the screenshot.

Q4 (never describe attachment content as evidence): honored. No
generated text purports to summarize what the screenshot shows.

## Spill-to-disk collapse discipline (Stage 1.5)

Two spill-projection pairs identified:

- Tool `ord-106` (`execute_metrics_query` avg by action/level) →
  spilled to file → `ord-114` (`bash.jq -r '.summary' <spill-path>`).
  The `jq` call reads a path that appears verbatim in ord-106's
  tool_result. Classification: **COLLAPSED** into
  `query-metric-by-dimension`. The node's declared output shape
  retains `summary` as a load-bearing field.
- Tool `ord-104` (`execute_metrics_query` trigger rate) → spilled →
  `ord-116` (`bash.jq -r '.summary' <spill-path>`). Classification:
  **COLLAPSED** into `query-trigger-rate-by-dimension`.

Meta-grep at `ord-86` (`grep oportun /Users/... /Users/.../.claude`)
is NOT a spill-projection — it grepped user history for context. Per
Stage 1.5's discrimination heuristic: no earlier tool_result
mentioned this path. Marked **INCIDENTAL**.

## Full omission ledger (Stage 2)

| Ord | Tool | Class | Reason |
|---|---|---|---|
| 26 | Bash grep | REQUIRED | Emission-site locator — becomes `locate-metric-emission-site` |
| 27 | Bash grep | SUPPORTING | Broader grep for `claim_wait` variants; superseded by ord-33's targeted `claimWaitHist` grep. Omitted from DAG (redundant navigation), retained in audit |
| 30 | Read telemetry.go | REQUIRED | Feeds `read-emission-code` |
| 33 | Bash grep claimWaitHist | REQUIRED | Located the recording site — collapses into `locate-metric-emission-site`'s conceptual role (the operator's first grep was too broad; this one was targeted). Both become the single locator node in compiled form |
| 35 | Read manager.go:150-250 | REQUIRED | The actual `claimWaitHist.Record` code cited in the conclusion. Feeds `read-emission-code` |
| 36 | (tool_result of 35) | — | Not a call |
| 43 | Bash grep effectiveMaxAge | REQUIRED | Located `laneEffectiveMaxAgeExpr` / `LevelMaxAgeCap` — the semantic anchor for "different levels should vary." Feeds `interpret-metric-semantics` as part of the code corpus |
| 46 | Bash git log | EXPLORATORY | Recent worklane changes — did not surface a cause |
| 48 | Bash grep broader metric refs | EXPLORATORY | No additional signal |
| 50 | Read kb/specs/telemetry/phase-latency.md | EXPLORATORY | KB doc scan; not cited in conclusion |
| 55 | ToolSearch (loading MCP schemas) | INCIDENTAL | Meta-work — the operator loading kube+lakerunner tool schemas into their agent |
| 63 | kubectl (bash.kubectl) | FAILED | SSO expired — errored |
| 64 | mcp kube list_resources Pod (label) | EXPLORATORY | 0 pods matching label |
| 68 | mcp kube list_resources Pod (namespace) | EXPLORATORY | Namespace lookup path, abandoned |
| 70 | mcp kube list_resources Namespace | EXPLORATORY | 35 KB namespace list; operator abandoned kube path after this |
| 83 | mcp lakerunner list_instances | SUPPORTING | Operator confirmed instance `prod` covered "oportun" data. In compiled form, `instance` is an input; this call is not needed at run time |
| 86 | Bash grep oportun in ~/.claude | INCIDENTAL | Meta-grep under `~/.claude` per F3/Stage 1.5's INCIDENTAL rule (not a spill-projection) |
| 94 | mcp lakerunner execute_metrics_query (aggregate) | REQUIRED | Becomes `query-metric-aggregate` |
| 104 | mcp lakerunner execute_metrics_query (trigger by dim) | REQUIRED | Becomes `query-trigger-rate-by-dimension` |
| 106 | mcp lakerunner execute_metrics_query (metric by dim) | REQUIRED | Becomes `query-metric-by-dimension` |
| 114 | Bash jq -r .summary (spill projection of 106) | COLLAPSED | Absorbed into `query-metric-by-dimension` |
| 116 | Bash jq -r .summary (spill projection of 104) | COLLAPSED | Absorbed into `query-trigger-rate-by-dimension` |

## Expression-language usage

Uses only Subset B (tool-argument interpolations) per SKILL-v2:

- String interpolation: `${inputs.metricName}`, `${inputs.service}`.
- Arithmetic on times: `${execution.now - inputs.lookbackWindow}`.
- `join(array, sep)` for constructing PromQL-style
  `avg by (...)` clauses from `groupByDimensions` input.
- Ternary in `severityExpression` (permitted per §8 example).
- Subset A only in condition-node `expression:` fields.

No functions invented beyond enumerated.

## Node-ID stability

All 13 node IDs were chosen in Stage 4 and preserved through
validation. No renames after Round 1. IDs pass the "read out loud"
test: each says what the node does.

## Unresolved / spec-clarification-needed

- Array-of-object interpolation in `read-emission-code.config.arguments.paths`
  uses a `[*].path` splat idiom. §13 doesn't enumerate this and
  Subset B doesn't cover it either. Flagged as
  `spec-clarification-needed: array-projection-in-tool-arguments`.
- Object key subscript with a string containing a colon in
  `emit-starvation-finding.severityExpression`
  (`ageFractionByLane["ingest:0"]`) — Subset B doesn't explicitly
  enumerate object indexing. Flagged.

## Success criterion self-check

- `sentinel.yaml` structurally valid against §8 shape.
- Every `dependsOn` target exists; expressions resolve to declared
  nodes/inputs.
- DAG acyclic; outputs reachable.
- Attachment discipline honored (Stage 4.5).
- §32 amended rules honored: three-way selection recorded for every
  judgment; no `when:` gating on any llm/ask_human; ask_human
  considered and rejected with documented reasons.
- Rationale answers "yes, this is what the investigation was."

# REUSE-EVAL.md — Session 93710745 vs. Session A ground-truth Sentinel

Ordering discipline: Phase 1 (`compiled/sentinel.yaml`, `compiled/rationale.md`) was
completed before any file in `spike/skill-driven-harvest/{ground-truth,out,out-v2,out-v3,EVAL.md,SCORING.md}` or Session A's JSONL was read.

## A. Procedure-signature comparison (§25)

Session 93710745 signature (extracted from `compiled/rationale.md` Stage 3):

```yaml
objectiveClass: log-event-root-cause-explanation
evidencePattern:
  - single-log-event
  - emission-site-source-code
  - mint-site-source-code
  - call-chain-context
  - corroborating-live-logs   # optional
transformations:
  - locate-source-string-in-repo
  - extract-enclosing-symbol
  - trace-callers
judgments:
  - causal-code-chain-synthesis
outputClass: root-cause-code-explanation
capabilitiesUsed: [bash.grep, bash.cat, telemetry.execute_logs_query (opt), LLM]
```

Session A GT signature (inferred from `spike/skill-driven-harvest/ground-truth/sentinel.yaml`):

```yaml
objectiveClass: anomaly-explanation
evidencePattern:
  - symptom-metric-timeseries
  - suspected-driver-metric-timeseries
  - driver-metric-timeseries-by-dimensions
  - driver-metric-emission-code-semantics
  - alternative-driver-metric-timeseries
transformations:
  - locate-metric-emission
  - summarize-metric-emission-semantics
  - query-metric-aggregate
  - query-metric-by-dimensions
  - detect-histogram-degeneracy
  - query-alternative-drivers
judgments:
  - metric-emission-semantics-interpretation
  - real-driver-causal-hypothesis
outputClass: correlated-anomaly-explanation (integrity + driver-hypothesis)
capabilitiesUsed: [observability.query-metrics, code.grep, code.read, llm.reason]
```

| Axis | Verdict | Justification |
|------|---------|---------------|
| **objectiveClass** | MISMATCH | Both are "explain a symptom", but A explains a *statistical anomaly on a continuous signal*; 93710745 explains a *discrete emission event*. Different question shape → different retrieval anchor. |
| **evidencePattern** | MISMATCH | Only "source code" appears in both, and it plays DIFFERENT roles: in A, code is read to justify a metric-integrity claim (semantic-code-read); in 93710745, code IS the primary substrate, read to build a call chain. A's dominant evidence is *time series*; 93710745's is *AST-shaped code context*. Zero time series in 93710745. |
| **transformations** | MISMATCH | Superficial match on "locate name in repo" (metric name vs. log message). Downstream is disjoint: A does per-dimension aggregation and histogram-degeneracy detection; 93710745 does symbol extraction and iterative caller-tracing. |
| **judgments** | PARTIAL | Both terminate in an LLM qualitative-reasoning node, but the judgments differ in kind. A: "what SHOULD this metric look like, and given actual data, is it degenerate?" 93710745: "given a call chain, what code path triggers this emission?" Same node kind (llm), incompatible prompts, incompatible output schemas. |
| **outputClass** | MISMATCH | A: `correlated-anomaly-explanation` with integrity boolean + real-driver hypothesis. 93710745: `root-cause-code-explanation` with `codeReferences[]` + `triggeringConditions[]`. Downstream routing consumers differ; dedupe keys differ. |
| **capabilities used** | PARTIAL | Shared: `LLM`, `code.grep`, `code.read`/`bash.cat`. Divergent: A's primary capability is `observability.query-metrics` (heavy, required); 93710745 does not use it at all. 93710745's optional `telemetry.execute_logs_query` has no analog in A. |

**Summary: 4 MISMATCH / 2 PARTIAL / 0 MATCH.**

## B. Variation feasibility (§26)

Node-by-node feasibility of expressing Session 93710745 as a Variation of A's Sentinel:

| A's node | Reusable in 93710745? | Kind of change |
|----------|----------------------|----------------|
| `locate-driver-emission` | Partial — grep for message vs. metric name | Bind: pattern; still a `code.grep` node. |
| `summarize-driver-semantics` (llm) | No | Replace: different prompt, different output schema (`dimensionsExpectedToVary` ≠ `enclosing symbols`). Node replacement, not scalar patch. |
| `query-driver-timeseries` (tool) | No | Disable — no metric query in 93710745. |
| `query-driver-by-dimensions` (tool) | No | Disable. |
| `detect-degeneracy` (function) | No | Replace with `identify-enclosing-symbols` — completely different arity and output schema. |
| `integrity-condition` (condition) | No | Disable. |
| `query-alternative-drivers` (tool) | No | Replace with `trace-callers` — different tool class (metrics vs. grep). |
| `explain-real-signal` (llm) | Partial | Rename → `synthesize-root-cause`; replace prompt; replace output schema. |
| `emit-explanation` (emit) | No | Replace: different finding type, different evidence refs. |
| `emit-inconclusive` (emit) | Yes | Reusable with bind only. |
| `emit-driver-confirmed` (emit) | No | Disable — no analog in 93710745. |

Additional NEW nodes 93710745 needs that A does not have: `resolve-target-service-repo`, `extract-inner-error-phrase`, `grep-outer-message`, `grep-wrapper-prefixes`, `grep-inner-error`, `select-primary-hits`, `read-emission-contexts`, `read-mint-contexts`, `identify-enclosing-symbols`, `trace-callers`, `read-caller-contexts`, `query-corroborating-logs`. That is **12 net-new nodes** requiring `insert-node` operations at extension points that A does not declare.

**Node-replacement estimate:** 8 of A's 11 nodes require replace/disable (~73%). Node-add estimate: 12 new nodes (109% growth). Total structural churn: replacements + insertions swamp reuse.

**Threshold check (§26 rule "no more than 40% of nodes require replacement"):** VIOLATED at 73%.

**Verdict:** the honest overlay is not a Variation. Per §23, forcing this as a Variation would be an unsafe fork mislabelled as reuse; per §22, most patches would target non-variation points that A does not declare (A has no extension point for "insert new tools between locate and query"). A Variation resolver would reject.

## C. §24 matcher prediction

Scoring Session 93710745 as a candidate query against A's Sentinel record:

| §24 criterion | Score (0–1) | Justification |
|---------------|-------------|---------------|
| Semantic similarity of investigation objective | 0.15 | Both "explain a symptom", but symptom types (anomaly on continuous metric vs. discrete log event) are far apart in embedding space. |
| Capability-class overlap | 0.35 | 3 of ~7 total capabilities shared (LLM, code.grep, code.read). Metric-query and log-query classes are disjoint. |
| Evidence-type overlap | 0.15 | Both use source code as SOME evidence. All other evidence types disjoint (time-series vs. call-chain). |
| Conclusion-type similarity | 0.10 | `correlated-anomaly-explanation` vs. `root-cause-code-explanation`. Almost no consumer overlap. |
| Tool compatibility | 0.40 | `code.grep`/`code.read` compatible; `metrics.query-timeseries` incompatible with `telemetry.execute_logs_query`; bash.cat can bind to code.read. |
| Input-concept compatibility | 0.20 | `serviceName` and `codeRepoPath` overlap; `symptomMetric`/`suspectedDriverMetric` have no analog; `logEvent` object has no analog. |

Weighted mean (equal weights) ≈ **0.23**. Well below any reasonable reuse threshold (typical retrieval accept ≥ 0.6).

**§24 verdict: DO-NOT-REUSE.** A is a low-ranked candidate; the correct outcome is `NEW_SENTINEL` at §29 stage 6.

## D. Negative-reuse discipline validated?

**Validated at compile time.**

The Sentinel produced in Phase 1 is structurally distinct from A's:
- Different name (`log-event-root-cause-from-code` vs. `correlated-anomaly-explanation` / `worklane-metric-degeneracy-and-cause`).
- Different node inventory. Only three overlaps by function (`locate-*-emission-site`, one `llm` synthesis node, one `emit` node); everything else is new or replaced.
- Different outputClass, different dedupe key shape, different downstream consumers.
- No `detect-degeneracy`, no `query-metric-*`, no `integrity-condition`, no `histogram` shape anywhere.

The compilation did NOT drift toward A's shape. §40 discipline held at the compiler layer.

Independent evidence: I refused to compile Phase B of Session 93710745 (task execution), which is the *other* form of shape-matching temptation (forcing a shell-script "playbook" into DAG form). Both refusals — B not being an investigation, and 93710745's investigation not being A's procedure — are the correct §40 outcomes.

## E. Meta-report

### E.1 Compiler bias check — where SKILL-v2 tempted me

- **Metric-query-shape temptation.** SKILL-v2's example Sentinel is the deployment-error-regression shape (baseline window / current window / comparison). The log-corroboration step in 93710745 IS a windowed telemetry query. I could have named it `query-baseline-logs` / `query-current-logs` and framed it as a baseline/comparison. **§40 redirect:** the source session did NOT do a baseline/current split — it did four independent probes searching for ANY corroborating event in a wide window. Framing that as baseline/current would have been synthesis-invented, not compilation.
- **Optional-vs-required LLM temptation.** I initially drafted a `confirm-target-instance` ask_human node with `when: telemetryInstance == null`. §32's "no optional analytical" rule caught this before I emitted. Removed it, exposed as extension point. If I hadn't re-read §32, I would have shipped a spec-violating base Sentinel.
- **Bounded-hops-as-tool-loop temptation.** I nearly modeled `trace-callers` as an LLM-driven agent with grep tool access ("the LLM iterates"). §12's "prohibit undisclosed tool calls" caught this. Replaced with fixed function+tool nodes and a `maxCallChainHops` input bound.

### E.2 Spec/SKILL gaps surfaced by compiling a second session

Session A alone couldn't surface any of these — they only appear when a *different-shape* investigation forces different modeling choices:

1. **Mixed-phase sessions.** SKILL Stage 3 treats a session as compile-or-refuse. This session is compile-part / refuse-part (investigation followed by task execution). The spec has no notion of "compile prefix, drop suffix." I ad-hoc'd it in rationale; real spec needs a Stage 2 segmentation output that names phase boundaries.
2. **Code-reading option D.** Stage 2.5's A/B/C don't cover the case where code IS the entire evidence base. My "Option A-modified" (deterministic grep+cat tool nodes driven by function-extracted symbol lists, feeding a synthesis LLM) is a fourth pattern that should be enumerated.
3. **Bounded-iteration budget as input.** `maxCallChainHops` is a soft budget for "how many levels of call tracing". Metric-shaped Sentinels don't need this. Real spec should describe trace-budget/depth-budget input conventions and where the bound is enforced.
4. **Expression language subset B is missing constructs I actually needed.** `quote(...)`, `length(...)`, iteration across an array to build a shell command line (`for h in ${format(...)}`), and safe path/line arithmetic on grep-result records. I flagged these `spec-clarification-needed` in the rationale rather than pretend the grammar covers them.
5. **`ask_human` interacts awkwardly with `optional` telemetry.** §14a is a clean node kind, but §32's non-optional-analytical rule combined with "operators sometimes want a ratification gate" pushes ask_human into Variation-only territory. That's defensible, but the spec should say so explicitly, because the natural authoring instinct is `when: someInput == null` and that instinct is forbidden.
6. **Non-Go-only assumptions.** My `identify-enclosing-symbols` is regex-Go. This Sentinel isn't portable across languages without a Variation. Session A's per-dimension aggregation is language-neutral. The spec's "capability abstraction" idea (§10) applies to tools; there's no equivalent for language-parsing helpers embedded in function nodes. Gap.

### E.3 Ask-human materialization

Yes — two candidate ask_human moments materialized in this session, neither embedded in the base Sentinel:

- **Override user-supplied `telemetryInstance`** when the tool refuses it. Rejected as ask_human: the source session's override was itself a compliance failure with the user's explicit "use this exact slug" directive. The Sentinel should NOT reproduce that failure. Right behavior: `telemetryInstance` is a binding-time input; if the tool refuses it, `query-corroborating-logs` returns `status: unavailable` and downstream proceeds code-only. No human prompt at runtime.
- **Ratify the code-only diagnosis before emission.** Legitimate ask_human moment (accountability, judgment). Not embedded because §32 forbids optional analytical nodes and forcing every unattended execution through a human gate defeats scheduling. Exposed as the declared `before-emit-finding` extension point; a `paranoid-review` Variation can insert an ask_human there without patching a non-variation point.

Log-event investigations do NOT structurally have more human-in-loop moments than metric ones. What differs is that the "which instance" and "which repo" decisions are more consequential in log investigations because they can silently target the wrong system. The Sentinel handles both by lifting them to binding-time inputs, keeping runtime ask_human as a Variation opt-in.

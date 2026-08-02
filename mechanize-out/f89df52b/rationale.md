# Compilation rationale — service-health-assessment

Session compiled: `f89df52b-607a-4081-a808-2c42c8e3ff02.jsonl`
Investigation objective (first user text): "let's figure out the service health for lakerunner process logs"
Investigation phase: JSONL lines 1–75 (everything before the `/mechanize` invocation on line 76).

## Segmentation summary

- 16 tool_use blocks in the investigation phase.
- No attachments the operator saw (all `attachment` events were `hook_success` records from the Cardinal instrumentation harness, not image/pdf/binary content the model consumed).
- No `Bash` calls, no spill-to-disk pairs — every tool was an MCP-hosted `lakerunner.*` call plus two `ToolSearch` schema-loads.
- No code reading (no Read/Grep/Edit calls) — Stage 2.5 is N/A. Recorded below.
- Conclusion form: "healthy" — present-tense stative classification. This is an investigation, not task execution. §40 does NOT fire.

## Procedure signature (Stage 3)

```
objectiveClass: service-health-assessment
evidencePattern:
  - service-inventory-lookup
  - error-count-summary
  - log-level-distribution
  - log-body-error-count
  - kubernetes-availability-metric
  - container-uptime-metric
  - container-resource-limit-utilization-metric
transformations:
  - threshold-based-signal-classification
  - error-count-reconciliation
judgments:
  - overall-health-classification
  - error-source-consistency
outputClass:
  - primary: service-health-report
  - conditional-findings: [health-degraded, error-count-mismatch]
```

## Stage 2 classification (all 16 tool calls)

Numbering matches source-message order.

| # | Tool | Classification | Node in DAG | Reason |
|---|---|---|---|---|
| 1 | `ToolSearch` (load list_instances, list_services, error_overview, discover_service_graph) | INCIDENTAL | — | Harness schema-loading; not part of the investigation semantics. |
| 2 | `lakerunner.list_services` (filter service_name~=process-logs) | REQUIRED | `resolve-service` | Canonical service name (`lakerunner-process-logs`) flowed into every subsequent call. |
| 3 | `lakerunner.error_overview` (skip_lookback=true) | REQUIRED | `query-error-overview` | Produced the `total_errors: 321` that anchors the reconciliation caveat. |
| 4 | `lakerunner.error_overview` (no skip_lookback) | EXPLORATORY | — | Retry without the flag to see if lookback populated the breakdown. Same null result. Refuted the hypothesis "lookback will fill in `errors`." |
| 5 | `ToolSearch` (load execute_logs_query, discover_metrics, execute_metrics_query) | INCIDENTAL | — | Harness schema-loading. |
| 6 | `execute_logs_query` `{level=~"(?i)err.*"}` | EXPLORATORY | — | Probed "does the level label expose an err bucket?" `deadend: true` refuted it. |
| 7 | `discover_metrics(question=..., filters=...)` | EXPLORATORY | — | Refined semantic search for error/latency/cpu/memory metrics returned `{}`. Superseded by call #12. |
| 8 | `execute_logs_query` `|~ "(?i)error|fail|panic"`, 3h | EXPLORATORY | — | Retry with broader regex, still deadend. Same hypothesis being probed as #6, #10, #11. |
| 9 | `execute_logs_query` `sum by (level) count_over_time(...[1h])`, 6h | REQUIRED | `count-logs-by-level` | Produced the "3700 INFO/min, no ERROR bucket" evidence cited in the conclusion. |
| 10 | `execute_logs_query` `sum(count_over_time(... |~ error\|panic\|fatal\|failed\|failure)[5m])`, 6h | REQUIRED | `count-body-error-terms` | Deadend result. **Retained** because its null count is what makes the reconciliation caveat load-bearing: 321 reported errors vs 0 body matches. |
| 11 | `execute_logs_query` `|~ error\|panic\|fatal\|failed\|failure` (raw events), 1h | EXPLORATORY | — | Same hypothesis as #10 but returning raw events; #10's aggregated form is what the Sentinel needs. |
| 12 | `discover_metrics(filters only)` | SUPPORTING | omitted | Orientation for the operator — enumerated available metrics so #13–16 could be chosen. At Sentinel runtime the four metric names are known constants; re-discovery adds cost without changing the DAG. Recorded in audit; not emitted. |
| 13 | `execute_metrics_query(k8s_deployment_available, min)` | REQUIRED | `measure-deployment-availability` | "1 replica up 100% of window" — one of the four health signals. |
| 14 | `execute_metrics_query(container_uptime, min by pod)` | REQUIRED | `measure-pod-uptime` | Monotonic uptime → no restarts; cited in conclusion. |
| 15 | `execute_metrics_query(k8s_container_cpu_limit_utilization, max)` | REQUIRED | `measure-cpu-utilization` | CPU headroom signal. |
| 16 | `execute_metrics_query(k8s_container_memory_limit_utilization, max)` | REQUIRED | `measure-memory-utilization` | Memory headroom signal. |

Retained (in the DAG): 8 source calls → 8 tool nodes.
EXPLORATORY (recorded, omitted from DAG): 5.
SUPPORTING (recorded, omitted from DAG): 1.
INCIDENTAL (harness, omitted): 2.

## Judgment call worth flagging

**Call #10 sits between EXPLORATORY and REQUIRED.** The tool response was `deadend: true` — the same signature as calls #6, #8, #11 which I classified EXPLORATORY. The distinction: #10 is the *aggregated* form (a `sum(count_over_time)` that yields a single scalar), whereas #6, #8, #11 return raw event streams. The operator's caveat depends on *the count being zero* — that's a mechanical fact a Sentinel can re-check. So #10 stays in the DAG as REQUIRED for the reconciliation branch, and the deadend result is treated as a legitimate "zero matches" outcome, not a tool failure.

If the lakerunner tool starts distinguishing "no data" from "query error" more explicitly, `count-body-error-terms` will need its output schema tightened. Right now it accepts any object.

## Stage 2.5 — Code-reading option chosen

**N/A.** The source investigation did no code reading. All 16 tool calls were external observability queries. No Grep/Read/Edit/Bash-jq happened. This section is recorded for completeness — a reviewer glancing at "no Option A/B/C mentioned" should not read that as "the compiler forgot."

## Stage 4.5 — Attachment handling

**N/A — no operator-visible attachments in the investigation.** The session file contains `attachment` events, but every one is a `hook_success` payload emitted by the Cardinal instrumentation harness (`SessionStart:startup`, initiative convention reminder, spend-band advisory). These are system-injected context text, not image/PDF/binary artifacts the model reasoned about visually. The Stage 4.5 chooser has nothing to fire on.

Recorded under the four-option taxonomy: **omitted, not visible to the investigation reasoning**.

## §32 analytical-node selection

Two analytical nodes were created; both `function`. No `llm`, no `ask_human`.

- **`compute-health-summary`** — every signal in the operator's health call was a threshold check:
  - "1 replica up 100% of window" → `minReplicas >= inputs.minAvailableReplicas`
  - "uptime monotonically increasing" → `min(diff(uptime)) >= 0` per pod
  - "cpu peak 49%" → `peak < inputs.cpuLimitPeakThreshold`
  - "memory peak 64%" → `peak < inputs.memoryLimitPeakThreshold`
  - "no ERROR/WARN buckets" → `!("ERROR" in distribution.keys) && !("WARN" in distribution.keys)`

  The conjunction "healthy iff all signals healthy" is boolean AND. Deterministic → `function`. Not `llm`.

- **`compute-error-count-reconciliation`** — arithmetic comparison: `errorOverview.total > 0 && bodyErrorCount == 0`. Deterministic → `function`. Not `llm`.

`pick-resolved-service` is also a `function`, but it's a mechanical projection (pick the first element of `resolved.services`), not an analytical judgment. Its existence is a Round 1 fix, not a judgment call — see below.

There are no `when:` gates on any `function` node whose input isn't optional. There are no gates on `llm`/`ask_human` nodes because those nodes don't exist.

## Round 1 iteration (Stage 6)

**Single fix:** the source investigation's canonical service name (`lakerunner-process-logs`) was returned inside `list_services.output.services[0].name`. My first draft used `${nodes.resolve-service.output.services[0].name}` — array indexing that is not documented in the §13 condition-node grammar or the skill's tool-argument-expression subset (B). Rather than flag a spec-clarification-needed and hope, I added a `pick-resolved-service` function node that reads the array and returns `{name, candidateCount}`. Downstream nodes reference `${nodes.pick-resolved-service.output.name}`.

Tradeoffs:
- Adds a node the source investigation didn't have.
- If `services.length > 1` (an ambiguous service_name substring), the function currently picks the first — the source investigation implicitly assumed a unique match (`total_count: 1`). The function should probably fail-loud when `candidateCount > 1`; leaving that to the function's implementation, not to the Sentinel spec.
- If it turns out array indexing IS in the expression grammar (Subset B is a skill-defined pragmatism, not an §13 statement), this node is over-engineering.

Node IDs frozen after Round 1 per Stage 6 procedure. No further ID changes.

## Variation-point choices

Exposed:
- `inputs.instance` (bind) — for cross-instance use (e.g., `otel-demo`)
- `inputs.serviceQuery` (bind) — for cross-service use
- `inputs.window/cpuLimitPeakThreshold/memoryLimitPeakThreshold/minAvailableReplicas/default` (replace) — for team-specific thresholds
- Three `toolRef` replace-binding points (`resolve-service`, `query-error-overview`, `measure-deployment-availability`) — to swap the lakerunner backend for a compatible-shaped tool (Prometheus, Datadog, etc.)

Not exposed as variation points but reasonable candidates a Variation could patch anyway:
- `execution.failureMode` (currently `continue-independent` so that a metric-query failure in one branch doesn't sink the whole assessment)
- `count-logs-by-level.config.arguments.query` — for teams whose logs don't carry a `level` label

## Fidelity losses worth naming

1. **The four EXPLORATORY log-body queries are erased.** A future operator running this Sentinel won't see the trail of "we tried three regex shapes before landing on the aggregated `sum(count_over_time)` form." The audit records their existence, but the runtime user sees only the winning shape.
2. **The `discover_metrics` orientation step is erased.** A first-time user of this Sentinel against an unfamiliar metrics backend won't get the "here's what's available" list; the Sentinel assumes the four metric names exist. If they don't (or are named differently on another provider), the tool nodes fail.
3. **The `severity` distinction (critical vs warning) inside `emit-health-finding`** is derived from `compute-health-summary.overall`, but the source investigation didn't emit any severity — it said "healthy" once. The severity ladder in the emit expression is inferred, not observed.
4. **The reconciliation finding severity is hardcoded `info`.** The source investigation phrased the caveat as "not currently affecting availability" — I picked `info` to match. A future team might reasonably want `warning` if `total_errors` is high in absolute terms.

## Spec-question flag

- Skill's Subset B (tool-argument expression grammar) says "the spec (§13) does not explicitly enumerate a tool-argument expression grammar." **I avoided the ambiguity entirely** by using a function node instead of array indexing. Flagging here anyway: if a future skill iteration commits to including index access (`x[0].y`) in the grammar, `pick-resolved-service` becomes deletable.

## Unresolved

- None that block Stage 7 emit. The `severityExpression` inside `emit-health-finding` uses ternary syntax matching the §8 example (`x ? "a" : "b"`), which the skill's Subset B endorses. Should the runtime not support that, the emit-node can fall back to a fixed `severity: warning` and a separate finding-type for critical.

## Files emitted

- `sentinel.yaml` — the compiled candidate.
- `rationale.md` — this file.

Not emitted:
- `audit.jsonl` — the classification table above serves as the audit for this spike; the machine-readable form is deferred until the executor exists to consume it.
- Function source (`functions/pick-resolved-service.mjs`, `compute-health-summary.mjs`, `compute-error-count-reconciliation.mjs`) — this compiler is spike-quality; function synthesis (§31) is out of scope for this pass. The Sentinel declares the entrypoints and I/O contracts; hand-writing the three functions is the executor-integration step.

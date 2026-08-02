# rationale.md — session 93710745 → log-event-root-cause-from-code

## Session under compilation
- Path: `~/.claude/projects/<lakerunner-cwd>/93710745-d81f-4c90-897a-8b4e1f50b721.jsonl`
- 373 events; 98 tool_use / 98 tool_result; 0 attachments; 0 spill-to-disk markers.
- Cwd: `<lakerunner-repo>`.

## Stage 1 — Segmentation

The session has TWO structurally distinct phases:

**Phase A — investigation (tool-uses 1–32, lines 19–121, first ~1/3 of session).**
User prompt: "Investigate this ERROR log event: <verbatim log with message/labels/timestamp>. Also search the code for this log message and figure out … why this may be happening, and what's a fix. … identify the service(s) involved and start any code searches in the repo that backs them. If unsure which repo, list available repos rather than guessing." Final assistant text (line 124) is an EXPLANATION: "`queryapi/metrics_evaluator.go:548` is the last funnel. Working backward: 1. `pushdown_artifact.go:359` wraps the underlying dispatch error as `worker-batch (N segments) dispatch: <err>`. …". Present-tense stative verbs; cites file:line evidence. Investigation-shaped per SKILL Stage 3 and F4.

**Phase B — task execution (tool-uses 33–98, lines 136+).**
Follow-up user prompts: "come up with a reasonable fix", "file a github issue, then make a PR against it, assign it to Michael (skandragon)", "revert and reset go back to main". Assistant conclusion (line 368) is a REPORT of actions: "Reverted. Working tree clean on `main`, `fix/worker-disconnect-retry` deleted. Issue #1162 is still open …". Past-tense action verbs; F4 task-execution shape. **Refused for compilation.**

Compilation proceeds against Phase A only. Phase B is recorded in `omittedSteps` with reason `task-execution-phase-not-part-of-investigation`.

## Stage 1.5 — Spill-to-disk

None. No tool_result carried an `Output has been saved to …` marker; no follow-up bash call reads a `~/.claude/projects/.../session-*.json` spill path. Refined-F3 rule does not fire. Skipped.

## Stage 2 — Per-tool classification (Phase A)

| ord | tool | classification | note |
|-----|------|----------------|------|
| 1 | bash.grep "segment failures in metrics group" | REQUIRED | locates emission site |
| 2 | bash.grep "no workers available for reassignment" | REQUIRED | locates mint site of inner error |
| 3 | bash.grep "worker-batch" | REQUIRED | locates wrapper prefix format string |
| 4 | Read workmanager/manager.go | REQUIRED | emission-site context |
| 5–12 | bash.grep for DisconnectWorker variants + Read manager.go | SUPPORTING | 8 micro-probes tracing one function. **Collapsed** in the DAG into `trace-callers` + `identify-enclosing-symbols`. The source investigation ran these iteratively because the operator was guessing at symbol names; a compiled DAG can run the grep once with an alternation of symbol names extracted from the emission snippets. |
| 13 | Read coordinator.go | REQUIRED | caller-hop 1 evidence |
| 14 | Read coordinator.go (repeat) | COLLAPSED | same file, different offset; represented as one context read |
| 15 | bash.grep AssignByRendezvous\|AllWorkers | SUPPORTING (collapsed) | absorbed into `trace-callers` |
| 16 | Read assignment.go | REQUIRED | caller-hop 2 evidence |
| 17 | Read metrics_evaluator.go | REQUIRED | outermost caller — pushdown funnel |
| 18 | Read pushdown_artifact.go | REQUIRED | wrapper site (produces the outer prefix) |
| 19 | bash.grep DispatchAndWaitToWorker | SUPPORTING (collapsed) | absorbed into `identify-enclosing-symbols` |
| 20 | Read workmanager/manager.go | REQUIRED | dispatch-loop context |
| 21 | Read core/workcoord/worker_state.go | REQUIRED | IsAvailable() gate |
| 22 | bash.grep HandleWorkerDisconnected | SUPPORTING (collapsed) | absorbed into `trace-callers` |
| 23 | Read workmanager/manager.go | REQUIRED | HandleWorkerDisconnected body |
| 24 | bash.grep OnWorkerDisconnected | SUPPORTING (collapsed) | absorbed into `trace-callers` |
| 25 | Read streammanager/manager.go | REQUIRED | event source: gRPC stream teardown |
| 26 | ToolSearch (load MCP schemas) | INCIDENTAL | tool-fetching mechanic, not investigation |
| 27 | MCP execute_logs_query instance=cardinalhq | FAILED (surfaced) | tool returned `{"error":"instance_required","availableInstances":["otel-demo","prod"]}`. Retained in provenance because the FAILURE informed the operator's choice to fall back to code-only. In the DAG this shape is expressed by the `query-corroborating-logs.when: telemetryInstance != null` gate plus the `status: unavailable` enum in that node's output schema. |
| 28 | ToolSearch (list_instances) | INCIDENTAL | tool-fetching |
| 29 | MCP execute_logs_query prod (worker stream events) | SUPPORTING | returned `deadend:true`; became load-bearing evidence that live logs did not corroborate |
| 30 | MCP execute_logs_query prod (single worker ID) | EXPLORATORY | one-off probe on a specific worker ID; not generalizable; omitted from DAG |
| 31 | (same as 30, next probe) | EXPLORATORY | not generalizable; omitted |
| 32 | MCP execute_logs_query prod (frequency question) | SUPPORTING | wider time window, same result: deadend. Retained conceptually as the "was this common?" probe absorbed into `query-corroborating-logs`'s single wide query. |

Nothing was marked LOCAL_ONLY because the sole local-only fact (which repo backs which service) is lifted to the required `serviceRepoMap` input at Binding time.

## Stage 2.5 — Code-reading option chosen

**Option A-modified: expose code reading as concrete `bash.grep` + `bash.cat` tool nodes driven by function-extracted symbol lists; downstream `synthesize-root-cause` LLM node does the qualitative synthesis over the retrieved snippets.**

The entire investigation IS code reading. Option C (compress operator's conclusion into structured inputs) is untenable: the operator's conclusion IS the DAG's output. Pure Option A (a single LLM node with directory access) is disallowed by §12's "prohibit undisclosed tool calls" and would concentrate all judgment in one opaque box.

What is preserved with the hybrid:
- The grep patterns are derived from the log event itself (message, wrapperPrefixes, innerErrorPhrase). No prior operator judgment sneaks in.
- Symbol extraction from snippets is a `function` node; deterministic.
- One `trace-callers` hop is a `tool` node; deterministic re-run.
- Only the final "given all this code, what's the root cause?" step is `llm`, matching where the source investigation's own thinking was irreducibly qualitative.

**Fidelity loss recorded.** The source session did MORE THAN 2 hops of caller-tracing (message → mint → HandleWorkerDisconnected → OnWorkerDisconnected → runWorkerStream cleanup path). The DAG's `maxCallChainHops` default is 2, which would truncate before reaching `streammanager/manager.go` in this specific case. A Variation would need `maxCallChainHops: 3` for this exact log. Flagged as a compilation weakness rather than papered over.

## Stage 3 — Procedure signature

```yaml
objectiveClass: log-event-root-cause-explanation
evidencePattern:
  - single-log-event                  # the concrete incident
  - emission-site-source-code         # grep for message
  - mint-site-source-code             # grep for inner error
  - call-chain-context                # bounded caller-hop reads
  - corroborating-live-logs           # optional; often empty
transformations:
  - locate-source-string-in-repo
  - extract-enclosing-symbol
  - trace-callers
judgments:
  - causal-code-chain-synthesis
outputClass: root-cause-code-explanation
capabilitiesUsed:
  - bash.grep
  - bash.cat
  - telemetry.execute_logs_query (optional)
  - LLM (analytical-medium)
```

Distinguishing feature vs. the metric-anomaly triage shape: this procedure's PRIMARY evidence pattern is **source code**, not time-series. There is no "baseline window / current window / comparison" structure because there is no signal being compared — there is one event, and the question is "where in the code is this fired and what upstream event triggers that path?"

## Stage 4 — Node choices & justifications

- **`resolve-target-service-repo` (function).** Deterministic lookup in `serviceRepoMap` keyed by the log event's `service_name`. §32 rule 1 (deterministic transformation).
- **`extract-inner-error-phrase` (function).** Deterministic textual extraction from `logEvent.labels.err`: strip counts (`(50 segments)`), UUIDs, dispatch-worker prefixes; keep the atomic phrase. §32 rule 1.
- **`grep-outer-message`, `grep-wrapper-prefixes`, `grep-inner-error` (tools).** External invocations of `bash.grep` with declarable arguments. §10 satisfied.
- **`select-primary-hits` (function).** Rank hits: prefer non-test files, prefer hits inside the target service's source tree, deduplicate. Deterministic. §32 rule 1.
- **`read-emission-contexts`, `read-mint-contexts` (tools).** `bash.cat` with sed windows around each hit. External tools, declarable args.
- **`identify-enclosing-symbols` (function).** Regex-based Go-only symbol extraction from snippets. Deterministic. Explicit v0 scope limitation.
- **`trace-callers` (tool).** Single `bash.grep` for symbol alternation. External; declarable.
- **`read-caller-contexts` (function).** Bounded by `maxCallChainHops`; returns snippet map. Deterministic.
- **`query-corroborating-logs` (tool).** Optional; gated by `when: inputs.telemetryInstance != null`. Tool nodes with `when:` are allowed per §32 (optional-analytical prohibition applies to llm/ask_human only).
- **`synthesize-root-cause` (llm).** The one irreducible judgment. `judgmentJustification` recorded per §32. Output schema supports `inconclusive`.
- **`emit-finding` (emit).** Gated on `classification != 'inconclusive'`.

## Attachment handling

Session has zero attachments. No Stage 4.5 disposition required.

## Ask-human considered

The new §14a node kind was considered for two moments in the source session:

1. **Overriding the user-supplied `cardinalhq` instance to `prod`.** The operator did this by hand after `list_instances` returned `[otel-demo, prod]`. This LOOKS like an ask_human candidate (accountability moment, high-stakes choice). Rejected because:
   - It's actually a compliance failure in the source session: the user explicitly said "use this exact slug (do not pick a different instance)". A Sentinel that silently re-derives an instance would repeat the failure.
   - The right base-Sentinel behavior is: `telemetryInstance` is a binding-time input; if the operator supplies one and the tool refuses it, `query-corroborating-logs` returns `status: unavailable` and the DAG proceeds. No ask_human.
   - A stricter Variation may add an ask_human between `resolve-target-service-repo` and `query-corroborating-logs` at the declared extension point.

2. **Ratifying the code-only diagnosis before emission.** The source operator delivered a code-only diagnosis when live logs came back empty. A more cautious operator might want to ratify. This IS a legitimate ask_human moment.
   - Not embedded in base Sentinel: §32 forbids optional analytical nodes, and forcing every execution through a human-ratification gate would break unattended scheduling — which is the primary v1 use-case.
   - Exposed as the `before-emit-finding` extension point, with a note in `variationPoints` that Variations can insert an ask_human there.

## Unresolved / spec-clarification-needed

- **Expression subset B (§13 vs SKILL "Expression language").** I used `${quote(...)}`, `${join(array, sep)}`, `${format(array, template)}`, `${length(...)}`, `${x[0].line-30}` (arithmetic on indexed field access). SKILL Subset B enumerates `join` and `format` and time arithmetic; `quote`, `length`, and shell-line generation via `for h in ${format(...)}` push past the enumerated set. **spec-clarification-needed.**
- **Non-Go codebases.** `identify-enclosing-symbols` is Go-only in v0. If this Sentinel is retargeted at a Python/Rust/TS log emitter, the symbol-extraction function needs a Variation (replace-node), and the extension point should possibly cover that too.
- **maxCallChainHops default = 2 is insufficient for the source session (needed 3).** Documented in Stage 2.5 fidelity loss.
- **The `synthesize-root-cause` LLM will need to cite file:line ONLY from supplied snippets** — schema enforces `codeReferences[].file/line` but does not enforce the LLM only uses supplied snippets. Runtime validation would need a hash-check that cited references appear in the snippet payloads. Deferred.

## Compilation not-forced

I did NOT compile Phase B (task execution). Attempting to would have produced a `git-and-gh-commands` playbook masquerading as a Sentinel, exactly the F4 failure mode. The refusal is not a bug; it's the classifier working. See `omittedSteps: tool-uses-33-98`.

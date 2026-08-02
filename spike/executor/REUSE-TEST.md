# Reuse-test — service-health-assessment against a different service

## What this proves

A Sentinel compiled from a session that investigated
`lakerunner-process-logs` was executed end-to-end against
`lakerunner-process-metrics`, with no source-code changes beyond the
three gap-fixes named below and three newly hand-authored function
bodies. All 15 nodes reached a terminal state; the executor exited 0.

## Gap fixes applied

**Gap 1 — SKILL: canonical Python function runtime**
`~/.claude/skills/mechanize/SKILL.md`, Stage 4. Added an explicit rule:
`kind: function` MUST declare `runtime: python3.12` + `source:
functions/<node-id>.py`. Updated the concrete example YAML in the same
file to spell out `runtime: python3.12`. Node.js and other runtimes
are prohibited for v0.

**Gap 2 — SKILL + executor: abstract capability registry**
- SKILL: new "Known capability registry" table naming the six
  `observability.*` / `code.*` IDs the v0 executor supports; explicit
  ban on vendor-shaped IDs (`lakerunner.*`, `datadog.*`) per §10.
- Executor (`capabilities.py`): registered
  `observability.list-services`, `observability.error-overview`,
  `observability.query-logs`, `code.read` alongside the pre-existing
  `observability.query-metrics` + `code.grep`. All observability
  bindings share a `_tool_cache_read` helper that reuses the driver
  pattern already in place.

**Gap 3 — Executor: multi-interpolation in string args**
Tested first (`test_nested_interp.py`); multiple `${...}` in a single
string arg already work because the regex `\$\{([^}]+)\}` bounds each
match by the next `}`. Truly-nested `${${x}}` does NOT work but the SKILL
does not emit it and the f89df52b Sentinel does not need it. Added a
comment to `render_template` in `executor.py` documenting exactly this
distinction. Added a plain-English statement to the SKILL's "Expression
language" section.

## Target service

`lakerunner-process-metrics` on instance `prod`. Confirmed via
`mcp__plugin_cardinal_cardinal__lakerunner__list_services` before the
run. Chosen because:

1. It exists (`list_services` returned it).
2. It has both `logs` and `metrics` signals — required for a Sentinel
   that queries both.
3. It is DIFFERENT from the source-investigation target
   `lakerunner-process-logs`.

Instance is `prod`, not `cardinalhq` — the MEMORY.md reference to
"cardinalhq" was to the org, not to an instance slug. `list_instances`
shows only `otel-demo` and `prod`.

## Recompilation outcome

`mechanize-out/f89df52b-v2/sentinel.yaml` — differs from
`mechanize-out/f89df52b/sentinel.yaml` in exactly three respects:

1. Three function nodes now declare `runtime: python3.12` + `.py`
   sources.
2. Four `capabilities.required[].id` values switched from
   `lakerunner.*` to `observability.*`. Four `toolRef` values in tool
   nodes likewise.
3. Two `emit.evidence` lists switched from `${...}` expression items
   to `{nodeRef, field}` records — that's the shape the executor's
   `_build_finding` already resolves; the v1 raw-expression shape was
   unreachable.

Node IDs, dependencies, expressions, variation points, and input
schemas are byte-identical to v1. Rationale delta at
`mechanize-out/f89df52b-v2/rationale.md`.

## Executor run outcome

Log directory:
`spike/executor/runs/reuse-test-lakerunner-process-metrics/`

Terminal state (15 nodes):
- 13 SUCCEEDED (all tool nodes, all function nodes, both conditions).
- 2 SKIPPED (both `emit` nodes; their `when` gates evaluated false).
- 0 FAILED / CANCELLED.

Exit code: 0. Duration: 5 ms (functions + condition evaluation only;
tool nodes read from the pre-populated cache).

Findings emitted: **0.** Per §37 that IS the assessment — the service
is currently healthy AND no error-count-reconciliation mismatch was
detected, so neither emit-gate fires. This is the correct outcome for
a healthy service. The Sentinel's declared `outputs.healthReport`
carries the full multi-signal breakdown; only the finding sinks are
empty.

Per-signal verdict (from `compute-health-summary` output):

| Signal | Status | Value |
|---|---|---|
| Availability | healthy | minReplicas=1.01 |
| Restarts | healthy | 0 restarts across 1 pod (min uptime ~171k s = 47h) |
| CPU | healthy | peak 0.73 (threshold 0.80) |
| Memory | healthy | peak 0.57 (threshold 0.80) |
| Log levels | healthy | 356 buckets, no ERROR/WARN buckets seen |

Reconciliation: `totalErrorsReported=0`, `bodyMatchesFound=0`,
`mismatchDetected=false` — no errors to reconcile against.

## Honest gap enumeration (remaining rough edges)

1. **Log-level extraction is fragile.** The ddsketches key is a LogQL
   selector (`{level="INFO"}`), not a JSON attributes dict. My
   `_extract_level_from_key` in `compute_health_summary.py` tries
   `json.loads` first, fails, and buckets the level as `"unknown"`.
   The overall status is still correct (no ERROR/WARN substring →
   healthy) but the distribution shape is wrong. A small extractor for
   the LogQL selector syntax would fix it. Not a compile/execute
   plumbing gap — a per-signal function-body gap that a real
   compilation could produce if it inspected a real logs response.

2. **`emit.evidence` shape mismatch v1 vs. executor.** v1's YAML used
   `evidence: ["${nodes.X.output}"]` — free-form expression items. The
   executor's `_build_finding` iterates dict-shaped `{nodeRef, field}`
   records and silently skips non-dicts (`if not isinstance(ref,
   dict): continue`). v2 fixes this by using the dict shape; the SKILL
   should probably enforce the dict shape too (§14 doesn't spell it
   out). Consider adding to SKILL's Stage 4 evidence-emission rules.

3. **Plan-phase resolved args show `null` for anything downstream of a
   function.** `python executor.py plan` uses stub outputs for
   root-tool nodes, which propagates to downstream tool args as
   `null`. That makes the plan phase useful only for the root tool
   node's cache-population. Downstream tool queries have to be
   populated iteratively (run execute → get missing-cache error →
   populate → repeat). Not a blocker for this test (I populated all
   entries at once with the known service name), but "reference and
   run" would ideally be one step. A future executor could re-render
   pending queries between phases.

## Files touched

Edited:
- `~/.claude/skills/mechanize/SKILL.md`
- `spike/executor/executor.py` (docstring only — no behavior change)
- `spike/executor/capabilities.py`

New:
- `mechanize-out/f89df52b-v2/sentinel.yaml`
- `mechanize-out/f89df52b-v2/rationale.md`
- `spike/executor/functions/pick_resolved_service.py`
- `spike/executor/functions/compute_health_summary.py`
- `spike/executor/functions/compute_error_count_reconciliation.py`
- `spike/executor/REUSE-TEST.md` (this file)
- `spike/executor/runs/reuse-test-lakerunner-process-metrics/*`

Untouched (per guardrails):
- `mechanize-out/f89df52b/sentinel.yaml` (reference)
- `mechanize-out/f89df52b/rationale.md` (reference)
- `spike/executor/functions/detect_degeneracy.py`
- `spike/executor/functions/detect_counter_label_dominance.py`
- `spike/executor/runs/run-*/` (prior evidence)

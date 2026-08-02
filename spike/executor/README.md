# Sentinel spike executor

A minimum-viable Python runtime that executes a compiled Sentinel DAG against
a live target. Written to prove the v2-authored `sentinel.yaml` at
`spike/skill-driven-harvest/out-v2/sentinel.yaml` is executable, not to be a
production engine.

## What is here

| File / dir | Purpose |
|---|---|
| `executor.py` | DAG scheduler, expression evaluator, per-node validation, finding emission. Two phases: `plan` (resolve root tool args, write pending-queries) and `execute` (read cached tool responses, run the full DAG). |
| `capabilities.py` | Bindings for the two capabilities the Sentinel declares: `observability.query-metrics` (reads a pre-populated tool cache) and `code.grep` (shells out to `grep -rn`). |
| `functions/` | Deterministic Python function-node bodies: `detect_degeneracy.py`, `detect_counter_label_dominance.py`. Loaded by path from the YAML. |
| `tools/cache_from_spill.py` | Compacts a spilled Cardinal MCP metric-query result (dropping the huge `data_points` array) into the tool-cache format. |
| `inputs.json` | The Sentinel input bindings for this spike (the `lakerunner_worklane_claim_wait_ms` degeneracy target). |
| `runs/run-N/` | One directory per invocation. `summary.json` + `audit.jsonl` + `events.jsonl` + `pending-queries/` + `tool-cache/`. |
| `runs/findings.jsonl` | Append-only sink for every emitted finding across all runs. |
| `RELIABILITY.md` | Post-hoc validation report for the 5-run reliability suite. |

## How to run it

The executor cannot call an MCP server directly, so a run happens in three
steps:

```bash
cd spike/executor
RUN=runs/run-N
mkdir -p "$RUN"

# 1. plan: resolve root tool-node arguments and write pending-queries/<node>.json
./.venv/bin/python executor.py plan \
    --sentinel ../skill-driven-harvest/out-v2/sentinel.yaml \
    --inputs inputs.json \
    --run "$RUN"

# 2. driver populates $RUN/tool-cache/<node>.json for every pending query
#    (in this spike, an operator or agent runs the Cardinal MCP tool
#    execute_metrics_query with the pending-queries/*.json arguments,
#    then either writes the response directly or uses cache_from_spill.py
#    to compact a spilled result)

# 3. execute: read tool-cache, run every node, emit findings
./.venv/bin/python executor.py execute \
    --sentinel ../skill-driven-harvest/out-v2/sentinel.yaml \
    --inputs inputs.json \
    --run "$RUN"
```

`plan` and `execute` are safe to re-run. `execute` fails a tool node with
`MissingCacheError` if its cache entry is missing, and records the resolved
arguments to `pending-queries/<node>.json` for the driver to satisfy.

## Runtime inputs (`inputs.json`)

Bindings for the Sentinel's declared input schema. In this spike:

| Field | Value | Meaning |
|---|---|---|
| `metricName` | `lakerunner_worklane_claim_wait_ms` | The metric under investigation. |
| `serviceName` | `lakerunner-process-logs` | `service_name` label scoping the query. |
| `instance` | `prod` | Cardinal MCP lakerunner instance slug. |
| `signal` | `logs` | `signal` label value. |
| `dimensionalBreakdown` | `["action", "level"]` | Labels the metric SHOULD vary across if healthy. |
| `lookbackWindow` | `3h` | Rolling query window ending at `execution.now`. |
| `relatedCounterMetric` | `lakerunner_worklane_claim_trigger` | Companion counter whose label distribution characterizes WHY work is happening. |
| `relatedCounterLabels` | `["action", "level", "trigger"]` | Group-by labels for the counter query. |
| `relatedCounterDominanceLabel` | `trigger` | Which label's one-value dominance flags a capacity-starvation pattern. |

`codeRepoPath` is intentionally omitted, which gates off
`locate-metric-emission-code` and `interpret-metric-semantics-from-code`.

## Capabilities wired

* `observability.query-metrics` -> `capabilities.query_metrics` (tool-cache
  reader). Normalizes MCP response spellings (`seriesTotal` vs
  `series_total`, `ddsketches` as dict or list).
* `code.grep` -> `capabilities.code_grep` (subprocess `grep -rn`, restricted
  to the requested path, with `--include=` globs).

## Known limits

* **No LLM runtime.** `interpret-metric-semantics-from-code` is only reachable
  via its `when: ${inputs.codeRepoPath != null}` gate. A required LLM node
  would raise `LlmUnavailableError`.
* **Cache-driven tool calls.** The executor does not embed an MCP client. A
  driver (subagent, script, or operator) must populate `tool-cache/*.json`
  between `plan` and `execute`.
* **Expression subset.** Only the subset needed to evaluate v2 renders: attr
  access on `inputs`, `nodes.<id>.output`, `execution.now`; comparisons,
  boolean ops, ternary, arithmetic on numbers and datetimes, `null` checks,
  and `join(list, sep)`. Wider `${...}` grammar (spec section 13) is not
  fully supported.
* **No retry / backoff.** A failed node stays FAILED for the run; downstream
  nodes with it in `dependsOn` are CANCELLED.
* **No concurrency.** Nodes are executed in a topological order, one at a
  time, despite the Sentinel declaring `concurrency: 3`.

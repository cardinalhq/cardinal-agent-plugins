# Compilation rationale — service-health-assessment (v2)

Session compiled: `f89df52b-607a-4081-a808-2c42c8e3ff02.jsonl`
Investigation objective (first user text): "let's figure out the service health for lakerunner process logs"
Investigation phase: JSONL lines 1–75.

## What changed vs. v1 (mechanize-out/f89df52b/)

This is a recompilation of the same session after three fixes to the
mechanize SKILL and the executor. The recompilation is byte-identical to
v1 in every respect **except** the three items the fixes address:

1. **Function-node runtime canonicalized to Python.** Every `kind: function`
   node now declares `runtime: python3.12` and `source: functions/<node>.py`
   (was `runtime: nodejs22` + `.mjs`). Nodes affected:
   `pick-resolved-service`, `compute-health-summary`,
   `compute-error-count-reconciliation`. The SKILL now requires this in
   Stage 4; §11 of `sentinels.md` remains normative in shape but the
   `.mjs` example there is informative-only until the spec is updated.

2. **Capability IDs abstracted per §10.** Vendor-shaped IDs were replaced
   with the SKILL's known abstract registry:

   | v1 (vendor-shaped, prohibited) | v2 (abstract) |
   |---|---|
   | `lakerunner.list-services` | `observability.list-services` |
   | `lakerunner.error-overview` | `observability.error-overview` |
   | `lakerunner.execute-logs-query` | `observability.query-logs` |
   | `lakerunner.execute-metrics-query` | `observability.query-metrics` |

   The v0 executor's `capabilities.py` now registers all four IDs
   (previously only `observability.query-metrics` + `code.grep`). Provider
   binding to the lakerunner MCP tool happens in `capabilities.py` and in
   the tool-cache driver, not in the Sentinel.

3. **`emit` evidence entries switched from ad-hoc `${nodes.X.output}` list
   items to explicit `{nodeRef, field}` records** so the executor's
   evidence resolver in `_build_finding` can walk them. v1 declared
   evidence as raw expressions that the executor didn't know how to
   dereference; v2 uses the shape the executor already implements.
   Purely a serialization fix — no semantic change.

Everything else — node IDs, dependencies, arguments, expressions,
variation points — is unchanged from v1. Node IDs remain frozen from v1's
Round 1.

## Reference back to v1

For the full Stage 1–7 walkthrough (segmentation, all 16 tool-call
classifications, procedure signature, judgment calls, code-reading
option, attachment handling, variation-point choices, fidelity losses
worth naming) see `mechanize-out/f89df52b/rationale.md`. This file is
strictly a delta.

## Nested interpolation

The v2 `count-logs-by-level` query still contains an inner `${...}`
reference to `inputs.aggregationBucket` embedded in the LogQL body along
with the surrounding `${nodes.pick-resolved-service.output.name}`. The
SKILL's "Expression language" section now explicitly permits multiple
`${...}` interpolations per string; the v0 executor evaluates each
independently in place. Verified against the executor's `render_template`
before compilation.

## Unresolved (unchanged from v1)

- Skill's Subset B tool-argument expression grammar remains a pragmatic
  ruling pending spec clarification. The v2 Sentinel does not introduce
  any new expression forms.

## Files emitted

- `sentinel.yaml` — recompiled with the three fixes above.
- `rationale.md` — this delta.

Not emitted:
- `audit.jsonl` — inherited from v1's classification table.
- Function source — hand-authored under `spike/executor/functions/` as
  part of the reuse-test integration step, not part of compiler output.

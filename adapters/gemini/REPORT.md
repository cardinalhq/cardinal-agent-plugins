# Gemini adapter migration report (P2)

Source: `cardinalhq/cardinal-gemini-plugin` `plugins/cardinal-gemini-plugin/`
(shipped v0.1.0) → `adapters/gemini/` consuming `core/cardinal_core`.

## What migrated

| Surface | Disposition |
| --- | --- |
| `hooks/cardinal-gemini-telemetry.py` | Rewritten on core. Moved to core imports: OTLP building/emission + resource attrs (`otlp`), git facts / initiative resolution / command detection (`initiative`), bash classification (`bashclass`), pricing (`pricing.GEMINI_PRICING_USD_PER_M` + `compute_cost_usd`), spend-limits gate + refresh (`limits.gate_output(hook_event_name="BeforeAgent")`, `maybe_refresh_verdict`), session counters / plan stamp / convention prompt / budget standing (`session`), state paths (`paths.AgentPaths(home=~/.gemini)`). Kept adapter-side: Gemini payload-key probing (`usage`/`usageMetadata` spellings, `normalize_usage`, `usage_attrs`), tool-name normalization + `TARGET_KEYS`, `subagent_description` extraction, `session_id` spellings, debug-payload dump, event dispatch. |
| `hooks/_limits_common.py` (260 LOC) | Deleted — fully replaced by `cardinal_core.limits` + `cardinal_core.paths`. |
| `hooks/_plugin_version.py` | Copied verbatim (reads `../.gemini-plugin/plugin.json` at hook time). |
| `scripts/cardinal-connect` | Rewritten on `cardinal_core.deviceflow` (device-code flow, reachability probes, `derive_deployment_env`) + `cardinal_core.paths` writers. Kept adapter-side: extension-bundle install, settings.json telemetry/hooks/mcpServers writer, state/secrets shape, CLI. `DeviceFlowError` is caught and mapped to the same `sys.exit` messages. |
| `scripts/cardinal-disconnect` | Near-verbatim; `atomic_write`/`backup` and state paths now from `cardinal_core.paths`. |
| `scripts/cardinal-status` | Verbatim (its probe copy is status-specific; no real duplication removed by core). |
| `extension/` (gemini-extension.json, hooks/hooks.json, GEMINI.md), `skills/`, `.gemini-plugin/plugin.json`, `README.md`, `LICENSE` | Copied verbatim. |

Vendoring: hooks import `cardinal_core/` from a sibling directory created by
`python3 build/vendor.py gemini` (gitignored build output). The test suites
auto-vendor when the directory is missing.

## LOC

- Before (source repo Python): **2,647** = hooks 1,429 (telemetry 1,128 + _limits_common 260 + _plugin_version 41) + scripts 1,031 + tests 187.
- After (adapter Python, excluding vendored core): **2,177** = hooks 619 (telemetry 578 + _plugin_version 41) + scripts 845 + tests 713.
- Product code (hooks + scripts): 2,460 → 1,464 (**−996, −40%**), with `_limits_common.py` deleted outright. Test LOC grew 187 → 713 because the golden capture/parity harness is new.

## Golden coverage (`tests/goldens/`, captured from the SHIPPED v0.1.0 hook)

Captured by `tests/capture_goldens.py --hook <shipped repo>/hooks/cardinal-gemini-telemetry.py`
over `tests/scenarios.py`; deterministic across runs (fixed git identity/dates
→ stable fixture-repo SHA; verified by double-capture diff).

| Golden | Covers | Records |
| --- | --- | --- |
| `session-start.json` | convention-prompt stdout in a git repo; suppression outside one | 0 (stdout goldened) |
| `before-agent.json` | `cardinal.git_state` (branch/repo/sha/initiative), `/slash-command` + `<command-name>` detection, non-git suppression, `user_turn_seq` advance | 3 |
| `after-model.json` | `api_request` + `cardinal.turn_usage`: `usage` and `usageMetadata` spellings, thought/cached/toolUse buckets, exact + longest-prefix pricing, unpriced model (no `cost_usd`), plan stamp + `cardinal.plan_state` (once, then sig-stable), zero-usage suppression, `turn_seq` advance | 11 |
| `after-tool.json` | `cardinal.turn_tool` + `tool_result`: compound bash (`file-write` + `bash_multi`), `git-read`, `mcp__` qualified tool (raw name on turn_tool, server/tool split), `read_file`/`Read`/`write_file` targets, success via bool / `exit_code` / `exitCode` / `status`, `tool_seq` advance | 12 |
| `after-agent.json` | `cardinal.subagent_usage`: each identifying facet (type, description, agent_id+duration+status, `usageMetadata.totalTokenCount`, `tool_input.description`, 160-char prompt truncation), facet-less suppression, plan-stamp merge | 6 |
| `pre-compress.json` | `cardinal.plan_usage` with `plan.compact_trigger`, snake/camel key spellings, boolean attr encoding | 2 |
| `session-end.json` | no-op (no emission, rc 0) | 0 |
| `limits-gate.json` | BeforeAgent stdout: warn verdict (additionalContext + systemMessage) with band hysteresis on the second turn; block verdict enforced every turn | 0 (stdout goldened) |

Total: 22 batches / 34 records, plus goldened hook stdout + returncode for
every one of the 34 steps.

## Normalization (documented deltas)

On top of `core/tests/harness.py` normalization (timestamps, `ts`,
`cardinal.core_version` value), `tests/scenarios.py::normalize_extra` pins
`cardinal.plugin_version` + OTel scope `version` and `cardinal_cwd`
(sandbox temp paths), and **drops the `cardinal.core_version` resource
attribute** — the one intentional contract addition from core, absent from
pre-migration output by definition. A mutation check confirmed the parity
suite fails on any other attribute change, and a separate raw-batch check
confirmed the migrated hook does emit `cardinal.core_version` with
everything else byte-identical (see CORE_GAPS.md).

## Test results

`python3 -m unittest discover -s tests` from `adapters/gemini/`: **11 tests, all passing** —

- `test_parity.GoldenParityTests.test_scenarios_match_goldens` (8 scenario subtests, byte-equal batches + stdout, plus golden↔scenario sync check)
- `test_smoke.HookSmokeTests` (6): no-crash on all 7 events without state, SessionStart git-repo suppression, AfterModel progress cursor, AfterTool progress, AfterAgent facet gating, BeforeAgent `user_turn_seq` advance — ported from the source repo
- `test_smoke.CoreFunctionTests` (4): pricing exact/prefix/unpriced + thought-token billing, bash single-verb + compound write-risk — ported to exercise the vendored core

## Unproven / caveats

- **Live-endpoint scripts:** `cardinal-connect` / `cardinal-disconnect` were migrated by code inspection and syntax check only — the device-code flow, key revocation, and extension install need a real Cardinal backend to exercise. Their moved parts (`deviceflow`, `paths`) are covered by `core/tests/test_core.py`.
- **Progress-file shape:** core's `save_progress` persists the whole state dict (superset of the shipped fixed key set, e.g. `plan_stamp` is now always present). Reads are `.get()`-based on both sides; no OTLP or behavior change, but the on-disk JSON is not byte-identical.
- **Real Gemini CLI payloads:** scenarios are synthetic (built from the shipped code's own key-probing and the parity spec), same as the source repo's tests — no capture from a live Gemini CLI run was performed here.
- `docs/specs/gemini-parity.md` remains in the source repo (`docs/` here is read-only for this migration).

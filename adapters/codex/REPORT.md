# Codex adapter migration report (P1)

Source: `cardinalhq/cardinal-codex-plugin` `plugins/cardinal-codex-plugin/`
at v0.5.2 (commit 2338884). Target: `adapters/codex/` consuming
`core/cardinal_core` (frozen, untouched).

## What migrated where

Moved to core imports (deleted from adapter code):

| Was (source repo) | Now (cardinal_core) |
| --- | --- |
| kv / log_record / parse_ts_ns / emit_records / resource_attrs / load_connection (hook) | `otlp` (`connection_from_paths`, `emit_records`, ...) |
| resolve_initiative / strip_worktree_noise / canonical_repo / detect_command / git / `_is_git_repo` (hook) | `initiative` |
| classify_bash_command + tables (hook, ~140 lines) | `bashclass` |
| MODEL_PRICING_USD_PER_M / price_for_model / compute_cost_usd (hook) | `pricing` (`OPENAI_PRICING_USD_PER_M`) |
| `_limits_common.py` (264 lines, whole file) | `limits` + `paths` |
| limits_gate_output (hook) | `limits.gate_output(..., hook_event_name="UserPromptSubmit")` |
| CONVENTION_PROMPT / `_budget_standing` (hook) | `session.convention_prompt("Codex")` / `session.budget_standing` |
| plan stamp read/write, progress load/save, seq counters, plan-usage throttle (hook) | `session` (`read/write_plan_stamp`, `load/save_progress`, `begin_user_turn`, `end_model_call`, `plan_usage_throttled`) |
| device flow, deployment-env derivation, ingest/MCP reachability probes (cardinal-connect) | `deviceflow` (DeviceFlowError caught -> `sys.exit`) |
| atomic_write / atomic_write_secret / backup (cardinal-connect) | `paths` |

Stayed adapter-side (Codex-specific by design):

- Transcript scraping: `handle_stop` with `MAX_EVENTS_PER_STOP=512` and
  resume-line semantics (`last_line` cursor rides through the core
  progress dict as an extra key), truncation reset, pending-call pairing.
- `normalize_tool_name` / `extract_patch_target` / `output_success` /
  `usage_attrs` / TARGET_KEYS; token-event assembly from `token_count`
  records (plan_state sig, plan_usage field mapping).
- Payload spellings: `session_id_from_payload`,
  `transcript_path_from_payload`, `subagent_description_from_payload`,
  env-gated P5 debug dump.
- `cardinal-connect`: parse-aware `strip_managed_block` TOML writer
  (issue-#10 drift handling), managed `hooks.json` writer, state/secrets
  schema.
- `cardinal-status` and `cardinal-disconnect`: kept verbatim (disconnect
  intentionally keeps its own `strip_managed_block` copy, in lockstep with
  connect, so each script stays runnable standalone).

## LOC

| | pre-migration | post-migration |
| --- | --- | --- |
| hooks (telemetry + _limits_common + _plugin_version) | 1,502 | 666 |
| scripts (connect / status / disconnect) | 1,095 | 910 |
| **adapter code total** | **2,597** | **1,576 (-1,021, -39%)** |
| tests | 1,133 | 1,705 (incl. 690-line shared fixture/capture harness) |

(Vendored `hooks/cardinal_core/` is a gitignored build output, not source.)

## Golden coverage (all captured from the shipped v0.5.2 hook)

OTLP batches (normalized per core harness + local extras — plugin_version
pinned, cardinal.core_version dropped, sandbox HOME path scrubbed;
git SHA made deterministic via pinned commit identity/dates):

1. `stop_first` — 12 records: turn_tool/tool_result x4 (Bash compound
   command with bash_class+bash_multi, Read with TARGET_KEYS target,
   apply_patch->Edit with patch target, `mcp__` qualified name + split
   server/tool), api_request with computed cost_usd, turn_usage,
   plan_state, plan_usage (both rate-limit windows).
2. `stop_second` — resumed cursor: user_turn_seq advances, turn_seq
   resets, plan_state/plan_usage throttled out.
3. `user_prompt_submit` — git_state with head SHA, branch, canonical
   repo, worktree-noise-stripped initiative, detected slash command, and
   persisted plan stamp.
4. `subagent_stop` — subagent_usage with description + plan stamp.

Hook stdout: `session_start_stdout` (convention prompt + budget standing
from a forced limits fetch), `session_start_outside_repo_stdout` (silent),
`gate_block_stdout`, `gate_block_override_stdout`, `gate_warn_stdout`,
`gate_warn_repeat_stdout` (band hysteresis).

Goldens verified deterministic (two capture runs byte-identical). The
migrated hook matched all 10 goldens byte-for-byte on first run.

## Test results

`python3 -m unittest tests.test_parity` — **36 tests, all passing**
(~52s): 1 golden smoke + 3 golden-parity methods (10 golden comparisons
via subTest) + 32 behavioral tests ported from the source repo's
`test_cardinal_plugin.py` (Manifest, Connect x7 incl. issue-#10 drift,
Status/Disconnect, Stop transcript + cost math, plan throttle/stamp, cap
resume, gate block/warn, SessionStart, Enrichment x6, contract lockstep
fixtures x6). `core/tests` still green (17 tests) — core untouched.

## Core gaps

None. Every extracted API fit the codex hook exactly (the extraction was
sourced from this plugin, so this was expected). No `CORE_GAPS.md` needed.

Non-blocking observation for a future core pass: `cardinal-status`'s
ingest/MCP probes duplicate `deviceflow._ingest_probe_once` /
`verify_mcp_reachable` semantics with status-specific message copy; a core
probe returning structured results would let status format its own copy.

## Intentional deltas vs. the shipped plugin

- Resource attrs now include `cardinal.core_version` (added by core's
  `resource_attrs` by design; dropped in golden normalization).
- `cardinal.plugin_version` / scope version stamp 0.6.0 (this adapter's
  plugin.json) instead of 0.5.2 (normalized in goldens).
- plugin.json homepage/repository point at the monorepo.

## Unproven / caveats

- SubagentStop payload shape remains unobserved in the wild (P5) — the
  key-spelling probes are covered by tests but real payloads are still
  pending debug captures; unchanged from the source repo.
- The 512-event cap path is proven behaviorally (cap -> cursor resume ->
  300/300 api_requests across two Stops), not via goldens (a 512-record
  golden adds bulk without new shape coverage).
- Sustained-throttle plan_usage re-emission after 10 min of wall time is
  not directly tested (would need clock control); the throttle-suppression
  path and first-snapshot-unthrottled anchor are golden-covered.
- Live device-flow against a real Cardinal backend not exercised (stub
  only), same as the source repo's suite.

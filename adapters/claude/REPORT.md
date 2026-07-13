# Claude adapter migration report (P4)

Source: `cardinalhq/cardinal-claude-plugin` `plugins/cardinal/` at
v0.12.2 (commit ce9f1df). Target: `adapters/claude/` consuming the
vendored `cardinal_core`.

## What migrated

Everything the plugin ships: 7 per-event hook scripts (kept as SEPARATE
scripts — `hooks/hooks.json` registration is verbatim from the source),
3 `bin/` executables, `.claude-plugin/plugin.json`, `.mcp.json`,
`agents/`, `skills/`. Hooks import a vendored `cardinal_core/` directory
next to them (`python3 build/vendor.py claude`; the copy is gitignored).

Structural notes honored:
- Claude Code's NATIVE OTel emitter does the heavy lifting; these hooks
  fill gaps. Their OTLP connection comes from Claude Code's OTel
  settings (`~/.claude/settings.json` env block), NOT from
  cardinal-secrets.json — that acquisition stays adapter-side in
  `hooks/_otel_settings.py`, which constructs core's
  `otlp.IngestConnection` from it.
- `_plan_cache.py` (OAuth profile/usage fetch) stays ADAPTER-ONLY by
  explicit decision — copied verbatim.
- Claude emits cost natively — core `pricing.py` is deliberately unused.

## LOC

| Surface | Before (shipped) | After (adapter, excl. vendored core) |
| --- | ---: | ---: |
| hooks/*.py | 2,891 | 1,959 |
| bin/cardinal-{connect,status,disconnect} | 1,104 | 913 |
| **Total** | **3,995** | **2,872 (−1,123, −28%)** |

Of the "after", 489 LOC are `_plan_cache.py` + `_plugin_version.py`
kept verbatim by decision; the actively migrated surface went
3,506 → 2,383 (−32%).

## Replaced by core vs kept adapter-side

| Shipped code | Disposition |
| --- | --- |
| git-state.py `_resolve_initiative` | → core `initiative.resolve_initiative` |
| git-state.py `_strip_worktree_noise` | → core `initiative.strip_worktree_noise` |
| git-state.py `_canonical_repo` | → core `initiative.canonical_repo` |
| git-state.py `_detect_command` | → core `initiative.detect_command` |
| git-state.py `_git` | → core `initiative.git` |
| git-state.py `_kv` + hand-built OTLP body/POST | → core `otlp.kv` / `log_record` / `emit_records` |
| git-state.py `_PREFIX_TO_TYPE`/`_INITIATIVE_TYPES`/branch tables | → core `initiative.PREFIX_TO_TYPE` (enum derived; back-compat aliases kept on the module for importers/tests) |
| limits-gate.py verdict→hook-output logic (block/warn/hysteresis/ack) | → core `limits.gate_output(hook_event_name="UserPromptSubmit")` |
| initiative-convention.py `PROMPT` | → core `session.convention_prompt("Claude Code")` (byte-identical — proven by stdout golden) |
| initiative-convention.py `_is_git_repo` | → core `initiative.is_git_repo` |
| turn-usage.py `_classify_bash` + BASH_* tables | → core `bashclass.classify_bash_command` |
| turn-usage.py `_ts_ns_from_record` | → core `otlp.parse_ts_ns` |
| turn-usage.py `_kv`, per-batch POST | → core `otlp.kv` / `emit_records` (chunk loop stays) |
| subagent-usage.py POST assembly | → core `otlp.emit_records` |
| plan-state.py / plan-usage.py `_kv`, POST assembly | → core `otlp.kv` / `emit_records` |
| `_limits_common.py` `limits_config`/`read_verdict`/`fetch_status`/`standing_lines`/`atomic_write_json`/paths | → core `limits.*` + `paths.AgentPaths` / `atomic_write_json_compact` (file deleted; 288 → 90-line shim) |
| `_limits_common.py` `git_facts` | → core `initiative.git_facts` |
| cardinal-connect device flow (`_post_json`/`start_device_code`/`poll_device_token`) | → core `deviceflow` (client_id="cardinal-claude-plugin"; `DeviceFlowError` → `sys.exit` adapter-side) |
| cardinal-connect `derive_deployment_env`, `verify_ingest_reachable` (+401 ladder), `verify_mcp_reachable`, `atomic_write` | → core `deviceflow` / `paths.atomic_write` |
| cardinal-disconnect `backup` | → core `paths.backup` |
| cardinal-status limits standing (fetch/render/git facts) | → core `limits.fetch_status` / `standing_lines` + `initiative.git_facts` |
| **Kept adapter-side** | |
| `_otel_settings.py` (settings.json env loading, kv-CSV parsing, IngestConnection construction, resource-attr passthrough + emit-time version stamp) | Claude-specific acquisition by design (see CORE_GAPS §1–3) |
| `_limits.py` `maybe_refresh_verdict` TTL loop + `ingest_api_key` | key sourced from OTel headers, not cardinal-secrets.json (CORE_GAPS §1) |
| initiative-convention.py `_budget_standing` composition | same key-sourcing reason (uses core `standing_lines`/`git_facts`) |
| turn-usage.py `_walk_current_turn`, `_is_real_user_message`, `_build_records`, `TARGET_KEYS`/`_extract_target` | Claude transcript walking — agent-specific |
| subagent-usage.py `_sum_transcript_usage`, caps, attr assembly (all-stringValue wire contract via str-coercing `_kv`) | Claude subagent-transcript specifics |
| plan-state/plan-usage attr builders + 10-min throttle | plan-telemetry wire shape (incl. frozen scope version "0.11.1") |
| `_plan_cache.py` (444 LOC, verbatim) | adapter-only by explicit decision |
| `_plugin_version.py` (verbatim) | reads sibling plugin.json at emit time |
| cardinal-connect settings-env writes, OWNED_ENV_KEYS, `.claude.json` legacy cleanup, state/pending files, CLI flow | Claude Code write surface |
| cardinal-status probes/report; cardinal-disconnect env-strip/revoke | Claude Code surface |

## Golden coverage (tests/goldens/, captured from the SHIPPED v0.12.2 hooks)

12 scenarios; normalization = core harness (timestamps, `ts`,
`cardinal.core_version`) + local (`cardinal.plugin_version`, scope
version, `cardinal.cwd`); everything else byte-compared, including
attribute order and value types. Capture is deterministic (re-capture
diffed clean).

- `git_state_{feature_branch,worktree_branch,main_branch}` — initiative
  resolution buckets, command detection, plan stamps, git facts.
- `turn_usage_stop` — 9 records: 3 model calls × usage + 6 tool records
  (targets, bash_class multi/single, MCP names, user_turn_seq/turn_seq/
  tool_seq).
- `subagent_usage_{full,missing_transcript}` — totals/component split/
  dominant model/tool histogram, and the no-transcript degradation.
- `plan_state_{oauth,no_token}` + `plan_usage_stale_cache` — via a mock
  api.anthropic.com (profile+usage), incl. the plan_type=api branch.
- `initiative_convention` (stdout), `limits_gate_{warn,block}` (stdout +
  ack hysteresis side effect).

## Test results (all green)

| Suite | Tests | Notes |
| --- | ---: | --- |
| test_parity.py | 12 | migrated hooks vs shipped-plugin goldens, byte-equal |
| test_cardinal_plugin.py (ported) | 66 | 2 adaptations: `_limits.py` module path in the importlib loader; connect 401-retry test follows core's longer ladder/message |
| test_plan_state.py (ported) | 23 | unchanged beyond path constants |
| test_turn_usage.py (ported) | 21 | unchanged beyond path constants |
| test_subagent_usage.py (ported) | 14 | unchanged beyond path constants |
| **Total** | **136** | |

Run: `python3 build/vendor.py claude && cd adapters/claude/tests &&
python3 -m unittest discover -s . -p 'test_*.py'`

## Behavioral deltas (CLI text only — OTLP is byte-equal)

- cardinal-connect transient-401 retry ladder: ~15s (4 retries) →
  ~63s (6 retries, core deviceflow), with reworded progress/exhaustion
  messages ("ingest key returned 401; retrying…", "ingest key did not
  propagate").
- Device-flow failure texts come from core (`✗ ` prefix added
  adapter-side; ":" instead of "—" in init-failure detail).

## Gaps

See CORE_GAPS.md (5 items: limits key-sourcing injection, single-header
IngestConnection, resource-attr passthrough, retry-ladder injection,
plan-stamp path divergence). **Resolved by core 0.2.0 — see below.**

## Core 0.2.0 rewire (gaps 1–4 adopted)

Core 0.2.0 added the argument-injection APIs the migration filed as
CORE_GAPS; the adapter-side shims are gone. This section supersedes the
`_limits.py` / `_budget_standing` rows above and the retry-ladder
behavioral delta.

- **Gap 1** — `hooks/_limits.py` DELETED (90 LOC). git-state.py calls
  core `limits.maybe_refresh_verdict(..., api_key=...)`;
  initiative-convention.py's `_budget_standing` collapsed to core
  `session.budget_standing(paths, session_id, cwd, api_key=...)`; the
  key still comes from `_otel_settings.ingest_api_key` (Claude fact).
- **Gap 2** — `ingest_connection()` forwards all OTLP header pairs via
  `IngestConnection.extra_headers` (key pair stays the auth header).
- **Gap 3** — `resource_attrs()` delegates to core
  `otlp.passthrough_resource_attrs` (resources now also carry
  `cardinal.core_version`; the parity normalizer drops it — goldens
  unchanged).
- **Gap 4** — cardinal-connect passes `sleeps=(1, 2, 4, 8)` to
  `verify_ingest_reachable`, restoring the shipped ~15s
  persistent-401 abort (was ~63s on core's hardwired ladder); the
  ported abort test's sleep budget adjusted back.
- **Gap 5** — still moot (plan cache is adapter-only by decision).

LOC: −96 net adapter code (155 deleted, 59 added; hooks 1,959 → 1,858,
bin 913 → 918 — the +5 is the injected ladder + comment). Test count
unchanged at 136, all green; goldens byte-identical. The only test-file
change is LimitsCommonTests, rewired from the deleted shim onto the
surface the hooks now call (core `limits` + `_otel_settings`).

What still can't move to core (by design, not gaps): the OTel-settings
acquisition itself (`_otel_settings.py` — settings.json env block, CSV
parsing, key extraction), Claude payload/env session-id sourcing, and
`_plan_cache.py` (verbatim, standing decision).

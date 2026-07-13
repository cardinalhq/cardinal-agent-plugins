# Cursor adapter migration report (P3)

Source: `cardinalhq/cardinal-cursor-plugin` v0.2.0
(`plugins/cardinal-cursor-plugin/`, read-only) → `adapters/cursor/`
consuming `core/cardinal_core` (vendored into `hooks/cardinal_core/` by
`build/vendor.py cursor`; the vendored copy is gitignored).

## Method

1. **Goldens first, from the shipped code.** `tests/capture_goldens.py`
   ran the *existing* v0.2.0 hook script in a sandboxed HOME
   (prefabricated `~/.cursor/cardinal.json` + `cardinal-secrets.json`
   pointing at `core/tests/harness.py::StubIngest`, plus a
   deterministic git repo — fixed author/committer identity and dates →
   stable HEAD sha) against 10 synthetic hook payloads, freezing
   normalized OTLP batches *and hook stdout* into `tests/goldens/*.json`.
2. **Adapter written second**, then proven byte-equal against those
   goldens by `tests/test_parity.py::GoldenParityTests` using the same
   fixtures (`tests/fixtures.py`).

Normalization = core harness `_normalize` (timestamps, `ts`) plus an
adapter-local scrub (`fixtures._scrub`): drop `cardinal.core_version`
(new in core, absent from pre-migration output — see CORE_GAPS.md §3),
pin `cardinal.plugin_version` and the OTel scope version, replace the
sandbox path. Everything else — event names, attribute keys, values,
attribute order, record order, resource/scope shape — is compared
byte-for-byte.

## Golden coverage (10 steps, 12 OTLP records + 2 stdout contracts)

| Golden | Event | Locks in |
|---|---|---|
| 01-sessionStart | sessionStart | `additional_context` convention prompt (byte-equal, incl. core `session.convention_prompt("Cursor")` wording); no OTLP |
| 02-beforeSubmitPrompt-plain | beforeSubmitPrompt | `cardinal.git_state`: head sha, branch, canonical repo, initiative name/type, cwd from `workspaceRoots` |
| 03-postToolUse-shell-compound | postToolUse | `cardinal.turn_tool` (`Bash`, `bash_class=build`, `bash_multi=true` for `tsc && pytest`) + `tool_result`; turn tick on new `generation_id` (user_turn_seq 1) |
| 04-postToolUse-mcp | postToolUse | MCP-qualified `turn_tool.tool_name` (raw `mcp__cardinal__lakerunner__list_services` preserved) + `mcp_server_name`/`mcp_tool_name`; `tool_seq` advance within same generation |
| 05-postToolUse-read-file-notify | postToolUse | `target` from TARGET_KEYS (`read_file.path`); **Divergence E**: staged `<conv>.notify.json` surfaced as `additional_context` stdout; new generation → user_turn_seq 2 |
| 06-beforeSubmitPrompt-command | beforeSubmitPrompt | `cardinal_command` slash-command detection (`/cardinal:status`) |
| 07-subagentStop | subagentStop | `cardinal.subagent_usage`: subagent_type/status/description (prefers `description` over `task`/`summary`), duration_ms, message/tool_call/loop counts (camelCase spellings) |
| 08-afterAgentThought | afterAgentThought | `cardinal.turn_thought`: `durationMs` + text **length only** (Divergence J) |
| 09-afterAgentResponse | afterAgentResponse | `cardinal.turn_response`: text **length only** |
| 10-preCompact | preCompact | `cardinal.plan_usage` context-window slice: `plan.context_tokens/context_window/context_pct/compact_trigger/messages_to_compact/is_first_compaction` (Divergence K) |

Every golden also locks the resource stamp (Divergence L):
`cursor.model`, `cursor.model_id`, `cursor.model_params` (JSON-compact
string), `cursor.version` from the payload base fields, over the
standard Cardinal resource attributes.

Deliberately absent (parity spec gap D, do not invent): no
`cardinal.turn_usage` / `cardinal.api_request` — Cursor exposes no
per-model-call token counts anywhere; `stop` remains a debug-capture
no-op.

## What migrated to core vs. stayed adapter-side

**Consumed from core** (`initiative`, `bashclass`, `otlp`, `limits`,
`session`, `paths`, `deviceflow`):
initiative resolution + worktree stripping + command detection +
canonical repo + git helpers; bash classification; OTLP kv/log_record/
resource_attrs/emit + connection loading; limits verdict read/refresh +
age constants + ack/override paths + standing lines (via
`session.budget_standing`); convention prompt; progress counters
(`load_progress`/`save_progress`/`begin_user_turn`); plan stamp; atomic
write/backup/secret file primitives; the whole device-code flow +
ingest/MCP reachability probes in `cardinal-connect`.

**Stayed adapter-side (correct per spec, Cursor diverges most):**
- camelCase payload mapping (`conversationId`, `toolName`/`toolInput`/
  `toolOutput`, `durationMs`, `modelId`/`modelParams`/`cursorVersion`,
  `workspaceRoots`) with snake_case fallbacks; `workspace_roots` → cwd.
- Divergence E gate: Cursor `{continue:false, user_message}` output
  schema, notify staging (`<conv>.notify.json`), `postToolUse`
  `additional_context` surfacing, `CARDINAL_CURSOR_STRICT_WARN=1`
  escalation, and the band/age/override/hysteresis walk (core's
  `gate_output` couples that policy to the hookSpecificOutput rendering
  — CORE_GAPS.md §1/§2).
- Cursor tool normalization (`run_terminal_cmd` → Bash, `mcp__` split),
  TARGET_KEYS, `output_success` exit-code scraping.
- `generation_id`-based turn ticking (Cursor has no user-message
  transcript boundary).
- turn_thought/turn_response/plan_usage/subagent_usage handlers
  (Cursor-only events), debug payload capture.
- `mcp.json` / `hooks.json` JSON managed-entry writers + `--project`
  mode (cloud-agent coverage) in `cardinal-connect`; `cardinal-status`
  and `cardinal-disconnect` kept verbatim (self-contained, no
  meaningful core overlap; disconnect must keep working even if the
  vendored core copy is damaged, so it stays dependency-free).
- Dropped: the shipped plugin's dead pricing table (~40 LOC; gap D means
  it could never run) and `_limits_common.py` (absorbed by core + the
  adapter gate).

## LOC

| File | v0.2.0 (source repo) | adapter | Δ |
|---|---|---|---|
| hooks/cardinal-cursor-telemetry.py | 1109 | 707 | −402 |
| hooks/_limits_common.py | 312 | — (core + adapter gate) | −312 |
| hooks/_plugin_version.py | 41 | 41 | 0 |
| scripts/cardinal-connect | 693 | 504 | −189 |
| scripts/cardinal-status | 167 | 167 | 0 |
| scripts/cardinal-disconnect | 258 | 258 | 0 |
| **Plugin code total** | **2580** | **1677** | **−903 (−35%)** |
| tests (suite + fixtures + capture) | 551 | 1037 | +486 |

Test LOC grew because the adapter suite adds the golden-parity harness
(fixtures.py 338 + capture_goldens.py 71) on top of the ported
behavioral tests; the shipped repo had no golden layer.

## Test results

- `python3 -m unittest discover -s adapters/cursor/tests -t adapters/cursor/tests`
  → **43 tests, 0 failures** (11 golden-parity, 32 behavioral — all
  meaningful tests from the source suite ported, plus a stale-verdict
  fail-open case; the source repo's two `_limits_common`-specific tests
  are re-pointed at the adapter's `notify_path`/`consume_notify`).
- Golden parity: all 10 steps byte-equal after normalization, including
  both stdout contracts (sessionStart convention context; postToolUse
  notify surfacing).
- Core suite untouched and still green:
  `PYTHONPATH=core python3 -m unittest discover -s core/tests -t core/tests`
  → **37 tests, 0 failures**.

## Caveats

- `.cursor-plugin/plugin.json` keeps version `0.2.0`; the release flow
  that bumps it should treat the monorepo as the new source of truth.
- The vendored `hooks/cardinal_core/` is a gitignored build output;
  `test_parity.py` auto-runs `build/vendor.py cursor` when it is
  missing.
- Goldens were captured from the shipped repo at
  `~/workspace/cardinal-cursor-plugin` (v0.2.0). Re-capturing requires
  that checkout (`capture_goldens.py --source …` /
  `CARDINAL_CURSOR_SOURCE`); it should only ever be re-run if the
  *contract* deliberately changes.
- See CORE_GAPS.md for the core API changes that would let the gate
  policy walk (currently duplicated adapter-side) collapse into core.

# Cardinal Codex adapter

Connect Codex to Cardinal telemetry and the unified MCP endpoint in one
browser-approved consent. Migrated from
[cardinalhq/cardinal-codex-plugin](https://github.com/cardinalhq/cardinal-codex-plugin)
(P1 of the agent-core extraction — see `../../docs/specs/agent-core.md`);
shared algorithms and the OTLP contract now come from `core/cardinal_core`.

| Skill | What it does |
| --- | --- |
| `cardinal-connect` | Runs Cardinal's device-code flow, mints ingest and MCP keys, writes managed Codex MCP config, and installs Cardinal telemetry hooks. |
| `cardinal-status` | Shows the recorded Cardinal workspace and probes the configured ingest and MCP endpoints. |
| `cardinal-disconnect` | Best-effort revokes Cardinal keys, removes managed Codex config/hooks, and deletes local state. |
| `cardinal-decision` | Turns opt-in decision capture on/off and shows its status (see Decision capture). |
| `cardinal-optimize-toolkit` | Mines the caller's last 30 days of session telemetry via the `cardinal` MCP server's `outcomes__*` tools for capability-fit recommendations, and on explicit confirmation writes the accepted artifact to `.codex/agents/<name>.toml`. Explicit-invocation only (W4.T4.2; see `docs/specs/toolkit-hive-mind.md` §4). |

## Layout

- `hooks/cardinal-codex-telemetry.py` — the single telemetry hook
  (SessionStart / UserPromptSubmit / Stop / SubagentStop). Codex-specific
  logic lives here: transcript scraping with resume-line semantics
  (MAX_EVENTS_PER_STOP=512), tool-name normalization
  (`exec_command`→Bash, `apply_patch`→Edit with patch-target extraction,
  `mcp__*` splitting), and token-event assembly from `token_count`
  transcript records. Everything shared — OTLP record building/emission,
  initiative resolution, bash classification, pricing, spend-limits
  delivery, session counters, plan stamp — is imported from
  `cardinal_core`.
- `hooks/cardinal_core/` — vendored copy of `core/cardinal_core`,
  created by `python3 build/vendor.py codex` at build time. Gitignored;
  run the vendor step after checkout before executing hooks or tests.
- `scripts/` — `cardinal-connect` (device flow + probes from
  `cardinal_core.deviceflow`; the parse-aware TOML managed-block writer
  and the Codex `hooks.json` writer stay adapter-side), `cardinal-status`,
  `cardinal-disconnect`.
- `tests/goldens/` — normalized OTLP/stdout fixtures captured from the
  pre-migration shipped plugin (v0.5.2) by `tests/capture_goldens.py`.
- `tests/test_parity.py` — asserts the migrated hook is byte-equal to the
  goldens, plus the behavioral suite ported from the source repo.

## Telemetry scope

Codex does not expose Claude Code's native OpenTelemetry emitter; the hook
reads Codex hook payloads and local session JSONL transcripts and emits the
Cardinal/Lakerunner event contract over OTLP/HTTP:

- `api_request` token usage (with plugin-computed `cost_usd`) from
  `token_count` transcript events.
- `tool_result` plus `cardinal.turn_tool` from legacy function call/output
  and current custom tool call/output transcript events (privacy-safe
  `bash_class` enum, raw qualified MCP names on turn_tool). Single-operation
  `functions.exec` programs are unwrapped to Bash/Edit/MCP; compound programs
  retain only their nested tool names, never the raw JavaScript source.
- `cardinal.git_state` on `UserPromptSubmit`, including initiative
  classification from the branch name and slash-command detection. A
  research-classified context shell is also emitted outside Git and for
  repositories without a first commit. When `gh pr view` resolves the
  branch's PR (via `cardinal_core.decisions.resolve_pr`, cached per
  repo+branch under `~/.codex/cardinal/decisions/cache/`, skipped on
  protected branches) the record also carries `cardinal_pr_number` and
  `cardinal_pr_url`; otherwise both keys are absent. Codex runs
  `UserPromptSubmit` synchronously, so the `gh` call is bounded to 1.5s.
- `cardinal.decision` from `scripts/cardinal-decision record` (see
  Decision capture below).
- `cardinal.turn_usage` per model call; `cardinal.plan_state` once per
  session and `cardinal.plan_usage` throttled to one snapshot per 10
  minutes, from Codex rate-limit blocks.
- `cardinal.subagent_usage` when Codex hook payloads include subagent
  token totals.

SessionStart injects the initiative branch-naming convention plus the
session's spend-budget standing; the per-prompt spend-limits gate reads the
locally cached verdict (file I/O only) and fails open.

State lives under `~/.codex/cardinal/` (telemetry progress cursors, plan
stamp, limits verdicts, decision setting + ledgers); `cardinal-disconnect`
removes it.

## Decision capture

Same contract as the Claude adapter (`docs/specs/decision-telemetry.md`),
off by default:

```bash
python3 scripts/cardinal-decision on|off|status [--session <id>]
python3 scripts/cardinal-decision record --session <id> --choice "..." [--question ...] [--why ...] ...
```

`CARDINAL_DECISIONS=1/0` in the environment overrides on/off (Codex has no
settings env block). `record` emits one `cardinal.decision` log with the
same connection and resource attributes the telemetry hook uses
(`~/.codex/cardinal.json` + `cardinal-secrets.json`); unconnected, it only
writes the local ledger. The `cardinal-decision` skill wraps on/off/status.

When capture is on, the existing `UserPromptSubmit` hook (no new
`hooks.json` entry) injects the recording instructions plus the session
ledger as `hookSpecificOutput.additionalContext`. The command it gives the
agent is `python3 <plugin root>/scripts/cardinal-decision record --session
<id>`, where the plugin root is the one the stable launcher actually
executed, so it matches the running install. Codex parses a hook's stdout
as a single JSON object, so the decision context is merged into the spend
gate's output (appended after any warn-tier context); a block verdict is
emitted unchanged.

Host-surface evidence that Codex feeds this into the model:
- Docs (https://learn.chatgpt.com/docs/hooks, formerly
  developers.openai.com/codex/hooks): `UserPromptSubmit` is synchronous and
  its `additionalContext` "is added as extra developer context".
- Source (openai/codex `a4354e2d`):
  `codex-rs/hooks/src/events/user_prompt_submit.rs` `parse_completed`
  appends `parsed.additional_context` to `additional_contexts_for_model`;
  `codex-rs/hooks/schema/generated/user-prompt-submit.command.output.schema.json`
  defines `hookSpecificOutput.additionalContext` (with
  `additionalProperties: false` on the top-level object).

Unverified: whether Codex exports a session id to shell commands. The CLI
falls back to `CODEX_SESSION_ID` / `OPENAI_CODEX_SESSION_ID`, but the prompt
always passes `--session` explicitly.

## Tests

```bash
python3 build/vendor.py codex          # from the repo root, once
cd adapters/codex
python3 -m unittest tests.test_parity -v
```

`tests/test_decisions.py` covers `cardinal-decision`, prompt injection and
`git_state` PR linkage. `tests/fixtures.py` puts a stub `gh` first on PATH
for every hook run, so no test reaches GitHub.

Exception to the rule below: `user_prompt_submit.json` was re-captured
from this adapter for the intentional `cardinal_pr_number` /
`cardinal_pr_url` addition (the pre-migration plugin never emitted them);
the diff was only those two attributes, and every other golden was
unchanged.

To re-capture goldens (only if fixtures change — goldens must always come
from the shipped pre-migration plugin, never from this adapter):

```bash
python3 tests/capture_goldens.py --hook /path/to/cardinal-codex-plugin/plugins/cardinal-codex-plugin/hooks/cardinal-codex-telemetry.py
```

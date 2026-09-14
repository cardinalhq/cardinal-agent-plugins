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
  repositories without a first commit. When the branch has a PR the record
  also carries `cardinal_pr_number` and `cardinal_pr_url`; otherwise both
  keys are absent. Codex runs `UserPromptSubmit` synchronously and
  discards its stdout on timeout (5s in `hooks.json`), which would also
  drop the spend-gate warning and decision context. So this path never runs
  `gh`: it reads the PR cache that `cardinal_core.decisions.resolve_pr`
  maintains (`~/.codex/cardinal/decisions/cache/prs.json`, 10 min hit /
  2 min miss). On a miss or stale entry it spawns a detached refresh child
  (own session, devnull stdio) and uses the stale value, if any. The first
  prompt on a new branch therefore lacks the PR keys; later prompts carry
  them. Protected branches never run `gh`.
- `cardinal.decision`, emitted by the `PostToolUse` handler (see Decision
  capture below).
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
off by default. `CARDINAL_DECISIONS=1/0` in the environment overrides
on/off (Codex has no settings env block).

### Why the hook emits, not the CLI

Codex runs the agent's shell commands under the `workspace-write` sandbox
by default. Network is off, and the writable roots are cwd, `/tmp` and
`$TMPDIR`, not `~/.codex`: see `SandboxPolicy::WorkspaceWrite` and
`get_writable_roots_with_cwd` in `codex-rs/protocol/src/protocol.rs`. Hooks
are spawned by Codex outside the sandbox (plain `Command::new`,
`codex-rs/hooks/src/engine/command_runner.rs`). So the flow is:

1. **Prompt.** `UserPromptSubmit` injects the instructions and session
   ledger as `hookSpecificOutput.additionalContext`, merged into the spend
   gate's single JSON object; a block verdict is emitted unchanged. The
   command given is `python3 <plugin root>/scripts/cardinal-decision record
   --session <id> ...`, from the install the launcher actually executed.
   Injection only happens when the managed `PostToolUse` hook is
   registered; otherwise the flow could never fire.
2. **Agent (sandboxed).** `record` validates, prints exactly one
   `cardinal-decision-record:v1 <json>` line (keys `v, session, cwd, id,
   choice, question, why, alt, by, anchor, follows, refines, supersedes`;
   the same wire format as the Cursor adapter) and exits 0. It never reads
   or writes `~/.codex`, never runs `gh`, and makes no network call.
3. **PostToolUse (unsandboxed), the only emitter.** It acts on
   `tool_name` `Bash` whose `tool_input.command` itself invokes
   `cardinal-decision record` and whose `tool_response` has a line starting
   with the marker.
   - **Spoof guard:** matching uses shell-aware tokenization. A
     command-position token (optionally after env assignments or a
     `python3 [-flags]` interpreter) must have basename
     `cardinal-decision` and be followed by `record`. So
     `echo`/`printf`/`grep`/`cat` of marker lines is ignored.
   - **What it does:** applies the on/off gate, re-validates, assigns the
     final id against the ledger, resolves anchors and clusters, writes the
     ledger and emits one `cardinal.decision` with the hook's connection
     and resource attributes.
   - **Report-back:** the result goes to the agent as PostToolUse
     `hookSpecificOutput.additionalContext`. Failures are reported as "NOT
     recorded" with the reason, never swallowed.
   - **Session:** it uses the payload's `session_id` only, never the
     marker's.
   - **Time budget:** the PR comes from cache only, with a detached refresh
     on a miss; D18 `git ls-tree` gets 1s and the OTLP post 1.5s. This fits
     well inside the 10s timeout that `cardinal-connect` registers for this
     hook.

   Identical markers are processed once. Every other shell call returns
   immediately and prints nothing.

`record --emit` is for a human in their own terminal: it records and emits
directly and prints no marker, so PostToolUse never emits it again, even
under `danger-full-access`. `on`/`off` write `~/.codex/cardinal`, which the
sandbox blocks. They report the exact terminal command on failure, and the
`cardinal-decision` skill runs only `status` and tells the user to toggle
from their own terminal.

**Upgrading:** installs connected before this change have no `PostToolUse`
entry. Run `cardinal-connect --repair-hooks` and restart Codex;
`cardinal-decision status` reports whether the hook is registered.

### Host-surface evidence (openai/codex `a4354e2d`, https://learn.chatgpt.com/docs/hooks)

- **UserPromptSubmit context.** `codex-rs/hooks/src/events/user_prompt_submit.rs`
  `parse_completed` appends `additionalContext` to the model context. The
  output schema (`hooks/schema/generated/user-prompt-submit.command.output.schema.json`)
  is a single object. Stdout is discarded on timeout
  (`command_runner.rs` `run_command`, the timeout arm).
- **PostToolUse payload.** `codex-rs/core/src/tools/handlers/unified_exec.rs`
  `post_unified_exec_tool_use_payload` builds it with
  `tool_name: HookToolName::bash()` ("Bash"). `unified_exec_tests.rs`
  asserts `tool_input: {"command": "echo three"}` and
  `tool_response: "three"`. `PostToolUseRequest`
  (`hooks/src/events/post_tool_use.rs`) adds `session_id`, `turn_id`,
  `cwd`, `tool_use_id`, `permission_mode`.
- **When PostToolUse runs.** `core/src/tools/registry.rs` runs it after
  successful dispatch, and the docs say Bash also fires on non-zero exit.
  Code-mode `exec` callbacks dispatch through
  `ToolRouter::dispatch_tool_call_with_code_mode_result` into the same
  registry path (`core/src/tools/router.rs`).
- **Matchers.** An alphanumeric matcher is an exact match
  (`hooks/src/events/common.rs` `matches_matcher`).
- **Background children.** Detached helpers survive a completed hook; the
  process group is killed only on timeout or error (`command_runner.rs`
  `ProcessTreeGuard`).

Unverified or limits:
- Whether Codex exports a session id to shell commands. The CLI falls back
  to `CODEX_SESSION_ID` / `OPENAI_CODEX_SESSION_ID`, but the prompt always
  passes `--session`.
- Codex may truncate very large command output. A record chained with a
  noisy command could lose its marker.
- Worst-case `UserPromptSubmit` time is now git + git_state POST (2s) +
  limits refresh (2s), under the 5s timeout but not by much.

## Tests

```bash
python3 build/vendor.py codex          # from the repo root, once
cd adapters/codex
python3 -m unittest tests.test_parity -v
```

`tests/test_decisions.py` covers the sandboxed CLI (no writes, no `gh`,
no POST), hook-side emission from a Codex-shaped PostToolUse payload, no
double emission, prompt injection and cache-only PR linkage with
background refresh. `tests/fixtures.py` puts a logging stub `gh` first on
PATH for every hook run, so no test reaches GitHub, and seeds the PR cache
for the golden scenario so it doesn't depend on subprocess timing.

Exception to the rule below: `user_prompt_submit.json` was re-captured
from this adapter for the intentional `cardinal_pr_number` /
`cardinal_pr_url` addition (the pre-migration plugin never emitted them).
The diff was only those two attributes; every other golden was unchanged.

To re-capture goldens (only if fixtures change — goldens must always come
from the shipped pre-migration plugin, never from this adapter):

```bash
python3 tests/capture_goldens.py --hook /path/to/cardinal-codex-plugin/plugins/cardinal-codex-plugin/hooks/cardinal-codex-telemetry.py
```

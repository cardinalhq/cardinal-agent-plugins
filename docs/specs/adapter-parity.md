# Adapter parity: PR linkage + decision capture

Status: in progress (branch `feat/adapter-parity`)

## Why

An audit of all seven adapters (2026-09-14) found:

| Adapter | PR linkage | Decision capture |
|---|---|---|
| claude | partial — `git_state` has repo/branch/head_sha, no PR | yes |
| codex | partial | no |
| cursor | partial (cloud agents need `--project` hooks; `sessionStart` doesn't run there) | no |
| gemini | partial | no |
| omnigent | no — branch/repo only, no head_sha | no |
| opencode | partial | no |
| pi | partial | no |

PR number/URL only travelled on Claude's `cardinal.decision` events. Outcomes
scoring had to join session → PR server-side on branch. Every adapter should
emit the same two capabilities.

## Target contract

### 1. PR linkage on `cardinal.git_state`

Every `cardinal.git_state` record carries the branch's PR when one resolves:

- Keys follow the adapter's existing `git_state` key style: Claude uses dotted
  keys (`cardinal.pr_number`, `cardinal.pr_url`); adapters that emit
  `cardinal_head_sha` style use `cardinal_pr_number`, `cardinal_pr_url`.
- Resolution goes through `cardinal_core.decisions.resolve_pr(cwd, repo,
  branch, decisions.cache_dir(runtime_dir))` — `gh pr view`, cached per
  repo+branch (10 min hit / 2 min miss), skipped on protected branches.
- No PR → keys absent. Never fail or delay the record because `gh` is missing.
- Synchronous hooks must bound the `gh` call (short timeout) so a prompt is
  never held for more than ~1.5s on a cache miss.

### 2. Opt-in decision capture

Same semantics as Claude (`docs/specs/decision-telemetry.md`):

- Toggle: `decisions.is_enabled(runtime_dir, env CARDINAL_DECISIONS)`, default
  off, flipped by `cardinal-decision on|off|status`.
- Record: `cardinal-decision record --session <id> …` (or a host-native tool
  where the host has no shell-out model) → `decisions.build_decision` →
  ledger → one `cardinal.decision` OTLP log with `decision_attributes`
  (repo, branch, head_sha, PR, anchors, code clusters).
- Prompt: when enabled, the host's context-injection surface tells the agent
  to record material choices and shows the session ledger
  (`decisions.render_ledger`).

Host surfaces must be verified against the host's real hook/plugin API, not
assumed.

Agent shell commands may run sandboxed: Codex (`workspace-write`, the default)
and Cursor 3.6+ Auto-review (macOS/Linux) block network and writes outside the
workspace. On those hosts the agent-run `record` command only validates and
prints a marker; an unsandboxed hook parses it, writes the ledger, and emits
the single `cardinal.decision`. Claude's sandbox is opt-in and prompts to
escalate, so Claude keeps CLI-side emission. Where a host cannot inject context, the adapter documents the gap
instead of shipping a flow that never fires.

## Per-adapter work

| Adapter | PR linkage | Decision capture |
|---|---|---|
| claude | add PR to `hooks/git-state.py` (async hook) | done |
| codex | add PR to `git_state` in `cardinal-codex-telemetry.py` | CLI + UserPromptSubmit injection |
| cursor | add PR to `beforeSubmitPrompt` `git_state` | CLI + whichever Cursor surface can inject context (hook output or rule); cloud-agent gap documented |
| gemini | add PR to BeforeAgent `git_state` | CLI + BeforeAgent/SessionStart `additionalContext` |
| opencode + pi | add PR in shared `common/native/cardinal_native.py` | `decision` CLI subcommand, a request/response bridge call behind a `cardinal_record_decision` host tool, and host-native prompt injection |
| omnigent | add head_sha + PR where the server has a local checkout | only if the policy API can reach the model; otherwise documented as unsupported |

## Verification

- Targeted adapter tests (pytest per adapter, `npm run test:native` for
  native). Goldens re-captured only for the intended `git_state` change.
- An independent review per adapter checks the host surface claims against
  the host's API/types and runs the tests.

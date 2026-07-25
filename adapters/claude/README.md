# Cardinal Claude Code plugin

Connect Claude Code to Cardinal telemetry and the unified MCP endpoint in
one browser-approved consent. Migrated from
[cardinalhq/cardinal-claude-plugin](https://github.com/cardinalhq/cardinal-claude-plugin)
(P4 of the agent-core extraction — see `../../docs/specs/agent-core.md`);
shared algorithms and the OTLP contract now come from `core/cardinal_core`.

## Layout

- `hooks/` — one script per Claude Code hook event, each best-effort and
  silent on failure (never blocks the agent loop):
  - `git-state.py` (UserPromptSubmit, async) — `cardinal.git_state` +
    spend-limits verdict refresh.
  - `limits-gate.py` (UserPromptSubmit, sync) — reads the cached verdict
    and turns it into hook output; the only turn-critical hook, network-free.
  - `initiative-convention.py` (SessionStart, sync) — injects the
    branch-naming convention + budget standing as `additionalContext`.
  - `plan-state.py` (SessionStart, async) — `cardinal.plan_state` /
    `cardinal.plan_usage` from the OAuth profile+usage cache.
  - `invariant-check.py` (PreToolUse on Edit\|Write\|MultiEdit, sync) —
    advisory Invariant `check-pr.ts` guard.
  - `subagent-usage.py` (PostToolUse on Agent\|Task, async) —
    `cardinal.subagent_usage` summed from the subagent's own transcript.
  - `turn-usage.py` (Stop, async) — per-model-call `cardinal.turn_usage` /
    `cardinal.turn_tool`.
  - `plan-usage.py` (Stop, async, throttled) — `cardinal.plan_usage`
    refresh when the cached usage half is stale.
  - `_otel_settings.py`, `_plan_cache.py`, `_plugin_version.py`,
    `_debug_capture.py` — shared local helpers imported by the hooks
    above (not part of vendored `cardinal_core`).
  - `cardinal_core/` — vendored copy of `core/cardinal_core`, created by
    `python3 build/vendor.py claude` at build time. Gitignored; run the
    vendor step after checkout before executing hooks or tests.
- `bin/` — `cardinal-connect`, `cardinal-status`, `cardinal-disconnect`,
  `cardinal-install-site`.
- `skills/` — `connect`, `status`, `disconnect`, `install-site`,
  `optimize-toolkit`.
- `tests/` — hook-level subprocess tests, `tests/goldens/` parity
  fixtures, `tests/fixtures.py` (shared scenario builder + `run_hook` /
  `base_env` harness), `tests/capture_goldens.py`.

## Capturing debug payloads for fixtures

Set `CARDINAL_CLAUDE_DEBUG_PAYLOADS=1` in the environment Claude Code
launches hooks in (e.g. `CARDINAL_CLAUDE_DEBUG_PAYLOADS=1 claude` for one
session) to make every hook write its raw stdin payload to:

```
~/.claude/cardinal/telemetry/debug/<event>-<time_ns>.json
```

`<event>` is the Claude Code hook event name (`UserPromptSubmit`,
`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`) — one file per hook
firing, so a session with several hooks bound to the same event (e.g.
`initiative-convention.py` and `plan-state.py` both on `SessionStart`)
produces multiple dumps for that event, each from a different hook.

The dump happens as the FIRST thing each hook does after parsing stdin —
before session-id sourcing, matcher checks, redaction, or any network
call — so a capture survives even if the hook errors or exits early
further down. This mirrors the affordance already wired into the Codex,
Cursor, and Gemini adapters (`CARDINAL_CODEX_DEBUG_PAYLOADS`,
`CARDINAL_CURSOR_DEBUG_PAYLOADS`, `CARDINAL_GEMINI_DEBUG_PAYLOADS`); see
`docs/local-notes/plans/phase0-fixture-inventory.md` for why Claude
needed this wiring — it was the one adapter left "UNWIRED".

Unlike those three adapters' raw dumps, Claude's capture path scrubs
every string value for known secret patterns
(`cardinal_core.redaction.scrub_payload_recursively`, built on the same
`scrub_secrets` used by the live telemetry wire) before the payload
touches disk — Claude tool payloads routinely carry file contents,
command text, and transcript excerpts that the other adapters' raw hook
payloads have not historically included. A detected secret is replaced
in place with `<redacted:PATTERN_NAME>`; structure (dict keys, list
order/length, non-string values) is left untouched.

The scrub makes these dumps safe to **inspect**, but still review a dump
by hand before copying it into a committed fixture folder — the known-
pattern list is not exhaustive, and dumps can otherwise contain whatever
shape Claude Code actually sends (prompts, tool arguments, transcript
paths, environment-shaped strings).

**WARNING: keep this flag OFF in production.** Dumps are unbounded —
every hook firing writes a new file, with no cap or rotation — and will
accumulate quickly on an active session. Turn it on only for a scoped
capture session, harvest the dumps, then turn it back off.

## Tests

```bash
python3 build/vendor.py claude          # from the repo root, once
cd adapters/claude
python3 -m unittest discover tests -v
```

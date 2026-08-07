# Phase 0.D — Fixture inventory & capture gap analysis

Reconnaissance run 2026-07-25 by subagent. Establishes which of the six Phase 0
scenarios have usable synthetic bootstraps and which are hard gaps requiring
real captures with `CARDINAL_*_DEBUG_PAYLOADS=1`.

**Bottom line:** every existing fixture is hand-written by adapter engineers
for parity/golden tests, not a `CARDINAL_*_DEBUG_PAYLOADS=1` dump. No real
captures are checked in anywhere. `skill-invocation` (both `requested-only`
and `executed`) is a total 10/30 gap across all adapters — no scenario
anywhere models skill request-vs-execution.

## Matrix (adapter × scenario)

Legend: **bootstrap** = usable synthetic fixture exists, replace with real
capture later. **gap** = no fixture, real capture mandatory.

| Scenario | claude | codex | cursor | gemini | omnigent |
|---|---|---|---|---|---|
| simple-turn | bootstrap: `adapters/claude/tests/fixtures.py:144-150` | bootstrap: `adapters/codex/tests/fixtures.py:233-280` | bootstrap: `adapters/cursor/tests/goldens/02-beforeSubmitPrompt-plain.json` | bootstrap: `adapters/gemini/tests/scenarios.py:141-195` | bootstrap: `adapters/omnigent/tests/fixtures.py:103-149` |
| tool-invocation | bootstrap: `fixtures.py:159-168` (Read/Bash blocks) | bootstrap: `fixtures.py:251-263` (exec_command / apply_patch / mcp) | bootstrap: goldens `03-postToolUse-shell-compound.json`, `04-postToolUse-mcp.json` | bootstrap: `scenarios.py:196+` (after-tool golden) | bootstrap: `fixtures.py:150-195` (tool_call_shell/mcp/result) |
| parallel-tools | weak bootstrap: `fixtures.py:172-183` — 4 concurrent tool_use blocks in one turn, not a true concurrent-dispatch capture | **gap** | **gap** | **gap** — AfterTool fires sequentially | **gap** |
| subagent-invocation | bootstrap: `fixtures.py:193-233` (`make_subagent_transcript`) + `scenario_subagent_usage_full` | bootstrap: `fixtures.py:352-363` (SubagentStop) — **real shape unconfirmed per plan** | bootstrap: `goldens/07-subagentStop.json` — **no identity beyond generation_id** | weak bootstrap: `goldens/after-agent.json` fires 7× with empty payload — **real AfterAgent key shape is a Phase-0 unknown** | bootstrap: `fixtures.py:196-256` (sys_session_send delegate) — **claude/codex-native subagent visibility gap called out in plan** |
| skill-invocation (requested-only) | **gap** — only slash-command detection, not skill lifecycle | **gap** | **gap** | **gap** | **gap** — `fanout` skill in comments only |
| skill-invocation (executed) | **gap** | **gap** | **gap** | **gap** | **gap** |
| session-continuation | weak bootstrap: `fixtures.py:144-188` (2-turn same-session, does not cross resumed boundary) | best bootstrap: `fixtures.py:311-337` + goldens `stop_first.json`/`stop_second.json` | **gap** | **gap** — pre-compress/session-end goldens exist but no resume-continue | **gap** |

## Debug payload dump wiring

| Adapter | State | Path | Command |
|---|---|---|---|
| claude | **UNWIRED** — `debug_dir` exists at `adapters/claude/hooks/cardinal_core/paths.py:130` but `dump_debug_payload` is never called from any hook. **Must be added before Phase 0.D claude capture can run.** | `~/.claude/cardinal/telemetry/debug/` | (n/a until wired) |
| codex | wired | `~/.codex/cardinal/telemetry/debug/<event>-<time_ns>.json` | `CARDINAL_CODEX_DEBUG_PAYLOADS=1 codex ...` |
| cursor | wired | `~/.cursor/cardinal/telemetry/debug/<event>-<time_ns>.json` | `CARDINAL_CURSOR_DEBUG_PAYLOADS=1 cursor-agent ...` |
| gemini | wired | `~/.gemini/cardinal/telemetry/debug/<event>-<time_ns>.json` | `CARDINAL_GEMINI_DEBUG_PAYLOADS=1 gemini ...` |
| omnigent | **no dump mechanism** — Python library using contextvars, not a hook plugin. Capture requires a different approach (test-mode sink in `telemetry.py`). | (n/a) | (n/a) |

## Cross-check with lakerunner

`~/workspace/lakerunner/internal/agentsessions/testdata/fixtures/*.json` (2
files) — flattened, already-ingested OTLP-shaped events (source:
`harness.generate_fixtures`), not raw adapter payloads. Useful only as
post-ingest reference, not for seeding `fixtures/<adapter>/<scenario>/`.

## Prerequisites to unblock Phase 0.D captures

1. **Wire `dump_debug_payload` into every Claude hook** — one-line change per
   hook file, gated on `CARDINAL_CLAUDE_DEBUG_PAYLOADS`. Should be a
   separate small PR before capture sessions.
2. **Add an omnigent test-mode sink** — a mode-flag on
   `cardinal_omnigent/telemetry.py` that writes each event to
   `~/.cardinal/omnigent-debug/` in addition to (or instead of) OTLP emit.
3. **Design the `skill-invocation` capture recipe** — since all five adapters
   are gaps here, need explicit test sessions where:
   - `requested-only`: user types `/nonexistent-command` (or a real command
     that fails to load) — capture shows the command in prompt but no
     downstream skill activity.
   - `executed`: user invokes a known skill (`/cardinal:status`), skill loads
     and drives tool calls — capture shows the full chain.

## Recommended capture playbook (once wiring is in place)

For each adapter:
- Run 3 sessions per scenario with `CARDINAL_<adapter>_DEBUG_PAYLOADS=1`.
- Copy dumps from `~/.<adapter>/cardinal/telemetry/debug/` into
  `fixtures/<adapter>/<scenario>/session-<n>/`.
- Redact per `docs/privacy-redaction.md` **before** committing (scrub secrets,
  hash prompts, drop origin URL credentials).
- Cross-reference against `adapter-capability-matrix.md` — every `native`
  claim in the matrix must have a fixture path here.

## What can proceed without new captures

Phase 0.A/B/F (envelope + canonical model + privacy spec) are complete and
committed. The existing synthetic bootstraps are shape-accurate enough for
Phase 1 reducer development on `simple-turn` / `tool-invocation` /
`subagent-invocation` / `session-continuation` (Claude, Codex). Everything
else needs real captures before it can be trusted end-to-end.

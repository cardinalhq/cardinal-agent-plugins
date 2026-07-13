# Core gaps found while migrating the Cursor adapter

Core (`core/cardinal_core`) was extracted from the codex/gemini sources;
Cursor diverges most (camelCase payloads, a different hook-output
schema, no per-model-call token counts). These are the places where core
could not be consumed as-is. Per the migration rules, everything below
was worked around adapter-side; each entry states what was needed, why,
and a suggested core API.

> **Status (core 0.2.0):** gaps 1–3 are RESOLVED — core shipped the
> suggested APIs and the adapter-side shims were deleted (see the
> per-gap status lines below and REPORT.md §core 0.2.0). Gap 4 stays
> adapter-side by design; gap 5 was informational only.

## 1. `limits.gate_output()` fuses policy with the hookSpecificOutput rendering

**RESOLVED by core 0.2.0.** `limits.gate_decision(paths, session_id) ->
GateDecision | None` and `limits.ack_band(paths, session_id, band)`
landed exactly as suggested; `gate_output()` is now a thin renderer over
them. The adapter's local ~60-line policy walk was deleted —
`limits_gate_output()` in the hook script is now purely the Cursor
channel mapping (`{continue:false, user_message}` for block /
strict-warn escalation, staged notify for warn/notify, ack only when a
message is actually staged). One residual wrinkle: `GateDecision.reason`
is populated for the block tier only, so the strict-warn escalation's
fallback copy (warn verdict with no `user_message`) re-reads the verdict
via `limits.read_verdict()` to preserve the pre-0.2.0 `block_reason`
fallback byte-for-byte. If core ever carries `reason` (or `block_reason`)
on warn-tier decisions too, that re-read can go.

**What I needed.** Cursor's `beforeSubmitPrompt` output schema is
`{continue: false, user_message}` only — no `additionalContext`, no
`systemMessage`, and non-blocking messages cannot be surfaced on the
submit path at all (parity spec Divergence E). Warn/notify must instead
be *staged* to a file and surfaced on the next `postToolUse` via
`additional_context`, and `CARDINAL_CURSOR_STRICT_WARN=1` escalates warn
to block.

**Why core doesn't fit.** `limits.gate_output()` performs the whole
policy walk (block age check, override downgrade, band hysteresis, ack
write) and then renders Claude/Codex/Gemini's
`hookSpecificOutput`/`systemMessage` JSON in the same function. There is
no way to get the *decision* without the *rendering*, so the Cursor
adapter re-implements the ~60-line policy walk in
`limits_gate_output()` (hooks/cardinal-cursor-telemetry.py) on top of
core's lower-level primitives (`read_verdict`, `BLOCK_MAX_AGE_SEC`,
`WARN_MAX_AGE_SEC`, `AgentPaths.override_path`/`ack_path`,
`atomic_write_json_compact`). That duplication is exactly the drift risk
the extraction was meant to remove.

**Suggested API.** Split policy from channel:

```python
@dataclass
class GateDecision:
    tier: Literal["block", "warn", "notify"]  # after override downgrade
    band: int
    reason: str | None          # server block_reason / fallback copy
    agent_context: str | None
    user_message: str | None
    is_new_band: bool           # hysteresis: band rose vs last ack

def gate_decision(paths, session_id) -> GateDecision | None: ...
def ack_band(paths, session_id, band) -> None: ...
```

`gate_output()` becomes a thin hookSpecificOutput renderer over
`gate_decision()`; the Cursor adapter renders `{continue:false,
user_message}` / stages a notify file from the same decision.

## 2. No staged-notify channel in `AgentPaths` / `limits`

**RESOLVED by core 0.2.0.** `AgentPaths.notify_path(session_id)`,
`limits.stage_notify(paths, session_id, message, band)` and
`limits.consume_notify(paths, session_id)` landed; the adapter's local
`notify_path()` / `consume_notify()` were deleted. Core's staged-file
shape (`{"message", "band", "staged_at"}`) and one-shot read-and-delete
semantics (invalid/empty file → None without unlink) are identical to
what the adapter wrote, so no behavioral difference is observable —
goldens (step 05, notify surfacing) and the behavioral suite pass
unchanged.

**What I needed.** The Divergence-E staging file
`<conv>.notify.json` under `<agent-home>/cardinal/limits/`, written by
the gate and consumed (read-and-delete, one-shot) by `postToolUse`.

**Why core doesn't fit.** `AgentPaths` knows `verdict/ack/override`
paths but has no `notify_path()`, and `limits` has no
`stage_notify()`/`consume_notify()`. Kept adapter-side
(`notify_path`, `consume_notify` in the hook script).

**Suggested API.** `AgentPaths.notify_path(session_id)` plus
`limits.stage_notify(paths, session_id, message, band)` and
`limits.consume_notify(paths, session_id) -> str | None`. Any future
adapter whose prompt-time hook lacks a non-blocking message slot (or
omnigent delivering deferred standing) needs the same channel.

## 3. `otlp.resource_attrs()` unconditionally appends `cardinal.core_version`

**RESOLVED by core 0.2.0** (harness side). `core/tests/harness.py::
_normalize` now DROPS `cardinal.core_version` (reconciling key presence
against pre-migration goldens) and pins `cardinal.plugin_version` and
the OTel scope version. The adapter's `tests/fixtures.py::_scrub`
shrank to the one Cursor-specific rule left: sandbox tempdir path
replacement. Goldens still compare byte-equal. (The `extra:` parameter
idea for `resource_attrs()` did not ship; the adapter still stamps
`cursor.*` by dict mutation, which remains workable — insertion order
is stable and golden-locked.)

**What I needed.** Byte-equal output against goldens captured from the
shipped v0.2.0 plugin, which never emitted `cardinal.core_version`.

**Why core doesn't quite fit.** The new attribute is presumably wanted
going forward, but `core/tests/harness.py::_normalize` only *pins its
value* — it cannot reconcile key **presence** between pre-migration
goldens and post-migration output. Worked around in the adapter-local
normalizer (`tests/fixtures.py::_scrub` drops the attribute entirely
before comparison; core harness untouched).

**Suggested API.** Either make harness `_normalize` drop (not pin)
`cardinal.core_version`, or document that pre-migration goldens must be
compared through an adapter-side scrub that strips it. Also mildly
useful: an `extra: dict[str, str] | None` parameter on
`resource_attrs()` for adapter stamps (the Cursor adapter appends
`cursor.model` / `cursor.model_id` / `cursor.model_params` /
`cursor.version` by dict mutation today — workable, but insertion order
is load-bearing for byte-parity and worth making explicit).

## 4. No per-adapter strict-warn escalation knob

**REMAINS adapter-side — by design, not a core gap.** With gap #1's
`gate_decision()` landed in 0.2.0, escalation is now exactly the pure
rendering concern predicted below: `strict_warn_enabled()` plus a
block-body render over a warn-tier `GateDecision` (~10 lines). Nothing
further should move to core.

**What I needed.** `CARDINAL_CURSOR_STRICT_WARN=1` (documented in the
Cursor README and parity spec) escalating a warn verdict to a block.

**Why core doesn't fit.** Core has no concept of warn escalation — on
Claude/Codex/Gemini a warn is deliverable inline so escalation is never
needed. Kept adapter-side (`STRICT_WARN_ENV`, `strict_warn_enabled()`).
This is arguably *correctly* adapter-side; listed for completeness. If
gap #1's `gate_decision()` lands, escalation stays a pure rendering
concern in the adapter.

## 5. Cosmetic / non-blocking observations

- `session.load_progress()` injects a `plan_stamp` dict into the
  progress state, so `save_progress()` persists it into
  `<conv>.json`. Harmless (nothing on the wire) but the old plugin's
  progress files carried only the counters + `last_prompt_generation`;
  file-format drift worth knowing about.
- `initiative.detect_command`'s `<command-name>` tag form is
  Claude-specific and cannot occur on Cursor; the adapter inherits it
  via core with no ill effect (kept for cross-adapter fixture parity).
- Core `pricing` was NOT consumed: the shipped Cursor plugin's pricing
  table was dead code (Cursor exposes no token counts — parity spec gap
  D — so `cost_usd` is never computed). Dropping it removed ~40 lines;
  when Cursor exposes usage, `cardinal_core.pricing` is the right home
  and no adapter table should return.

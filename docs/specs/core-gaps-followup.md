# Core 0.2.0 — gap reconciliation plan

Status: **planned** · Input: the four adapters' `CORE_GAPS.md` files from
the P1–P4 parallel migration (2026-07-13). Nothing here blocked byte-equal
parity; every gap has a working adapter-side shim. Reconciling them into
core deletes those shims and closes the residual drift surface.

Ranked by how many adapters (present or future) each serves:

## 1. Split the limits gate: `gate_decision()` / `ack_band()` (cursor #1)

`gate_output()` fuses the policy walk (block age, override downgrade,
band hysteresis, ack write) with the hookSpecificOutput rendering. Cursor
re-implements the ~60-line policy walk because its output schema is
`{continue: false, user_message}`. The omnigent adapter will need the
decision-without-rendering shape too (PolicyResult, not hook JSON).

Adopt cursor's suggested API: a `GateDecision` dataclass +
`gate_decision(paths, session_id)` + `ack_band(paths, session_id, band)`;
`gate_output()` becomes a thin renderer over it. Deletes the cursor shim.

## 2. Injectable credential in limits refresh (claude #1)

`maybe_refresh_verdict` hardwires `paths.read_secrets()`; Claude's key
lives in OTel settings and omnigent's will come from server config. Add
`api_key: str | None = None` (default: `ingest_api_key(paths)`), and give
`session.budget_standing` the same pass-through. Deletes Claude's
`_limits.py` shim and its local `_budget_standing`.

## 3. `IngestConnection.extra_headers` (claude #2)

Claude forwards every pair from `OTEL_EXPORTER_OTLP_HEADERS`; core sends
exactly one auth header. Latent-only today (connect writes one pair), but
one field + a dict-merge in `emit_records` closes it.

## 4. `resource_attrs()` passthrough mode (claude #3, cursor #3)

Core's builder emits a fixed key set with `unknown` defaults and an
unconditional `cardinal.core_version`; Claude must pass through whatever
CSV cardinal-connect baked into `OTEL_RESOURCE_ATTRIBUTES`, order
preserved. Add a `from_pairs(pairs, **overrides)` constructor path and
make `core_version` stamping explicit rather than automatic.

## 5. Staged-notify channel (cursor #2)

`AgentPaths.notify_path()` + `limits.stage_notify()/consume_notify()`.
Needed by any adapter whose prompt-time hook lacks a non-blocking message
slot (Cursor's Divergence E; plausibly omnigent deferred standing).

## 6. Golden-harness normalization (all four, harness-level)

Every agent locally re-implemented the same two normalizations:
pin/scrub `cardinal.plugin_version` (+ scope version), and DROP
`cardinal.core_version` (pre-migration goldens lack the key; pinning
can't reconcile presence-vs-absence). Fold both into
`StubIngest.normalized_batches()`.

## 7. Smaller items

- Injectable retry ladder in `verify_ingest_reachable` (claude #4) — a
  `sleeps: tuple[float, ...]` parameter; also lets tests run it fast.
- `cardinal-status` scripts duplicate deviceflow probe semantics with
  different copy (codex observation) — export the probes' status-message
  rendering from core.
- `plan_stamp_path` layout divergence (claude #5) — moot while
  `_plan_cache.py` is adapter-only; revisit only if that decision changes.

## Sequencing

Single release: core 0.1.0 → 0.2.0, then one shim-deletion pass across
the four adapters. Parity stays proven by the existing goldens — none of
these changes may alter emitted bytes for the current adapters (the
contract test and each adapter's parity suite guard that). Ship BEFORE
the omnigent adapter (P5) so it lands on the corrected APIs (items 1, 2,
4 are direct omnigent prerequisites).

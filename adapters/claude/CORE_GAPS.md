# Core gaps found while migrating the Claude adapter (P4)

Workarounds are all adapter-side; nothing here blocked byte-equal
parity. Listed in priority order for a future core rev.

**Status (core 0.2.0):** gaps 1–4 RESOLVED and adopted by this adapter
(the `_limits.py` shim is deleted; connection/resource construction and
the connect probe ladder now go through core APIs). Gap 5 remains moot
by design. Per-gap resolution notes below.

## 1. `limits.maybe_refresh_verdict` hardwires key sourcing to `paths.read_secrets()`

> **RESOLVED by core 0.2.0.** `maybe_refresh_verdict(..., api_key=...)`
> and `session.budget_standing(paths, session_id, cwd, api_key=...)`
> take the credential as an argument. `hooks/_limits.py` is DELETED:
> git-state.py calls core `maybe_refresh_verdict` and
> initiative-convention.py calls core `budget_standing`, both passing
> `_otel_settings.ingest_api_key(...)` through. What stays adapter-side
> (by design, not as a gap): the OTel-settings key acquisition itself
> (`_otel_settings.ingest_api_key`) and the payload/env session-id
> sourcing in initiative-convention.py — both Claude Code facts, not
> limits logic.

**Needed:** the verdict-refresh TTL loop with the ingest key coming from
Claude's OTel settings (`~/.claude/settings.json` env →
`OTEL_EXPORTER_OTLP_HEADERS`, `x-cardinalhq-api-key=<key>`). Claude has
no `cardinal-secrets.json`; Claude Code's native exporter owns the
credential file, and the plugin must read the same source of truth.

**Workaround:** `hooks/_limits.py` reproduces the ~20-line TTL loop
around core `fetch_status`/`read_verdict`/`atomic_write_json_compact`.
Same reason `session.budget_standing` couldn't be used —
`initiative-convention.py` keeps a local `_budget_standing` that
composes core `git_facts` + `standing_lines` around the shim.

**Suggested API:** accept the credential as an argument —
`maybe_refresh_verdict(..., api_key: str | None = None)` (default
`ingest_api_key(paths)`), and `budget_standing(..., refresh=callable)`
or the same `api_key` pass-through. Identity-as-argument is already the
core design principle; the key should follow it.

## 2. `otlp.IngestConnection` speaks exactly one auth header

> **RESOLVED by core 0.2.0.** `IngestConnection.extra_headers:
> tuple[tuple[str, str], ...]` is merged into every POST by
> `emit_records`. `_otel_settings.ingest_connection()` now forwards ALL
> parsed `OTEL_EXPORTER_OTLP_HEADERS` pairs: the `x-cardinalhq-api-key`
> pair (else the first pair) as api_key/api_header, the rest via
> extra_headers. The latent drop-extras regression is closed.

**Needed:** the shipped hooks forwarded EVERY pair parsed from
`OTEL_EXPORTER_OTLP_HEADERS` onto the POST (the env var is
spec-comma-separated and may carry several headers).

**Workaround:** `_otel_settings.ingest_connection()` picks the
`x-cardinalhq-api-key` pair (falling back to the first pair) and drops
any extras. No known deployment sets more than one pair —
cardinal-connect writes exactly one — so this is a latent, not live,
regression.

**Suggested API:** `IngestConnection.extra_headers: dict[str, str] = {}`
merged into the request headers by `emit_records`.

## 3. `otlp.resource_attrs()` has a fixed shape that can't passthrough

> **RESOLVED by core 0.2.0.** `otlp.passthrough_resource_attrs(pairs, *,
> service_name, agent_runtime, plugin_version, include_core_version=True)`
> is order-preserving, setdefaults service.name/agent.runtime, and
> overwrites cardinal.plugin_version at emit time.
> `_otel_settings.resource_attrs()` now delegates to it (keeping only
> the OTEL_RESOURCE_ATTRIBUTES CSV parse and the plugin.json version
> read). One deliberate wire change: resources now also carry
> `cardinal.core_version` — the parity normalizer drops it, goldens
> unchanged.

**Needed:** Claude's resource attributes are whatever CSV
cardinal-connect baked into `OTEL_RESOURCE_ATTRIBUTES` (order
preserved), plus `service.name`/`agent.runtime` setdefaults and the
emit-time `cardinal.plugin_version` overwrite. Core's builder emits a
fixed key set including `cardinal.core_version` and `unknown` defaults,
which would change the wire bytes.

**Workaround:** `_otel_settings.resource_attrs()` keeps the passthrough
construction; core's builder is unused by this adapter.

**Suggested API:** `resource_attrs(base: dict[str, str] | None = None,
stamp_core_version: bool = False, ...)` — or split "identity defaults"
from "version stamping".

## 4. `deviceflow` ingest-probe retry ladder is not injectable

> **RESOLVED by core 0.2.0.** `verify_ingest_reachable(..., sleeps=...)`
> is injectable; cardinal-connect passes the shipped ladder
> `(1, 2, 4, 8)`, restoring the ~15s persistent-401 abort (the ported
> abort test's sleep budget was adjusted back accordingly).

**Needed:** the shipped connect retried transient 401s for ~15s
(1+2+4+8); core's `INGEST_PROBE_RETRY_SLEEPS` is ~63s (…+16+32). CLI
behavior (not OTLP) — we accepted core's longer ladder and adjusted the
ported test, but a persistent-401 abort now takes ~63s.

**Suggested API:** `verify_ingest_reachable(..., retry_sleeps:
tuple[float, ...] = INGEST_PROBE_RETRY_SLEEPS)`.

## 5. `AgentPaths.plan_stamp_path` doesn't match Claude's plan-cache location

> **Still moot as of core 0.2.0** — the plan cache is adapter-only
> (`_plan_cache.py`, verbatim by standing decision) and the adapter
> never touches `plan_stamp_path`. Nothing to adopt.

Core puts the plan stamp at `<home>/cardinal/telemetry/plan.json`;
Claude's shipped cache is `<home>/cardinal/plan.json` and is owned by
`_plan_cache.py`, which stays adapter-only by explicit decision (OAuth
profile fetch: organization_type, billing_type, seven-day windows).
No workaround needed — the adapter never touches `plan_stamp_path` —
but if plan caching ever moves toward core, the layouts must be
reconciled with a migration.

## Non-gaps worth recording

- `AgentPaths(home=~/.claude)` matches the shipped state layout exactly
  (`cardinal.json`, `cardinal-pending.json`,
  `cardinal/limits/<session>.{verdict,ack,override}.json`), so
  `limits.gate_output` and `limits_config` were adopted unchanged.
- `session.convention_prompt("Claude Code")` is byte-identical to the
  shipped PROMPT (proved by the `initiative_convention` stdout golden).
- Claude emits cost natively — `pricing.py` deliberately unused.

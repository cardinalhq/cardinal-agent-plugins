# Omnigent adapter (P5) — spec

Status: **draft** · Verified against omnigent commit `6e71197`
(main, 2026-07-13; latest tag v0.5.1; `requires-python >= 3.12`).
Prerequisites: core 0.2.0 (gaps 1, 2, 4) — landed.

## What it is

`adapters/omnigent/` — a pip-installable package (`cardinal-omnigent-policy`)
exposing Cardinal policy modules for omnigent's policy engine
(`policy_modules` + module-level `POLICY_REGISTRY`). Unlike the four CLI
adapters, core is a real dependency, not vendored. One adapter covers every
harness omnigent runs (Claude, Codex, Cursor, Hermes, Pi, OpenCode, custom
YAML agents).

## Verified integration facts

Sourced from the repo at the pinned commit; adapter must be re-verified on
omnigent upgrades (alpha — contract may churn).

- **Policy callable**: `def policy(event: PolicyEvent) -> dict | None`
  (sync or async; optional `config` second arg by arity). Return
  `{"result": "ALLOW"|"DENY"|"ASK", "reason", "data", "state_updates",
  "set_labels"}`; `None` abstains. (`omnigent/policies/function.py`,
  `schema.py`.)
- **Registration**: server config YAML `policy_modules:` lists modules
  scanned for `POLICY_REGISTRY`; dotted-path resolution via importlib;
  factory form (`function: {path, arguments}`) for closure state.
  (`registry.py`.)
- **Phases**: request / response / tool_call / tool_result / llm_request /
  llm_response. `PHASE_TOOL_CALL` and `PHASE_REQUEST` fail CLOSED when a
  policy can't evaluate. (`types.py` FAIL_CLOSED_PHASES.)
- **Usage data**: `llm_response.data.usage` = per-TURN totals
  (`input_tokens`, `output_tokens`, `total_tokens`, `context_tokens`,
  cache buckets, `model`) — cumulative across the turn's API calls, one
  event per round-trip. Every phase additionally carries
  `event.context.usage` = cumulative session `{input_tokens,
  output_tokens, total_tokens, total_cost_usd}` (server-maintained).
  (`schema.py`, `inner/claude_sdk_executor.py`.)
- **State**: `state_updates` with set/increment/delete/append actions;
  applied on ALLOW/DENY, withheld on ASK. Session-scoped (native engine
  docstring says workflow-turn-scoped — treat as engine-dependent).
- **Identity**: `event.context.actor` = `{run_as: email, client_id}`.
- **Execution locus**: policies run in the omnigent SERVER process (runner
  proxies `policy_evaluation.requested` to
  `POST /sessions/{id}/policies/evaluate`); the OS sandbox wraps the
  agent, not policies. **No cwd/repo/branch anywhere in the contract.**

## Design

Two policy modules, both consuming `cardinal-agent-core` as a pip dep:

### `cardinal_omnigent.telemetry` (observe-only, all phases, always ALLOW/abstain)

| Cardinal event | Omnigent phase | Notes |
| --- | --- | --- |
| `cardinal.git_state` | `request` | initiative facts from labels — see below |
| `cardinal.turn_usage` + `api_request` | `llm_response` | **per-turn granularity** (documented divergence: CLI adapters emit per-model-call where available); `cost_usd` from delta of `context.usage.total_cost_usd`, falling back to core pricing tables keyed on `data.model` |
| `cardinal.turn_tool` + `tool_result` | `tool_call` / `tool_result` | core bashclass on shell tools; tool_name from resolved `event.target` |
| `cardinal.subagent_usage` | `response` of sub-agent sessions | omnigent's BeforeAgent/AfterAgent equivalent is session-scoped; needs a per-engine mapping pass during implementation |

Resource attrs via `otlp.resource_attrs(...)`: `agent.runtime="omnigent"`,
plus `cardinal.omnigent_harness` from `event.context.harness` so
downstream can slice by underlying harness. `user.email` from
`context.actor.run_as` per event — core 0.2.0's identity-as-argument.
Emission via `otlp.emit_records` with an `IngestConnection` built from
the policy's `config:` block (endpoint + key configured server-side by
the org admin; a `cardinal-connect --omnigent` variant can mint it).

### `cardinal_omnigent.spend_limits` (enforcing, `request` phase)

- Reads the Cardinal verdict via core `limits` primitives (0.2.0
  `gate_decision`) against a server-writable state dir, refreshed
  post-emit like the CLI adapters — plus omnigent's own
  `context.usage.total_cost_usd` for session-scope caps.
- Renders `GateDecision` → PolicyResponse: block → `{"result": "DENY",
  "reason": <server copy>}`; warn/notify → ALLOW with the standing
  message in `set_labels`/`reason` channels + `ack_band` hysteresis.
- **Fail-closed for free**: `PHASE_REQUEST` is in omnigent's
  FAIL_CLOSED_PHASES — if our policy errors, the request does not
  proceed. This is the enforcement upgrade the CLI plugins cannot have;
  the sales-deck sentence is "Cardinal budgets are enforced, not
  suggested."

## The initiative-attribution gap (design decision needed)

Policies never see the workspace: no cwd, no git. Options:

1. **Labels convention (recommended)**: omnigent agent specs / operator
   config stamp `cardinal.repo` + `cardinal.branch` (or
   `cardinal.initiative`) into session labels; `context.labels` flows to
   every policy event; core `resolve_initiative` runs on the label value.
   Zero fragility, but requires teams to label agents — document in the
   connect flow.
2. Tool-call sniffing: parse shell `tool_call` payloads for git commands
   (their `builtins/working_dir.py` pattern). Fragile; rejected as
   primary, possible fallback enrichment later.
3. Upstream contribution: PR omnigent to carry workspace repo/branch in
   `EvaluationContext` — the right long-term fix and the
   strategic-beachhead move; pursue in parallel, don't block on it.

Sessions without labels attribute to `initiative=None, type=research` —
same as protected-branch sessions today, so rollups stay honest.

## Packaging & layout

```
adapters/omnigent/
├── pyproject.toml            # cardinal-omnigent-policy; deps: cardinal-agent-core>=0.2
├── cardinal_omnigent/
│   ├── __init__.py           # POLICY_REGISTRY = [telemetry_policy, spend_limits_policy]
│   ├── telemetry.py
│   ├── spend_limits.py
│   └── connect.py            # writes policy_modules + config block into server config.yaml
└── tests/                    # PolicyEvent fixtures from schema.py docstrings; StubIngest goldens
```

Not vendored (`build/vendor.py` skips it — no `hooks/` dir, already true).
Version pinning: `OMNIGENT_VERIFIED_COMMIT = "6e71197"` constant + a CI
job that re-reads `schema.py`/`types.py` upstream and diffs the contract
surface, so churn is detected instead of discovered in prod.

## Open questions

1. Subagent mapping: which omnigent event marks a child-agent boundary
   per engine (native vs claude_sdk executor)? Resolve during
   implementation from `runner/app.py`.
2. `session_state` scoping ambiguity (turn vs session) — pick
   verdict-file state (core paths) as source of truth; use
   `state_updates` only for advisory counters.
3. Upstream label-stamping PR (option 3 above) — separate decision with
   the user before any outward contribution.

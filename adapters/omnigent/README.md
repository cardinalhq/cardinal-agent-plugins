# cardinal-omnigent-policy

Cardinal policy modules for omnigent's policy engine. One
pip-installable package (`cardinal-omnigent-policy`) covers every harness
omnigent runs (Claude, Codex, Cursor, Hermes, Pi, OpenCode, custom YAML
agents) — unlike the four CLI adapters in this repo, `cardinal-agent-core`
is a real dependency here, not vendored, and the policies run inside the
omnigent **server** process.

Verified against omnigent commit `2b3b54a4`
(`cardinal_omnigent.OMNIGENT_VERIFIED_COMMIT`); CI re-diffs the upstream
contract surface against that pin (`build/omnigent_drift.py`, the
"Omnigent contract drift" workflow). omnigent is alpha: every event-field
read goes through defensive accessors (`_events.py`), and the contract
must be re-verified on omnigent upgrades.

## What it ships

| Module | Kind | Phases | Emits / enforces |
| --- | --- | --- | --- |
| `cardinal_omnigent.telemetry` | observe-only (never blocks; ALLOWs only to carry `state_updates`) | request, llm_response, tool_call, tool_result, response | `cardinal.git_state` (labels convention + branch sniffing), `api_request` + `cardinal.turn_usage` (per-turn on the claude_sdk executor, `usage_granularity="turn"`), `cardinal.turn_tool` (core bashclass on shell tools), `tool_result`, `cardinal.subagent_usage` (sub-agent child conversations — see below) |
| `cardinal_omnigent.spend_limits` | enforcing | request | Cardinal server verdicts → `DENY` (block) / `ALLOW` + standing message with `ack_band` hysteresis (warn/notify); plus an omnigent-native per-session cap (`session_cost_limit_usd`) checked against `context.usage.total_cost_usd` |

Because `request` is in omnigent's FAIL_CLOSED_PHASES, spend limits here
are **enforced, not suggested** — if policy evaluation fails, the request
does not proceed. Our own internal errors deliberately abstain
(return `None`), never self-DENY: only a genuine server verdict or the
configured session cap denies (see the `spend_limits` module docstring).

## Session identity (minted)

omnigent's policy contract carries **no session or conversation id** —
the event a callable receives is `{type, target, data, context{actor,
usage, user_daily_cost, model, harness, labels, subtree_usage},
session_state, llm_client, request_data}`. Cardinal telemetry is
session-keyed, so the policies mint a `cardinal.session_id` into the
engine's per-conversation `session_state` via `state_updates` (durable
server-side; an in-process memo keyed by the engine-shared `llm_client`
keeps both policies on one id until it persists). See `_identity.py`.

## Subagent mapping

`cardinal.subagent_usage` is emitted on the `response` phase of
conversations whose **labels** mark them as sub-agent children:

- claude-native / codex-native UI children carry `omnigent.wrapper`
  (`*-native-ui-subagent`) plus id labels
  (`omnigent.claude_native.subagent_id`,
  `omnigent.codex_native.subagent_thread_id` / `parent_thread_id` /
  `agent_nickname` / `agent_role`) — stamped by the omnigent server.
- native workflow / custom-YAML sub-agents get no upstream label:
  stamp `cardinal.subagent: <name>` in the sub-agent spec's guardrails
  labels (the Cardinal convention).

Emitted usage is the child conversation's cumulative `context.usage`
(`usage_scope="session_cumulative"` — downstream takes last-per-session,
not a sum) with `model` from the engine-injected `context.model`.

Cost attribution: `cost_usd` on usage events is the delta of the
server-maintained cumulative `context.usage.total_cost_usd` (anchored via
omnigent `state_updates` with an in-process mirror), falling back to
core pricing tables (`anthropic` / `openai` / `gemini`, core 0.3.0) keyed
on the model — platform-prefixed SKUs like `databricks-claude-opus-4-8`
re-resolve from the embedded `claude-` name. Unpriced models emit no
`cost_usd` rather than a misleading $0.

## Registering with an omnigent server

The easy way — device-flow connect (mints the ingest credential, writes
the state dir, merges a `cardinalManaged`-tagged block into your server
config). Install into the interpreter that runs your omnigent server:

```sh
# venv
pip install cardinal-omnigent-policy

# pipx-installed omnigent (use --include-apps so the CLI lands on PATH)
pipx inject omnigent cardinal-omnigent-policy --include-apps

# then, from anywhere:
cardinal-omnigent-connect --config /etc/omnigent/config.yaml
```

`cardinal-agent-core` is resolved as a dependency. `python3 -m
cardinal_omnigent.connect` still works as an alternative to the
`cardinal-omnigent-connect` console script.

Or by hand: `policy_modules:` names the module; omnigent scans it for
`POLICY_REGISTRY` (both policies take `config` by arity). The
`cardinal:` block is what the policies receive as config:

```yaml
policy_modules:
  - cardinal_omnigent

cardinal:
  ingest_endpoint: "https://intake.us-east-2.aws.cardinalhq.io"
  ingest_api_key: "..."                  # minted by connect / org admin
  ingest_api_header: "x-cardinalhq-api-key"
  deployment_environment: "prod"
  org: "your-org-slug"
  state_dir: "~/.omnigent"               # server-writable; verdict/ack files
  session_cost_limit_usd: 25.0           # optional per-session hard cap
  service_name: "omnigent"               # optional resource override
```

Factory registration (closure state) is also supported via omnigent's
`function: {path, arguments}` form with
`cardinal_omnigent.make_telemetry_policy` /
`cardinal_omnigent.make_spend_limits_policy`.

## The labels convention (initiative attribution)

Policies never see the workspace — omnigent's policy contract carries no
cwd, repo, or branch. Initiative attribution therefore rides **session
labels**: have your agent specs / operator config stamp

```yaml
labels:
  cardinal.repo: "your-org/your-repo"      # 'org/name'
  cardinal.branch: "feat/outcomes-observability"
```

`cardinal.branch` runs through core `resolve_initiative` (one branch =
one initiative, `<type-prefix>/<kebab-name>`). Sessions without labels
attribute to `initiative=None, type=research` — same as protected-branch
sessions on the CLI adapters, so rollups stay honest. Consequently
`cardinal.git_state` here carries repo/branch/initiative facts but not
`head_sha`/`cwd`/`remote_url` (structurally unavailable server-side).

**Branch sniffing (enrichment):** creation-time labels can't see a
mid-session `git checkout -b`. Shell `tool_call` events are watched for
branch creation/switch commands (`checkout -b/-B`, `switch`/`switch -c`);
a detected branch emits a fresh `cardinal.git_state` at the boundary
(marked `cardinal_branch_source="tool_sniff"`) and is used at request
time when labels carry no branch. Labels always win when present.

Identity is per-event: `user.email` comes from
`event.context.actor.run_as`; `cardinal.omnigent_harness` (resource
attribute) from `event.context.harness` so downstream can slice by
underlying harness.

## Tests

```sh
cd adapters/omnigent
PYTHONPATH=../../core:. python3 -m unittest discover tests
```

(`tests/fixtures.py` also bootstraps `sys.path` for core and the adapter,
so a bare `python3 -m unittest discover tests` from this directory works
too.) The suite drives both policies against the core StubIngest harness
and asserts attribute coverage against the repo-root contract test's
REQUIRED_KEYS.

This adapter has no `hooks/` directory on purpose: `build/vendor.py`
discovers vendoring targets by the presence of `hooks/`, and this package
must never be vendored — core is a pip dependency.

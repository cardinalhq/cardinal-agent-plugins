# cardinal-omnigent-policy

Cardinal policy modules for omnigent's policy engine. One
pip-installable package (`cardinal-omnigent-policy`) covers every harness
omnigent runs (Claude, Codex, Cursor, Hermes, Pi, OpenCode, custom YAML
agents) — unlike the four CLI adapters in this repo, `cardinal-agent-core`
is a real dependency here, not vendored, and the policies run inside the
omnigent **server** process.

Verified against omnigent commit `6e71197`
(`cardinal_omnigent.OMNIGENT_VERIFIED_COMMIT`). omnigent is alpha: every
event-field read goes through defensive accessors (`_events.py`), and the
contract must be re-verified on omnigent upgrades.

## What it ships

| Module | Kind | Phases | Emits / enforces |
| --- | --- | --- | --- |
| `cardinal_omnigent.telemetry` | observe-only (always abstains) | request, llm_response, tool_call, tool_result | `cardinal.git_state`, `api_request` + `cardinal.turn_usage` (per-TURN granularity, `usage_granularity="turn"`), `cardinal.turn_tool` (core bashclass on shell tools), `tool_result` |
| `cardinal_omnigent.spend_limits` | enforcing | request | Cardinal server verdicts → `DENY` (block) / `ALLOW` + standing message with `ack_band` hysteresis (warn/notify); plus an omnigent-native per-session cap (`session_cost_limit_usd`) checked against `context.usage.total_cost_usd` |

Because `request` is in omnigent's FAIL_CLOSED_PHASES, spend limits here
are **enforced, not suggested** — if policy evaluation fails, the request
does not proceed. Our own internal errors deliberately abstain
(return `None`), never self-DENY: only a genuine server verdict or the
configured session cap denies (see the `spend_limits` module docstring).

`cardinal.subagent_usage` is not emitted yet — the child-agent boundary
event is engine-dependent (spec open question 1).

Cost attribution: `cost_usd` on usage events is the delta of the
server-maintained cumulative `context.usage.total_cost_usd` (anchored via
omnigent `state_updates` with an in-process mirror), falling back to
core pricing tables keyed on `data.usage.model` for models the server
does not price. Anthropic SKUs without a session total emit no `cost_usd`
rather than a misleading $0.

## Registering with an omnigent server

The easy way — device-flow connect (mints the ingest credential, writes
the state dir, merges a `cardinalManaged`-tagged block into your server
config):

```sh
pip install cardinal-omnigent-policy
python3 -m cardinal_omnigent.connect --config /etc/omnigent/config.yaml
```

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

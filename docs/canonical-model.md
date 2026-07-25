# Canonical execution graph model (schema v1)

This document specifies the causal execution graph Cardinal builds from
every instrumented adapter (Claude, Codex, Cursor, Gemini, Omnigent). It is
the human-readable counterpart to `core/cardinal_core/envelope.py`, which
implements this contract in code. Every enum value and field name below
appears verbatim in that module — if the two ever disagree, the code and
this doc must be reconciled together; neither is allowed to drift alone.

See `docs/local-notes/plans/agent-execution-graph.md` for the full
architecture and phased rollout this document is Phase 0.B of.

## 1. Purpose

The differentiated asset is not "we can emit OTLP traces from every coding
agent." It is a single causal model connecting user intent → turns →
models → skills → subagents → tools → artifacts → engineering spend → PRs
→ initiatives → outcomes — across every adapter, in one shape. The graph
is the product model. OTLP traces are a *projection* of that graph,
materialized server-side for interoperability with Tempo/Honeycomb/Datadog;
they are never the source of truth and adapters never emit OTLP directly.

## 2. Node model

Every observed execution atom is a node. `node_kind` is closed and small;
new tool categories extend `tool_kind`, never `node_kind`.

### `NodeKind`

| Value        | Used when                                                        |
|--------------|-------------------------------------------------------------------|
| `turn`       | One user-turn or one model-call boundary in a session             |
| `llm_call`   | A single model invocation (carries `request_model`)                |
| `invocation` | A tool, skill, subagent, or hook call (carries `invocation_kind`) |
| `artifact`   | A file, PR, diff, or other produced output                        |
| `event`      | Reserved for event-shaped nodes; instantaneous facts normally live in `execution_events`, not as nodes |

### `InvocationKind` (only on `node_kind = invocation`)

| Value      | Used when                                             |
|------------|--------------------------------------------------------|
| `tool`     | A builtin/MCP/shell/filesystem tool call (carries `tool_kind`) |
| `skill`    | A skill/slash-command invocation                       |
| `subagent` | A delegated subagent invocation                        |
| `hook`     | A lifecycle hook firing (PreToolUse, Stop, etc.)        |

### `ToolKind` (only on `invocation_kind = tool`)

| Value        | Used when                                    |
|--------------|-----------------------------------------------|
| `builtin`    | Adapter-native tool (Edit, Bash, Read, ...)   |
| `mcp`        | MCP-server-provided tool                      |
| `shell`      | Direct shell/subprocess execution             |
| `filesystem` | Direct filesystem operation                   |

`ToolKind` is intentionally extensible — new categories are added as
adapters surface them, without touching `node_kind` or `invocation_kind`.

### `ToolkitType` (fact-table product classification)

Used only on `toolkit_invocation_facts`, not on graph nodes. It re-buckets
`(invocation_kind, tool_kind)` into the categories the product surfaces
(adoption dashboards, cost rollups) actually report on.

| Value          | Corresponds to                                  |
|----------------|--------------------------------------------------|
| `skill`        | `invocation_kind = skill`                        |
| `mcp_tool`     | `invocation_kind = tool`, `tool_kind = mcp`       |
| `subagent`     | `invocation_kind = subagent`                      |
| `builtin_tool` | `invocation_kind = tool`, `tool_kind != mcp`      |

### Composition

The three kinds nest strictly:

```
node_kind = invocation
  └── invocation_kind = tool
        └── tool_kind = builtin | mcp | shell | filesystem
```

Only `invocation` nodes carry `invocation_kind`. Only `tool` invocations
carry `tool_kind`. A `skill` or `subagent` invocation never carries
`tool_kind`. `llm_call` and `turn` nodes never carry `invocation_kind` or
`tool_kind` at all — see `envelope.validate()`, which rejects any node
where `invocation_kind` is set but `node_kind != invocation`, or where
`tool_kind` is set but `invocation_kind != tool`.

## 3. Edge model

Edges are typed relationships between nodes, not merely parent/child.

### `EdgeKind`

| Value                | Example                                                        |
|-----------------------|-----------------------------------------------------------------|
| `parent_of`          | turn `parent_of` llm_call                                       |
| `invoked`            | llm_call `invoked` tool-invocation                              |
| `delegated_to`       | subagent-invocation `delegated_to` child turn                   |
| `continued_as`       | session A's final turn `continued_as` session B's first turn (restart) |
| `used_toolkit`       | skill X `used_toolkit` mcp_tool Y                                |
| `produced_artifact`  | tool-invocation `produced_artifact` file artifact                |
| `contributed_to`     | artifact `contributed_to` PR artifact                            |
| `linked_to_outcome`  | PR artifact `linked_to_outcome` outcome record                   |

Not every edge is `parent_of` — that is the point of a typed edge model.
Cross-file, cross-session, or workflow-contribution relationships use the
other kinds and are rendered in the OTLP projection (Phase 2) as
span-links rather than parent spans.

## 4. Event model

Events are instantaneous facts, not spans — they never carry `start_ns`/
`end_ns`, only a single `event_ns`.

### `EventKind`

| Value                | Example                                                          |
|-----------------------|--------------------------------------------------------------------|
| `model_switch`        | Session moves from Sonnet to Opus mid-conversation                 |
| `context_compaction`  | Adapter compacts/summarizes context                                |
| `approval_request`    | User is prompted to approve a tool call                            |
| `permission_denial`   | A tool call is denied by permission policy                         |
| `retry`               | A tool or model call is retried after failure                      |
| `hook_result`         | A lifecycle hook returns block/allow/modify                        |
| `context_reset`       | Context window is reset/cleared                                    |
| `file_mutation`       | A file is created/edited/deleted, independent of the tool node     |
| `skill_resolution`    | A skill transitions lifecycle state (see §5)                       |
| `execution_failure`   | A node fails terminally (uncaught error, timeout)                  |
| `record_conflict`     | Reducer sees two same-provenance observations disagree (see §10)   |

## 5. Skill lifecycle

A `skill` invocation node carries `lifecycle_state ∈ SkillLifecycleState`:

| State       | Meaning                                                              |
|-------------|------------------------------------------------------------------------|
| `requested` | A prompt regex matched a `/command`; no evidence the adapter loaded it |
| `resolved`  | The adapter loaded the skill (instructions injected into context)      |
| `executed`  | The skill actually ran / controlled subsequent execution               |

**Adoption metrics count `executed` only, by default.** `requested` and
`resolved` are surfaced as separate meters when a product surface
specifically needs funnel visibility (e.g. "skills requested but never
resolved" as a friction signal) — they must never be silently folded into
an adoption count.

## 6. Model semantics

Two distinct fields, never conflated:

- **`request_model`** (a.k.a. `gen_ai.request.model`) — set only on
  `llm_call` nodes. The exact model that call went to.
- **`orchestrator_model`** — set on `tool`, `skill`, and `subagent`
  invocation nodes. The model whose turn invoked the node, not the model
  that ran inside it.

**UX phrasing rule**: *"invoked by Opus"*, never *"tool ran on Opus"* — a
tool invocation has no model of its own; only the orchestrating turn does.

**MCP-internal model gap**: an MCP server may internally call a model
Cardinal cannot observe (e.g. a summarization step inside the MCP tool
implementation). That model is unknown to us and must never be silently
attributed to `orchestrator_model` or `request_model` — it is simply
absent from the graph.

## 7. Identity rules

```
execution_key = HMAC(cardinal_execution_key, org_id || adapter || session_id)
execution_id  = internal PK (ULID), assigned by ingest on first observation
node_key      = HMAC(execution_key, node_kind || native_seed)
                -- native_seed = tool_use.id | call_id | (user_turn_seq, turn_seq, tool_seq)
session_id    = adapter-native, verbatim
trace_id      = HMAC(cardinal_trace_key, org_id || adapter || session_id)[:16]
                -- OTLP projection only, never used to look up product identity
```

Adapters never mint or persist `execution_id`. Every envelope carries
`(org_id, adapter, session_id)`; ingest computes `execution_key`, looks up
or creates the row, then attaches the internal `execution_id`. `trace_id`
exists solely for the OTLP projection (Phase 2) and must never be used as
a join key for product queries — `execution_key`/`node_key` are the only
stable identity for that.

## 8. Execution context and inheritance

Phase 0.1 made `org_id`, `model` (request vs. orchestrator), and
`outcome_id` linkage first-class. It did not make **repo, PR, initiative,
or engineer/actor** first-class — those dimensions would otherwise end up
scattered across ad-hoc `attributes` JSONB, rediscovered independently by
every adapter and every rollup query. Phase 0.2.a fixes this with an
**inheritance contract**: a two-class split between execution-wide context
and node-specific context.

### The two-class split

- **Execution-wide inheritable context** — set once per execution (on an
  `ExecutionContext` observation, `RecordType.CONTEXT_OBSERVED`) and
  inherited by every node in that execution: org/repo/branch/commit/PR/
  initiative/outcome/engineer/adapter/session. This is *who, where, and
  what for* — it does not change from one tool call to the next within a
  single execution.
- **Node-specific context** — stays on individual nodes: `request_model`,
  `orchestrator_model`, `toolkit_type`/`toolkit_name`, and the six-axis
  provenance (§9). This is *what happened at this step*.

Transport-level identity (`org_id`, `adapter`, `session_id`) already lives
on the `Envelope` wrapper and is never duplicated onto `ExecutionContext`.

### `ExecutionContext` field reference

| Field | Meaning | Example | Typical source(s) |
|---|---|---|---|
| `actor_id` | Engineer identity (email or ID); distinct from the OTel resource attr `user_email` | `rjha@cardinalhq.io` | `session_start`, `user_supplied` |
| `workspace_id` | Cardinal workspace grouping | `ws_9f2a` | `session_start` |
| `team_id` | Cardinal team grouping | `team_platform` | `session_start`, `user_supplied` |
| `repository_id` | Cardinal-canonical repo ID, if known | `repo_3a1c` | `git_state` (once resolved) |
| `repository_name` | `owner/repo` form | `cardinalhq/lakerunner` | `git_state` |
| `repository_url` | Canonical URL, userinfo-scrubbed | `https://github.com/cardinalhq/lakerunner.git` | `git_state` |
| `branch` | Current branch at observation time | `feat/agent-execution-graph-p0` | `git_state` |
| `commit_sha` | Current commit at observation time | `abc1234` | `git_state` |
| `pr_id` | Cardinal-canonical PR ID, if known | `pr_771` | `pr_created` |
| `pr_number` | GitHub-style PR number | `42` | `pr_created` |
| `initiative_id` | Matched initiative | `init_2026h2_toolkit` | `initiative_matched` |
| `outcome_id` | Linked outcome record | `outcome_9911` | `outcome_resolved` |
| `environment` | Deployment/runtime environment | `dev`, `prod-shadow` | `session_start`, `user_supplied` |
| `agent_runtime_version` | Adapter/agent runtime version | Claude Code `1.5.2` | `session_start` |
| `plugin_version` | Cardinal plugin version | `0.9.0` | `session_start` |
| `attributes` | Extensibility escape hatch for org-specific context fields | `{"cost_center": "eng-42"}` | any |

### Inheritance rule

Nodes inherit context **by `execution_id` at query time** — the query
layer joins a node to the resolved `ExecutionContext` row for its
execution, it does not copy context fields onto every node. The
fact-projection (`toolkit_invocation_facts`) additionally **snapshots**
context at projection time, so a fact row reflects the context as it was
known when that row was written, not whatever it later evolves into (see
§Fact model in the architecture plan).

Nodes MAY override specific fields for themselves — e.g. a subagent
working against a different repository sets `repository_name` in its own
node `attributes`. The precedence rule for overrides:

> **`node.attributes` wins over inherited context for that node's own
> attributes only; child nodes inherit the execution-wide context, not
> the parent node's override.**

In other words, an override is scoped to the single node that set it and
does not propagate down the tree — every child still inherits from
`ExecutionContext` unless it sets its own override.

### Late-bound enrichment example

Context fields do not all arrive at once. A single execution typically
accumulates them over its lifetime, each field independently:

```
t0  session_start   → actor_id, workspace_id, environment, plugin_version
t1  git_state       → repository_name, repository_url, branch, commit_sha
t2  pr_created       → pr_id, pr_number         (mid-session: PR opened from a branch push)
t3  outcome_resolved → outcome_id               (after session ends: outcome marked in review)
```

Each of these is a separate `CONTEXT_OBSERVED` envelope for the same
`execution_key`. The reducer (0.2.b) merges them per field under
`CONTEXT_SOURCE_PRECEDENCE` — `commit_sha` set at `t1` and `pr_number` set
at `t2` both coexist on the resolved context; neither observation
overwrites fields the other didn't touch.

### `ContextSource` values and precedence

Single provenance axis for `ExecutionContext` observations — distinct from
the six-axis node provenance in §9, since a context observation describes
execution-wide facts, not a single node.

| `ContextSource` | Precedence | Meaning |
|---|---|---|
| `pr_created` | 60 | PR is late-bound but authoritative once it arrives |
| `outcome_resolved` | 60 | Outcome linkage is late-bound but authoritative once resolved |
| `initiative_matched` | 50 | Initiative-matching heuristic/service result |
| `user_supplied` | 40 | Manual override from user/org config |
| `git_state` | 30 | Git-state hook at session start or turn boundary |
| `session_start` | 20 | `SessionStart` hook basic info |
| `unknown` | 10 | No better source available |

Reducer semantics (implemented in Phase 0.2.b, documented here so the
contract and the reducer never drift apart):

- **Per field**, the observation with higher precedence wins.
- **Equal precedence, different value** → keep the first value, emit
  `execution_events(event_kind='record_conflict')` with both values (same
  pattern as node-field conflicts in §9).
- **Fields evolve independently** — `pr_number` may arrive later from
  `pr_created` while `commit_sha` was already set at `git_state`; both
  values persist on the resolved context simultaneously. Precedence is
  evaluated per field, never across the whole record.

## 9. Provenance axes

Every node carries all six axes. Downstream UX reads them directly to
qualify what it renders (e.g. "timing estimated" badges) rather than
presenting inferred data as fact.

| Axis              | Values                                                     | Meaning per value                                                                 |
|-------------------|--------------------------------------------------------------|-------------------------------------------------------------------------------------|
| `identity_source` | `native`, `derived`, `synthetic`                              | native: adapter-supplied stable id. derived: computed from other native fields. synthetic: Cardinal invented an id with no native anchor. |
| `parent_source`   | `native`, `transcript`, `temporal`, `inferred`, `unknown`     | native: adapter states the parent explicitly. transcript: parsed from transcript structure. temporal: nearest-preceding-node heuristic. inferred: prompt/heuristic guess. unknown: no parent could be determined. |
| `timing_source`   | `native`, `reconstructed`, `estimated`, `marker`, `unknown`   | native: adapter-supplied timestamps. reconstructed: derived from surrounding native timestamps. estimated: modeled/approximated duration. marker: a single observation timestamp stood in for a span. unknown: no timing available. |
| `model_source`    | `explicit`, `inherited`, `session_default`, `unknown`         | explicit: model named on this call. inherited: taken from the invoking turn. session_default: fell back to the session's configured default. unknown: no model could be determined. |
| `toolkit_source`  | `native`, `command_parse`, `prompt_inference`, `unknown`      | native: adapter reports the toolkit call directly. command_parse: parsed from a `/command` invocation. prompt_inference: inferred from prompt text. unknown: no toolkit attribution possible. |
| `usage_source`    | `native`, `allocated`, `estimated`, `unknown`                 | native: adapter-reported token/cost usage. allocated: usage split across sibling nodes by a rule. estimated: modeled from heuristics. unknown: no usage data available. |

## 10. Provenance precedence

Global precedence order, applied per field during reduction:

```
native > reconstructed > derived > estimated > inferred > unknown
```

Reducer behavior on each new observation, compared to the current
canonical value for that field:

| Current provenance | New provenance    | Action                                                                 |
|---------------------|--------------------|--------------------------------------------------------------------------|
| lower                | higher             | Replace canonical value; prior value retained in raw `execution_observations` as superseded |
| equal                 | equal, same value  | No-op                                                                     |
| equal                 | equal, different value | Keep canonical value; emit `execution_events(event_kind=record_conflict)` with both values |
| higher                | lower              | Keep canonical value; new observation retained in raw table only          |

`execution_nodes`/`execution_edges`/`execution_events` are always
regenerable from `execution_observations` — replay is `TRUNCATE
execution_nodes; SELECT reduce(observations)`.

## 11. Envelope record types

Adapters emit `Envelope` records. `record_type` and the payload's
dataclass are 1:1 — `envelope.validate()` rejects any mismatch.

| `RecordType`              | Emitted when                                                        | Payload shape                                                    |
|-----------------------------|------------------------------------------------------------------------|----------------------------------------------------------------------|
| `node_observed`            | First observation of a node                                            | `NodeObserved` — full node shape + all six provenance axes           |
| `node_updated`             | A later observation revises fields on an already-observed node (e.g. `end_ns` filled in on completion) | `NodeUpdated` — same shape as `NodeObserved` |
| `edge_observed`            | A typed relationship between two nodes is observed                     | `EdgeObserved` — source/target node keys + `edge_kind`                |
| `execution_event`          | An instantaneous fact occurs                                           | `ExecutionEvent` — `event_kind` + `event_ns` + optional related node  |
| `usage_observed`           | Token/cost usage is attributed to a node                               | `UsageObserved` — input/output/cached tokens + cost + `usage_source`  |
| `artifact_link_observed`   | A node is linked to an artifact reference (file path, PR URL, etc.)    | `ArtifactLinkObserved` — `artifact_kind` + `artifact_ref`             |
| `context_observed`         | Execution-wide context is observed or enriched (first observation or a later enrichment — same record type; see §8) | `ExecutionContext` — inheritable fields + `context_source` |

## 12. OpenTelemetry semantic conventions mapping

This section is **normative** — Phase 2's OTLP projection (see the
architecture plan's "OTLP as projection") reads from this table when
materializing `execution_nodes`/`ExecutionContext` into `ResourceSpans`.
It is the single place adapter/context/node fields are assigned an OTel
attribute name and scope.

**Rule: use OTel SemConv where a stable one exists; use the `cardinal.*`
namespace only for concepts OTel does not define cleanly.**

| Cardinal field | OTel projection | Attribute scope | Notes |
|---|---|---|---|
| `org_id` | `cardinal.org.id` | Resource | Cardinal namespace — no OTel equivalent |
| `adapter` | `cardinal.adapter` | Resource | |
| `execution_id` | `cardinal.execution.id` | Resource | |
| `session_id` | `cardinal.session.id` | Resource | |
| `plugin_version` | `service.version` | Resource | OTel standard |
| `agent_runtime_version` | `cardinal.agent.runtime.version` | Resource | |
| `actor_id` | `enduser.id` | Resource | OTel standard (informational) |
| `workspace_id` | `cardinal.workspace.id` | Resource | |
| `team_id` | `cardinal.team.id` | Resource | |
| `environment` | `deployment.environment.name` | Resource | OTel standard (renamed from `deployment.environment` in newer conventions) |
| `repository_name` | `vcs.repository.name` | Span | OTel VCS SemConv |
| `repository_url` | `vcs.repository.url.full` | Span | OTel VCS SemConv |
| `branch` | `vcs.ref.head.name` | Span | OTel VCS SemConv |
| `commit_sha` | `vcs.ref.head.revision` | Span | OTel VCS SemConv |
| `pr_number` | `cardinal.pr.number` | Span | No stable OTel PR convention yet |
| `pr_id` | `cardinal.pr.id` | Span | |
| `initiative_id` | `cardinal.initiative.id` | Span | |
| `outcome_id` | `cardinal.outcome.id` | Span | |
| `request_model` | `gen_ai.request.model` | Span | OTel GenAI SemConv |
| `orchestrator_model` | `cardinal.orchestrator.model` | Span | Cardinal-specific — no OTel equivalent for "orchestrating LLM" |
| `toolkit_type` | `cardinal.toolkit.type` | Span | |
| `toolkit_name` | `cardinal.toolkit.name` | Span | |
| `invocation_kind` | `cardinal.invocation.kind` | Span | |
| `tool_kind` | `cardinal.tool.kind` | Span | |

Scope convention: fields that are constant for the whole execution
(everything sourced from `ExecutionContext`, plus adapter/session
identity) project onto the OTLP **Resource**; fields specific to a single
node project onto that node's **Span**. This mirrors the execution-wide
vs. node-specific split in §8 — resource attributes are exactly the
inheritable fields, span attributes are exactly the node-specific ones.

## 13. Ingestion contract

Envelopes carry observations, never writes. Ingest appends every envelope
to `execution_observations` (append-only, raw) and a reducer derives
canonical state into `execution_nodes`/`execution_edges`/
`execution_events` under the precedence rule in §10. The canonical tables
are a materialized view over the raw log in every meaningful sense: they
are always fully regenerable by replaying observations from scratch, so a
reducer bug or ontology fix never loses information — it is fixed by
re-running the reducer, not by backfilling adapters.

`context_observed` records follow the exact same append-only + reducer
flow: each is appended to `execution_observations` verbatim, and the
reducer (0.2.b) derives a single resolved `execution_context` row per
execution under `CONTEXT_SOURCE_PRECEDENCE` (§8), field by field — not a
separate ingestion path.

## 14. Compatibility projection

The existing `agent_sessions_toolkit_maps.{skills_used, agents_used,
mcp_servers_used, tool_counts}` columns become a **derived read model**:
a projector reads `toolkit_invocation_facts` and writes those columns for
any session whose graph pipeline is authoritative
(`agent_sessions.execution_graph_authoritative = true`). Until every
session is on the graph pipeline, the two write paths are mutually
exclusive per-session — the old hook-based writer is disabled exactly
where the projector takes over — so existing dashboards keep reading the
same columns unmodified throughout the migration.

## 15. Schema version and evolution rules

`SCHEMA_VERSION = 1` is locked by this document and by
`envelope.SCHEMA_VERSION` in code. Evolution rules:

- **Additive only, within v1**: new enum members, new optional payload
  fields, and new `RecordType`/payload pairs may be added without a
  version bump, as long as every existing field's meaning and every
  existing enum value's meaning is unchanged.
- **Breaking changes bump to v2**: renaming or removing a field, changing
  a field's type, changing the meaning of an existing enum value, or
  changing precedence semantics all require a new `schema_version` and a
  parallel-run migration plan — never an in-place redefinition of v1.
- `envelope.validate()` enforces `schema_version == 1` today; a v2 module
  would introduce its own validator rather than overload this one.

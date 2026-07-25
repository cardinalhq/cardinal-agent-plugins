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
| `record_conflict`     | Reducer sees two same-provenance observations disagree (see §9)    |

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

## 8. Provenance axes

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

## 9. Provenance precedence

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

## 10. Envelope record types

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

## 11. Ingestion contract

Envelopes carry observations, never writes. Ingest appends every envelope
to `execution_observations` (append-only, raw) and a reducer derives
canonical state into `execution_nodes`/`execution_edges`/
`execution_events` under the precedence rule in §9. The canonical tables
are a materialized view over the raw log in every meaningful sense: they
are always fully regenerable by replaying observations from scratch, so a
reducer bug or ontology fix never loses information — it is fixed by
re-running the reducer, not by backfilling adapters.

## 12. Compatibility projection

The existing `agent_sessions_toolkit_maps.{skills_used, agents_used,
mcp_servers_used, tool_counts}` columns become a **derived read model**:
a projector reads `toolkit_invocation_facts` and writes those columns for
any session whose graph pipeline is authoritative
(`agent_sessions.execution_graph_authoritative = true`). Until every
session is on the graph pipeline, the two write paths are mutually
exclusive per-session — the old hook-based writer is disabled exactly
where the projector takes over — so existing dashboards keep reading the
same columns unmodified throughout the migration.

## 13. Schema version and evolution rules

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

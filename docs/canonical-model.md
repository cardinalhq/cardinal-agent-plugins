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

## 8. Where execution-wide context lives

For a given execution (identified by `execution_key`, one-to-one with
`session_id` today), the "who / where / what-branch / what-PR" facts are
already in `agent_sessions` in lakerunner (`repo`, `branch`, `head_sha`,
`user_email`, `initiative_name`, `team_id`, `organization_id`,
`agent_runtime`). Join `execution_nodes → agent_sessions ON (org_id,
session_id)` at query time — the same join `agent_sessions_attribution.go`
uses for token cost.

No parallel context table. No `context_observed` envelope. Fields not yet
on `agent_sessions` (`pr_number`, `plugin_version` at time of writing) are
added there as ordinary columns when they matter, not resurrected as a
separate context contract.

This is a change from the plan's Phase 0.2.a sketch, which introduced an
`ExecutionContext` observation type with per-field precedence merging.
That mechanic was justified against a theoretical late-bound-enrichment
scenario; in practice nothing emitted the envelopes end-to-end, so the
complexity earned nothing. See the drop migration
`lrdb/migrations/1784961003_drop_execution_context.up.sql` for the removal
rationale in code.

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

Precedence is **per-axis**, not global — each of the six axes in §9 has
its own ordered vocabulary and its own ranking. There is no cross-axis
merge; a `timing_source` observation and an `identity_source` observation
compare independently. The ordering below is the SHAPE that every axis
follows (higher on the left wins), read against each axis's own §9
vocabulary — not a literal universal ranking:

```
native > reconstructed > derived > estimated > inferred > unknown
```

For example: `identity_source` values are only `{native, derived, synthetic}`,
so its actual ranking is `native > derived > synthetic`, following the same
shape. Concrete per-axis rank tables live in the reducer implementation
(`internal/executiongraph/envelope.go` in lakerunner) and MUST match the
§9 vocabulary.

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

## 12. OpenTelemetry semantic conventions mapping

This section is **normative** — Phase 2's OTLP projection (see the
architecture plan's "OTLP as projection") reads from this table when
materializing `execution_nodes` into `ResourceSpans`. It is the single
place adapter/context/node fields are assigned an OTel attribute name and
scope.

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
(everything sourced from `agent_sessions` at join time per §8, plus
adapter/session identity) project onto the OTLP **Resource**; fields
specific to a single node project onto that node's **Span**. This mirrors
the execution-wide vs. node-specific split in §8 — resource attributes
are the join-time execution facts, span attributes are the node-specific
ones.

This table is the field-name/scope authority; §14 is the concrete
application of it when materializing envelopes into OTLP spans for Phase
1 — every attribute name below appears verbatim in §14's per-record-type
shape, and §14 additionally documents where a `cardinal.envelope.*`
namespaced attribute (record-level bookkeeping this table doesn't cover:
`record_type`, `record_id`, `schema_version`, provenance axes, etc.)
coexists alongside the SemConv-mapped name for the same field. If the two
sections ever disagree on a mapped field's OTel name, this table wins —
§14 must be corrected to match, not the reverse.

## 13. Ingestion contract

Envelopes carry observations, never writes. Ingest appends every envelope
to `execution_observations` (append-only, raw) and a reducer derives
canonical state into `execution_nodes`/`execution_edges`/
`execution_events` under the precedence rule in §10. The canonical tables
are a materialized view over the raw log in every meaningful sense: they
are always fully regenerable by replaying observations from scratch, so a
reducer bug or ontology fix never loses information — it is fixed by
re-running the reducer, not by backfilling adapters.

## 14. OTLP wire format — envelopes as spans

Envelopes describe span-shaped execution atoms: a node has a start/end
(or a marker timestamp standing in for one); an edge, event, usage, or
artifact-link observation is a zero-duration fact anchored to a single
instant. This section is the **normative wire spec** for materializing
schema-v1 envelopes as OTLP `ResourceSpans` and is implemented verbatim
in `core/cardinal_core/traces.py`. It replaces the custom `/v1/envelopes`
HTTP endpoint from the first Phase 1 attempt (commit `a4a26ad` and
related) — that endpoint was an architectural mistake; envelopes ride the
OTLP **traces** signal instead, the same intake endpoint the plugin
already uses for logs (`core/cardinal_core/otlp.py::emit_records` ->
`/v1/logs`), just a different signal (`/v1/traces`).

### Identity derivation (non-secret, deterministic)

- `trace_id` (16 bytes) = `SHA-256(f"{org_id}|{adapter}|{session_id}")[:16]`
- `span_id` (8 bytes) — per record type:
  - node payloads (`node_observed`/`node_updated`): `SHA-256(f"{trace_id.hex()}|node|{node_key}")[:8]`
  - `edge_observed`: `SHA-256(f"{trace_id.hex()}|edge|{record_id}")[:8]`
  - `execution_event`: `SHA-256(f"{trace_id.hex()}|event|{record_id}")[:8]`
  - `usage_observed`: `SHA-256(f"{trace_id.hex()}|usage|{record_id}")[:8]`
  - `artifact_link_observed`: `SHA-256(f"{trace_id.hex()}|artifact|{record_id}")[:8]`
- `parent_span_id` (8 bytes, optional) — only set for node payloads when
  the payload has a natural `parent_of`-derivable predecessor within the
  transcript. Left unset otherwise; lakerunner reconstructs edges from
  `edge_observed` spans regardless.

  **Implementation note (resolved ambiguity):** `traces.py::envelope_to_span`
  is a pure, single-envelope function — it has no visibility into the
  accumulated edge set for an execution, so it can never itself tell
  whether a node has a `parent_of`-derivable predecessor without a
  second, graph-aware correlation pass. Phase 1 therefore never sets
  `parent_span_id`; every span is emitted parentless at the OTLP layer.
  This is intentional, not an oversight: `parent_span_id` is optional per
  the rule above, and lakerunner's reducer already reconstructs the full
  graph from `edge_observed` spans independent of whether the OTLP
  `parentSpanId` field is populated. A future enhancement could add a
  graph-aware batch pass (accepting the full node+edge envelope set and
  filling `parent_span_id` for direct `parent_of` predecessors), but nothing
  downstream depends on it today. **Lakerunner must read this same rule** —
  do not treat a missing `parent_span_id` as a data gap; it is the
  documented Phase 1 behavior.

**Rationale**: HMAC dropped in favor of SHA-256 because `trace_id`/`span_id`
are public on the OTLP wire — they identify but do not need to be
unforgeable (auth happens via the ingest API key on the POST itself, not
via trace/span identity). Lakerunner recomputes the same SHA-256 for
lookup, so no shared secret needs to be distributed to adapters or cross
the wire.

### Resource attributes

Constant for an execution — set once per `ResourceSpans` batch
(`traces.py::trace_resource_attrs`):

| Attribute | Value |
|---|---|
| `service.name` | adapter name (e.g. `"claude-code"`) |
| `service.version` | `plugin_version` |
| `cardinal.org.id` | `org_id` |
| `cardinal.adapter` | `adapter` |
| `cardinal.session.id` | `session_id` |
| `cardinal.plugin_version` | same as `service.version` |
| `cardinal.core_version` | `CORE_VERSION` |

### Common span attributes

Present on every envelope-carrying span:

| Attribute | Value |
|---|---|
| `cardinal.envelope.record_type` | one of the seven `RecordType` values (§11) |
| `cardinal.envelope.record_id` | for reducer idempotency (see below) |
| `cardinal.envelope.schema_version` | `1` |
| `cardinal.envelope.observed_ns` | always present |
| `cardinal.envelope.effective_ns` | always present |
| `cardinal.envelope.execution_key` | every payload carries `execution_key`; emitting here lets the reducer look up the parent execution without switching on `record_type` |

### Per-record-type span shape

Cross-references §11 (payload shapes) and §12 (SemConv mapping).

- **`node_observed` / `node_updated`** — span name = `node_name`;
  start/end from the payload's `start_ns`/`end_ns` (falling back to
  `observed_ns` when unset, then to `start_ns` for `end_ns`).
  Attributes: `cardinal.envelope.node_key` (required — the reducer's
  identity seed for this node; span_id is a truncated hash of it and
  is not reversible); `cardinal.envelope.node_kind`,
  `.invocation_kind` (if set), `.tool_kind` (if set); all six §9 provenance axes as
  `cardinal.envelope.provenance.identity_source`,
  `.provenance.parent_source`, `.provenance.timing_source`,
  `.provenance.model_source`, `.provenance.toolkit_source`,
  `.provenance.usage_source`; the SemConv-mapped fields per §12
  (`gen_ai.request.model` for `request_model`, `cardinal.orchestrator.model`
  for `orchestrator_model`, when set); and the payload's `attributes`
  dict expanded as `cardinal.envelope.attributes.<key>` (nested one level
  as `.attributes.<key>.<subkey>`; deeper nesting or lists are
  JSON-encoded to a string rather than dropped).
- **`edge_observed`** — zero-duration span (`start = end = observed_ns`);
  name = `f"edge:{edge_kind}"`. Attributes:
  `cardinal.envelope.edge.source_node_key`, `.edge.target_node_key`,
  `.edge.kind`, `.edge.attributes.<key>`.
- **`execution_event`** — zero-duration span (`start = end = event_ns`);
  name = `f"event:{event_kind}"`. Attributes: `cardinal.envelope.event.kind`,
  `.event.related_node_key` (if set), `.event.ns`, `.event.attributes.<key>`.
- **`usage_observed`** — zero-duration span (`start = end = effective_ns`);
  name = `"usage"`. Attributes: `cardinal.envelope.usage.node_key`,
  `.usage.input_tokens`, `.usage.output_tokens`, `.usage.cached_tokens`
  (if set), `.usage.cost_usd` (if set), `.usage.source`.
- **`artifact_link_observed`** — zero-duration span (`start = end =
  effective_ns`); name = `f"artifact:{artifact_kind}"`. Attributes:
  `cardinal.envelope.artifact.kind`, `.artifact.ref`, `.artifact.node_key`,
  `.artifact.attributes.<key>`.

### Wire encoding

OTLP/HTTP JSON (`content-type: application/json`) — matches what
`otlp.py::emit_records` already uses for logs. Endpoint =
`<OTEL_EXPORTER_OTLP_ENDPOINT>/v1/traces`.

### Idempotency

The reducer dedupes by `cardinal.envelope.record_id` (from
`execution_observations.record_id`'s unique constraint). Same envelope
emitted twice = one canonical row, regardless of how many times its span
crosses the OTLP wire.

### Design note

Multiple observations of the same node produce multiple OTLP spans
sharing the same `span_id` (each with a distinct `record_id`). This is
legal OTLP and the reducer handles it via the §10 precedence rule. OTel
backends that don't dedupe on `(trace_id, span_id)` will show these as
duplicate spans in a raw trace view — an accepted trade-off of the
observation model (§13): the OTLP projection is for interoperability, not
the source of truth, and the reducer is what makes `execution_nodes`
canonical.

## 15. Compatibility projection

The existing `agent_sessions_toolkit_maps.{skills_used, agents_used,
mcp_servers_used, tool_counts}` columns become a **derived read model**:
a projector reads `toolkit_invocation_facts` and writes those columns for
any session whose graph pipeline is authoritative
(`agent_sessions.execution_graph_authoritative = true`). Until every
session is on the graph pipeline, the two write paths are mutually
exclusive per-session — the old hook-based writer is disabled exactly
where the projector takes over — so existing dashboards keep reading the
same columns unmodified throughout the migration.

## 16. Schema version and evolution rules

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

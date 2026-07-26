# Agent Execution Graph — implementation plan

> **Update 2026-07-26 (post-p1)**: The `ExecutionContext` / `context_observed`
> contract described in §0.2.a was dropped after p1 validation — every field
> duplicated an existing `agent_sessions` column. See `docs/canonical-model.md
> §8` for the current story. References to `ExecutionContext`,
> `CONTEXT_OBSERVED`, `ContextSource`, `CONTEXT_SOURCE_PRECEDENCE`, or the
> `execution_context` table below are historical.

## Purpose

Build a **causal execution graph** of agent work across every adapter Cardinal
instruments (Claude, Codex, Cursor, Gemini, Omnigent), with OTLP traces as one
projection of that graph. The graph is the product model; traces are for
interoperability.

The differentiated asset is not "we can emit traces from every agent." It is
that Cardinal can build one causal model connecting user intent → turns →
models → skills → subagents → tools → artifacts → engineering spend → PRs →
initiatives → outcomes.

This document captures the architecture and phased implementation plan agreed
in the design conversation on 2026-07-25.

## Architecture

### Canonical model

**Nodes** — one row per observed execution atom.

```sql
execution_nodes(
  execution_id, node_id,
  node_kind,                    -- turn | llm_call | invocation | artifact | event
  invocation_kind,              -- tool | skill | subagent | hook          (when node_kind=invocation)
  tool_kind,                    -- builtin | mcp | shell | filesystem | ...  (when invocation_kind=tool)
  node_name,                    -- e.g. "brainstorm", "Edit", "list_services"
  start_ns, end_ns,             -- nullable; filled by later observations
  orchestrator_model,           -- model whose turn invoked this node
  request_model,                -- exact model on llm_call nodes only
  attributes JSONB,
  identity_source, parent_source, timing_source,
  model_source, toolkit_source, usage_source
)
```

**Edges** — typed relationships, not merely parent_of.

```sql
execution_edges(
  execution_id, source_node_id, target_node_id,
  edge_kind,   -- parent_of | invoked | delegated_to | continued_as
               -- | used_toolkit | produced_artifact | contributed_to | linked_to_outcome
  attributes JSONB
)
```

**Events** — instantaneous, not spans.

```sql
execution_events(
  execution_id, related_node_id,
  event_kind,  -- model_switch | context_compaction | approval_request
               -- | permission_denial | retry | hook_result | context_reset
               -- | file_mutation | skill_resolution | execution_failure
               -- | record_conflict
  event_ns,
  attributes JSONB
)
```

### Ontology — orthogonal dimensions

Closed and small. New tool categories extend `tool_kind`, not `node_kind`.

```
node_kind        ∈ {turn, llm_call, invocation, artifact, event}
invocation_kind  ∈ {tool, skill, subagent, hook}
tool_kind        ∈ {builtin, mcp, shell, filesystem, ...}
toolkit_type     ∈ {skill, mcp_tool, subagent, builtin_tool}   -- product classification
                                                                   for fact table only
```

### Identity

```
execution_key = HMAC(cardinal_execution_key, org_id || adapter || session_id)
execution_id  = internal PK (ULID), assigned by ingest on first observation
node_key      = HMAC(execution_key, node_kind || native_seed)
                -- native_seed = tool_use.id | call_id | (user_turn_seq,turn_seq,tool_seq)
session_id    = adapter-native, verbatim
trace_id      = HMAC(cardinal_trace_key, org_id || adapter || session_id)[:16]
                -- OTLP projection only, never used to look up product identity
```

Adapters never mint or persist `execution_id`. Every envelope carries
`(org_id, adapter, session_id)`; ingest computes `execution_key`, looks
up-or-creates the row, then attaches its `execution_id`.

### Provenance — six independent axes

Every node carries all six. UX reads them directly to qualify what it renders.

| Field              | Values                                                        |
|--------------------|---------------------------------------------------------------|
| `identity_source`  | native, derived, synthetic                                    |
| `parent_source`    | native, transcript, temporal, inferred, unknown               |
| `timing_source`    | native, reconstructed, estimated, marker, unknown             |
| `model_source`     | explicit, inherited, session_default, unknown                 |
| `toolkit_source`   | native, command_parse, prompt_inference, unknown              |
| `usage_source`     | native, allocated, estimated, unknown                         |

### Skill lifecycle

Node `skill` carries `lifecycle_state ∈ {requested, resolved, executed}`.

- `requested` — prompt regex matched a `/command`; no evidence adapter loaded it.
- `resolved` — adapter loaded the skill (instructions injected into context).
- `executed` — skill actually invoked / controlled subsequent execution.

**Adoption metrics count `executed` only** by default. `requested`/`resolved`
surface as separate meters when relevant.

### Model semantics

Two distinct concepts on nodes:

- `request_model` (a.k.a. `gen_ai.request.model`) — only on `llm_call` nodes.
- `orchestrator_model` (a.k.a. `cardinal.orchestrator.model`) — on `tool`,
  `skill`, `subagent` invocation nodes; the model whose turn invoked them.

UX phrasing: *"invoked by Opus"*, never *"tool ran on Opus"*.

An MCP server may internally invoke a model Cardinal cannot observe — that
model is unknown to us and must not be silently attributed.

### Ingestion is observation-based

Envelopes carry **observations**, not writes. Two-table pattern:

```sql
execution_observations(          -- append-only raw
  org_id, execution_key, node_key, field, value, provenance, observed_ns
)

execution_nodes(...)             -- materialized canonical state, derived by reducer
```

**Precedence** per field:
`native > reconstructed > derived > estimated > inferred > unknown`

Reducer rules on each observation batch:

- higher provenance → **replace** canonical value; prior value retained in raw
  table as superseded observation
- same provenance, same value → **no-op**
- same provenance, incompatible value → keep canonical, emit
  `execution_events(event_kind='record_conflict')` with both values
- lower provenance → keep observation in raw table; canonical unchanged

`execution_nodes` is always regenerable from `execution_observations`. Replay
is `TRUNCATE execution_nodes; SELECT reduce(observations)`.

### Fact model

```sql
toolkit_invocation_facts(        -- one row per actual invocation
  org_id, execution_id, node_id,
  toolkit_type, toolkit_name,
  orchestrator_model, request_model,
  input_tokens, output_tokens, cached_tokens, cost_usd,
  start_ns, end_ns, duration_ns,
  outcome_id,                    -- FK to outcome linkage (nullable)
  provenance JSONB,              -- six axes verbatim, so UX can qualify
  context_snapshot_ns,           -- point-in-time marker: which execution_context
                                  -- state was baked into this row (see Phase 0.2.b)
  repository_name, branch, pr_number, initiative_id, actor_id, ...
                                  -- inheritable ExecutionContext fields, snapshotted
                                  -- at projection time (Phase 0.2.b), not joined live
)
```

Rollups (7d / 30d / per-repo / per-engineer / per-initiative) are
`SELECT ... GROUP BY` at query time initially. Add an acceleration table
only when a specific query proves slow — never as the semantic layer.
Per-repo/per-engineer/per-initiative rollups are made possible by the
`ExecutionContext` contract (Phase 0.2, see below) — the fact table
snapshots the resolved context per row, so a late-bound field (e.g.
`pr_number` arriving after the session ends) does not retroactively change
rows already written; a re-projection picks up the enrichment.

### Compatibility projection

`agent_sessions_toolkit_maps.{skills_used, agents_used, mcp_servers_used,
tool_counts}` become **derived read models** written by a projector that
reads `toolkit_invocation_facts`. Once the graph pipeline is authoritative
for a session, hook-based writers to those columns are disabled for that
session (flagged by `agent_sessions.execution_graph_authoritative=true`).
Both surfaces read the derived JSONB during dashboard migration; no
dashboard code changes until new UX ships.

### One canonical pipeline

```
adapter observation
    ↓
execution_observations  (append-only raw)
    ↓
reducer (provenance precedence)
    ↓
execution_nodes / execution_edges / execution_events
    ↓
toolkit_invocation_facts
    ├── new toolkit UX / drilldown
    └── compat projector → agent_sessions_toolkit_maps → existing dashboard

Adapters serialize each envelope as an OTLP `Span` on emit (per
`canonical-model.md` §14 "OTLP wire format — envelopes as spans") and POST
`ResourceSpans` to the same OTLP intake `/v1/traces` endpoint that already
carries every other adapter signal. No custom `/v1/envelopes` endpoint, no
separate auth, no HMAC gate.

### OTLP as wire format (revised from earlier design)

**Original plan called for a separate `/v1/envelopes` handler with
adapter→envelope→lakerunner→OTLP projection.** Corrected 2026-07-25:
envelopes describe span-shaped execution atoms, so the natural wire
representation IS OTLP traces. This collapses "Phase 2 OTLP projection"
into "Phase 1 emission" — the traces are valid the moment they leave the
adapter, and Tempo/Honeycomb/Datadog render them meaningfully without
lakerunner in the loop.

Lakerunner's job: **tap** the OTLP trace pipeline (same tap pattern as
`internal/agentsessions/processor.go` uses for log records today), filter
spans with `cardinal.envelope.record_type` present, deserialize back into
`Envelope`, run the reducer. No HTTP handler on lakerunner.

Field-level mapping (Cardinal field → OTel attribute → resource/span
scope) is normative in `docs/canonical-model.md` §12 (SemConv) and §14
(wire format). Both plugin emit and lakerunner tap read those tables
directly.

### Ownership boundary

- **cardinal-agent-plugins** — native observation, adapter semantics,
  envelope emission.
- **lakerunner** — observation ingestion, canonical graph, fact derivation,
  OTLP projection, query surface.
- **conductor** — product interpretation, UX.

*Agent plugins observe. Lakerunner understands. Conductor explains.*

## Phases

### Phase 0 — Evidence and contract

No emitter or ingest code ships. Deliverables:

- **`core/cardinal_core/envelope.py`** — record types, schema version 1,
  validation. Record types: `node_observed`, `node_updated`, `edge_observed`,
  `execution_event`, `usage_observed`, `artifact_link_observed`.
- **`docs/canonical-model.md`** — node/invocation/tool/toolkit kinds, edge
  kinds, event kinds, six provenance axes, identity rules, skill lifecycle
  states, model semantics, precedence table.
- **`docs/privacy-redaction.md`** — rules for prompts, tool args, tool
  results, file diffs, secrets. What lands verbatim, what's hashed, what's
  truncated, what never leaves the adapter.
- **`fixtures/<adapter>/<scenario>/`** — captured raw payloads per adapter
  for six core scenarios: `simple-turn`, `tool-invocation`, `parallel-tools`
  (where supported), `subagent-invocation` (where supported),
  `skill-invocation` (with `requested-only` vs `executed` distinguished),
  `session-continuation`. Capture via `CARDINAL_*_DEBUG_PAYLOADS=1` on real
  sessions.
- **`docs/adapter-capability-matrix.md`** — table (adapter × capability ×
  `{native | derivable | heuristic | unavailable}`) with a fixture path
  proving each `native`/`derivable` cell.
- **Empirical resolutions**:
  - **Claude `parent_tool_use_id`** — RESOLVED 2026-07-25 against real
    transcripts: the field is **never present**. Every invocation node's
    `parent_source` is `TRANSCRIPT` (positional inference), never `NATIVE`.
  - **Claude subagent tool name** — RESOLVED 2026-07-25: the tool is named
    `Agent`, not `Task`. Prior plan/spec text referring to `Task` is a
    documentation drift.
  - **Claude skill mechanism** — RESOLVED 2026-07-25: skills surface as a
    `Skill` tool_use with `input={"skill": name, "args": ...}`. There is
    no `SlashCommand` tool_use in real transcripts (kept as a
    forward-compat alias only).
  - **Claude subagent identity link** — RESOLVED 2026-07-25: the reliable
    correlation is `<transcript_dir>/subagents/agent-<id>.meta.json`'s
    `toolUseId` field. Static `tool_result` for a subagent call carries
    free text, not a structured `agentId` (that only appears on the live
    hook payload, not the transcript).
  - Codex `SubagentStop` payload shape — still open.
  - Cursor per-turn identity beyond `generation_id` — still open.
  - Gemini `AfterAgent` payload keys — still open.
  - Omnigent claude/codex-native subagent visibility gap — still open.

**Gate**: given fixtures, can the reducer produce a `toolkit_invocation_facts`
row with `toolkit_name`, `orchestrator_model`, `duration_ns`, and (at least)
`end_ns`, each with honest provenance? Yes → adapter is Phase-1-ready. No →
matrix documents the gap.

### Phase 0.2 — Execution context contract and reducer

Gate between Phase 0 and Phase 1: repo/PR/initiative/engineer dimensions
must be first-class in the contract *before* the lakerunner reducer
hardens, or every rollup query ends up rediscovering them from ad-hoc
`attributes` JSONB per adapter. Two sub-items:

- **0.2.a — contract update** (this task). Adds `ExecutionContext` as a
  first-class, execution-wide, inheritable tagging contract:
  `core/cardinal_core/envelope.py` gains `RecordType.CONTEXT_OBSERVED` +
  the `ExecutionContext` payload + `ContextSource` provenance axis +
  `CONTEXT_SOURCE_PRECEDENCE`; `docs/canonical-model.md` gains the
  "Execution context and inheritance" and "OpenTelemetry semantic
  conventions mapping" sections. Contract-only — no ingest/reducer code,
  no adapter emission changes. `SCHEMA_VERSION` stays 1 (additive).
- **0.2.b — lakerunner reducer extension** (follow-up, other repo). New
  `execution_context` table: `org_id, execution_id` (one row per
  execution), every inheritable field as a nullable column, `attributes`
  JSONB, and `context_source_per_field` JSONB tracking which
  `ContextSource` set each field (so the precedence merge is auditable
  per field, not just per row). Late-bound enrichment applied via
  `CONTEXT_SOURCE_PRECEDENCE` per field, same observation model as node
  reduction (§9 of the canonical model). The OTLP projection (Phase 2)
  consumes this table at span-emit time using the mapping table added in
  0.2.a.

**Gate**: `ExecutionContext` roundtrips through `envelope.to_json`/
`from_json`, validation rejects empty/malformed observations, and
`CONTEXT_SOURCE_PRECEDENCE` is committed — before lakerunner starts
building the 0.2.b reducer against a contract that might still move.

### Phase 1 — Claude vertical slice (end-to-end, thin)

- **Adapter**: Claude `Stop` hook walks transcript and emits envelope
  records (nodes for turn/skill/subagent/tool/llm_call; edges; events for
  `file_mutation` + `skill_resolution`). Wildcard PreToolUse registered only
  if Phase 0 shows it's required for `parent_source=native`.
- **Lakerunner ingest**: `/v1/envelopes` HTTP handler → append to
  `execution_observations` → run reducer → upsert
  `execution_nodes`/`execution_edges`/`execution_events`. Migrations are
  additive; existing tables untouched.
- **Fact projector**: worker maintaining `toolkit_invocation_facts` from
  new/updated nodes.
- **Query layer**: two MCP tools + REST endpoints —
  - `toolkit_invocations(org, toolkit_type, toolkit_name, window)`
  - `execution_neighborhood(execution_id, node_id, depth=1)`
- **Conductor UX**: `OutcomeToolkit.tsx` click-through → session list →
  case-file view (user turn → skill with lifecycle_state badge → subagents →
  tools → files → PR). **No Gantt yet.** Model mix widget adjacent.

### Phase 2 — OTLP projection ~~(deferred/collapsed)~~

**Collapsed into Phase 1** by the 2026-07-25 rewire: envelopes are OTLP
spans on the wire, so the "projection" step vanishes. Anything remaining
here is downstream polish (backend-specific trace-view tuning, span-link
ergonomics for non-parent_of edges once real cross-file/cross-session
graphs land). Not a blocker for any subsequent phase.

### Phase 3 — Codex + Gemini

- Both adapters against the same fixture-based conformance suite.
- Codex: transcript walk at `Stop`, `call_id`-seeded node keys, `end_only`
  subagent spans marked in provenance.
- Gemini: per-hook emission from `BeforeAgent`/`AfterModel`/`AfterTool`/
  `AfterAgent`; counter-synthesized node keys with timestamp mixed in for
  concurrent-call disambiguation.
- **Adoption metrics count `executed`-lifecycle skill nodes only.**

### Phase 4 — Cursor

- Truthful minimum only: session, turn, tool, subagent markers.
- No manufactured parentage, timing, or model attribution.
- `postToolUse` → one-shot tool node; `subagentStop` → leaf subagent node.
- No `llm_call` nodes (documented gap).

### Phase 5 — Omnigent

- Real `contextvars` context propagation into the same canonical graph.
- Delegated conversations → child `subagent` nodes via
  `parent_conversation_id`.
- No privileged schema — same envelope contract as CLI adapters.
- Document known gaps: `llm_response` per-turn granularity on some engines,
  silent native-UI subagents.

### Phase 6 — Advanced product surfaces

- Full execution tree / Gantt.
- Parallel subagent timeline.
- Retry-loop detection.
- Skill effectiveness.
- Toolkit cost + latency analysis.
- Model × toolkit interaction analysis.
- Outcome contribution attribution.
- Cross-adapter execution comparisons.

## Subagent DAG for implementation

Each phase decomposes into agents with typed inputs and outputs. Solid arrows
are dependencies; dashed are optional inputs. Concurrent-capable agents are
grouped; sequential gates are named.

### Phase 0

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ Phase 0 — Evidence & contract                                    │
  │                                                                  │
  │  [P0.A envelope-spec]         [P0.B ontology-spec]               │
  │  in: design doc               in: design doc                     │
  │  out: envelope.py             out: canonical-model.md            │
  │         │                            │                           │
  │         └─────────────┬──────────────┘                           │
  │                       ↓                                          │
  │              [P0.C schema-gate]         (sequential)             │
  │              validates ontology ↔ envelope alignment             │
  │              out: schema-v1.frozen                               │
  │                       │                                          │
  │        ┌──────────────┼──────────────────────┐                   │
  │        ↓              ↓                      ↓                   │
  │  [P0.D-claude]  [P0.D-codex]  [P0.D-cursor] [P0.D-gemini]        │
  │                                              [P0.D-omnigent]     │
  │        (five parallel fixture-capture agents)                    │
  │  in: schema-v1.frozen + adapter source + real session access     │
  │  out: fixtures/<adapter>/<scenario>/*.json + capability rows     │
  │                       │                                          │
  │                       ↓                                          │
  │              [P0.E matrix-synth]        (sequential)             │
  │  in: five capability-row sets                                    │
  │  out: docs/adapter-capability-matrix.md                          │
  │                                                                  │
  │              [P0.F privacy-spec]        (parallel with D+E)      │
  │  in: fixtures samples                                            │
  │  out: docs/privacy-redaction.md                                  │
  │                                                                  │
  │              [P0.G phase-0-gate]        (sequential)             │
  │  in: matrix + fixtures                                           │
  │  out: per-adapter go/no-go for Phase 1                           │
  └──────────────────────────────────────────────────────────────────┘
```

### Phase 0.2 — Execution context contract and reducer (gate before P1)

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ Phase 0.2 — Execution context contract                           │
  │                                                                  │
  │  [P0.2.a context-contract]      (sequential, this task)          │
  │  in: canonical-model.md + envelope.py (Phase 0 baseline)         │
  │  out: ExecutionContext + ContextSource + CONTEXT_SOURCE_PRECEDENCE│
  │       in envelope.py; "Execution context and inheritance" +      │
  │       "OTel SemConv mapping" sections in canonical-model.md      │
  │                       │                                          │
  │                       ↓                                          │
  │  [P0.2.b context-reducer]       (sequential, other repo)         │
  │  in: P0.2.a contract (frozen)                                    │
  │  out: lakerunner execution_context table + per-field precedence  │
  │       merge + OTLP projection reads the table at span-emit time  │
  └──────────────────────────────────────────────────────────────────┘
```

### Phase 1 — Claude vertical slice

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ Phase 1 — Claude vertical slice                                  │
  │                                                                  │
  │  [P1.A reducer]              [P1.B storage-migration]            │
  │  in: canonical-model.md      in: canonical-model.md              │
  │  out: internal/graph/*.go    out: lrdb migrations                │
  │         │                            │                           │
  │         └──────────────┬─────────────┘                           │
  │                        ↓                                         │
  │            [P1.C ingest-endpoint]       (sequential)             │
  │  in: reducer + migrations                                        │
  │  out: /v1/envelopes handler + tests using P0 fixtures            │
  │                        │                                         │
  │        ┌───────────────┼───────────────────┐                     │
  │        ↓               ↓                   ↓                     │
  │  [P1.D claude-emitter]  [P1.E fact-projector]  [P1.F queries]    │
  │  in: fixtures            in: nodes/edges       in: nodes/facts   │
  │  out: hooks/trace-*.py   out: toolkit_facts    out: MCP + REST   │
  │                                                                  │
  │        (three parallel — all read reducer + schema)              │
  │                        │                                         │
  │                        ↓                                         │
  │            [P1.G integration-test]      (sequential)             │
  │  in: emitter + fact + queries                                    │
  │  out: end-to-end fixture → observation → node → fact → query     │
  │                        │                                         │
  │                        ↓                                         │
  │            [P1.H conductor-ui]          (sequential)             │
  │  in: query surface                                               │
  │  out: OutcomeToolkit drilldown + case-file view                  │
  │                        │                                         │
  │                        ↓                                         │
  │            [P1.I compat-projector]      (sequential)             │
  │  in: fact projector + agent_sessions_toolkit_maps                │
  │  out: derived-map writer + feature flag                          │
  └──────────────────────────────────────────────────────────────────┘
```

### Phase 2 — OTLP projection

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ Phase 2 — OTLP projection                                        │
  │                                                                  │
  │  [P2.A span-mapper]                                              │
  │  in: nodes + edges + edge-kind → span-link rules                 │
  │  out: internal/graph/otlp_projection.go                          │
  │                        │                                         │
  │                        ↓                                         │
  │            [P2.B traces-endpoint]                                │
  │  in: mapper                                                      │
  │  out: /v1/traces + emitter loop                                  │
  │                        │                                         │
  │            ┌───────────┴───────────┐                             │
  │            ↓                       ↓                             │
  │      [P2.C tempo-verify]     [P2.D honeycomb-verify]             │
  │      out: rendered trace     out: rendered trace                 │
  │            evidence                evidence                      │
  └──────────────────────────────────────────────────────────────────┘
```

### Phase 3 — Codex + Gemini

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ Phase 3 — Codex + Gemini                                         │
  │                                                                  │
  │  [P3.A codex-emitter]           [P3.B gemini-emitter]            │
  │  in: fixtures + reducer         in: fixtures + reducer           │
  │  out: hooks emit envelopes      out: hooks emit envelopes        │
  │        │                                │                        │
  │        └─────────────┬──────────────────┘                        │
  │                      ↓                                           │
  │        [P3.C conformance-suite]         (sequential)             │
  │  in: both emitters + shared fixture harness                      │
  │  out: red/green per (adapter, scenario, capability)              │
  │                      │                                           │
  │                      ↓                                           │
  │        [P3.D adoption-lifecycle-gate]   (sequential)             │
  │  in: skill lifecycle_state distribution across new fixtures      │
  │  out: adoption metrics restricted to executed only               │
  └──────────────────────────────────────────────────────────────────┘
```

### Phase 4 — Cursor

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ Phase 4 — Cursor                                                 │
  │                                                                  │
  │  [P4.A cursor-emitter]                                           │
  │  in: fixtures + reducer                                          │
  │  out: postToolUse + subagentStop → envelopes, provenance marked  │
  │                      │                                           │
  │                      ↓                                           │
  │        [P4.B fidelity-doc]                                       │
  │  in: emitter behavior                                            │
  │  out: docs/adapter-fidelity/cursor.md — what's honestly missing  │
  └──────────────────────────────────────────────────────────────────┘
```

### Phase 5 — Omnigent

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ Phase 5 — Omnigent                                               │
  │                                                                  │
  │  [P5.A tracer-integration]                                       │
  │  in: cardinal_omnigent/telemetry.py + envelope contract          │
  │  out: contextvars propagation + envelope emission per phase      │
  │                      │                                           │
  │                      ↓                                           │
  │        [P5.B subagent-conversation-link]                         │
  │  in: parent_conversation_id + envelope                           │
  │  out: delegated_to edges emitted correctly                       │
  │                      │                                           │
  │                      ↓                                           │
  │        [P5.C fidelity-doc]                                       │
  │  out: docs/adapter-fidelity/omnigent.md                          │
  └──────────────────────────────────────────────────────────────────┘
```

### Phase 6 — Advanced surfaces (parallel product agents, one queue)

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ Phase 6 — Advanced product surfaces                              │
  │                                                                  │
  │  Each is an independent conductor-side agent, unblocked once     │
  │  Phase 3–5 land its data source. Dependencies noted.             │
  │                                                                  │
  │  [P6.A execution-tree-ui]        needs: Phase 1                  │
  │  [P6.B parallel-subagent-gantt]  needs: Phase 3                  │
  │  [P6.C retry-loop-detector]      needs: Phase 1 + events         │
  │  [P6.D skill-effectiveness]      needs: Phase 3 + outcome links  │
  │  [P6.E toolkit-cost-latency]     needs: Phase 1                  │
  │  [P6.F model-toolkit-matrix]     needs: Phase 3                  │
  │  [P6.G outcome-contribution]     needs: Phase 1 + PR linkage     │
  │  [P6.H cross-adapter-compare]    needs: all adapters shipped     │
  └──────────────────────────────────────────────────────────────────┘
```

### DAG top-view

```
       P0 (evidence & contract)
              │
              ↓
       P0.2 (execution context contract [0.2.a] → reducer [0.2.b])
              │        (gate: contract lands before reducer hardens)
              ↓
       P1 (Claude vertical slice)
         ┌────┴────┐
         ↓         ↓
       P2       P3 (Codex + Gemini)
     (OTLP)         │
                    ↓
                  P4 (Cursor)
                    │
                    ↓
                  P5 (Omnigent)
                    │
                    ↓
                  P6 (advanced surfaces, parallel)
```

P2 branches off P1 (doesn't block P3). P4/P5 sequential only because
they share a single reviewer bandwidth constraint, not a technical
dependency — P4, P5, and P3 could all run in parallel once P1 lands
if capacity allows.

## Open items requiring decision before Phase 1

1. **Where does the `/v1/envelopes` endpoint live in lakerunner?** — new
   service, or extend `internal/agentsessions/`?
2. **Cost calculation** — does lakerunner compute `cost_usd` at fact-projection
   time from `(request_model, tokens)` via the existing pricing table, or does
   the adapter attach it upstream? (Recommendation: lakerunner side, single
   source of pricing truth.)
3. **Outcome linkage timing** — `outcome_id` on the fact table: written by
   the projector when the outcome exists at reduction time, or a separate
   backfill job as outcomes are marked later? (Recommendation: backfill job,
   since outcomes lag execution.)

## Non-goals for v1

- Live span emission (all Phase-1 emission is retroactive at Stop).
- Real-time trace streaming to third-party backends (OTLP projection is
  batched/read-through).
- Cross-session execution graphs (parent_session_id restart chains remain
  session-of-session, not merged into one execution).
- Full-fidelity Cursor (documented gap; Cursor stays coarse).
- Deprecation of `agent_sessions_toolkit_maps` (compat projection stays as
  long as dashboards read it).

# Agent Execution Graph — implementation plan

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
  provenance JSONB               -- six axes verbatim, so UX can qualify
)
```

Rollups (7d / 30d / per-repo / per-engineer / per-initiative) are
`SELECT ... GROUP BY` at query time initially. Add an acceleration table
only when a specific query proves slow — never as the semantic layer.

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

    (separately, from execution_{nodes,edges}):
    → OTLP projection → /v1/traces → Tempo / Honeycomb / Datadog
```

### OTLP as projection

A materialization walks the graph and emits `ResourceSpans`: turn → skill →
subagent → tool → llm_call, with **span-links** where the graph edge isn't
`parent_of` (multi-file, cross-session, workflow contribution). Emitted from
the ingest side, never from the adapter. Adapters emit envelopes only.

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
  - Claude `parent_tool_use_id` presence on Task calls.
  - Codex `SubagentStop` payload shape.
  - Cursor per-turn identity beyond `generation_id`.
  - Gemini `AfterAgent` payload keys.
  - Omnigent claude/codex-native subagent visibility gap.

**Gate**: given fixtures, can the reducer produce a `toolkit_invocation_facts`
row with `toolkit_name`, `orchestrator_model`, `duration_ns`, and (at least)
`end_ns`, each with honest provenance? Yes → adapter is Phase-1-ready. No →
matrix documents the gap.

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

### Phase 2 — OTLP projection

- Ingest-side materialization from execution graph to OTLP `ResourceSpans`.
- New `/v1/traces` endpoint on lakerunner (read-through or scheduled emit).
- Sanity render in Tempo and Honeycomb.
- Span-links used where edge kind isn't `parent_of`.
- **No adapter-side OTLP code.** Trace generation stays server-side.

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

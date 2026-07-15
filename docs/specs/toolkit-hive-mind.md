# Toolkit Hive-Mind — cross-adapter capability-fit optimization

Status: draft v1 · 2026-07-15
Owner: (assign)
Home: authored in `cardinal-agent-plugins/docs/specs/` but **cross-repo** — it
spans `cardinal-agent-plugins` (emit), `lakerunner` (processor), and `conductor`
(miner + optimize skill + UI). Split per-repo when adopted.
Related (in `conductor/docs/specs/`): `latent-subagent-harvester.md` (⚠ deleted
from main in the 2026-07-09 skill-first pivot; survives on branch
`feat/latent-subagent-harvester`),
`optimize-toolkit-mcp-tools.md`, `in-session-optimization-skill.md`,
`model-mix-derived-taxonomy.md`, `agent-sessions.md`, `agent-outcomes-skills.md`.

> Written to be implementable in small, verifiable steps. Every task names the
> exact file(s) to touch, the field shapes, and a definition of done. Where a
> decision could go two ways, this spec **makes the decision** — do not
> re-litigate; if a decision is wrong, change it here first, then implement.

---

## 0. Thesis

`optimize-toolkit` is not a sub-agent extractor. It is a **cross-session,
cross-adapter capability-fit optimizer** — a "hive-mind" over agent session
telemetry. It mines what every capability (sub-agent, skill, slash-command,
built-in tool, MCP server/tool) is used for, at what token cost, and with what
outcome, then recommends a better fit. Sub-agent extraction is **one**
recommendation kind among several.

Two invariants govern the whole design:

- **Generic-first.** The mining, normalization, scoring, and recommendation
  logic is runtime-neutral and covers all five adapters (`claude-code`,
  `codex`, `cursor`, `gemini`, `omnigent`). No Claude-Code-shaped assumptions
  in the shared path.
- **Omnigent is special only at the edge.** Omnigent (the polly orchestrator
  runtime) is the one adapter that can *consume a recommendation as a live
  dispatch policy* instead of only materializing an artifact a human later
  invokes. The generic recommendation object is identical for all adapters;
  omnigent gets one extra **consumption** mode, never a different producer.

---

## 1. Current state (verified 2026-07-15)

Confirmed by direct code reads across three repos. This is the foundation the
spec builds on — do not re-derive, but re-verify a cited line before editing
near it.

### 1.1 Emit side — `cardinal-agent-plugins`
- Tokens ride only on the per-model-call event (`cardinal.turn_usage`) and the
  per-spawn event (`cardinal.subagent_usage`). `cardinal.turn_tool` carries
  per-tool **identity only, no tokens** (`tests/test_contract.py:72-76`).
- No `skills_used` / `commands_used` on the wire (grep-empty). The only per-tool
  breakdown is a **count** histogram `subagent_tool_counts`, **claude-only**
  (`adapters/claude/hooks/subagent-usage.py:21,243-251`).
- Capability names are raw/per-runtime; normalization is explicitly deferred
  downstream (`adapters/codex/hooks/cardinal-codex-telemetry.py:374-377`). The
  cited `docs/specs/subagent-telemetry-enrichment.md` does **not exist** in the
  checkout (dangling reference from `subagent-usage.py:21`, `turn-usage.py:26`).
- The **shared** contract test covers only 4 of 5 adapters — omnigent absent
  (`tests/test_contract.py:38`: `ADAPTERS = ("claude","codex","cursor","gemini")`).
  Omnigent IS telemetered and has its own tests
  (`adapters/omnigent/tests/test_omnigent.py`); it is untested by the *shared*
  parity gate, not untelemetered.

### 1.2 Processor — `lakerunner`  (where per-capability tokens are computed)
- `internal/agentsessions/processor.go` folds native Claude-Code OTel events
  (`api_request`, `tool_result`) plus `cardinal.*` into one row per
  `(org, session_id)`.
- **Per-capability token attribution EXISTS** via equal-split apportionment:
  each `api_request`'s worked tokens (`input + cache_creation + output`) are
  split across the `tool_result`s that pended since the previous request
  (`lrdb/agent_sessions_attribution.go:129-206`). Result folds into
  `skills_used` / `agents_used` / `mcp_servers_used` / `tool_counts` as
  `{n, err, tok, tok_by_model}`. `agents_used` also carries `subtok` from
  `cardinal.subagent_usage`.
- **`commands_used` is count-only** (from `cardinal.git_state`'s
  `cardinal_command` tag; never enters the pending set).
- **Only `claude-code` + `codex` are ingested** — `internal/agentsessions/
  lkrn_tap.go:29`: `{service_name=~"^(claude-code|codex)$"}`.
- **Identity extraction is Claude-Code-shaped only** — `toolkitKey`
  (`processor.go:341-376`) hard-codes Claude's `tool_parameters` JSON keys
  (`skill_name`, `subagent_type`, `mcp_server_name`). `agent_runtime` is stored
  (`processor.go:158`) but **never read** to branch extraction. No canonical-name
  mapping anywhere.
- Attribution is an approximation: equal-split within a turn; `cache_read`
  deliberately not attributed (`agent_sessions_attribution.go:50-53`).

### 1.3 Consume side — `conductor`
- `ToolkitPanel` (`packages/ui-pages/src/dashboards/system/OutcomeToolkit.tsx`)
  renders segments `Skills / Agents / MCP servers / Tools` with a
  `tokens|adoption` toggle; `ToolkitUseCount = {n, err, tok, tok_by_model}` per
  capability (`packages/maestro/src/routes/agent-outcomes.ts:223-230`). Commands
  render count-only (`OutcomesContext.tsx:48`).
- `optimize-toolkit-mcp-tools.md` defines 8 MCP tools; the mining stages exist
  (`packages/maestro/src/services/subagent-harvester-stages.ts`,
  `subagent-spec-generator.ts`); the ledger exists (migration
  `20260708120000_subagent_recommendations`) and its `kind` enum ALREADY includes
  `pin`/`session_model` (`subagent-recommendations.ts:22`) — only the runtime
  scorers are limited to `mint`/`pipeline` (`subagent-harvester-stages.ts:667`).
- The `/cardinal:optimize-toolkit` **skill itself is unbuilt** (spec-only).
- **No cross-adapter normalization** (raw names, per-runtime).

### 1.4 One-line gap summary
The per-capability token signal the hive-mind needs **already exists** — but
only for 2 of 5 adapters, only under Claude-shaped parsing, with raw
un-normalized names, and mining is still sub-agent-only. Close those four gaps
and the hive-mind is unblocked.

---

## 2. Gaps → workstreams

| # | Gap | Workstream | Repo |
|---|-----|-----------|------|
| G1 | Only claude-code+codex ingested; Claude-shaped identity extraction | **W1 — Generic ingestion & identity** | lakerunner |
| G2 | No canonical cross-adapter capability identity | **W2 — Capability taxonomy** | lakerunner + conductor |
| G3 | Miner is sub-agent-only; kinds = mint/pipeline | **W3 — Generalize the miner** | conductor |
| G4 | `/optimize` skill + per-adapter render + omnigent live-consume unbuilt | **W4 — Delivery** | conductor + adapters |
| G5 | commands count-only; MCP server-level only | **W5 — Attribution completeness** | lakerunner |
| G6 | Emit-side identity parity; missing enrichment spec | **W-plugins** | cardinal-agent-plugins |

Dependency order: **W-plugins ↔ W1 → W2 → W3 → W4**. W5 is independent/optional.

---

## 3. Design decisions (made — do not re-open casually)

### D1 — Capability identity is a normalized triple
Every mined unit is a `Capability`:

```
Capability = {
  kind:  "tool" | "agent" | "skill" | "mcp",   // canonical identity kinds (the live 4-way set)
  key:   string,   // canonical, cross-adapter identity (see D2)
  raw:   string,   // verbatim per-runtime name, retained for display/debug
}
```

`kind` + `key` is the aggregation grain for the hive-mind. `raw` is never the
aggregation grain (that is the bug today).

**Canonical kind set = the shipped 4-way `tool | agent | skill | mcp`.** This is
the ONE identity vocabulary across the whole stack — there are no parallel grains.
It is what every implementation already emits: lakerunner
(`internal/agentsessions/identity/identity.go` — `KindTool | KindAgent | KindSkill | KindMCP`),
conductor model-mix (`ItemKind`), and the outcomes UI (the consolidated shared
`CapabilityKind` type). Reconciled 2026-07-16: an earlier draft listed a 6-way set
(`mcp_server`/`mcp_tool` split + `subagent` + `command`); that set was never
implemented and is retired here to match shipped reality. Rules:

- Use `agent`, never `subagent` — the label every implementation actually uses.
- `command` is **count-only**, not a token-attributed identity kind. Commands are
  sourced separately (the `cardinal.git_state` `cardinal_command` tag), never flow
  through the equal-split attribution pipeline (D3), and surface only as a
  co-occurrence axis. model-mix may carry them as a local `cmd` graph node
  (zero-cost) layered on top of the 4 canonical kinds — a documented local
  extension, not a 5th canonical kind.
- **MCP grain is single (`mcp`) today; the `mcp_server`/`mcp_tool` split is
  deferred to W5.T5.2 (low priority).** The raw wire data for the split already
  exists (codex/cursor/gemini emit `mcp_tool_name`), but lakerunner reads only
  `mcp_server_name` and drops the tool grain, so `mcp` stays server-level until
  W5.T5.2 lands.

**Attribution mapping (single source of truth for T1.3).** The apportionment
switch in `lrdb/agent_sessions_attribution.go` accepts exactly
`skill | agent | mcp | tool` — identical to the canonical kind set, so the mapping
is the identity. `command → (not attributed; count-only)`. Do not add a new
attribution kind without a migration.

### D2 — Normalization is a deterministic rule table first, LLM-label second
Build `canonicalizeCapability(runtime, kind, raw) -> key` as a **pure,
table-driven** function. Phase 1 = deterministic rules only:
- strip plugin/namespace prefixes to a stable stem (e.g.
  `plugin_cardinal_cardinal__lakerunner__execute_logs_query` and
  `mcp__cardinal__lakerunner__execute_logs_query` → `lakerunner.execute_logs_query`);
- lowercase, collapse separators;
- map known per-runtime synonyms via an explicit dictionary
  (`Read`/`read_file`/`view` → `fs.read`, etc.).
Phase 2 (separate, gated task) may add an LLM labeling pass reusing
`subagent-spec-generator.ts`'s shape, but **the deterministic layer must stand
alone** and be the default. No network call in the hot ingestion path.

### D3 — Retain equal-split token attribution; do not attempt true per-tool metering
The wire cannot support true per-tool tokens. Equal-split is a documented,
acceptable approximation for **relative** clustering (which capability dominates
cost). Keep it. Extend it — do not replace it.

### D4 — The recommendation object is runtime-neutral; adapters render
```
Recommendation = {
  id, org, subject_key: Capability.key, subject_kind: Capability.kind,
  kind: "extract"|"pin"|"downgrade"|"adopt"|"swap"|"consolidate"|"gap",
  evidence: { sessions:int, tok:int, tok_by_model:{}, merged_rate:float,
              cohort_adoption:float },
  proposal: { ... kind-specific ... },
  savings_est_usd: number,
  status: "open"|"accepted"|"dismissed"
}
```
Extend the existing `maestro_subagent_recommendations` ledger (widen `kind`
enum) rather than adding a new store.

### D5 — "Better fit" requires the outcome join
`adopt`/`swap`/`pin`/`downgrade` are emitted only when the outcome signal
(conductor's merged/lost PR classification) is available for the cohort. Absent
outcomes, the miner may emit only `extract`/`gap`/`consolidate` (structure-only
kinds). This keeps recommendations evidence-backed per the harvester's
"evidence or silence" rule.

---

## 4. Implementation plan (ordered, small, verifiable)

Each task: **Files · Change · Done-when.** A task is independently PR-able.

### W1 — Generic ingestion & identity (lakerunner)

**T1.1 Widen the ingest tap to all five runtimes.**
- File: `internal/agentsessions/lkrn_tap.go:29`.
- Change: selector → `{service_name=~"^(claude-code|codex|cursor|gemini|omnigent)$"}`.
- Done-when: unit test asserts the compiled selector matches all five service
  names and rejects an unrelated one.

**T1.2 Introduce a runtime-dispatched identity extractor.**
- Files: `internal/agentsessions/processor.go` (`toolkitKey`), new
  `internal/agentsessions/identity/` package.
- Change: replace the Claude-only body of `toolkitKey` with
  `extractCapability(agentRuntime, evt) (kind, raw string, ok bool)`, dispatched
  on the stored `agent_runtime`. One extractor per runtime. Claude's extractor =
  today's exact logic (byte-for-byte preserved). Codex/cursor/gemini/omnigent
  extractors read their own native event shapes (derive each from that adapter's
  emit code in `cardinal-agent-plugins/adapters/<runtime>/`; see PLG.1).
- **Feasibility (verified 2026-07-15 — this SCOPES the task):** existing
  telemetry supports these `(runtime × axis)` cells:

  | runtime | subagent | skill | command | mcp |
  |---|---|---|---|---|
  | claude-code | YES | YES | YES | YES |
  | codex | YES | n/a | YES | YES |
  | cursor | YES (`subagent_type`, no stable spawn id) | **NO** | YES* | YES |
  | gemini | PARTIAL (payload names unobserved) | **NO** | YES* | YES |
  | omnigent | PARTIAL (`subagent_description/id`; child gaps) | **NO** | YES* | YES |

  `*` command identity rides `cardinal_command` on `cardinal.git_state` — emitted
  only when the git-state path fires. `skill` is Claude-mostly:
  cursor/gemini/omnigent emit no skill identity.
- **Scope decision:** the extractor emits identity **only where present; it never
  fabricates.** A `NO` cell returns `ok=false` (axis absent for that runtime —
  not an error). IN SCOPE for T1.2 now: `mcp` + `command` (all runtimes) and
  `subagent` for claude/codex/cursor. The gemini/omnigent `subagent` PARTIAL
  cells and any `skill` emission for non-claude runtimes are handled by **PLG.1
  emit enrichment as a prerequisite** — do NOT block T1.2 on them; wire them as
  `ok=false` until PLG.1 lands.
- Done-when: table-driven tests cover every IN-SCOPE cell (one fixture per cell);
  `NO`/deferred cells assert `ok=false`; Claude fixtures reproduce current output
  unchanged (golden).

**T1.3 Keep attribution kind-agnostic.**
- File: `lrdb/agent_sessions_attribution.go:166-179`.
- Change: no change to the split math; ensure the `switch kind` covers all
  `kind`s and routes each to the right map. (Fold `mcp_tool` under `mcp_server`
  until W5.T5.2 lands.)
- Done-when: attribution test feeds a mixed-runtime pending set and asserts
  tokens land in the correct maps with equal-split + earliest-remainder.

### W2 — Capability taxonomy (lakerunner + conductor)

**T2.1 Implement `canonicalizeCapability` (deterministic).**
- Files: new `internal/agentsessions/identity/canonical.go`.
- Change: implement D2 Phase-1 rules + synonym dictionary. Canonicalize at
  **write time** in lakerunner (conductor stays pass-through). Key the JSONB map
  by canonical `key`; keep `raw` as a field inside the entry for display →
  entry becomes `{n, err, tok, tok_by_model, raw}`.
- Done-when: unit tests cover the four worked examples in D2 and assert two
  different-runtime raws collapse to one `key`.

**T2.2 Surface `key` through maestro types.**
- Files: `packages/maestro/src/routes/agent-outcomes.ts:230` (`ToolkitUseCount`),
  `packages/ui-pages/src/dashboards/system/OutcomesContext.tsx:15`.
- Change: group the panel by `key`, fall back to `raw` for legacy rows.
- Done-when: a test asserts two raws with one `key` render as a single row;
  existing tests pass.

### W3 — Generalize the miner (conductor)

**T3.1 Generalize the candidate object from spawn → capability.**
- Files: `packages/maestro/src/services/subagent-harvester-stages.ts`
  (`buildCandidateGroups`), `packages/maestro/src/db/repositories/
  subagent-recommendations.ts`, migration to widen the `kind` enum (D4).
- Change: cluster over `Capability(kind,key)` across sessions (the
  "one grain up" already sketched in `model-mix-derived-taxonomy.md:36-40`).
  Thread `tok`, `tok_by_model`, and the outcome join into each group.
- Done-when: stages test produces capability-level candidate groups from a
  multi-session, multi-kind fixture with correct `tok` sums.

**T3.2 Implement the new recommendation kinds.**
- Files: `subagent-harvester-stages.ts` (scorers), `subagent-spec-generator.ts`
  (artifact bodies for `extract`/`mint`).
- Change: add scorers for `adopt`, `swap`, `consolidate`, `gap`, `pin`,
  `downgrade` (D4); D5 gates the outcome-dependent kinds. Reuse existing
  thresholds (`RECURRENCE_MIN_SESSIONS`, pool floors).
- Done-when: one golden test per kind asserting emit/suppress at the thresholds.

### W4 — Delivery (conductor + adapters)

**T4.1 Build the 8 MCP tools** per `optimize-toolkit-mcp-tools.md`
(open, discover, score, compose, mark) over the generic recommendation object.
- Done-when: each tool has a contract test matching the spec's I/O shape.

**T4.2 Build the `/cardinal:optimize-toolkit` skill** per
`in-session-optimization-skill.md` — orchestrates the 8 tools; on confirmation
writes the adapter-native artifact. Render targets: claude →
`.claude/agents/<name>.md` / skill dir; codex/cursor/gemini → native
agent/skill/config path; omnigent → polly roster/skill entry.
- Done-when: dry-run test per adapter produces the correct artifact path + body
  from a fixed recommendation.

**T4.3 Omnigent live-consume mode (the one special case).**
- Change: omnigent's optimize surface loads `pin`/`downgrade`/`pipeline`
  recommendations as a **dispatch policy** the polly orchestrator reads at
  routing time, in addition to writing the artifact.
- Done-when: given a `pin(cluster→model)` rec, an omnigent dispatch test shows
  the default model for that cluster changed; other adapters ignore live-consume
  (artifact-only).

### W-plugins — emit support (cardinal-agent-plugins)

**PLG.1 Reconstruct + write `docs/specs/subagent-telemetry-enrichment.md`** from
the field-citation evidence (the enrichment already shipped; only the spec is
missing). Then confirm each adapter emits enough native identity for T1.2's
extractors; add per-adapter emit only where an extractor cannot derive identity
from existing native events. **Add `omnigent` to `ADAPTERS`** in
`tests/test_contract.py:38` and extend the parity gate to the
capability-identity fields.
- Done-when: contract test covers all five adapters for the identity fields the
  extractors depend on.

### W5 — Attribution completeness (optional, last, lakerunner)
- **T5.1** Give `commands_used` token attribution by routing typed slash-commands
  into `attr_pending`. A typed command has no `tool_result`, so attribute it the
  reasoning span of its turn — or leave count-only. **Default: leave count-only;
  revisit only if a command-cost recommendation is demanded.**
- **T5.2** Split MCP to `mcp_tool` grain alongside `mcp_server` (one extra kind
  in the pending set + one map). Low priority.

---

## 5. Non-goals, risks, open questions

- **Non-goal:** true per-tool token metering (wire can't support it; D3).
- **Non-goal:** replacing conductor's PR-based outcome classification.
- **Risk — taxonomy quality (highest).** If `canonicalizeCapability` under- or
  over-merges, the hive-mind aggregates wrong. Mitigation: deterministic rules +
  a golden corpus of raw→key pairs per adapter; LLM labeling only as an additive
  second pass, never the sole source.
- **Risk — privacy boundary.** Telemetry carries capability *names + counts +
  tokens*, never prompts/args/results. "What is this capability used for" is
  inferable only from co-occurrence + outcome, not intent. `adopt`/`swap` copy
  must be phrased as evidence ("ships 90% on the cheaper model"), never as
  claimed understanding of intent.
- **Open — taxonomy ownership.** Where does the raw→key dictionary live and who
  curates it? Proposal: versioned in lakerunner (`identity/`), mirrored
  read-only into conductor. Decide before W2.
- **Open — historical backfill.** Re-canonicalize existing rows or forward-only?
  Proposal: forward-only; panels fall back to `raw` for legacy rows.

---

## 6. Definition of done (initiative)

1. All five adapters ingested; identity extracted per-runtime (W1).
2. A Claude skill and a Codex command for the same job aggregate under one `key`
   in the toolkit panel (W2).
3. The miner emits at least `extract`, `adopt`, and `pin` recommendations at the
   capability grain, each evidence-gated (W3).
4. `/cardinal:optimize-toolkit` produces the correct adapter-native artifact for
   each of the five runtimes, and omnigent additionally live-consumes `pin` (W4).
5. `subagent-telemetry-enrichment.md` exists and the contract test covers all
   five adapters (PLG.1).

---

## 7. Review corrections (independent review, 2026-07-15)

Applied above: feasibility-scoped T1.2; `Capability.kind`→attribution-kind map
(D1); ledger-enum / omnigent-tests / deleted-doc precision. Remaining, folded
into their workstreams:

- **W2.T2.2** must also add `raw` to the maestro API type (`ToolkitUseCount` has
  none today, `agent-outcomes.ts:230`) and update the UI, which aggregates by map
  KEY (`outcomes-toolkit.ts:185`); include a legacy fallback so pre-`key` rows
  still render.
- **W3 builds nothing batch:** the batch orchestrator was removed
  (`conductor/.../index.ts:756`). W3 = refactor the pure LIBRARY stages
  (`subagent-harvester-stages.ts`) to the capability grain; invocation is via the
  W4 MCP tools, not a revived job.
- **W4 external dependency:** the MCP tool-hosting/gateway work that
  `optimize-toolkit-mcp-tools.md:405` marks deferred is a W4 prerequisite, not
  in-scope plumbing.
- **Command caveat:** `command` identity depends on the `cardinal.git_state` path
  firing; sessions without git state carry no commands (note in W5.T5.1).

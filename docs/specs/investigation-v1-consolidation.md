# Investigation v1 — consolidation plan

Status: APPROVED 2026-10-07 (all five decisions in §6; dark-deploy sequencing in §4).
Audited 2026-10-07
against conductor `origin/main` e019b95a (maestro v1.102.0), cardinal-agent-plugins
`origin/main` 0b28f99 (plugin 0.43.0), and the unmerged work below.

Feature development is stopped. This document turns the experiments into one
product object, the **Investigation**, and says exactly which code survives,
under which names, and in what merge order.

> Cardinal gives every agent session a durable Investigation. The
> Investigation remembers what the user asked, what the agent learned, and the
> evidence behind it. It continuously explains itself to humans through a
> Storyboard and can be observed and challenged by other agents.

## 0. What was audited

| Work | Where | State |
|---|---|---|
| Investigation object, event stream, live lifecycle, semantic WAL, evidence ledger | conductor main (#2066, #2069, #2070, #2071) | in prod |
| Storyboard projection (server) | conductor #2073 `feat/storyboard-projection` @87b94bd0, migrations `20261012090000`, `20261013090000` | draft, never deployed |
| Projection UI | conductor #2072 `feat/storyboard-projection-ui` @20dd614f | draft, **stacked on a37012f0, three commits behind #2073** |
| Compare prefab fix | conductor #2075 @0530de79 | draft, independent |
| Task intent + CardinalWatch (server) | conductor `feat/task-intent` 9089b03d, 186ab3e4 (local), migration `20261014090000` | local only |
| Projection guidance (plugin 0.44.0) | plugins #172 `feat/storyboard-projection` | draft |
| Task intent + watch CLI (plugin) | plugins `feat/task-intent` a1362e7, d565100, da60964, 9e72d3b (local) | local only, still 0.43.0 |
| Supervision experiment spec | plugins #173 | draft doc |

Nothing unmerged has reached a prod database. Every unmerged name, type,
column and migration can still be changed at no cost. Names already on main
(`cue.added`, `advisory`, `producer_claim`, the semantic types, and so on)
have prod rows, so this plan leaves them alone.

## 1. Target model

```
Investigation                                  maestro_investigations
  identity      id, org, origin, author        (exists)
  participants  author; joined sessions;       session_joins (exists)
                grantees                       investigation_grants (new, C2)
  frame         question, window               (exists)
  ordered events                               maestro_investigation_events
    owner_input   authority owner_input_client_attested   (new, C2)
    semantic      authority producer_claim                (exists)
    control       authority advisory                      (exists)
  evidence                                     maestro_investigation_evidence + receipts (exists)
  projections
    Storyboard  draft watermark + publish watermark   storyboard_projections (C3)
  scoped external access                       grant token (C2)
```

Roles:

| Role | Writes | Reads | How it is identified |
|---|---|---|---|
| Worker (author session) | owner_input (hook only), semantic (checkpoint), control `acknowledged`, frame, evidence, Storyboard | all classes | the author principal |
| Member (human or agent in the org) | control cue/question/challenge | semantic + control | `user:` / `api_key:` principal |
| Supervisor (external agent) | control cue/question/challenge | what the grant's scopes allow, owner_input included only if granted | `grantee:<grant_id>`, never the author |
| Projector | Storyboard draft content only, plus projection tables | semantic + control (control marked untrusted), **never** owner_input | in-process maestro, no principal |
| Public share reader | — | published Storyboard, redacted | none |

The Storyboard is the human view of the Investigation. The projector and the
supervisor are both consumers of the Investigation. The worker both produces
for it and consumes from it. None of these is a product of its own.

## 2. Findings

### 2.1 Duplicated APIs and abstractions

| # | Duplication | Where | v1 resolution |
|---|---|---|---|
| D1 | Event type lists hand-copied in 8+ places | SQL CHECKs (3× per migration), `investigation-events.ts` repo constants, route zod unions, `ui-pages/.../investigation-api.ts:127-144`, `shared/.../projection.ts` `MATERIAL_EVENT_TYPES`, Go `investigation_events.go` `postEventTypes` + jsonschema strings, plugin `investigation_events.py` (≥6 lists), CLI `choices=` | One **event registry** (§3). Every list is derived from it or checked against it by a contract test. |
| D2 | Authority decided by complement (`!== "producer_claim"`) | projector `context.ts:94`, `author.ts:430` | Read authority and class from the registry. Fixed for good by InvestigationReader (§2.3). |
| D3 | Projector runs its own SQL against the event table, with a literal `'producer_claim'` | `storyboard-projections.ts` `materialHead`, `candidates` | Move into InvestigationReader (`materialHead(viewer)`). |
| D4 | Projector re-implements author liveness in SQL, ignoring `principal_user_id` | `storyboard-projections.ts:475-493` `authorLive` | Reuse `storyboard/principal.ts`. Grant liveness (`storyboardTokenContext`) uses the same function. |
| D5 | `walCitedReceipts` copies `evidenceOf` and doesn't filter by authority | projector `:249-260` vs repo `:399` | `reader.claimEvidence()`, producer_claim rows only. |
| D6 | Model-facing event shaping copies `eventWire` | `author.ts:662-687` vs route `eventWire` | One wire shape from the reader. The projector only adds its untrusted fence. |
| D7 | Read visibility in four places: `includeIntent` boolean (repo), `mayReadIntent` (route), class→types mapping (route), `get()` with no filter | task-intent `investigation-events.ts:931,986`, routes | One viewer-scoped reader (§2.3). No caller-supplied boolean. |
| D8 | `watch read / challenge / show` CLI copies `events / post / show` | plugin task-intent CLI `:1178,1211`; `_watcher()` check duplicated at CLI `:1012` and core `:1023` | Delete `watch`. The existing commands accept a grant token from the environment. |
| D9 | Ad hoc capability flags in the ensure response (`storyboard_projection.enabled`). Plugin intent capture has none, so on today's prod every prompt queues on disk until prune. | #2073 `investigations.ts:450`; plugin task-intent `:1609-1716` | One `capabilities` block on ensure. The plugin does nothing for a capability that isn't advertised. |
| D10 | UI re-declares the shared projection state lists | #2072 `projection.ts:43-44` | Import from shared. |
| D11 | Fourth copy of `canonical()` | projection `state.ts:47-58` | Use `lib/canonical-json.ts`, plus a flag to drop `undefined` if needed. |
| D12 | Six plugin read paths and sixteen POST call sites; checkpoints have no outbox while intent has one | plugin core | Keep one transport (`investigation_state_sync._post`), which already supports both credentials. Only owner_input needs an outbox, because it is the only producer the agent can't retry. This asymmetry is deliberate and documented. |

Pre-existing and **out of scope**: the plugin's other HTTP helpers
(`storyboard_discovery._post`, `fetch_storyboard`, `evidence_promote`), the
mismatched principal string formats (`api_key:` / `apikey:` /
`service_account:api_key:`), and the three meanings of "question" (frame
question, `question.added`, `question.opened`). These are recorded so nobody
mistakes them for consolidation work.

### 2.2 One canonical Investigation event API

**Registry.** A single module, `packages/shared/src/investigation/events.ts`,
lists every type:

```
type → { class: owner_input | semantic | control,
         authority: owner_input_client_attested | producer_claim | advisory,
         writer: author_hook | author | participant,   // participant = member or grantee with `advise`
         write_route: record-owner-input | checkpoint-investigation | append-investigation-event,
         material: bool,                             // re-projection trigger (replaces MATERIAL_EVENT_TYPES)
         payload: zod schema }
```

- **Derived from it:** every TS constant (`SEMANTIC_EVENT_TYPES`, the advisory
  list, the material list), `authorityOf`, the class→types read filter, and
  the UI lists. `ADVISORY_EVENT_TYPES` (three types) is renamed
  `PARTICIPANT_CONTROL_TYPES` so "advisory" stops meaning two different sets.
  The authority stays `advisory` for all four control types.
- **Checked against it:** a generated, checked-in JSON export of the
  registry. A Go test asserts the gateway enums and descriptions match it. An
  integration test asserts the `pg_constraint` CHECK definitions match it.
  SQL stays hand-written; drift fails CI.
- **Writes stay on three routes, one per authority.** That is the authority
  boundary, not duplication: owner_input can never share the generic append
  route.
- **Reads go through one route**, `read-investigation-events`, with a class
  filter. Every event on the wire carries `class` and `authority`. Clients
  (UI, plugin renderer, projector) branch on those fields and stop keeping
  their own type lists for reading. The plugin keeps lists for writing only.

### 2.3 InvestigationReader (projector access belongs here)

`InvestigationReader.for(viewer)` takes a viewer of `author`, `member`,
`grantee(scopes)` or `projector`. It is the only path that reads
`maestro_investigation_events`.

| Responsibility | Moves from |
|---|---|
| Class visibility: owner_input only for `author` and `grantee` with `owner_input:read`. Projector gets semantic + control. | `includeIntent`, `mayReadIntent` (task-intent) |
| **Known-type allowlist**: rows whose type is not in this build's registry are dropped, and unknown producer kinds render as `unknown` | new. Makes rolling deploys and rollbacks safe (§2.7). |
| Trust marking: `authority === "advisory"` ⇒ `trust: untrusted` | projector `context.ts:94`, `author.ts:430` |
| Paging through an observed head (`readThroughHead`) | projector `:227-246` |
| `materialHead`, the latest material seq per investigation | projection repo SQL |
| `claimEvidence()` (receipts cited by producer_claim rows, with seq) | `walCitedReceipts` |
| Owner-input summary `{first_seq, latest_seq, count}` | task-intent `intentSummary` |
| `get(seq)` behind the same visibility | repo `get()` (unfiltered today) |

What stays in the projector: its prompt, the untrusted fence text, budgets,
leases, watermark CAS, and the stand-down rule for hand-authored drafts.

### 2.4 CardinalWatch → generic scoped Investigation access

| Experiment | v1 |
|---|---|
| `CardinalWatch` scheme, aud `cardinal-watch`, scope `investigation:watch` | `CardinalInvestigation` scheme, aud `cardinal-investigation`, `scopes ⊆ {read, owner_input:read, advise}` |
| principal `watcher:<jti>`, kind `watcher`, `producer_delegated_by` | principal `grantee:<grant_id>`, kind `grantee`, `producer_granted_by` |
| route `mint-investigation-watch-token` | `grant-investigation-access` (author-only), plus `list-investigation-grants` and `revoke-investigation-grant` (decision Q1) |
| stateless; revocation only by revoking the minting key (which also kills the plugin's own credential) | `maestro_investigation_grants(id, investigation_id, org_id, scopes, granted_by, expires_at, revoked_at)`, checked on each request alongside the existing liveness check; grantees show in `get-investigation.principals` (fixes the UI labelling watchers as API keys, `InvestigationPage.tsx:155`) |
| one fixed route set (`WATCH_ROUTE`) | scope → route table derived from the registry: `read` → read-events (semantic + control) + get-investigation; `owner_input:read` → owner_input class; `advise` → append with participant control types |
| grantee can set `session_id` on a cue to the worker's session | server sets `producer_session_id = NULL` for grantee rows; `to_session_id` is still allowed |
| plugin `watch-token` / `watch read\|challenge\|show`, `CARDINAL_WATCH_*` | `cardinal-storyboard investigation grant [--scope …] [--ttl 4h]`; `events` / `post` / `show` use `CARDINAL_INVESTIGATION_TOKEN` when set |

Out of scope: delegated authority (no tool veto, no ack on the author's
behalf), grant chaining, per-grantee rate plans.

### 2.5 Experimental terminology that does not survive

| Remove | Replace with | Notes |
|---|---|---|
| task intent, `task_intent.created` / `.amended`, class `intent`, `INTENT_*`, `record-task-intent`, `intent_turn`, `CARDINAL_TASK_INTENT` | owner input, single type `owner_input.recorded`, class `owner_input`, `record-owner-input`, `owner_input_turn`, `CARDINAL_OWNER_INPUT=0` | **The created/amended state machine goes away.** The server never enforced it (amended without created is accepted, and several created are allowed). "First" is the lowest turn; the summary returns `first_seq`. |
| authority `owner_prompt_client_attested` | `owner_input_client_attested` | Keeps the honest "client attested" claim and matches the class name. |
| watch, watcher, watch token, CardinalWatch, "Investigation watcher" | grant, grantee, access grant | "Supervisor" stays a **role** in docs, never a principal kind or identifier. |
| "recorded **verbatim**" (SessionStart line) | "recorded (credentials scrubbed, up to 32 KiB)" | The current string is false. |
| semantic WAL, "Step 4" | semantic events / checkpoint | WAL stays an internal implementation word. |
| projection overloads: `projected_through_event_seq` (publish) vs `projected_through_event_seq` (draft table); "managed", "material", "stood down", "nudge" | wire names `published_through_seq` and `draft_through_seq`; projection state vocabulary frozen in shared (`none, building, current, behind, updating, paused` + paused reasons) | Internal identifiers can keep their jargon; wire and UI strings use the frozen set. The main column is in prod and is aliased on the wire only, not renamed. |
| `supervision-experiment.md` phases 2–4 | parked | Not v1. |

### 2.6 Migrations

| Migration | State | Conflict / dependency |
|---|---|---|
| `20261007…11090000` | prod | v1 base. Unchanged. |
| `20261012090000_maestro_storyboard_projections` | #2073, never deployed | Needs 07 (composite key), 09 (origins), 10 (`authority`, publish watermark). |
| `20261013090000_maestro_storyboard_projection_limits` | #2073, never deployed | Alters 12. No down guard. |
| `20261014090000_maestro_investigation_intent` | local only | Alters event CHECKs; independent of 12/13. |

**Hazard:** golang-migrate v4.19.1 only moves forward. If any database
reaches 14 before 12/13, it skips them silently. The task-intent E2E database
already has. Also, #2072 rebased naively onto a squash-merged #2073 can drop
13 entirely (stale base).

**Resolution.** Retire all three timestamps and use fresh ones that no
database has seen, so a local database carrying the old versions fails
loudly (table exists) instead of silently skipping:

- `20261015090000_maestro_investigation_owner_input_grants` (C2): owner_input
  type, authority, shape CHECK, one-per-turn unique index, principal kind
  `grantee`, `producer_granted_by`, CHECKs (grantee ⇒ participant control
  types; granted_by ⇔ grantee; grantee ⇒ `producer_session_id IS NULL`), and
  the `maestro_investigation_grants` table. Down refuses while owner_input or
  grantee rows exist.
- `20261016090000_maestro_storyboard_projections` (C3): 12 and 13 squashed
  into one. Down refuses while a lease is live (fix the stale "10 min" comment
  to match the 20 min lease).
- Before C2 merges, confirm prod `schema_migrations.version = 20261011090000`
  and that no PR-image deploy touched prod. Local databases: `migrate force`
  back to 11, or rebuild.

### 2.7 Security and authority invariants (each needs a named test in its PR)

1. **Append-only, ordered.** The immutability trigger is unchanged; seq is
   assigned under `FOR UPDATE` on the investigation row; gapless and commit
   ordered.
2. **Server-stamped provenance.** `producer_*` always comes from the
   authenticated caller. Client-supplied `session_id` / `client` are shown as
   *claimed*. Type→authority is pinned by a DB CHECK, not by route code.
3. **Author-only mutation.** Checkpoint, ack, record-owner-input, grant /
   revoke, set-question, set-frame, put-state, investigation evidence upload,
   and storyboard writes on `session` / `created` investigations
   (`guardInvestigationWrite`).
4. **Grantee cannot impersonate the worker or author.** `principal.ts`
   returns null for grant requests; `is_investigation_author` is always false;
   a DB CHECK limits grantee rows to participant control types; no
   `session_id`; no ack, checkpoint, owner_input, grant, state, evidence or
   storyboard tool.
5. **Projector cannot resolve challenges.** It runs in-process with no
   principal. Its dependencies are typed read-only for events
   (`InvestigationReader`). It writes only draft content of author-locked
   storyboards plus projection tables; no event, ack, publish or share. A test
   asserts its dependency surface.
6. **Projector concurrency and watermarks.** Per-storyboard lease; storyboard
   row lock then revision/act CAS, then watermark CAS
   (`seq IS NULL OR seq < N` under a live lease), all in one transaction. The
   publish watermark is the observed head of the managed act. The projector
   stands down on hand-authored content.
7. **Automatic projection stays flag-gated.** `STORYBOARD_PROJECTION_ENABLED`
   defaults off. When off, no timer, no nudge, `draft: null`;
   `request_projection` answers 409 `projection_disabled`; the capability
   says `enabled: false`.
8. **Owner input privacy.**
   - There is no agent-facing write path: no MCP tool and no CLI command.
   - Readable only by the author and grantees holding `owner_input:read`.
   - Never projected, never evidence: the `control_log_not_evidence`
     patterns cover the new route and CLI.
   - Never public; grant ids and `granted_by` are added to the redactor's
     identifiers.
   - Scrubbed, at most 32 KiB. `prompt_sha256` is the hash of the
     **scrubbed**, untruncated text, never the original: a hash of the
     original would let any reader brute-force a redacted secret offline
     (amended 2026-10-07 after the P2 privacy review). One row per
     (investigation, session, turn); first write wins; a different hash
     returns 409.
   - **Disclosure before capture**: the user sees a notice (a `systemMessage`,
     not only model context) before the first prompt of a session is
     recorded, including when capture turns on mid-session.
   - Known, accepted: a prompt that another parallel `UserPromptSubmit` hook
     blocks may still be recorded, and machine-generated prompts (`/loop`,
     scheduled runs, `claude -p`) are recorded as `user_prompt`.
   - No OTEL prompt logging, and `git-state` stays "never args".
   - The local outbox is 0600 and bounded, and is used only for transient
     errors when the server **advertises** the capability. An absent
     capability or a 404 means drop, never queue.
9. **Evidence restrictions.** Tiers witnessed / captured / reported. Uploads
   are captured or reported only. `control_log_not_evidence` and the public
   redactor stay. The sweep pins producer_claim-cited receipts; checkpoints
   hold `FOR KEY SHARE`.
10. **Transcript independence.** Every consumer (supervisor, projector,
    takeover agent) works from Investigation state alone. No route or tool
    requires or accepts a transcript.
11. **Safe rollout and rollback.** C1 ships the known-type allowlist
    *before* any new type exists. During a C2 rollout, and after a code
    rollback to C1, old pods hide owner_input rows instead of leaking them
    (today's main would leak them to every member). The owner-input server
    flag (`INVESTIGATION_OWNER_INPUT_ENABLED`, default off) controls both
    acceptance and the capability.

Accepted for v1, documented and not fixed: `head_seq` and seq gaps show
members *when* the owner typed, not *what*. Fixing that needs per-class
sequences.

### 2.8 Supersede vs merge

| PR / branch | Verdict | Reason |
|---|---|---|
| conductor #2075 | **Merge as-is**, before C3 | Independent prefab fix; the projector inherits its validation. |
| conductor #2073 | **Supersede** by C3 (keep 90b67874 as C0) | Its reads must go through the reader (D2–D6), and its migrations need renumbering and squashing. |
| conductor #2072 | **Supersede** by C4 | Stale base (lacks the review fixes, the 429 / daily-cap semantics, and the publish-gate change); re-cut on C3. |
| conductor `feat/task-intent` | **Supersede** by C2 | Renames, single type, grants table, grantee session-id rule, registry-based reads. |
| plugins #172 | **Supersede** by P1 | Content survives; it moves to the `capabilities` block. |
| plugins `feat/task-intent` | **Supersede** by P2 | Renames; drop `watch`; capability-gated capture; no queueing on 404. Keep NUL stripping and turn renumbering past the server's highest turn. |
| plugins #173 | **Close**, replaced by this doc (D0) | Phases 2–4 are parked. |

Supersede means close with a link to the replacement and cut a fresh branch
from main. Commits are cherry-picked and then rewritten; no history is kept.
The "nothing to change on the server" conclusion from the integration pass
does not hold up. This audit found four server issues: grantee `session_id`
spoofing, no per-token revocation, an unfiltered repo `get()`, and old-pod
leakage during rollout.

## 3. Investigation v1 architecture

**Server (maestro)**

```
packages/shared/src/investigation/
  events.ts           registry: type → class/authority/writer/route/material/schema
  events.json         generated export (Go + SQL contract tests read it)
  wire.ts             EventWire {seq, type, class, authority, trust, producer, payload}
                      ProjectionWire (moved from storyboard/projection.ts)
packages/maestro/src/investigation/
  access.ts           resolve caller → Viewer {author | member | grantee(scopes) | projector}
                      (wraps storyboard/principal.ts + grant verification + liveness)
  reader.ts           InvestigationReader.for(viewer): read, readThroughHead, get,
                      materialHead, claimEvidence, ownerInputSummary
  log.ts              writers, one per authority: appendControl, checkpoint (semantic),
                      recordOwnerInput — all seq assignment and caps live here
  grants.ts           grant / list / revoke / verify (token signer reused from scoped-tokens.ts)
routes/investigations.ts, investigation-events.ts, investigation-evidence.ts
                      thin: zod, then access → reader/log
services/storyboard-projector.ts
                      consumer: InvestigationReader.for(projector) + storyboard repo
```

**Plugin (cardinal_core + claude adapter)**

```
investigation_state_sync._post   single transport; API key or CardinalInvestigation token
investigation_bootstrap          ensure → binding incl. capabilities{projection, owner_input}
producers   checkpoint (semantic) · owner-input hook (owner_input, capability-gated, outbox)
            · ack/post (control)
consumers   poller → inbox → delivery (render by wire class/authority) · `investigation events`
CLI         investigation link|question|checkpoint|events|post|ack|show|grant|state …
```

## 4. PR and migration sequence (approved: deploy dark, then enable)

Each step leaves prod working. "Deploy" means merge → tag → kubernetes-clusters
bump → fully rolled out, with CI green, review done and a prod smoke test.
Every capability ships **off** or inert. Investigation v1 is fully deployed
before any flag is flipped, and each flip is a separate, explicit owner
decision.

| # | Repo · branch | Contents | Migration | Gate before the next step |
|---|---|---|---|---|
| D0 | plugins · `docs/investigation-v1` | This doc replaces #173 | — | — |
| C0 | conductor · `fix/bedrock-claude5-temperature` | 90b67874 from #2073 on its own; conductor #2075 merged as-is alongside it | — | CI |
| C1 | conductor · `refactor/investigation-reader` | Registry + JSON export + Go/SQL contract tests; `InvestigationReader` and `access.ts` for the existing two classes; wire `class` / `authority` / `trust`; **known-type allowlist**; `capabilities` block on ensure (empty-safe); UI reads `class` from the wire. No behaviour change. | none | deployed; prod smoke: read/post/checkpoint/ack/ensure unchanged |
| P1 | plugins · `feat/investigation-capabilities` (0.44.0) | #172 content on `capabilities.projection`; render_preview env fix; render by wire class; CLI `choices` from core constants. Merging auto-releases. | — | released; C1 deployed first |
| C2 | conductor · `feat/investigation-owner-input` | Owner input (`record-owner-input`, `owner_input.recorded`), grants (grant / list / revoke, `CardinalInvestigation`, `grantee:<id>`), reader visibility, grantee `session_id` rule, redactor identifiers, `control_log_not_evidence` patterns, gateway enum, UI; flag `INVESTIGATION_OWNER_INPUT_ENABLED` **off** → `capabilities.owner_input` | `20261015090000_maestro_investigation_owner_input_grants` | deployed dark; prod `schema_migrations` = 20261015 |
| P2 | plugins · `feat/investigation-owner-input` (0.45.0) | Owner-input hook (capability-gated; drop on 404 / absent; outbox only for transient errors), NUL strip, turn renumbering, `grant` CLI, token-aware `events` / `post` / `show`, renamed strings | — | released; inert against the dark server |
| C3 | conductor · `feat/storyboard-projection-v1` | #2073 re-cut onto C1/C2: projector reads only through `InvestigationReader.for(projector)`; D3–D6 and D11 removed; `authorLive` → access.ts; capability `projection`; flag `STORYBOARD_PROJECTION_ENABLED` **off** | `20261016090000_maestro_storyboard_projections` (12+13 squashed) | deployed dark; a test proves the projector never sees owner_input rows (real rows present) |
| C4 | conductor · `feat/storyboard-projection-ui` | #2072 rebuilt on C3: imports shared states; handles the 429 daily cap, 409 `projection_disabled`, and the new publish gate | — | deployed |

**Milestone: Investigation v1 deployed dark.** After it, each step is its own owner decision:

1. Enable owner_input for the Cardinal org.
2. Run the real supervision experiment.
3. Enable projection for the Cardinal org.
4. Run a combined dogfood of Investigation v1 as a whole: a real engineering
   task, with owner input captured, checkpoints, the Storyboard evolving
   automatically, and a grantee observing, challenging, and the worker
   responding. The next primitive (likely Activity) is chosen from that run,
   not before.

C2 and C3 have no schema dependency. C2 goes first so the projector is built
and tested against a reader that already holds real owner_input rows.

## 5. Not doing

Activity capture, the WAL→state reducer, compliance tracking, delegated
supervisor authority (no tool veto, no ack on the author's behalf), new
projector features, Phase 4 delivery fixes, idle wake, Codex / Cursor / Gemini
adapters, flipping `STORYBOARD_PROJECTION_ENABLED`, and fixing the
out-of-scope duplications listed in §2.1.

## 6. Decisions (approved 2026-10-07)

1. **Stored grants table**: per-grant revocation, explicit scopes, durable
   participant identity, `grantee:<id>`. Grantees can advise but can never
   impersonate the worker, acknowledge for it, checkpoint, or change its state.
2. **A single `owner_input.recorded` type**: created/amended is gone. Owner
   inputs are an append-only sequence of client-attested records; there is no
   task-contract state machine.
3. **Names, exactly as proposed**: `owner_input`, `owner_input_client_attested`,
   `grant`, `grantee`, `CardinalInvestigation`, `CARDINAL_INVESTIGATION_TOKEN`.
   CardinalWatch is retired. "Supervisor" is a role and never appears in
   storage or authentication primitives.
4. **Server flag default off**: deploy the substrate, then the capability-aware
   plugin, then enable capture explicitly.
5. **Timing side channel accepted for v1** and documented (§2.7).

Do not salvage the 12/13/14 migrations: use fresh 15/16, and rebuild or force
local databases.

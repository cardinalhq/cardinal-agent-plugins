# Supervision verdicts: a challenge is done when the supervisor verified it

Status: DESIGN, revision 5, 2026-10-18. Answers items 1 and 3 of the supervision UX
feedback of 2026-10-18 (a Codex supervisor of a multi-language Claude worker), and the part
of item 2 that the Investigation record can carry. Items 4–6 are listed under Not in this
spec. Revisions 2–4 resolve the blockers of three independent reviews (Review record).

Spans both repos, like `supervise-stepback-trigger.md`: conductor (registry, migration,
routes, status derivation, timeline UI) and this repo (CLI, worker delivery, skill).

## Problem

The delegated-supervision lifecycle (conductor `docs/specs/investigation-delegated-
supervision.md`, `challengeStatuses`; plugin mirror `core/cardinal_core/supervision_status.py`)
ends at `addressed`: *the worker* checkpointed a semantic event whose refs cite `#<challenge>`.
That is the worker's claim that it fixed something. Nothing on the record says:

1. **whether the supervisor checked the correction.** Today the supervisor posts a cue whose
   text starts `Review: upheld — …`. No code reads it; the status never changes; the owner
   cannot tell "worker says fixed" from "supervisor confirmed fixed". With several accepted
   challenges open at once, the supervisor keeps that join in a private handoff file.
2. **what revision anything was checked at.** Checkpoint `artifacts` bind file hashes and
   `gate` records a pass/fail by name. Neither names a commit or PR. A test passing on
   commit A reads the same as approval of a PR whose head is now B.
3. **how far a verdict reaches.** "Fixture replay passes" and "deployed and observed" are
   both a cue reading `upheld`. The feedback's example: a checkpoint said replay
   determinism was resolved; the actual correction removed one finding from a golden test
   whose ordering varied. A narrow fix was recorded as a broad guarantee.

## Design in one picture

```
 supervisor          worker                        supervisor
 challenge #c ──► ack accepted ──► checkpoint (refs ["#c"],     ──► verification.recorded
                  (note: plan)      revision {repo, commit, pr})     {verifies: c, correction: k,
                                    = correction #k                   verdict, scope, subject,
                                                                       evidence, text}
 status:  open      accepted          addressed                      verified | rejected |
                                     ("awaiting verification")        unverifiable
                                                                       │
                a later correction #k2 citing #c, carrying revision or artifacts ──┘
                → addressed again; the verdicts on #k stay, shown as superseded
```

Three additions, nothing removed:

- **`revision` on checkpoints**: a structured field beside `artifacts` / `gate`, so a
  correction says which commit it is.
- **`verification.recorded`**: the supervisor's explicit, scoped, revision-bound verdict on
  a correction (or on a claim). It is the only way a challenge reaches `verified`.
- **Status derivation, timeline and CLI** that join challenge → position → ruling →
  corrections → verdicts in one place, with staleness.

Plus one supporting change: **grant succession** while the old grant is still live (§4).

## 1. `revision` on checkpoints

Optional structured field on **every** semantic event type (a correction may be
checkpointed as any of them), gated by a new capability `checkpoint_revision` (missing =
disabled; the CLI strips with a warning, as it strips artifacts today):

```
revision: { repo: "<host>/<owner>/<name>",    // REPO_RE (lowercase), as session repo provenance
            commit: "<40 lowercase hex>",
            pr?: <int 1..10^9>,
            dirty?: true,                     // working tree had uncommitted changes
            tree?: "<40 lowercase hex>" }     // only with dirty: git tree id of the full working state
```

`tree` binds a dirty snapshot: two dirty trees at the same HEAD with different changes have
different `tree` ids. `"auto"` computes it whenever the tree is dirty, without touching the
real index: copy the real index (`git rev-parse --git-path index`) to a private temp file (this
keeps sparse-checkout / skip-worktree bits and the stat cache), then with
`GIT_INDEX_FILE=<temp>`: `git add -A` (tracked and untracked, ignored files excluded),
`git write-tree`; the temp index is deleted (the blobs it wrote stay in the object store,
harmless). The id is content-addressed after git's clean filters, so it is deterministic for
the same working state **and git config**; with `core.fileMode=false` mode changes are not
captured.

`tree` is **omitted** (the revision is then snapshot-unbound) when: a submodule is dirty
(`git status --porcelain=v2` reports it; `git add -A` records only the submodule's HEAD), or
`git add` fails (a missing required filter such as git-lfs). The checkpoint still succeeds.
A dirty revision **without** `tree` (these cases, an explicit object, or the MCP path) is
**snapshot-unbound**: shown as such, never as an exact revision.

- `cardinal-storyboard investigation checkpoint` takes `"revision": "auto"` in an entry:
  the CLI resolves `git rev-parse HEAD`, the `origin` remote normalised and lowercased to
  `REPO_RE`, the open PR for the branch when `gh` is available and authenticated (absent
  otherwise, never guessed), and `dirty: true` when `git status --porcelain` is non-empty. It
  records what is true; it never refuses a dirty tree. `auto` outside a git worktree is a
  usage error.
- **Resolve once, retry the resolved batch.** `auto` makes the batch non-deterministic
  (`gh` reachability, a new commit), and the default idempotency key hashes the normalised
  events (`investigation_events.py` `default_idempotency_key`). So the CLI resolves `auto`
  once, writes the resolved batch and its key to a private checkpoint outbox keyed by the
  sha256 of the *unresolved* input and session, and sends from it. A retry of the same input
  reuses the outbox: same body, same key, server `duplicate`. The outbox is removed on a
  definite answer (201/200/4xx), kept on an uncertain one, like post outboxes. When a kept
  outbox is replayed, the CLI says so and prints the commit it carries ("replayed an earlier
  send at abc1234; HEAD is now def5678: checkpoint again for the new commit").
- `"auto"` is CLI-only. The MCP `storyboard__checkpoint` path takes only an explicit object;
  worker guidance names the CLI for corrections.
- An explicit object is validated (shape only; the server does not contact the forge).
- Worker guidance (`ACK_GUIDANCE` in `investigation_events.py`): "When a checkpoint reports a
  correction, add `"revision": "auto"` for code or `artifacts` for anything else, so a later
  summary citing the challenge is not mistaken for it." One sentence; no new section. (An
  unjudged correction with neither is displaced by any later citing event, by design: nothing
  distinguishes it from a summary.)

`dirty: true` is shown as "commit abc1234 + uncommitted changes": the supervisor can still
verify, but the verdict's subject is then its own revision (below), not the worker's.

## 2. `verification.recorded`

Registry (`packages/shared/src/investigation/events.json`):

| field | value |
|---|---|
| class | `control` (delivered to the worker; it reads the worker's own investigation) |
| authority | new `supervisor_attestation`: the supervisor's claim; not binding, not owner authority |
| writer | `supervisor` (a grantee holding `supervise`) |
| route | new `record-verification` (`Authorization: CardinalInvestigation <grant>`) |
| material | true |

Written in the **worker's** investigation, beside the challenge it closes, not in the
supervisor's own investigation where `supervision`-class bookkeeping lives: the owner reads
the lifecycle on the worker's timeline, and the status derivation reads one stream.
`to_session_id` is fixed by the server: the challenge's addressee for a challenge target, the
target event's producer session for a claim target.

Payload (strict):

```
{ verifies:   <seq>,                 // a challenge.added, or a semantic claim event, of this investigation
  correction?: <seq>,                // required when verifies is a challenge (rules below)
  verdict:    "verified" | "rejected" | "unverifiable",
  scope:      "code_review" | "test_run" | "fixture_validation"
            | "merge_readiness" | "deployment" | "rollout_observed",
  supersedes_verdict?: <seq>,        // this lineage's own earlier verdict it replaces (below)
  subject:    { repo?, commit?, pr?, dirty?, tree?,   // same shape as revision
                artifacts?: [{path, sha256}] ≤20,     // same validation as checkpoint artifacts
                target?: 1..200 chars,                // e.g. "k8s prod-us maestro/deploy/maestro gen 41"
                observed_at?: RFC 3339 },
  text:       1..2000 chars,         // what was checked and the deciding result
  evidence:   [ev_…|rcpt_…] 0..20,   // ≥1 unless verdict is unverifiable
  refs?:      ≤10 }
```

Scope ⇒ subject rules (400 `invalid_verification`), so a verdict cannot be wider than what it
names:

- `code_review`, `test_run`, `fixture_validation`: `repo` + `commit`, and `tree` whenever
  `dirty` (the supervisor's own snapshot is always bound; 400 `dirty_subject_requires_tree`).
- `merge_readiness`: `repo` + `commit` + `pr`, and **never `dirty`** (400
  `merge_readiness_requires_clean`): the checked subject must be the PR head itself. Means "this
  PR at this head is mergeable as far as the challenge goes", not "merged".
- `deployment`, `rollout_observed`: `target` + `observed_at`. Means "this was running / rolled
  out when observed", not "healthy forever".

Scopes are an ordered ladder only in the UI's wording; the server never infers a wider scope
from a narrower one. A correction may get verdicts in several scopes; how they combine is §3.

Evidence from a supervisor on another machine (`ev_…` in its own spool) is readable by no one
else until uploaded; grantee upload is the companion attachments spec. Until then the
verdict's `text` must state the deciding result, as cue verdicts do today.

### Rules (each its own 4xx, checked under the investigation's append lock)

- Caller is a grantee holding `supervise` (`token_scope_mismatch` / `verification_requires_supervise_grant`, 403).
  `grants.ts` `GRANT_ROUTE` / `GRANT_ROUTE_SCOPE` gain `record-verification` (a widening of the
  grant route table: security review). Challenges posted through the MCP gateway or by a user
  principal have no grantee poster and **cannot be verified** by any grant (the same rule as
  `issue-ruling`); the skill says to post challenges under a supervise grant, and to re-post one
  that needs verification.
- **Challenge target** (`verifies` is a `challenge.added`):
  - same grant-ownership rule as `issue-ruling`: the grant that posted the challenge, its
    successor lineage (§4), or an `owner_web` grant. Else 422
    `verification_target_not_own_challenge`.
  - `correction` must equal the challenge's **current correction** `k = correctionOf(c)`
    (§3; one pure function in `@cardinalhq/shared` and its Python mirror, used by the route,
    the UI and the plugin). No citing event → 422 `correction_not_found`; a different citing
    event → 409 `correction_not_current` with the current `k`. There is no verdict without a
    named correction, no verdict the derivation would not count, and no path to `verified`
    except this route.
  - status must be one of `addressed`, `directive_addressed`, `verified`, `rejected`,
    `directive_rejected`, `unverifiable` (a correction exists). Any other → 409
    `verification_requires_correction` (`overruled` / `withdrawn`: the challenge is closed).
- **Claim target** (`verifies` is a semantic event): `correction` must be absent; `rejected` is
  refused (422 `refute_with_challenge`: disagreement with a claim is a challenge, which needs
  the worker's position). A claim target that is the current correction of a challenge whose
  status accepts verdicts is refused (422 `verify_the_challenge`, naming the challenge seq): a
  verdict on a correction goes through its challenge, never around it. Claims that merely
  mention a challenge stay verifiable, and a `supersedes_verdict` revising an existing verdict on
  the claim is exempt (a mistaken claim verdict stays retractable). This replaces the `Review: upheld` / `Review:
  unverifiable` cue convention with a structured record.
- **Uniqueness of active verdicts**, enforced by a guard under the append lock (409
  `verification_exists` with its seq). A verdict is *active* until superseded.
  - challenge targets: one active verdict per (`verifies`, `correction`, `scope`) **across all
    grants**. The ownership rule already limits who can write, so no grant can pre-empt
    another, and an `owner_web` grant cannot replace another grant's verdict; the owner
    overrules with `owner.decided` (existing).
  - claim targets: one active verdict per (`lineage_root_id`, `verifies`, `scope`). Claims have
    no ownership rule, so a worker's self-minted grant must not be able to occupy a scope and
    block the real supervisor's verdict. The claim card lists every lineage's verdicts with
    its grant label.
- **A reviewer may revise its own verdict** without new worker activity (new review evidence is
  not a new implementation: an `unverifiable` code review becomes checkable when access
  returns; a mistaken `verified` must be retractable). `supersedes_verdict` names an active
  verdict with the same `verifies`, `correction` and `scope`, recorded by the **same grant
  lineage** (else 422 `supersede_not_own_verdict`; already superseded → 409
  `verdict_already_superseded`). The new verdict becomes active; the old one stays in history
  marked "revised by #n". At most 3 revisions per **chain** (the `supersedes_verdict` links from
  one root verdict, which belong to one lineage; 409 `verdict_revision_limit`), so a verdict
  cannot flap indefinitely and one lineage's revisions never count against another's. A retried
  supersede from a renewed grant that gets 409 `verdict_already_superseded` is treated by the
  CLI as success when the verdict that superseded it has identical content. No lineage can supersede
  another's verdict, so a rejection from one lineage stays active unless that lineage revises
  it.
  The CLI treats a 409 whose stored verdict has identical content as success (a retry from a
  renewed grant is a new principal) and prints the stored verdict's grant, so a verdict another
  lineage recorded is never reported as this grant's.
- At most 2 000 verifications per investigation (`event_limit_reached`, scope `verifications`).
- Idempotency exactly as `issue-ruling` (`idempotency_key`; retry answered before any rule).
- Cost: the status check reads the challenge's acks, ruling, citing semantic events and
  verdicts. Measure on the largest investigation first; if a GIN index on semantic
  `payload->'refs'` is needed, it is its own `CREATE INDEX CONCURRENTLY` migration, outside the
  transactional `lock_timeout` migrations, so it never blocks writes.

### Acks of verdicts

The ack route accepts `verification.recorded` targets: `noted` only, note optional, for every
verdict. A `rejected` verdict takes no position: the worker's position on the challenge is
final per session, as today. `accepted`/`declined`/`conflicts` on a verdict → 422
`verification_takes_no_position`. Acks never change verdict status.

### Residual risk (unchanged in kind, stated plainly)

The worker holds the same author key as the supervisor's minting client. It can mint an
`author_client` supervise grant and record verdicts on *claims* and on challenges *that
grant posted*. It cannot verify a challenge another grant lineage posted, unless it reads that
lineage's live token from the shared filesystem, which already lets it act as that grant
today. The timeline shows every verdict's grant label, `granted_via`, supervisor session, and
whether the grant was later revoked, so a self-verifying worker is visible, not prevented.
Strong separation still needs an isolated supervisor runtime (`semantic-wal-supervision.md`).

## 3. Status derivation

Extend `challengeStatuses` (shared TS) and its Python mirror, pinned by the shared fixture.
The fixture is already `version: 2`; this is **`version: 3`**, and every existing expected
status object gains `correction_seq: null, verification_seq: null` (both tests compare whole
objects). New fields: `correction_seq`, `verification_seq` (the deciding verdict), and
`verdicts` (per-scope list for display; not compared by the status chip).

Evaluated after the existing rules, only when they produce `addressed` or
`directive_addressed`:

- **Corrections: `correctionOf(c)`.** A *citing event* is an author semantic event after the
  worker's current position whose refs contain the challenge: if the challenge has an upheld
  ruling `r`, after the latest author position on `r`, refs containing `#r` or `#c`; otherwise
  after the latest author position on `c`, refs containing `#c`.

  A citing event `e` is **substantive** if it carries `revision` or `artifacts`, or its refs
  contain `#v` for a `rejected` or `unverifiable` verdict **on the current `k`** (the fold's `k`
  when it reaches `e`) that is **active as of `e.seq`** (`v < e.seq` and not superseded by a
  verdict with seq `< e.seq`): it answers that verdict. A ref to a rejection of an earlier,
  already-replaced correction answers nothing, so it cannot reopen a later verified one. A worker citing a rejection its reviewer has already revised to `verified` reopens
  nothing. Refs to a `verified` verdict do not count, so thanking the
  supervisor reopens nothing. The server already refuses refs to later seqs
  (`semantic_ref_not_found`); the fold relies on refs pointing only backwards and checks
  `v < e.seq` anyway, so hand-built test streams behave the same.

  `correctionOf(c)` is a fold over the citing events in seq order, starting with `k = none`:

  ```
  for e in citing events (ascending seq):
      if k is none:                         k = e
      elif judged(k, before=e.seq):         # a verdict on k has seq < e.seq
          if substantive(e):                k = e
      else:                                 # k not yet judged when e arrived
          if substantive(e) or not substantive(k):  k = e
  ```

  So: an unjudged revision-bearing correction is not displaced by a later plain summary (the
  supervisor verifies #12, not the summary #14); a plain correction is displaced by a later
  event (nothing distinguishes them); once judged, only a substantive event supersedes, so a
  summary cannot hide a rejection; and a correction that answered a verdict by ref is itself
  substantive, so a later summary does not displace it either. A verdict on an earlier `k`
  with none on the current `k` is just the "not yet judged" branch. The route's rule (current
  `k` at the moment of the verdict) and the fold agree because the fold only ever looks at
  verdicts with lower seqs than the event it is deciding. The rejection re-surfacing text
  tells the worker how to make a substantive correction (below).
- **Verdicts on `k` combine; the latest does not win.** Let `V` = **active** verdicts (not
  superseded by a `supersedes_verdict`) with `verifies = c` and `correction = k`. Superseded
  verdicts still count as "judged" for `correctionOf` (a review happened), but only a verdict
  active as of the answering event can be answered by `#v` (the substantive rule above).
  - No `V` → keep `addressed` / `directive_addressed` (UI: **Correction submitted · awaiting
    verification**).
  - any `rejected` in `V` → `rejected` (or **`directive_rejected`** on the directive path);
    `verification_seq` = the earliest rejecting verdict. A verified verdict in another scope
    cannot hide it.
  - else any `verified` → `verified`; `verification_seq` = the earliest verified verdict. Other
    scopes' `unverifiable` verdicts stay listed in `verdicts` and on the chip ("Verified
    (code_review @ abc1234) · deployment unverifiable").
  - else (all `unverifiable`) → `unverifiable`.
- A verdict on an earlier correction never counts for `k`: a new correction re-opens
  verification; the old verdicts stay in history marked **superseded by #k**.
- `overruled` still wins over everything; `withdrawn` is unchanged.

Fixture cases include: rejected test_run then verified code_review → `rejected`; verified then
summary citing `#c` without revision → still `verified`; rejected on #12 then summary #14
citing only `#c` → still `rejected`, `correctionOf` = 12; rejected then a citing event with
refs `["#c", "#v"]` and no revision → `addressed`, old verdict superseded; verified then a
correction with revision → `addressed`; directive path rejected → `directive_rejected`;
overruled beats verified; correction #12 with revision then summary #14 (no verdict yet) →
`correctionOf` = 12, a verdict naming 14 is `correction_not_current`; rejected #13 on #12,
answer #15 with `#v`, summary #17 → `correctionOf` = 15, status `addressed`; a checkpoint citing
a verified verdict's `#v` → still `verified`; `unverifiable` then the same lineage's `verified`
with `supersedes_verdict` → `verified`; `rejected` then the same lineage's `verified` superseding
it → `verified`; lineage A `rejected` and lineage B `verified` on different scopes → `rejected`.
The fixture pins `correctionOf` as well as the status. Staleness cases (UI/CLI, not the status
fixture): two dirty revisions at the same HEAD with different `tree` → revision mismatch; dirty
without `tree` → snapshot unbound.

### Worker delivery (plugin)

- `rejected` and `directive_rejected` re-surface like `accepted` (every 5 boundaries, same
  back-off), with the verdict text: "Your correction #k for #c was rejected (#v): <text>.
  Checkpoint a new correction with refs ["#c", "#v"] (and "revision": "auto" when it is
  code)." The worker has no new exit: the supervisor may rule, the owner may overrule.
  `rejected` and `directive_rejected` join Python `RESURFACE` (`supervision_status.py`) and TS
  `OUTSTANDING_CHALLENGE_STATUSES` (`supervision.ts`).
- **Stop.** `directive_rejected` joins `OPEN_DIRECTIVE` in `supervision_status.py`, so a
  rejected directive correction blocks Stop again. When a rejection arrives, the plugin
  removes the ruling's seq from the binding's `stop_blocked` list, so the once-per-directive
  block fires once more for the rejected state.
- `verified` / `unverifiable` are delivered once as information, ackable `noted`.
- `verification.recorded` joins `DELIVERABLE` in `investigation_events.py`.
- **Local tracking** (`track_supervision` and its helpers):
  - keep every `verification.recorded` whose `verifies` is a tracked challenge, slimmed to
    `verifies`, `correction`, `verdict`, `scope`, `supersedes_verdict` and `text` (≤500 chars);
  - `_slim` of tracked semantic events keeps `has_revision` and `has_artifacts` booleans
    beside `semantic_id` and `refs`, so the local `correctionOf` sees what the server's sees;
  - `_challenges_of` maps a verdict to its challenge through `verifies`, not `refs`;
  - `essential_events` keeps what the `correctionOf` fold still needs for each open challenge:
    the current `k`, every verdict on `k`, and every citing event after `k` (slimmed, with
    `has_revision` / `has_artifacts` / refs). Events before `k` cannot change the result, so the
    set stays bounded under the 100-event cap;
  - the "citing a stubbed challenge cannot change a settled status" shortcut no longer holds
    (verified → addressed); keep substantive citing events (revision, artifacts, or answering a
    verdict by `#v`) for challenges whose status is `verified`, `rejected`,
    `directive_rejected` or `unverifiable`;
  - `rejected`, `directive_rejected`, `verified` and `unverifiable` count as open for eviction;
    only `withdrawn` / `overruled` stay final.
- **Old workers.** A worker plugin older than this never delivers `verification.recorded`.
  The supervisor cannot see the worker's plugin version, so the skill says: if a rejected
  correction has no new correction or acknowledgement by your next pass, post a `challenge`
  citing the verdict's seq.

### Staleness (item 3)

Computed, never stored, by the UI and the CLI from the record:

- **superseded**: a later correction with `revision` or `artifacts` exists (above).
- **revision mismatch**: the verdict's `subject.commit` differs from `k`'s `revision.commit`
  for the same repo, **or** either side is dirty and their `tree` ids differ. When exactly one
  side is dirty this always reports a mismatch (the clean side's tree is not on the record, and
  the server does not contact the forge); that is intended: a clean commit and a dirty snapshot
  are different subjects. Shown as
  "verified at def5678; correction claims abc1234" (or "… working tree 1a2b… vs 3c4d…"). Not an
  error: the supervisor may have verified a later head that contains the fix. The owner sees
  both.
- **snapshot unbound**: `k`'s revision **or the verdict's subject** is dirty without `tree`.
  The verdict is shown "verified · snapshot unbound": no exact-revision claim is made. (The
  route makes this impossible on the verdict side for code scopes and `merge_readiness`; the
  check covers both sides anyway, for verdicts recorded before a rule tightened.)
- **grant revoked**: the verdict's grant was revoked after the verdict for a reason other than
  succession (§4 records the reason); shown, not discounted.
- **PR head moved** (CLI only, §5): the live PR head is not the verdict's commit. The server
  does not poll the forge; the timeline does not claim to know the current head.

### Next action (one rule for the timeline and the CLI)

`nextAction(status, staleness)` lives in `@cardinalhq/shared` beside `challengeStatuses`, with a
Python mirror; the timeline's "Next:" line and `challenges`' next column both use it, so the two
surfaces never disagree. Pinned by its own cases in the shared fixture file.

| status | staleness of the deciding verdict | next actor | action |
|---|---|---|---|
| open, directive | — | worker | take a position / comply |
| accepted, directive_accepted, rejected, directive_rejected | — | worker | submit a correction |
| addressed, directive_addressed | — | supervisor | verify correction #k |
| declined | — | supervisor | rule on #c |
| conflicted | — | owner | decide the conflict |
| verified | revision mismatch or snapshot unbound | **supervisor** | re-verify #k at its revision (the recorded verdict stays as history) |
| verified | none | nobody | — |
| unverifiable | any | **owner** | decide whether to accept it unverified; the supervisor may revise the verdict once it can check |
| withdrawn, overruled | — | nobody | — |

A verified verdict at a different commit is a scoped, historical fact, not completion of the
current revision: the next actor is never "nobody" while a mismatch or unbound snapshot stands.
No deployment or merge gate is implied for a challenge whose verdicts are code-review only.

## 4. Grant succession (live predecessor only)

A 4-hour grant outlives few real supervisions; today renewal creates a grant that cannot rule
on, or (with this spec) verify, the old grant's challenges unless it is `owner_web`.
Succession lets the supervisor carry its own authority forward **before** it lapses, and adds
no power beyond holding the live predecessor token:

- `grant-investigation-access` takes optional `succeeds: {grant_id, token}`. The server
  verifies `token` with the normal verifier (`signer.verify`, which rejects expired tokens),
  checks its `gid` equals `grant_id` and the grant row is the same org and investigation, not
  revoked and not expired. Expired → 409 `predecessor_expired`; revoked → 409
  `predecessor_revoked`. After expiry the existing rule applies: only an `owner_web` grant can
  rule on or verify those challenges.
- The new grant's scopes must equal the predecessor's (422 `succession_scope_mismatch`).
- **One successor per grant**: unique index on `succeeds_grant_id`. No forks from a leaked token.
- **The predecessor is revoked** in the same transaction as the successor is minted, with
  `revoked_reason = 'succeeded'` and `succeeded_by = <successor id>` (new nullable columns;
  other revocations record `'revoked'`).
- **Succession is idempotent** (a lost reply must not lock the supervisor out): a request
  whose `succeeds.token` verifies (signature and `exp`) for a predecessor already revoked with
  reason `succeeded` answers `200 {grant, token, duplicate: true}` with a freshly signed token
  for that successor (same `gid`, scopes and `exp`; JWTs are not stored, so re-signing is how
  the existing route returns a token at all), provided the successor is itself unrevoked and
  unexpired and its own scopes and label match the request. Otherwise 409
  `predecessor_succeeded`. The match compares scopes, label and the supervisor
  investigation/session fields; `ttl_seconds` is ignored on a retry. Two concurrent succession
  requests race on the unique index: the loser re-reads and takes this idempotent path, never a
  500. On the succession path the server signs the successor's token **before** committing
  the transaction (today's route revokes a new grant whose mint fails after commit, which here
  would leave both grants revoked). This grants nothing new: the caller holds the predecessor's live
  token, which was that authority a moment ago. The retry window is the predecessor's
  remaining lifetime; after that, recovery is an `owner_web` grant, as today.
- **Lineage** = the chain of `succeeds_grant_id`; its root id is stored on each grant row as
  `lineage_root_id`, read as `COALESCE(lineage_root_id, id)` so rows minted before the
  migration are their own root. Ruling and verification ownership accept any grant with the
  challenge-posting grant's lineage root.
- **Only `author_client` lineages succeed.** An `owner_web` grant needs no succession (it may
  already rule on and verify any supervise grant's challenges), and minting one from a pasted
  token is not a web flow; `succeeds` naming an `owner_web` predecessor is 422
  `owner_web_does_not_succeed`. `granted_via` therefore never changes along a lineage.
- Revoking any grant revokes nothing else; a revoked lineage member cannot be succeeded except
  by the idempotent retry above, so revoking the live tip ends the lineage.
- Rotation of the storyboard-token signing secret invalidates every live token, so succession
  across a rotation fails closed (`predecessor_expired`); the supervisor then needs an
  `owner_web` grant for old challenges, as today.

Plugin: `investigation grant --succeeds-credential-file <old>` is the one renewal command. It
is gated on a new capability `grant_succession` (an older pod's strict grant schema answers
400, and the existing unrecognised-supervisor-fields retry must not re-mint without the
supervisor link while still sending `succeeds`):

1. Refuse while any outbox under the old identity is unresolved (an uncertain post would
   become a 401 after the predecessor is revoked, and re-posting under the new grant could
   duplicate it): "resolve or retry these posts first".
2. Reserve the new credential file (existing grant `--out` reservation), mint with `succeeds`;
   on an uncertain answer keep the reservation and retry with the same `succeeds` (idempotent
   above).
3. Write the new token, then run the existing `renew-cursor` logic against it (the cursor's
   identity includes `grant_id`, so this must precede any deletion).
4. Only then delete the old credential file.

The skill renews when under 30 minutes remain and restarts its watch on the new credential
file (a watch on the old file gets 401 once the predecessor is revoked). Its sentence "renewal
does not transfer ruling authority" becomes "succession before expiry carries it; a new grant
after expiry does not".

## 5. Where the lifecycle is shown

### Timeline (conductor `packages/ui-pages/src/storyboards/`)

- **Challenge card**: the status chip (existing) uses the lifecycle labels:
  Awaiting response · Accepted, in progress · Correction submitted, awaiting verification ·
  Verified (<scope> @ <short sha>) · Correction rejected · Unverifiable · Declined, awaiting
  ruling · Directive … (existing, plus Directive correction rejected) · Withdrawn · Overruled.
- **Challenge detail** (expand): one ordered list joining the challenge, the event it
  challenges (its `#seq` ref), every position ack, the ruling, each correction (with
  `revision`, `artifacts`, `gate`, evidence chips) and each verdict under the correction it
  judges (scope chip, subject, evidence with tier, grant label / `granted_via`, superseded,
  revision-mismatch and revoked badges). All history stays visible.
- **Claim cards** (finding etc.): a verdict chip, "Verified · fixture_validation @ abc1234",
  never a bare "Verified".
- **Header**: open commitments by status (awaiting response n · in progress n · awaiting
  verification n · rejected n · verified n), each a filter.

### CLI (this repo)

- `cardinal-storyboard investigation verdict <seq> --verified|--rejected|--unverifiable
  --scope <scope> [--correction <seq>] [--repo R --commit SHA [--pr N] | --revision auto]
  [--artifact path] [--target T --observed-at TS] --text "<what was checked>" --evidence ev_… …
  --credential-file <grant>`.
  - **`--correction` is required for a challenge target.** The CLI refuses a correction seq
    above the committed review cursor's watermark when a `--cursor-file` is known ("review the
    page containing #k first"): a verdict follows a review, never a fetch.
  - `--revision auto` resolves the **supervisor's own** worktree (where it reproduced), never
    the worker's.
  - The fully resolved payload and its key are written to the outbox **before** the first
    POST; every retry sends from the outbox.
  - `--supersedes <seq>` revises this lineage's own active verdict on the same correction and
    scope; a 409 `verification_exists` from the same lineage names the flag.
  - Against a server without `capabilities.supervision_verifications`, or an old pod answering
    403 `token_scope_mismatch` on the route, it says the server does not support verdicts yet
    and suggests the cue convention.
- `cardinal-storyboard investigation challenges <inv> --credential-file <grant> [--check-heads]
  [--json]`: one row per challenge: seq, status, position, latest correction (seq, revision),
  verdicts (scope, commit, superseded/mismatch/revoked), and the next action (`await worker`,
  `verify #k`, `rule`, `none`). `--check-heads` runs read-only `gh pr view <pr> --repo <repo>
  --json headRefOid,statusCheckRollup` for each PR named in a correction or verdict and marks
  **head moved** and the CI rollup state; without `gh` it says "heads unchecked". This table is
  also the challenge half of a generated handoff (item 5), from structured state, with no
  credential contents.
- `events` shows the new type compactly: `#N verification.recorded verified code_review
  github.com/x/y@abc1234 of #c (correction #k)`.

### Skill (`common/supervise/CORE.md`)

- Verdicts and posts: `upheld` and `unverifiable` on a finding become `investigation verdict
  <seq> --verified|--unverifiable --scope …`; `challenged` stays a challenge. State the scope
  you actually checked; never record `merge_readiness` or `deployment` from a fixture run.
- Rulings: "it accepted and later checkpointed the correction: verify that correction, then
  `investigation verdict <c> --correction <k> --verified|--rejected …`". Rejected names what
  is still wrong; a second rejection on the same point goes to a ruling, not a third round.
- Old-worker fallback (§3) and gateway-posted challenges (§2) in one line each.
- Pass loop: start each pass with `investigation challenges <inv> --check-heads` instead of the
  private handoff file. Renew by succession with under 30 minutes left.
- Closing summary counts come from `challenges --json`.

## Not in this spec (named so they are not lost)

- **Attachments for rehydration** (companion spec): grantee and author upload of objects
  (briefs, handoffs, plans, relied-on memories, test logs, diffs) linked from the log, so a
  remote supervisor's evidence is readable and an investigation can be resumed elsewhere.
- **Item 2, activity join**: activity lives in the local evidence spool, not on the server.
  "Activity since last review with no checkpoint" needs a local reconciliation command.
- **Item 4, one review workspace**: the page-file flow already gives one immutable page and an
  explicit commit; a combined tag/mark/commit UI is ergonomics on top.
- **Item 5, full handoff**: `challenges --json` plus budget and cursor state.
- **Item 6, supervision status line**: depends on watcher health reporting.
- Server-side forge polling for PR heads or CI.

## Compatibility and rollout

1. conductor: registry (type, authority), migration (type/authority CHECKs restated in full
   from the newest migration, grantee CHECK widened to `verification.recorded`, a
   `maestro_investigation_events_verification_supervisor` CHECK, grants `succeeds_grant_id`
   nullable FK with a unique index, `lineage_root_id`, `revoked_reason`, `succeeded_by`), `record-verification` route and
   grant route table entry, ack route accepting verdict targets, `revision` on every semantic
   payload, `succeeds` on grant minting, `challengeStatuses` + fixture v3, capabilities
   `checkpoint_revision` and `supervision_verifications`, UI. Deploy before any plugin.
   **The migration runs before any new pod serves traffic**: the grants repository selects the
   new columns, so a new pod against the old schema fails every grant lookup (as with
   `20261031091000`).
2. Older pods' readers hide the new type (known-type allowlist); an older plugin's status
   mirror ignores it and shows `addressed`: correct, just less informative.
3. plugin: CLI `verdict`, `challenges`, `revision` with the checkpoint outbox, `grant
   --succeeds-credential-file`, worker delivery and local tracking (§3), skill text, status
   mirror against the vendored fixture v3. Every new path is gated on its capability; an older
   server keeps today's behaviour.
4. Rollback: code-only; the down migration refuses while any `verification.recorded` row or any
   grant with `succeeds_grant_id` exists.

## Validation

- Fixture v3 cases above; every v2 case unchanged apart from the two null fields.
- Route tests: every 4xx above; a second verdict on the same (verifies, correction, scope) from
  an `owner_web` grant refused; a verdict naming a non-current citing event refused with
  `correction_not_current`; a self-minted lineage's claim verdict does not block another
  lineage's; claim verdict on a current correction refused; succession refused when expired,
  revoked (not by succession), scopes differ, or the predecessor is `owner_web`; the
  predecessor is revoked with reason `succeeded` on success; a retried succession returns the
  same successor with `duplicate: true`; a dirty code-scope subject without `tree` →
  `dirty_subject_requires_tree`; a dirty `merge_readiness` subject →
  `merge_readiness_requires_clean`; a same-lineage `supersedes_verdict` accepted, another
  lineage's refused, a fourth revision refused.
- CLI/UI: two dirty snapshots at the same HEAD with different `tree` show a revision mismatch;
  `verdict --scope merge_readiness` from a dirty tree refuses locally before sending.
- Plugin: `revision: auto` resolve-once (retry after `gh` flakes and after a new commit sends the
  identical body); `verdict` refuses a correction above the review watermark and retries from the
  outbox; re-surfacing and Stop re-block for `directive_rejected`; local tracking keeps verdicts
  through compaction; `challenges --check-heads` with a stub `gh`; capability-off stripping.
- One live round trip on a private investigation: challenge → accepted → correction with
  `revision: auto` → `verdict --verified --scope code_review` → timeline shows Verified
  (code_review @ sha); a second correction → awaiting verification with the superseded verdict;
  a rejected verdict re-surfaces in the worker; succession before expiry → the new grant
  verifies the old challenge.

## Review record

First independent review (2026-10-18): BLOCKERS, six, each resolved in revision 2:

1. Latest-verdict-wins let a verified verdict in another scope, or from an `owner_web` grant,
   hide a rejection → verdicts combine per correction, rejected dominates, one verdict per
   (verifies, correction, scope) across all grants (§2 Rules, §3).
2. Succession by an expired token was unimplementable (`signer.verify` rejects expiry; no token
   hash stored) and turned a stale on-disk token into permanent authority → live predecessor
   only, one successor, predecessor revoked, `granted_via` cannot widen (§4).
3. Auto-filled `--correction` could verify an unreviewed correction or change across retries →
   required, checked against the review watermark, resolved payload persisted before POST (§5).
4. `revision: auto` broke checkpoint retry dedup → resolve once into a checkpoint outbox (§1).
5. A rejected directive correction did not re-block Stop → `directive_rejected` in
   `OPEN_DIRECTIVE`, `stop_blocked` cleared on rejection (§3).
6. Local tracking dropped verdicts, so `rejected` never re-surfaced → tracking, mapping,
   essential events and eviction rules specified (§3).

Non-blocking points folded in: fixture v3 and null fields, ack route, grant route table and
old-pod detection, uniqueness as a lock guard, `to_session_id`, gateway-posted challenges,
incidental citations, the directive correction rule, wrong-target refusal, `revision` on every
semantic type, old-worker fallback, server cost, revoked-grant flag, doc locations.

Second review (2026-10-18): the six fixes held except local tracking; four new BLOCKERS, each
resolved in revision 3:

A. A lost succession reply revoked the predecessor and left no usable token → idempotent
   succession re-signs the successor's token for a caller holding the live predecessor
   token; revoke reason recorded; old file deleted only after the new token and cursor are
   on disk (§4).
B. The route accepted verdicts on citing events the derivation never counted, so a rejection
   could vanish → one shared `correctionOf`; the route requires `correction` = current `k`;
   after a verdict only an event answering it (`#v`) or carrying revision/artifacts
   supersedes (§2, §3).
C. Cross-grant uniqueness on claims let a self-minted worker grant occupy scopes and block the
   supervisor → claim uniqueness is per lineage (§2).
D. `_slim` dropped `revision`/`artifacts`, so local status diverged → `has_revision` /
   `has_artifacts` kept; verdicts slimmed explicitly; `essential_events` uses `correctionOf`
   (§3).

Non-blocking points folded in: outbox and watch handling at succession, renewal ordering,
`lineage_root_id` backfill, no `owner_web` succession, concurrent GIN migration, narrower
`verify_the_challenge`, replay notice for kept checkpoint outboxes, `auto` CLI-only, TS/Python
re-surface sets.

Third review (2026-10-18): A, C, D closed; one BLOCKER, resolved in revision 4:

1. `correctionOf` let a later plain summary displace an unjudged revision-bearing correction
   (forcing the verdict onto the summary) and left "verdict on an earlier k, none on the
   current k" undefined → an explicit fold with a "substantive" predicate (revision,
   artifacts, or answering a rejected/unverifiable verdict by `#v`), fixture cases for both
   failure cases, and `essential_events` keeping every fold input (§3).

Non-blocking points folded in: sign before commit on succession, concurrent succession takes
the idempotent path, match fields and ignored TTL, `grant_succession` capability, stub
shortcut keeps answering events, refs to a verified verdict reopen nothing, 409 prints the
stored grant.

Fourth review (2026-10-18): **NO BLOCKERS.** The fold is deterministic and agrees with the
route's current-`k` rule because every input it reads has a lower seq. Non-blocking points
folded in: backward-only `#v` stated and checked, `essential_events` bounded to the fold's
live inputs, worker guidance to attach `artifacts` to non-code corrections.

Supervisor review (Codex GPT-6 supervisor on this Investigation, 2026-10-18), two accepted
challenges, resolved in revision 5:

- #19: one immutable verdict per (challenge, correction, scope) forced worker activity for a
  new review result and left a mistaken verdict unretractable → `supersedes_verdict`, own
  lineage only, at most 3 revisions per chain, only active verdicts combine (§2, §3).
- #25: `dirty: true` plus a commit did not bind a snapshot → `revision.tree` from a temporary
  index, required on dirty verdict subjects for code scopes, tree comparison in the mismatch
  check, "snapshot unbound" otherwise (§1, §2, §3).
- #33 (follow-up to #25): `merge_readiness` still allowed a dirty, unbound subject →
  `merge_readiness_requires_clean`; snapshot-unbound display covers the verdict subject too.

Fifth independent review (2026-10-18), of revision 5: one BLOCKER, resolved in place:

1. The revision cap was keyed without lineage, so on a claim a worker's own revision chain
   could exhaust it and block the supervisor's retraction → the cap counts per chain.

Non-blocking points folded in: renewed-grant retry of a supersede, `#v` substantive only while
the verdict is active, tree computation seeded from the real index, submodule and filter
failures leave `tree` out, `core.fileMode`, exactly-one-side-dirty always mismatches.

Implementation review of the conductor diff (2026-10-18): SHIP after aligning §2, folded in:
gateway/member-posted challenges cannot be verified (code and `issue-ruling` agree; the spec
sentence was wrong); `#v` answers only a verdict on the current correction; supersedes exempt
from `verify_the_challenge`; the 409 message names `supersedes_verdict` for the same lineage;
migration before new pods.

## Review gate

An independent reviewer reads this spec and the code it names, reports blockers with concrete
failure cases, and cannot edit. Revise until no blockers before implementation.

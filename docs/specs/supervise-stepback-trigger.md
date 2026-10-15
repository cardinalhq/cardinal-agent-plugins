# A measured step-back trigger for supervise

Status: proposal, 2026-10-15. Follows docs/specs/supervise-stepback.md (the budget trigger on
the held branch `feat/supervise-stepback`). Numbers below were read on 2026-10-15 from
Cardinal (read-only); scratch work is in
`/private/tmp/claude-501/-Users-ruchirj-workspace-cardinal-agent-plugins/2650df3c-bf06-4edc-9d92-fddedf508937/scratchpad/trigger-proposal/`.

## Summary

- The current lag trigger is a reasonable first guess. It is not a measured one: its
  thresholds were picked by hand and checked on one in-sample replay.
- There is not yet enough data to measure it. On this account, 5 investigations have 100 or
  more events, 10 have 50 or more, all are from one author over 8 days, and 1 had a
  supervisor. A properly powered, pre-registered study must wait for more data.
- So: ship the branch with its thresholds marked provisional. Log every candidate signal in
  shadow at each `--check`. Move the budget into typed events in the supervisor's own
  investigation (needs a new server event class). Run the labeling protocol now as a pilot.
  Fit thresholds only once the held-out set has enough rework episodes.
- On the evidence, churn is worth testing, but not in the form first proposed. Revisions of
  the same claim id miss most of wingbeat's rework, because the worker minted new ids for it.
  Raw churn counts also do not carry across investigations. Signals based on hypotheses and on
  "time since a claim was settled" have almost no data or no contrast, so they should be
  dropped now rather than studied.

## 1. What is weak about the current trigger

| Weakness | Why it matters | Evidence |
|---|---|---|
| Thresholds hand-picked (0.15 × P80, earned < ½ spent, +50% re-fire, 1/2/4× backstops, 30-min idle gap) | We cannot say how often it fires on healthy work, or how early it fires on real rework | `DEFAULT_THRESHOLDS` in core/cardinal_core/investigation_budget.py |
| Checked once, in-sample | The replay used the milestones from the backtest that the trigger was designed after. It fires at #288 because it was built to. | core/tests/testdata/wingbeat-phase-f-worker-times.json; spec "As built" (Replay) |
| Progress moves in steps, and the step size depends on how finely the work is cut into milestones | With 6 milestones of ~2 h each, earned jumps in 2 h steps. The same work cut into 3 milestones lags twice as long before anything is earned. | spec §2 |
| Estimator bias goes straight into earned | Earned is the *P50 hours* of done milestones. An estimator that pads P50 makes every done mark worth more and hides a lag. | spec §2 |
| "Done" is the supervisor's call | A done mark needs a ref, but nothing checks that the ref proves the milestone. Nothing un-marks it when the cited claim is later revised or retracted. | `mark_done` |
| Blind to churn, wingbeat's strongest signal | In phase F (#199–#336), 20 of the worker's 68 semantic events were revisions, retractions, or new claims named as rework, amendments or repairs (29%). In #1–#198 the share was 12%. Earned against spent cannot see any of this. | §3, S2 |

## 2. Milestones as typed events in the supervisor's own investigation

### Why not use the existing semantic types

The supervisor session already has its own investigation (every connected session gets one
at SessionStart). It could record milestones there with today's `finding.proposed` and
`decision.proposed`, with no server change. That does not work, for three reasons:

1. **Refs cannot cross investigations.** A semantic ref is "#<seq> of an earlier event of this
   investigation" (conductor `packages/maestro/src/routes/investigation-events.ts`, `refs`), so
   `#288` would point at the supervisor's own #288.
2. **They leak to the worker.** Org members can read semantic events (`reader.ts`
   `visibleClasses`: member → control, semantic, owner, wake). Semantic text is also indexed for
   storyboard search (`storyboard/search-doc.ts`), and the worker's storyboard discovery hooks
   surface storyboards written from the same repo. A budget recorded as findings would reach
   the worker's context through ordinary discovery.
3. **They are claims about the work, not supervision records.** They would show up on the
   supervisor's Storyboard as findings.

### Proposed server change: a new class, `supervision`

Event types are registered in one place: `packages/shared/src/investigation/events.ts`. That
file exports `events.json`, and a vitest drift test regenerates and checks it. The Go contract
test and the Postgres CHECK contract test both read it. Each new type also needs a migration that
widens `maestro_investigation_events_type_check` and `_authority_class`, as
`20261021090000_maestro_investigation_session_wake.up.sql` did for `session.wake`.

| Type | Payload (server-validated) |
|---|---|
| `direction.set` | `{direction_id, question, subject:{investigation_id, since_seq}, worker_session_id, basis, worker_estimate?, thresholds{}}` |
| `milestone.planned` | `{direction_id, milestone_id, title, p50_h, p80_h, basis, done_when}` (one per milestone; re-planning sends a new one with `replaces`) |
| `milestone.done` | `{direction_id, milestone_id, fraction, refs:[{investigation_id, seq}]}` |
| `plan.snapshot` | `{direction_id, components:[{name, set_by: owner\|worker\|policy, refs}]}` (taken from each brief) |
| `stepback.fired` | `{direction_id, trigger: opening\|lag\|backstop\|<signal>, signals:{…all values at this check…}, shadow: bool}` |
| `stepback.verdict` | `{fired_seq, verdict: on_track\|worker_choice\|owner_constraint, routed_to: none\|worker\|owner, post_ref?}` |
| `stepback.outcome` | `{verdict_seq, outcome: accepted\|declined\|overruled\|changed_constraint\|no_response, refs}` (appended later; events never change) |
| `direction.closed` | `{direction_id, outcome: done\|stopped\|replaced, spent_h, earned_h, per_milestone_actuals}` |

- **Class and authority.** The new class is `supervision`, with a new authority
  `supervisor_record`. Only the investigation's author can write these events, through a new
  route `record-supervision` (or `checkpoint-investigation` with a class check). Grantees
  never write them. They are not material, are left out of `search-doc`, the storyboard
  projection and discovery, and are never deliverable. Older plugins already skip an unknown
  class and only move their cursor past it, which is how `wake` was introduced.
- **Refs across investigations.** A `{investigation_id, seq}` ref is accepted only if the writer
  can read that investigation (as its author, or through a grant with `read`/`supervise`) and
  `seq ≤ head_seq`. This is the one genuinely new rule for the server.
- **Who can read them.** The author always can. A grantee can only with a new scope,
  `supervision:read`, so the owner can share calibration data deliberately. Ordinary org members
  cannot, unlike `semantic`.
- **Remaining risk.** The worker usually runs as the same person as the supervisor, so it is an
  *author* of the supervisor's investigation, and no principal-based rule can hide the events
  from it. It is protected only by never being given the supervisor's investigation id and
  never having the events delivered or surfaced by search or discovery. A worker that went
  looking could read them. I recommend documenting this, not building session-level access
  control.

Rough effort (conductor): registry and types, migration, route and validation, reader scope,
search and projection exclusion, contract tests. About 3–4 days, plus a maestro deploy before the
plugin starts writing.

### Making "done" checkable

- Each `milestone.planned` carries a `done_when` that the estimator writes before any work
  starts, for example "a finding showing all four corpora byte-identical".
- A `milestone.done` counts as **verified** when every ref is a worker `finding.*`,
  `decision.*` or `experiment.completed`, the supervisor posted `Review: upheld` citing it,
  and there is no later `*.revised` or `finding.retracted` of the same `semantic_id`. A later
  revision or retraction **re-opens the milestone automatically**, so earned goes back down.
  `--check` computes all of this from the record, so the supervisor's judgment is limited to
  "this claim meets `done_when`", which an auditor can re-check.

### Moving off budget.json

1. `budget --export` posts the open budget and its history as events (idempotency key:
   direction id + seq).
2. Events become the source of truth. budget.json becomes a cache of them (it keeps 0600 and
   flock) so `--show` keeps working offline.
3. The estimator's reference class reads `direction.closed` from every investigation the owner
   can read, not just `~/.cardinal/supervise/*` on one machine.

## 3. Candidate signals, computable at every worker event

"Worker events" means events with `producer.session_id` = the worker session (as in
`worker_event`). "Window" means the trailing 2 worker-active hours, using the 30-min idle rule.
Every value is logged as shadow data in `stepback.fired.signals`, whether or not anything fires.

| # | Signal | Exact definition (fields) | Wingbeat / corpus evidence | Pre-study call |
|---|---|---|---|---|
| S1 | Earned/spent (current) | earned = Σ p50_h × fraction over **verified** `milestone.done`; spent = worker active hours since `direction.set.since_seq` (event `created_at` plus spool timestamps) | First fires at #288 (4.10 h, earned 2 h). In-sample. | Keep as the baseline |
| S2 | Churn, structural | Count in the window of worker `finding.revised`, `decision.revised`, `experiment.revised`, `finding.retracted` | Phase F: 8 (#211 #223 #228 #229 #236 #248 #312 #332) | Test |
| S2b | Churn, broad | S2 plus first appearances of a `semantic_id` matching `rework\|amend\|repair\|supersed\|retract` | Phase F: 20 of 68 (29%), against 12% in #1–#198. Window count peaks at 10–11 from #237 to #263 (1.6–2.6 active h), well before S1's #288. But three unlabeled investigations reach 15, 16 and 20, so **raw counts do not carry over** | Test as a *share* of the window's semantic events and as a ratio to the worker's own first 2 h |
| S3 | Claim-family depth | Most revisions of one `semantic_id` family (strip a trailing `_vN`) within the direction | Peaks at 3 (#133/#139/#141, before phase F). Phase F rework used new ids, not revisions | Test, low expectations |
| S4 | Open hypotheses / questions | `hypothesis.proposed` minus `hypothesis.resolved`; `question.opened` minus `question.resolved` | 0–2 hypotheses and 0–8 questions per investigation across the 10 largest | **Drop**: workers do not use these types |
| S5 | Time since a claim was settled | Worker-active minutes since the latest worker claim that has a `Review: upheld` cue citing its `#seq` and no later revision | Phase F maximum is 32 min: claims kept being upheld while the approach was wrong. This needs a supervisor (1 of the 10 largest had one) | **Drop**: it measures local progress, which is the thing that misled us |
| S6 | Plan growth | Number of components in the latest `plan.snapshot`, minus the first one for the direction | Not recorded today. Only changes at briefs, so it is coarse | Start recording; study later |
| S7 | Verdict mix | Supervisor challenges ÷ (challenges + upheld) in the window, and the interaction "all upheld while S2b is high" | Phase F: 0 challenges against 30 `Review: upheld` cues. #1–#198 had 4 challenges (#8 #12 #67 #181) | Test the interaction |
| S8 | Declined approach concerns | Worker `acknowledged` with `disposition: declined` on a supervisor question or challenge that a step back or approach-level review raised, per direction | #205 declined #204; the supervisor withdrew at #206 | Test (rare; mostly useful in routing) |
| S9 | File re-edits | Edits in the activity spool (`cardinal-evidence list`) to a path already edited ≥3 times in the window | Only when the worker's spool is on the supervisor's machine. Not measured in this corpus | Record when available; study later |

There is a real problem with S2b. Its lexical rule depends on how the worker names things, and a
worker could rename to avoid it. It only stays safe because the worker never sees signals. A
structural alternative is an optional `corrects: <semantic_id>` field on `finding.proposed`.
That changes the worker's own checkpoint schema, though only its own record, never the budget.
See decision 5.

## 4. Study design (pre-registered)

### Corpus: what exists today (feasibility count, run 2026-10-15)

| | Count |
|---|---|
| Investigations bound on this machine (org c4375e34…, excluding local-dev) | 646 (1 unreadable) |
| … with ≥1 / ≥20 / ≥50 / ≥100 / ≥300 events | 147 / 35 / 10 / **5** / 1 |
| Sizes of the ≥100 group | 451 (wingbeat, now), 140, 140, 134, 127 |
| First-event dates | 2026-10-07 to 2026-10-15 |
| With any grant / with a `supervise` grant | 2 / **1** (wingbeat) |
| Worker-active hours across the 10 with ≥50 events | 3.2–16.6 h each, ~69 h total |
| `owner.decided` / `owner_input.recorded` in those 10 | 0 / 0 (owner_input capture is off in prod) |
| Distinct authors found | 1. Four `storyboard__find` searches returned 22 investigations, all by the owner and all already in the local set |

There is no route that lists investigations org-wide, so this is a lower bound for the owner's
own work and says nothing about other members. Someone with database access can get the org
figure with: `SELECT count(*) FROM (SELECT investigation_id FROM maestro_investigation_events WHERE org_id = '<org>' GROUP BY 1 HAVING max(seq) >= 100) x;`.

What this means: n = 5 (or 10) investigations cannot support a train/held-out split with any
power. **The unit of analysis has to be the labeled episode, not the investigation**, and the
corpus has to grow. Sources, in order:

1. Investigations accrued from now on, with shadow logging (§5).
2. The ≥20-event tail (35).
3. Local Claude/Codex transcripts, so that labelers can see owner pivots that the record does not
   (decision 4).

### Labeling: blind to the signals

- **Labeler**: a fresh agent given the full worker record (events plus artifacts), the repo's git
  history over the window (reverts, deleted files, superseded design docs), and transcripts if
  allowed. It **never sees signal values, budgets or firings**.
- **Task**: mark *episodes*. For each one, give:
  - **onset**: the earliest seq by which the facts the eventual fix relied on were all on record,
    the way the #199 backtest found the accounting simplification from #199-era facts;
  - **resolution**: the seq where the plan actually changed (a REWORK decision, an owner pivot,
    an abandoned design, or the third revision of a claim family);
  - the evidence seqs and a confidence (high/med/low).

  Wingbeat phase F is the worked example: resolution at #330/#336.
- **Agreement**: a second labeler from a different model family labels 30% of investigations.
  Score Cohen's κ on 30-minute active windows (inside an episode or not), and onset agreement
  within ±1 active hour. **κ < 0.6 means the rubric gets fixed before anyone looks at signals.**
  The owner settles a sample of disagreements.
- **Negatives**: every active window outside an episode.

### Split, metric and decisions

- **Split**: by date, frozen in advance. Investigations started before the cut are *train*;
  later ones are *held-out*. Thresholds are chosen on train only, using leave-one-investigation-out.
  Held-out is evaluated once.
- **Primary metric**: episode recall at a fixed false-alarm rate. A fire counts as a hit when it
  lands between onset and resolution; lead time = resolution minus fire time, in worker-active
  hours. A false alarm is a fire outside any episode, counted per active hour.
- **The false-alarm budget**: a false alarm costs one read-only fresh-context pass and posts
  nothing when the answer is "on track". The supervisor's summary gets one line. A miss can cost
  hours: phase F was ~6 h before the REWORK. So the budget can be generous. **Proposed: at most
  1 false alarm per 3 worker-active hours** (decision 3).
- **Models**: S1 alone (the baseline); each signal alone; S1 OR (one other signal); and at most
  a three-signal OR. Thresholds are monotone and come from a grid. There is no learned model
  until there are ≥50 episodes.
- **Decisions per signal**, measured on held-out at the operating point:
  - **keep** if it adds ≥ 2 episodes of recall over S1 alone, or ≥ 1 active hour of median lead,
    without breaking the false-alarm budget;
  - **change** (re-normalize, e.g. absolute count → share) if it helps on train but not held-out;
  - **drop** if it adds nothing on either.

  S1 itself is dropped as a firing rule, and kept only as the backstop, if S1 OR S2b beats it
  and S1 adds nothing on top.
- **Power**: the confirmatory run waits until held-out has **≥ 20 high/med-confidence
  episodes**. With fewer, report it as a pilot and leave the thresholds alone.

## 5. Calibration after shipping

- **Shadow logging from day one**: every `--check` computes S1–S3, S6–S9 and writes them in
  `stepback.fired` with `shadow: true` when only a shadow signal crossed its provisional line.
  Shadow fires run **no** step back.
- **Labels from use**: a live fire is *useful* when its verdict is not `on_track` and its outcome
  is `accepted` or `changed_constraint`. *Noise* is `on_track`, or `declined` with the supervisor
  agreeing. A *miss* is a labeled episode with no live fire before its resolution, found by
  running the labeler over closed directions every week.
- **Updating thresholds**: re-fit on all labels after every 20 new labeled fires or misses, but
  only inside pre-set bounds (each threshold can move at most ±25% per update). Every update is
  written as an event, so the history can be replayed. A bad update can be rolled back by
  pinning `thresholds` on `direction.set`.
- **Guarding against Goodhart**:
  - The worker never sees budgets, signals or triggers (unchanged, and the post text never
    names the trigger).
  - Signals are computed by the CLI from the record. The supervisor contributes only milestones,
    done marks and plan snapshots.
  - Done marks must verify (§2) and re-open when the cited claim is revised.
  - The "on track" verdict comes from the fresh-context pass, not from the supervisor. Usefulness
    is scored on *outcomes*, not on verdicts.
  - The owner audits a monthly sample of 5 `on_track` verdicts.

## 6. Phases

| Phase | What | Effort | From `feat/supervise-stepback` |
|---|---|---|---|
| 0. Ship provisional | Merge the branch as is. Mark thresholds "provisional" in the skill and the spec. Add shadow computation of S2/S2b/S3/S7/S8 to `--check` (stored in budget.json `firings[].signals`) | ~1 day | Everything: budget CLI, opening step back (threshold-free, and what the backtest actually validated), lag/backstop, routing, stepback agent and Gemini policy, replay test. Version re-bumped above the investigation-mode PR |
| 1. Pilot labeling | Labeling rubric and agent; label the 10 investigations with ≥50 events (double-label 3); a replay harness that computes signals offline from the event streams; a pilot report. No threshold changes | ~2–3 days | The replay fixture becomes one row of the harness's test data |
| 2. Typed records | Conductor: `supervision` class, 8 types, `record-supervision`, cross-investigation refs, `supervision:read`, exclusions. Plugin: write events, budget.json as cache, `--export`, done verification and re-open | ~3–4 days server, ~2 days plugin | `investigation_budget.py` keeps its maths; storage moves to events |
| 3. Online calibration | Weekly miss-labeling, re-fits within bounds, owner audit | ~1 day, then running cost | — |
| 4. Confirmatory study | When held-out reaches ≥20 episodes: one pre-registered run; keep/change/drop per signal | ~2 days | — |

## Open questions for the owner

1. Ship phase 0 now with provisional thresholds and shadow logging, or hold the branch until the
   pilot is done? (Recommendation: ship. The opening step back alone justifies it.)
2. Approve a new server class `supervision` (author-only writes, `supervision:read` grant,
   members excluded, not indexed), accepting that a same-principal worker could read it if it
   went looking?
3. Set the operating point: at most 1 false alarm per 3 worker-active hours?
4. Corpus: may labelers read local Claude/Codex transcripts (owner pivots are invisible in the
   record while owner_input capture is off)? And should other members' investigations be included
   if they agree?
5. Churn: rely on the lexical rule (rework/amend/repair names) for now, or add an optional
   `corrects:` field to the worker's `finding.proposed` schema?

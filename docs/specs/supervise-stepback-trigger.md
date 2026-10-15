# Supervise step backs: a per-milestone rate trigger, recorded on the server

Status: accepted 2026-10-15 (owner accepted all five §9 recommendations; healthy-session backtest runs before the build). It replaces the lag/backstop trigger in
docs/specs/supervise-stepback.md (branch `feat/supervise-stepback`) and the earlier version of this
document. Backtest artifacts:
`/private/tmp/claude-501/-Users-ruchirj-workspace-cardinal-agent-plugins/2650df3c-bf06-4edc-9d92-fddedf508937/scratchpad/backtest/`.

## Summary

- **Trigger**: on each active milestone, compare the hours burned per unit delivered with the plan.
  At 2× the planned rate, run one step back scoped to that milestone.
- **Records**: plans, attribution, deliverable marks, firings, verdicts and outcomes are `supervision`
  events in the supervisor's own investigation, and the timeline draws them. budget.json becomes a
  cache.
- **Evidence (n=1)**: on wingbeat phase F, 2× fires at #270, after 3.1 worker-active hours. That is
  ahead of the old trigger (#288, 4.1 h), the REWORK (#330, 6.0 h) and the owner's pivot (#336,
  6.1 h). The #270 step back found the simpler design.
- **Calibration**: a live pilot. Each firing is labeled by its verdict and the owner's outcome, and T
  moves within 1.5–3×.
- **Tokens**: the step-back section must shrink. Formulas and prompts move into the CLI, and tagging
  and marking ride on the existing `reviewed` commit.

## 1. The trigger

### Definitions (milestone m)

| Quantity | Definition |
|---|---|
| estimate_m | the estimator's P50 hours for m |
| delivered_m | deliverables done ÷ total. A deliverable counts while its latest mark is done or redone, with a ref. |
| spent_m | worker-active hours charged to m |
| planned rate | estimate_m ÷ 100% |
| actual rate | spent_m ÷ delivered_m |
| ratio_m | actual ÷ planned = (spent_m ÷ estimate_m) ÷ delivered_m |

- **Scope**: the rules run only on *active* milestones: those with time charged in this review batch
  and less than 100% delivered.
- **Fire** when ratio_m ≥ T. T starts at 2.
- **Zero progress**: when delivered_m = 0, fire when spent_m ≥ Z × estimate_m. Z starts at 0.5.
- **Floor**: no firing while spent_m < min_spent_h. It starts at 0.5 h (owner, 2026-10-15; §2a).
- **Re-fire**: once per milestone per crossing. After a firing at ratio r_f, m re-arms only when:
  - ratio_m ≥ 1.5 × r_f (at 0% delivered: spent ≥ 1.5 × the spent at that firing; a reopen that
    drops m to 0% counts as an infinite ratio). A zero-progress firing counts as r_f = T, so once
    something is delivered the next firing needs ratio ≥ 1.5 × T (owner, 2026-10-15); or
  - m is re-planned (a `milestone.planned` that replaces it).

### Attribution

- In its review pass, the supervisor tags each worker event with the milestone(s) it advances, or
  `none`.
- The active time leading up to an event is charged to its milestones. That time is the gap since
  the previous worker event, measured over the existing union of event and spool timestamps.
- Gaps over 30 minutes are idle and not charged.
- An event with two milestones is split evenly between them.

### At a firing

The step back runs as the existing fresh-context, read-only pass, with the existing agent, routing
and runtimes. Its brief is scoped to the milestone:

- the deliverables, with `done_when` and their status and refs (the CLI writes this block);
- the facts, by `#seq`;
- the constraints, each tagged owner, worker or policy;
- the worker's current approach for this milestone;
- the code.

The prompt is unchanged: it begins "An elegant solution is often hiding in plain sight…", prices
owner constraints, and first asks whether this is an ordinary overrun. The worker never sees
budgets, rates or triggers, and a post to it never names the trigger.

### What happens to the branch's trigger

| Branch mechanism | Fate | Why |
|---|---|---|
| Direction-level lag (earned < ½ spent after 15% of P80) | Dropped | Earned moves in milestone-sized steps, so a burning milestone hides behind done ones. On wingbeat it fired 1 h after the rate trigger. |
| Earned weighted by P50; `--fraction` | Dropped | A padded P50 inflates earned. Checklists replace fractions. |
| Backstops at 1×, 2× and 4× of total P80 | **One kept: 2× total P80**, which fires a direction-wide step back | It is one comparison with no skill prose. It is the only check that sees time tagged `none` or spent outside the plan, which per-milestone rates cannot see. On wingbeat it would not fire (47 h): it is a safety net, and a firing means the plan missed the work. |
| Opening step back, 30-minute idle rule, `--set`/`--amend`/`--close` | Kept | — |

## 2. Backtest: wingbeat phase F (inv_cab0d4ba690509cd646915c2, #199–#336)

**Method**:

1. A blind planner, given only the #199 facts brief and the code at 8b51c326, wrote 7 milestones:
   P50 11.5 h in total, each with 2–4 checklist items with `done_when` (milestones-199.json).
2. A marker dated each deliverable done or reopened, with refs (marks-199.json).
3. A tagger attributed all 103 worker events: 6 `none` and 4 split between two milestones
   (event-tags-199.json).
4. rate_trigger.py replays the worker's event times through the rules.

| Trigger | Fires at | Worker-active h since #199 | Detail |
|---|---|---|---|
| Rate 1.5× | #255 | 2.3 | chunked-lane-implementation: 0.80 h of 2 h, 25% done |
| **Rate 2×** | **#270** | **3.1** | **chunked-lane-implementation: 1.16 h of 2 h, 25% done, ratio 2.33** |
| Rate 3× | #306, #330 | 4.9, 6.0 | late |
| Old earned-vs-spent lag | #288 | 4.1 | direction-wide |
| REWORK | #330 | 6.0 | — |
| Owner pivot to Postgres worklanes | #336 | 6.1 | — |

**What the #270 step back found** (stepback-270.md, from brief-270.md and the code at fb1c8422):

- **Reuse existing lane code.** The legacy `tickNode` is already a chunk executor parameterized by
  `h`: it frees scratch, resumes from the chain head and commits through the conditional put.
  Executed tests already cover same-hour increasing-H and crash-resume, though the record did not
  credit them.
- **Unneeded machinery.** About 1,650 non-test lines of in-native scratch accounting (scratchbudget,
  entry frontiers, group rewind) were reworked four times (#211, #228, #237, #261), and no owner
  constraint requires them.
- **Owner constraint priced.** It priced the owner's §4.5 "stop on any observed breach" waiver as
  what makes that machinery attractive, and asked whether a prospective refusal counts as a breach.
- **Simpler design.** 4 mechanisms against the worker's 9.

**Why attribution matters**: the first naive run had no tags. It split time across every "started",
unfinished milestone, and stale starts spread it six ways. 2× then fired first on the docs milestone
(#232), and on chunked-lane-implementation only at #332. Per-event tags put the firing on the
milestone that was actually burning.

**Ref readability**: a ref into another investigation is readable when the caller authored it,
when org members can see it, or when `subject_grant` is a live grant on that investigation with
`read` or `supervise`. The plugin sends the grant it reads the worker with. The token is left out
of the idempotency hash and never stored.

**Limits**:

- **One case.** n=1, and the marks and tags are LLM-made.
- **It missed the "no lease database" rabbit hole.** phase2-shard-proposal reached 0.38 h of 1.0 h
  with 0 of 2 done, and never fired. Other milestones fired late or not at all. The opening step
  back, which prices owner constraints up front, stays the backstop for constraint-driven overbuild.
- **No "redone" mark.** The marks format could not say "redone after reopen". When it is modeled
  (aws .1 re-met at #280, ci .1 at #295), 2× still fires at #270, and the re-arm rule adds #306,
  #323 and #330: 4 step backs over about 3 active hours. Only #270 was run.
- **Next.** Before shipping, backtest one healthy session to measure false alarms (§6).

## 2a. Backtest: a healthy session (lakerunner topm P0, inv_125102bcc5c974f4fffd5150, #1–#140)

The same method. The blind planner saw only BOOTSTRAP.md and PLAN.md (both written before the
session), events #1–#2 and the code at 5d65a5129. The session has 118 worker events, 5.34
worker-active hours, no revised or retracted events, and steady red→green progress.

- **Plan**: 9 milestones (one per P0 item), 37 deliverables, P50 46 h, P80 78.5 h.
- **Marks**: 32 of 37 met. **Tags**: 18 `none` (15% of active time) and 15 multi-tagged.
- **As planned**: 0 firings at 1.5×, 2× and 3×. The backstop is at 157 h. The §6 gate passes.
- **But the plan is 8.6× the actual hours**: per milestone, actual ÷ P50 runs from 0.01 to 0.16. The
  planner had no agent pace to anchor on, so it planned at human pace. At that calibration the
  trigger has no power either way. The wingbeat planner had agent timings from the record before
  #199.
- **Sensitivity** (the one plan scaled uniformly, not a re-plan):

| Plan scale (P50 total) | 2× firings | Detail |
|---|---|---|
| 1.0–0.2 (46–9.2 h) | 0 | — |
| 0.15 (6.9 h) | 2 | Both zero-progress firings: p0-7 at #69 (0.45 h, after the worker's own red test reopened its only done item) and p0-8 at #101 (0.27 h) |
| 0.1 (4.6 h, about the actual) | 4 | #19, #69, #97, #99 |
| 0.15 with `min_spent_h` 0.5 | 0 | — |
| 0.1 with `min_spent_h` 0.5 | 3 | #77, #99, #115 |

- **Wingbeat with `min_spent_h` 0.5**: unchanged, #270, #306, #323, #330. The smallest charge at a
  firing was 1.16 h.

**Consequences**:

- The estimator prompt (§3, written by `budget pass estimator`) asks for agent-active hours and
  gives the worker's measured pace: hours per deliverable from the record so far, and from
  `direction.closed` across the owner's investigations. With neither, the direction is marked
  `uncalibrated` in `direction.set`, and the CLI says so on its status line.
- `min_spent_h` is pinned in `direction.set`, default 0.5 (owner, 2026-10-15).

## 3. Milestones and supervisor duties

**Estimator rules.** `budget --set --plan <file>` validates the estimator's JSON. Each error names
the fix.

- Give a reference class (comparable work and its actual hours) or "none comparable" with a wider
  P80.
- Each milestone has an id (`[a-z0-9-]{1,24}`), a title, P50 and P80 hours (P80 ≥ P50 > 0) and a
  one-line basis.
- Each milestone has 2–6 deliverables, each with a `done_when` that is an observable fact in the
  record (a commit, a test result, a finding), not "works".
- Unknowns carry their own ranges and stay out of the totals.

**Each review pass** uses one command, the existing commit:

```
reviewed --batch <hash> --tag 208-215:chunked-lane --tag 220:none --tag 255:chunked-lane+ci-image
         --done chunked-lane.1@#255 --reopen aws-qual.1@#270 --redone aws-qual.1@#280
```

`--tag all:<m>` covers a batch that is all one milestone. `reviewed` reads the batch's worker
events with the grant. It refuses to commit when:

- a worker event is untagged, for example: "#272, #273 untagged: add `--tag 272-273:<milestone|none>`;
  open: chunked-lane, aws-qual";
- a mark has no worker-event or `ev_` ref;
- a mark is an impossible transition.

Otherwise it writes `work.attributed` and `deliverable.marked` (idempotent on the batch hash),
evaluates the rules, and commits the cursor. If a write fails, the cursor stays and the page
replays. It prints one line per active milestone:

```
chunked-lane 1.16/2.0 h · 1/4 done · 2.3× · FIRE step back
aws-qual 0.90/3.0 h · 1/4 done · 1.2×
reviewed through #270
```

## 4. Server: `supervision` events

The existing semantic types do not fit, for the earlier proposal's reasons:

- their refs cannot leave their own investigation;
- org members can read them;
- they are indexed for search and discovery, which is how they would reach the worker;
- they would read as findings.

| Type | Payload (server-validated) |
|---|---|
| `direction.set` | `{direction_id, question, subject:{investigation_id, since_seq}, worker_session_id, reference_class, calibration: calibrated\|uncalibrated, worker_estimate?, thresholds:{T, Z, rearm, backstop, idle_min, min_spent_h}}` |
| `milestone.planned` | `{direction_id, milestone_id, title, p50_h, p80_h, basis, deliverables:[{id, done_when}], replaces?}` |
| `work.attributed` | One per review batch: `{direction_id, batch, through_seq, tags:{"<seq>": [milestone…] or "none"}, charged_h:{milestone: h}}`. `charged_h` is derived and can be recomputed. |
| `deliverable.marked` | `{direction_id, milestone_id, deliverable_id, mark: done\|reopened\|redone, refs:[{investigation_id, seq} or {evidence_id}]}` |
| `stepback.fired` | `{direction_id, scope: opening\|milestone\|backstop, milestone_id?, snapshot:{spent_h, estimate_h, delivered, ratio, threshold}, worker_active_h}` |
| `stepback.verdict` | `{fired_seq, verdict: on_track\|worker_choice\|owner_constraint, routed_to: none\|worker\|owner, post_ref?}` |
| `stepback.outcome` | Appended later: `{verdict_seq, outcome: accepted\|declined\|overruled\|changed_constraint\|no_response, refs}` |
| `direction.closed` | `{direction_id, outcome: done\|stopped\|replaced, per_milestone:[{id, estimate_h, spent_h, delivered}], missed:[milestone_id]}` |

- **Class and authority**: class `supervision`, authority `supervisor_record`.
  - Only the author writes these events, through a new `record-supervision` route.
  - They are not material and never deliverable.
  - They are excluded from `search-doc`, the storyboard projection and discovery.
  - Older plugins skip the unknown class, as with `wake`.
- **Cross-investigation refs**: `{investigation_id, seq}` is accepted only when the writer can read
  that investigation (as author, or through a `read`/`supervise` grant) and `seq ≤ head_seq`. This
  is the one new server rule.
- **Visibility**:
  - the author can read them;
  - a grantee can read them only with a new `supervision:read` scope;
  - org members cannot.
- **Caveat**: a worker running as the same person is an author of the supervisor's investigation.
  It is shielded only because it is never given that id, and the events are never delivered or
  surfaced to it.
- **Plugin side**:
  - the CLI finds its own investigation by `--session <your_sid>`, as `supervising` does;
  - budget.json caches the events (0600, flock);
  - the estimator's reference class reads `direction.closed` across the owner's investigations.
- **Effort (conductor, about 3–4 days, then a maestro deploy)**:
  - registry entries in `packages/shared/src/investigation/events.ts`, plus the `events.json` drift,
    Go and Postgres CHECK contract tests;
  - a migration widening `maestro_investigation_events_type_check` and `_authority_class`, like
    `…_session_wake.up.sql`;
  - the route and its validation;
  - the `reader.ts` scope;
  - the search and projection exclusions.

### 4a. Wire contract (v1)

Both repos build to this; neither changes it without the other.

**Write**: `record-supervision`, modeled on `checkpoint-investigation` (batch, idempotent) and
`record-owner-input` (author-only).

```
{investigation_id, session_id, idempotency_key: [A-Za-z0-9_.:-]{1,128},
 events: [{type, payload}]  (1–50), subject_grant?: <grant token>}
→ 200 {investigation_id, duplicate: bool, events: [{seq, type, at}], head_seq}
```

| Condition | Response |
|---|---|
| caller is not an author of `investigation_id` | 403 `not_author` |
| a type outside class `supervision` | 400 `unknown_event_type` |
| a payload fails its strict schema | 400 `invalid_payload` with the path |
| a cross-investigation ref the caller cannot read, or `seq > head_seq` | 400 `ref_not_readable` / `ref_out_of_range` |
| `fired_seq` / `verdict_seq` not a `stepback.fired` / `stepback.verdict` in this investigation | 400 `invalid_payload` |
| same key and same body | 200 with the stored events, `duplicate: true` |
| same key, different body | 409 `idempotency_conflict` |
| database not migrated (CHECK violation) | 400 `supervision_unsupported`; the plugin keeps its cache and says once that the server is too old |

**Limits**: `direction_id` is `dir_[0-9a-f]{16}`; milestone ids `[a-z0-9-]{1,24}`, deliverable ids
`<milestone>.<n>`; text ≤ 1,000 characters, titles ≤ 200; 1–20 milestones per direction; 2–6
deliverables each; `work.attributed.tags` ≤ 500 keys, every key ≤ `through_seq` ≤ the subject's
`head_seq`; refs are `{investigation_id, seq}` (checked) or `{evidence_id: ev_[0-9a-f]{12}}`
(passed through). `thresholds` is `{T, Z, rearm, backstop, idle_min, min_spent_h}`.

**Read**: the existing read route with `class: "supervision"`. The author sees the class, a grantee
only with `supervision:read`, and org members never.

## 5. Timeline visualization (v1, author-only)

This is a Supervision panel on the supervisor's investigation page, next to the existing
`investigation-timeline/Supervision.tsx` header.

- **Direction header**: the question, the P50/P80 totals, worker-active hours, the share tagged
  `none`, and the thresholds in force.
- **Burn chart per milestone**:
  - x is hours charged and y is % delivered;
  - the planned line runs from (0, 0) to (estimate, 100%), and the threshold line from (0, 0) to
    (T × estimate, 100%);
  - the actual line is a step line, and it fires when it crosses right of the threshold line;
  - a tick marks Z × estimate.
- **Checklist**: each deliverable with its mark history. Refs link to the worker's investigation at
  that seq.
- **Firing markers** on the chart and the timeline, each with its verdict, its outcome once
  recorded, and links to the brief and the answer.
- **Later**: a link from the worker's page header, which needs `direction.set` indexed by subject.

Effort: about 2–3 days.

## 6. Pilot and calibration

**Labels.** Each firing is labeled by:

- the fresh pass's verdict;
- the owner's outcome.

A firing is **useful** when the verdict is not `on_track` and the outcome is `accepted` or
`changed_constraint`. It is **noise** when the verdict is `on_track`, or when the outcome is
`declined` and the supervisor agrees. A **miss** is a milestone listed in `direction.closed.missed`
(later rework or a pivot, with no firing).

**Adjustment.** After every 5 labeled firings, `budget calibrate` prints a suggestion and its counts.
It moves T by 0.25×, within 1.5–3×:

- up when 4 of the last 5 were noise;
- down when there was a miss and at most 1 of the last 5 was noise.

Z moves within 0.3–0.8. The owner approves each change, and new directions pin the thresholds in
`direction.set`, so any firing replays under its own rule.

**Before shipping** (done, §2a). Run the wingbeat method on one healthy session: a ≥100-event investigation
with no rework or pivot. It passes when 2× fires at most once per 3 worker-active hours, and each
fresh pass answers `on_track` or finds something real.

**Regression tests.** The wingbeat fixture (worker times #197–#336, plan, marks with redone, tags)
becomes a core test:

- at 2×, the first firing is chunked-lane-implementation at #270 (ratio 2.33);
- at 1.5×, the first firing is at #255;
- the re-arm sequence is #270, #306, #323, #330.

The healthy fixture joins it: no firing at 2× as planned, 2 firings at 0.15× scale, and 0 at 0.15×
with `min_spent_h` 0.5.

## 7. Token bloat

**Surfaces.** Sizes are cl100k tokens (tiktoken). The worker sees nothing new, and no hook text
changes (the SubagentStart skip for `cardinal:stepback` already exists).

| Surface (who reads it) | origin/main | Branch now | Budget |
|---|---|---|---|
| CORE.md step-back section (supervisor) | 0 | 1,197 | **≤ 850**. Never above 1,197. |
| CORE.md budget lines in set-up and the loop | 0 | 43 | ≤ 43. The `--check` step folds into `reviewed`. |
| CORE.md whole | 3,375 | 4,594 | ≤ 4,250 after the rebase on #222 |
| SKILL.md fresh-context sections (Claude, Codex, Cursor, Gemini) | 0 | 119, 184, 100, 170 (573) | ≤ 573 in total |
| SKILL.md whole (same order) | 728, 664, 614, 586 | 794, 797, 685, 705 | Only the line above changes |
| agents/stepback.md | 0 | 151 | Unchanged |
| Per review pass: `budget --check` plus the `reviewed` ack | 31 | 151 + 31 = 182 | One `reviewed` output, ≤ 1 line per active milestone plus 1: about 60–90 for 2 milestones |
| CLI errors | — | — | ≤ 2 lines, naming the fix |
| Estimator and step-back prompts (fresh passes only) | 0 | In CORE.md: 99 + 184, plus about 70 for the brief's first line and the FIRE preface | Written by `budget pass` into the pass's brief copy. Estimator ≤ 250. The step back adds only an answer cap of 800 words. |
| Step-back answer, read by the supervisor | 0 | 3,640 (stepback-270.md) | ≤ 1,200, through the cap |
| Firing brief | 0 | 2,625 (brief-270.md) | The status block is CLI-written, ≤ 30 per deliverable |

**Where the cut comes from.** About 350 tokens leave the skill:

- the two prompts, the brief's first line and the FIRE preface move into the CLI. `budget pass
  estimator|stepback --brief F [--milestone m]` writes them into the pass's brief copy;
- the `--done`/`--fraction` paragraph (95 tokens) goes.

About 60 come back: one sentence on `reviewed` tagging and marking. Its errors teach the syntax.

**Process**:

1. A measure script checks these budgets each build round, and a round over budget is not done.
2. The last gate before push is one independent token-bloat review, under the owner's standing
   rule.
   - The reviewer has no context, and gets per-surface before/after counts against origin/main.
   - Every cut must keep behavior, shown by targeted tests (`core/tests/test_investigation_budget.py`,
     the replay fixtures, the adapter skill tests) and by rendered transcripts of a set-up, a
     tagged review pass and a FIRE.
3. Never run `make check` locally; CI is the gate.

## 8. Phases

| Phase | Work | Effort |
|---|---|---|
| A. Server | Class, 8 types, route, cross-investigation refs, `supervision:read`, exclusions, tests, deploy | 3–4 d |
| B. Plugin | Rate engine replacing `evaluate`/`earned_hours`; `--set --plan`; `reviewed` tags and marks; event writer with budget.json as cache; `budget pass`, `budget calibrate`; 2× backstop; skill rewrite to budget; rebase on main (#220–#222) and bump above claude 0.56.3 | 3–4 d |
| C. Fixtures | Wingbeat regression and healthy-session backtest | 1.5 d |
| D. UI | Supervision panel | 2–3 d |
| E. Bloat review | Independent review, cuts, re-test | 0.5 d |
| F. Pilot | A few weeks; about 15 min of labeling per firing | Calendar |

**Reused from `feat/supervise-stepback`**:

- the threshold-free opening step back;
- routing (owner constraint → owner, worker choice → `--type question`, on track → nothing) and the
  never-repeat rule;
- the read-only `cardinal:stepback` agent and its hook skip;
- the Gemini policy and `-e none`;
- the Codex isolation flags;
- the Cursor limit in docs/parity.json;
- the budget CLI shell: lock, 0600, `active_hours`, the worker-event filter, the spool union,
  `--since-seq`, `--set`/`--amend`/`--close`, history and the load shape check.

## 9. Owner decisions (accepted 2026-10-15)

| # | Decision | Accepted |
|---|---|---|
| 1 | Starting T | **2×** (bounds 1.5–3×). 1.5× fired at #255, with less to judge from; 3× came after the rework began. |
| 2 | Zero-progress share Z | **0.5** (bounds 0.3–0.8). |
| 3 | Re-fire rule | **As specified.** It gives 4 firings in wingbeat's 6.1 h. Add a 1-active-hour cooldown per direction only if the pilot shows clustering. |
| 4 | Same-person visibility caveat | **Accept and document.** No session-level access control. |
| 5 | Tagging cost in the review loop | **Required on every commit**, with ranges and `all:<m>`. 99 of 103 wingbeat events took a single tag (6 of them `none`). Without tags, rates blur (§2). |

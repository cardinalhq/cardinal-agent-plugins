# Supervise: step backs on a work budget

Status: approved design, 2026-10-15. Plugin only (shared core + supervise skill, all four
runtimes); no server change. Builds on relayed owner decisions
(docs/specs/relayed-owner-decisions.md): a step back that questions an owner constraint ends,
if the owner changes it, in a `decide:` or an approved draft.

## Why

The supervise review loop checks each claim against its evidence. It cannot see that every
claim is right while the approach is bigger than the question needs. Evidence: the wingbeat
investigation inv_cab0d4ba690509cd646915c2 (343 events, ~16.5 h). 68 of 92 supervisor posts
were per-claim "upheld"; two approach-level concerns were withdrawn after one decline; ~6 h
went into S3 claims, barriers and an in-binary autoscaler before the owner replaced them with
Postgres worklanes (#336), and a scratch-accounting layer was revised five times before a
REWORK (#208-#330).

Backtest at #199 (record cut there; a fresh agent given only a facts brief and the code at
8b51c326): it found the accounting simplification (the four separate budgets were required by
nothing; one rows-per-chunk cap through the existing advance/publish/cleanup path meets the
bound), i.e. before the five revisions. It found the existing Postgres claim/lease worker but
excluded it because the owner had said "no lease database", without pricing that constraint
for the owner. Hence: a step back must price every owner constraint.

A careful estimate for the same phase (reference class from the repo's history, milestones,
separate unknowns) was P50 13 h / P80 18 h, against a ~4 h guess from the question text and
~6.1 h actually spent before the REWORK. A spend-only trigger at an honest P80 fires too late;
progress against spend does not.

## Design

### 1. A budget per direction

When supervision starts, and whenever the owner gives the worker a new direction (a new
question, a decision that changes scope, a pivot the worker records on the owner's
instruction), the supervisor writes a facts brief and runs two fresh-context passes on it in
parallel:

- **Estimator**: milestones, each with P50 and P80 active hours and a one-line basis; the
  reference class used (comparable past work from the repo's history and records, and the
  owner's earlier investigations where readable) or "none comparable" with a wider range; the
  known unknowns, each with its own range, kept out of the totals. Never the worker's own
  estimate as input; if the worker stated one, the supervisor shows it alongside.
- **Opening step back** (section 4), before any of the new direction's work is spent.

The supervisor records the budget (`investigation budget --set`) and tells the owner, in at
most two lines, the range and its basis, plus any step-back question for the owner. The owner
may correct the budget (`--set` again). The worker never sees the budget.

### 2. Progress: earned against spent

- **Spent**: the worker's active hours since the budget was set: the union of the
  timestamps of the worker session's events in the investigation and, when the worker's
  activity spool is on this machine, its captured tool calls; a gap over 30 min between
  consecutive timestamps is idle and not counted.
- **Earned**: the sum of the P50 hours of the milestones marked done. A milestone is marked
  done only with a ref proving it (`--done <milestone> --ref "#<seq>"` or `--ref ev_…`). A
  milestone can be marked partly done (`--done <m> --fraction 0.5 --ref …`) only when the
  record shows a measurable part (e.g. 2 of 4 corpora).

### 3. Triggers (`investigation budget --check`)

Fire one step back when:

- **lag**: spent ≥ 15% of the P80 total AND earned < ½ × spent; re-fire only after spent has
  grown by another 50% since the last firing while the lag persists; or
- **backstop**: spent first crosses 1×, 2× or 4× of the P80 total.

Thresholds (0.15, 0.5, 1.5, the backstop multiples, the 30 min idle gap) are stored in the
budget file with the budget, so a later calibration can change them per budget. `--check`
prints spent, earned, their ratio, the next trigger point, and either `FIRE <reason>` or
`ok`; it records the firing so the same crossing never fires twice.

### 4. The step back

A fresh-context pass (never the supervisor's own context, which has read the stream) gets
only the brief and the code:

- the owner's current question, plainly;
- established facts that matter for the design, each with its `#seq`;
- constraints, each tagged with who set it (owner / worker's own choice / policy) and its
  `#seq`;
- the worker's current plan: its components and mechanisms;
- where the code is (and the commit).

No suggestions in the brief. The prompt:

> An elegant solution is often hiding in plain sight. Take a step back. Knowing only this
> brief and the code, what is the simplest design that answers the question? Search the code
> for mechanisms that already do part of the job before inventing one. Then compare it with
> the worker's plan: for each component the plan has and yours does not, name the constraint
> or fact that requires it (by #seq), or say that nothing does. Then, for each constraint the
> owner set: what would the simplest design be without it, and what does keeping it cost
> (components, risk, time)? Do not drop an owner constraint; price it, so the owner can
> decide. If the worker's plan is already about as simple as it can be, say so; that is a
> good answer. Say what you could not verify.

At a lag or backstop firing, the pass also gets the milestone status (done, open) and is asked
first whether the overrun is ordinary (most milestones done: answer "on track" with why).

### 5. Routing

- A component that exists only because of an **owner** constraint, or an owner constraint
  with a material price: ask the owner in the supervisor's terminal, with the cost named. If
  the owner changes it: `decide:` or a draft they approve.
- A component the **worker** chose, or an existing mechanism it missed: `--type question` to
  the worker: `Step back (<trigger>): <the simpler design; existing code at file:line; what it
  would remove; what could not be verified>`.
- Nothing to remove, or "on track": nothing posted; one line in the supervisor's summary.

One step back per firing. Never repeat a point the worker declined or the owner overruled
unless new evidence arrived. If the worker declines a step-back point and the supervisor still
believes it, it takes it to the owner once, with the worker's argument attached, instead of
withdrawing it.

### 6. Close and calibrate

When the direction's work ends (done, stopped, or replaced by a new direction),
`investigation budget --close --outcome done|stopped|replaced` records actual spent against
the estimate, per milestone, in the budget file's history. The estimator reads earlier closed
budgets for the same repo (from `~/.cardinal/supervise/*/budget.json`, this machine) as part of
its reference class.

## CLI: `cardinal-storyboard investigation budget`

```
investigation budget <inv> --credential-file F --budget-file B --set --question "<one line>"
    --milestone "<id>:<P50h>:<P80h>:<title>" ... --basis "<reference class; unknowns>"
    [--worker-session SID] [--worker-estimate "<what the worker said>"]
investigation budget <inv> ... --done <id> --ref "#<seq>"|ev_… [--fraction F]
investigation budget <inv> ... --check [--json]
investigation budget <inv> ... --show [--json]
investigation budget <inv> ... --close --outcome done|stopped|replaced
```

- Budget file: private (0600, the supervise directory's `budget.json`), flock'd; holds the
  current budget (question, milestones, basis, thresholds, set_at seq/time, worker session,
  firings, done marks with refs) and a bounded history of closed budgets.
- `--set` closes the current budget as `replaced` first (with its actuals) when one is open.
- Milestone ids `[a-z0-9-]{1,32}`; 1-20 milestones; hours > 0, P80 ≥ P50.
- Spent reads the worker's events with the grant (read scope) and, when available, the local
  spool via the evidence module's public listing, never by opening spool files directly.
- Output is inert text (no server string interpreted); `--json` for the skill.
- Standard library only; read-only on the server.

## Runtimes

The CLI and the skill text are shared. The fresh-context passes:

- Claude: a subagent (Agent tool) given only the brief file path and the code path.
- Codex, Cursor, Gemini: their headless mode, read-only, with the brief (`codex exec`,
  `cursor-agent -p`, `gemini -p`), if verified to work from inside a session; otherwise that
  runtime ships the budget and triggers and tells the owner the step back needs a Claude
  supervisor (recorded in docs/parity.json).

## Tests

Budget file (perms, lock, validation, set-replaces-and-closes, history bound); spent
(idle-gap rule, union of events and spool, worker-session filter, no spool); earned (refs
required, fraction); triggers (lag threshold, re-fire after +50%, backstops, never twice,
nothing before 15%); `--check` output; `--close` actuals; a fixture replaying the wingbeat
#197-#330 timestamps with the backtest's milestones to show the lag trigger fires before #336.

## As built (2026-10-15): deviations and clarifications

- **`--since-seq SEQ`** (added to `--set`): count the worker's time from the new direction's
  first event (its created_at is the spool cut-off). Without it, counting starts after the
  investigation's head at `--set` time. A supervisor usually notices a new direction a few
  events late; this keeps those events in.
- **Worker events**: the worker session's semantic and control events (`producer.session_id`
  = `--worker-session`); never owner input, owner decisions or a grantee's posts. Without
  `--worker-session`, the investigation author's session events; then the spool is not read.
- **One firing per check**: a lag and a backstop crossed in the same `--check` are one firing
  (`FIRE backstop Nx + lag`); crossing several backstop multiples at once fires once and
  marks them all. After a firing, `--check` prints the next trigger points past it.
- **Refs**: `--done` takes `#<seq>` or `ev_<12 hex>` only.
- **Offline modes**: `--done` and `--show` touch only the budget file (`--show` needs no
  investigation or credential, so earlier budgets can be read for the reference class).
  `--set`, `--check` and `--close` read the worker's events with the grant.
- **Per-milestone actuals** (`--close`, and `--set` replacing an open budget): the worker's
  cumulative active hours when each done mark's ref happened, minus the previous mark's (in
  ref order); open milestones have none.
- **Estimator reference class**: the supervisor appends the closed budgets (`--show` of each
  `~/.cardinal/supervise/*/budget.json`) to the estimator's copy of the brief, since a
  read-only headless pass may not run commands.
- **Runtimes, verified on 2026-10-15 from a shell (not yet from inside a running session of
  that runtime)**: Codex `codex exec --sandbox read-only --ephemeral -C <checkout> - < brief`
  (codex-cli 0.162.1: reads and searches; a write was refused). Gemini `gemini --skip-trust
  --approval-mode default -p "…" < brief` (gemini-cli 0.64.0: reads and searches; a write was
  refused). `--approval-mode plan` is **not** read-only headless: it left plan mode and wrote
  the file. `--skip-trust` is needed in an untrusted folder. Cursor: `cursor-agent` was not
  installed, so unverified; the Cursor supervisor estimates in its own context (saying so in
  `--basis`), keeps the triggers, and tells the owner the step back needs a Claude (or Codex /
  Gemini) supervisor. docs/parity.json `supervise_stepback`: Cursor is a limit.
- **Replay**: the fixture holds the worker's event times for #197-#336
  (core/tests/testdata/wingbeat-phase-f-worker-times.json). With the careful estimate (P50 13 h
  / P80 18.5 h) and only `design` done (#202), the lag trigger first fires at **#288**
  (10:59Z, 4.10 active hours; earned 2 h), before the REWORK (#330) and the owner's pivot
  (#336); it re-fires at #336 (6.15 h).

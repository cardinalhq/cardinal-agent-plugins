# Cumulative architectural compatibility: implementation and replay

Implemented and validated locally on 2026-10-15 America/Los_Angeles. This report
records pre-release validation; the subsequent owner-requested release is tracked
separately. Validation performed no installed-plugin update, production change or
Storyboard publication.

## Minimal implementation

`common/supervise/CORE.md` adds cumulative compatibility to the existing material
review, at opening/recovery or a requirement, design or evidence change. Routine
events keep their existing reviews. The supervisor retrieves InvestigationState,
refreshes a local copy, reconciles authored commitments and reads pinned source
decisions. It compares cumulative design with the objective and authorized
amendments, checks scaling dependencies, asks necessity before optimization, and
reports Progress / Architectural delta / Compatibility / Recommendation.

Conflicts receive REDIRECT/STOP with the smallest correction and the owner
decision needed for a contract exception. Compatible work continues. Existing
verdicts, acknowledgments, rulings, authority boundaries and budget step backs
remain intact. A supervision grant does not authorize overwriting worker state.

`core/cardinal_core/investigation_state.py` extends the existing authoring guide:
use constraints, rationale, scoped exceptions, supersession and versioned document
references as the commitment index. No schema, receipt taxonomy, runtime state
machine, database, product agent, DSL or memory subsystem was added. The four
adapter copies of the shared supervision core were synchronized.

## Frozen validation inputs

Original investigation: `inv_cab0d4ba690509cd646915c2` / `sb_4013a113619d1acf17681ef4`.
All source documents are exact Git blobs from Wingbeat, not current working-tree
files. State files are replay-authored v1.1 indexes and pass the existing checker;
they are not historical server snapshots or verified owner attestations. Earlier
architecture is supplied in the pinned decision/design documents; each history
contains the complete recent direction beginning at #750, rather than all earlier
events. Captured activity and receipt bodies are unavailable in these packets.

A final provenance audit found a labeling error in the replay-authored state:
its document references say `cardinalhq/wingbeat`; the actual source remote is
`cardinalhq/cardinal-wingbeat`. Every frozen document was rechecked byte-for-byte
against its recorded commit and path in that actual repository and matched.
The original evaluated packets remain unchanged; the correction is recorded
[separately](/Users/ruchirj/workspace/artifacts/architectural-supervision-20261015/source-provenance-correction.json).
Evaluators resolved references only to the supplied files, so no wrong repository
was queried, but these inputs should not be described as error-free source indexes.

| Case | History available | Newly presented event | Source commit | Result |
|---|---|---|---|---|
| A | #750–#779 | #780, producer integration and census selection barrier | `da0a610ffc9f5a9dd4cc5e477d01a322d08e7a51` | REDIRECT |
| B, bounded control | #750–#764 | #765, seven-file AWS consumer-policy validation | `a8a04880dad87a5e5309f37bc81fcd29c7b92b8e` | CONTINUE |
| C, evidence interpretation | #750–#803 | #804, admission counterexample | `c9d5e361506e9dc65dd424355d5dfdef58efa0db` | CONTINUE scoped assessment |

Each case used a separate evaluator with `fork_turns=none`. The evaluator received
only a neutral task pointing to its frozen packet, the revised supervision guide,
state/module, source documents and event records. It was instructed to read no
other files, use no network/live tools, make no changes and infer no later outcome.
The same prompt form was used for all cases; it supplied no expected verdict or
incident critique. This is conversation/input isolation, not an OS-enforced
filesystem sandbox. Each evaluator reported invoking the supplied state refresh.
No implementation or input tuning occurred after the results.

Per-file SHA256 manifests, contiguous sequence checks and source-commit timestamps
verify the frozen artifacts. The early packet contains neither #804 nor the later
31-hour result or this session's critique. Scoring criteria were saved separately
from the packets before the parent received the results.

## Results and deciding evidence

Case A flagged the requirement at #780:

> Partial work cannot be selected until the census completes.

Its recommendation was “REDIRECT the census barrier before adopting it; continue
compatible bounded integration.” It identified:

- A potential census-completion selection barrier coupling healthy lanes to slow
  or failed lanes, and delaying the first useful result.
- Threats to classify-once incremental rollups and independent progress, citing
  the tally-producer proposal §§4–5, bounded-window amendment and incomplete-first
  decision.
- An unresolved cheaper alternative: per-node progress, exact-H coverage checks
  and explicit missing/uncertain outcomes. It requested the census population,
  bounds, failure behavior and correctness claim before optimizing it.
- A specific owner decision before adopting a necessary global architectural
  exception; removing an unnecessary barrier fits existing approval.

The evaluator correctly called these risks requiring clarification, not verified
implementation defects. #780 does not specify the census population or prove it
is global/all-history. This validates early potential-conflict detection, not an
independent finding that the implementation had those properties.

Case B allowed the legitimate bounded validation stage to continue and imposed no
new owner decision. It retained qualification limits and noted earlier unresolved
cost concerns without stopping the scoped operation. This is one useful control,
not a false-positive-rate estimate.

Case C recognized that the constructed admission test refutes a universal receipt
timestamp relation but establishes neither production prevalence nor the necessity
of an all-history census. It asked whether existing authenticated metadata could
supply the inventory before optimizing historical extraction. It also respected
the already-recorded capture amendment and continued only scoped sidecar assessment;
it did not silently reverse approval or recommend an unsafe receipt shortcut.

## Temporal and approval limits

The combined requested criterion cannot honestly be tested at #780: the synthetic
test in the feedback is #804, recorded later. It was excluded from A and evaluated
separately in C. The record also places capture-metadata/backfill approval before
#804, in the `c9d5e361` owner document and #802. That amendment allows a one-time
capped historical extraction where immutable inventory is incomplete and permits
a simpler complete selection method; it does not make every history-scaled
operation an unauthorized conflict.

The early evaluator flagged the potential barrier at #780, before that later
recorded amendment at #802. There is no explicit census-barrier approval in A's
supplied documents. This establishes detection before the later recorded
capture/backfill approval, not proof that no unseen owner authorization existed
or that an actual supervisor intervened before implementation started. The replay
made no live posts and changed no historical work item.

These are three single record/document-only reviews. Test results and owner words
inside historical records were not independently verified against their receipts
or transcripts. There was no baseline comparison, production validation or broad
detection-rate measurement. The supported claim is: the revised supervisor flagged
a potential conflict with architectural intent in this temporally isolated case.
Autonomous architectural governance is not established.

## Checks and artifacts

- Existing InvestigationState/state-sync/supervision suites: 218 tests passed.
- Repository contract tests: 4 passed.
- Shared-skill synchronization and `git diff --check`: passed.
- Skill validation: Codex, Cursor and Gemini passed. The Codex skill validator
  rejects Claude's pre-existing `argument-hint` frontmatter; that unchanged
  adapter-specific field was retained. Initial missing PyYAML and test-import-path
  issues were resolved in a temporary validator environment / correct test path.

Frozen packet manifests:
[A](/Users/ruchirj/workspace/artifacts/architectural-supervision-20261015/case-a/manifest.json),
[B](/Users/ruchirj/workspace/artifacts/architectural-supervision-20261015/case-b/manifest.json),
[C](/Users/ruchirj/workspace/artifacts/architectural-supervision-20261015/case-c/manifest.json).
Evaluator responses (prose retained; source links relocated to saved packets):
[A](/Users/ruchirj/workspace/artifacts/architectural-supervision-20261015/case-a-review.md),
[B](/Users/ruchirj/workspace/artifacts/architectural-supervision-20261015/case-b-review.md),
[C](/Users/ruchirj/workspace/artifacts/architectural-supervision-20261015/case-c-review.md).
[Manual scoring and hashes](/Users/ruchirj/workspace/artifacts/architectural-supervision-20261015/results.json).

Source worktree: `/Users/ruchirj/workspace/cardinal-agent-plugins-stepback`,
validation base `1804bee88ee0e198b93c4b1968948a9a1b9943f5`. The validated changes
were subsequently prepared for release on `feat/supervise-architectural-compatibility`
from `origin/main` (`77f9990`); the relevant source files match the validation base.

# Intent coordination validation — October 21, 2026

The owner requested intent-based coordination for both supervisor and worker,
removing the fixed tool/action taxonomy. Shared sync/wait now accepts arbitrary
purposes and durable exact prerequisites; new requests contain intent instead
of actions. Ordinary tools pass through. Existing feedback review, lifecycle
identity, exact acceptance, cancellation and verified grant succession remain.

Backend focused validation passed **539 tests**, including real PostgreSQL
intent-only persistence/constraints and existing lifecycle/concurrency/lineage
checks. Typecheck and gateway protocol checks passed. Two initial capability
expectations were updated before the successful full focused rerun.

The final focused core suite passed **67 tests**. An earlier broad core run
passed 1,216 tests before the final multi-consumer fix; final-head CI remains
authoritative. Codex investigation/connect checks passed **73 tests** and the
cross-adapter contract/parity suite passed **35 tests**. Initial direct module
invocations had existing bare-test-module import errors; reruns used the test
directory on PYTHONPATH. The Claude broad run executed 716 tests with two failures
and three skips; it is not a full pass. One failure was a stale registered-hook
inventory, corrected for the removed coordination registration. The unchanged
owner-input refusal test passed on focused recheck alongside the inventory checks
(four tests); the two changed Claude Investigation modules passed 87 tests.
Final-head CI remains required.

Independent backend review found no source blockers and documented that old
clients fail closed on intent-only requests. Upgrade participants sharing those
Investigations; deploy backend/migration before new clients. Plugin review found
and verified the fix for multiple-consumer feedback starvation. Synchronization
now checks every consumer and prioritizes feedback, attention and errors over
waiting, including handled blocked progress alongside another completed result.
No remaining source blocker was reported; runtime/CI acceptance is separate.

The first installed-runtime attempt is preserved under private
`coordination_gate_phase3/runs/intent-20261021-r1/`: Codex requester passed 44/44;
Claude requester failed its case-marker locators despite all four server-recorded
request lifecycles completing. The initial barrier also expected a bare outcome
where an agent passed a full marker. The harness now uses exact request IDs and
unambiguous literal purposes, with additional identity and decision-after-ready
assertions. Both fresh role-direction reruns are pending under `intent-20261021-r2/`;
they are not yet claimed as passed. Product artifacts are unchanged.

The first local docs build used stale dependencies after fetching newer main and
failed on its newly added Mermaid dependency. After frozen-lockfile installation,
the production build passed, including TypeScript/static export/search indexing.
Public docs PR150 build-and-test CI passed. The initial build remains recorded as
failed, rather than retroactively counted as a pass.

Candidate versions are Codex **0.40.0** and Claude **0.62.0**. No intent extension
release/deployment or authenticated production acceptance is claimed here yet.
Waiting is bounded incremental reading/backoff, not server push or indefinite
host parking. Functional acceptance does not establish fleet/performance claims.

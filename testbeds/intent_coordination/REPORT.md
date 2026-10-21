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
assertions. Both fresh role-direction reruns under `intent-20261021-r2/` passed **52/52**
each, with both harnesses exiting zero after cleanup. Independent review verified
exact principal/session/request references, timeout-before-reply, attention before
resolution, decision notes after ready, native work between blocked and complete,
zero hook denials and owned binding cleanup. Tested and final artifacts match all
bytes and modes (Codex119files, Claude155files); product artifacts are unchanged.

The first local docs build used stale dependencies after fetching newer main and
failed on its newly added Mermaid dependency. After frozen-lockfile installation,
the production build passed, including TypeScript/static export/search indexing.
Public docs PR150 build-and-test CI passed. The initial build remains recorded as
failed, rather than retroactively counted as a pass.

Final plugin CI at `37d6d530229181888605bf027a85d9b51109ef0b` passed all checks:
core1,218 on Python3.9 and3.12, Claude716 (3 skips), Codex227, Cursor186 and
Gemini192 (3 skips), plus parity, behavior, import checks and native packaging.
This is the authoritative final functional-source run; the present report update
changes no executable or artifact files.

An initial unrestricted backend `make check` failed31files/230tests with10,061
passed. It exposed two stale security-test fixtures: the historical C1 event
registry included the three newer request types, and a privacy race observer
matched an obsolete SQL projection. Both are corrected without changing production
behavior or relaxing assertions. Many other failures were real-PG10s setup timeouts
and a shared-process Canvas RSS threshold. The supported `VITEST_MAX_WORKERS=2`
rerun passed Maestro (including real PostgreSQL/Canvas) and every JavaScript suite;
Go vet/tests and aggregate completion remain pending at this pre-release cutoff.
Both the original failure and the rerun logs remain separate evidence.

GitHub's October21 webhook incident delayed push deliveries. An older delivered
push superseded the reviewed newer run; restarting that existing source pipeline
preserved exact commit and protected push contexts. Temporary-branch skipped image
copy and manual diagnostic results must never count as original PR image-copy
acceptance. Final backend CI, exact PR/release image, full rollout and authenticated
production acceptance remain required.

Candidate versions are Codex **0.40.0** and Claude **0.62.0**. No intent extension
release/deployment or authenticated production acceptance is claimed here yet.
Waiting is bounded incremental reading/backoff, not server push or indefinite
host parking. Functional acceptance does not establish fleet/performance claims.

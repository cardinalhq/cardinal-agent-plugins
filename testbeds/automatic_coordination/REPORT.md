# Automatic coordination validation — October 20, 2026

Implementation adds two simultaneous checks with no default-off configuration:
handle new authenticated feedback; retain explicitly declared request dependencies.

## Functional results

Installed Claude requester / Codex responder passed every assertion in
`coordination_gate_phase3/runs/automatic-20261020-r1/claude-requester/result.json`.
Installed Codex requester / Claude responder passed every assertion in
`coordination_gate_phase3/runs/automatic-20261020-r2/codex-requester/result.json`.
Private run directories preserve versions, invocation arguments, candidate
artifacts, audits, state and final results; model reasoning is not published.

Both runs demonstrated automatic first-edit denial with no mode/policy/enable,
explicit chunk reads and hash review, attributed witness delivery and same-session
acknowledgments. For complete, blocked, abstain and reject, dependent commits were
denied both before a response and after the response but before resolution.
Only exact-result acceptance or explicit cancellation released replacement
commits. A native unrelated edit occurred while blocked, before later completion.
Participant identities and accepted response sequences matched server records.

These runs use actual Conductor router/reader/guard code with fixture auth/storage.
They do not establish production authentication. Candidate r3/r4 artifacts preceded
final fixes to scoped-consumer copy retirement and lexical installed-path matching;
those changes have separate regression tests and require final deployed acceptance.

## Failed attempts and regression evidence

The first Codex-requester run preserved its failed result. Coordination assertions
passed, but workspace-write prevented `.git/index.lock`, so all replacement commit
checks failed. The rerun used an invocation-only full-access sandbox in fresh
repositories; no normal runtime configuration or authentication was replaced.

The final focused plugin suite passed 73 tests. Backend `make check-coordination`
passed 533 route/registry/lifecycle/real-Postgres tests, including cycle concurrency,
terminal-result races, exact retries, canonical identities, grant succession,
quota continuity and predecessor proof. Typecheck and Go gateway contract checks
passed before the final test-only capability updates. Docs production build passed.

A local full core run passed 1,219 of 1,222 tests and failed three existing wall-clock
speed bounds during concurrent broad backend testing; it is not recorded as a
passing full suite. A broad local `make check` was interrupted after contention;
its completed type/build checks do not establish a full pass. Adapter broad tests
exposed obsolete permanent-guidance expectations, corrected to the new automatic
rule. Final required CI against the final commit remains authoritative.

## Release status

Backend PR2159, plugin PR233 and docs PR147 are open. Backend protocol deployment,
authenticated HTTP/MCP lifecycle acceptance and paired installed-runtime acceptance
against real storage remain pending. Publish adapters only after backend migration
and every serving replica support request protocol 1. Broad fleet/performance
acceptance is separate work and is not claimed by these functional results.

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
those changes have separate regression tests and were subsequently covered by
the authenticated production acceptance below.

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

## Authenticated production acceptance

The final r6 artifacts passed **58/58 assertions in each role direction** against
production authentication and durable storage, using the installed Codex and
Claude CLIs. Both harness processes exited zero after owned binding/outbox cleanup.
Normal configuration fingerprints were unchanged. No mode variable, manual policy,
or enable command was used. Private evidence remains under
`coordination_gate_phase3/runs/automatic-live-20261020-r1/`:

- Codex requester / Claude responder: `codex-requester/result.json`,
  Investigation `inv_41dde61074e27fa4884832e1`.
- Claude requester / Codex responder: `claude-requester/result.json`,
  Investigation `inv_1856226092c9ea9014ce66f5`.

Both runs verified all four outcomes, post-response/pre-resolution commit denial,
exact complete-result acceptance, explicit cancellation after abstain/reject, and
unrelated native edits between blocked and complete. Immutable server records
contain exactly four complete request lifecycles per pair; Git histories contain
only baseline and four replacement commits. All four owned bindings were archived
and removed. An independent reviewer verified the evidence and found no blocker
for the declared direct CLI paths. Both final vendored core trees byte-match the
reviewed source; the r6 payloads also match the release dry runs byte-for-byte and
mode-for-mode (Codex 119 files, Claude 155 files).

A separate deployed HTTP/MCP suite passed all 19 checks, including wrong-actor
refusal, cycles, all outcomes, authenticated grant succession, unrelated-lineage
refusal and MCP lifecycle reads. Every test request was resolved; all temporary
grants were revoked. This acceptance uses real server authorization/storage,
superseding the earlier fixture-only limitation for these functional scenarios.

## Release record

[Backend PR2159](https://github.com/cardinalhq/conductor/pull/2159) merged as
`2a21911a1b19a70300a36e5d3c3aed2a1eb0a5b1`; its tree equals the reviewed head.
[Maestro v1.125.0 release](https://github.com/cardinalhq/conductor/actions/runs/39533401217)
succeeded. [GitOps PR2431](https://github.com/cardinalhq/kubernetes-clusters/pull/2431)
merged as `85732411cb5bb0d0b5332ec39b2aa4533fa9fb7f`. Argo was Synced/Healthy at
that revision, both replicas and all four app/gateway containers ready with zero
restarts, and both gateway logs confirmed migrations applied and database ready.
Every container uses image index digest
`sha256:79ed77fdbe9154852cf18f82b6d3f4f62f1bb9eaaa06f84d8e0a5000d2f0020d`.
An independent reviewer rechecked deployment and the 19-check acceptance artifact.

[Plugin PR233](https://github.com/cardinalhq/cardinal-agent-plugins/pull/233) merged
as `f19fa7e55e17c374b465deac83d316d652d22551`, with exactly the reviewed tree at
`650803f5033ac8962435129507f5e5f340ee5538`, after deployed acceptance completed.
All required final-source CI passed, including full core Python 3.9 and 3.12.
The [standard mirror release workflow](https://github.com/cardinalhq/cardinal-agent-plugins/actions/runs/39535070541)
tracks publication of Codex **0.39.0** and Claude **0.61.0**. Publication
completion must be established from its completed result and the mirror refs;
this acceptance report alone does not establish publication completion.
[Public docs PR147](https://github.com/cardinalhq/cardinal-docs/pull/147) documents
the same automatic behavior and supported scope.

Broad fleet/performance acceptance remains separate and is not claimed here.
Desktop, arbitrary wrappers, nested JavaScript and continuing interactive stdin
remain outside validated enforcement. The runtime can fail open on a true hook
crash or timeout; bounded operational failures produce supported deny responses.

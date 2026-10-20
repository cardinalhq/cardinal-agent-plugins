# Phase 3 runtime validation — 2026-10-20

The runtime adapters and delivery coexistence are implemented. Local integration
uses installed Codex CLI 0.162.1 and Claude Code 2.1.304, without model overrides.
Authenticated acceptance against the deployed server subsequently passed during
the release on October 20, 2026; see the release follow-up below. The original
local run and its limits are retained separately.

## Implementation and scope

Source is in the `codex/coordination-gate-engine` worktree, based on plugin
revision `c4ef0dd4907c7859715fe92f40151dc7cbf4148c`. Phases 2–3 were merged
in [plugin PR231](https://github.com/cardinalhq/cardinal-agent-plugins/pull/231)
at `95b22c50d00d70f791a0490b5db223d878dc88c0`. See the [runtime specification](../../docs/specs/coordination-gate-runtime.md)
for registration, readiness, durable activation, delivery ownership and exclusions.

The testbed runs the production hook entry points and shared engine against the
Phase 1 Conductor checkout's actual HTTP event router, acknowledgment validation,
verification write route and fixed-head reader. Authentication, capability
metadata and memory storage are fixtures. It does not validate deployed
authentication, actual grant issuance or Postgres behavior. Phase 1 backend tests
are separate evidence.

Every case first exercises the required native transports and activates through
a real pre-tool receipt. Worker changed-path cases require an actual denial,
absence of the original effect, all chunk hashes, a same-consumer acknowledgment,
explicit review, a replacement action and a random peer witness in model output.
Supervisor cases review a worker semantic event without acknowledging it; only
a revised verdict may reach the real verification route. A successful model exit
alone is insufficient.

## Installed-runtime results

Final main run: `runs/20261020T110640-2c1111/`.
Additional Claude native edit run: `runs/20261020T110641-f4fb09/`.
Raw artifacts are local and ignored by Git; prompts, argv, versions, JSONL,
adapter output audits, server requests, state and checks are retained per case.

| Case | Codex | Claude | Required behavior |
|---|---|---|---|
| Worker commit reconsideration | Pass | Pass | Original commit absent; review and newly chosen action succeed |
| Native patch / Write | Pass | Pass | Original target absent while pending; full handling before replacement |
| Native Edit | Not in declared Codex transport | Pass | Original target unchanged while pending |
| Native NotebookEdit | Not in declared Codex transport | Pass | Original cell unchanged while pending |
| Supervisor verdict reconsideration | Pass | Pass | Premature verdict denied; exactly one revised verdict recorded |
| 100 separate covered quiet calls | Pass | Pass | Fresh snapshot per call, distinct effects, no denial, zero hook stdout |
| Read failure after activation | Pass | Pass | Supported denial, no original effect, no handling advancement |

All 12 final runtime cases passed. Each quiet case recorded 101 snapshot reads
(activation plus 100 checks), 100 distinct effects and zero quiet hook stdout.
Final cleanup removed five Codex-created fixture project-trust tables.

Changed-path feedback in the final cases is at most 590 UTF-8 bytes,
including the adapter's handling instruction, below the 1,000-byte cap. This is
the denial reason size, not a claim about total model context or token savings.
Quiet calls use a harmless gh-shaped fixture executable registered explicitly
in policy; they do not merge real pull requests. Commit effects are confined to
disposable repositories. Real `git push`, production PR merges and unlisted
command forms are not claimed as live side-effect tests.

Codex's tool interface in this installed run uses the Phase 0 transparent,
uncaught code-mode transport. Catching or suppressing a rejection, arbitrary
JavaScript effects, interactive stdin, unlisted wrappers and nested command
equivalents remain uncovered. Actual hook crashes, imports, malformed output or
outer timeout may fail open; caught engine/read failures return valid denial.
Desktop and interactive UI coverage is unvalidated.

## Regression and instruction-size checks

- Core suite: 1,208 tests passed.
- Codex investigation adapter suite: 15 tests passed.
- Claude adapter suites selected for this change: 90 tests passed.
- Repository packaging/contract suite: 35 tests passed; parity checks: 10 passed.
- Focused gate/runtime suite: 57 passed; guidance suite: 22 passed.
- Harness targeted project-trust cleanup: one test passed.

Regression checks cover stale/missing transport receipts, exact one-use enable
commands, explicit endpoint credential isolation, interrupted activation,
old/requested coverage during policy transitions, missing active engine state,
bounded denial, legacy queue retention, poller ownership, disable/resumption,
different Investigation/runtime delivery and implicit supervisor credentials.
Engine tests separately exercise fixed-head pagination, chunk replay, required
post retry, concurrency and persistence faults. These are deterministic tests,
not a substitute for every Phase 4 runtime fault scenario.

Generated guidance samples: Codex 4,727 → 3,064 UTF-8 bytes; Claude 4,389 → 2,843.
Non-enabled guidance retains its existing text. These samples do not establish
token savings, latency targets or fleet capacity.

## Fixture failures and cleanup

Early attempts exposed the installed Codex sandbox's refusal to run `ps` from
the CLI. Readiness now checks ancestry in the unsandboxed hook and consumes the
exact enable-command receipt in the sandboxed CLI. Other attempts caught the
implicit supervisor credential scope needing refresh after route resolution;
the final supervisor cases cover that correction.

Run `20261020T105649-dbd535` completed its 100-call gate assertions but failed
the original strict configuration fingerprint check: Codex itself persisted
project-trust records despite invocation-scoped trust options. That run is
retained as a failed fixture-isolation result, not counted as a passing run.
The harness now removes only its disposable project tables, preserves unrelated
configuration and checks other configuration semantically plus hook/settings
files byte-for-byte. Cleanup has a regression test. No whole-file snapshot is
restored over user changes. Earlier failed attempts remain in ignored `runs/`.

Normal installed hooks, CLI authentication, HOME and CODEX_HOME were not changed
by the testbed. Unique fixture bindings are archived then removed. Runtime gate
and route state lives inside the private test directories. Codex-created trust
entries are cleaned after each run; interrupted runs can use
`run.py --clean-project-trust`.

## Remaining acceptance

The deployed Phase 1 capability and authenticated direct CLI worker/supervisor
acceptance are complete, as recorded below. No alternative endpoint was needed.

Phase 4 still requires at least 1,000 quiet checks per runtime on the declared
network, matched changed-path token/latency baselines, fleet-load measurements
and its broader runtime fault/recovery matrix. Default activation remains off;
the owner-enabled pilot is Phase 5.


## Authenticated release follow-up — 2026-10-20

The unified Maestro/MCP gateway release is `maestro/v1.124.0`, source
`4f5edf855da3f5a484bbf8792f7ea1b88625d36b` from
[Conductor PR2156](https://github.com/cardinalhq/conductor/pull/2156).
[GitOps PR2424](https://github.com/cardinalhq/kubernetes-clusters/pull/2424)
deployed it at `e61b3474cadc38dc256268a430a145d24bd89271`.
Independent review confirmed ArgoCD Synced/Healthy, two ready replicas, no old
replicas, and both ready native MCP gateway sidecars at the verified digest
`sha256:cc03c3cdd951a51a553fcf222c6805b179725a0723b7b5c91e4839e82`.
No migration, separate gateway deployment or server configuration change was
needed.

All 15 authenticated production HTTP/MCP checks passed: advertised version 1,
explicit zero with empty/nonempty global heads, fixed-prefix continuation with
intervening appends and a fresh read finding the later event, actual semantic
worker contributions, scoped supervisor identity/visibility, owner-input
exclusion, real aggregated MCP tool discovery/calls with the same pagination
proofs, scoped MCP rejection and revoked-grant rejection between pages. The
backend test grant was revoked.

The installed runtime cases used Codex CLI `0.162.1` and Claude Code `2.1.304`,
without model overrides, and the exact independently reviewed packaged Codex
`0.38.0` and Claude `0.60.0` artifacts. Actual author credentials stayed in
worker child environment; supervisor children received only their scoped grant
file. No fixture authentication, storage or event router was used.

| Authenticated case | Codex | Claude |
|---|---|---|
| Worker commit: denial → same-consumer acknowledgment → full review → replacement | Pass | Pass |
| Supervisor: premature verdict denial → full semantic review → exactly one reconsidered verdict → replacement marker | Pass | Pass |
| Peer witness in model output; original effect absent; ordered actions | Pass | Pass |
| Normal configuration preserved; credential files and bindings removed; grants revoked | Pass | Pass |

Each case recorded one actual denial with a maximum reason of 590 UTF-8 bytes.
The runs first probed the declared native transports (Codex Bash/apply_patch;
Claude Bash/Write/Edit/NotebookEdit) and consumed same-process readiness and
exact enable-command receipts. Worker runs are retained locally in
`runs/live-release-20261020-r2/{codex-worker,claude-worker}/`; successful
supervisor runs are in
`runs/live-release-20261020-r3/{codex-supervisor,claude-supervisor}/`.
Private raw artifacts remain ignored. Review agents independently checked
server rollout/acceptance, artifact contents and worker ordering/identity.

Two scratch harness errors are retained as failures: the first setup supplied
a session ID on a scoped-grant cue, correctly rejected with HTTP400 before
runtime launch; the first supervisor replacement used an unsupported verdict
scope, correctly rejected by CLI argument validation after handling. The
harness was corrected to omit the grantee session and use `fixture_validation`;
fresh sessions then passed. All temporary grants from failed and successful
cases were revoked. Neither error required product changes.

All plugin PR CI suites passed on the exact reviewed head before merge,
including Python 3.9 support, core and all adapter regressions. Backend release
checks passed on the exact merged tree, with 493 targeted TypeScript tests
including 18 real-Postgres tests and targeted Go MCP tests. Two duplicate local
Conductor `make check` attempts were interrupted under severe host overload;
they are not claimed as passes. Complete authoritative CI plus the changed-path
database checks formed the independently reviewed release gate. Full existing
Postgres integration breadth was not added to this acceptance claim.

Phase 3 authenticated acceptance is complete for the declared direct CLI
paths. Default gate activation remains off. Phase 4's 1,000 quiet checks per
runtime, matched token/latency measurements, fleet load and broader fault matrix,
and Phase 5's owner-enabled pilot remain outstanding. This release makes no
new runtime coverage, measured token savings or fleet-readiness claim.

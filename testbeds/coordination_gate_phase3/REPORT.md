# Phase 3 runtime validation — 2026-10-20

The runtime adapters and delivery coexistence are implemented. Local integration
uses installed Codex CLI 0.162.1 and Claude Code 2.1.304, without model overrides.
Authenticated acceptance against the configured deployed server remains pending:
its read-only capability response did not advertise `event_snapshot`.

## Implementation and scope

Source is in the `codex/coordination-gate-engine` worktree, based on plugin
revision `c4ef0dd4907c7859715fe92f40151dc7cbf4148c`. Phase 2 and Phase 3 changes
remain uncommitted. See the [runtime specification](../../docs/specs/coordination-gate-runtime.md)
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

Deploy Phase 1 to the configured backend, confirm its versioned `event_snapshot`
capability, and repeat the direct CLI worker/supervisor cases with actual author
and scoped supervisor credentials and durable backend storage. No alternative
endpoint is required merely to finish this local implementation.

Phase 4 still requires at least 1,000 quiet checks per runtime on the declared
network, matched changed-path token/latency baselines, fleet-load measurements
and its broader runtime fault/recovery matrix. Default activation remains off;
the owner-enabled pilot is Phase 5.

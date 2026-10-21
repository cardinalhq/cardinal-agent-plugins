# Automatic coordination acceptance

`run.py` launches installed Codex and Claude CLIs with two isolated candidate
artifacts, no mode variable, no policy file and no enable command. It uses the
actual Conductor HTTP router/reader/guard with fixture authentication and storage.
Fixture runs do not claim production authentication or database acceptance.
`--live` uses the existing authenticated Cardinal Cloud connection and actual
server storage in a dedicated acceptance Investigation after backend deployment.

Build artifacts with `build/release.py`'s `build_artifact` helper, then run once
with `--requester codex --responder claude` and once with the roles reversed.
Provide `--backend`, `--artifacts` and a fresh `--output` directory beneath
`testbeds/coordination_gate_phase3/runs/`. Model/runtime defaults are preserved.
Neither normal HOME/CODEX_HOME nor installed hooks/authentication are replaced.

Checks include automatic first-edit denial, two same-session acknowledgments,
explicit chunk reads/hash review, random witness delivery, and four request
outcomes. Dependent commits are probed before a response and after the response
but before acceptance/cancellation. Original probes must remain absent; replacement
commits must exist. An unrelated native edit must occur between a blocked reply
and its later completion. Participant identities and exact accepted response IDs
are checked against server records. Private artifacts include prompts, runtime
JSONL, audit, final state and `result.json`; do not publish model reasoning.

The harness stops/reaps children and removes only its own disposable bindings and
Codex project trust entries. Any cleanup failure prevents a successful run.
Real Postgres concurrency, renewed identity, quotas and deployed authentication
have their own backend/live checks. Historical Phase 3 preview runs do not count
as acceptance of this default-on behavior.

Codex uses an invocation-only `danger-full-access` sandbox for functional git
acceptance in the fresh disposable repositories. The earlier workspace-write
run retained its failed result: the hook checks passed, but Codex denied Git
index.lock writes and replacement commits could not complete. This adjustment
does not change normal user configuration or disable hooks.

Cleanup archives/removes only each prepared SID's owned request outbox after
validating its session and Investigation identity, as well as its binding.

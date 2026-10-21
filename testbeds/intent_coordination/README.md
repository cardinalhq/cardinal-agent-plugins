# Intent coordination acceptance

`run.py` launches the installed Codex and Claude CLIs using isolated candidate
artifacts and the actual Conductor HTTP router/reader/lifecycle guard. Default
mode uses fixture authentication/storage; `--live` requires deployed intent
support and uses the existing Cloud connection in a dedicated Investigation.

Run both requester/responder directions with `--backend`, `--artifacts`, and a
fresh `--output` beneath `testbeds/coordination_gate_phase3/runs/`. Neither HOME,
CODEX_HOME, normal hooks/configuration nor runtime authentication is replaced.
The invoke helper forwards normal CLI arguments to the unmodified artifact and
records public JSON results; assertions never inspect private model reasoning.

The acceptance purpose is choosing an explanation, rather than editing or
committing. Ordinary native tools must remain available with unread feedback.
Both agents explicitly sync, read every chunk, make attributed acknowledgments
and review hashes. Each required result must produce a real timeout retaining
the dependency, feedback interruption, and a non-ready result after response
arrival but before exact acceptance/cancellation. Only then may sync report
ready and the requester write its decision note. All four outcomes, blocked
progress, independent native work, private witness delivery and exact references
are checked against server records and public CLI results. Separate invocations
exercise durable resumption between operations.

Old registration handlers are invoked by an audit-only test hook to prove they
never deny native calls during an upgrade; shipped adapters register no new
coordination pre-tool hook. Owned bindings/outboxes and disposable Codex trust
entries are archived/cleaned, with cleanup failures preventing success.

The first draft's barrier accepted only a bare outcome, while one agent supplied
its full CASE marker. The helper now normalizes either representation and the
prompt explicitly asks for a bare outcome. Preserve those attempts as harness
evidence; this change does not relax any product assertion.

This does not establish server push, indefinite host parking, fleet performance,
or production authorization unless `--live` acceptance is separately recorded.

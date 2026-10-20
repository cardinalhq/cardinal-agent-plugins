# Phase 3 direct CLI acceptance testbed

Runs the installed Codex/Claude CLIs through production adapter entry points and
the shared gate engine. A loopback server imports Phase 1's real Conductor event
router, acknowledgment validation and fixed-head reader. Authentication,
get-investigation metadata and storage are fixtures, not deployed acceptance.

Requires Python 3.11+ and Node with the Phase 1 checkout's dependencies installed.
Build the local artifacts, then run:

```sh
python3 build/vendor.py codex claude
python3 testbeds/coordination_gate_phase3/run.py --cases reconsider patch verdict quiet read_error --quiet-count 100 --timeout 900
python3 testbeds/coordination_gate_phase3/run.py --runtime claude --cases edit notebook --timeout 600
```

`--phase1 PATH` selects the Phase 1 Conductor checkout with installed dependencies;
`--runtime codex|claude|all`, `--timeout`, `--quiet-count` (default 10) and `--output` control the runs. No model
is selected or overridden. Codex uses the Phase 0 transparent uncaught code-mode
transport when the current model only exposes tools through that interface.

The harness does not redirect HOME or CODEX_HOME, install normal hooks, change
CLI auth or supply a replacement normal configuration. Invocation-scoped hooks
and fixture connection variables are supplied.
Codex receives invocation-scoped trust bypass for the reviewed test source and
loopback network access; this is not a production trust-installation test. Its
sandbox additionally permits the existing Investigation sessions directory for
the fixture binding/ack bookkeeping. SessionStart creates a unique fixture binding
and refuses to overwrite an existing one. The binding is archived in the run and
removed afterwards. Gate/route state and effects stay inside the private run.

Codex itself persists project-trust tables for these disposable directories even
with invocation-scoped trust options. On completion the harness removes only
tables belonging to that run, preserving other settings, comments and file
permissions. Configuration comparisons exclude only this testbed's trust tables;
normal hook files and Claude settings are compared byte-for-byte. After an
interrupted run, `--clean-project-trust` removes this testbed's remaining tables.

The observed hook wrapper captures real pre-tool payloads and the unchanged
production adapter output. Cases assert actual denial, absent original effects,
same-session acknowledgments, reviewed cursors, peer witness in model output,
explicit replacement and feedback bounds. Supervisor verdict cases also require
that only a revised verdict reaches the actual Phase 1 write route. Claude Edit
and NotebookEdit use real native tools. Quiet checks are separate covered
gh-shaped fixture invocations with fresh server reads, not a loop in one shell.
The read-error case requires an actual denial after arming the 503 fault.

Raw prompts, JSONL, adapter/server audit, state, versions and results are retained
under ignored `runs/`. Failed fixture attempts are retained too. Consult
[REPORT.md](REPORT.md); a model exit of zero alone is not a protection result.

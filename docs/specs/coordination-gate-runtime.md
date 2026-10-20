# Mechanical coordination runtime adapters (Phase 3)

The shared engine now has synchronous Codex and Claude adapters. They remain
opt-in and limited to the Phase 0 CLI transports. Installing the hook does not
enable a gate. Existing sessions retain their delivery behavior until explicit
activation. These hooks are coordination guardrails, not an OS security boundary.

## Registration and scope

- Codex `scripts/cardinal-connect` registers `PreToolUse` matcher
  `Bash|apply_patch`, running `cardinal-codex-telemetry.py --event CoordinationGate`
  synchronously with a five-second timeout. Managed-hook replacement/removal
  follows the existing connect/disconnect mechanism. Development did not install
  normal hooks. Codex-created disposable project-trust entries are removed by
  the testbed's targeted cleanup.
- Claude `hooks/hooks.json` registers synchronous `coordination-gate.py`, matcher
  `Bash|Write|Edit|NotebookEdit`, timeout five seconds. `MultiEdit` remains excluded.
- Both call `investigation_gate_runtime.dispatch`; adapters supply only runtime,
  home, connection, client and payload. Codex's canonical `Bash` hook input uses
  `tool_input.command`; it is normalized to the engine's `exec_command`/`cmd`.
- A successful quiet check emits no stdout and no allow decision. Pending work,
  partial scans and caught operational errors emit the supported
  `hookSpecificOutput.permissionDecision: deny` shape with a nonempty reason.
  Engine content plus adapter instructions stays within 1,000 UTF-8 bytes.

Only the direct command forms in [the engine specification](coordination-gate.md)
are covered. Code mode can transport a transparent uncaught await of the tested
shell/edit tool (the Phase 0 positive probe); catching/suppressing that rejection,
arbitrary JavaScript effects, continuing interactive stdin and unlisted wrappers
remain outside coverage. Desktop and interactive UIs remain unvalidated.
Subagent payloads do not inherit a main-session gate or consume its deliveries.

Actual process crashes, import failures, malformed runtime input/output and outer
hook timeouts can still fail open on the tested runtimes. Catching a recoverable
engine/read failure and returning valid denial does not remove those limitations.
See the [official Codex hook contract](https://learn.chatgpt.com/docs/hooks).

## Enablement and readiness

Launch the supported CLI with `CARDINAL_COORDINATION_MODE=direct-cli`. This is
an operator declaration of the tested protocol, not automatic proof of every
future tool path. Runtime adapters identify the CLI ancestor without reading
prompts or command arguments. Enablement requires observed pre-tool receipts for
every transport in that same process/version within five minutes:

| Runtime | Required probes |
|---|---|
| Codex CLI | `Bash`, `apply_patch` |
| Claude `-p` | `Bash`, `Write`, `Edit`, `NotebookEdit` |

Exercise harmless calls using these transports before `gate-enable`. The
enable command itself must have a fresh (30-second), exact parsed-command
receipt through a registered CLI prefix. That receipt is consumed once.
Matching binds the consumer session, runtime, process, actual adapter/core source
digest and exact enable arguments. Merely finding a registration file or an old
receipt cannot enable the CLI path. This also works when the sandboxed CLI cannot
execute `ps`: process ancestry is checked by the unsandboxed pre-tool hook.

The fixture protocols exercise actual runtime dispatch. Receipts are private
local bookkeeping, not cryptographic runtime attestation; later disabling hooks,
changing trust or leaving the supported protocol remains outside the guarantee.

`gate-enable --gate-file ... --policy ... --session ...` also checks the Phase 1
capability, viewer/author and consumer identity. An incompatible server never
silently falls back to the legacy delivery cursor. Supervisor enablement pins
its credential and budget paths as in Phase 2.

Runtime routing lives in private `<SID>.gate-route.json`; readiness is in
`<SID>.gate-route.ready.json` beside it. Default location is the existing
Investigation sessions directory. An explicit absolute
`CARDINAL_COORDINATION_STATE_DIR` selects a private directory for routing and
receipts, shared by the hooks and CLI. It does not redirect existing bindings,
credentials, failure markers or legacy delivery state. It can keep gate routing
inside a declared writable workspace without rewriting HOME or CODEX_HOME.

Activation persists an `activating` route before enabling the engine and commits
`active` only after engine enablement succeeds. Interrupted activation defers
covered actions. During a policy change it protects the union of the old and
requested command forms until the new epoch commits. Route mutations use atomic
private writes and directory fsync. One session cannot silently replace another
active route. Explicit disable preserves pending content and cancels incomplete
activation even when no engine file was created.

## Commands and changed-path instructions

After activation, `gate-status`, `gate-read`, `gate-reviewed`, `gate-disable`
can resolve their gate, investigation and scoped credential from this session's
private route. Supply `--session SID` explicitly where the runtime does not
export it. Explicit investigation/credential overrides must match the route.
There is no author-key fallback for an implicit supervisor route.

`gate-read` defaults to the pending batch's next undelivered chunk; after all
chunks it replays chunk 1. Explicit `--batch` and `--chunk` remain supported.
Output is still one bounded chunk per invocation. `gate-reviewed` still requires
the exact batch and every ordered chunk hash; reads never count as review.

For matching enabled sessions the guidance builder replaces coordination
reminders with one rule:

> When a coordination gate defers a call, handle its attributed batch, then choose your next action again.

Session identity, owner-input transparency, storyboard authorization and sparse
checkpoint instructions remain. Non-enabled guidance is unchanged. On-demand
handling instructions are part of each new hashed pending page: advisory
authority, acceptance versus completion, required challenge/directive/verdict
positions, or the supervisor's existing budget/required-post review path. Batch
ids and chunk commands arrive on the changed path rather than in permanent
instructions. Existing pending pages remain replayable.

## Delivery coexistence

For the routed Investigation, the active gate owns automatic contribution
delivery. Shared turn logic and the background poller use the route lock:

- Post-tool delivery, staged delivery at prompts, Stop inbox reads and legacy
  supervision/decision resurfacing leave their queues and cursors untouched.
- The poller pauses event reads for that Investigation. Its capability refresh
  and unrelated bootstrap/owner-input maintenance retain their existing roles.
- Owner-input flush still runs at turn end. Unrelated naming reminders retain
  their existing behavior.
- Disable releases ownership and legacy queues can deliver again. An incomplete
  or unreadable active route conservatively retains its queues.
- A supervisor's gate on a worker Investigation does not suppress the
  supervisor's own Investigation delivery. Unvalidated runtimes keep their
  existing delivery paths; the two implemented adapters deny covered calls when
  their active route belongs to a different runtime/mode.

The gate never imports a legacy delivery cursor or filters pending work using
another consumer's acknowledgments. A legacy cursor of 999 cannot hide event #1
from a newly activated gate. Explicit manual event reads remain available; they
do not give gate handling credit. No lock spans a model turn.

## Validation boundary

The local testbed launches the installed CLIs, real packaged adapters and engine,
and the Phase 1 checkout's actual HTTP event router/reader and acknowledgment
validation. Authentication, get-investigation metadata and storage are fixtures.
This validates runtime transport and the reader contract; it is not acceptance
against deployed authentication or Postgres. Phase 1's database/authentication
tests remain the separate backend evidence.

The production server now advertises `event_snapshot` version 1 after the
Maestro/MCP gateway `v1.124.0` deployment (Conductor PR2156, GitOps PR2424).
Independent review confirmed both replicas and sidecars healthy at the verified
image digest. All 15 authenticated deployed HTTP/MCP snapshot checks passed,
including fixed-head continuation across appends, scoped visibility, owner-input
exclusion and revoked-grant rejection. Dedicated installed Codex and Claude worker and scoped supervisor cases passed
with the reviewed packaged artifacts. Each original action was denied before
explicit handling and a revised action; supervisors posted exactly one revised
verdict. Temporary grants were revoked and credential files/session bindings
removed. These runs validate the declared direct CLI paths; normal sessions
remain disabled.
The [testbed report](../../testbeds/coordination_gate_phase3/REPORT.md) records
passing cases, failed fixture attempts, exact versions and artifacts.
Phase 4 still owns 1,000 quiet checks per runtime, matched token/latency baselines,
fleet request load and broader fault/performance acceptance.

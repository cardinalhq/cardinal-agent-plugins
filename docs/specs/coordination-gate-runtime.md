# Automatic coordination on direct CLI paths

Codex and Claude adapters automatically apply both checks before covered actions:

1. Read a fresh fixed-head snapshot and explicitly handle applicable new feedback.
2. Keep actions held by unresolved requests paused until the requester accepts an
   exact complete result or explicitly cancels with a reason.

There is no mode setting, policy file, readiness ceremony or enable command for
normal use. The installed hook and a Cardinal connection are prerequisites. A
connected session without its Investigation binding bootstraps one; initialization
or a read error returns a bounded deferral. An unconnected session retains normal
behavior. This is a coordination guardrail, not an OS security boundary.

## Registration and coverage

Codex connect registers synchronous `PreToolUse`, matcher `Bash|apply_patch`,
running `cardinal-codex-telemetry.py --event CoordinationGate`, timeout five
seconds. Claude registers `coordination-gate.py`, matcher
`Bash|Write|Edit|NotebookEdit`, also five seconds. Shared dispatch resolves the
actual CLI ancestor and generates the versioned policy from installed executable
paths. The [engine specification](coordination-gate.md) lists the finite direct
command grammar.

Covered actions are native edits, direct `git commit`, `git push`, `gh pr merge`,
and direct Investigation verdict commands. A transparent uncaught await in Codex
code mode transports these native tools. Arbitrary JavaScript effects, caught or
suppressed rejections, nested wrappers, compound shell commands, continuing
interactive stdin and unlisted tools remain outside coverage. Desktop and other
interactive UI paths remain unvalidated. Subagents have their own identities and
bindings; they do not consume a parent session's feedback.

Quiet success produces no model-visible output. A deferral prevents that
invocation and returns a supported denial within 1,000 UTF-8 bytes, including
adapter instructions. Activation and scanning share an internal 3.3-second budget
below the five-second runtime hook timeout. This is an implementation bound,
not a measured latency SLA. Actual crashes, import failures, malformed runtime
input/output and outer timeouts can still fail open in the tested runtimes.

## Identity and durable routing

Automatic policy selects authenticated peers of the same Investigation, excluding
same-principal/session bookkeeping. It derives author/participant/grantee role
from authenticated server metadata. Every snapshot requires `event_snapshot`
version 1 and `request_protocol=1`; missing support defers with an upgrade message.
An older server cannot silently omit request events and clear the action.

Each session can own an author consumer and multiple explicitly selected scoped
consumers. Explicit CLI `--credential-file` selects a scoped consumer; there is no
scan for credentials and no author-key fallback for one. Ambiguous CLI handling
requires an Investigation, batch or credential selector. Each known active
consumer must pass its check or be replaced through verified grant succession.

Private `<SID>.gate-route.consumers.json` indexes consumer routes. Each consumer
has its own route and state files. An activating route is durable before index
publication; interrupted activation, missing files and wrong credential
replacement defer actions. Existing pending pages, chunk hashes and pinned
supervisor response guards survive compatible upgrades. A changed consumer
rescans from zero. Old private pages remain available during verified renewal.

Explicit grant succession writes and fsyncs a private old-to-new journal before
removing the old credential. The server verifies all supplied `previous_grant_ids`
against the authenticated grant's stored lineage and Investigation. Only then may
the new consumer replace its predecessor; an unrelated replacement fails closed.
Renewal retains pinned supervisor budget/response obligations.

`CARDINAL_COORDINATION_STATE_DIR` optionally changes only the private route/state
location. It is not an enable switch and does not redirect bindings, credentials
or normal delivery queues. Legacy manual gate commands remain for compatibility;
disabling an old route does not opt a supported connected session out of automatic
coordination: the next covered action rearms it.

## Handling feedback and dependencies

A denial identifies its batch. `gate-read --session SID --batch B` returns one
bounded lossless chunk at a time. Read every chunk, record required acknowledgments
under this principal/session, then `gate-reviewed --session SID --batch B
--chunk-hash H1 ...` with every ordered hash. Reads and delivery never count as
review. Request lifecycle events need explicit batch review but no acknowledgment.
Participants without author acknowledgment permission use explicit hash review;
supervisors retain their existing budget/required-post review checks.

The agent then chooses its next action again. A reply can introduce feedback and
leave a dependency unresolved at the same time. Handling that feedback permits
unrelated work; it does not release the declared dependent actions. See
[request/result coordination](coordination-requests.md) for commands and outcomes.

## Delivery coexistence

For each routed Investigation, automatic coordination owns contribution delivery.
Post-tool, prompt, Stop and poller paths leave their corresponding queues/cursors
untouched while that ownership is active, including during repair. Owner-input
flush and unrelated bootstrap/naming maintenance continue. Another Investigation
or an unvalidated runtime keeps its existing delivery behavior. The engine never
imports an old delivery cursor or borrows another consumer's acknowledgments.
No lock spans a model turn.

## Validation boundary

The prior preview release (Maestro v1.124.0, Codex plugin 0.38.0, Claude plugin
0.60.0) validated fixed-head HTTP/MCP reads and explicit activation on installed
CLI paths. Those historical results are recorded in the
[Phase 3 report](../../testbeds/coordination_gate_phase3/REPORT.md); they do not
prove automatic activation or request/result behavior.

The [automatic coordination testbed](../../testbeds/automatic_coordination/README.md)
launches two installed CLIs with isolated candidate artifacts against actual
Conductor router/reader/guard code and fixture authentication/storage. It tests
both role directions, first-action activation, explicit feedback handling,
post-response dependency denial, all four outcomes and unrelated work while
blocked. Real Postgres serialization and authenticated deployed acceptance remain
separate checks. Phase 4's fleet load, 1,000 quiet checks per runtime and matched
latency/token baselines remain outside these narrower acceptance claims.

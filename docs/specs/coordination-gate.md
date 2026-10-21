# Durable synchronization engine

The engine synchronizes an explicit agent intent. It never infers intent from a
tool name or a shell-command grammar. `Gate.synchronize(purpose, required)` is
shared by both adapters' sync/wait commands. Former `check`/hook entry points are
pass-through compatibility stubs; no action taxonomy participates in clearance.

Each invocation reads a fresh authenticated fixed-head snapshot. It persists scan
progress, immutable feedback pages and lossless bounded chunks before delivery.
All chunk hashes, required acknowledgments, supervisor budget review and pinned
response guards must be satisfied before advancing the handled position. Delivery,
read cursors, acknowledgments by other sessions and model claims are insufficient.
A completed old partial snapshot requires another fresh synchronization.

Request lifecycle state folds independently from feedback review. For each exact
purpose, a private context retains explicitly selected request sequences and
this requester's requests carrying that intent. Changing to independent work
requires an explicitly different purpose; the engine never guesses from text.
A missing, foreign or malformed prerequisite returns no clearance. Complete
responses remain unresolved until exact-result acceptance, while cancellation
requires a reason. Timeout, blocked, abstain and reject never release work.

Intent references survive compatible epochs and verified credential succession.
Legacy action-scoped request history is preserved; explicit ID selection replaces
action matching. Consumer identities, private 0600 files, locking, atomic fsynced
writes and server-authenticated grant predecessor proof preserve existing safety
properties. No durable tool permit is produced or cached, and no lock spans an
agent turn or wait interval.

The role/peer policy remains for authenticated feedback applicability and review
obligations. Historical git/gh/prefix fields can be loaded for old state hashes;
new policies contain no executable registry and those fields have no enforcement
meaning. Actual host tool execution remains the runtime's responsibility.

See [runtime integration](coordination-gate-runtime.md) and
[request/result commands](coordination-requests.md). Historical Phase 0–3 and
automatic-action acceptance reports describe previous contracts, not this one.

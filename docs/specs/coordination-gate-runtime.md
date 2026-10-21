# Coordination chosen by agent intent

Both Codex and Claude expose the shared `investigation sync`, `request`, `wait`,
`respond` and `resolve` interface. An agent may synchronize before planning,
reasoning, answering, editing or any other work. The purpose is free-form text;
requirements are exact authenticated request references. There is no action
registry, runtime process probe, mode setting, policy file or enable ceremony.

Ordinary tool calls are not classified or automatically blocked. New installs
register no coordination PreToolUse hook. Codex hook repair removes the former
managed registration; stale Codex/Claude hook handlers return quietly during an
upgrade. Other hooks (telemetry, evidence, storyboard context, spend limits) keep
their separate responsibilities.

The [request interface](coordination-requests.md) defines result states, durable
waiting and explicit resolution. Waiting is a bounded CLI invocation using
incremental server reads with backoff, not a push subscription or an indefinitely
parked host task. New feedback interrupts it for explicit handling. Result arrival
does not release a prerequisite until exact acceptance or reasoned cancellation.

## Identity and state

Explicit sync/wait selects its Investigation and credential, retaining known
consumers of that Investigation without scanning for credentials. Authenticated
metadata selects worker/participant/supervisor handling. No author-key fallback
is permitted for a scoped consumer. Multiple consumers retain separate immutable
batches and review state. Known missing or changed credentials require repair or
server-verified succession; copies do not retire live predecessor obligations.

Private routes and state preserve fixed-head paging, pending hashes, acknowledgments,
supervisor budget/required-post guards and intent prerequisites. Compatible policy
epochs and verified grant succession retain required request references, including
legacy requests without an intent field. Changed credentials rescan the stream;
only verified predecessor proof permits retirement. No lock spans a model turn.

`CARDINAL_COORDINATION_STATE_DIR` changes only private route/state location; it is
not an enable switch. Low-level `gate-read/reviewed/status` commands remain for
lossless feedback handling. Legacy explicit gate activation is compatibility
bookkeeping, not automatic tool interception.

## Delivery coexistence and validation

Normal delivery continues notifying agents about new information outside an
outstanding explicit catch-up exchange. While sync owns an immutable pending
batch, normal delivery leaves the corresponding queue/cursor untouched, avoiding
silent consumption or an alternative review path. Delivery never imports its
cursor into explicit synchronization. Owner input and unrelated maintenance
retain their existing paths.

Prior automatic-action release evidence is historical, not acceptance of this
intent interface. New acceptance must exercise arbitrary reasoning/decision
purposes on both installed runtimes, all response outcomes, restart/resumption,
feedback arriving during a wait, unchanged normal configuration and absence of
action interception. Fleet load, token/latency baselines and indefinite host
parking are separate claims; do not infer them from functional tests.

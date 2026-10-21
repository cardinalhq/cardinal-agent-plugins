# Intent-based request/result coordination

Supervisor and worker use the same primitives at any point in their work. The
agent states what it is trying to do and which result it needs. Coordination does
not classify tool names, shell commands, edits, commits, merges or verdicts.

## Catch up, request, wait, resolve

```sh
cardinal-storyboard investigation sync --session SID \
  --intent 'Choose which hypothesis to explain'
cardinal-storyboard investigation request --session SID \
  --assignee user:USER_ID --assignee-session PEER_SID \
  --intent 'Choose which hypothesis to explain' --text 'Evaluate these explanations'
cardinal-storyboard investigation wait --session SID \
  --intent 'Choose which hypothesis to explain'
cardinal-storyboard investigation respond REQUEST_SEQ --session PEER_SID \
  --outcome complete --text 'Result and evidence'
cardinal-storyboard investigation resolve REQUEST_SEQ --session SID \
  --outcome accepted --response RESPONSE_SEQ --text 'Reviewed this exact result'
```

Use one Investigation for both participants. `--investigation INV` overrides the
session binding; scoped participants use `--credential-file`. Authentication
determines the principal. User/key assignees name their session; grantees omit it.
A request's `--intent` defaults to its text. Reuse the exact purpose to resume it;
the system never guesses equivalent purposes from prose.

`sync --intent PURPOSE` obtains a fresh fixed-head snapshot and handles any
outstanding feedback batch before reporting readiness for that purpose. It
retains all of this requester's requests with that exact intent. Repeat
`--request Q` to add specific existing prerequisites, including legacy requests.
Those exact references survive restart and compatible policy/grant succession.
An independent purpose can proceed; ordinary tools are never intercepted.

`wait --intent PURPOSE` runs the same synchronization and waits while the required
results are pending. It returns immediately on feedback needing handling or a
result requiring a decision. `--incoming` waits for feedback/assignments even with
no outgoing dependency. `--request Q --after-response R` waits for progress beyond
a handled blocked update. Complete, abstain and reject always need attention.

A single wait invocation performs bounded incremental reads internally with
backoff; the agent does not repeatedly poll or copy cursors. This is not a server
push subscription or a promise that the host can park/resume a model indefinitely.
`--timeout` is at most 300 seconds. Timeout returns exit 3, `released:false`, and
preserves the purpose/prerequisites; resume with the same intent. No lock spans
the wait or a model turn.

## Results and feedback

`ready` means this explicit synchronization completed and its selected required
results are resolved. `needs_feedback` supplies the immutable batch and first
bounded chunk. Use `gate-read --batch B` for every chunk, make required attributed
acknowledgments, then `gate-reviewed --batch B --chunk-hash H1 ...` with all ordered
hashes. Reading/delivery is never acceptance. `waiting` means no result yet;
`needs_attention` includes the exact response and its request reference. `retry`
means synchronization did not finish; none of these states releases the intent.

The assignee responds `complete`, `blocked`, `abstain` or `reject`. Only the
requester (or its verified successor) can resolve: `accepted` names the exact
latest complete response; `cancelled` requires an explanation. Blocked permits
progress and a later complete result. Abstain/reject require cancellation and a
new request for reassignment. ACKs, feedback review, silence and timeouts never
resolve dependencies. These records confer no owner authority or permission to
message another agent.

## Persistence, compatibility and limits

Immutable `request.created/responded/resolved` events, authenticated identities,
canonical grant lineages, serialized lifecycle/cycle checks and idempotency remain.
An uncertain creation retains its durable retry key and prevents clearance of its
purpose until reconciled. Later identical successful delegation is a new request.
Old action-scoped records remain readable and can be selected by exact request ID;
their former action categories do not intercept tool execution in intent clients.

Limits remain 500 requests per Investigation, 100 per producer across verified
succession, and ten blocked progress replies, with reserved completion/resolution
headroom. Revocation or disappearance does not release a dependency. A malformed,
missing or foreign required request cannot produce readiness.

The backend advertises `coordination_intents: {enabled:true, version:1}` alongside
`coordination_requests` and `event_snapshot` version 1. Intent clients require
`request_protocol:2` on every page, including grant predecessor proofs. Legacy
protocol 1 remains accepted, but old clients cannot fold intent-only records and
fail closed when they encounter one. Upgrade all participants sharing an
Investigation before using intent requests; mixed-client availability is not claimed. Deploy backend migration
`20261104100000_maestro_coordination_intents` before new clients; old servers
reject protocol 2. Rollback refuses while immutable intent requests exist.

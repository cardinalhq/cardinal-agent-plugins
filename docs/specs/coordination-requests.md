# Durable request/result coordination

An agent chooses an explicit dependency when delegating work. It names the
assignee and the action categories that require the result. Automatic runtime
checks evaluate feedback and dependencies together; there is no mode selector
and no feature flag keeping either behavior off by default.

## CLI example

Use the same Investigation for both participants. User/API-key participants name
their actual session; scoped grants identify a grant lineage and omit the
assignee session. The server derives the caller identity from authentication.

```sh
cardinal-storyboard investigation request --session REQUESTER_SID \
  --assignee user:USER_ID --assignee-session RESPONDER_SID \
  --blocks commit --blocks push --blocks merge --text 'Review this change'
cardinal-storyboard investigation requests --session RESPONDER_SID --wait
cardinal-storyboard investigation respond REQUEST_SEQ --session RESPONDER_SID \
  --outcome complete --text 'Reviewed result and evidence'
cardinal-storyboard investigation requests --session REQUESTER_SID \
  --request REQUEST_SEQ --wait
cardinal-storyboard investigation resolve REQUEST_SEQ --session REQUESTER_SID \
  --outcome accepted --response RESPONSE_SEQ --text 'Reviewed this exact result'
```

Use `--investigation INV` or the existing binding; scoped participants pass
`--credential-file`. Request actions are `edit`, `commit`, `push`, `merge`,
`verdict`. Choose the smallest set needed: a review needed before merge should
leave edits possible. Arbitrary prose such as “please review” does not silently
create a dependency; use `request` whenever work must wait for another agent.

`requests --wait` waits for an assigned incoming request or, with `--request Q`,
a response to that request. `--after-response R` waits for a newer response after
a progress update. Waiting is bounded (`--timeout`, at most 300 seconds); timeout
returns exit 3 with `released:false`, and never resolves the dependency.

## Outcomes and resolution

| Responder outcome | Meaning | Dependent action |
|---|---|---|
| `complete` | Result is ready for requester review | Held until exact-result acceptance |
| `blocked` | Missing information or prerequisite; further responses allowed | Held; unrelated work remains possible |
| `abstain` | Assignee cannot responsibly decide | Held until explicit cancellation/reassignment |
| `reject` | Result fails the requested acceptance condition | Held until explicit cancellation/correction |

Only the assignee or its server-verified grant successor may respond. Only the
requester or its verified successor may resolve. `accepted` requires the exact
latest `complete` response sequence. `cancelled` requires an explanation; start
a new request for reassignment. Abstain/reject are terminal responses for that
request. Blocked permits up to ten progress responses, then still permits a
terminal result. Acknowledgments, ordinary verdicts, silence, read cursors and
timeouts never resolve a request.

## Persistence, identity and limits

The append-only events are `request.created`, `request.responded`, and
`request.resolved`, all advisory control records. They confer no owner authority
or permission to message other agents. Requests are confined to one Investigation.
The server validates new assignee liveness and canonicalizes person-owned API
keys and grant lineages. Session identity remains client-attested, as on existing
Investigation events; the system is not proof that a model understood a result.

The database append lock serializes lifecycle transitions and cycle detection.
Unresolved A→B→A dependencies are refused, including across grant succession.
An exact idempotent retry returns the committed event before fresh validation,
quota or lifecycle refusal. Creation uses a durable private outbox: an uncertain
network response retains the same key and holds dependent actions until the exact
request command reconciles it. A later successful identical delegation is a new
request with a fresh key.

There are at most 500 requests per Investigation and 100 per producer, counted
across a grant's verified succession lineage. Separate event headroom reserves
blocked updates, terminal replies and resolutions even after ordinary advisory
or creation quotas fill. Request state is folded independently of feedback's
handled cursor and survives restart. Revocation or assignee disappearance does
not auto-release an existing dependency; the requester must explicitly cancel.

## Server and deployment contract

The server advertises `coordination_requests: {enabled:true, version:1}` and coordination-aware clients require
`request_protocol:1` on every page; legacy readers may omit it. Migration
`20261104090000_maestro_coordination_requests` adds event constraints and a
reference index. Deploy migration/backend and verify capability before publishing
automatic adapters. Old backend pods reject the new read protocol; a mixed
rollout can defer actions rather than silently hide obligations. Rollback of the
migration refuses while immutable request records exist; roll forward to repair.

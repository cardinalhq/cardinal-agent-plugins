# Private semantic WAL supervision

Implementation plan and local acceptance run, 2026-10-10.

## Product contract

No publication or public link is needed. A Storyboard is the human-facing
view; the Investigation's ordered event API is the source for supervision.
Publishing and public sharing are separate explicit actions. The worker
checkpoints outcomes, hypotheses, experiments and decisions with evidence
references; it does not publish private reasoning or every tool call.

A supervisor reads bounded pages of semantic **and control** events, evaluates
material changes, posts advisory cues/questions/challenges when justified,
then observes acknowledgment and subsequent results. An acknowledgment proves
receipt/disposition, not that the requested behavior happened. An evidence ID
is linkage, not proof the supervisor inspected that evidence. Missing WAL
activity means unknown activity, not compliance or success.

## Existing substrate and blocker

The deployed service already supports revocable Investigation grants with
`read`, `advise`, and optional `owner_input:read`, plus authenticated event
reads and posts. Claude's CLI has grant support. Codex/Cursor/Gemini's shared
CLI had only author operations and rejected grant tokens for every command.
The existing worker poller delivers advisory context at a tool boundary. It
does not wake an idle agent and does not force obedience.

## Implementation

1. Add shared CLI `grant`, `grants`, `revoke`, `show`, and scoped `post`.
   Keep author-only operations unavailable under a scoped credential.
2. Grant defaults to `read,advise`, four hours. Write the token to a new 0600
   JSON file; print only metadata. Explicit credentials fail closed without
   loading the owner's connection. Grantees never claim an author session.
3. Add `events --credential-file FILE --cursor-file STATE`. Bind cursor state
   to origin, org, investigation and grant. Persist one bounded pending page;
   repeat reads replay it until `reviewed --batch HASH` commits that exact
   page. Never use the worker's delivery cursor. Pages include ACKs.
4. Add `post ... --outbox-file FILE`: persist exact advisory intent before
   sending, content-address its idempotency key, and reuse both on retry.
   Changing content requires a different outbox file. Identical content also
   deduplicates across outbox files: include the triggering WAL sequence in
   `--ref` to distinguish a later review decision. Server deduplication
   handles an accepted request whose response was lost.
5. Launch separate user-visible worker/supervisor chats. Keep the worker active
   during the bounded acceptance run. Prove checkpoint → scoped read → scoped
   advisory → hook delivery → ACK → later semantic result. Record actual seqs.
6. Test replay, stale/unread commits, credential mismatch, expired/revoked
   tokens, secret handling and no author-key fallback. Independently review
   the plan and final diff.

The local grant file is **not OS isolation**: both chats share the user's
filesystem. Scoped requests establish server-enforced permissions and grantee
provenance for this client path. A compromised local agent can still access
other same-user files. Strong separation needs a credential broker or isolated
runtime with only its scoped tool endpoint.

## Commands (shared Codex/Cursor/Gemini CLI)

Use the adapter's `scripts/cardinal-storyboard` with Python. The source tree
must first be vendored with `python3 build/vendor.py codex` (or its adapter).

```
cardinal-storyboard investigation grant --session WORKER --out /private/grant.json --ttl 1h --label Supervisor
cardinal-storyboard investigation events --credential-file /private/grant.json --cursor-file /private/cursor.json
cardinal-storyboard investigation post challenge 'Check the missing negative path at WAL #4.' --to-session WORKER --credential-file /private/grant.json --outbox-file /private/advice-4.json --ref '#4'
cardinal-storyboard investigation reviewed --credential-file /private/grant.json --cursor-file /private/cursor.json --batch HASH_FROM_READ
cardinal-storyboard investigation revoke GRANT_ID --session WORKER
```

Read one page, judge it, finish/retry any advisory, then mark it reviewed. Drain
pages while next_after < head_seq; otherwise wait with bounded backoff. Do not
post on every poll. Stop on completion, explicit cancellation, grant expiry,
revocation, or the agreed deadline. Failures must not be reported as an empty
WAL. Never paste grant tokens into chat, prompts, evidence, or public URLs.
A persisted pending batch is a local historical copy; replaying it is not a
fresh server authorization check. Revocation blocks new reads/posts, not data
already read and held by the grantee.

## ChatGPT cloud path

A public storyboard URL is neither a subscription nor a scoped tool
credential. Use authenticated MCP read/post tools. ChatGPT supports custom
MCP servers and authenticated read/write tools:
https://developers.openai.com/api/docs/guides/custom-mcp-server
https://developers.openai.com/plugins/build/auth

The existing Cardinal MCP read/post tools can serve authenticated organization
members today. For an external least-privilege supervisor, add an OAuth-backed
broker mapping its authenticated connection to one Investigation grant. Keep
bearer credentials server-side; enforce investigation/scope on every tool
call. Expose bounded `read_events`, `post_advisory`, and explicit review cursor
commit, with producer provenance and stable idempotency keys. No owner-input
scope unless deliberately granted. Do not treat a public share token as WAL
access. Acceptance: cross-investigation denial, revocation, scoped identity,
and all the local delivery tests through this broker.

A connection supplies access, not guaranteed autonomous wakeups. Start with a
user-authorized heartbeat in the supervising chat, storing its cursor and
staying quiet on unchanged state. A production service can instead consume
WAL notifications, coalesce by investigation, lease one review at a time, and
invoke a supervisor only on material new events. It needs bounded retries,
budget/rate caps, cancellation, expiry and a dead-letter state. Wake the worker
only via a supported, explicitly authorized runtime mechanism; otherwise show
pending delivery honestly until its next tool boundary. Cloud webhook/tool
support must be verified for the chosen host before promising this behavior.

## Independent review

An independent sub-agent reviewed the plan and confirmed the CLI blocker.
Its required corrections are incorporated: durable pending batches, separate
consumer identity, persisted idempotent advisories, ACK visibility, private
credentials, fail-closed auth, no claimed author identity, explicit idle-worker
and same-filesystem limitations, and a separate cloud integration milestone.
The initial local run proves transport and response, not generalized autonomous
drift detection or compliance. Its worker task is a bounded validation of this
implementation, with supervisor advice based on actual checkpoints.

## Validation record

- Independent sub-agent approved the plan and final diff for a bounded launch;
  it independently reran the six consumer tests.
- 207 core investigation tests, eight existing Codex investigation tests, and
  fourteen supervision/adapter HTTP tests pass. The last group also covers
  expired and revoked token refusal without author-key fallback.
- Live service probe: a read-only grant read a private Investigation; owner
  input and cross-investigation reads returned 403, advice without its scope
  returned 403, and reading after revocation returned 401 `grant_revoked`.
- A separate local worker chat and supervisor chat exercised a private Investigation.
- Supervisor uses a one-hour `read,advise` grant with a distinct grantee
  principal. No storyboard was published or shared publicly.
- WAL #1: worker validation plan. #2: supervisor challenged unspecified
  crash/retry coverage. #3: worker accepted. #4: worker started follow-up
  validation and reported first receipt through hook context, without an
  events-CLI lookup. #5 records the completed process-crash/retry follow-up,
  citing a captured test receipt; #9 completes the
  worker experiment. #8 carefully distinguishes worker-attested hook delivery
  from independent evidence of the process tests. The parent inspected the
  captured test receipt. This proves a bounded advisory/ACK/result round trip;
  it is not a proof of generalized drift detection or enforced compliance.

- Supervisor's exact live retry returned `duplicate: true` with original event
  #2, and it committed review through #9. This tests live idempotent replay,
  not an injected lost network response. Both bounded sessions completed.

The change was run from a vendored source checkout; it has not been released
as a new installed plugin version. Cloud broker and wakeup service remain
explicit subsequent milestones.

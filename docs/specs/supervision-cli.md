# Lane P: Investigation supervision CLI

Stacked on `feat/semantic-wal-supervision` (#190). No plugin version bump is
included: release follows the Maestro deployment. InvestigationState v1.1 and
the control-log evidence prohibition are unchanged.

The Codex, Cursor, Gemini, and Claude launchers expose the shared investigation
and receipt commands. All investigation commands execute through the shared core. Claude keeps
its state/context/discover surfaces and its established human output. Its
legacy parser metadata remains importable for compatibility; the shared
runtime parser accepts the expanded command set. The core's `READ_CLASSES`
includes `owner`; the original `EVENT_CLASSES` tuple remains the legacy Claude
parser vocabulary.

## Examples

Use the adapter's `cardinal-storyboard` launcher, after vendoring the core in
source checkouts:

```sh
cardinal-storyboard investigation events inv_… --follow --session SUPERVISOR --timeout 60 --max 20
cardinal-storyboard investigation events inv_… --json --fields compact --class semantic --type finding.revised
cardinal-storyboard investigation ack inv_… 41 --disposition accepted --note 'Checking it' --session WORKER
cardinal-storyboard investigation ack 41 accepted --session WORKER
cardinal-storyboard investigation post inv_… --type challenge --text 'Check the negative case'
cardinal-storyboard investigation grant --session WORKER --scope read,advise,evidence:read --out /private/grant.json
cardinal-storyboard investigation events --credential-file /private/grant.json --cursor-file /private/cursor.json --follow --auto-review
cardinal-storyboard investigation verify finding_result --root .
cardinal-storyboard receipt ev_…
cardinal-storyboard receipt rcpt_… --session WORKER --rows 0:50
cardinal-storyboard receipt rcpt_… --credential-file /private/grant.json --outline
```

`post cue TEXT` is also accepted; member posts can broadcast and use an
explicit idempotency key or a durable outbox, but cannot combine them. Scoped
posts require both a target session and an outbox. A narrow compatibility
exception preserves Claude's pre-#190 environment-token post when no credential
file or outbox is supplied: the unchanged `TokenMode` parity test requires that
broadcast form. It still never sends the member key or claims an author session.
Explicit credential files and all shared Codex/Cursor/Gemini scoped posts retain
the required target/outbox rule. Member outbox identity has
`grant_id: null`; retries retain the same advisory content and key.

## Read and follow semantics

Explicit investigation reads and Claude reads page through the visible stream
without a target-session filter. Human output in the shared CLI is compact;
Claude retains its existing inert, one-line event envelopes for parity. JSON
defaults to full events, with `--fields compact` selecting the bounded field
set. A follow invocation prints successive JSON page objects; empty pages are
printed too, so an empty stream remains distinct from a failed read.

The interval defaults to five seconds, with a two-second minimum. Follow uses
an in-memory cursor unless a grant-backed `--cursor-file` is supplied. The
latter requires `--auto-review`: print and flush each pending page, then commit
its whole batch hash. Class/type/self filters only affect the display in this
mode. A crash can replay a printed page. If `--max` stops partway through a
pending page, the page remains uncommitted and is replayed on the next run.
There is no second durable cursor format.

Self exclusion uses the server's `viewer.principal`. Grantee self is principal
only; member self requires both the principal and `--session` to match.
Owner decisions are never excluded. An older server without viewer metadata
excludes nothing. With repeated class filters, the route's single-class filter
cannot represent the union, so the CLI reads unfiltered and displays the union.
In-memory reads send single-class and type filters to the server.

## Capabilities and authority

Shared CLI `link` refreshes binding capabilities synchronously. Claude cached
links preserve the unchanged CLI parity gate's network-free behavior by
queuing the existing poller refresh marker; new/recovered links fetch fresh
capabilities. With the poller disabled, the marker waits, and a client-refused
`experiment.revised` write refreshes once and reevaluates the gate, so a
server upgrade does not leave a session stuck on old capability data.
Transient refresh failures retain the cache and fail the gate closed.

The binding's capability block gates `owner_decision`, `checkpoint_artifacts`,
`experiment_revised`, and `receipt_read`. Against an older Maestro:

- artifact/gate fields validate locally, then strip with a warning before the
  default checkpoint idempotency key is computed;
- experiment revisions fail client-side without a write;
- remote receipt reads explain that the capability is unavailable;
- owner decisions are not delivered, and existing advisory delivery continues.

An owner decision is posted from the investigation author's signed-in Cardinal
web session. In an author session it is the owner's own words, alongside the
session transcript. In another human's joined session it is advisory and is
not that session's owner authority. Nothing claims that a human physically
pressed a button.

Decision delivery uses the existing per-session cursor. Only an author ack
claiming this session suppresses a decision inside a fetched page. The binding
retains at most ten pending decisions, with up to two Stop reminders per
entry, without moving the cursor. Reminders contain only the sequence and a
command to read that event; local cache/binding files are same-user, mutable
state and must never mint owner authority by replaying stored decision text. A successful CLI ack or a polled ack from
this session removes the entry.

## Recording and recovery

Failure detection inspects only shell-execution tools and executable CLI
command words. Status comes from structured runtime fields or leading runtime
prefixes; success clearing requires exit 0, never success-looking stdout.

Checkpoint/question/post/ack reachability failures exit 3 and start stderr with
`NOT RECORDED —`; refusals, missing connections and protocol/shape errors exit 1; usage errors
exit 2. Missing connections use `not recorded: not connected to Cardinal
(run /cardinal:connect)` without the reachability marker. Hook detection runs
before capture opt-out/control-plane returns, even when evidence context is
off. Hooks keep a private `<sid>.failures.json`, warn at most every ten minutes,
and clear it on the next successful write. Claude also emits a user-visible
`systemMessage`. The Codex/Cursor/Gemini launchers fail open only for context when its core
is missing; other commands exit 3.

Grant `--out` reserves a new 0600 file. Definite failures remove it. Timeouts or
connection drops with a possibly sent request preserve it and instruct the
caller to list/revoke the possible grant before deleting the reservation.
The grant transport tracks whether its POST write started after TCP/TLS
connection. Connect/handshake timeouts delete the reservation; a timeout/reset
after a POST write may have sent bytes and preserves it. Proxy CONNECT traffic
is not counted as a grant write. HTTP refusal responses are definite, including
5xx. A malformed 2xx grant response is ambiguous: the server may have minted
the grant even though its credentials cannot be recovered. Injected/custom openers without phase tracking classify conservatively. A disk
failure after a successful grant preserves the reservation too.

Checkpoint evidence uploads add ev-to-receipt mappings to the session's
existing promoted ledger, keyed by investigation. `receipt ev_…` reads the
local capture plus known mappings without a remote call; standalone member
receipt reads use the latest binding for their connection's org when no session
is supplied; `receipt rcpt_…`
uses `get-receipt`. Scoped receipt reads pin `investigation_id` and require
`read,evidence:read`, with no author-key fallback.

Security review note for the eventual PR: companion Maestro changes permit
plugin member keys to call `get-receipt`, consistent with existing member
receipt access. A grantee requires the new evidence scope and may read only
receipts cited by semantic events in its pinned investigation; that boundary
is enforced by Maestro. Grant files and local evidence remain same-user
filesystem artifacts, as in #190, and do not establish OS isolation.

`verify <semantic_id>` verifies every artifact-bearing revision, newest first,
with each result labeled by event sequence. Path validation matches Maestro
(including format characters); traversal, absolute paths, controls and lone
surrogates remain refused.

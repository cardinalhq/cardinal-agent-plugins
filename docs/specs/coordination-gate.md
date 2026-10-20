# Coordination gate engine (Phase 2)

Implemented in `core/cardinal_core/investigation_gate.py`. This is an opt-in
shared engine and CLI, with a strict Phase 1 snapshot reader. Phase 3 adds
[runtime adapters, readiness checks and delivery coexistence](coordination-gate-runtime.md).
Gate activation remains explicit; deployed-server acceptance remains pending
Phase 1 deployment. The engine contract below also documents the Phase 2 layer
independently of those runtime integrations.

## Policy and declared coverage

A policy is explicit JSON. Example for a worker (replace the peer identity,
session and installation paths with the actual collaboration):

```json
{
  "version": 1,
  "runtime": "codex",
  "role": "worker",
  "peers": [{"principal": "grantee:grt_REPLACE", "session": "supervisor-session"}],
  "cli_prefixes": [
    ["/absolute/plugin/scripts/cardinal-storyboard"],
    ["/absolute/python3", "/absolute/plugin/scripts/cardinal-storyboard"]
  ],
  "git": ["/usr/bin/git"],
  "gh": ["/opt/homebrew/bin/gh"]
}
```

Set `runtime` to `claude` for Claude. A supervisor uses role `supervisor` and the
worker's principal/session as its peer. It requires a scoped credential file so
its existing supervisor review guard can run. Workers require the investigation
author's own credential. Enablement verifies the server's investigation identity,
author, viewer and `event_snapshot: {enabled: true, version: 1}` capability.

Executable prefixes are literal token arrays, not basename matches. Prefer
absolute executable/interpreter paths. A bare registered executable relies on
the declared shell's PATH and absence of aliases/functions; the engine does not
inspect shell configuration. This is an operational coverage declaration, not
a shell security boundary.

| Entry point | Classification |
|---|---|
| Codex `apply_patch`, `functions.apply_patch` | Covered |
| Claude `Write`, `Edit`, `NotebookEdit` | Covered |
| Codex `exec_command`/`functions.exec_command`, Claude `Bash` | Parse one literal command using `cmd`/`command` |
| Registered git executable; optional repeated `-C DIR` and `--no-pager`; `commit` or `push` | Covered with the finite option grammar below |
| Registered gh executable; optional repeated `-R REPO`/`--repo REPO`; `pr merge` | Covered with the finite option grammar below |
| Registered CLI prefix + `investigation verdict ...` | Covered for every outcome, including malformed arguments |
| Exact CLI `ack`, `events`, `show`, `challenges`, `link`, five `gate-*` operations | Coordination when the real CLI parser validates arguments and consumer context matches |
| Other tools/commands, wrappers, compound commands, pipelines, redirects, substitutions, shell expansions, aliases, interactive input, nested `functions.exec`, `write_stdin`, `MultiEdit` | Uncovered; no enforcement claim |

The option grammar accepts separate option values and `--option=value`, literal
operands, and `--` before operands. Short flag bundles and attached short values
are uncovered. Registered options:

- `git commit`: flags `-a --all --amend --no-edit --allow-empty
  --allow-empty-message --no-verify -n --signoff -s --dry-run --no-gpg-sign`;
  valued `-m --message -F --file --author --date --fixup --squash`.
- `git push`: flags `-u --set-upstream --force -f --force-with-lease --tags
  --all --atomic --dry-run -n --no-verify --delete -d --mirror --prune`;
  valued `--repo --receive-pack --exec --push-option -o`, and optional-value
  `--force-with-lease=REF:EXPECTED`.
- `gh pr merge`: flags `--squash --merge --rebase --auto --disable-auto
  --delete-branch -d --admin`; valued `--match-head-commit --body -b
  --body-file -F --subject -t`.

Unknown options do not inherit coverage. A semicolon inside a quoted message is
literal data; a command separator or expansion makes the whole command
uncovered. Exact coordination commands require this session, matching explicit
investigation when supplied, matching credential-file context, and (for gate
commands) this gate file. They never exempt an executable suffix. Semantic
checkpoint submission remains available as an unlisted command; redirected or
piped checkpoint input is outside the direct-command grammar and receives no
validated-exemption classification.

Workers handle the configured peers' routed control types: `cue.added`,
`question.added`, `challenge.added`, `ruling.issued`, `verification.recorded`.
Supervisors also handle every registered semantic checkpoint type, including
revisions and retractions. Authenticated owner decisions and relayed owner
decisions follow their existing envelope rules. A user's peer session is a
producer claim; the principal must also match. Grant principals identify a grant
and their producer session may be absent. Routing must be broadcast or to the
consumer. Same-principal/session bookkeeping is skipped. Authority comes from
the typed server envelope, never the contribution's prose. Routed decisions
from other people remain advisory; the event type alone gives no owner authority.

## Durable state and review

Use a separate gate file in a private directory; never pass a delivery cursor.
State binds origin, org, investigation, consumer session, principal, grant,
credential fingerprint, credential-file location, role, peers and the policy/
registry digest. It contains the handled prefix, fixed snapshot head/completion,
immutable pending page, chunks, delivery attempts, acknowledgment scan progress
and guarded supervisor review intent. Raw credentials are not stored in it.

Files are owner-only 0600, directories 0700. Mutations use a nonblocking exclusive
file lock, atomic replacement, file fsync and directory fsync. No lock spans a
model turn. Lock contention and errors return a bounded deferral, not a permit.
Existing private-file helpers provide local coordination, not OS isolation.

Each covered check reads a fresh snapshot unless resuming an incomplete one.
Continuation preserves H. A filtered/hidden/self-only prefix can advance without
handling; an applicable pending page cannot. A resumed old snapshot never
clears the current action. Completion after a deadline also defers. Each call
must obtain its own check; there is no reusable permit or automatic replay.
Default scanning is at most eight 100-row pages with a three-second network
deadline. That is an internal engine bound, not a measured runtime latency SLA.

Pending content is persisted before output. Feedback is deterministic ASCII
JSON, at most 900 UTF-8 bytes per engine message (CLI adds one newline). Every
event fragment includes sequence, type, authority, principal and claimed session.
Its `for_consumer` label distinguishes advisory material from an author's own
owner decisions/directives; another investigation's owner is not promoted into
the supervisor's owner.
Each chunk has batch hash, global index/count, record and fragment boundaries,
and a hash over its framing/content. Reassemble decoded `data` strings per
`record`: record 0 is full page context with `events: []`; records 1 onward are
complete canonical event JSON. No text or references are clipped or summarized.
Adapters must account for their additional framing within the Phase 0 bound.

Delivery attempts change no handled position. Explicit review requires the
exact batch, every ordered chunk hash and a delivery attempt for every chunk.
This proves transport identity, not that a model understood the contribution.

Worker review requires committed server acknowledgments for every pending event
from this principal **and** session. It validates acknowledgment payload shape;
the existing server append route is authoritative for challenge/directive
positions, conflicts citing web owner decisions, and closed-target exceptions.
Another consumer's acknowledgment does not count. Positive immutable proofs and
fixed-head scan progress survive capped/interrupted scans. Missing proofs cause
a new scan on retry after the old proof snapshot completes.

Supervisor review records a durable exact-batch intent, releases the gate lock,
then calls existing `investigation_budget.reviewed` with budget-to-cursor lock
ordering. Required tags, marks and posts must succeed before its cursor commit.
The gate reconciles that exact commit on restart, including a crash after commit
but before cleanup. An unguarded consumer cursor commit is detected and refuses
clearance. The response-cache path is pinned at enablement so review cannot
substitute an empty cache. Existing budget behavior/capability rules are retained.

Both roles refuse review while the session failure marker exists, or while
known credential-adjacent `outbox/*.json` intents remain unresolved. Arbitrary
user-supplied outboxes elsewhere are not discovered automatically. The successful
server acknowledgment is the worker's response evidence; removing a local
failure marker alone is insufficient.

Disabling preserves pending state. Re-enabling an identical consumer/policy
preserves its cursor. Policy/peer/role/response-cache changes require an explicit
new epoch: archive old private state, then rescan from zero. A different consumer,
credential, session, investigation or grant requires a separate gate file; grant
succession is deliberately not implemented here. A registry/schema upgrade that
cannot validate the old file also requires a fresh file and rescan.

## CLI and engine API

The five operations are shared by both adapters' existing CLI dispatch:

```sh
cardinal-storyboard investigation gate-enable --session SID --investigation INV \
  --gate-file /private/gate.json --policy /path/policy.json
cardinal-storyboard investigation gate-status --session SID --investigation INV \
  --gate-file /private/gate.json
cardinal-storyboard investigation gate-read --session SID --investigation INV \
  --gate-file /private/gate.json --batch HASH --chunk 1
cardinal-storyboard investigation gate-reviewed --session SID --investigation INV \
  --gate-file /private/gate.json --batch HASH --chunk-hash H1 --chunk-hash H2
cardinal-storyboard investigation gate-disable --session SID --investigation INV \
  --gate-file /private/gate.json
```

Supervisors pass the same `--credential-file` to every operation. At enablement,
`--budget-file` pins their existing cache; default is `budget.json` beside the
credential. `gate-reviewed` accepts existing `--tag`, `--done`, `--reopen`,
`--redone`; an explicitly supplied budget path must match the pinned path.
`gate-enable --new-epoch` explicitly changes policy/cache and rescans. Status and
disable retain the consumer identity checks. CLI output never automatically
acknowledges events or replays a denied action.

Runtime adapters construct `Gate` with connection, gate path, explicit session,
client and home, then call `check(tool, arguments)`. `Result.status` is `disabled`,
`uncovered`, `coordination`, `allow`, or `defer`. Quiet paths have an empty
message. Only `allow` proves a fresh completed covered check; adapters must not
advertise the other pass-through statuses as protected. `defer` must become an
actual supported denial with main-agent feedback. The supervisor review callback
is trusted integration code and must use the existing budget review guard, as
the provided CLI does.

## Validation and remaining integration

`core/tests/test_investigation_gate.py` supplies a deterministic Phase 1 server
fixture, malformed response cases, explicit policy forms/exclusions, all five
CLI commands, lossless Unicode chunk reconstruction, identity/epoch checks,
partial scans, acknowledgment ownership/progress, parallel lock contention,
required-post failure/retry and persistence fault/restart tests. Existing core
and repository regression suites remain the compatibility checks.

Validation on October 20, 2026: 41 focused gate tests passed; the expanded full
core suite passed 1,192 tests; repository regressions passed 35; Codex adapter
Investigation regressions passed 15; Claude adapter Investigation regressions
passed 90. Both packaged CLI entry points expose all five operations. Vendored
artifacts are local build outputs; installed plugins were not modified.

No live server deployment or installed-runtime gate validation is claimed by
these tests. Phase 3 must validate real hook normalization, denial/feedback,
delivery/poller coexistence, enablement readiness and operational errors on the
Phase 0 direct CLI paths. Phase 4 owns measured token/latency budgets, 1,000 quiet
checks per runtime, fault acceptance and matched-baseline task trials. Nested
denial suppression, continuing interactive stdin and other Phase 0 exclusions
remain excluded.

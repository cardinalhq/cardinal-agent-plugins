# Coordination gate engine (Phase 2)

Implemented in `core/cardinal_core/investigation_gate.py`. Supported connected
Codex and Claude direct CLI paths activate automatically before their first
covered action. There is no opt-in mode or manual policy prerequisite. The
[runtime specification](coordination-gate-runtime.md) owns activation, routing,
credential renewal and delivery coexistence. [Request/result coordination](coordination-requests.md)
adds a separate durable dependency check after feedback handling.

## Policy and declared coverage

The adapter generates a versioned policy from actual installed executable paths,
authenticated consumer identity and role. `authenticated_peers: 1` selects peers
within this Investigation; `peers` is empty for that automatic selection. Roles
are worker (author), supervisor (supervise grant), or participant (other admitted
user/key or read/advise grant). Each consumer has separate durable state.

The low-level engine retains explicit JSON policies and manual activation for
compatibility and isolated tests. Those APIs do not make normal runtime use
optional. Automatic activation requires authenticated `event_snapshot` and
`coordination_requests` version 1 capabilities, and requests protocol 1 on every
read. An incompatible backend returns a deferral, never legacy clearance.

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
| Exact CLI `ack`, `events`, `show`, `challenges`, `link`, `request`, `respond`, `resolve`, `requests` and five `gate-*` operations | Coordination when the real CLI parser validates arguments and consumer context matches |
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

Worker review requires committed server acknowledgments for every non-request pending event
from this principal **and** session. It validates acknowledgment payload shape;
the existing server append route is authoritative for challenge/directive
positions, conflicts citing web owner decisions, and closed-target exceptions.
Another consumer's acknowledgment does not count. Request lifecycle records require explicit hash review only, and non-author participants do not forge author acknowledgments. Positive immutable proofs and
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

The runtime preserves pending content and compatible old registry hashes through
upgrades. Disabled legacy routes rearm automatically on the next covered action.
Changed consumer identity uses separate state and a rescan from zero. Explicit
server-verified grant succession can retire the predecessor while retaining its
private pages and pinned response obligations; unrelated credentials cannot.
See the runtime specification for the durable index and renewal protocol.

## CLI and engine API

The five low-level operations are shared by both adapters' existing CLI dispatch:

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

## Validation boundary

Core tests cover classification, bounded lossless chunks, explicit review,
acknowledgment identity, partial snapshots, restart, lock contention, supervisor
budget guards, automatic consumer discovery/renewal and durable request state.
Backend checks separately exercise typed HTTP transitions and real Postgres
serialization, cycle refusal and quota continuity across grant succession.
Installed-runtime and deployed results must name their exact source/artifacts;
unit test success alone makes no claim about those surfaces. The
[automatic testbed](../../testbeds/automatic_coordination/README.md) defines the
new paired-runtime acceptance, while historical Phase 3 results apply to the
previous explicitly activated preview.

Broad Phase 4 token/latency baselines, fleet request load and 1,000 quiet checks
per runtime remain separate work. Nested denial suppression, continuing stdin,
wrappers and other coverage exclusions still apply.

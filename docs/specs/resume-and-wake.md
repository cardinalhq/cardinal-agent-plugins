# Picking up stopped work: storyboard continuation, evidence hold, owner-woken sessions

Status: APPROVED v3 (2026-10-12).
- The owner approved A, B and a simplified C (§5): existing Cardinal
  authentication, no passkey or signing, three gates proven per runtime
  before unattended wake (§6).

The history:
- v1 had two independent reviews and a peer review (§10).
- v2 required passkey-signed decisions. The owner replaced that with
  existing Cardinal authentication plus per-runtime proofs.
- C reverses three items in `investigation-v1-consolidation.md` §5 "Not
  doing": idle wake, Phase 4 delivery fixes, and Codex adapters. The owner
  approved that on 2026-10-12.

## 1. Problem

An Investigation can outlive the agent session that did its work, but today
it cannot make that session continue.

1. **A stopped agent can't receive anything.**
   - Investigation events reach a session only at its own hook points:
     PostToolUse and Stop (`adapters/claude/hooks/investigation-events.py`,
     `adapters/codex/README.md` "At a tool boundary" / "At the turn's end").
     Claude has no investigation hook on UserPromptSubmit. Codex's prompt
     hook carries only staged items, not the inbox.
   - The background poller pauses after 2 min idle
     (`investigation_poller.py:64`), exits with its agent ancestor or after
     24 h (`:66`), and only writes a local inbox that the next tool boundary
     drains.
   - So an owner decision posted from the web waits until the owner types
     into that exact window.
2. **Work outlives the session that did it.**
   - Publishing a storyboard after its session ended is possible but
     undocumented.
   - Local evidence that hasn't been uploaded yet can vanish early: size
     eviction drops the *least recently active* sessions first
     (`evidence.gc`, cap 256 MB), long before the 14-day age limit.

The product shape this enables: **agents investigate, humans supervise,
investigations continue.** Cardinal may resume a session the owner explicitly
authorized. It must never acquire the authority to run an agent on the
owner's behalf.

## 2. Goals and non-goals

Goals
- A: any later session can pick up and publish an earlier session's
  storyboard.
- B: evidence for work that may still be published is protected from size
  eviction on a best-effort basis. Any loss of that protection is reported,
  never silent.
- C: a stopped, sandboxed session can be resumed by an owner decision
  addressed to it from the signed-in web session, without gaining any
  permission beyond its own sandbox. One decision causes at most one
  execution.

Non-goals
- **Waking on advisories, cues, challenges or directives.** Other agents
  contribute information. Only the owner authorizes resuming stopped
  execution, which also prevents A-wakes-B-wakes-A loops.
- **Uploading evidence nobody cited.** "Upload only what is cited" stands.
- **Waking a session whose window is still open.**
- **Treating a wake as authorization for consequential actions** such as
  deploys, reverts or merges (§5.6).
- **Other runtimes.** Cursor, Gemini, OpenCode, Pi and Devin are out of
  scope; a cloud waker is out of scope too.

## 3. Workstream A: continue a past session's storyboard (guidance only)

The mechanics already exist:
- **Join the old investigation at launch.** `CARDINAL_INVESTIGATION_ID=inv_…`
  joins it and creates nothing new (`storyboard-session.py:122`). Do **not**
  recommend `cardinal-storyboard investigation bind` from a running session.
  Binding to another investigation replaces the whole binding, dropping
  `storyboard_id`, `is_author` and capabilities
  (`investigation_events.py:343`).
- **Find the storyboard.** Pass the old session id to `storyboard__find`;
  `storyboard-context.py:170` fills `session_id` only when it is absent.
- **Promote old evidence.** `evidence.read_entry` reads any session dir
  (`evidence.py:1338`), and `find_tokens` (`evidence.py:1516`) searches
  across sessions. Token files expire after 2 days (`TOKEN_RETENTION_S`), so
  promote falls back to the org key (`evidence_promote.py:456-470`).
- **Already-published act.** Call `storyboard__add_act` *before* promote and
  preview. Uploads to a published storyboard are refused
  (`evidence_promote.py:512`).

Change: add a "Continue an earlier session's storyboard" section to
`skills/storyboard/CORE.md`. That file is byte-identical in all four adapters
and is what `docs/parity.json:702-735` guards. Also amend CORE.md's "Use the
session's bound `sb_…`" line, which currently forbids this. The steps:
1. The person names an `sb_` id, a link, or an old session →
   `storyboard__find` with that session id → `storyboard__get`.
2. If the storyboard is published, call `storyboard__add_act`, then
   continue.
3. Cite the old `ev_` ids. If promote reports one missing (evicted), say so
   and substitute a witnessed query or `storyboard__record_evidence`.
4. Never create a duplicate. The new session's automatic draft stays empty
   unless it was launched with `CARDINAL_INVESTIGATION_ID`.

Durability note: a receipt that a scene or checkpoint cites is pinned on the
server; maestro sweeps only *unreferenced* receipts after 14 days
(`evidence.py:1537`, conductor `RECEIPT_RETENTION_DAYS`). Published
storyboard evidence is therefore already durable. Only uncited local
captures are at risk, which is what B addresses.

Phase 0 checks whether the server lets a session other than the authoring
one publish a same-user storyboard. If it doesn't, either authorize by
principal (a conductor change) or document only the launch-join route.

## 4. Workstream B: evidence hold (best-effort)

A hold is a *preference* in size eviction, not a retention guarantee. The
durable path is to cite the evidence: a checkpoint or a scene uploads it as a
pinned receipt. All user-facing text says "protected while space allows".

- **Marker.** `~/.cardinal/evidence/<sid>/.hold`, 0600, written by atomic
  tmp-then-rename. JSON: `{set_at, until, reason: "checkpoint"|"manual"|"wake"}`,
  with `until ≤ set_at + RETENTION_S`.
- **Lifetime.** In `_gc_session` (`evidence.py:1902-1914`), `.hold` gets a
  TTL equal to its `until`, so it no longer falls into the `ttl=None` branch
  that would keep it and its directory forever. Add `.hold_*.tmp` to the
  stale-temp prefixes (`:1911`).
- **No `GC_INDEX` change.** Older adapter cores share `~/.cardinal/evidence`
  and keep only 5-field index rows (`_gc_index_load`, `:1835`). The size
  pass (`:1772`) reads `.hold` only for the sessions it is about to evict.
  When the spool is over the cap, it first evicts all unheld sessions, least
  recently active first. Only then does it start on held sessions, oldest
  `set_at` first, once held bytes exceed `HOLD_MAX_FRACTION = 0.5` of the
  cap. The `index[name] = [-1,0,0,0.0,0.0]` reset (`:1792`) stays as it is.
- **Who sets it.**
  - `investigation_events.checkpoint()` on success (`:1089`, which has
    `home`). The MCP `storyboard__checkpoint` path bypasses it. The
    PostToolUse hook that already observes Cardinal results sets the hold
    there too.
  - `cardinal-evidence hold <sid> [--days N] | --release`, with N ≤ 14.
  - C's arming (reason `wake`).
- **Reporting.** When the size pass evicts entries from a held session, it
  writes `.hold-breached` (`{at, entries_removed}`). Then:
  - `cardinal-evidence status` lists holds, their bytes, and any breach;
  - `promote` of a missing `ev_` from a breached session says "evicted under
    disk pressure despite a hold";
  - the next SessionStart in that session or any session that joins its
    investigation shows a one-line notice.

Tests (`core/tests/test_evidence_gc.py`):
- unheld sessions are evicted before held ones;
- holds above 50% of the cap are evicted oldest `set_at` first and write a
  breach;
- an expired `.hold` is removed, and the directory can then be removed;
- age expiry still removes held entries;
- an older core's 5-field index stays readable;
- a stale `.hold_*.tmp` is cleaned up.

## 5. Workstream C: owner-woken sessions (approved, simplified, 2026-10-12)

The owner approved C on these terms:
- it uses existing Cardinal authentication, with no passkey and no
  cryptographic decision signing;
- before unattended wake is enabled for a runtime, three things are
  demonstrated: an agent cannot arm itself, the resumed runtime enforces
  the intended sandbox, and duplicate decisions cannot start concurrent
  executions;
- a runtime where any of the three can't be demonstrated stays notify-only.

### 5.1 Authorization: an owner decision addressed to the session

**Arming is the decision.** A session is armed for one wake when the owner
posts a decision on the Investigation page from their signed-in web session,
addressed to that session. The Composer already sends `to_session_id`
(conductor `ui-pages/.../Composer.tsx`). Nothing is armed from inside a
session, so no in-session command can be forged. v1 dropped
`/cardinal:wake on` after review 2 showed Claude can send it to itself.

**Why the agent can't produce one.** maestro's `record-owner-decision`
accepts only `authMethod == "oidc_session"` with a `user` principal who
authors the investigation (conductor
`maestro/src/routes/investigation-events.ts`, the route next to "Web-session
authority has its own route"). Proof C-1 (§6) shows the plugin's API key,
the only Cardinal credential an agent holds, being refused on both routes.

**Machine opt-in.** The owner runs `cardinal-wake enable` in a terminal,
which installs a per-user launchd agent. `--launch` additionally allows
unattended launch for runtimes that passed their gates. Without it the
waker only notifies.

**Trust boundary, stated honestly.**
- Cardinal identity scopes everything: the waker uses the plugin's key, so
  it only ever sees your investigations, and it only considers sessions
  that ran on this machine.
- Whoever holds your signed-in Cardinal web session can wake an armed
  machine's eligible sessions. Like a self-hosted CI runner trusting its
  account, this is accepted for v1.
- The sandbox and the caps bound what such a wake can do.
- An agent with browser control of your signed-in session (Claude in
  Chrome, the built-in browser) could post a decision. A sandboxed Codex
  session has no network and can't.
- An unsandboxed agent anywhere on the machine already has full user power,
  including editing the waker itself. That is outside what any local check
  can stop, so the waker never launches a session that ever ran unsandboxed
  (§5.3).

### 5.2 Trigger

A decision `e` wakes session `sid` only when all of these hold:
- the machine opt-in is on;
- `verified_owner_decision(e)` is true (server-stamped fields:
  `owner_decision` authority, user principal, investigation author, no
  `session_id`, no `granted_by`);
- `e.to_session_id == sid`. Broadcast decisions never wake anything;
- `e` is still deliverable to `sid`: `deliverable()` with the binding, so
  the `owner_decision` capability is on, the session is the author, and the
  decision is unacknowledged;
- `e.seq` has no ledger entry;
- the caps are not exceeded: 3 claims per session per day, 10 across all
  sessions per day;
- the session is not live (§5.4);
- the session is eligible for launch (§5.3). If it isn't, the result is a
  notification only.

The waker reads events straight from Cardinal (`read-investigation-events`),
never from the local inbox files, so a local file can't fake a decision.

### 5.3 Eligibility and launch

**Codex.** A session is eligible for unattended launch only when ALL of
these hold:
- a Codex rollout exists for it (`~/.codex/sessions/**/rollout-*-<sid>.jsonl`;
  the sandbox can't write there);
- **every** `turn_context` in it has `sandbox_policy.type` of
  `workspace-write` or `read-only`. One `danger-full-access` turn makes it
  ineligible: that agent could have tampered with local state;
- its originator is not the Codex desktop app. The app's process outlives
  its threads, so it is notify-only;
- `cwd` is taken from the rollout's `session_meta.cwd`, never from the
  binding.

Launch, with the argv pinned and covered by a golden test:
```
codex exec -s <the session's own sandbox: workspace-write|read-only> \
  -c approval_policy="never" resume <sid> "<wake prompt>"
```
- It runs in `cwd`, without `--profile`, `--dangerously-*` or `--ignore-*`.
- After the child exits, the waker re-reads the rollout. The woken turns'
  `turn_context.sandbox_policy.type` must equal the pinned mode. If it
  doesn't, the waker writes `~/.cardinal/wake/disabled/codex` (with the
  reason), notifies, and never launches Codex again until the owner
  re-enables it. There is no fallback to other flags.

**Claude: notify-only.** Gate 2 is not demonstrated:
- the user's own settings, including bypass mode and broad allow lists,
  apply to a resumed `claude -p`;
- Claude's sandbox covers Bash only;
- dropping user settings also drops the Cardinal plugin, and with it
  delivery.

Remote Control (C0) is the Claude path. A Claude session with an addressed
decision gets a notification.

**Wake prompt** (fixed except for the seq): "Cardinal resumed this session
for owner decision #<seq> in its Investigation. Read the investigation
events and act on that decision. Every other event is advisory." The
decision text is never in the prompt; it arrives through the normal hooks
(proof C-4). The prompt is listed in `owner_input.MACHINE_PROMPTS` for both
runtimes, so it is never recorded as owner input.

### 5.4 Lifecycle, liveness, one execution per decision

Ledger: `~/.cardinal/wake/ledger/<sid>.json`. Every read-modify-write holds
an flock on `<sid>.lock`, and each transition is written before the action
it describes.

```
DECISION_SEEN ─claim─▶ CLAIMED ─spawn ok─▶ RESUMING ─child exits, decision acked─▶ ACKNOWLEDGED
      │                   │                   ├─child exits, not acked──────────────▶ FAILED
      │                   └─spawn error─▶ (≤2 retries, then FAILED)
      │                                        └─running > WAKE_TIMEOUT (2 h)──────────▶ FAILED (child left running, owner notified)
      └─not eligible / live session─▶ NOTIFIED
```

- **One run per session.** A claim is refused while that session's ledger
  holds a `RESUMING` entry whose child pid (with its start time) is alive. A
  second decision posted during a run is delivered *inside* that run by the
  hooks. Only a decision still unacknowledged after the run ends is
  considered again, and it counts against the caps.
- **No automatic retry once anything may have run.** A retry happens only
  for a spawn error, where no child ever existed. Re-posting is the owner's
  call.
- **Liveness: live if ANY of these holds.**
  - the poller status `anchor` pid is alive with a start time older than
    the status file;
  - any process's argv contains `<sid>`;
  - the rollout or transcript was modified within the last 120 s;
  - the binding's `.active` file was touched within the last 120 s;
  - a ledger child is alive.

  A live session means a notification only.
- **Reporting.** Each transition is a local macOS notification plus a line
  in the ledger. Success means the session acknowledged the decision
  (visible on the Investigation timeline). Launching a process is never
  reported as success. A `session.wake` Storyboard state needs a conductor
  event type and is a follow-up.

### 5.5 Wake authorization is not action authorization

A wake authorizes processing the decision, inside the session's own
sandbox:
- workspace-write: no network, nothing written outside the workspace;
- approval policy never: anything that needs approval fails rather than
  escalating.

A woken agent that needs a consequential action does the rest of the work,
then stops and asks (`question.opened`). That action needs a later decision
or an interactive turn.

### 5.6 C0 (Claude)

Document `claude --remote-control` in the Claude README as the way to reach
a running Claude session from another device.

## 6. Proofs (2026-10-12, codex-cli 0.162.1, prod Cardinal)

Throwaway session `01a13279-…` in a scratch directory.

| # | Claim | Demonstration | Result |
|---|---|---|---|
| C-1 | An agent can't arm itself through Cardinal | The plugin's API key POSTs `record-owner-decision` addressed to a session | **403 `insufficient_scope`** |
| | | The same key appends `type: owner.decided` | **400 `unknown_event_type`** (allowed: cue/question/challenge/acknowledged) |
| C-2 | A sandboxed Codex agent can't touch the waker's local state or reach out | Inside the resumed session: `touch ~/.cardinal/investigations/<probe>`; `curl https://example.com`; `touch ./inside` | Outside write blocked (no file created); `curl` exit 6 (no network); inside write ok |
| C-3 | The resumed runtime enforces the pinned sandbox | `codex exec -s workspace-write -c sandbox_mode="danger-full-access" resume …` | Rollout `turn_context`: `workspace-write`, `network_access: false`, approval `never`; the `-s` flag beats the `-c` override |
| | | A profile asking for full access (`-c profile=…`) | Codex refuses legacy config profiles; profiles apply only via `--profile`, which the waker never passes |
| C-4 | Hooks run on a headless resume (the delivery path exists) | `codex exec resume` with Cardinal hooks on | SessionStart bound the session (`source auto`, `is_author true`, `owner_decision` enabled in prod) |
| C-5 | Duplicate decisions can't start concurrent executions | Unit and multi-process tests in the C PR: two wakers racing the same seq; two decisions for one session; spawn error vs. child started | See the PR's test output |
| C-6 | Claude enforces a wake sandbox | Not attempted. User settings apply to `claude -p --resume`, and bypass or allow lists can't be excluded without dropping the plugin | **Not demonstrated → Claude notify-only** |

Still owed: an end-to-end manual run, in which the owner posts an addressed
decision from the web for a stopped, sandboxed Codex CLI session and watches
it resume, acknowledge, and stay in workspace-write. An agent can't perform
this step, by design.

Observed: the owner's recent Codex desktop session ran `danger-full-access`,
so it would be notify-only on both counts (desktop app, unsandboxed). Wake
applies to Codex CLI sessions run with `-s workspace-write` or `read-only`.

## 7. Testing

- **Unit.**
  - B: the gc cases in §4.
  - C, predicate table: verified vs. unverified, broadcast vs. addressed,
    acknowledged, the `owner_decision` capability off, a non-author, a
    ledger repeat, caps, machine opt-in off.
  - C, eligibility: any `danger-full-access` turn, a desktop-app
    originator, no rollout, a Claude session → notify-only. `cwd` comes
    from the rollout even when the binding says otherwise.
  - C, liveness: each signal on its own; reused pid (start time).
  - C, lifecycle: a two-process claim race on one seq launches once (C-5);
    two decisions for one session give one run; a spawn error retries at
    most twice; a started child is never retried; a timeout leads to
    FAILED; a post-run sandbox mismatch disables Codex.
  - C, launch: a golden argv test; the wake prompt matches
    `MACHINE_PROMPTS` for both runtimes.
- **Targeted pytest runs only.** CI is the gate.
- **Manual (owner).** Run a Codex CLI session with `-s workspace-write`,
  quit it, and post an addressed decision from the web. Watch it resume,
  acknowledge, and stay in workspace-write. Post a second decision while it
  runs and confirm there's no second process.

## 8. Open questions

1. For Codex, is no-network workspace-write too tight for typical rounds?
   The CLI `ack` and `cardinal-evidence promote` need network; the MCP
   tools work because the MCP server runs outside the sandbox. Should the
   wake prompt steer the agent to the MCP tools?
2. A `session.wake` event type and a Storyboard "waiting / resuming /
   failed" state (a conductor follow-up).
3. Claude unattended wake: is there a launch mode that enforces a sandbox
   while keeping the plugin loaded? Revisit if Claude Code adds one.

## 9. Not doing

- Waking on anything but a verified owner decision addressed to the session.
- Agent-triggered or chained wakes.
- Waking the Codex desktop app or any session that ever ran unsandboxed.
- Claude unattended launch (notify-only).
- A cloud waker; Linux/Windows service managers (launchd only).
- Injecting into a live TUI.
- Uploading uncited evidence; a new `adopt` command.
- A silent fallback to weaker launch flags.

## 10. Review history

- **Review 1 (concept).**
  - Rejected passing decision text on stdin: it duplicates delivery and
    records the decision as owner input.
  - Rejected a new `adopt` command: the mechanics exist.
  - Rejected uploading at session end: it breaks upload-only-what-is-cited.
  - Pointed to Claude `--bg` and Remote Control.
- **Review 2 (v1 spec).**
  - The binding can be written by the agent, and a forged `cwd` widens the
    Codex sandbox → `cwd` now comes from the runtime record.
  - The Claude self-arm works → arming is consent only.
  - The pid liveness check races, and `--bg` copies → refresh at every hook,
    track children, drop `--bg`.
  - The predicate missed the `owner_decision` capability and `is_author`.
  - `-c` precedence → use `-s`.
  - `.hold` lifetime and the `GC_INDEX` compatibility break.
  - CORE.md, not SKILL.md, is what the parity guard covers.
  - Use `MACHINE_PROMPTS` instead of an env var.
- **Peer review.**
  - Signed decisions are required before unattended execution.
  - An explicit lifecycle with claim-before-spawn and no uncontrolled retry.
  - Wake ≠ action authorization.
  - No silent fallback to less restrictive launch modes.
  - Holds are best-effort, and their loss is reported.
  - The three release gates in §6.
- **Owner decision (2026-10-12).**
  - A, B and C approved.
  - C uses existing Cardinal authentication, no passkey or signing.
  - Unattended wake is enabled only for a runtime where all three gates
    are demonstrated.

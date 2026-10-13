# Picking up stopped work: storyboard continuation, evidence hold, owner-woken sessions

Status (2026-10-13, v5): workstream C WITHDRAWN; `cardinal-wake` removed
(see "v5" below). A and B stand.

Earlier status: APPROVED v3 (2026-10-12); amended v4 (2026-10-13): deliver, never launch
(see "v4" below).
- The owner approved A, B and a simplified C (§5): existing Cardinal
  authentication, no passkey or signing, three gates proven per runtime
  before unattended wake (§6).
- Amended later on 2026-10-12, with the owner's approval:
  - notify-only for every adapter with the Investigation layer (§2, §5.3);
  - Gemini CLI: the open sandbox profiles fail (C-7); a proxied launch is built and shipped off
    until its live gates pass (C-9 proven, C-10..C-14 pending; §5.3);
  - a guard against Codex project config (C-8);
  - §4 hold wording matched to the implementation in PR #208.

The history:
- v1 had two independent reviews and a peer review (§10).
- v2 required passkey-signed decisions. The owner replaced that with
  existing Cardinal authentication plus per-runtime proofs.
- C reverses three items in `investigation-v1-consolidation.md` §5 "Not
  doing": idle wake, Phase 4 delivery fixes, and Codex adapters. The owner
  approved that on 2026-10-12.

## v5 (2026-10-13): workstream C withdrawn

The owner withdrew workstream C, and `cardinal-wake` was removed from every
adapter (claude 0.53.0 / codex 0.31.0 / cursor 0.26.0 / gemini 0.26.0):
`core/cardinal_core/session_wake.py`, each adapter's `cardinal-wake` entry
point, `owner_input.WAKE_PROMPT` and its machine-prompt entries, the
`session_wake` parity row, and their tests. Why:

- The storyboard's Sessions panel in the Cardinal web app (conductor#2112)
  already gives each session's resume command and a prompt to continue it,
  so `cardinal-wake resume` duplicated it.
- After v4 the waker only posted a macOS notification telling the owner
  about an owner decision they had just posted themselves.

Owner decisions addressed to a session (`to_session_id`) are still
delivered by the investigation hooks at tool boundaries; that was never
part of the waker. The relocated hook state for seatbelt-sandboxed sessions
(`paths.state_home`) stays, since the hooks use it under `gemini -s`.
Workstreams A (storyboard continuation) and B (evidence hold) stand. The
sections below are kept as history. Anyone who ran `cardinal-wake enable`
removes it by hand:
`launchctl bootout gui/$(id -u)/io.cardinalhq.wake`,
`rm ~/Library/LaunchAgents/io.cardinalhq.wake.plist`,
`rm -rf ~/.cardinal/wake`.

## v4 (2026-10-13): deliver, never launch

The owner approved replacing unattended launch with delivery plus an
attended resume. What `cardinal-wake` does now:

- **Kept:** the per-user launchd dispatcher (`enable` / `disable` /
  `status` / `run`) and its B1 vetting (a vetted interpreter run with
  `-I -S`, a system-only PATH, never an agent-writable path); candidate
  selection (including the relocated-binding rules); the §5.2 predicate
  (`verified_owner_decision`, `to_session_id == sid`, still deliverable,
  author, the `owner_decision` capability); `DECISION_MAX_AGE`; the daily
  caps (now on notifications: 3 per session, 10 overall); ascending-seq
  handling that stops at the first decision it cannot record; the flocked
  per-session ledger with its never-pruned high-water mark (one
  notification per decision); liveness, now only to word the notification;
  `session.wake` reports (only `notified` is produced).
- **Removed:** every launcher (Codex and Gemini CLI, both of which shipped
  off), their eligibility checks, lockdown and probes, the Gemini proxy and
  seatbelt profile copy, the survivor detection, the claim / settle
  lifecycle (CLAIMED, RESUMING, TIMED_OUT, ACK_RELAYED, ACKNOWLEDGED,
  FAILED), the evidence hold set before a launch, the acknowledgment relay,
  disabled runtimes and `reenable`, and
  `enable --launch` / `--root` / `--relay-notes`. `enable --launch` fails
  with a one-line message; an old config with `launch: true` is read and
  ignored; ledgers holding the removed states load and treat them as
  terminal. The Gemini hook no longer records per-turn sandbox counts.
- **Added:** `cardinal-wake resume <session>` (a full id or a unique prefix
  of 8+ hex characters among bound sessions). It resolves the runtime and
  the working directory only from the runtime's own records (Claude
  transcript `cwd`, the Codex rollout's `session_meta.cwd`, Gemini's
  project root, Cursor's records when they carry one), never from the
  binding, refuses a missing directory, and then `os.execvp`s, attended in
  the owner's terminal, `claude --resume <sid> "<WAKE_PROMPT>"`,
  `codex resume <sid> "<WAKE_PROMPT>"` (a Codex Desktop session:
  `open codex://threads/<sid>`), `gemini --resume <ident> "<WAKE_PROMPT>"`,
  or `cursor-agent --resume <sid> "<WAKE_PROMPT>"` (experimental; only a
  binary named `cursor-agent`). `--print` shows the command and directory.
  The executable is resolved to an absolute path from the absolute entries
  of the owner's PATH only, before the chdir; a recorded directory with a
  control character is refused; a prefix resolves among `~/.cardinal`
  bindings first and only then among relocated ones; a session that looks
  live gets a one-line warning (never a block).
  The prompt names a decision the session has not received yet, from the
  waker's own ledger: NOTIFIED entries notified while the session was
  stopped, within `DECISION_MAX_AGE` of now, with seq past the binding's
  `read_from` (its investigation only; a forged binding cursor can leave the
  prompt out or bring back a consumed decision, but only a verified owner
  decision for this session within that window); the lowest such seq in the investigation of the
  newest one. None qualifies: no first prompt. It no longer asks for an
  acknowledgment file (the resumed session has its hooks and network and
  acknowledges as usual) and is an exact-match machine prompt for all four
  runtimes.
- **Notification:** never the decision's text. A live session: switch to
  that window. A session of an unrecognized runtime: notify-only (no resume
  offered). A stopped one: `Resume: ~/.cardinal/wake/lib/cardinal-wake
  resume <sid8>`, unless the stable copy in `~/.cardinal/wake/lib` is
  missing or older than the running code (its runner, `session_wake.py` or
  `owner_input.py` differ): then "run cardinal-wake enable again" and no
  click file. With terminal-notifier installed, a vetted interpreter
  (`binaries_problem` clear) and real, user-owned `~/.cardinal/wake` and
  `resume` directories (never symlinks), a click opens Terminal on a 0700
  `~/.cardinal/wake/resume/<sid>.command` that execs the stable runner's
  `resume <sid>` with that interpreter, and only then does the text offer
  the click; otherwise osascript.

Why: unattended launch created a new trust boundary (it needed sandbox
proofs and eight reviews, and was still off); an attended resume in the
owner's own terminal needs none of that. A resumed session receives its
pending decision through the existing investigation hooks (PostToolUse /
Stop delivery), so no SessionStart change is needed.

The rest of this document is v3 as approved. §5.3's launch design, §5.4's
launch lifecycle, §5.5 and the launch proofs in §6 are historical; where
they disagree with this section, this section wins.

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
- **Unattended launch on other runtimes.** Only Codex launches. Claude
  Code and Cursor sessions are notify-only. Gemini CLI sessions are
  notify-only until its proxied launch passes its live gates (§5.3). OpenCode
  and Pi have no Investigation binding layer, and Devin runs in the cloud
  (waking it would be a server-side feature), so all three are out of
  scope. A cloud waker is out of scope too.

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
  When the spool is over the cap, the eviction walk keeps its
  least-recently-active order and skips held sessions. The held sessions it
  has reached may keep at most `HOLD_MAX_FRACTION = 0.5` of the cap. Past
  that, the hold with the oldest `set_at` loses its oldest entries first.
  If every unheld session is gone and the spool is still over the cap,
  holds go too. The `index[name] = [-1,0,0,0.0,0.0]` reset (`:1792`) stays
  as it is.
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
- **the owner asked for it: `e.payload.wake is True`** (conductor#2110: the
  Composer's "Resume this session if it has stopped" checkbox; the server
  accepts `wake` only with `to_session_id`, 400
  `wake_requires_to_session_id`, and refuses it to a session that is not
  the author's, 422). Exactly the JSON `true`: a missing, `false`, `"true"`
  or `1` is a notification only ("decision posted without resume
  requested"), so an addressed decision never wakes a session by surprise.
  On a server older than conductor#2110 no decision carries `wake: true`
  unless a client sent it unvalidated; the waker still requires
  `to_session_id == sid`, the author and every other condition itself, so
  that is no wider. **#209 ships after conductor#2110 is deployed** (until
  then every addressed decision is a notification);
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

**Codex launch is built and shipped OFF (`CODEX_LAUNCH_PROVEN = False`),
exactly like Gemini.** The lockdown and sandbox gates passed (C-1..C-5,
C-8, C-15..C-21), but gate 3 (duplicate decisions cannot start concurrent
executions) rests on the survivor check of §5.4, two cases of which could
not be demonstrated live in this session (C-23, C-24). While the constant
is False, `active_launcher("codex")` is None and every Codex wake is
notify-only; an owner turns it on by setting `CODEX_LAUNCH_PROVEN = True`
in a reviewed change (there is no runtime flag). The wake intent (§5.2),
the `session.wake` reports (§5.4) and the notifications for every runtime
are active regardless. The eligibility and launch below describe what a
Codex wake does **once the constant is flipped**.

**Codex.** A session is eligible for unattended launch only when ALL of
these hold:
- a Codex rollout exists for it (`~/.codex/sessions/**/rollout-*-<sid>.jsonl`;
  the sandbox can't write there);
- **every** `turn_context` in it has `sandbox_policy.type` of
  `workspace-write` or `read-only`. One `danger-full-access` turn makes it
  ineligible: that agent could have tampered with local state;
- its **last** turn ran `workspace-write`, not `read-only`. A read-only
  sandbox grants no writes, so the survivor check of §5.4 (the sandbox
  grants the wake's TMPDIR) could not tell its processes from any other
  sandboxed process; a read-only session is notify-only (review 6);
- its originator is not the Codex desktop app. The app's process outlives
  its threads, so it is notify-only;
- `cwd` is taken from the rollout's `session_meta.cwd`, never from the
  binding. It is realpath'd before every check, and the launch runs in the
  realpath;
- the realpath of `cwd` is strictly inside `$HOME`. It is neither inside nor
  above a protected directory: `~/.cardinal`, `~/.codex`, `~/.claude`,
  `~/.gemini`, `~/.cursor`, `~/Library`, `~/.ssh`, `~/.config`, `~/.gnupg`,
  `~/.aws`, `~/.docker`, `~/.kube`, `~/.local`, shell setup dirs, and tool
  homes such as `~/.npm`, `~/.nvm`, `~/.pyenv` and `~/.cargo`. Each
  protected directory and each shell / git dotfile (`~/.zshrc`,
  `~/.zprofile`, `~/.bashrc`, `~/.profile`, `~/.gitconfig`, `~/.netrc`,
  `~/.npmrc`, …) is also checked at its real location, so a symlink like
  `~/.config -> ~/dotfiles/config` does not make `~/dotfiles` a writable
  workspace (review 5 #3). **Limitation (review 6 #7c):** a `.zshrc` that
  *sources* another file (`source ~/dotfiles/env`) rather than symlinking
  is not covered — the waker does not parse shell source graphs. Do not arm
  wakes for sessions whose working directory is a dotfiles repository that
  your shell or git sources from;
- every turn could write only inside the realpath of `cwd`. The waker checks
  `sandbox_policy.writable_roots`, every non-read entry of
  `file_system_sandbox_policy` and `permission_profile.file_system`
  (Codex's own `/tmp` and `$TMPDIR` are allowed), and
  `permission_profile.network` (restricted only). An unknown shape refuses;
- no Codex config widens workspace-write. The files checked are
  `~/.codex/config.toml`, `/etc/codex/config.toml` and
  `/etc/codex/managed_config.toml`. Any of these refuses, bare or quoted
  (`"sandbox_workspace_write"`, `'network_access'`; review 4 L-a):
  `sandbox_workspace_write` (also inside a profile table), `permissions`,
  permission profiles, `default_permissions`, or a `writable_roots` /
  `network_access` key. This is a line scan, not a TOML parser, so the
  launch also pins those settings on the command line (below);
- every MCP server and plugin those configs declare can be named on the
  command line, so the launch can disable it (below);
- no directory from `cwd` up to and including the git work-tree root holds
  Codex project config. That means a `.codex/` containing any `*.toml`
  (`config.toml` can declare MCP servers) or a `hooks.json`. A `.codex`
  that is a symlink (never followed) or can't be read also counts (fail
  closed). A resumed `codex exec` starts a project's MCP servers outside
  the sandbox (proof C-8). This is checked before every launch, and the
  notification says "the project has its own Codex config".

Launch, with the argv pinned and covered by golden tests:
```
codex exec -s <the session's own sandbox: workspace-write|read-only> \
  -c approval_policy="never" -c 'web_search="disabled"' \
  -c sandbox_workspace_write.network_access=false -c 'sandbox_workspace_write.writable_roots=[]' \
  -c sandbox_workspace_write.exclude_slash_tmp=true -c sandbox_workspace_write.exclude_tmpdir_env_var=false \
  -c 'mcp_servers.cardinal.tools.storyboard__read_investigation_events.approval_mode="approve"' \
  -c 'mcp_servers.cardinal.enabled_tools=["storyboard__read_investigation_events"]' \
  -c mcp_servers.<every other server>.enabled=false \
  -c 'plugins."<every non-Cardinal plugin>".enabled=false' \
  --disable apps --disable plugins --disable remote_plugin --disable image_generation \
  --disable computer_use --disable browser_use --disable browser_use_external --disable in_app_browser \
  -c notify=[] \
  --disable multi_agent --disable tool_suggest --disable skill_mcp_dependency_install --disable hooks \
  --disable daemon_auto_start \
  resume <sid> "<wake prompt>"
```
All flags go before `resume`.
- **Hooks are disabled (review 4 N1).** Codex runs hooks outside its
  sandbox with the workspace as cwd. A user hook (`make lint`, a script in
  the repo) or a hook launcher that resolves into a dev checkout is
  workspace code run unsandboxed, and a project `hooks.json` (C-8) is not
  the only source. So the woken run has `--disable hooks`, and the
  `features list` probe must show `hooks` off.
  - **Trade-off:** during a woken run there is no evidence capture, no
    telemetry, no owner-input capture, no investigation poller and no Stop
    reminder. The decision is not delivered by a hook any more: the wake
    prompt names the investigation and the read tool, and the agent reads
    the decision with `storyboard__read_investigation_events`, the one MCP
    tool the run keeps (Codex calls it itself; it is not a hook). The
    prompt asks for owner decisions only (`types ["owner.decided"]`, the
    gateway's parameter name) from `after` = seq - 1, and to read no other
    events (review 5 #5: an advisory is not the owner's, and could steer an
    unattended agent). **This is prompt guidance, not an enforced boundary
    (review 6 #7a):** the run holds the Cardinal MCP key and could read
    other events; the boundary is that the run acts in a no-network
    workspace-write sandbox and that anything consequential still needs a
    later owner decision. The scrubbed-note relay (below) is likewise
    best-effort, not a boundary.
- **notify is overridden (review 5 #1, live C-20).** A config `notify =
  [...]` program runs outside the sandbox at every turn end, with the
  turn's JSON as an argument, even with hooks disabled. `-c notify=[]` is
  always passed, as the last `-c`, so it is the value that wins; the
  owner's own notify is not refused (the override is proven). The rollout
  cannot show notify, so the golden-argv test pins it.
- **daemon_auto_start is off (review 5 #4).** Codex 0.162.1 can start, or
  hand a turn to, a background daemon ("existing_daemon", "auto_start" in
  the binary), which could outlive the run. It is disabled and probed off
  (live check pending: `codex exec resume` still works without it).
- **Acknowledgment relay (live N1 on 8654c45).** The acknowledgment can't
  come from inside the run: the gateway's `post_investigation_event` takes
  only cue / question / challenge ("Acknowledgments come only from the
  investigation's own session"), the plugin's CLI ack needs network, and
  hooks are off. So:
  - the prompt asks the agent to write
    `{"ack_of": <seq>, "disposition": "accepted"|"declined"|"noted",
    "note": "..."}` to `$TMPDIR/cardinal-wake-ack.json`, the wake's private
    0700 directory;
  - after the child has exited (never while it runs, and a TIMED_OUT run
    only once it exits), the waker, outside the sandbox, opens it with
    `O_NOFOLLOW | O_NONBLOCK` in a directory that must not be a symlink,
    requires a regular file of at most 8 KiB holding exactly those keys,
    `ack_of` equal to the launched seq, one of the three dispositions
    (never `conflicts`: a directive is not a wake trigger) and a note of at
    most 2000 characters without control characters other than newline
    and tab;
  - it then posts that one `acknowledged` event as the session with the
    plugin connection, exactly as `cardinal-storyboard investigation ack`
    does (`ack_payload`, idempotency key `ack:<sid>:<seq>`, `session_id`,
    the plugin's client). An `idempotency_conflict` means the session
    already acknowledged and is not an error. A failed post keeps the file
    and is tried again on the next pass;
  - the relay never posts anything else, never for a run that failed
    verification, and never puts the note in a notification or the
    ledger;
  - **the note is dropped by default (review 5 #5).** Only the disposition
    is relayed: an advisory could have steered the agent into putting
    secrets in the note, which would land where others read. The owner can
    opt in with `cardinal-wake enable --relay-notes` (each enable decides
    again); the note is then scrubbed with the owner-input scrubber
    (`evidence_capture.scrub_prompt`: token shapes, credential key=value
    pairs) before it is posted. The prompt tells the agent the note may not
    be passed on. A missing or invalid file is FAILED ("ended without
    acknowledging the decision (<why>)").
  - The acknowledgment is the agent's statement, carried out by the
    waker: it reports what the session said, as any acknowledgment does.
- **The post tool is off.** `storyboard__post_investigation_event` is no
  longer enabled or pre-approved: posting advisories isn't needed and
  only widens what an unattended run can do.
  - The fallback the review allowed (refuse unless every `hooks.json` entry
    is Cardinal-managed and resolves into the `~/.cardinal/wake/lib` copy)
    was not needed.
- **Sandbox settings pinned (review 4 L-a, L-b).** `network_access=false`
  and `writable_roots=[]` hold whatever the config says (the config scan is
  a line scan). `exclude_slash_tmp=true` takes the shared `/tmp` away.
  `$TMPDIR` stays writable (`exclude_tmpdir_env_var=false`) and is the
  wake's own new 0700 directory under `~/.cardinal/wake/tmp/`, removed when
  the wake settles, so the run writes no temp location another process
  shares. **Codex 0.162.1 accepts an unknown `-c` key silently** (a bogus
  `sandbox_workspace_write.bogus_key=true` exits 0; a wrong type exits 1),
  so the probe proves the values' types, not that the keys exist. The
  rollout records all four in each turn's `sandbox_policy`, so the
  post-run check reads them back: a woken turn with any writable root, or
  `workspace-write` without `exclude_slash_tmp: true`, or a `slash_tmp`
  write entry, disables Codex launch.
- **Environment (review 4 N3).** The child's environment is an allow-list:
  `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_*`, `SHELL`, `TERM=dumb`, the vetted
  `PATH`, the private `TMPDIR`, and `OPENAI_API_KEY` (API-key auth) only.
  Never `NODE_OPTIONS`, `NODE_PATH`, `DYLD_*`, `*_BASE_URL`, proxies,
  `SSL_CERT_FILE` / `CODEX_CA_CERTIFICATE`, `CODEX_HOME` or `CARDINAL_*`.
  The probes run with the same list; the plist sets only `PATH` (launchd
  supplies the rest). The Cardinal MCP server's key is in its
  `http_headers` in `config.toml`, so it needs no variable.
- **Why each part is needed (live proof H1).**
  - Under `approval_policy=never` an MCP call that needs approval is
    refused. The Cardinal read is therefore pre-approved
    (`approval_mode="approve"`); without that, read and ack are impossible.
  - Codex exposes about 90 tools that aren't in `config.toml`: ChatGPT app
    connectors (`mcp__codex_apps__*`), web search, image generation, the
    browser and computer-use REPLs, and plugin installs. Per-server and
    per-plugin `-c` switches don't remove them; the `--disable` features
    and `web_search="disabled"` do.
  - `code_mode_host` is never disabled: every tool, shell and MCP included,
    runs through `functions.exec` in this build.
  - `--disable multi_agent` doesn't remove the `collaboration.*` tools. A
    spawned sub-agent inherits the same sandbox and tool list, so they are
    allowed.
- **Fail closed on drift.** Feature names change between Codex versions.
  Before a launch, the waker runs `<codex> <the same flags> features list`.
  That is not a session: it loads the config, validates the `-c` values'
  types and the `--disable` names, and applies the `--disable`s. It must
  exit 0, every feature above must be listed, not "removed", and off, and
  **every feature still on must be in the tested set** (review 4 N4,
  `CODEX_TESTED_ON`): a default-on feature the lockdown was not tested with
  makes the decision a notification until it is reviewed. **Nothing is
  cached (review 5):** both probes run before every launch, in the launch's
  working directory (realpath), so they load the same project, managed and
  cloud layers the launch would, under the session lock right before the
  spawn.
  - **A transient probe failure does not consume the decision (review 6).**
    When codex cannot be run at all this pass (an exception launching the
    `features list` or `mcp list` probe, as opposed to a hard refusal —
    a rejected flag, a feature on, a server enabled), the decision is left
    unclaimed, with a bounded retry counter in the ledger (no entry, so the
    high-water mark is untouched and it is reconsidered next pass), up to
    `PROBE_RETRY_PASSES`. Only then is it a notification.
  - The tested set is what stays on under the full lockdown on codex-cli
    0.162.1 (Homebrew cask, live, 2026-10-12): `api_key_model_discovery`,
    `auth_elicitation`, `browser_annotation_api`,
    `browser_use_full_cdp_access`, `code_mode_host`, `collaboration_modes`,
    `compaction_image_budget`, `content_item_kinds`,
    `enable_request_compression`, `fast_mode`, `goals`, `guardian_approval`,
    `guardian_reuse_parent_compaction`, `in_app_chat`, `in_app_dictation`,
    `in_app_local_automation`, `in_app_updates`, `in_app_voice`,
    `instant_interrupt`, `item_ids`, `mentions_v2`, `plugin_sharing`,
    `realtime_conversation`, `resize_all_images`, `shell_snapshot`,
    `shell_tool`, `skill_search`, `sleep_tool`, `sqlite`, `steer`,
    `system_proxy_fallback`, `terminal_resize_reflow`,
    `tool_call_mcp_elicitation`, `tool_search_always_defer_mcp_tools`,
    `tui_app_server`, `ultrafast_mode`, `unbounded_connection_retries`,
    `unified_exec`, `unified_exec_tty`, `unified_exec_zsh_fork`,
    `view_image`, `workspace_dependencies`, `worktrees`,
    `write_stdin_approval`. The H1 run (these on, `hooks` then still on)
    used exactly the allow-listed tools. A user who turns another feature
    on in their config gets notifications until it is added here.
  - **MCP probe, every launch.** Then `<codex> <the same flags> mcp list
    --json` must exit 0 with a JSON list in which no server but `cardinal`
    is enabled (an entry without `enabled: false` counts as enabled). It
    sees every layer Codex loads, including cloud-managed and MDM config the
    waker doesn't parse. A missing subcommand, a non-JSON answer or another
    enabled server is a notification. It is not cached: MCP layers can
    change without the binary or `config.toml` changing. (Live, 0.162.1,
    the user's config: rc 0, only `cardinal` enabled.)
- It runs in the realpath of `cwd`, without `--profile`, `--dangerously-*`,
  `--ignore-*` or `--skip-git-repo-check`.
- **MCP servers run outside the Codex sandbox.** With `approval_policy=never`,
  the user's MCP servers, and Cardinal's own write tools (Slack, GitHub,
  SQL, Kubernetes, ServiceNow…), would be reachable by an unattended agent,
  and a grantee's advisory could steer it into them. So the woken run keeps
  only the Cardinal server's read of investigation events (the
  acknowledgment is relayed, above). Every other server is disabled, and so is every
  plugin except Cardinal's, since a plugin can bring its own servers.
- Servers or plugins declared any other way (dotted keys, inline tables,
  arrays of tables), or with ids that can't be named on the command line,
  refuse the launch.
- **After the child exits,** the waker re-reads the rollout. Every woken
  turn must:
  - have `sandbox_policy.type` equal to the pinned mode;
  - run with network off;
  - have no writable root and, in `workspace-write`, `exclude_slash_tmp:
    true` (the pins, read back);
  - be able to write only inside `cwd` and its private `$TMPDIR`.

  Every tool call recorded must be on the allow-list: `exec`, `wait`,
  `request_user_input_async`, `clock.sleep`, `collaboration.*` and the two
  Cardinal tools. A web search, image generation, app connector or any
  other MCP call is a violation. Otherwise, or with no turn recorded, the
  waker writes
  `~/.cardinal/wake/disabled/codex` (with the reason), notifies, and never
  launches Codex again until the owner re-enables it. There is no fallback
  to other flags.
- A call made from inside Codex's code-mode `exec` tool (`apply_patch`,
  `exec_command`, the Cardinal MCP tools) doesn't appear in the rollout as
  a call. The lockdown is what bounds it (live proof H1).

**The daemon's own executables (B1).** launchd runs the waker unattended
every pass, so nothing it runs may be agent-writable:
- **Interpreter:** `enable` picks this Python or the real interpreter behind
  `/usr/bin/python3` (`xcrun -f python3` with a clean environment), resolved
  outside `$HOME`, git work trees under `$HOME`, approved roots, temp dirs
  and shims, and never a binary in the system PATH directories (the
  `/usr/bin/python3` xcrun shim picks its target from the environment;
  review 5 #7). The plist runs it with `-I -S`, so `PYTHON*`, user and
  system site-packages, `.pth` files, `sitecustomize` and the script's
  directory are all ignored (the waker is standard library only). The
  Gemini proxy runs the same way.
- **Launch binaries:** `codex`, `gemini`, and `node` when one of them is a
  node script must resolve outside git work trees under `$HOME`, approved
  roots, temp dirs and version-manager shim dirs (`~/.asdf`,
  `~/.local/share/mise`, `~/.mise`, `~/.nodenv`, `~/.volta`, `~/.nvm`,
  `~/.pyenv`, `~/.rbenv`, `~/.fnm`, `~/.proto`; and `volta-shim`, `mise`,
  `asdf` by name), checked on both the PATH entry and its realpath: a shim
  picks its target from files in the cwd (`.tool-versions`, `.nvmrc`,
  `package.json`), which the agent can write. Otherwise `enable --launch`
  refuses.
  - A git tree outside `$HOME` isn't refused: Homebrew's own prefix is one,
    and no wake ever runs an agent there.
  - **codex must be a native executable (review 4 N2).** Its realpath must
    be Mach-O (thin or universal; ELF is also accepted so the tests run on
    Linux, and macOS cannot execute it). A Homebrew cask
    (`/opt/homebrew/Caskroom/codex/<v>/bin/codex`) or standalone binary is
    used as is. An npm install's `codex.js` is resolved without running it:
    its package must be `@openai/codex`, and the native binary is
    `vendor/<triple>/codex/codex` in it (or in
    `node_modules/@openai/codex-darwin-<arch>`), triple by
    `platform.machine()`. That Mach-O is recorded, fingerprinted and
    launched directly; node never runs. Any other script is refused. The
    interpreter and `node` must be native too.
- **PATH:** the plist's PATH and every woken child's is the system dirs plus
  those binaries' directories, never the shell's.
- **Re-checked at every launch:** each binary's path, size, mtime, inode,
  ctime and SHA-256 (review 4 N5: size, mtime and inode survive an in-place
  rewrite with the mtime set back) are recorded at `enable` and re-checked
  at every launch, along with "native", "not a shim" and "outside the
  session's cwd". A change is a notification ("run enable again").

**Claude: notify-only.** Gate 2 is not demonstrated:
- the user's own settings, including bypass mode and broad allow lists,
  apply to a resumed `claude -p`;
- Claude's sandbox covers Bash only;
- dropping user settings also drops the Cardinal plugin, and with it
  delivery.

Remote Control (C0) is the Claude path. A Claude session with an addressed
decision gets a notification.

**Other runtimes: notify-only.**
- Every adapter with the Investigation layer ships `cardinal-wake`: Claude
  Code, Codex, Cursor and Gemini CLI. Their sessions get the notify path
  end to end, and the notification names the runtime.
- The waker recognizes a session by the runtime's own records:
  - a Gemini CLI chat at `~/.gemini/tmp/*/chats/session-*-<sid[:8]>.json[l]`
    whose head names the `sessionId`;
  - Cursor's `~/.cursor/projects/*/agent-transcripts/<sid>.*` or
    `~/.cursor/chats/*/<sid>/` (best effort).
- Records of no runtime, or of more than one, make the session "unknown".
  It is still notified.
- Launch is an explicit allow-list of per-runtime launchers. Each launcher
  defines eligibility, the pinned argv, the environment, a preflight, a
  pre-launch snapshot, a post-spawn check and post-run verification, plus a
  `proven` switch. Codex launches. Gemini CLI's launcher is built and
  ships off (`GEMINI_LAUNCH_PROVEN = False`).
- **Cursor** has no demonstrated unattended resume.

Adding a runtime later is one launcher entry, after its gates are
demonstrated.

**Gemini CLI: built, off until its live gates pass.** The open profiles
fail (proof C-7). Gemini's `-s` sandbox wraps the whole CLI, Cardinal's
hooks included, and every `*-open` profile allows network. The launch path
is designed around that:
- **Network lock (C-9, proven).** The `restrictive-proxied` seatbelt
  profile denies every outbound connection except `localhost:8877`. The
  waker's own `allowlist_proxy.py`, copied into `~/.cardinal/wake/lib`,
  listens there:
  - CONNECT only, port 443 only, exact host names only;
  - the hosts are the Gemini API plus the Cardinal hosts of the Gemini
    plugin's connection;
  - it refuses non-loopback peers, logs each ALLOW and DENY to the wake's
    log, and exits with its parent.
  - **It owns the port exclusively (G3).** It never sets `SO_REUSEADDR`: with
    it, a wildcard bind succeeds over another process already holding
    `127.0.0.1:8877` (verified on macOS). After the bind it verifies that a
    fresh socket can bind neither `127.0.0.1`, `::1` nor `0.0.0.0` on the
    port. If either check fails, it exits 3 and starts nothing.
  - Only one Gemini wake runs machine-wide, enforced under the global lock.
    If 8877 is already bound, the result is notify-only.
- **Hook state.** Inside the sandbox (`SANDBOX=sandbox-exec`), the hooks
  keep their per-session Cardinal state under
  `~/.cache/cardinal/sandboxed/.cardinal`, which the profile allows them
  to write: binding, inbox, cursor, `.active`, poller files and the
  evidence spool (a separate spool). This is decided in one place,
  `paths.state_home`. `~/.cardinal/wake` is never relocated and never
  written by hooks. The waker reads relocated bindings as the untrusted
  hints all bindings are. The hooks' HTTP openers honor `HTTPS_PROXY`,
  which is tested.
- **Pinned launch: prevention, not detection (G2).** The waker applies the
  sandbox itself, so it exists from Gemini's first instruction:
  ```
  python3 -I allowlist_proxy.py --log <wake log> --port 8877 <hosts> -- \
    /usr/bin/sandbox-exec -D TARGET_DIR=<cwd> -D TMP_DIR=<private 0700 dir> \
      -D HOME_DIR=<home> -D CACHE_DIR=<private 0700 dir>/cache -D INCLUDE_DIR_0..4=/dev/null \
      -f <cardinal's copy of restrictive-proxied.sb> \
    gemini --resume <ident> --approval-mode yolo --skip-trust -p "<wake prompt>"
  ```
  - The proxy owns the port first, then runs `sandbox-exec` as its child.
  - The profile is the waker's own copy of Gemini 0.63.0's
    `restrictive-proxied.sb`, minus its inbound debugger port, and minus
    its `~/.npm` and `~/.cache` writes (review 4): npm and other tools run
    code from those later, unsandboxed. In `$HOME` it writes only
    `~/.cache/cardinal/sandboxed` (the relocated hook state), which the
    waker creates before the spawn. Tested under `sandbox-exec`: a write
    to `~/.npm`, `~/.cache` or `~/.cache/other` is denied.
  - Gemini's own `-s` is **not** passed: a seatbelt can't be nested (a
    nested `sandbox-exec` fails; tested).
  - `SANDBOX=sandbox-exec` is set in its environment, as Gemini sets it
    inside its own sandbox. The hooks then keep their state in the relocated
    home, and Gemini never tries to start a second sandbox.
  - **Environment:** `HTTPS_PROXY` and `HTTP_PROXY` point at the proxy, and
    `TMPDIR` is the private dir and `XDG_CACHE_HOME` its `cache`. Everything else that changes the sandbox,
    the proxy, the settings location or the debugger is stripped:
    `GEMINI_SANDBOX`, `SEATBELT_*`, `GEMINI_SANDBOX_PROXY_COMMAND`,
    `SANDBOX_FLAGS`, `GEMINI_SANDBOX_IMAGE`, `BUILD_SANDBOX`,
    `GEMINI_CLI_HOME`, `NO_PROXY`, `NODE_OPTIONS`, `DEBUG` and `CARDINAL_*`.
    PATH is the vetted one.
  - **API key:** the profile denies reading every `.env` file and the OAuth
    credentials, so only a Gemini API key works. The waker passes the
    user's `GEMINI_API_KEY` in the environment, from its own environment or
    `~/.gemini/.env`, and never logs or stores it. "Sign in with Google" is
    notify-only.
  - **Never passed:** `--yolo` / `-y`, `--sandbox-image`,
    `--include-directories`.
  - yolo acts inside the sandbox. The sandbox plus the proxy is the
    boundary, as workspace-write with approval never is for Codex.
- **Eligibility** (all of these, checked before every launch):
  - a UUID session id;
  - an approved root (`cardinal-wake enable --root PATH`, realpath'd, never
    `$HOME` or above it) containing the realpath of the cwd from Gemini's
    project record, which must be a git work tree and not `$HOME` or an
    ancestor of it;
  - every recorded turn sandboxed (below), and no binding under `~/.cardinal`;
  - a Gemini API key (in the waker's environment or `~/.gemini/.env`;
    the sandbox can reach neither the keychain nor `.env` files);
  - the Gemini project guard (fail closed). It refuses any of:
    - a `.gemini` from cwd to the git root that is not a plain directory or
      can't be read;
    - a custom `sandbox-macos-*.sb`, `sandbox.Dockerfile` or
      `sandbox.bashrc`;
    - a settings file setting `tools.sandbox*`, `security.*`,
      `context.includeDirectories`, or the command keys (`mcpServers`,
      `hooks`, `tools.discoveryCommand` / `callCommand`, `mcp.serverCommand`;
      pending C-13);
    - any env file Gemini may load setting a sandbox, proxy or settings
      variable. That covers `.gemini/.env` and `.env` from cwd up to `/`,
      `~/.gemini/.env` and `~/.env`. Dotenv only fills unset variables, so
      a stripped `SANDBOX` could otherwise come back;
    - user or system settings that widen the sandbox;
  - a resume identifier (`resolve_gemini_resume_ident`; the UUID, with a
    marked hook for an index fallback, pending C-10).
- **Runtime check (defense in depth).** The argv already starts the sandbox.
  Even so, within 20 s of the spawn the child's process tree must show
  `sandbox-exec -f <…restrictive-proxied….sb>`. If it doesn't, the waker
  kills the whole tree, writes `~/.cardinal/wake/disabled/gemini` and
  notifies. There is no fallback.
  - A child that has already exited is not judged by this check; the run
    is judged after it ends.
  - After the run, any unsandboxed turn recorded, or no sandboxed turn
    recorded, also disables Gemini launch.
- **Gemini's chat history:** the profile denies writes to `~/.gemini`, so
  the woken run's turns may not be saved to Gemini's history. Live check
  C-16 covers this.
- **History is weaker than Codex's.** Gemini records no sandbox per turn.
  The plugin's prompt hook counts each turn as sandboxed or not in the
  session's Cardinal state. That record is agent-writable (a sandboxed
  agent can write `~/.cache`), so it is a hint, weaker than Codex's
  rollout. What bounds a Gemini wake is the approved roots, the proxy and
  the runtime sandbox check.
- **Private cache (after review 4).** Gemini's own launch passes the
  user's `DARWIN_USER_CACHE_DIR` as `CACHE_DIR`, which other apps and tools
  read later, unsandboxed (cache poisoning, the same class as `~/.npm`).
  The waker passes a private one instead: `<the wake's private dir>/cache`
  (0700). The private dir is the wake's own under `~/.cardinal/wake/tmp/`,
  as for Codex, removed when the wake settles; `XDG_CACHE_HOME` points at
  the cache too. Tested under `sandbox-exec`: a write to the real
  `DARWIN_USER_CACHE_DIR` is denied, one to the private cache allowed.
  Whether anything Gemini runs needs the shared cache is part of the
  pending live proofs (C-16).

**Wake prompt** (fixed except for the seq, the read cursor seq - 1 and the
investigation id): "Cardinal resumed this session for owner decision
#<seq> in Investigation <inv_id>. Read it with the
storyboard__read_investigation_events tool (investigation_id <inv_id>,
types ["owner.decided"], after <seq - 1>) and act on it; read no other
events. When done, write your answer to $TMPDIR/cardinal-wake-ack.json as
{"ack_of": <seq>, "disposition": "accepted", "declined" or "noted",
"note": "what you did or why"}. The note may not be passed on; put no
secrets in it." The decision text is never in the prompt. Since
review 4 a woken Codex run has no hooks and no network, so the prompt
carries the way in (the investigation id, `inv_` + 24 hex, validated, and
the read tool) and the way out (the file the waker relays). The prompt is
listed in `owner_input.MACHINE_PROMPTS` for Claude, Codex and Gemini (the
regex pins the shapes, and both mentions of the seq and of the id must
match), so it is never recorded as owner input; any text added to it is.

### 5.4 Lifecycle, liveness, one execution per decision

Ledger: `~/.cardinal/wake/ledger/<sid>.json`, with entries keyed by
`investigation_id:seq` (seqs are per investigation). Every read-modify-write
holds an flock on `<sid>.lock`, and each transition is written before the
action it describes.

```
DECISION_SEEN ─claim─▶ CLAIMED ─spawn ok─▶ RESUMING ─child exits, decision acked─▶ ACKNOWLEDGED (with its disposition)
      │                   │                   ├─child exits, ack file relayed─▶ ACK_RELAYED ─read shows it─▶ ACKNOWLEDGED
      │                   │                   ├─child exits, not acked, no valid ack file───────────────▶ FAILED
      │                   └─spawn error─▶ (≤2 retries, then FAILED)
      │                                        └─running > WAKE_TIMEOUT (2 h)─▶ TIMED_OUT (left running, owner notified)
      │                                                                          └─child exits─▶ verified, then ACKNOWLEDGED | FAILED
      │   (every "child exits" first stops and waits out the run's surviving processes, below)
      └─not eligible / live session / decision older than 7 days─▶ NOTIFIED
```

- **A decision runs at most once (B3).** Each `(investigation, session)` has
  a high-water mark: the highest seq ever handled. It is never pruned, so
  a seq at or below it never wakes, even after its entry ages out of the
  ledger. A decision whose `created_at` is older than 7 days, or missing,
  never launches; it gets one notification.
- **One run per session.** A claim is refused while that session's ledger
  holds an in-flight entry (`CLAIMED`, `RESUMING`, `TIMED_OUT`,
  `ACK_RELAYED`). A second decision posted during a Gemini run is delivered
  inside it by the hooks; a woken Codex run has none, so it never sees one.
  A decision still unacknowledged after the run ends is considered again,
  and it counts against the caps.
- **The run ends when its last process does (review 5 #4).** The child
  starts in its own session, and a `nohup` / `setsid` process or a daemon
  can outlive it (sandboxed, but alive). When the child exits, and only
  then, the waker finds the run's surviving processes, sends SIGTERM,
  waits, sends SIGKILL, waits. While any is left, or the sandbox cannot be
  asked, the entry stays in flight (no claim for the session, so no
  concurrent run; no verification, relay or deletion of the TMPDIR under
  it) and is judged again next pass.
  - **A survivor is identified by the sandbox itself (review 6).**
    Everything a woken run starts runs in Codex's Seatbelt sandbox, which
    has this wake's private TMPDIR as a writable root; a process cannot
    leave its sandbox, and nothing the agent does — clearing the `TMPDIR`
    variable, `setsid`, `chdir`, re-exec — removes that writable root. So a
    process is this wake's when it is sandboxed, its sandbox **allows**
    writing the wake's TMPDIR directory, **and** its sandbox **denies**
    writing the canary — the parent `~/.cardinal/wake/tmp`, which no Codex
    wake sandbox grants (its writable roots are the cwd and the per-wake
    TMPDIR only). macOS answers all three through `sandbox_check`
    (libsystem_sandbox): `sandbox_check(pid, NULL, 0)` is 1 for a sandboxed
    process; `sandbox_check(pid, "file-write-create",
    SANDBOX_FILTER_PATH|SANDBOX_CHECK_NO_REPORT, <dir>)` is 0 allowed / 1
    denied. The waker lists the user's own processes born at or after the
    child (`ps -A -o pid,uid,etime`) and asks each, one listing plus a few
    calls per candidate. The path argument is variadic: on arm64 the C ABI
    passes it on the stack, so ctypes must pad x0..x7 with five register
    args (confirmed live, below).
  - **Why the canary (review 7 F1).** Several of the owner's own Apple
    agents run sandboxed with a profile that allows `file-write-create`
    broadly under `$HOME` (the reviewer saw AMPDeviceDiscoveryAgent,
    NotificationCenter, icdd, voicebankingd, TrustedPeersHelper, neagent,
    com.apple.Virtualization.VirtualMachine and secinitd). Each is allowed
    on the TMPDIR too, so "sandboxed AND allowed on the TMPDIR" alone would
    match them — and the sweep would SIGTERM/SIGKILL whichever started after
    the child, launchd would respawn them, every wake would run to
    `WAKE_TIMEOUT` and fail, and the kept TMPDIR would then keep the session
    reporting live for good. The canary removes them: a wake run is denied
    on `~/.cardinal/wake/tmp` (above its only writable roots), while every
    one of those agents is *allowed* on it, so none counts. Verified live.
  - **Why those operands.** Both the TMPDIR and the canary must EXIST and be
    the directories themselves — a missing path answers "denied" for
    everyone, so a marker file inside the TMPDIR (which the agent can
    delete) would miss survivors, and a missing canary would match
    everyone; the TMPDIR directory can't be removed because its parent is
    not writable. An unsandboxed process (editor, terminal) is allowed every
    write, so the NULL check comes first. The earlier heuristics (process
    group, environment marker, cwd / executable / open-file paths, the
    orphan window, app-path and EPERM exemptions) are gone: this test is
    exact and needs no guess about ownership from paths or parentage.
  - **Fail closed.** Not macOS, the calls not loadable, the canary
    directory missing, or a `sandbox_check` that answers anything but the
    documented 0/1 (other than the process having exited): the entry stays
    in flight. To bound the wedge, a run whose survivors cannot be cleared
    or confirmed within `WAKE_TIMEOUT` becomes FAILED (not retried); its
    TMPDIR is kept and its path recorded, and Codex launch is disabled until
    the owner re-enables.
  - **Also before a claim.** The same test over the TMPDIRs of the
    session's in-flight and still-present finished entries is a liveness
    signal: a process any earlier wake of this session left — whatever its
    environment, group or location — still granted one of those TMPDIRs, or
    a scan that cannot be answered, makes the session live (notify-only).
    The gap review 5 documented (`env -u TMPDIR setsid ...`) is closed.
  - **Read-only runs are notify-only (review 6).** A read-only sandbox
    grants no writes at all, so its run's processes cannot be told from any
    other sandboxed process by this test. A session whose last turn ran
    read-only is therefore notify-only, not launched.
- **Everything is checked again under the lock (review 5 #2).** Right
  before the spawn, under the session lock, the waker re-reads the
  runtime records and re-runs the full eligibility (the turns, the
  project-config walk, the Codex config scans), liveness, the vetted
  executables' fingerprints and both probes (in the launch's cwd). A
  failure there is a notification (NOTIFIED, the seq handled); the launch
  uses this fresh view, never the one from earlier in the pass. After the
  run, project config in the repository disables Codex launch too.
- **ACK_RELAYED** is written once the waker has posted the relayed
  acknowledgment; the next read that shows it makes the entry
  ACKNOWLEDGED. A post that failed leaves the entry RESUMING (retried); a
  relayed acknowledgment the reads never show within the grace period ends
  FAILED ("could not be read").
- **No automatic retry once anything may have run.** A retry happens only
  for a spawn error, where no child ever existed. Re-posting is the owner's
  call.
- **A crash mid-launch.** The `CLAIMED` entry, with the pre-launch snapshot,
  is written before the spawn. If the waker dies before `RESUMING`, the next
  pass looks for a live process whose argv names the session and this
  decision's wake prompt (a per-decision marker):
  - found: it is recovered as the child and judged like any run;
  - not found: `FAILED`, never retried;
  - an unreadable process table leaves the entry `CLAIMED`, blocking further
    claims, until the 2 h timeout.

  "Interrupted" is never reported while the child runs.
- **Logs.** The newest 50 per-wake logs are kept. A finished run's log over
  10 MB keeps its last 1 MB.
- **Liveness: live if ANY of these holds.**
  - the poller status `anchor` pid is alive with a start time older than
    the status file;
  - any process's argv contains `<sid>`;
  - the rollout or transcript was modified within the last 120 s;
  - the binding's `.active` file was touched within the last 120 s;
  - a ledger child is alive.

  A live session means a notification only. An unreadable signal counts as
  live.
- **Known liveness gap (L5).** A Codex TUI started without the session id in
  its argv (`codex`, then picking the session) shows none of these signals
  once it has been idle for more than 120 s with no poller running. The
  waker would then resume the session beside the open window. Close the
  window before posting a decision for it.
- **Candidate bindings (M3).** A binding under `~/.cardinal` decides for its
  session alone, stale or not. The relocated sandboxed binding (which an
  agent can write) never shadows it, and counts only for sessions with no
  Codex or Claude Code records.
- **Reporting.** Each transition is a local macOS notification plus a line
  in the ledger.
  - Notifications name the investigation by its id and the plugin
    connection's host, never by a link the binding carries.
  - Success means the session acknowledged the decision, and the report
    includes its disposition (accepted, declined, noted, conflicts).
  - Only an acknowledgment from the investigation author's own principal
    naming the session counts: never a grantee's, never one with
    `granted_by`. An agent holding the author's key could still post one
    in the session's name, so an acknowledgment reports what the session
    said, not proof that it acted.
  - Launching a process is never reported as success.
  - **Server-side reports (conductor#2110).** Each lifecycle state an
    entry reaches (NOTIFIED, CLAIMED, RESUMING, TIMED_OUT, ACKNOWLEDGED,
    FAILED; never ACK_RELAYED) is posted once as a `session.wake` event
    through `append-investigation-event` with the plugin connection:
    `session_id` = the woken session (never `to_session_id`), payload
    `{decision_seq, state, runtime, reason?}`, idempotency key
    `wake:<sha256(investigation:sid:seq)[:40]>:<state>`. Only when the
    binding advertises `session_wake: {enabled: true}`; best effort, after
    the pass, under the session lock, with a 10 s timeout; it never changes
    the lifecycle. `reason` is a fixed vocabulary (`WAKE_REASONS`, mapped
    from the waker's own notes, never the notes themselves): every org
    member and read grantee can read it, so never decision text, an ack
    note, a path or a key. Responses: `idempotency_conflict` counts as
    reported; `unknown_event_type` (an older server), 404 without
    `wake_decision_not_found`, 403 and `event_limit_reached` stop reports
    for that investigation (kept in the ledger); 404
    `wake_decision_not_found`, 422 and 400 `invalid_event` end that entry's
    reports; anything else is retried on later passes, 3 times. Only
    entries created by this version are reported. Readers never deliver
    or render it: class `wake` is not a read class (the cursor moves past
    it).

### 5.5 Wake authorization is not action authorization

A wake authorizes processing the decision, inside the session's own
sandbox:
- workspace-write: no network, nothing written outside the workspace (the
  waker refuses sessions whose recorded writable roots reach outside it);
- approval policy never: anything that needs approval fails rather than
  escalating.
- **MCP is the exception the sandbox doesn't cover.** Codex runs MCP servers
  outside its sandbox, so an approval-free agent could otherwise act through
  any configured server, Cardinal's own write tools included (Slack,
  GitHub, SQL, Kubernetes, ServiceNow).
  - The woken run therefore keeps only `storyboard__read_investigation_events`
    on the Cardinal server, pre-approved. The acknowledgment is relayed by
    the waker after the run (§5.3).
  - Every other server and every non-Cardinal plugin is disabled on the
    command line.
  - Built-in tools that act outside the sandbox are off too: app
    connectors, web search, image generation, the browser and computer-use
    REPLs, and plugin installs.
  - The lockdown is validated with `features list` (every feature off that
    must be, nothing on outside the tested set) and `mcp list` (only
    Cardinal enabled) before every launch (fail closed), and the rollout is
    checked afterwards for any call outside the allow-list.
  - **Hooks are the other exception (review 4 N1).** Codex runs them
    outside its sandbox in the workspace, so a woken run has none
    (`--disable hooks`). Nothing the plugin's hooks do happens during it:
    no evidence capture, telemetry, owner-input capture, poller or Stop
    reminder. The decision reaches the agent through the read tool the
    wake prompt names.
  - What the woken agent has (H1): the shell and `apply_patch` inside its
    sandbox, goals, MCP resource reads, `view_image`, the Cardinal read, and
    sub-agents that inherit the same lockdown.
  - Nothing it does reaches Cardinal except through the waker's relay of
    its one acknowledgment.

A woken agent that needs a consequential action does the rest of the work,
then stops and says so in its acknowledgment's note. That action needs a
later decision or an interactive turn.

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
| C-4 | Hooks run on a headless resume (the delivery path exists) | `codex exec resume` with Cardinal hooks on | SessionStart bound the session (`source auto`, `is_author true`, `owner_decision` enabled in prod). **Superseded for Codex by review 4 N1:** a woken run has `--disable hooks`; delivery is the prompt plus `storyboard__read_investigation_events` (C-17) |
| C-5 | Duplicate decisions can't start concurrent executions | Unit and multi-process tests in the C PR: two wakers racing the same seq; two decisions for one session; spawn error vs. child started | See the PR's test output |
| C-6 | Claude enforces a wake sandbox | Not attempted. User settings apply to `claude -p --resume`, and bypass or allow lists can't be excluded without dropping the plugin | **Not demonstrated → Claude notify-only** |
| C-7 | Gemini CLI enforces a wake sandbox that leaves Cardinal's state intact (gemini-cli 0.63.0) | Gemini's `-s` (macOS seatbelt) wraps the whole CLI process, hooks included. Its bundled profiles (`sandbox-macos-{permissive,restrictive,strict}-open.sb`, `TARGET_DIR` = workspace) were run directly: `touch ~/.cardinal/investigations/<probe>`; a write in the workspace; `curl https://example.com`. Inside the sandbox it also can't read its API key from the macOS keychain (exit 41) | Outside write "Operation not permitted" in all three (the hooks couldn't keep state, or `~/.cardinal` would have to be writable by the agent); inside write ok; `curl` 200 in all three. **The open profiles fail.** The launch is redesigned around the proxied profile and relocated hook state (§5.3 Gemini) |
| C-8 | Codex project config can't run unsandboxed code on a wake | A `.codex/config.toml` in the session's cwd declaring `[mcp_servers.x] command="/bin/sh"` that touches `~/probe`, under `codex exec -s workspace-write resume <sid>`. Inside workspace-write: `mkdir .codex`, `git config …` (`.git/config`) and `mkdir .agents` | The project MCP server ran **outside** the sandbox (`~/probe` created). A project `.codex/hooks.json` SessionStart hook did not run. The agent can't create any of these ("Operation not permitted"), but a cloned repo or another tool can. **So the waker refuses any session whose repo carries Codex project config (§5.3)** |
| C-9 | Gemini's proxied profiles lock the network to an allow-list proxy | Gemini's own `sandbox-macos-{permissive,restrictive}-proxied.sb` run directly, with a CONNECT allow-list proxy listening on `[::]:8877` | Direct internet blocked; a host the proxy denies blocked; `generativelanguage.googleapis.com` through the proxy reachable; other localhost ports blocked. **Proven** (the network lock of §5.3 Gemini) |
| C-10 | `gemini --resume <UUID> -s -p …` resumes the session headless (else the index fallback in `resolve_gemini_resume_ident`) | Owner, live | **Pending live proof** |
| C-11 | Inside the proxied sandbox, the hooks write the relocated state, and the woken session receives the decision and acknowledges it through the proxy | Owner, live: a woken run with `GEMINI_LAUNCH_PROVEN` flipped locally | **Pending live proof** |
| C-12 | The runtime check sees `sandbox-exec -f …restrictive-proxied….sb` in the child's tree, and the agent can't write `~/.cardinal` or reach a non-allow-listed host | Owner, live | **Pending live proof** |
| C-13 | Gemini's outer, unsandboxed process runs no project-defined command (MCP server, hook, tool discovery) before it enters the sandbox | Owner, live (until then the project guard refuses those settings) | **Pending live proof** |
| C-14 | Duplicate decisions and a second Gemini session can't start concurrent Gemini executions (port 8877 and the global lock) | Unit tests in the C PR; owner, live | Unit tests pass; **live pending** |
| C-15 / H1 | A woken Codex run can use only its sandbox and the Cardinal server's read/post of investigation events | Owner, live (codex-cli 0.162.1; a throwaway workspace-write session; real config with the cardinal, cardinal-behavior, computer-use and node_repl servers and 13 plugins). (1) `enabled_tools` plus per-server and per-plugin `enabled=false`. (2) Adding `tools.<t>.approval_mode="approve"`. (3) Adding `--disable apps plugins remote_plugin image_generation computer_use browser_use browser_use_external in_app_browser multi_agent tool_suggest skill_mcp_dependency_install` and `web_search="disabled"`. (4) `--disable code_mode_host`. (5) A spawned sub-agent | (1) The allow-list works (slack gone) **but** the two Cardinal tools were refused ("requires approval, but approval policy is never"), and about 90 non-config tools stayed (app connectors, web, image gen, cua REPL, plugin install). (2) The read completed. (3) Tools are exactly `exec`, `wait`, `request_user_input_async`, `clock.sleep` and `collaboration.*`; inside exec: `apply_patch`, `exec_command`, `write_stdin`, goals, MCP resource reads, `view_image`, `clock__curr_time` and the two Cardinal tools. `ls` ran, and the Cardinal hooks still fired. (4) Every tool fails, so it is never disabled. (5) The sub-agent inherits the lockdown (`touch ~/probe` not permitted, no network, same tools). **Proven** → the argv in §5.3 |
| C-16 | The waker's own `sandbox-exec -f <copy of restrictive-proxied>` around `gemini` (no `-s`, `SANDBOX` set) runs Gemini, delivers and acknowledges through the proxy, and Gemini starts no second sandbox | Locally: the shipped profile parses with the wake parameters (`/usr/bin/true` under it), and a nested `sandbox-exec` fails. Owner, live: a woken run | Profile and nesting: **proven locally**. End to end: **live pending** |
| C-17 / N1-live | With `--disable hooks`, a woken Codex run still gets the decision and acknowledges it | Owner, live (codex-cli 0.162.1, 8654c45's exact flags incl. `--disable hooks` and the four sandbox pins, `env -i` allow-list, private 0700 `TMPDIR`, a throwaway workspace-write session) | Hooks off (the Cardinal evidence spool unchanged across the run); `storyboard__read_investigation_events` completes with `approval_mode=approve`; `touch /tmp/x` "Operation not permitted"; `touch "$TMPDIR/x"` ok; curl: no network; rollout `sandbox_policy` = `{type: workspace-write, network_access: false, writable_roots: None, exclude_slash_tmp: true, exclude_tmpdir_env_var: false}`. **Proven.** **But acknowledging failed:** the gateway's post refuses acknowledgments (schema: no `ack_of` / `disposition` / `note`), the CLI ack needs network → the acknowledgment relay (§5.3). The read returned 0 events (that investigation had none); an `owner.decided` in the read is for the owner's E2E. Relay end to end: **live pending** |
| C-18 | The pre-launch probes see what a launch would load | Locally (0.162.1, the owner's real config, no session): `<codex> <flags> features list` and `<codex> <flags> mcp list --json` | features: rc 0, every disabled feature off, 45 features on (the tested set). mcp: rc 0, a JSON list of `{name, enabled, disabled_reason, transport, auth_status, startup_timeout_sec, tool_timeout_sec}`; only `cardinal` enabled (plugin servers vanish with `--disable plugins`). **Proven locally** |
| C-21 | The full lockdown leaves a working `codex exec resume` and nothing behind | Owner, live (codex-cli 0.162.1, the full cf17aba argv incl. `--disable daemon_auto_start` and `-c notify=[]`) | The agent ran `echo ALIVE`, exit 0; 5 s after exit no new codex-named process existed and no process carried the wake's TMPDIR. **Proven** |
| C-22 | A woken run's process is told from any other by the sandbox's TMPDIR-vs-canary test (review 6 / review 7) | Locally (this Mac, both Pythons): under `sandbox-exec`, a Codex-like `/bin/sleep` whose only writable root is DIR_A (this wake's TMPDIR), started `env -u TMPDIR`, new session, `cd /`; an Apple-like `/bin/sleep` whose profile allows writes under the whole parent tree (DIR_A **and** the canary); and a plain `/bin/sleep`. `sandbox_check(pid, NULL, 0)` and `sandbox_check(pid, "file-write-create", SANDBOX_FILTER_PATH\|NO_REPORT, <dir>)` via the arm64-padded ctypes prototype; then `sandboxed_procs(since, [DIR_A], canary=parent)` | **a/b matrix.** NULL: 1 Codex-like / Apple-like, 0 plain. Path (existing dirs): Codex-like → DIR_A **0**, sibling **1**, canary **1**; plain → every dir **0** (so the NULL check must gate it); Apple-like → DIR_A **0** AND canary **0**. A **missing** path answers **1** for everyone. `sandboxed_procs(since,[DIR_A],parent)` is **exactly `[codex-like pid]`**: the Apple-like one is filtered by the canary, the plain one by the NULL check, a sibling wake by the grant. **Proven locally** (sandbox-exec profiles; a process under Codex's **real** seatbelt is C-23) |
| C-25 / F1 | Unrelated sandboxed Apple agents are not swept as survivors | Reviewer, live read-only: `sandboxed_procs(0.0, [fresh dir under ~/.x/.cardinal/wake/tmp/...])` **before** the canary filter returned 8 of their own processes (AMPDeviceDiscoveryAgent, NotificationCenter, icdd, voicebankingd, TrustedPeersHelper, neagent, com.apple.Virtualization.VirtualMachine, secinitd); each is also allowed on a sibling canary | Without the canary the sweep would SIGTERM/SIGKILL whichever started after the child; launchd respawns them, so every wake would hit `WAKE_TIMEOUT`, end FAILED and disable launch, and the kept TMPDIR would keep the session live for good. The canary (deny on `~/.cardinal/wake/tmp`) removes **all 8** (ScanUnit covers it; the owner's E2E must show the sweep targets exactly the run's processes) |
| C-23 | The TMPDIR-vs-canary probe identifies **exactly** the processes of a run under Codex's **real** seatbelt (no false positive, no miss) | Owner, live: a harmless command under `codex`'s own seatbelt with a workspace-write policy whose TMPDIR is a wake-style dir, then `sandboxed_procs` against that TMPDIR with the `~/.cardinal/wake/tmp` canary — the result must be exactly that run's processes and nothing else on the machine | **Pending** (could not be run in this session). Blocks `CODEX_LAUNCH_PROVEN` |
| C-24 | The same identifies an env-cleared, setsid, re-exec survivor of a real woken run, and only it | Owner, live: a woken Codex run that leaves `env -u TMPDIR setsid python -c 'chdir("/"); sleep'`; the sweep stops exactly that process (nothing else), and the session stays live until it ends | **Pending** (could not be run in this session). Blocks `CODEX_LAUNCH_PROVEN` |
| C-20 | Codex `notify` cannot run outside the sandbox on a woken turn | Owner, live (codex-cli 0.162.1, the full lockdown argv incl. `--disable hooks`, `env -i` allow-list, private TMPDIR): `-c notify=["/bin/sh","-c","touch $HOME/probe"]`, then the same with `-c notify=[]` after it | Without the override the probe file **was created in $HOME**: notify runs outside the sandbox even with hooks off. With `-c notify=[]` after it: **absent**. **Proven** → `notify=[]` is the last `-c` of every launch (golden test); the owner's own notify is not refused |
| C-19 | The sandbox pins hold | Locally: `-c sandbox_workspace_write.{network_access=false, writable_roots=[], exclude_slash_tmp=true, exclude_tmpdir_env_var=false}` through `features list`. Owner, live: a woken turn's `sandbox_policy` | All accepted (rc 0), **but so is a bogus key**; a wrong type is rc 1. So existence is proven only by the rollout: a woken `workspace-write` turn must record `exclude_slash_tmp: true` and no `writable_roots` (enforced post-run). End to end: **live pending** |

Still owed: an end-to-end manual run, in which the owner posts an addressed
decision from the web for a stopped, sandboxed Codex CLI session and watches
it resume, acknowledge, and stay in workspace-write. An agent can't perform
this step, by design.

Observed: the owner's recent Codex desktop session ran `danger-full-access`,
so it would be notify-only on both counts (desktop app, unsandboxed). Once
`CODEX_LAUNCH_PROVEN` is on, launch applies to Codex CLI sessions whose last
turn ran `-s workspace-write` (a `read-only` last turn is notify-only, since
the survivor check needs a writable TMPDIR to identify the run's processes).

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
    `MACHINE_PROMPTS` for every runtime.
  - Review 3 regressions (`core/tests/test_session_wake_security.py`): each
    finding's case, including the reviewer's repro script. Each case failed
    on 9675269 except two positive controls.
  - Review 4 regressions (`core/tests/test_session_wake_review2.py`): N1-N5,
    L-a, L-b and the Gemini profile, the reviewer's five repro cases with
    their assertions turned around. 29 of 33 failed on 3223d8d; the other
    four are positive controls (a native codex accepted, a non-`@openai`
    package refused, the plist's PATH-only env, an off unknown feature).
  - The acknowledgment relay (`core/tests/test_session_wake_relay.py`): a
    valid answer relayed once and only after the exit (also after
    TIMED_OUT), the wrong seq, a symlink, a FIFO, an oversize file, bad
    JSON, a missing file, a strict schema (conflicts, extra keys, a text or
    bool seq, a long or control-character note), a failed post retried and
    posted once, a relayed acknowledgment not yet visible, no relay after a
    failed verification, and the post itself (`ack_payload`,
    `ack:<sid>:<seq>`, an idempotent repost).
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
2. Claude unattended wake: is there a launch mode that enforces a sandbox
   while keeping the plugin loaded? Revisit if Claude Code adds one.

## 9. Not doing

- Waking on anything but a verified owner decision addressed to the session.
- Agent-triggered or chained wakes.
- Waking the Codex desktop app or any session that ever ran unsandboxed.
- Claude or Cursor unattended launch (notify-only); Gemini CLI launch until its live gates pass.
- Launching a Codex session whose repository carries Codex project config,
  whose cwd is not strictly inside `$HOME` (or is in or above a protected
  directory), whose recorded sandbox could write outside its cwd, or whose
  Codex config widens workspace-write.
- Giving a woken run any MCP server or tool beyond the Cardinal
  investigation read, or any hook (Cardinal's included).
- Relaying anything from a woken run but its one acknowledgment of the
  decision it was woken for.
- Launching a codex that is a script, a version-manager shim or anything
  but the native binary that runs, or a Codex with a default-on feature
  outside the tested set.
- Passing a woken run any environment variable outside the allow-list.
- Running the daemon or a woken run with an executable, an interpreter or a
  PATH entry an agent could write.
- Waking on a decision older than 7 days, or twice on any decision.
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
- **Review 3 (independent security review of PR #209 at 9675269: BLOCK).**
  - B1: the daemon's interpreter and PATH came from the shell → a vetted
    interpreter outside `$HOME`, workspaces, approved roots and temp dirs,
    run with `-I`; launch binaries (and node) vetted the same way;
    a system-only PATH; fingerprints re-checked at every launch.
  - B2: `writable_roots` and `cwd == $HOME` were ignored → cwd strictly
    inside `$HOME` and outside protected dirs; every recorded write path
    inside cwd (eligibility and post-run); Codex config that widens
    workspace-write refuses.
  - B3: a pruned entry let an old decision run again → a never-pruned
    high-water mark per investigation and session; decisions older than
    7 days never launch.
  - H1: MCP servers run outside the sandbox → the woken run keeps only the
    Cardinal investigation read/post; everything else disabled with `-c`;
    a post-run rollout check.
  - M1-M3: realpath'd cwd; ledger keyed by investigation:seq; relocated
    bindings never shadow real ones and notification text never uses a
    binding's link.
  - G1-G3: the Gemini machine prompt; the waker's own `sandbox-exec` around
    Gemini (prevention) with the post-start check kept; an exclusively
    owned proxy port.
  - L1-L5: TIMED_OUT stays verifiable; crash-after-spawn recovery; the
    acknowledgment's disposition and principal; log rotation; the TUI
    liveness gap documented.
- **Review 4 (second independent security review of PR #209 at 3223d8d:
  BLOCK on gate 2).**
  - N1: hooks run outside the sandbox with the workspace as cwd → the woken
    run has `--disable hooks` (probed off); the prompt names the
    investigation and the read tool. Trade-off: no hook-driven capture or
    reminders during a woken run.
  - N2: the workspace could choose the codex binary (shims, scripts) → the
    native Mach-O only (npm's `codex.js` resolved to its vendor binary and
    launched directly), shim dirs refused, the executed binary fingerprinted.
  - N3: the child inherited the waker's environment → an allow-list, for the
    child and the probes; the plist sets only PATH.
  - N4: drift → a default-on feature outside the tested set refuses; an
    `mcp list --json` probe before every launch must show only Cardinal.
  - N5: the fingerprint survived an in-place rewrite → ctime and SHA-256.
  - L-a: quoted keys slipped the config scan → detected, and network and
    writable roots pinned on the command line.
  - L-b: the shared `/tmp` → `exclude_slash_tmp=true`, a private 0700
    `TMPDIR` per wake; the pins are read back from the rollout because
    Codex accepts unknown `-c` keys.
  - Gemini (off): the profile copy no longer writes `~/.npm` or `~/.cache`,
    only `~/.cache/cardinal/sandboxed`.
  - Live N1 follow-up (on 8654c45): hooks off, the read and the sandbox
    pins proven live, but no acknowledgment was possible from inside the
    run → the waker relays the one the agent writes to its private
    `TMPDIR` after the run; the MCP post tool is off.
- **Review 5 (third independent security review of PR #209 at 72f7f4f:
  APPROVE-WITH-CHANGES; Codex launch blocked on 1, 2, 4).**
  - 1: a config `notify` program runs outside the sandbox (live C-20) →
    `-c notify=[]`, always, last.
  - 2: eligibility was checked before the session lock, and the probes ran
    from `/` → everything re-checked under the lock right before the
    spawn; both probes in the launch's cwd; project config re-checked
    after the run.
  - 3: protected paths were not resolved through symlinks → each protected
    directory and shell / git dotfile is checked where it really is.
  - 4: processes could outlive the woken codex → the run's processes are
    found (TMPDIR marker, process group), stopped, and the entry stays in
    flight until none is left; `daemon_auto_start` disabled and probed.
  - 5: the acknowledgment note could carry secrets out → the prompt reads
    owner decisions only; only the disposition is relayed unless
    `enable --relay-notes`, and then the note is scrubbed.
  - 6: MCP servers or plugins declared with quoted or nested keys slipped
    the scan → any key-position mention outside the strict headers refuses.
  - 7: `-I` still runs site / `.pth` → `-I -S`; never the xcrun shim.
  - 8: a turn in another cwd was not seen without `permission_profile` →
    every turn's cwd must be the session's (realpath).
  - The probe cache is gone: both probes run before every launch.
- **Wake intent and reports (conductor#2110).** The waker launches only
  for `payload.wake is True` (a release blocker found by that PR's review:
  before it, any verified addressed decision launched); lifecycle states
  are reported as `session.wake` events with fixed-vocabulary reasons.
- **Survivor gap closed (owner: gate 3 is strict).** Survivors are
  identified through the sandbox itself (`sandbox_check`: allowed on the
  wake's TMPDIR, denied on the parent canary), and the same test is a
  liveness signal before every claim (C-21, C-22, C-25).
- **Review 6 (focused review of PR #209 delta 72f7f4f..bd493df: BLOCK on
  gate 3; gates 1 and 2 pass).** The path-based survivor scan missed
  Python / jsc re-execs and could kill unrelated sandboxed orphans.
  - 1/2/3: identify a wake's processes only by the sandbox granting the
    wake's TMPDIR (`sandbox_check` path form, arm64-padded ctypes); the
    cwd / executable / open-file paths, the process group, the environment
    marker, the orphan heuristic and the app / EPERM exemptions are gone.
    The sweep kills exactly those; pre-claim liveness is the same test over
    the session's in-flight and still-present TMPDIRs.
  - 2 (read-only): a read-only sandbox grants no writes, so its run cannot
    be told apart → read-only sessions are notify-only.
  - 4: the EPERM/`comm` exemption and all fd inspection are gone (not
    needed without path scanning).
  - 5: the fail-closed sweep is bounded — a run whose survivors cannot be
    cleared or confirmed within `WAKE_TIMEOUT` is FAILED, its TMPDIR kept
    and recorded, Codex launch disabled.
  - 6: the `mcp list --json` probe, not the config text scan, is the
    enforcement for an MCP declaration form the scan cannot read.
  - 7: owner-decided-only reads and the `--relay-notes` scrub are prompt
    guidance / best-effort, documented as such; sourced (non-symlinked)
    dotfiles are a documented limitation.
  - A transient probe failure no longer consumes the decision (bounded
    retry).
  - **(c)/(d) could not be verified live in this session** (a process
    under Codex's real seatbelt; an env-cleared setsid survivor of a real
    run). Per the owner's standing rule, **Codex launch ships OFF**
    (`CODEX_LAUNCH_PROVEN = False`), gated exactly like Gemini; an owner
    flips the constant in a reviewed change once C-23 and C-24 pass. The
    a/b matrix is proven locally (C-22).
- **Review 7 (review of e409fa8: BLOCK).**
  - F1: the TMPDIR-only survivor probe also matched 8 of the owner's own
    sandboxed Apple agents (their profiles allow writes broadly under
    `$HOME`, so they are allowed on the TMPDIR too). Unfixed, every wake
    would sweep whichever started after the child, run to `WAKE_TIMEOUT`,
    fail and disable launch, and the kept TMPDIR would keep the session
    live for good. Fix: a process counts only if its sandbox is allowed on
    the TMPDIR **and denied on the canary** (the parent `~/.cardinal/wake/
    tmp`, which no wake sandbox grants); a missing canary fails closed. The
    canary removes all 8. Applied in wake_owned, sandboxed_procs,
    wake_processes and end_wake_processes (C-25).
  - F2: a later decision without wake could raise the high-water mark past
    an earlier wake decision that recorded no entry (notified, old, busy,
    retry…), dropping it silently. Fix: handle pending decisions in
    ascending seq and stop at the first that launches or cannot be claimed;
    stale `probe_retries` keys are cleared and normalized in read_ledger.
  - Nits: dead `return` after `return left` removed; the "--launch allows
    Codex CLI sessions to resume" help text corrected; the sweep-timeout
    test asserts launch is disabled.

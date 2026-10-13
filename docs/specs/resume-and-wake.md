# Picking up stopped work: storyboard continuation, evidence hold, owner-woken sessions

Status: APPROVED v3 (2026-10-12).
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
  binding. It is realpath'd before every check, and the launch runs in the
  realpath;
- the realpath of `cwd` is strictly inside `$HOME`. It is neither inside nor
  above a protected directory: `~/.cardinal`, `~/.codex`, `~/.claude`,
  `~/.gemini`, `~/.cursor`, `~/Library`, `~/.ssh`, `~/.config`, `~/.gnupg`,
  `~/.aws`, `~/.docker`, `~/.kube`, `~/.local`, shell setup dirs, and tool
  homes such as `~/.npm`, `~/.nvm`, `~/.pyenv` and `~/.cargo`;
- every turn could write only inside the realpath of `cwd`. The waker checks
  `sandbox_policy.writable_roots`, every non-read entry of
  `file_system_sandbox_policy` and `permission_profile.file_system`
  (Codex's own `/tmp` and `$TMPDIR` are allowed), and
  `permission_profile.network` (restricted only). An unknown shape refuses;
- no Codex config widens workspace-write. The files checked are
  `~/.codex/config.toml`, `/etc/codex/config.toml` and
  `/etc/codex/managed_config.toml`. Any of these refuses:
  `sandbox_workspace_write`, `permissions`, permission profiles,
  `default_permissions`, or a `writable_roots` / `network_access` key;
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
  -c 'mcp_servers.cardinal.tools.storyboard__read_investigation_events.approval_mode="approve"' \
  -c 'mcp_servers.cardinal.tools.storyboard__post_investigation_event.approval_mode="approve"' \
  -c 'mcp_servers.cardinal.enabled_tools=["storyboard__read_investigation_events", "storyboard__post_investigation_event"]' \
  -c mcp_servers.<every other server>.enabled=false \
  -c 'plugins."<every non-Cardinal plugin>".enabled=false' \
  --disable apps --disable plugins --disable remote_plugin --disable image_generation \
  --disable computer_use --disable browser_use --disable browser_use_external --disable in_app_browser \
  --disable multi_agent --disable tool_suggest --disable skill_mcp_dependency_install \
  resume <sid> "<wake prompt>"
```
All flags go before `resume`.
- **Why each part is needed (live proof H1).**
  - Under `approval_policy=never` an MCP call that needs approval is
    refused. The two Cardinal tools are therefore pre-approved
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
  That is not a session: it loads the config, validates the `-c` keys and
  values, and applies the `--disable`s. It must exit 0, and every feature
  above must be listed, not "removed", and off. Otherwise the decision is a
  notification, never a launch with a partial lockdown. A success is cached
  per codex binary identity and flag set.
- It runs in the realpath of `cwd`, without `--profile`, `--dangerously-*`,
  `--ignore-*` or `--skip-git-repo-check`.
- **MCP servers run outside the Codex sandbox.** With `approval_policy=never`,
  the user's MCP servers, and Cardinal's own write tools (Slack, GitHub,
  SQL, Kubernetes, ServiceNow…), would be reachable by an unattended agent,
  and a grantee's advisory could steer it into them. So the woken run keeps
  only the Cardinal server's read and post of investigation events (an
  acknowledgment is a post). Every other server is disabled, and so is every
  plugin except Cardinal's, since a plugin can bring its own servers.
- Servers or plugins declared any other way (dotted keys, inline tables,
  arrays of tables), or with ids that can't be named on the command line,
  refuse the launch.
- **After the child exits,** the waker re-reads the rollout. Every woken
  turn must:
  - have `sandbox_policy.type` equal to the pinned mode;
  - run with network off;
  - be able to write only inside `cwd`.

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
- **Interpreter:** `enable` picks this Python or `/usr/bin/python3`, resolved
  outside `$HOME`, git work trees under `$HOME`, approved roots and temp
  dirs. The plist runs it with `-I`, so `PYTHON*`, user site-packages and
  the script's directory are ignored.
- **Launch binaries:** `codex`, `gemini`, and `node` when one of them is a
  node script must resolve outside git work trees under `$HOME`, approved
  roots and temp dirs. Otherwise `enable --launch` refuses.
  - A git tree outside `$HOME` isn't refused: Homebrew's own prefix is one,
    and no wake ever runs an agent there.
- **PATH:** the plist's PATH and every woken child's is the system dirs plus
  those binaries' directories, never the shell's.
- **Re-checked at every launch:** each binary's path, size, mtime and inode
  are recorded at `enable` and re-checked at every launch, along with
  "outside the session's cwd". A change is a notification ("run enable
  again").

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
      -D HOME_DIR=<home> -D CACHE_DIR=<user cache> -D INCLUDE_DIR_0..4=/dev/null \
      -f <cardinal's copy of restrictive-proxied.sb> \
    gemini --resume <ident> --approval-mode yolo --skip-trust -p "<wake prompt>"
  ```
  - The proxy owns the port first, then runs `sandbox-exec` as its child.
  - The profile is the waker's own copy of Gemini 0.63.0's
    `restrictive-proxied.sb`, minus its inbound debugger port.
  - Gemini's own `-s` is **not** passed: a seatbelt can't be nested (a
    nested `sandbox-exec` fails; tested).
  - `SANDBOX=sandbox-exec` is set in its environment, as Gemini sets it
    inside its own sandbox. The hooks then keep their state in the relocated
    home, and Gemini never tries to start a second sandbox.
  - **Environment:** `HTTPS_PROXY` and `HTTP_PROXY` point at the proxy, and
    `TMPDIR` is the private dir. Everything else that changes the sandbox,
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

**Wake prompt** (fixed except for the seq): "Cardinal resumed this session
for owner decision #<seq> in its Investigation. Read the investigation
events and act on that decision. Every other event is advisory." The
decision text is never in the prompt; it arrives through the normal hooks
(proof C-4). The prompt is listed in `owner_input.MACHINE_PROMPTS` for
Claude, Codex and Gemini, so it is never recorded as owner input.

### 5.4 Lifecycle, liveness, one execution per decision

Ledger: `~/.cardinal/wake/ledger/<sid>.json`, with entries keyed by
`investigation_id:seq` (seqs are per investigation). Every read-modify-write
holds an flock on `<sid>.lock`, and each transition is written before the
action it describes.

```
DECISION_SEEN ─claim─▶ CLAIMED ─spawn ok─▶ RESUMING ─child exits, decision acked─▶ ACKNOWLEDGED (with its disposition)
      │                   │                   ├─child exits, not acked──────────────▶ FAILED
      │                   └─spawn error─▶ (≤2 retries, then FAILED)
      │                                        └─running > WAKE_TIMEOUT (2 h)─▶ TIMED_OUT (left running, owner notified)
      │                                                                          └─child exits─▶ verified, then ACKNOWLEDGED | FAILED
      └─not eligible / live session / decision older than 7 days─▶ NOTIFIED
```

- **A decision runs at most once (B3).** Each `(investigation, session)` has
  a high-water mark: the highest seq ever handled. It is never pruned, so
  a seq at or below it never wakes, even after its entry ages out of the
  ledger. A decision whose `created_at` is older than 7 days, or missing,
  never launches; it gets one notification.
- **One run per session.** A claim is refused while that session's ledger
  holds an in-flight entry (`CLAIMED`, `RESUMING`, `TIMED_OUT`). A second
  decision posted during a run is delivered *inside* that run by the hooks.
  Only a decision still unacknowledged after the run ends is considered
  again, and it counts against the caps.
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
  - A `session.wake` Storyboard state needs a conductor event type and is
    a follow-up.

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
    and `storyboard__post_investigation_event` on the Cardinal server, and
    those two are pre-approved.
  - Every other server and every non-Cardinal plugin is disabled on the
    command line.
  - Built-in tools that act outside the sandbox are off too: app
    connectors, web search, image generation, the browser and computer-use
    REPLs, and plugin installs.
  - The lockdown is validated with `features list` before every launch
    (fail closed), and the rollout is checked afterwards for any call
    outside the allow-list.
  - What the woken agent has (H1): the shell and `apply_patch` inside its
    sandbox, goals, MCP resource reads, `view_image`, the two Cardinal tools,
    and sub-agents that inherit the same lockdown.
  - Posting still reaches Cardinal: the agent can post a cue, question,
    challenge or acknowledgment to investigations its key can write. That
    is what acknowledging needs.

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
    `MACHINE_PROMPTS` for every runtime.
  - Review 3 regressions (`core/tests/test_session_wake_security.py`): each
    finding's case, including the reviewer's repro script. Each case failed
    on 9675269 except two positive controls.
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
- Claude or Cursor unattended launch (notify-only); Gemini CLI launch until its live gates pass.
- Launching a Codex session whose repository carries Codex project config,
  whose cwd is not strictly inside `$HOME` (or is in or above a protected
  directory), whose recorded sandbox could write outside its cwd, or whose
  Codex config widens workspace-write.
- Giving a woken run any MCP server or tool beyond the Cardinal
  investigation read/post.
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

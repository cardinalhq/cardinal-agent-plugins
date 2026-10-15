# Relayed owner decisions: approve in the supervisor's terminal

Status: approved design, 2026-10-15. Plugin: cardinal-agent-plugins
`feat/relayed-owner-decisions`. Server: conductor `feat/relayed-owner-decisions`.

## Problem

A supervisor session (the supervise skill) can steer a worker only with advisory
input (cue, question, challenge) and directives (upheld rulings on its own
challenges). Scope, permission and policy decisions need owner authority, which
today comes only from the owner's words in the worker's own terminal or an
`owner.decided` posted from the owner's signed-in web session. In practice the
owner tells the supervisor the decision, the supervisor writes it up, and the
owner pastes it into the worker's terminal. The owner rejected web approval as
the fix ("navigating to the UI to approve is bad UX"): approval must happen in
the supervisor's terminal.

## Trust model (say it exactly; never claim more)

The owner approves by typing `approve` (or `decide: …`) as a prompt in the
supervisor session; the runtime's prompt hook relays it with the owner's API
key. The server can verify:

- the key belongs to a person (a `user:` principal), still a writing member of
  the org, who authors the worker's investigation;
- that person turned on "accept owner decisions I send from my supervisor
  sessions" from their signed-in web session (a per-user, per-org setting that
  only a web session can change; off by default);
- the claimed supervisor session belongs to an investigation the same person
  authors;
- the text it stores is the text whose hash the relay carries.

The server cannot tell who made the request: the agent holds the same key and
can always call the route directly, naming any supervisor session. The
plugin's guarantee is narrower and is all it claims: **it never relays a prompt
it cannot confirm a person typed in this terminal, and it refuses every prompt
a model created** (scheduled tasks, /loop iterations, peer messages, subagent
prompts, task notifications).

- **Claude Code** fires UserPromptSubmit for prompts no person typed, with no
  origin in the payload (verified in 2.1.300: a model-created one-shot
  scheduled task with prompt `approve` arrived exactly like a typed one; a
  cross-session message arrived wrapped in `<cross-session-message …>`, which
  never matches the forms). The session transcript records the origin on the
  user entry whose `promptId` is the hook's `prompt_id`: `origin.kind:
  "human"` (with `promptSource: "typed"`, `turnOrigin: "human"`) for what the
  person typed; scheduled and peer prompts carry `isMeta`, `promptSource:
  "system"` and another `turnOrigin`; tool results share the promptId but carry
  `toolUseResult`. That entry is written after the hook returns, so the hook
  defers: it records the prompt as pending and a detached verifier waits up to
  20 s for the entry and acts only when it is human-typed (below).
- **Codex, Gemini CLI, Cursor**: their prompt payloads carry no origin field
  (payload captures in this repo). Neither their documentation nor our captures
  show a model-schedulable prompt, so they relay synchronously, trusting that
  the prompt hook sees what the person typed. Known hook-made prompts: Gemini
  re-fires BeforeAgent with an AfterAgent block's reason as the prompt, and
  Cursor submits a `stop` hook's `followup_message` as the next user prompt
  (this plugin uses neither; another extension could). Unknown: whether a
  future scheduling feature of those runtimes would fire the prompt hook
  without saying so. Revisit when one ships.

The resulting authority is `owner_relayed`, attested by the owner's machine,
the same trust level as text pasted into the worker's terminal, and weaker than
`owner_decision` (web session). Workers treat it as the owner's own words; the
label says how it arrived. It never overrules a supervisor's challenge or
ruling and cannot be cited by `--conflicts-with` (server: 422
`owner_relay_answers_ruling` / `conflict_ref_not_owner`): only a web decision
does. A supervisor defers to a relayed decision only when its own session's
hook sent it (the reader marks it `relayed: by_you`); any other is unverified
and reported to the owner. The opt-in is the owner's explicit acceptance of
that level. Later hardening (out of scope): a local user-presence gesture
(Touch ID) the agent cannot perform.

Not used, on purpose: Claude's AskUserQuestion. Its input schema has an
`answers` field the model can fill, and we have not shown a hook can tell a
pre-filled answer from a real one. Revisit as an enhancement only after that is
verified.

## Flow

1. Owner tells the supervisor a decision for the worker (scope, permission,
   policy, stop rules...).
2. Supervisor drafts it and runs
   `cardinal-storyboard investigation propose-decision <worker_inv> --to-session <worker_sid> --text "<decision>" [--answers <seq>] [--ref <r>]... --session <supervisor_sid>`.
   The CLI stores a local proposal and prints its id (`dp_` + 16 hex) and the
   instruction to show the text verbatim and ask the owner to reply
   `approve dp_xxxxxxxxxxxxxxxx` (bare `approve` works for the one draft made
   since the owner's previous prompt) or `reject dp_xxxxxxxxxxxxxxxx`.
3. The owner types `approve`. The supervisor's prompt hook matches it, relays
   the proposal to `relay-owner-decision`, deletes the proposal, and tells the
   user and the model the outcome ("Relayed owner decision dp_… to session
   <8> as event #N", or why not, e.g. the settings link when the opt-in is off).
4. The worker receives an `owner.decided` (authority `owner_relayed`) at its
   next tool boundary through the existing owner-decision delivery, renders it
   as OWNER DECISION (relayed), and acknowledges it like any owner decision.

## Server (conductor, maestro)

### Per-user setting

- New table `maestro_investigation_user_settings (org_id, user_id,
  relay_owner_decisions boolean not null default false, updated_at,
  updated_by_auth_method)`, primary key `(org_id, user_id)`. Repository +
  migration (up/down).
- `GET /me/investigation-settings` (any authenticated user principal, including
  a user's API key) returns `{relay_owner_decisions, settings_url}`.
  `PUT /me/investigation-settings` requires `req.authMethod === "oidc_session"`
  and a `user` principal (403 `settings_require_web_session` otherwise).
- Web UI: a Settings page section "Investigations" with one toggle, "Accept
  owner decisions I approve in my supervisor sessions", and a short explanation
  of the trust level (approved on your machine; Cardinal checks the key and this
  setting, not a button press).

### Route `relay-owner-decision`

Under the same mcp-tools base as `record-owner-input`, on the plugin tool path
allowlist; `requirePermission("storyboards:write")`. Never a gateway tool.

Request (zod, strict):

```
{ investigation_id, to_session_id: sessionId,           // required
  relay_key: eventText(1,125),                           // idempotency, the proposal id
  payload: { text: 1..4000 chars, answers?: int, refs?: string[] (<=20, each <=300) },
  relay: { supervisor_session_id: sessionId, supervisor_investigation_id,
           proposal_sha256: hex64,                       // sha256(utf-8 payload.text)
           prompt_sha256: hex64,                         // sha256 of the scrubbed approving prompt
           approved_at: ISO-8601,
           source?: "proposal" | "verbatim" },            // default "proposal"
  client?: string }
```

No `wake`, `intent` or `audience` (a relay never wakes a session or requests a
story). Checks, in order (first failure answers):

1. auth is an API key (not `oidc_session`, not a grant token): else 403
   `owner_relay_requires_api_key`; the key resolves to a `user:` principal:
   else 403 `owner_relay_requires_user_key` (personless key).
2. investigation exists (404 `investigation_not_found`); caller is its author
   (`isInvestigationAuthor`): else 403 `owner_relay_requires_investigation_author`.
3. the caller's setting `relay_owner_decisions` is true: else 403
   `owner_relay_not_enabled` with `settings_url`.
4. `supervisor_investigation_id !== investigation_id` (422 `owner_relay_self`);
   the supervisor investigation exists, its author is the same principal
   (`samePrincipal`), and `supervisor_session_id` is an author-claimed session
   of it (`authorClaimedSession`): else 403 `owner_relay_supervisor_not_yours`.
5. `to_session_id` is an author-claimed session of the worker investigation:
   else 422 `owner_relay_target_not_author_session`.
6. `proposal_sha256 === sha256(payload.text)`: else 400 `owner_relay_text_mismatch`.
7. `answers`, when present, is an event of this investigation: else 404
   `owner_decision_answer_not_found`.
8. the owner-decision count limit applies as for `record-owner-decision`.

Writes `owner.decided` with authority `owner_relayed`, `to_session_id`,
payload `{text, answers?, refs?, relay: {supervisor_session_id,
supervisor_investigation_id, proposal_sha256, prompt_sha256, approved_at}}`,
producer `{principal: user, authMethod: "api_key", keyId, userId,
sessionId: supervisor_session_id, client, grantedBy: null,
isInvestigationAuthor: true}`. Idempotency key `or:` + sha256(relay_key)[:40];
the request hash covers `{investigation_id, to_session_id, payload, relay}`
minus `approved_at` and `prompt_sha256` (a retry of the same proposal is a
duplicate, 200).

### Registry and database

- `EVENT_AUTHORITIES` gains `owner_relayed`. `owner.decided` keeps its type
  and class (`owner`) so acks, `conflicts_with`, supervision status
  ("overruled"), pending-decision reminders and the web timeline keep working;
  the registry must allow both authorities for it (document the second writer
  and route).
- Migration: re-add `_authority_check` with `owner_relayed`;
  `_authority_class` lets `owner.decided` carry `owner_decision` or
  `owner_relayed`; `_owner_decision` becomes: authority `owner_decision` ⇒ the
  existing web-session constraints; authority `owner_relayed` ⇒
  `producer_auth_method = 'api_key'`, `principal_kind = 'user'`, `key_id` NOT
  NULL, `session_id` NOT NULL, `granted_by` NULL, `is_investigation_author`.
  Down migration restores the current checks (refusing if relayed rows exist,
  or deleting them: pick and document).
- Update the registry snapshot test, the Go contract test, reader int tests,
  and the type unions.

### Capability

`capabilitiesWire` gains `owner_relay: {enabled: true}` (server supports the
route; the per-user setting is reported by the route's 403 and by
`GET /me/investigation-settings`).

### Web

- Timeline / investigation page: an `owner.decided` with authority
  `owner_relayed` shows as an owner decision labelled "approved in supervisor
  session <8 chars>" (not "from the web").
- The settings toggle above.

## Plugin (cardinal-agent-plugins, core + all four adapters)

### Proposals (supervisor side)

- `cardinal-storyboard investigation propose-decision <worker_inv> --to-session
  <sid> --text T [--answers SEQ] [--ref R]... [--session <supervisor_sid>]`
  (session from the runtime env when omitted). Uses this machine's key, never a
  grant token (refuse when CARDINAL_INVESTIGATION_TOKEN is set or
  `--credential-file` is passed). Requires the supervisor session's binding
  with `is_author: true`, and the binding's capabilities to include
  `owner_relay` (else: "this Cardinal cannot relay owner decisions"). Refuses
  proposing to the supervisor's own investigation.
- Store: `~/.cardinal/investigations/sessions/<supervisor_sid>.proposals.json`
  (0600, flock like the binding): entries `{id, investigation_id,
  to_session_id, text, sha256, answers?, refs, created_at, expires_at}`;
  `id` = `dp_` + 16 random hex; expiry 30 min; at most 5 pending (oldest
  dropped). `investigation proposals [--session]` lists them;
  `investigation withdraw-proposal <id>` removes one.
- Output: the id, the text verbatim, and: "Show the owner this text verbatim and
  ask them to reply `approve <id>` or `reject <id>`. Never approve it yourself
  and never call the relay route: only the owner's own prompt approves."

### Approval (prompt hook, all four runtimes)

- In each runtime's prompt hook, before and independent of owner-input capture
  (owner input may be off; prod has it off): refuse subagent prompts (non-empty
  agent_id) and machine prompts (`normalize_prompt` must reduce it to typed
  text). Match the whole stripped prompt, case-insensitive:
  `^(approve|reject)(\s+dp_[0-9a-f]{16})?[.!]?$`. Bare form only for a draft
  made since the owner's previous prompt (see "Stale bare approve"); several
  such drafts: tell the user which ids are pending.
- approve: POST `relay-owner-decision` synchronously with a bounded wall clock
  (~3 s). Success (201 or 200 duplicate) removes the proposal. Transient
  failure keeps it and says "not relayed — retry with `approve <id>`". 403
  `owner_relay_not_enabled`: keeps it and shows the settings URL. Other
  refusals drop it with the server's reason (`ie.REFUSALS`-style plain text).
- reject: removes the proposal; nothing is sent.
- Either way the hook reports the outcome to the user (Claude: `systemMessage`;
  others: their user-visible channel where one exists) and to the model
  (additional context), e.g. `Cardinal: relayed owner decision dp_… to session
  1a2b3c4d as event #57 (owner_relayed).` The approving prompt still reaches
  the model.
- `prompt_sha256` = sha256 of `owner_input.scrubbed_of(prompt)`.

### Worker side

- `verified_relayed_owner_decision(e)`: type `owner.decided`, class `owner`,
  authority `owner_relayed`, producer `is_investigation_author` true, principal
  kind `user`, producer `session_id` present, no `granted_by`.
- Delivery: same path as owner decisions (capability `owner_decision` gate,
  pending-decision tracking, Stop reminders, ack removal).
- Rendering for an author session: `OWNER DECISION (relayed) — approved by
  <name> in their supervisor session <claimed sid>; Cardinal checked the key is
  <name>'s and that they turned on relayed decisions from the web. It attests
  their machine, not a button press.` Doctrine identical to a web owner
  decision (treat as your owner's own words; acknowledge). For a joined
  (non-author) session: advisory, as today.
- `--conflicts-with` accepts it (server already accepts any `owner.decided`).
- An older plugin renders it ADVISORY (authority is not `owner_decision`):
  safe failure, no change needed there.

### Skill

Supervise skill CORE: when the owner gives a decision for the worker, use
propose-decision, show the text verbatim, and ask for `approve <id>`. Never
hand the owner text to paste into the worker. Never post a cue/challenge that
claims owner approval. If relay is not enabled, tell the owner the one-time
setting link.

### Release

Minor bumps on all four adapters; `docs/parity.json` entry
`relayed_owner_decisions` (all four).

## Tests

Server: each refusal in order; idempotent retry; setting PUT refused for API
key / grant; DB check refuses an `owner_relayed` row with oidc auth or a
grantee and an `owner_decision` row with a session id; ack and
`conflicts_with` on a relayed decision; supervision status "overruled" by a
relayed decision; capability present.

Plugin: proposal store (perms, expiry, cap, lock); propose refusals (grant
token, no binding, not author, capability absent, own investigation); hook
matching (bare vs id, ambiguity, case, punctuation, subagent, machine prompt,
non-matching prompt untouched); relay success/duplicate/transient/not-enabled/
refusal paths; worker rendering for author vs joined session; old-authority
event unchanged; parity guard.

## Revision 2026-10-15: the verbatim form, targets, and UX rules (plugin)

Approved after the first implementation and an independent UX review. Where
this section and the text above differ, this section wins.

### `decide:` — the owner's own words, no draft

- The owner types `decide: <text>` (or `decide <name>: <text>`) as the whole
  prompt in the supervisor session (`^decide(\s+[a-z0-9-]{1,32})?\s*:(.*)$`,
  case-insensitive, matched on the normalized prompt). The prompt hook sends
  `<text>` unchanged (surrounding whitespace trimmed) with `relay.source:
  "verbatim"` and `relay_key` = `dv_` + 16 hex. No draft, no approve step.
  Same refusals (subagent, machine prompt), same bounded request and outcome
  reporting as `approve`.
- Not sent, with a line saying why: empty text; over 4000 characters; a NUL or
  lone surrogate; text ending with `?` ("that reads as a question");
  text the credential scrubber would change (see below).
- A `decide:` that failed transiently or because the setting is off keeps its
  relay key for 30 minutes: typing the same text to the same target again
  reuses it, so the server answers a duplicate rather than storing it twice.
- Approve sends `relay.source: "proposal"`. The worker renders verbatim as
  "OWNER DECISION (relayed) — <name>'s own words, typed in their supervisor
  session <sid>" and a proposal as "… approved by <name> in their supervisor
  session <sid> (drafted by the supervisor)"; any other value renders as a
  proposal. The trust line reads "It confirms their machine and key, not that
  they typed it." A relayed decision with `answers` adds "It answers #N."

### Targets

- `cardinal-storyboard investigation supervising <worker_inv> --to-session
  <worker_sid> [--name <[a-z0-9-]{1,32}>] [--remove] [--session <sid>]`
  records a worker target in the supervisor session's store
  (`<sid>.proposals.json`, 0600, flock), at most 5 (oldest dropped); a name
  names one target. No arguments lists them. Local only; refuses a grant token
  or `--credential-file`; same preconditions as `propose-decision`.
  `propose-decision` also registers its target (keeping an existing name).
- Bare `decide:` needs exactly one target; otherwise the outcome lists them
  ("codex (session 1a2b3c4d)"; an unnamed one "(unnamed: ask the supervisor to
  name it)"). `decide <word>:` naming no target, when no target has a name, is
  an ordinary prompt (no Cardinal output).
- Capabilities: the server may add `owner_relay.on_for_caller` (bool) and
  `owner_relay.settings_url` (an http(s) URL or a site path, made absolute with
  the connection's origin). The plugin keeps them; `supervising` and
  `propose-decision` print "Relayed decisions are off for your account: turn
  them on once at <url> (web only)." when `on_for_caller` is false. Missing:
  unknown, nothing said.

### Credentials

- If `owner_input.scrubbed_of(text)[0] != text`, nothing is sent:
  `decide:` answers "your decision looks like it contains a credential, so
  nothing was sent…"; `propose-decision --text` exits 2 with the same reason.

### Stale bare approve

- The store notes every owner prompt the hook sees in a session that keeps
  relay state (`prompt: {at, sha, prev}`; a Gemini re-fire of the same prompt
  keeps the previous time). A bare `approve` / `reject` applies only to a
  draft created after the owner's previous prompt (drafted in the turn just
  answered); otherwise "no draft is waiting for your approval (drafts expire
  after 30 minutes); nothing was sent." An explicit id is not limited.
- A session with no relay state stays silent on a bare `approve` (it is the
  owner talking to their agent); every `decide:` gets an outcome line.

### Outcome copy

- Owner-facing words: "draft", "sent", the worker's name, "owner decision #N".
  Success echoes a one-line, escaped ~160-character excerpt of what was sent
  and says "The worker sees it after its next tool call; if it is waiting for
  you, when it next runs (any prompt in its terminal). To change it, send
  another decide:; the worker sees both."
- Every refusal ends with the next step; unknown codes say "a reason this
  plugin version does not know" (the code goes to the debug log only).
- An id that is not waiting says whether it expired, was rejected (at HH:MM),
  was already sent (as #N) or was refused.
- The web toggle is labelled "Accept owner decisions I send from my supervisor
  sessions".
- Cursor has no user-visible channel on its prompt hook: the outcome is staged
  for the next tool call and listed by `investigation proposals`; the skill
  tells the agent to quote it verbatim.

## Revision 2026-10-15 (review): origin, uncertainty, and the server's new refusals

Where this section and the text above differ, this section wins.

### Claude: confirm the prompt's origin before acting

- UserPromptSubmit (`owner-relay.py`, `owner_relay.handle_prompt_deferred`)
  sends nothing. For a relay form it answers at once only what needs no
  origin (a text refusal; no worker; no draft; not connected) and otherwise
  records `{pid: prompt_id, at, state}` under `pending` in the session's store
  (`<sid>.proposals.json`, 0600, flock), starts a detached verifier (own
  session, isolated Python, the job on a pipe, as owner-input's flush), and
  tells the owner "Cardinal: confirming you typed this in this terminal, then
  sending draft dp_… to <name>." (the model: the outcome comes with the
  owner's next prompt).
- The verifier reads the transcript from the size it had at hook time (minus
  256 KiB of slack) for up to 20 s, for the user entry with that promptId that
  is not a tool result. It acts only when `origin.kind == "human"`, no machine
  marker is present (`isMeta`, `promptSource: "system"`, a `turnOrigin` other
  than `"human"`) and the entry's text is the prompt the hook saw. Any
  `promptSource` other than `"system"` is accepted with a human origin: we have
  only observed `"typed"`, and a pasted prompt should count if Claude marks it
  differently; `origin.kind` is the attribution. Not found in time,
  unreadable, another origin, an unknown shape: "Cardinal: not sent: Cardinal
  could not confirm you typed this prompt in this terminal." The prompt text
  is the owner's; no missing payload field (`prompt_id`, `transcript_path`) is
  guessed: then it is not sent.
- Outcomes are kept under `outcomes` and shown once per channel: the Stop hook
  (`owner-relay.py stop`, waiting at most 3 s while a prompt is pending) shows
  them to the owner (systemMessage); the next UserPromptSubmit shows the ones
  not shown yet (systemMessage) and gives the model each once
  (additionalContext). A pending prompt older than 60 s has lost its verifier:
  reported as not confirmed, or, if it had started sending, as "may not have
  been sent".
- Freshness counts only human prompts: every prompt of a supervising session is
  noted with its promptId; the verifier resolves earlier prompts' origins from
  the transcript. A bare `approve` takes a draft made after the owner's
  previous human prompt and before this one; noted prompts none of which is
  human (or any failure): nothing is fresh. So a scheduled prompt cannot make a
  draft written in its own turn look like the one the owner just read.
- The limits gate runs in parallel. A prompt it blocks is erased from the
  context and, as we understand Claude Code, never written to the transcript as
  a user entry, so the verifier would not find it and nothing is sent; the hook
  also reads the same verdict and does not act on a blocked prompt.

### Uncertain sends, unavailable server

- No answer in time, a network error, 5xx, 408, 429: "may not have been sent:
  Cardinal did not answer in time or could not be reached"; repeating is safe
  (same relay key: the server answers a duplicate). The draft stays, marked
  `uncertain`; rejecting or withdrawing it warns it may already have been
  delivered.
- 503 `owner_relay_unavailable`: not stored; "this Cardinal cannot store relayed
  decisions yet"; the draft stays.

### Text, targets, answers

- `decide:` is refused (nothing sent) with a blank line ("send one decision at
  a time"), any `?` ("a decision is not a question; to ask the worker
  something, tell the supervisor"), and the server's invisible characters
  (U+202A–202E, U+2066–2069, U+200E, U+200F, U+061C, U+E0000–E007F, U+200B,
  U+2060, U+FEFF; ZWJ/ZWNJ are fine), which propose-decision also refuses in
  `--text` and `--ref`.
- `supervising --name N` refuses a name held by a target of another
  investigation unless `--replace` (another session of the same investigation
  takes it). A bare `decide:` is refused when the target set changed (other
  than the first registration) since the owner's previous prompt ("it is now
  codex (session …); type it again as `decide codex: …`"), and any `decide:`
  when the targets changed after it was typed.
- propose-decision with `--answers N` reads event N with this machine's key and
  prints `answers #N: <type> "<excerpt, ≤80 chars>"`; it refuses a
  `challenge.added` / `ruling.issued` (a relayed decision cannot answer or
  overrule one). Unreadable: says so; the server checks. The approve outcome
  says `(answers #N)`.

### Server refusals, one owner line each

`owner_relay_not_member` (the key's person is not a member of the worker's org),
`owner_relay_answers_ruling` (only a web decision overrules a challenge or
ruling), `invalid_owner_relay` (incl. invisible characters),
`event_limit_reached` by `scope` (`owner_relayed_investigation`: the 50
relayed-decision cap, post from the web; `owner_decision_investigation`),
`Org mismatch`, `owner_relay_unavailable`. Unknown codes: "a reason this plugin
version does not know".

### Cursor

A relay-form prompt is blocked with `{"continue": false, "user_message":
<outcome>}`: the only way Cursor shows a hook's words to the user. The model
never sees that prompt; the outcome is staged for its next tool call.

### Worker and supervisor

- Verbatim label: "OWNER DECISION (relayed) — sent as <name>'s own words from
  their supervisor session <sid>"; the trust line is unchanged.
- Delegated supervision: only `owner_decision` overrules (shared fixture v2,
  `supervision_status.overrules_supervision`); `--conflicts-with` text names a
  web owner decision.
- A supervisor's (grantee) event reads mark each `owner_relayed` decision
  `relayed: by_you` when its own session's hook recorded sending that event
  (the store's `sent`, at most 50), else `unverified`. A local check: a process
  running as the same OS user could write that file too.

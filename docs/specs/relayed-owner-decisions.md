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

The owner approves by typing `approve` as a prompt in the supervisor session.
The runtime's prompt hook (harness-run; the model cannot produce a user prompt)
sees it and relays the drafted decision with the owner's API key. The server
can verify:

- the key belongs to a person (a `user:` principal), who authors the worker's
  investigation;
- that person turned on "accept owner decisions approved in my supervisor
  sessions" from their signed-in web session (a per-user, per-org setting that
  only a web session can change; off by default);
- the claimed supervisor session belongs to an investigation the same person
  authors;
- the text it stores is the text whose hash the relay carries.

It cannot verify that a human typed `approve`: the agent holds the same key and
could call the route itself. The resulting authority is `owner_relayed`, i.e.
attested by the owner's machine, the same trust level as text pasted into the
worker's terminal, and weaker than `owner_decision` (web session). Workers treat
it as the owner's own words; the label says how it arrived. The opt-in is the
owner's explicit acceptance of that level. Later hardening (out of scope): a
local user-presence gesture (Touch ID) the agent cannot perform.

Not used, on purpose: Claude's AskUserQuestion. Its input schema has an
`answers` field the model can fill, and we have not shown a hook can tell a
pre-filled answer from a real one. Revisit as an enhancement only after that is
verified.

## Flow

1. Owner tells the supervisor a decision for the worker (scope, permission,
   policy, stop rules...).
2. Supervisor drafts it and runs
   `cardinal-storyboard investigation propose-decision <worker_inv> --to-session <worker_sid> --text "<decision>" [--answers <seq>] [--ref <r>]... --session <supervisor_sid>`.
   The CLI stores a local proposal and prints its id (`dp_` + 8 hex) and the
   instruction to show the text verbatim and ask the owner to reply
   `approve dp_xxxxxxxx` (bare `approve` works when it is the only pending
   proposal) or `reject dp_xxxxxxxx`.
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
           approved_at: ISO-8601 },
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
  `id` = `dp_` + 8 random hex; expiry 30 min; at most 5 pending (oldest
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
  `^(approve|reject)(\s+dp_[0-9a-f]{8})?[.!]?$`. Bare form only when exactly
  one unexpired proposal is pending; otherwise tell the user which ids are
  pending.
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

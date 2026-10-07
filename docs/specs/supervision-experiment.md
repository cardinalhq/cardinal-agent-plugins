# Supervision experiment: task intent → activity → correction

Status: PLAN (not started). 2026-10-07.

Goal: build only enough to rerun one real scenario end-to-end and have an
external supervisor — no worker transcript — say from Cardinal alone:

> "It started to drift at #19. I intervened at #21. Claude accepted at #22.
> Subsequent activity returned to the bounded objective, baseline evidence was
> established, it merged, and stopped."

This is not the supervisor architecture. No Semantic WAL → InvestigationState
reducer, no delegated supervisor authority, no compliance engine.

## Why: what the survey found

Survey of maestro v1.102.0 (conductor `24b5e4d9`) + Claude plugin 0.43.0
(`0b28f99`), traced in code. Plugin 0.44.0 (PR #172, unmerged) does not change
any of this.

| Loop stage | Today | Gap |
|---|---|---|
| Bounded intent | FAIL | Investigation is created at SessionStart with `question: null` (`conductor:packages/maestro/src/routes/investigations.ts:565-581`). The prompt never leaves the machine (`adapters/claude/hooks/git-state.py:159-161` — "never args"; `OTEL_LOG_USER_PROMPTS` unset). `question` is Claude's paraphrase, ≤1000 chars one line, overwritable, tagged "not an owner instruction". No scope / forbidden / stop fields anywhere. |
| Observable activity | FAIL | No hook writes to the event stream; every `checkpoint`/`append_event`/`put_state` caller is in `bin/cardinal-storyboard`. Checkpoint guidance is shown once at SessionStart (`hooks/storyboard-session.py` `CHECKPOINT_LINE`) and says "Skip routine tool use". 20 minutes of off-scope work can leave `head_seq` unchanged. |
| Detect drift | FAIL | Nothing to compare against, nothing guaranteed to compare. No semantic field links work to an objective. |
| Corrective cue | PASS | `challenge.added` / `cue.added`, any org member with `storyboards:write`, optional `to_session_id` (`routes/investigation-events.ts:436-466`). Always `authority: advisory`. |
| Worker consumes cue | PARTIAL | Poller (5 s) → local inbox → shown after the *next* main-thread tool has already run (PostToolUse `additionalContext`) or as a Stop block. Skipped inside subagents (`hooks/investigation-events.py:58`). Never wakes an idle session. Stop skips its own read if the poller read "nothing" in the last ~6 s (`investigation-events.py:81-83`). Delivered-but-unacked is never redelivered. |
| Observe ack | PARTIAL | `acknowledged {ack_of, disposition, note}`, author-only, voluntary. No delivery receipt: "delivered, not acked" = "never delivered". |
| Verify compliance | FAIL | Nothing ties later behaviour to the cue; absence of off-scope work is unprovable. |

Capability ladder: **intent** (weak) → **activity** (weak) → judgment →
**intervention** (mostly exists) → **delivery** (exists, late) → **compliance**
(weak). Fix in that order, and only as far as the experiment needs.

## Ground rules

- **Intent capture is part of the Investigation feature, not telemetry.** It is
  a new `intent` event class with its own opt-in. General telemetry policy is
  unchanged: no `OTEL_LOG_USER_PROMPTS`, `git-state` stays "never args".
- **The agent has no path to write intent.** No MCP tool and no CLI subcommand
  can post intent events; only the hook can.
- **Supervisor input stays advisory** for this experiment. Owner instruction >
  owner-delegated supervisor directive > advisory challenge is a real ladder,
  but we first find out whether advisory challenges are enough.
- **Each phase ends with a real-session check** before the next phase starts.

## Phase 1 — task intent

### 1a. Conductor: `intent` event class

| Change | Where | Detail |
|---|---|---|
| Migration | new file in `packages/mcp-gateway/migrations/` | Add `task_intent.created`, `task_intent.amended` to the type CHECK. Add a new authority value, pinned by type through the existing type→authority CHECK. Add a unique index on (`investigation_id`, `producer_session_id`, `payload->>'turn'`) for intent rows: one event per turn, first write wins, no overwrite. The append-only trigger already exists. |
| Types | `packages/maestro/src/db/repositories/investigation-events.ts:58-73` | `INTENT_EVENT_TYPES`; `authorityOf()` maps them to the new authority. Separate cap (e.g. 500 per investigation), independent of the advisory and semantic caps. |
| Write route | `packages/maestro/src/routes/investigation-events.ts` | New `record-task-intent` route, author-only, `session_id` required, strict payload. Kept off the generic append route so the two can't be confused. |
| Read | same route + `packages/mcp-gateway/storyboard/tools/investigation_events.go` | Add `class: intent` to `read-investigation-events` and to the MCP tool's enum and description. `post_investigation_event` must keep rejecting intent types. |
| get-investigation | `packages/maestro/src/routes/investigations.ts:366-430` | Add `task_intent: {created_seq, latest_amended_seq, count}` so a supervisor can jump straight to it. |
| Projection | coordinate with conductor PR #2073 (touches `investigation-events.ts`; feeds the WAL to an LLM) | Projection must skip the `intent` class, or treat it as fenced untrusted input. Otherwise raw owner prompts end up in an org-visible Storyboard. Order the two migrations explicitly. |

Payload (strict):

```
text             verbatim prompt, scrubbed, ≤ ~32 KiB
truncated        bool
original_length  int
prompt_sha256    hash of the original, before scrubbing and truncation
source           "user_prompt"
turn             int (plugin's per-session counter)
captured_at      client timestamp
slash_command?   present when the prompt started with one
```

The first captured turn in a session is `created`; every later owner prompt is
`amended`. The plugin decides this mechanically and never judges whether a
prompt "counts" — "yes, go ahead" is a real owner amendment.

**Authority naming must be honest.** The hook posts with the same device key
that Claude's Bash can read from `~/.cardinal`, so the server cannot prove a
hook (rather than Claude running curl) wrote it. Proposed:
`owner_prompt_client_attested`, with a `trust` note on reads (same pattern as
InvestigationState's `quotes_attested_by: publishing_client`). It is still a
large step up from Claude's paraphrase:

- no agent-facing write path;
- written before the model sees the turn;
- one event per turn;
- the hash can be checked later against the transcript.

But the claim is "the Cardinal plugin recorded this as what the user typed",
not "Cardinal verified it". Truly unforgeable intent needs a credential the
agent can't reach; out of scope for this phase.

### 1b. Plugin: capture on `UserPromptSubmit`

| Change | Where | Detail |
|---|---|---|
| Hook | new `adapters/claude/hooks/task-intent.py`, registered on `UserPromptSubmit` in `hooks.json` | Exit if opt-in is off, the session has no binding, the binding is not `is_author`, or the turn is a task notification (reuse `decision-prompt.py:72-73`). Otherwise: scrub with the scrubber in `core/cardinal_core/evidence_capture.py`; increment `turn` in the binding under the existing lock; post synchronously within ~1.5 s; on failure append to `<sid>.intent-outbox.json`. |
| Outbox flush | the `investigation-events` hooks (Stop branch) and the next prompt's hook | Keeps the poller read-only ("never uploads anything"). `turn` keeps ordering reconstructible even if an intent row gets a later `seq` than a checkpoint from the same turn. |
| Client | `core/cardinal_core/investigation_events.py` | `record_intent()`. Intent types stay out of `deliverable()` (already advisory-only; add a test that keeps it so). |
| Opt-in | `cardinal-storyboard investigation intent on\|off\|status` (same pattern as `cardinal-decision on`) | Off by default. When on, SessionStart context says one line: "This session's prompts are recorded verbatim to Investigation inv_…" — transparency for the user, not an instruction to Claude. |
| Bootstrap-failure path | `adapters/claude/hooks/storyboard-session.py:277-284` | If the Investigation is created late, the outbox drains once the binding exists. |
| Tests | targeted pytest (hook + core); vitest (route + migration) | No local `make check`; CI is the gate. |

**Phase 1 exit check.** A fresh supervisor, as a different principal, reads
`class=intent` and quotes the Step 1–3 instruction verbatim with turn, time
and principal. An amendment appears as a new seq; #1 is unchanged.

## Phase 2 — rerun the scenario with intent only

Run on a real, messy repo task as close as possible to the original Step 1–3
session.

Setup that affects validity:

- **The supervisor must not be the worker's own user.** Another device-login
  key for the same user resolves to `user:<id>` and is treated as the
  investigation author. The worker's plugin then suppresses its cues if they
  carry the worker's session id (`_own`,
  `core/cardinal_core/investigation_events.py:982-988`), and it could post acks
  and checkpoints in the worker's name. Use a teammate's account or an org API
  key with no `principal_user_id`.
- **The worker must actually run the new plugin.** Installs are per machine
  (the authoring machine was still on 0.41.0 at survey time). Pin the build and
  confirm from the SessionStart context that the checkpoint guidance appears.
- **The supervisor gets only** Cardinal MCP read tools plus
  `storyboard__post_investigation_event`. No transcript.

Protocol:

1. The supervisor reads the stream every few minutes and logs every judgment
   with the seq it is based on.
2. Afterwards, compare against the transcript. For each drift moment record:
   when drift actually began (transcript), the first durable signal (seq +
   time), the latency, and the signal the supervisor would have needed.

Expected: intent PASS, drift detection still FAIL. The deliverable is the
measured gap list, which becomes the Phase 3 requirements: "the minimum
activity signal that would have let the supervisor see #19 on time."

## Phase 3 — minimal automatic activity (shape decided by Phase 2)

Not designed yet, on purpose — otherwise we build agent telemetry rather than
a supervisory substrate. Known starting points:

- **Labels already exist.** Claude Code's Bash and Agent tools carry a
  `description` Claude writes on every call ("Run LocalStack integration
  tests"). Captured automatically, it is a per-call summary Claude cannot skip,
  though Claude still words it. Bash `description` is optional.
- **Structural fields already ship off-machine.** The closed `bash_class` set
  and edit targets (`hooks/turn-usage.py`) and subagent descriptions capped at
  160 chars (`hooks/subagent-usage.py`) already go to lakerunner. Structural
  activity in an Investigation sets no new privacy precedent; command text and
  edit contents would.
- **Emit at PreToolUse**, so the supervisor sees work start, not finish.
  Coalesce consecutive same-label calls within a window. Own volume cap, own
  authority value (e.g. `runtime_captured`).
- **Cover subagents** (payload `agent_id` set), tagged by subagent — that is
  where a QEMU branch hides.

Gate: rerun the scenario. The supervisor should flag drift at the event
corresponding to #19, with acceptable latency.

## Phase 4 — delivery fixes (only if the Phase 2/3 runs show they mattered)

Small and local, in expected-value order:

1. **Show the correction before the tool runs.** PreToolUse checks the local
   inbox (no network); if a challenge is waiting, deny that one call with the
   challenge as the reason, and Claude may reissue it. The challenge stays
   advisory, but this is a one-time tool veto from a non-owner — an explicit
   authority decision.
2. **Close the Stop gap.** At Stop, skip the `fresh()` shortcut
   (`hooks/investigation-events.py:81-83`) when a challenge could matter (cost
   ≤2.2 s per Stop). This covers "merge and stop".
3. **Redeliver unacknowledged challenges** after N boundaries.
4. **Deliver inside subagents** via their own PostToolUse.

Not doing: waking an idle session, delegated supervisor authority, enforcing
acknowledgement.

## Not building yet

- WAL → InvestigationState reducer.
- Compliance tracking. After Phase 3, check whether intent + activity + ack +
  semantic events are enough for the success sentence above with no
  transcript. Build compliance only if that fails.
- Codex, Cursor and Gemini adapters.

## PR sequence

1. Conductor: intent class, route, read filter, gateway enum, projection
   exclusion. Coordinate with #2073; deploy as a PR image.
2. Plugin: hook, opt-in, outbox, client. Release 0.45.0 after #172 lands.
3. Phase 2 report → Phase 3 requirements.
4. Activity capture PRs (conductor + plugin).
5. Rerun and write it up against the success sentence.

## Open decisions (before Phase 1)

1. **Opt-in scope:** user flag only (simplest; fine for the experiment), or
   also an org setting the server enforces?
2. **What to capture:** every owner prompt as `created`/`amended`
   (recommended), or only the first?
3. **Text cap:** ~32 KiB plus hash? Pasted blocks run ~10 KiB.
4. **Authority name:** `owner_prompt_client_attested`, or another name that
   still says client-attested, not server-verified?
5. **Supervisor principal** for the experiment: a teammate, or an org API key?

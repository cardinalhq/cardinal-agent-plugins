# Cross-agent parity: Claude, Codex, Cursor, Gemini

Status: plan v2 (2026-10-12). v1 was independently reviewed (approve-with-fixes: 7
blockers, 12 should-fix); v2 resolves all of them (see §8).
Branch: `feat/cross-agent-parity`. Owner direction: close every parity gap between the
four adapters ("Everything"). A gap may be documented only when the agent runtime
itself can't support the feature.

Origin: review of the 2026-10-12 storyboard/investigation feedback. These were the real
problems:

1. Owner-input capture exists only in Claude. That is why a supervisor couldn't check
   what the owner told a Codex worker. A new event type is not needed.
2. Supervisors are never told about `investigation events --follow / --exclude-self /
   --fields compact`.
3. Before any publish, the storyboard link shows an empty draft. Codex prints both URLs
   without labels; Cursor and Gemini print neither.

Two audits then found that Cursor and Gemini lack almost all of the investigation
layer, and that storyboard authoring and Checks exist only on Claude.

## 1. Ground rules for every lane

- Shared behavior lives in core (`core/cardinal_core`) or `common/`. Adapters only map
  the runtime's channels: payload keys, output schema, hook registration.
- Code must run on Python 3.9 (CI `py39-support.yml`). Hooks use the stdlib only.
- Hooks fail open, respect unconnected-mode gating, and stay inside the runtime's
  timeout. Network work is detached or runs under a hard deadline.
- Re-vendor before testing in any checkout or worktree: `python3 build/vendor.py <a>`.
  The vendored copies are gitignored and are stale locally today.
- **Never run `make check` or any `check-*` target locally.** Run targeted tests only
  (`python3 -m pytest <files>`, or `cd adapters/<a> && python3 -m unittest <mod>`).
  CI is the gate.
- Each new hook behavior gets a test modeled on the Claude test for that feature.
- No server changes are needed. Maestro's `record-owner-input` accepts any `client`
  string (conductor `routes/investigation-events.ts:520-531, 753`, verified by the
  reviewer).
- **Core is frozen during Phase 2.** If a Phase 2 lane needs a core change, it stops
  and reports the need to the coordinator. It does not edit core.

## 2. Authority and privacy rules (they go into the core contract, not the lanes)

R1. **Advisory text never travels as owner input.** It may reach the model through
context channels (`additionalContext` / `additional_context`, or PostToolUse/AfterTool
output). A turn-end *block*, which re-enters the model as a user-role message, is
allowed only where (a) the runtime marks the injected text as hook-originated, and
(b) the block does not re-fire the prompt hook, so it can't be captured as owner input:

| runtime | turn-end block allowed | why |
|---|---|---|
| claude | yes (today's behavior) | the Stop-hook reason is shown as hook feedback, and UserPromptSubmit does not re-fire |
| codex | yes (today's behavior) | it is injected as a `<hook_prompt>`-wrapped item, and UserPromptSubmit does not re-fire (reviewer checked a local rollout) |
| gemini | **no** | an AfterAgent block arrives as plain user-role text and re-fires BeforeAgent with the reason as `prompt` (gemini-cli 0.50.0 `chunk-W4YMYD53.js:335749-335757`) |
| cursor | **no** | `stop.followup_message` arrives as a user turn |

Where blocking isn't allowed, turn-end deliverables are *staged*. They are delivered at
the next context-capable boundary: Gemini BeforeAgent `additionalContext` or AfterTool;
Cursor postToolUse or sessionStart.

R2. **Owner input = what the owner typed, or nothing.** Core `capture()`:
- refuses any payload with a non-empty `agent_id` (subagent context; Codex
  UserPromptSubmit carries `agent_id`/`agent_type`);
- runs `normalize_prompt(runtime, prompt)` and skips capture when that returns
  None. On Gemini, normalization strips `<hook_context>…</hook_context>` blocks (the
  plugin's own SessionStart context in `-p` mode) and the `--- Content from
  referenced files ---` section (inlined `@file` contents). A prompt that can't be
  reduced confidently to typed text is not captured;
- on Gemini only, skips a repeat of the same session + same scrubbed sha within one
  turn and within 30 s of when it was first seen (Gemini re-fires BeforeAgent on
  loop-detection resubmits; Claude, Codex and Cursor fire once per prompt);
- skips machine prompts (Claude task notifications, plus any per-runtime equivalents
  found during verification).

R3. **Disclosure before the first capture, on every runtime.**
- Claude, Codex and Gemini: show the disclosure as a user-visible `systemMessage` on
  the first prompt, and capture from the next prompt on. This is Claude's existing
  semantics. Codex's UserPromptSubmit `systemMessage` is proven
  (`codex/tests/test_parity.py:1274`). SessionStart `systemMessage` on Codex is
  unproven, so it is not relied on.
- Cursor has no non-blocking user-visible channel per session (`CORE_GAPS.md:31-37`).
  Capture there requires explicit opt-in consent given in the user's terminal:
  `scripts/cardinal-connect --owner-input on|off`, with the full `DISCLOSURE` text
  printed. Consent is stored versioned against the disclosure text's sha in
  `~/.cursor/cardinal/owner-input-consent.json`. Without consent, Cursor does not
  capture, and the per-session disclosure is recorded as a documented platform limit
  with that stronger opt-in as the compensation. Nothing blocks a prompt.

R4. **Kill switch on every runtime.** The `CARDINAL_OWNER_INPUT=0|false|off|no`
environment variable, plus a Cardinal-owned file `~/.cardinal/owner-input.json`
(`{"<runtime>": "off"}`) for GUI apps that never see shell env. Claude additionally
keeps its settings-`env` reading.

## 3. Gap inventory

Legend: P = present, A = absent, ~ = partial, L = platform limit (cited), N/A = not a
parity item (with reason).

### 3a. Investigation / supervision layer

| Feature | C | X | U | G |
|---|---|---|---|---|
| Investigation bootstrap + binding at session start | P | P | A | A |
| Background poller + capability refresh | P | P | A | A |
| Owner-input capture | P | A | A | A |
| Owner-input disclosure | P | A | L (connect-time opt-in, R3) | A |
| Owner-input outbox flush at turn end | P | A | A | A |
| Advisory delivery at tool boundary + subagent guard | P | ~ (no guard) | A | A |
| Turn-end: inbox, sync check when poller stale, supervision_stop, decision reminders | P | ~ (no sync check, no flush) | A | A |
| Re-surfacing accepted challenges / directives | P | P | A | A |
| Session guidance text (link, question, checkpoint, ACK/POST, SUPERVISION) | P | ~ (separate, shorter builder) | A | A |
| Labeled link text | P | ~ (unlabeled) | A | A |
| `investigation_failures.observe` | P | P | A | A |
| Supervisor watch guidance (`--follow` etc.) | A | A | A | A |

### 3b. Storyboard authoring, skills, other

| Feature | C | X | U | G |
|---|---|---|---|---|
| Preview auto-render PNGs (+ cover / unfurl mock) | P | A | A | A |
| Hero upload on publish | P | A | A | A |
| Evidence token store | P | A | A | A |
| Edit lookup | P | A | A | A |
| Context stamping on storyboard tools | P | ~ (L: needs `allow`) | L (beforeMCPExecution can't rewrite input) | P |
| Discovery re-look on prompt when branch/HEAD moved | P | A | A | A |
| Discovery on subagent start | P | A (SubagentStart exists) | verify | L (no subagent events, W4:324340-324352) |
| Connect hint + stray-key warning | P | A | A | A |
| canvas skill | P | A | A | A |
| decision skill | A | P | A | A |
| connect/disconnect/status skills | P | P | A | verify that Gemini loads them |
| brainstorm subagent | P | A (`[agents.<name>]` supported) | verify | A (`<ext>/agents` supported) |
| Checks preview + `cardinal-behavior` MCP | P | A | A | A |
| Evidence capture on tool failure | P | L (no failure event; Bash non-zero exit covered) | P | P |
| Invariant check | P | N/A | N/A | N/A |

The invariant check is N/A because it is an internal developer gate: a PreToolUse
check that needs a local conductor checkout. Run on post-tool events it fires after
the edit, which defeats it.

Telemetry limits that stay as they are (already documented): Cursor has no token
counts and no `sessionStart` in cloud agents; Gemini has no subagent stop and no
cached tokens; the Codex sandbox has no network and can't write to `~/.codex`.

## 4. Frozen core API (Phase 1 deliverable)

Lane A's part below is FINAL (implemented in Phase 1a; signatures as shipped).
Lane E's `storyboard_authoring` part is FINAL too (Phase 1b). Phase 2 lanes code only
against this section.

Conventions for every function below:
- `home` is the USER's home (`Path.home()`), never the agent dir; per-runtime
  files are derived from it (`~/.cardinal/…`, `~/.<runtime>/cardinal/…`).
- `deadline` is an absolute `time.monotonic()` value.
- Nothing raises: a failure delivers nothing / captures nothing.
- `wiring` is a `storyboard_agent.Wiring` (needs `runtime`, `paths`, `environ`,
  `client`, `connection()`, `_off()`, `cli`).

```python
# core/cardinal_core/investigation_text.py  (new; the ONE session-guidance builder)
CLAUDE_CMD = "cardinal-storyboard investigation"
STORYBOARD_LABEL = "Storyboard (private draft until published)"
TIMELINE_LABEL = "Investigation timeline (shows the live record before any publish)"
# also: SESSION_ID_LINE, ADVISORY_LINE, SHARE_LINE, SKILL_LINE, CHECKPOINT_LINE,
#       FIRST_UPDATE_LINE, ACK_COMMAND_LINE, JOINED_LINE, SANDBOXED, SANDBOX_LINE,
#       WATCH_GUIDANCE (the supervisor watch help: --follow, --exclude-self,
#       --fields compact, one --follow per investigation, sandbox approval, MCP tools)
def link_lines(view_url: Optional[str], investigation_url: Optional[str], *, sep: str = "\n") -> str
    # "<STORYBOARD_LABEL>: <u>" then "<TIMELINE_LABEL>: <u>", only those present; "" when none
def session_text(sid: str, binding: dict, *, cmd: str = CLAUDE_CMD, target: str = "",
                 runtime: str = "claude", question_stated: Optional[bool] = None,
                 owner_input_recorded: bool = False) -> str
    # One line. Variants: no storyboard (short) / joined / author. Author: ids, link_lines,
    # SHARE_LINE, SKILL_LINE, question line (until question_status == "stated"; None: from
    # the binding), owner_input.DISCLOSURE when owner_input_recorded, ADVISORY_LINE,
    # ie.ACK_GUIDANCE, ie.POST_GUIDANCE, ie.SUPERVISION_LINE when capable, CHECKPOINT_LINE.
    # Commands are `{cmd} <action>{ target}`; checkpoint always `--session <sid>`.
    # runtime != "claude": + FIRST_UPDATE_LINE, ACK_COMMAND_LINE, JOINED_LINE (joined);
    # runtime in SANDBOXED ("codex", "cursor"): + SANDBOX_LINE.
    # Claude's storyboard-session.py live_line and investigation_agent.describe both call it.
def watch_command(investigation_id: str, *, credential_file: Optional[str] = None,
                  cmd: str = CLAUDE_CMD) -> str
    # "<cmd> events <inv> [--credential-file F] --follow --json --fields compact"

# core/cardinal_core/investigation_turn.py  (new; runtime-neutral turn logic, no Wiring)
@dataclass
class Delivery:
    agent_context: List[str] = []      # model-context texts for this boundary, in order
    user_message: Optional[str] = None  # user-visible text (never model context)
    turn_end_block: Optional[str] = None  # only when TURN_END_BLOCK[runtime]
TURN_END_BLOCK = {"claude": True, "codex": True, "gemini": False, "cursor": False}
def can_block(runtime: str) -> bool                       # unknown runtime: False
def at_tool(home: Path, sid: str, org: Optional[str], runtime: str, *, is_subagent: bool) -> Delivery
    # is_subagent: nothing, nothing changes (inbox, cursor, staged items wait).
    # Else staged items first, then the inbox, then (only if nothing else) one re-surfaced
    # challenge/directive; the boundary is counted either way. org: binding of another
    # org gets nothing (None: not checked). No network.
def at_turn_end(home: Path, sid: str, conn: Optional[dict], client: str, runtime: str, *,
                environ: Mapping, stop_hook_active: bool, deadline: float,
                owner_input_flush: Optional[Callable[[float], None]] = None) -> Delivery
    # inbox; else (poller not fresh) one sync ie.check bounded by the deadline (keeps
    # TURN_END_RESERVE = 0.4 s for the flush); else supervision_stop / remind_decisions
    # (own org, no grant token in environ). can_block(runtime): turn_end_block; otherwise
    # every item is stage()d and nothing is returned. Always, last:
    # owner_input_flush(deadline) and owner_input.turn_ended(home, sid).
def at_prompt(home: Path, sid: str, runtime: str) -> Delivery   # consume staged items (no inbox)
def stage(home: Path, sid: str, text: str) -> None   # <sid>.staged.json, 0600, MAX_STAGED=20
def consume_staged(home: Path, sid: str) -> List[str]
def staged_path(home: Path, sid: str) -> Path

# core/cardinal_core/investigation_agent.py  (Wiring wrappers for codex/cursor/gemini)
start(wiring, sid, source=None, *, cwd=None) -> Optional[str]    # unchanged; text via describe
describe(wiring, sid, binding) -> str        # session_text(cmd=command(wiring),
                                             #   target="--session <sid>", runtime=wiring.runtime,
                                             #   owner_input_recorded=owner_input_recorded(...))
owner_input_recorded(wiring, binding) -> bool  # enabled + no kill switch + consent
deliver(wiring, sid, emit, *, is_subagent=False) -> None     # at_tool; emit(text) per item
turn_end(wiring, sid, *, stop_hook_active: bool, deadline: float, cwd: Optional[str] = None,
         owner_input: bool = True) -> Delivery
    # at_turn_end with owner_input.flush_at_turn_end as the flush (owner_input=False: none)
prompt(wiring, sid) -> Delivery                              # at_prompt
# Every text these return has "cardinal-storyboard investigation" replaced by
# command(wiring). All require: enabled(wiring), a valid sid, a binding on this
# connection's org; else nothing.

# core/cardinal_core/owner_input.py  (additions)
RUNTIMES = ("claude", "codex", "cursor", "gemini")
CONSENT_REQUIRED = frozenset({"cursor"})
DEDUPE_REPEATS = frozenset({"gemini"})   # within REPEAT_WINDOW = 30 s; others never deduped
MACHINE_PROMPTS = {"claude": (task-notification,), "gemini": (loop-recovery request,)}
def normalize_prompt(runtime: str, prompt: Any) -> Optional[str]
    # None: not a str / blank / machine prompt / unsure. gemini: strips every
    # <hook_context>…</hook_context> and the "--- Content from referenced files ---" …
    # "--- End of content ---" section, then .strip(); a leftover marker -> None.
    # Other runtimes: unchanged.
def is_machine_prompt(runtime: str, prompt: Any) -> bool
def kill_switch(runtime: str, home: Path, environ: Mapping, *, cwd: Optional[str] = None) -> bool
    # CARDINAL_OWNER_INPUT=0|false|off|no in environ; ~/.cardinal/owner-input.json
    # {"<runtime>": "off"} (a non-object file counts as off); claude also ~/.claude and
    # project (cwd, $CLAUDE_PROJECT_DIR) settings `env`. Cannot tell: True.
def kill_switch_path(home: Path) -> Path
def consent(runtime: str, home: Path) -> bool         # not in CONSENT_REQUIRED: True
def set_consent(runtime: str, home: Path, on: bool) -> None   # raises ValueError / OSError
def consent_text(runtime: str) -> str                 # print in full where the user consents
def consent_path(runtime: str, home: Path) -> Path    # ~/.<runtime>/cardinal/owner-input-consent.json
    # {on, text_sha256 (of consent_text), at}; another text => asked again
def disclosure_message(runtime: str, text: str) -> str   # user-visible; says how to turn it off
@dataclass
class CaptureResult:
    status: str                       # debug code, never the prompt
    disclosure: Optional[str] = None  # show to the user NOW (systemMessage or equivalent)
def capture(home: Path, sid: str, prompt: Any, *, runtime: str, conn: Optional[dict], client: str,
            environ: Mapping, agent_id: Optional[str] = None, deadline: float,
            detached: bool = False, cwd: Optional[str] = None, now: Optional[float] = None,
            opener=None) -> CaptureResult
    # The gate, in order: invalid (runtime/sid) -> kill switch ("disabled": outbox deleted,
    # disclosure forgotten) -> agent_id ("subagent") -> blank ("no_prompt") ->
    # normalize_prompt ("not_typed") -> enabled() ("unbound" / "not_author" /
    # "capability_off") -> consent ("no_consent", outbox deleted) -> DEDUPE_REPEATS
    # ("repeat"; "busy") -> not disclosed: mark it and return ("disclosed", message) — this
    # prompt is not captured; on CONSENT_REQUIRED runtimes consent stands in, so it is
    # marked and capture goes on -> no connection ("no_connection") -> detached: turn taken
    # and entry queued under the lock, spawn_flush ("detached"; "queued" if no process)
    # | else submit() bounded by deadline ("posted" / "duplicate" / "queued" / "dropped" / "busy").
    # What is sent is the normalized prompt.
def turn_ended(home: Path, sid: str) -> None        # the dedupe turn boundary (at_turn_end calls it)
def spawn_flush(home: Path, sid: str, conn: dict, client: str) -> bool
    # detached `python -I` posting the outbox; the connection goes over a pipe
def flush_at_turn_end(home: Path, sid: str, deadline: float, *, runtime: str, conn: Optional[dict],
                      client: str, environ: Mapping, cwd: Optional[str] = None) -> None
    # no outbox: nothing; kill switch: delete + forget; no consent: delete; else flush()

# core/cardinal_core/paths.py
AgentPaths.owner_input_consent_path -> Path          # <agent dir>/cardinal/owner-input-consent.json
```

How the Phase 2 lanes call it (Claude's hooks are the reference wrappers):
- Prompt hook: `res = owner_input.capture(Path.home(), sid, payload_prompt, runtime=…,
  conn=wiring.connection(), client=wiring.client, environ=wiring.environ,
  agent_id=payload.get("agent_id"), deadline=START + budget, cwd=payload.get("cwd"),
  detached=<Gemini: True>)`; show `res.disclosure` as a user-visible message when set.
  Then `investigation_agent.prompt(wiring, sid).agent_context` into the prompt's
  additional context.
- Tool boundary: `investigation_agent.deliver(wiring, sid, lines.append,
  is_subagent=bool(payload.get("agent_id")))`.
- Turn end: `d = investigation_agent.turn_end(wiring, sid, stop_hook_active=…,
  deadline=START + budget, cwd=…)`; block with `d.turn_end_block` only when it is set
  (it never is on Gemini/Cursor). Gemini's AfterAgent and Cursor's stop must call it:
  it is also the owner-input dedupe turn boundary.
- Cursor connect: print `owner_input.consent_text("cursor")` in full, then
  `owner_input.set_consent("cursor", Path.home(), on)`.

```python
# core/cardinal_core/storyboard_authoring.py  (new; Lane E; FINAL, as shipped in Phase 1b)
# Conventions (in addition to the ones above):
# - `paths` is the runtime's AgentPaths (agent_paths(runtime) = ~/.<runtime>; Wiring.paths).
#   Renders: paths.runtime_dir/"storyboards"/<sb>/r<rev>/ (render_preview.py --runtime <r>'s
#   default); edit-lookup state "storyboard-edit-lookup"; discovery cache "storyboard-discovery";
#   capability cache "server-caps.json". Evidence tokens and the connect / key-warning markers
#   are the user's (~/.cardinal/...).
# - `connection` is a zero-arg callable -> {origin, org, key} or {} (Wiring.connection),
#   called only after the local gates. None: codex/cursor/gemini use
#   evidence_promote.agent_paths_connection(paths); claude gets {} (its rule is the adapter's).
# - `client` is the X-Cardinal-Client value or a zero-arg callable returning it (resolved only
#   when a request is sent); None -> the runtime name. Lanes pass wiring.client.
# - Tool results are MCP-shaped ({content, structuredContent} / text blocks / JSON text; a
#   Cursor tool_output string is fine). Gemini converts AfterTool's {llmContent, returnDisplay}
#   first (its gemini_tool_response). Only claude follows a spill notice (~/.claude/projects).
# - Opt-outs (CARDINAL_STORYBOARD_HERO, CARDINAL_STORYBOARD_DISCOVERY) come from `environ`;
#   claude also reads ~/.claude/settings.json `env`. Nothing raises.
RUNTIMES = ("claude", "codex", "cursor", "gemini")
CARDINAL_SERVERS = {"claude": ("cardinal", "plugin_cardinal_cardinal"), "codex": ("cardinal",),
                    "cursor": ("cardinal",), "gemini": ("cardinal",)}
def is_cardinal_tool(runtime: str, tool_name, op: str | Iterable[str] | None = None, *,
                     server: Optional[str] = None) -> bool
    # Cardinal's own server in this runtime's naming (mcp_tool), and op (one name or several)
    # when given. claude/codex: mcp__<server>__<tool>. cursor: mcp__<server>__<tool>; the
    # serverless "MCP:<tool>" only with server= (unverified on a live Cursor; Lane D checks).
    # gemini: mcp_<server>_<tool> (gemini-cli 0.50 generateValidName, W4:265051-265070,
    # 265388-265400); pass server=mcp_context.server_name when the payload has it (the
    # flattened name is ambiguous), and the bare mcp_context.tool_name then works too.
def mcp_tool(runtime, tool_name, *, server=None) -> Optional[Tuple[str, str]]   # (server, tool)

def on_preview(paths, runtime: str, tool_result, *, session_id=None, tool_input: dict | None = None,
               renderer: Path | None = None, environ=None, started: float | None = None) -> str | None
    # SLOW (Chromium). Caller first checks is_cardinal_tool(runtime, name, "storyboard__preview")
    # (the renderer fetches what the result names with the Cardinal key). Runs
    # skills/storyboard/scripts/render_preview.py --runtime <r> (renderer default: the plugin
    # this core is vendored into), then the card's cover and link-preview mock (one Chromium
    # launch each). Returns the context: PNG dir, per-scene PNGs/errors, the Read instruction,
    # cover/mock lines; at most MAX_CONTEXT_CHARS. No Chromium: said once per session_id.
    # Bounded: renderer --timeout RENDER_TIMEOUT_S=120, killed at PREVIEW_BUDGET_S=135 from
    # `started`; the hook entry needs PREVIEW_HOOK_TIMEOUT_S=150 (Claude's hooks.json value).
    # Measured 2026-10-12 (Chrome 153, macOS arm64, 2-step local pages, no bundle fetch):
    # 1 scene 2.5 s warm / 7.5 s cold, 5 scenes 6.6-9.5 s, 10 scenes 12-19 s; the static mock
    # 2.4-3.0 s, the cover one more launch. Real scenes (d3, prefabs) and each bundle GET add
    # to that. So: a short preview with a card is ~10 s, a 10-scene one ~20-40 s, and the
    # 135 s budget is reachable on big storyboards. Gemini: a distinct command with a
    # ~150000 ms timeout; Cursor: if no entry can get ~150 s, render detached and stage the text.
def on_publish(paths, runtime: str, tool_result, *, connection=None, client=None, environ=None,
               opener=None) -> str | None
    # NETWORK: one PUT of the hero render (storyboard_hero), bounded by UPLOAD_TIMEOUT_S=7 (hook
    # entry ~10 s). Caller checks is_cardinal_tool(runtime, name, "storyboard__publish").
    # None: CARDINAL_STORYBOARD_HERO off, not connected, unpublished / card-less result.
def observe_cardinal_result(paths, runtime: str, tool_name, result, *, session_id,
                            server: Optional[str] = None, home: Path | None = None, environ=None) -> None
    # Local only. Gates itself on is_cardinal_tool(runtime, tool_name, TOKEN_TOOLS, server=server)
    # (storyboard__create / add_act / preview); stores the evidence token under
    # ~/.cardinal/evidence/<session_id>/token.json unless evidence capture is off.
def edit_lookup(paths, runtime: str, sid, path, cwd, *, connection=None, client=None, environ=None,
                deadline: float | None = None, opener=None,
                debug: Callable[[str], None] | None = None) -> str | None
    # NETWORK: one find, ending by deadline (default now + EDIT_BUDGET_S=1.5). Stdlib fast path
    # first (opt-out, directory already looked up, EDIT_MAX_LOOKUPS, a fresh <sid>.skip marker),
    # then connection(); unconnected / caps < 1 leaves the marker. The module imports nothing
    # heavy (subprocess, paths, tempfile load lazily: ~12-20 ms). Block <= 1 KB, framed as DATA.
def relook_if_moved(paths, runtime: str, sid, cwd, *, event: str = "UserPromptSubmit",
                    connection=None, client=None, environ=None, deadline: float | None = None,
                    deliver_by: float | None = None, opener=None) -> str | None
    # NETWORK: up to two requests (storyboard_discovery.discover), ending by deadline (default
    # now + 2 s); a block ready after deliver_by is stored as pending for the next prompt.
    # UserPromptSubmit looks only when branch/HEAD moved (one git call otherwise);
    # "SessionStart" always looks; "SubagentStart" re-emits the stored block (no network).
    # Callers run it detached or under a hard deadline, and stage what it returns.
def stray_key_warning(paths, runtime: str, *, sid=None, source=None, home: Path | None = None,
                      environ=None) -> str | None
    # Local only. "Tell the user, briefly: …" when an MCP key is set without a URL (claude:
    # settings env + environ; others: cardinal-secrets.json mcp_api_key / CARDINAL_MCP_API_KEY
    # vs cardinal.json mcp_url / CARDINAL_MCP_URL); once per sid (~/.cardinal/key-warning/),
    # without a sid only on source "startup". Also: key_without_url, mcp_url, stray_key_text.
def connect_hint(paths, runtime: str, *, source=None, connected: bool | None = None,
                 home: Path | None = None, environ=None) -> str | None
    # Local only. Unconnected: "Tell the user once, briefly: …" on this runtime's first
    # unconnected startup (~/.cardinal/connect-hint, connect-hint-<runtime> for non-claude),
    # else the line as context only. connected None -> is_connected(paths, runtime) (ingest
    # connection or MCP key); claude must pass it (hooks/_connection.is_connected).
# Also public: agent_paths, storyboards_dir, edit_state_dir, discovery_state_dir, caps_path,
# spill_root, unwrap, preview_result, publish_result, publish_message, build_context,
# card_lines, crash_context, scene_lines, mock_html, hero_disabled, discovery_disabled,
# edit_lookup_disabled, is_connected, connect_text.
```

How Phase 2 lanes call it (Claude's hooks are the reference wrappers):
- Post-tool: `if sa.is_cardinal_tool(rt, name, "storyboard__preview", server=…): ctx =
  sa.on_preview(wiring.paths, rt, result, session_id=sid, tool_input=…)` in its own
  long-timeout entry; same for `storyboard__publish` -> `sa.on_publish(wiring.paths, rt, result,
  connection=wiring.connection, client=wiring.client)`; on every tool,
  `sa.observe_cardinal_result(wiring.paths, rt, name, result, session_id=sid, server=…)`.
- Edit: `sa.edit_lookup(wiring.paths, rt, sid, file_path, cwd, connection=wiring.connection,
  client=wiring.client, deadline=START + 1.5)`.
- Prompt: `sa.relook_if_moved(wiring.paths, rt, sid, cwd, connection=wiring.connection,
  client=wiring.client, deadline=…)`, detached where the prompt budget is tight; stage the block.
- Session start: `sa.stray_key_warning(wiring.paths, rt, sid=sid, source=…)` and
  `sa.connect_hint(wiring.paths, rt, source=…)`.
- Claude: `storyboard-session.py` still has its own `connect_hint` / `key_warning` (Lane A's
  file; the same text as core's claude branch); routing them through core is a follow-up.

## 5. Phases and lanes

```
Phase 1a  Lane A  (core investigation + Claude refactor)             one agent
Phase 1b  Lane E  (storyboard authoring + canvas; on A's head)        one agent
Phase 2   Lanes B Codex | C Gemini | D Cursor | F Claude              parallel worktrees off 1b head
Phase 2b  Lane G  (Checks + cardinal-behavior on all runtimes)        after B/C/D merged
Phase 3   integration: parity.json + guard test, READMEs, version bumps
```

File ownership:
- Phases 1a and 1b run one after the other, so they can't conflict.
- In Phase 2 each lane owns only `adapters/<its adapter>/`. Lane F owns only
  `adapters/claude/skills/decision/` and the Claude README section for it.
- Lane G owns `common/behavior/`, `build/release.py`, `.github/workflows/`, and the MCP
  registration blocks in the four connect scripts. It edits connect scripts only after
  Phase 2 has merged.

### Lane A — core investigation layer + Claude refactor

Owns:
- `core/cardinal_core/{investigation_text.py (new), investigation_turn.py (new),
  investigation_agent.py, investigation_events.py, owner_input.py, investigation_cli.py,
  storyboard_agent.py, paths.py}`
- `adapters/claude/hooks/{storyboard-session.py, investigation-events.py, owner-input.py,
  _owner_input.py}`
- `adapters/claude/bin/cardinal-storyboard` (help text only)
- `common/storyboard/CORE.md` (new "Supervising another session" section)
- `docs/specs/supervision-cli.md`

Work:
- A1. Build `investigation_text`, `investigation_turn` and the `investigation_agent`
  wrappers per §4.
  - Move Claude's `live_line` into `session_text`. Claude keeps any lines specific to
    it (e.g. the `storyboard__find` instructions) as runtime-keyed branches inside
    `session_text`, not as a separate builder.
  - Make Claude's `investigation-events.py` Stop/PostToolUse logic a thin wrapper over
    `at_turn_end` / `at_tool`. The subagent guard is at `investigation-events.py:77`.
- A2. Labeled link text via `link_lines`. The guidance says that before a published act
  exists, the investigation URL is the one to share with someone who wants to follow.
- A3. `ACK_GUIDANCE` already carries the refs rule (`investigation_events.py:1449`,
  #202). Add "acceptance is not completion" to it. Remove the duplicate refs sentence
  from `SUPERVISION_LINE` (:1561-1562).
- A4. Supervisor watch guidance goes in four places:
  - the `investigation grant` output: one watch command line
  - the `events --help` epilog
  - the `common/storyboard/CORE.md` section
  - the spec

  It must say that `--follow` replaces hand-written loops, that `--exclude-self` is
  the default under `--follow --session`, that `--fields compact` gives a uniform
  `text`, and that there is one `--follow` per watched investigation. It must also
  say that inside a sandbox with network off (Codex workspace-write, Cursor
  Auto-review) these commands need approval; the MCP equivalents
  (`storyboard__read_investigation_events` / `storyboard__post_investigation_event`)
  avoid that when no local evidence promotion is needed. Fix the stale stub help in
  `adapters/claude/bin/cardinal-storyboard:89, 918-925`.
- A5. Owner-input additions per §2 R2–R4 and §4. `_owner_input.py` becomes a thin
  Claude wrapper.
- A6. Tests, all passing:
  - Existing tests, unmodified except where text intentionally changed:
    `adapters/claude/tests/{test_owner_input_hook.py, test_investigation_events_hook.py,
    test_unconnected_mode.py}`, `core/tests/{test_owner_input.py,
    test_delegated_supervision.py, test_investigation_events.py, test_storyboard_agent.py}`
  - New: `core/tests/test_investigation_text.py` and `test_investigation_turn.py`. These
    include per-runtime contract tests: Gemini/Cursor never return `turn_end_block`;
    Cursor capture requires consent; `agent_id` refuses capture; Gemini normalization
    strips hook_context and the referenced-files section; same-sha dedupe.
  - `python3 build/sync_skills.py` then `--check`.
  - **Done** when the tests pass and §4 is updated with the final signatures.

### Lane E — storyboard authoring + canvas (on Lane A's head)

Owns:
- `core/cardinal_core/{storyboard_authoring.py (new), storyboard_hero.py, evidence.py,
  storyboard_discovery.py}`
- `common/storyboard/scripts/render_preview.py`, `common/canvas/` (new),
  `build/sync_skills.py`
- `adapters/claude/skills/canvas/`
- `adapters/claude/hooks/{storyboard-preview.py, storyboard-hero.py, storyboard-token.py,
  storyboard-edit-lookup.py, storyboard-discovery.py, _storyboard_unfurl.py,
  _stray_key.py}`
- the import line in `storyboard-session.py`

Work:
- E1. One renderer.
  - Delete the fork `adapters/claude/skills/canvas/scripts/render_preview.py`; common
    already has everything it has.
  - The Claude hook calls `skills/storyboard/scripts/render_preview.py --runtime claude`.
  - Update `test_storyboard_preview.py` (fork path at :36; `--runtime` at :941;
    `CanvasSkillTests` :1091-1120 now point at `skills/storyboard/scripts/`).
  - Fix the `~/.claude` wording in common (:45, :340, :355-377, :1433) so it is
    runtime-keyed.
- E2. Build `storyboard_authoring` per §4.
  - `on_publish` is a real extraction from Claude's 181-line `storyboard-hero.py`.
  - `is_cardinal_tool` handles each runtime's MCP naming (Gemini `mcp_<server>_<tool>`,
    W4:265051).
  - `_storyboard_unfurl` moves into core.
  - The Claude hooks become thin wrappers.
- E3. canvas → `common/canvas/{CORE.md, exemplars/}`. Register
  `"canvas": ["CORE.md", "exemplars"]` in `SKILLS` (`build/sync_skills.py:35-38`).
  Per-adapter SKILL.md files carry the runtime-specific wording. This lane adds the
  Claude SKILL.md only; Phase 2 lanes add theirs.
- E4. Tests, all passing:
  - `adapters/claude/tests/{test_storyboard_preview.py, test_storyboard_unfurl.py,
    test_storyboard_hero_hook.py, test_storyboard_token.py,
    test_storyboard_edit_lookup.py, test_storyboard_discovery_hook.py,
    test_storyboard_skill.py (canvas assertions :100-105)}`
  - `core/tests/test_storyboard_hero.py`, plus a new `core/tests/test_storyboard_authoring.py`
  - `sync_skills.py --check`
  - **Done** when the tests pass and §4's authoring section is final.

### Lane B — Codex (`adapters/codex/` only)

- **Before enabling capture:** one live payload capture with
  `CARDINAL_CODEX_DEBUG_PAYLOADS=1` on the installed codex 0.160.1, covering a plain
  prompt, a custom `/prompts:` prompt, an `@file` mention, `/review`, `/compact` and a
  subagent turn. Record whether `prompt` is the typed text, and whether a subagent's
  `session_id` is the parent's. Put the findings in the PR description. If `prompt` is
  expanded, extend the normalization request back to the coordinator.
- **UserPromptSubmit:**
  - owner-input `capture(..., agent_id=payload.agent_id, deadline=START+3.5s)`, with the
    disclosure as `systemMessage` on the first prompt
  - `investigation_agent.prompt()` staged items into `additionalContext`
  - discovery re-look, always detached; its result is staged
  - the limits-gate verdict keeps priority: a hard global deadline of START+3.5 s
    (Codex discards stdout on timeout, `cardinal-codex-telemetry.py:39-41`).
- **PostToolUse:**
  - `deliver(..., is_subagent=bool(payload.agent_id))`
  - `on_preview` (separate hook entry, matcher `storyboard__preview`, long timeout)
  - `on_publish`, `observe_cardinal_result`
  - `edit_lookup` on `apply_patch`
- **Stop:** `turn_end(...)`. Blocking is allowed (R1); this adds the sync check and the
  owner-input flush.
- **SubagentStart:** register it in `scripts/cardinal-connect` for discovery.
- **SessionStart:** connect hint and stray-key warning.
- **Skills and agents:** canvas SKILL.md, plus brainstorm as a Codex custom agent
  (`[agents.brainstorm]` written by connect, or the plugin's mechanism if it has one).
- **Docs and tests:**
  - README: delete the "owner-input not wired" lines (:243, :255); document re-trusting
    modified hooks in the TUI, approval needs inside the sandbox, and capture semantics.
  - Flip `test_codex_investigation.py:62` and add owner-input / turn-end / authoring
    tests mirroring the Claude ones.
  - **Done** when the targeted codex tests pass and the live smoke test (§6) passes.

### Lane C — Gemini (`adapters/gemini/` only)

- **Verify first.** Find out how Gemini 0.50.0 actually loads the extension's skills and
  agents (`<ext>/skills`, `~/.gemini/skills`, `<ext>/agents`).
  `install_extension_bundle` (`scripts/cardinal-connect:214-246`) copies only GEMINI.md,
  the manifest and hooks, and **replaces** `mcpServers`. Fix it to copy `skills/` and
  `agents/` and to **merge** `mcpServers`.
- **SessionStart:** `investigation_agent.start`, poller spawn, `session_text` into
  `additionalContext`; connect hint and stray-key warning.
- **BeforeAgent:**
  - owner-input `capture(..., detached=True)`. Take the turn number under the lock
    synchronously and run the POST detached; there is no synchronous network call in
    BeforeAgent.
  - disclosure as `systemMessage`
  - `investigation_agent.prompt()` staged items into `additionalContext`
  - discovery re-look, detached and staged
- **AfterTool:**
  - `deliver` (Gemini has no subagent ids on tools; record that), `observe`
  - preview/publish/token/edit_lookup on `write_file|replace`
  - The preview entry gets a **distinct command** (`--event AfterTool --role preview`)
    and a timeout of about 150000 ms, because Gemini dedupes hooks by `name:command`.
    Change connect from one matcher per event (`EVENT_MATCHERS` :77, drift signature
    :95) to a list of groups.
- **AfterAgent:** register it, updating `REGISTERED_EVENTS` (:72-74) and
  `extension/hooks/hooks.json` together. Call `turn_end(...)`. **It never blocks** (R1):
  it flushes and stages only.
- **Skills and agents:** canvas, decision (status-only, matching Codex: the Gemini
  sandbox blocks `~/.gemini`), and brainstorm (`<ext>/agents`).
- **Limits to document:** discovery on subagent start (no event, W4:324340-324352);
  advisories that arrive after the turn's last tool call are delivered at the next
  BeforeAgent.
- **Tests and done:** tests mirror the Claude ones. **Done** when the targeted gemini
  tests and the live `gemini -p` smoke test (§6) pass.

### Lane D — Cursor (`adapters/cursor/` only)

- **sessionStart:** `investigation_agent.start`, poller, `session_text` into
  `additional_context`, connect hint and stray-key warning. Not available in cloud
  agents (documented).
- **postToolUse / postToolUseFailure:**
  - `deliver`, observe, `prompt()` staged items
  - authoring: preview, publish, token, and edit lookup via `afterFileEdit` → staged
- **Hook timeouts:** check Cursor's documented maximum and whether matchers work on
  postToolUse. 15 s is the plugin's own choice (`scripts/cardinal-connect:57`), not a
  platform limit. Raise it, or add a matcher-scoped entry for preview, if that's allowed.
  Use the detached-render-and-stage approach only if neither is.
- **beforeSubmitPrompt:**
  - owner-input `capture(...)`, only with connect-time consent (R3); no prompt is ever
    blocked
  - discovery re-look, detached and staged
- **`cardinal-connect`:** add `--owner-input on|off`, which prints the DISCLOSURE and
  sets versioned consent.
- **stop:** `turn_end(...)`, which flushes and stages and never uses `followup_message`
  (R1).
- **Skills:** connect, disconnect and status (SKILL.md over the existing scripts, with
  the Auto-review sandbox caveat), decision (status-only), canvas.
- **Brainstorm agent:** check whether Cursor supports agent definitions
  (`.cursor/agents`?). If it doesn't, record a limit with a citation.
- **Limits to document:**
  - per-session disclosure (R3)
  - advisories that arrive after the turn's last tool call wait for the next tool call
    or session
  - context stamping
  - cloud agents
- **No live test:** Cursor isn't installed on this machine. Test with fixtures in the
  existing `testdata` style and say so in the PR.
- **Done** when the targeted cursor tests pass.

### Lane F — Claude decision skill (`adapters/claude/skills/decision/`)

A decision skill for status and routing. Claude can run `bin/cardinal-decision` itself,
so the skill tells the agent how to report status and how to toggle capture when the
user asks. It mirrors `adapters/codex/skills/decision/SKILL.md`. Add a skills test.

### Lane G — Checks + `cardinal-behavior` on all runtimes (after Phase 2 merge)

Redesigned for sandboxed runtimes:
- Move the example ledger (`example set/approve/remove/expected`) and the range
  `status` / `watch` reads behind `cardinal-behavior` MCP tools. The stdio server runs
  outside the agent sandbox, so it can write `~/.cardinal/…` state. `cardinal-checks
  on|off` stays a command the user runs in their own terminal.
- `server.default_config` resolves the connection from `AgentPaths(runtime)`
  (`cardinal-secrets.json`), with env vars as an override.
- `RANGE_LISTEN` / `watch_command`: Claude keeps Monitor. Other runtimes poll with the
  new MCP status tool.
- `_checks.py` → core `checks_install` with a per-runtime state dir, and the
  `cardinal-checks` CLI → core `checks_cli.main(runtime)`. Each adapter gets a thin
  script.
- Gate: Claude keeps the Skill deny and `reloadSkills`. Elsewhere the skill is
  installed or removed at SessionStart and takes effect at the next session; document
  that as a limit (no skill reload, no skill-tool deny).
- Packaging and CI:
  - `build/release.py` copies `lib/behavior` + the checks template for all four
    (:97-101, `REQUIRED_ARTIFACTS` :45-54, `OPT_IN_SKILLS`)
  - the release-mirrors behavior suite runs for all adapters
  - `behavior.yml` path filters
  - `tests/test_behavior_release.py` builds all four
- MCP registration of `cardinal-behavior` in Codex `config.toml`, Cursor `mcp.json` and
  the Gemini extension (merged).
- Per-runtime checks SKILL.md files over a shared CORE.md.
- **Done:** `common/behavior/tests/*`, `adapters/claude/tests/test_checks.py`,
  `tests/test_behavior_release.py`, and the new per-adapter checks tests pass.

### Phase 3 — integration

- `docs/parity.json`: {feature: {adapter: "present" | "limit:<citation>" |
  "na:<reason>", "probe": {...}}}. Each feature gets one machine-checkable probe per
  adapter, of these kinds:
  - `hook`: an event plus a matcher regex in the adapter's registration (Claude
    hooks.json; Codex/Cursor/Gemini connect scripts; Gemini extension hooks.json)
  - `skill`: a dir under `adapters/<a>/skills`
  - `script`: a file under bin/ or scripts/
  - `mcp`: a server key in the MCP config writer
  - `grep`: a core call such as `owner_input.capture(` in the adapter's hook source
- `tests/test_parity_matrix.py` checks each "present" cell with its probe, and fails on
  a feature present on only some adapters unless the others are declared limit/na.
- `docs/parity.md` is the human-readable matrix with citations. Update all four READMEs.
- Version bumps (minor): claude 0.49.1→0.50.0, codex 0.27.2→0.28.0,
  cursor 0.22.2→0.23.0, gemini 0.21.2→0.22.0. Bump `gemini-extension.json` (0.1.0)
  only if Lane C finds that Gemini's install or update path reads it.

## 6. Verification

- Each lane runs its targeted tests (§5) and `sync_skills.py --check` where relevant.
- Live smoke tests, with a scratch HOME holding only a copy of the connection:
  - `codex exec` and `gemini -p`: check that the session's investigation has an
    `owner_input.recorded` with exactly the typed text; that the labeled links appear;
    that an advisory posted mid-turn is delivered at the next tool or prompt boundary;
    that the Gemini turn end never blocks; that a preview renders PNGs.
  - `claude -p`: regression check of the same flows.
- An independent review agent goes over the whole diff, then fixes, CI green, merge.
  The manifest bumps trigger `release-mirrors.yml` for all four. After the merge,
  confirm each mirror got a `vX.Y.Z` tag.

## 7. Risks

- Owner input from three more runtimes. Mitigated by R2–R4 and the gate tests; on
  Gemini and Codex, capture only after live payload verification.
- Hook timeouts (Codex 5 s prompt, Gemini ms, Cursor per-entry). All network work is
  detached or deadline-bounded, and the limits gate keeps priority.
- Claude refactor (A1): Claude's existing suites are the regression gate. No behavior
  change beyond the intended text changes.
- Scope: Lane G is the largest and newest. If it slips, Phase 3 still ships A–F, and G
  ships as a follow-up release. The owner hears that explicitly; it is not quietly
  relabeled as a limit.

## 8. v1 review resolution

| # | Finding | Resolution |
|---|---|---|
| 1 | A Gemini AfterAgent block becomes user/owner input | R1: Gemini never blocks; deliverables are staged |
| 2 | The Gemini BeforeAgent prompt is the expanded text | R2: `normalize_prompt`, skip when unsure, dedupe |
| 3 | Codex UserPromptSubmit fires in subagents | R2: `agent_id` refusal; Lane B live payload capture |
| 4 | Codex turn-end block is user-role | R1: allowed only when the text is hook-wrapped and the prompt hook doesn't re-fire; Codex qualifies, Gemini and Cursor don't |
| 5 | Phase 1 API too loose | §4 frozen API |
| 6 | A/E overlap | A then E, sequential; ownership lists; core frozen in Phase 2 |
| 7 | E1 done condition unmeetable | E1 edits the named tests; canvas sync spec |
| 8 | A3 already done | A3 reduced to wording and dedup |
| 9 | Claude keeps its own builder | A1 moves `live_line` and Stop into core |
| 10 | Codex subagent detection | `agent_id` guard; SubagentStart registered |
| 11 | Codex SessionStart systemMessage unproven | disclosure on the first prompt only |
| 12 | Codex prompt-hook budget | 3.5 s global deadline, detached re-look, re-trust note |
| 13 | Gemini hook dedupe and timeouts | distinct command per group; list-of-groups connect |
| 14 | Gemini manifest overwrite, skills not loaded | Lane C verifies loading, copies skills/agents, merges mcpServers |
| 15 | Checks not sandbox-compatible, not packaged | Lane G redesign (MCP-side ledger), packaging/CI, sequenced last |
| 16 | CLI needs network in sandboxes | A4 guidance plus README notes |
| 17 | Cursor disclosure (b) | (a) only: connect-time versioned consent (R3) |
| 18 | Cursor 15 s is self-imposed | Lane D checks the docs and raises the timeout before diverging |
| 19 | Invariant check | N/A (internal dev gate) |
| 20–23 | nits | folded into E2, Phase 3 probes, Lane C detached POST, A6 test lists |

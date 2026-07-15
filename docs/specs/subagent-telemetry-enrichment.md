# Subagent & turn telemetry enrichment — spec

Status: **implemented, reconstructed from shipped code** · Reconstructed
2026-07-15 (PLG.1, `docs/specs/toolkit-hive-mind.md` §W-plugins). This file
was a dangling reference (`adapters/claude/hooks/subagent-usage.py:21`,
`adapters/claude/hooks/turn-usage.py:26`, `adapters/codex/hooks/
cardinal-codex-telemetry.py:37`) — the enrichment it describes had already
shipped, but the doc was never written. Everything below documents
**behavior that exists today**; it is not a proposal. Every field is
grounded in a file:line citation. If a citation and the code disagree in a
future change, the code wins — update this doc, don't re-derive from it.

Cross-repo context: this doc is one input to `docs/specs/toolkit-hive-mind.md`
(the lakerunner identity extractor T1.2 depends on the capability-identity
fields documented in §5 below).

---

## 1. Scope

Two hooks on the Claude adapter originated the five numbered "Field"
sections cited by other adapters' code comments:

- `adapters/claude/hooks/subagent-usage.py` — one `cardinal.subagent_usage`
  record per completed `Agent`/`Task` tool call (§Field 1, §Field 5).
- `adapters/claude/hooks/turn-usage.py` — one `cardinal.turn_usage` +
  N `cardinal.turn_tool` records per completed user turn (§Field 2, §Field 3,
  §Field 4).

§Field numbers are a citation index, not a priority order — they're assigned
in the order the claude hooks introduced them and other adapters' code
comments point back at these same numbers (verified by
`grep -rn "§Field" --include="*.py" .`; see §7 for one comment that cites
this doc under a number that doesn't match its content).

Out of scope: the `target`-field privacy boundary on `cardinal.turn_tool`
(allowlisted file-path capture) is documented in `docs/specs/
per-turn-telemetry.md` per `turn-usage.py:24-25` — that file is *also*
missing from the checkout, but it is not part of PLG.1 and is not
reconstructed here.

---

## 2. §Field 1 — subagent token accounting

Source: `adapters/claude/hooks/subagent-usage.py:76-154` (`_sum_transcript_usage`),
`:222-253` (attribute assembly).

Claude Code reports all subagent activity inline under the parent
`session_id`, with no per-request marker the harness surfaces natively. This
hook sums the subagent's own transcript file
(`<transcript_dir>/<session_id>/subagents/agent-<agentId>.jsonl`) instead.

**Per-component split.** For every `assistant` message with a `usage`
object, `input_tokens` + `cache_creation_input_tokens` + `output_tokens` are
accumulated into `input`, `cache_creation`, `output` respectively
(subagent-usage.py:122-127). `total_tokens` (wire name) equals their sum —
the emitted `subagent_input_tokens` + `subagent_output_tokens` +
`subagent_cache_creation_tokens` sum exactly to `total_tokens` by
construction (subagent-usage.py:142-143, 227-232). This is a documented
downstream invariant, not incidental: "the three fields below sum exactly to
total_tokens — the downstream consistency check and the bimodal-Explore
signature both depend on it" (subagent-usage.py:227-229). `cache_read` tokens
are summed separately (`subagent_cache_read_tokens`) and are **not** part of
the sum (subagent-usage.py:128, 144, 225) — matching lakerunner's own
attribution choice to leave `cache_read` unattributed
(`toolkit-hive-mind.md` §1.2: "`cache_read` deliberately not attributed").

**Dominant model by worked tokens.** Each message's `model` accumulates
`input + cache_creation + output` into `model_worked[model]`
(subagent-usage.py:129-134). The emitted `model` is
`max(model_worked, key=model_worked.get)` — Python's `max` returns the first
maximum in iteration order, and dict iteration order is insertion order, so
ties break first-seen (subagent-usage.py:137-141). `subagent_model` (same
value) and `subagent_model_count` (distinct models seen) are also emitted for
back-compat (subagent-usage.py:234-241): "`model` is the cross-adapter
contract key (the mining harvester's clustering signal); `subagent_model`
(+`_count`) predate it and stay for downstream back-compat."

**Tool-name histogram — `subagent_tool_counts`.** Every `tool_use` block
name in the subagent's own assistant messages increments a `Counter`
(subagent-usage.py:109-117); the result is JSON-encoded onto
`subagent_tool_counts` (subagent-usage.py:243-251). **Claude-only today** —
no other adapter emits this field (verified: `grep -rn
"subagent_tool_counts" adapters/` matches only `adapters/claude/`). Capped at
`TOOL_COUNTS_CAP = 32` most-frequent names; if the cap trips,
`subagent_tool_counts_truncated="true"` is added (subagent-usage.py:57-59,
244-253).

**Footprint fields** (informational, not part of the worked-token
accounting): `final_context_tokens` (the harness's own `totalTokens` — the
final request's context footprint, a different quantity from cumulative
spend), `subagent_tool_use_count`, `subagent_duration_ms`
(subagent-usage.py:254-263).

---

## 3. §Field 2 — chunked / bounded emission

Source: `adapters/claude/hooks/turn-usage.py:47-52` (bounds),
`:124-245` (`_build_records`), `:304-315` (POST chunking).

`turn-usage.py` walks the transcript slice belonging to the just-completed
user turn and emits `cardinal.turn_usage` (one per model call) +
`cardinal.turn_tool` (one per tool-use block) records. Two independent
bounds:

- `MAX_RECORDS_PER_FIRING = 4096` — absolute ceiling per hook invocation,
  protecting the hook process from a pathological transcript
  (turn-usage.py:52, 150, 188). Past this, `truncated=true` is stamped onto
  the most recent `cardinal.turn_usage` record before the ceiling
  (turn-usage.py:233-240): "so the downstream consumer can fail loud rather
  than treat partial as complete."
- `BATCH_MAX_RECORDS = 256` — records are POSTed in slices of at most this
  size, in order; each POST is independently best-effort, so a failed batch
  drops only its own slice (turn-usage.py:51, 304-315).

Per-record `timeUnixNano` is offset by 1ns per record within a firing
(`now_ns + i`, turn-usage.py:292-301) — necessary because lakerunner's
`agent_session_events` primary key is `(organization_id, session_id,
chq_tsns)`; a shared timestamp across a firing's records would collapse them
to one row under `ON CONFLICT DO NOTHING` (turn-usage.py:284-291). The offset
index runs continuously across the ≤256-record batches of one firing (not
reset per batch), keeping the total spread within `MAX_RECORDS_PER_FIRING`
nanoseconds.

---

## 4. §Field 3 — `user_turn_seq`

Source: `adapters/claude/hooks/turn-usage.py:69-121` (turn-boundary
detection), `:159-163` (emission).

A "real" user message (string content, or list content containing a
non-`tool_result` block) marks a turn boundary; a `tool_result`-only user
message is loop continuation, not a boundary (`_is_real_user_message`,
turn-usage.py:69-83). `_walk_current_turn` streams the transcript forward,
resetting `current_turn` and incrementing `user_turn_seq` at every boundary,
returning only the records since the last boundary plus the 1-based ordinal
of that turn within the session (turn-usage.py:94-121).

`user_turn_seq=0` is reserved to mean "boundary never seen" (first turn, or
a truncated/rotated transcript where the walk found no prior boundary) — the
attribute is **omitted** rather than emitting a guessed `0`
(turn-usage.py:159-161, 199): "`user_turn_seq=0` means the boundary was
never seen ... omit rather than guess an ordinal." This makes `user_turn_seq`
optional on the wire even though it's logically always present once a
session has completed one real turn.

Both `cardinal.turn_usage` and `cardinal.turn_tool` carry `user_turn_seq`;
`cardinal.turn_tool` additionally carries `turn_seq` (the model-call index
*within* this firing) and `tool_seq` (the tool-use index within that model
call) — together `(user_turn_seq, turn_seq, tool_seq)` totally orders a
session's tool stream (turn-usage.py:135, 162, 182, 200-201, 224).

---

## 5. §Field 4 — `bash_class` closed enum

Source: `core/cardinal_core/bashclass.py` (shared implementation, vendored
into every adapter's `hooks/cardinal_core/`), invoked from
`turn-usage.py:207-221`.

Only the Bash tool's leading command word (or subcommand word, for
multiplexers like `git`/`npm`/`cargo`) ever feeds classification — arguments
are never consulted, and the command string itself never leaves the hook
process (bashclass.py:1-5, 108-118). `classify_bash_command` tokenizes on
shell separators (`&&`, `||`, `;`, `|`, newline), classifies each segment,
and resolves compound commands to the most write-risky class present
(`BASH_CLASS_RANK`, bashclass.py:16-26, 137-140), emitting `bash_multi=true`
when segments span more than one class. Closed enum:
`file-write, git-write, pkg, network, build, test, git-read, file-read,
other` (bashclass.py:16-26). This is the single copy — "per-plugin fixture
parity is enforced by the cross-adapter contract test" (bashclass.py:7-8).

`turn-usage.py:207-221` applies this only to `tool_name == "Bash"`, adding
`bash_class` (+ `bash_multi` when true) to that `cardinal.turn_tool` record;
the raw command text stays out of the OTLP payload entirely (comment:
"Closed-enum verb class only (spec §Field 4); the command string never
leaves this process," turn-usage.py:208-209).

---

## 6. §Field 5 — `subagent_description` privacy boundary

Source: `adapters/claude/hooks/subagent-usage.py:61-64` (cap),
`:213-221` (boundary + emission).

`subagent_description` carries **only** the orchestrator's short task label
for the spawn — the `Agent`/`Task` tool call's `description` argument,
verbatim but hard-truncated (no ellipsis marker) at `DESCRIPTION_CAP = 160`
chars (subagent-usage.py:61-64, 219-221). It is explicitly **not** tool
content: "prompts, tool arguments, and tool results remain never-captured"
(subagent-usage.py:213-218). The field is omitted entirely when absent,
empty, or non-string (subagent-usage.py:220).

Every other adapter that emits `cardinal.subagent_usage` replicates this
same boundary and the same 160-char cap — see §6 in the per-adapter table
below for citations. `codex-telemetry.py:546-568`
(`subagent_description_from_payload`) states the parity explicitly: "parity
with cardinal-claude-plugin v0.12.1's `subagent_description`."

This is the sharpest instance of the repo-wide privacy invariant restated in
`toolkit-hive-mind.md` §5: "Telemetry carries capability *names + counts +
tokens*, never prompts/args/results."

---

## 7. Per-adapter capability-identity emission

The lakerunner identity extractor (`toolkit-hive-mind.md` T1.2) keys on
three identity surfaces: MCP tool identity on `cardinal.turn_tool`, subagent
identity on `cardinal.subagent_usage`, and command identity on
`cardinal.git_state`. Verified by reading each adapter's hooks directly (not
assumed from one adapter's shape).

### 7.1 `cardinal.turn_tool` — `tool_name` (+ MCP split)

`tool_name` is universal — every adapter emits it on every `cardinal.turn_tool`
record (REQUIRED_KEYS already enforces this, `tests/test_contract.py:73`).
Whether the qualified MCP tool name is additionally **split** into
`mcp_server_name` + `mcp_tool_name` diverges:

| adapter | splits MCP? | citation |
|---|---|---|
| claude | **No** — raw qualified name only, in `tool_name` | `adapters/claude/hooks/turn-usage.py:192-203` has no `mcp__` branch; `tool_name` is `block.get("name")` verbatim |
| codex | Yes | `adapters/codex/hooks/cardinal-codex-telemetry.py:204-208` (`normalize_tool_name`), `:373-380` (emission: `tool_name` keeps the raw qualified name, `mcp_server_name`/`mcp_tool_name` added alongside) |
| cursor | Yes | `adapters/cursor/hooks/cardinal-cursor-telemetry.py:207-216` (`_mcp_split`), `:432-437` |
| gemini | Yes | `adapters/gemini/hooks/cardinal-gemini-telemetry.py:298-308` (`normalize_tool`), `:342-347` |
| omnigent | Yes | `adapters/omnigent/cardinal_omnigent/telemetry.py:399-406` (`_split_mcp`), `:450-452` |

Claude's non-split is a **deliberate, existing asymmetry, not a gap**: the
lakerunner extractor's claude branch (`toolkitKey`, `processor.go:341-376`)
already reads `mcp_server_name` off Claude Code's own *native* OTel
`tool_result`/`tool_parameters` event — an event this repo does not emit
(claude's native harness does; see `tests/test_contract.py:17-18`: "claude:
no tool_result / api_request (Claude Code's native OTel emits them)"). The
`toolkit-hive-mind.md` T1.2 feasibility table marks claude's `mcp` cell
`YES` for exactly this reason. `cardinal.turn_tool`'s raw name is
supplementary telemetry, not the extractor's only source for claude. No
emit was added here — adding a redundant split to claude's hook would not
close a real gap and was avoided per "never fabricate an emit."

The other four adapters emit **both** the raw qualified name (on `tool_name`)
and the split fields, so the harvester keeps its strongest clustering signal
(the raw name) alongside the two fields lakerunner's non-claude extractors
will parse directly (codex-telemetry.py:373-378, gemini-telemetry.py:342-345
make this "keep both" tradeoff explicit in comments).

### 7.2 `cardinal.subagent_usage` — subagent identity

| adapter | `subagent_type` | `agent_id` | `subagent_description` | `model` | per-component split / `subagent_tool_counts` |
|---|---|---|---|---|---|
| claude | Yes — `subagent-usage.py:192-196,210` | Yes (when present) — `:197,211` | Yes — `:219-221` (§Field 5) | Yes (dominant by worked tokens) — `:234-242` | **Yes** — the only adapter with either (§Field 1) |
| codex | Yes (probed) — `cardinal-codex-telemetry.py:588` | Yes (probed) — `:589` | Yes — `:590`, `subagent_description_from_payload` `:546-568` | Yes (probed) — `:593` | No — `total_tokens` only, `:594` |
| cursor | Yes (probed, no `agent_id`) — `cardinal-cursor-telemetry.py:569` | **No** — grep-empty for `agent_id` in this file | Yes — `:570`, `subagent_description_from_payload` `:545-555` | Yes (probed) — `:574` | No — and **no `total_tokens` either** (Cursor's documented `subagentStop` payload has no token field at all — "gap D"; only `duration_ms`/`message_count`/`tool_call_count`/`loop_count` are emitted, `:559-561,575-579`) |
| gemini | Yes (probed) — `cardinal-gemini-telemetry.py:444-450` | Yes (probed) — `:451` | Yes — `:452`, `subagent_description_from_payload` `:395-414` | Yes (probed) — `:455-460` | No — `total_tokens` only, `:431-439,461` |
| omnigent | **No** — no type-taxonomy field exists in the contract | Not by that name — carries `subagent_id`, a thread/conversation id (`telemetry.py:543-544`), semantically closer to Claude's `agent_id` than to `subagent_type` | Yes — `telemetry.py:505-514,532` (`_subagent_description`; preference order: `cardinal.subagent` label → codex nickname/role/prompt → wrapper marker) | Yes (engine-injected `context.model`) — `:535` | No — `input_tokens`/`output_tokens`/`total_tokens` are cumulative-to-date (`usage_scope="session_cumulative"`, `:536-538,542`), not per-component; no tool_counts |

`subagent_type`/`agent_id`/`model` are **probed** (multiple candidate
payload keys tried in sequence) on codex/cursor/gemini because none of the
three harnesses' native subagent-stop payload shape has been directly
observed — codex documents this explicitly ("Codex's SubagentStop payload
shape has never been observed in the wild," `cardinal-codex-telemetry.py:38-39`)
and ships an env-gated capture affordance,
`CARDINAL_CODEX_DEBUG_PAYLOADS=1` → `dump_debug_payload`
(`cardinal-codex-telemetry.py:41,117-128,574`). **Gemini ships the identical
affordance** — `CARDINAL_GEMINI_DEBUG_PAYLOADS`
(`cardinal-gemini-telemetry.py:48`, `dump_debug_payload:100-110`, called
from `handle_after_agent:418`) — and already probes the same four identity
fields (`subagent_type`/`agent_id`/`subagent_description`/`model`) plus
`total_tokens`/`duration_ms`/`status`. **This supersedes
`toolkit-hive-mind.md`'s T1.2 feasibility table, which marks gemini's
`subagent` cell "PARTIAL (payload names unobserved)"** (line 223 as of
2026-07-15) — the field coverage is not partial; only the real-world payload
key names are still unconfirmed, which the debug-capture affordance exists
to resolve. No code change was needed here; this doc corrects the stale
characterization for whoever reads T1.2 next.

Omnigent's absence of `subagent_type` is a **structural** difference, not a
missing probe: omnigent's engine has no native "subagent type" taxonomy to
read — a spawned child is identified by labels (`cardinal.subagent`, codex
nickname/role, or a wrapper marker), which is exactly what
`subagent_description` already carries (`telemetry.py:505-514`). Synthesizing
a `subagent_type` value by copying `subagent_description` into a
differently-named field would not add information and would misrepresent
the two fields' distinct semantics (a free-text label vs. a closed-ish
type). No emit was added for this reason — see §8 for how the shared
contract test treats this instead of forcing a fabricated field.

### 7.3 `cardinal.git_state` — `cardinal_command`

All five adapters emit `cardinal_command`, sourced from the same shared
`cardinal_core.initiative.detect_command(prompt)` (`core/cardinal_core/
initiative.py:95-106`): matches a leading `/command-name` typed form or an
expanded `<command-name>` tag, returns `None` (and the attribute is then
dropped by `otlp.log_record`'s None-filtering, `core/cardinal_core/
otlp.py:70`) when neither matches.

| adapter | citation |
|---|---|
| claude | `adapters/claude/hooks/git-state.py:118,143` — comment: "closes the user-typed-skill gap in the native telemetry" |
| codex | `adapters/codex/hooks/cardinal-codex-telemetry.py:167-169` |
| cursor | `adapters/cursor/hooks/cardinal-cursor-telemetry.py:371` |
| gemini | `adapters/gemini/hooks/cardinal-gemini-telemetry.py:156` |
| omnigent | `adapters/omnigent/cardinal_omnigent/telemetry.py:212`, tested at `adapters/omnigent/tests/test_omnigent.py:123-127` |

Because `detect_command` returns `None` for prompts with no leading slash
command, `cardinal_command` is present only on the subset of `cardinal.git_state`
records where a command actually fired — it is legitimately absent from most
records, not a bug (this is why `tests/test_contract.py:178-194`'s
initiative-key test treats it as allowed-optional rather than required on
every record; §8 below adds a *presence-somewhere* check, not a per-record
one).

### 7.4 No skill/slash-command identity outside `cardinal_command`

By design, cursor, gemini, and omnigent have no "skill" concept and emit no
skill-identity event or field — confirmed by grep-empty for `skill` (cursor,
gemini) and by the omnigent event inventory in §7.5 (no skill-shaped event).
`toolkit-hive-mind.md` §1.1 already states this ("skill is Claude-mostly");
this doc adds the verification. Per the parent spec's scope decision
(T1.2), this is absent **by design**, not a gap to close — "do NOT invent
skill events for runtimes that have no skills."

### 7.5 Omnigent's event inventory (for context)

Omnigent emits six OTLP event types from `telemetry.py`, reusing the exact
`cardinal.*` / bare-name convention and underscore-spelled attribute keys
the other four adapters converge on post-normalization: `cardinal.git_state`
(`:216`), `api_request` (`:360`), `cardinal.turn_usage` (`:361`),
`cardinal.turn_tool` (`:463`), `tool_result` (`:498`),
`cardinal.subagent_usage` (`:547-548`). No `cardinal.plan_state`,
`cardinal.plan_usage`, `cardinal.turn_thought`, or `cardinal.turn_response` —
omnigent has no plan/rate-limit surface and no separate thought/response
capture. See §8 for why this adapter is not (yet) added to the shared
contract test's `ADAPTERS` tuple despite the key-shape compatibility.

---

## 8. Contract-test coverage and the omnigent divergence

`tests/test_contract.py` is the executable form of §7's per-adapter table
for four of five adapters (`ADAPTERS = ("claude", "codex", "cursor",
"gemini")`). This PLG.1 change adds capability-identity assertions scoped to
the adapters that actually emit each field (`MCP_SPLIT_ADAPTERS`,
`SUBAGENT_TYPE_ADAPTERS`, `COMMAND_IDENTITY_ADAPTERS` in
`tests/test_contract.py`) rather than widening the universal `REQUIRED_KEYS`
set — `REQUIRED_KEYS` is imported verbatim by
`adapters/omnigent/tests/test_omnigent.py` (`CONTRACT.REQUIRED_KEYS`,
`test_omnigent.py:36-38`), so growing it with a field omnigent (or claude,
for the MCP split) doesn't emit would turn an intentional per-adapter
asymmetry into a false failure in a suite that already passes.

**Omnigent was evaluated for inclusion in `ADAPTERS` and does not qualify
yet — this is an infrastructure gap, not a shape mismatch:**

1. **Attribute-key shape is already compatible.** Omnigent's emitted keys
   are underscore-spelled at the source (`cardinal_repo`, `tool_name`,
   `turn_seq`, `subagent_description`, …) — the exact post-normalization
   form `test_contract.py`'s `_normalize_key` produces from claude's dotted
   keys. No adapter-side change would be needed to satisfy
   `REQUIRED_KEYS` for `cardinal.turn_tool`, `cardinal.turn_usage`,
   `api_request`, `tool_result`, and `cardinal.subagent_usage` — each is
   satisfied today (verified against `tests/test_omnigent.py`'s live
   assertions, e.g. `:238,145,137,256,362`).
2. **`cardinal.git_state` legitimately fails 3 of the 6 required keys** —
   `cardinal_head_sha`, `cardinal_cwd`, `cardinal_remote_url`. Omnigent
   policies never see the workspace (`telemetry.py` module docstring,
   `docs/specs/omnigent-adapter.md` §Verified integration facts: no cwd,
   repo, or branch anywhere in the policy contract); `test_omnigent.py:40-42`
   names this `WORKSPACE_KEYS` and subtracts it explicitly. This is a
   structural, permanent divergence — not something PLG.1 can close with an
   emit, since the data doesn't exist on omnigent's wire.
3. **`cardinal.subagent_usage` has no `subagent_type`** (§7.2) — the new
   `SUBAGENT_TYPE_ADAPTERS` check in `tests/test_contract.py` would fail for
   omnigent if it were included, for the structural reason given in §7.2.
4. **No committed golden fixtures exist.** `tests/test_contract.py`'s
   `load_adapter_events` globs `adapters/<adapter>/tests/goldens/*.json`
   (`test_contract.py:120-121`); `adapters/omnigent/tests/` has no
   `goldens/` directory — `test_omnigent.py` instead drives
   `telemetry.telemetry_policy(event, config)` directly against a
   `StubIngest` capture per test (`test_omnigent.py:81-94`,
   `fixtures.load_stub_ingest`). Adding `"omnigent"` to `ADAPTERS` today
   would fail immediately at `setUpClass` ("no golden records found for
   adapter 'omnigent'") before any key-level assertion even runs — this is
   the actual blocker, ahead of items 2–3.

Per PLG.1's rule against forcing a broken/red test, omnigent is **not**
added to `ADAPTERS`. It keeps its own, already-thorough suite
(`adapters/omnigent/tests/test_omnigent.py`), which already asserts the
capability-identity fields that do apply to it: MCP split on
`cardinal.turn_tool` (`test_mcp_tool_call_keeps_qualified_name`,
`:243-249`), `subagent_description` + `model` on `cardinal.subagent_usage`
(`test_codex_native_child_emits_subagent_usage`, `:347-370`), and
`cardinal_command` on `cardinal.git_state`
(`test_slash_command_detected_from_prompt`, `:123-127`). Closing items 2–4
above (a `goldens/` fixture set, a `subagent_type`-equivalent taxonomy, or a
server-side-workspace substitute) is future work if omnigent needs to join
the shared gate — none of the three is a PLG.1-sized change.

---

## 9. A citation inconsistency, noted rather than silently resolved

`adapters/codex/hooks/cardinal-codex-telemetry.py:37` cites this doc as
"subagent-telemetry-enrichment field 4, step 1" for the `CARDINAL_CODEX_
DEBUG_PAYLOADS` capture affordance (`:38-41`). That affordance captures
`SubagentStop` payload shape — i.e., it's about subagent identity
(§Field 1 / §7.2 above), not about `bash_class` (§Field 4, §5 above), which
is what every other "§Field 4" citation in this codebase refers to
(`turn-usage.py:208`, `test_turn_usage.py:539`). The two can't both be
"Field 4" under one consistent numbering. Since the doc never existed for
the codex author to check against, this reads as a citation slip rather
than a real second numbering scheme — nothing else in the codebase treats
"field 4" as meaning subagent-payload capture. Left as-is (out of scope to
edit code comments for PLG.1's doc-and-test mandate); flagging here so a
future edit near that line fixes the citation to "§Field 1" instead of
silently perpetuating it.

---

## 10. Definition of done (this doc)

Satisfied when: every §Field and every per-adapter emission claim above
cites a real, current file:line; `tests/test_contract.py` enforces the
capability-identity fields in §7.1–7.3 for the adapters that emit them; and
the omnigent divergence in §8 is precise enough that a future contributor
could close it without re-deriving the investigation.

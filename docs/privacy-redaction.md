# Privacy & Redaction — Agent Execution Graph (Phase 0.F)

Status: spec, not discussion. Governs what adapter-side envelope emitters
(`node_observed`, `node_updated`, `edge_observed`, `execution_event`,
`usage_observed`, `artifact_link_observed` — `core/cardinal_core/envelope.py`)
may put on the wire. Applies to every adapter: Claude, Codex, Cursor, Gemini,
Omnigent.

Baseline for this spec is the current hook emitters (read in full for this
doc): `adapters/claude/hooks/{subagent-usage,turn-usage,git-state}.py`,
`adapters/codex/hooks/cardinal-codex-telemetry.py`,
`adapters/cursor/hooks/cardinal-cursor-telemetry.py`,
`adapters/gemini/hooks/cardinal-gemini-telemetry.py`,
`core/cardinal_core/otlp.py`. The envelope model must not regress it, and
should fix the gaps this review found (§7).

No fixtures existed under `fixtures/` at the time this doc was written (P0.D
runs in parallel with this doc); classifications below are derived from the
real payload shapes already handled by the hooks above, which is the same
surface the fixture captures will exercise.

## 1. Threat model

The adversary is not a nation-state or an endpoint compromise — those are out
of scope. The realistic risks are: (a) accidental disclosure to Cardinal's
shared multi-tenant backend (a bug in tenant scoping, an overly broad query,
a support engineer debugging a ticket); (b) leakage through infrastructure
logs that sit outside product access controls (ingest gateway access logs,
OTLP collector debug dumps, error-tracking breadcrumbs); and (c) exposure on
a Cardinal dashboard to teammates or managers who have org-level access but
no business reason to read a colleague's prompts, file contents, or command
output. The design goal is that a payload leaked through any of these paths
is unembarrassing and non-exploitable — it should tell you *what kind of
thing* happened (a Bash call, an Edit, a subagent spawn) and *how much*
(bytes, tokens, duration), never *the content*.

## 2. Data categories

| Category | Examples | Classification | Why |
|---|---|---|---|
| Session metadata | `session_id`, `user_email`, `repo`, `branch`, `cwd`, `head_sha` | **verbatim** | Required for identity (§ Canonical model) and attribution; low content-sensitivity — these are facts about *where* work happened, not *what* was said or done. |
| Git remote URL | `remote_url` | **verbatim, userinfo-stripped** | Needed to resolve canonical repo, but remote URLs can embed `https://user:TOKEN@host/...` credentials — see §3. |
| Prompts | user prompt text, system prompt fragments, skill instruction bodies | **never** (hash + length only) | Highest-sensitivity category — arbitrary pasted content, proprietary business context, PII, sometimes credentials pasted inline. |
| Short task labels | subagent spawn `description`, slash-command *name* (not args) | **verbatim, capped** | Deliberate narrow exception, already shipped (Claude v0.12.1, mirrored in Codex/Cursor/Gemini): a 3–5 word orchestrator-authored label, hard-capped at 160 chars. Not tool content. See §3 for the boundary. |
| Tool arguments | Bash commands, Edit/Write file content or patches, SQL query text, MCP tool call args, grep/glob patterns | **never** (hashed/classified only) | Same risk profile as prompts — commands and edits routinely carry secrets, internal hostnames, proprietary logic. |
| Tool results | stdout/stderr, file contents read, MCP tool responses | **never** (length + exit_code + optional hash) | Highest-volume category and the easiest way to accidentally exfiltrate an env dump, a `.env` file, or a config secret. |
| File diffs | added/removed line ranges, diff hunks | **never** content; **verbatim** counts only | Line counts are useful signal (churn, size) with zero content risk. |
| Model responses / reasoning | assistant text, thinking/reasoning blocks | **never** (length only) | Matches Cursor's existing `turn_response`/`turn_thought` pattern — keep it as the cross-adapter default, not a Cursor-only quirk. |
| Token counts / cost | `input_tokens`, `output_tokens`, `cost_usd`, etc. | **verbatim** | Pure numerics, no content risk, required for the product's core metering. |
| Timing | `start_ns`, `end_ns`, `duration_ms` | **verbatim** | Pure numerics. |
| Tool / skill / subagent / node names | `node_name` (e.g. `"Edit"`, `"brainstorm"`, `"cardinal:optimize-toolkit"`), `mcp_server_name`, `mcp_tool_name` | **verbatim** | Closed-ish vocabulary, essential for product classification. A name is safer than content — err toward keeping names, restricting everything hung off them. |
| File paths | `attributes.file_path`, Bash/Edit `target` | **verbatim if inside cwd, hashed if outside** | See §3 `redact_file_path`. |
| Identifiers | `tool_use.id`, `call_id`, `agent_id`, node-key seeds | **verbatim** | Opaque, required for identity/parentage; carry no content. |
| Secrets / credentials | API keys, tokens, `.env` assignments, JWTs | **never** | Scrub-then-drop; see §3 and §4. If detected inside any other field, that field is dropped, not merely hashed (a hash of a secret is still tied 1:1 to the secret and is not a safe thing to store or compare against leaked-credential databases). |

## 3. Per-field rules

Rules below name the target envelope attribute, then the current
hook-emitted field(s) it supersedes/matches where applicable.

- **`node_name`** (e.g. `"Edit"`, `"Bash"`, `"brainstorm"`, `"pr-review-toolkit"`)
  — verbatim. Matches every adapter's existing `tool_name`/`tool_type`.

- **`attributes.command`** (Bash/shell tool nodes) — never verbatim. Emit:
  - `bash_class` — closed enum from `cardinal_core.bashclass.classify_bash_command` (already the rule in Claude's `turn-usage.py` and mirrored in Codex/Cursor/Gemini's `turn_tool`).
  - `bash_multi` — bool, already emitted.
  - `command_hash` — `hash_field(full_command)` (§4), for exact-match dedup/clustering without recovering the command text.
  - **Regression to fix**: Codex/Cursor/Gemini's `tool_result` event currently emits the *raw* command string verbatim inside `tool_input` JSON (see §7). That path must be redirected through `redact_command` before it reaches the envelope.

- **`attributes.file_path`** — `redact_file_path(path, cwd)` (§4): verbatim
  (path relative to `cwd`) when the path resolves inside the session's `cwd`;
  hashed placeholder when it resolves outside. Rationale: a path inside the
  repo the team already has access to reveals nothing a teammate with repo
  access doesn't already see, and file-path-shaped signal (which files
  changed) is core product value. A path outside `cwd` can leak home
  directory usernames, unrelated project names, or system paths (`/etc/…`,
  `~/.ssh/…`) and buys little product value, so it's hashed.

- **`attributes.prompt`** (user prompt, system prompt fragment, skill body)
  — never verbatim. Emit `hash_field(prompt)` → `{hash, length, truncated}`
  only. No adapter today emits full prompt text on any hook — keep it that
  way; this rule exists so a future emitter doesn't regress it.

- **`attributes.diff_summary`** — `added_lines`/`removed_lines`/`files_changed`
  counts only, never hunk content. No current hook emits diffs at all; this
  is new surface for the envelope model and starts at the strictest setting.

- **Tool result stdout/stderr** — never verbatim. Emit `length`, `exit_code`
  (or `success` bool), and optionally `hash_field(stdout[:4096])` for
  dedup/clustering. **Regression to fix**: Cursor's `output_success` and
  Gemini/Codex's exit-code scraping already read stdout text internally but
  correctly stop short of emitting it — keep that discipline, and make it
  the documented rule rather than an emergent property of three separate
  implementations.

- **MCP tool call arguments and responses** — never verbatim. Args: hash +
  length (same treatment as Bash args generally, no special-case). Responses:
  `hash_field` of a size-capped prefix + length. MCP tool *names*
  (`mcp_server_name`, `mcp_tool_name`) stay verbatim (§2).

- **`user.email`** — verbatim. Already emitted on every adapter's resource
  attributes (`otlp.resource_attrs`); required for attribution and this spec
  does not change that.

- **Slash-command / skill name** (`cardinal.command` / `cardinal_command`)
  — verbatim, name only. Already correctly scoped in every adapter
  (`detect_command` returns the command name, never the prompt args that
  followed it) — matches, no change needed.

- **`subagent_description`** — verbatim, hard-capped at 160 chars. This is
  the one place free text crosses the boundary today, and it's a
  consciously scoped exception (see the `PRIVACY BOUNDARY` comment in
  `subagent-usage.py`): it is the orchestrator's short task label passed as
  a tool argument to the spawn call, not tool content, not a prompt, not a
  result. Keep the cap; do not widen the source field it's read from.

- **Secrets** (env var values, API keys, tokens shaped like known patterns)
  — `scrub_secrets()` runs **before** any hashing or truncation, on every
  string-valued field above that isn't already a closed enum or a name. If a
  known secret pattern matches:
  1. The field is **dropped entirely** (not hashed — see §2 rationale).
  2. Emit `execution_event(event_kind='secret_detected')` with the pattern
     name(s) matched (not the matched text) and the field name it was found
     in, so a human can go audit locally without the secret ever leaving
     the machine.

## 4. Redaction helpers — `core/cardinal_core/redaction.py`

Signatures only; implementation is a separate ticket. All helpers are pure,
side-effect-free, and safe to call on every string-valued attribute before
it's added to an envelope record.

```python
def hash_field(value: str, max_bytes: int = 4096) -> dict:
    """SHA-256 over the first `max_bytes` of `value` (UTF-8 encoded).

    Returns {"hash": "<hex sha256>", "length": <int, full byte length of
    value>, "truncated": <bool, True if value exceeded max_bytes>}.

    `length` is always the FULL length, not the hashed-prefix length —
    truncation must not hide how big the original value was.
    """


def scrub_secrets(value: str) -> tuple[str, list[str]]:
    """Replace every substring matching a known secret pattern (§ secret
    regex list below) with a fixed placeholder (e.g. "<redacted:AWS_KEY>").

    Returns (cleaned_value, detected_pattern_names) — detected_pattern_names
    is the list of pattern names matched (possibly empty), always returned
    even when the caller intends to drop the field outright, so the caller
    can still emit execution_event(event_kind='secret_detected').
    """


def redact_command(cmd: str) -> dict:
    """Bash/shell-specific redaction. Wraps classify_bash_command (already
    in cardinal_core.bashclass) + scrub_secrets + hash_field.

    Returns {"bash_class": <enum|None>, "bash_multi": <bool>,
    "command_hash": <hash_field() dict>, "secret_patterns": <list[str]>}.
    Never returns the command text itself in any field.
    """


def redact_file_path(path: str, cwd: str) -> str:
    """Path-scoping redaction (§3 attributes.file_path).

    If `path` resolves (after normalization) inside `cwd`: return it
    relative to `cwd`, verbatim.
    If `path` resolves outside `cwd`, or `cwd` is unknown/unavailable:
    return a hashed placeholder, e.g. f"outside-cwd:{sha256(path)[:16]}" —
    stable across calls with the same path+cwd (so the same excluded file
    touched twice clusters as "same file", without revealing where it is).
    """
```

### Common secret regex list

Names below are the `secret_patterns` values `scrub_secrets`/`redact_command`
report on detection (never the matched text itself):

| Pattern name | Shape | Example (illustrative, not real) |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | `AKIA[0-9A-Z]{16}` | `AKIAIOSFODNN7EXAMPLE` |
| `AWS_SECRET_ACCESS_KEY` | 40-char base64-ish string immediately following an `aws_secret_access_key`-shaped key name | — (heuristic; see Open Questions) |
| `GITHUB_PAT` | `gh[pousr]_[A-Za-z0-9]{36,}` or `github_pat_[A-Za-z0-9_]{22,}` | `ghp_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX` |
| `SLACK_TOKEN` | `xox[baprs]-[A-Za-z0-9-]{10,}` | `xoxb-XXXXXXXXXXXX-...` |
| `GENERIC_ENV_SECRET_ASSIGNMENT` | `[A-Z_][A-Z0-9_]*_(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S+` | `DATABASE_PASSWORD=hunter2` |
| `JWT_LIKE` | `eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+` | `eyJhbGciOi....` |
| `PRIVATE_KEY_BLOCK` | `-----BEGIN [A-Z ]*PRIVATE KEY-----` | — |
| `BEARER_TOKEN` | `[Bb]earer\s+[A-Za-z0-9._-]{20,}` | `Bearer eyJ...` |
| `REMOTE_URL_USERINFO` | `://[^/@\s]+:[^/@\s]+@` (matches credential-embedded URLs) | `https://x-access-token:ghp_xxx@github.com/...` |

This list is deliberately conservative and pattern-based (no entropy
scanning in v1 — see Open Questions).

## 5. Configuration knobs

- **`CARDINAL_REDACT_MODE`** ∈ `{strict, standard, permissive}`, default
  `standard`.
  - `standard` — the rules in §3 as written.
  - `strict` — every field classified `hashed` in §2/§3 is upgraded to
    `never` (dropped) rather than hashed. `subagent_description` and the
    slash-command name stay (they're already the narrowest allowed
    exception). Intended for orgs with a zero-content-egress policy.
  - `permissive` — allows `verbatim` emission on an explicit, hardcoded
    allowlist of fields *for local debugging only* (e.g. a developer running
    the adapter against a local test ingest endpoint to inspect payload
    shape). **`permissive` must never activate against a production ingest
    endpoint.** Enforcement: the emitter must refuse to honor
    `CARDINAL_REDACT_MODE=permissive` unless the resolved ingest endpoint
    host matches a local/loopback allowlist (`localhost`, `127.0.0.1`, or an
    explicit `CARDINAL_INGEST_ALLOW_PERMISSIVE=1` escape hatch that itself
    only takes effect for non-default endpoints) — a config flag alone is
    not sufficient gating, the destination has to be checked too.

- **`CARDINAL_REDACT_MAX_ATTR_BYTES`** — global hard cap (default suggested:
  8192 bytes) on any single attribute value after redaction is applied. This
  is a backstop, not the primary mechanism — `hash_field`'s `max_bytes`
  already bounds hashed fields — but nothing in the current hooks (`otlp.py`
  `kv`/`log_record`) enforces any size ceiling today (§7), so a
  verbatim-classified field (a name, a path) with a pathological length
  isn't currently bounded at all.

## 6. Test requirements — `core/cardinal_core/redaction.py`

- **Secret detection**: one test per regex in §4's table — true positive on
  a synthetic example, true negative on a plausible near-miss (e.g. a
  36-char alphanumeric string that isn't a GitHub PAT).
- **Secret dropping, not hashing**: when `scrub_secrets`/`redact_command`
  detects a pattern, assert the field is absent from the resulting envelope
  attribute set, not present as a hash.
- **Hash stability**: `hash_field(x) == hash_field(x)` across calls (same
  process and cross-process — no salt, no randomness); `hash_field(x) !=
  hash_field(y)` for `x != y` (collision spot-check, not a formal proof).
- **Hash truncation semantics**: for a value longer than `max_bytes`,
  assert `truncated=True` and `length` equals the *full* original length,
  not the truncated-prefix length.
- **No verbatim leakage in `strict` mode**: property test — for every field
  classified `hashed` in §2, assert the strict-mode output contains none of
  the original substrings of length > N (pick N small, e.g. 12, to catch
  partial leakage too).
- **Path scoping**: `redact_file_path` returns the relative path unchanged
  for a path inside `cwd`; returns a stable hash placeholder (not the raw
  path) for a path outside `cwd`; same `(path, cwd)` pair produces the same
  placeholder across calls.
- **`redact_command` never contains the command**: assert the returned dict
  has no key whose value is or contains the input string (beyond the closed
  `bash_class` enum, which by construction can't reproduce it).
- **`permissive` mode gating**: assert permissive verbatim emission is
  refused when the resolved ingest endpoint is not on the loopback
  allowlist, even if `CARDINAL_REDACT_MODE=permissive` is set.
- **Attribute byte cap**: assert `CARDINAL_REDACT_MAX_ATTR_BYTES` truncates
  (or rejects) any attribute value exceeding it, including verbatim-class
  fields.
- **Idempotence**: running redaction twice on an already-redacted value
  (hash dict, enum, capped string) is a no-op — guards against a future
  double-redaction bug that would corrupt already-safe data.

## 7. Baseline comparison

| File | Emits today | Verdict under new rules | Note |
|---|---|---|---|
| `adapters/claude/hooks/subagent-usage.py` | `subagent_type`, `agent_id`, `subagent_description` (capped 160, verbatim), token counts, `model`, `subagent_tool_counts` (names+counts only, no args) | **match** | Already conforms to §2/§3 exactly — this file is the reference implementation for the free-text exception and the names-only tool histogram. |
| `adapters/claude/hooks/turn-usage.py` | `tool_name`, `target` (file_path, **allowlisted tools only**, emitted verbatim with no cwd check), `bash_class`/`bash_multi` (never raw command) | **tighten** | Bash discipline already matches §3. `target` is not currently scoped to `cwd` — a `Read`/`Edit`/`Write` outside the repo (e.g. `~/.ssh/config`, a stray absolute path) is emitted verbatim today. New rule requires routing `target` through `redact_file_path`. |
| `adapters/claude/hooks/git-state.py` | `cwd`, `head_sha`, `branch`, `repo`, `remote_url`, initiative name/type, `command` (name only) | **tighten (fix a real gap)** | `remote_url` is emitted verbatim from `git remote get-url origin` with no credential stripping. Origin URLs with embedded tokens (`https://x-access-token:TOKEN@github.com/...`, common in CI-cloned or PAT-authenticated checkouts) would leak a live credential today. New rule requires `REMOTE_URL_USERINFO` scrubbing before emission — this is the most concrete pre-existing gap this review found. |
| `adapters/codex/hooks/cardinal-codex-telemetry.py` | `cardinal.turn_tool` matches Claude's discipline (`bash_class` only); but `tool_result` emits `tool_input`/`tool_parameters` as **raw JSON of the full tool call arguments** — for Bash this is the entire command string, for `apply_patch` this is the entire diff/patch text | **regression — must fix** | This is the single largest gap found. `append_tool_result_event` serializes `pending["tool_input"]` (the raw `arguments` dict from the transcript) verbatim into an OTLP attribute. Every Bash command and every file edit's patch content Codex has run is currently sent to the ingest endpoint in full. New envelope emission must route `tool_result` payloads through `redact_command`/`hash_field` the same way `turn_tool` already does — the two events must not diverge in redaction discipline. |
| `adapters/cursor/hooks/cardinal-cursor-telemetry.py` | `turn_tool` matches discipline; `tool_result.tool_input` has the **same raw-JSON regression** as Codex (full `command`, or full file `path`+possibly content depending on tool payload shape); `turn_response`/`turn_thought` correctly emit **length only**, never text (documented in-code as deliberate) | **mixed — one regression, one exemplar** | `turn_response`/`turn_thought`'s length-only pattern is exactly right and should become the canonical cross-adapter rule for model responses/reasoning (§2). `tool_result.tool_input` needs the same fix as Codex. `cursor.model_params` (resource attr, arbitrary JSON) has no size bound today — see Open Questions. |
| `adapters/gemini/hooks/cardinal-gemini-telemetry.py` | `turn_tool` matches discipline; `tool_result.tool_input` has the **same raw-JSON regression** as Codex/Cursor (raw `args` from `parse_args_json`, e.g. full shell command or file write content) | **regression — must fix** | Same fix as Codex/Cursor: `tool_result` must not diverge from `turn_tool`'s redaction. |
| `core/cardinal_core/otlp.py` | Generic transport (`kv`, `log_record`, `emit_records`); no size bound on any attribute value; `log_record` drops only falsy values, not oversized ones | **tighten** | No current attribute-size ceiling exists anywhere in the shared transport layer. `CARDINAL_REDACT_MAX_ATTR_BYTES` (§5) needs an enforcement point — most naturally in `kv()` or a wrapping helper in the new `redaction.py`, called by every adapter before `log_record`. |

**Summary**: three of five telemetry emitters (Codex, Cursor, Gemini) share
one real regression today — `tool_result.tool_input`/`tool_parameters`
carries raw tool call arguments verbatim, including full Bash command text
and file edit/patch content. Claude's `turn-usage.py` never had an
equivalent `tool_result` event, so it never had the gap. Fixing this in the
three affected adapters — bringing `tool_result` construction under the same
`redact_command`/`redact_file_path` discipline `turn_tool` already uses — is
the concrete Phase-1 follow-up this doc surfaces, independent of the new
envelope model. `git-state.py`'s unscrubbed `remote_url` is the second
concrete pre-existing gap across all four adapters that emit it (Claude,
Codex, Cursor, Gemini all call `git remote get-url origin` and forward the
result verbatim).

## Open questions

1. **`AWS_SECRET_ACCESS_KEY` detection** — unlike the access key ID
   (`AKIA...`, a fixed prefix), the secret key itself is an opaque 40-char
   base64-ish string with no distinguishing prefix. Regex alone will
   over-match (any 40-char token) or under-match (miss it entirely if not
   paired with a recognizable key name on the same line/field). Needs a
   decision: pair-based heuristic (only flag when adjacent to an
   `aws_secret_access_key`-shaped key name), entropy scoring, or accept the
   miss rate and rely on the generic `GENERIC_ENV_SECRET_ASSIGNMENT` pattern
   catching most real-world cases (`AWS_SECRET_ACCESS_KEY=...` env
   assignments would already match that pattern; the gap is only for
   secrets embedded mid-command with no surrounding key name).
2. **`cursor.model_params`** — an arbitrary JSON blob Cursor sends on every
   hook payload (sampling params, possibly custom system-prompt overrides
   depending on Cursor's mode config). Unclear whether this needs
   content-level redaction beyond the generic `CARDINAL_REDACT_MAX_ATTR_BYTES`
   cap, or whether it's config-only and safe verbatim. Flagging rather than
   classifying `verbatim` outright.
3. **`remote_url` scope** — is userinfo-stripping (§3, §4) sufficient, or
   should `remote_url`/`repo` be dropped entirely for internal/self-hosted
   Git hosts (i.e., anything that isn't `github.com`/`gitlab.com`/
   `bitbucket.org`) to avoid leaking internal VCS hostnames to a shared
   backend? Current behavior (all adapters) sends the canonicalized repo
   host regardless of whether it's a public or internal host.
4. **`strict` mode and `subagent_description`** — §5 currently exempts the
   free-text task-label exception from `strict` mode's hash-upgrade
   (alongside the slash-command name, which is a true closed name, not free
   text). Is that the right call, or should `strict` also hash
   `subagent_description`, given it's the one place arbitrary short prose
   crosses the boundary at any redact mode?

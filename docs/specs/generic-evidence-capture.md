# Generic evidence capture: every tool call can be cited

Status: IMPLEMENTED on `feat/generic-evidence-capture` (base: cardinal-agent-plugins `main` bf553ca,
#120/#122/#124/#128). The design below is kept as written; §11 lists where the implementation differs.
Requirement: "support evidence for ANY tool calls, not just bash + cardinal. This needs to be generic."

---

## 0. What exists today (read, not assumed)

```
Claude Code PostToolUse / PostToolUseFailure  matcher "mcp__.*"   (hooks.json)
   │
   ▼ hooks/evidence-capture.py  (sync, timeout 2s, fail-open)
   │   split_mcp_tool(tool_name) ── not mcp__ ──► return (NOTHING captured)   ◄── the gap
   │   server in {cardinal, plugin_cardinal_cardinal} ──► return (witnessed already)
   │   evidence.build_entry → scrub_and_cap → write_entry → gc
   ▼
~/.cardinal/evidence/<session>/ev_<12hex>.json   (dir 0700, file 0600, atomic; v1 schema)
   │   additionalContext: "[evidence:ev_…] captured locally from <server>/<tool>; to cite it … promote ev_…"
   │
   │   (nothing leaves the machine)
   ▼
cardinal-evidence promote [--storyboard sb_…] ev_…     (adapters/claude/bin — Claude only)
   │   auth: evidence_token (storyboard-token.py kept it) → else MCP key
   ▼
POST /api/orgs/:org/storyboards/:sb/evidence  (maestro evidence-upload.ts, zod EvidenceItemSchema)
   ▼
gateway /org/:org/receipts/ingest → BuildExternal (external.go) → maestro_receipts provenance='captured'
```

Findings that shape the design:

1. **The model is already lazy-promote.** Capture is local-only; upload happens only on explicit
   `promote`. No redesign of the upload model is needed; only the capture decision and what is spooled.
2. **The capture decision is an MCP-name gate in three places**: Claude `evidence-capture.py`
   (`split_mcp_tool`), Cursor `capture_evidence` (`split_mcp_tool`), Gemini `capture_evidence`
   (`mcp_server_and_tool`, and it also drops every failed call). Codex, pi and opencode never capture.
3. **`evidence.normalize()` would mis-read built-in results.** Its dict branch fires on any dict with a
   `content` key. Claude Code's `Write` result is `{type, filePath, content, structuredPatch, originalFile,
   userModified}` (sampled from local transcripts), so today's normalizer would keep the whole written file as
   "text" and drop the patch. The MCP-content reading must only fire on the MCP shape.
4. **The scrub is the gateway's leaf rule.** A plain string leaf gets `redact_value_shapes` only (URL userinfo,
   `Bearer x`, token shapes, PEM). It does NOT redact `DB_PASSWORD=hunter2` lines. That's fine for MCP JSON.
   It's not enough for shell stdout or file contents. `redact_plain_text` (key=value rules) exists in
   core already; built-in text needs it.
5. **The spool has TTL only (14 d), no size bound**, though spec §18 promises "under 256 MiB, oldest first".
   Capturing every Read/Grep/Bash makes that promise load-bearing.
6. **Maestro/gateway already accept arbitrary source/tool** as long as each is an identifier:
   `^[A-Za-z0-9_.:/@-]+(?: [A-Za-z0-9_.:/@-]+)*$`, source_server ≤128, tool ≤200, client ≤64, args must be a
   JSON object (evidence-upload.ts `ident`, external.go `checkIdent`). A tool's name is "a label, never a key"
   (spec §17.2): no tool-keyed semantics apply to captured receipts. **No validation/DB change is needed.**
   The *text* that tells the model what "captured" means (grammar.ts `EVIDENCE_GUIDE.tiers.captured`: "another
   MCP server's real tool response") and the viewer's source line ("uploaded from <client>") are MCP-specific
   and need a small wording change.
7. **Promote exists only for Claude** (`adapters/claude/bin/cardinal-evidence`). Cursor/Gemini write the shared
   spool but have no promote path; the storyboard skill exists only in the Claude adapter (#125 slimmed it).
8. An earlier Bash-only attempt (branch `feat/bash-evidence-capture`) had no commits: nothing to reuse; it was deleted.

Real Claude Code result shapes (key sets sampled from this machine's transcripts, counts from 40 sessions):

| shape keys | tool | n |
|---|---|---|
| `stdout, stderr, interrupted, isImage, noOutputExpected` (+`returnCodeInterpretation`, `backgroundTaskId`, `persistedOutputPath/Size`, `gitOperation`, `bashEditDiff`, `timedOutAfterMs`) | Bash | ~1850 |
| `file, type` (`file.{filePath,content,numLines,startLine,totalLines}` or `file.base64` for images/PDF) | Read | 347 |
| `filePath, oldString, newString, originalFile, replaceAll, structuredPatch, userModified` | Edit | 306 |
| `type, filePath, content, structuredPatch, originalFile, userModified` | Write | 36 |
| `agentId, status, isAsync, outputFile, prompt, …` | Agent (async) | 78 |
| plain string | MCP / misc | 309 |

---

## 1. Data flow (target)

```
 ANY tool call  (built-in · any MCP server · future/unknown tool)
        │
        ▼
 ┌────────────────────── adapter hook (thin; runtime-specific I/O only) ──────────────────────┐
 │ Claude  PostToolUse + PostToolUseFailure  matcher ".*"                                     │
 │ Cursor  postToolUse + postToolUseFailure       Gemini  AfterTool (matcher "")              │
 │ Codex   PostToolUse (all tools)                pi/opencode  in-process events → bridge     │
 │   build ToolCall{runtime, tool_name, source, input, response|error, ids, cwd, spill_root}  │
 └────────────────────────────────────────┬───────────────────────────────────────────────────┘
                                          ▼
 ┌──────────────── cardinal_core.evidence_capture.capture_call()  (ONE pipeline) ─────────────┐
 │ 0 budget guard: 1.5 s wall (setitimer) · stdin ≤ 32 MiB · fail open, exit 0                 │
 │ 1 opt-out?  CARDINAL_EVIDENCE_CAPTURE=0 | ~/.cardinal/evidence/disabled ──► nothing        │
 │ 2 dedupe:   source is Cardinal's own gateway (witnessed receipt exists) ──► nothing         │
 │ 3 SENSITIVITY GATE over every string in tool_input (tool-neutral)                           │
 │      path rules · secret-command rules · credentialed URL/header rules · user rules         │
 │      hit ──► WITHHELD STUB {tool, source, status, reason, rule_id}  (no args, no result)    │
 │ 4 NORMALIZE: first matching pluggable normalizer, else generic JSON fallback               │
 │      (normalizers only reshape; any exception ──► generic)                                  │
 │ 5 REDACT every field: structural scrub (gateway port) + plain-text key=value rules          │
 │      + PEM + token shapes + surrogates/NUL + sensitive-path lines + base64 blobs            │
 │      + local-path scrub (spill root → [local file], cwd → ".", $HOME → "~")                 │
 │ 6 CAP: args ≤ 64 KiB, result ≤ 256 KiB → #1982 envelope {truncated, original_bytes, prefix} │
 │ 7 WRITE ev_<12hex>.json (v2, atomic, 0600/0700) · gc (TTL + size + per-session caps)        │
 └───────────────────────────────┬─────────────────────────────────────────────────────────────┘
                                 ▼
   ~/.cardinal/evidence/<session>/ev_….json         ◄── never leaves the machine by itself
   context line to the agent: "[evidence:ev_…]"  |  "[evidence:ev_… withheld: <reason>]"
                                 │
          agent cites it ────────┘
                                 ▼
   cardinal-evidence promote ev_…  (withheld ⇒ refused locally; nothing sent)
        ▼  wire item: provenance "captured", source_server = <mcp server> | "builtin:<runtime>",
        ▼             tool = ident(tool), client, client_called_at, args{}, result
   maestro POST …/storyboards/:sb/evidence ─► gateway BuildExternal (scrubs AGAIN, caps AGAIN)
        ▼
   rcpt_…  (tier badge "captured"; source line "Bash · claude-code built-in · reported by claude-code/0.x")
```

The capture decision is exactly steps 1–3. None of them names a tool. Step 2 checks the *source*: skip when
Cardinal's gateway already witnessed the call. Step 3 checks the *strings*. An unknown tool goes through the
same steps with zero code changes.

---

## 2. Spool entry schema v2

`cardinal.evidence.v2`. Readers (`list`, `show`, `promote`, `gc`) accept v1 and v2. Writers write v2 only.

```jsonc
{
  "schema": "cardinal.evidence.v2",
  "evidence_id": "ev_3c1f0a9b7d22",
  "tier": "captured",                       // unchanged meaning
  "session_id": "3f2a9c1e-…",               // null → "no-session" dir (as v1)
  "tool_use_id": "toolu_01ABC",             // when the runtime gives one (≤256 chars)
  "source": {                               // NEW: provenance of the call
    "kind": "mcp" | "builtin" | "tool",     //  "tool" = runtime can't tell MCP from built-in (opencode)
    "server": "grafana",                    //  kind=mcp only
    "runtime": "claude-code"                //  always
  },
  "server": "grafana" | "builtin:claude-code",   // v1-compatible field = the wire source_server
  "tool": "Bash",                           // short tool (MCP: tool part; else tool_name)
  "tool_name": "Bash",                      // raw runtime name (scrubbed, ≤256)
  "client": "claude-code/0.36.0",           // the adapter's client string, as each spells it today
  "called_at": "2026-09-29T17:02:11.123456Z",   // hook time (runtime gives no start time)
  "status": "ok" | "error",                 // NEW (v1: is_error bool, still written for v1 readers)
  "exit_code": 2,                           // NEW, optional (shell normalizer / "Exit code N")
  "normalizer": "shell" | "file-edit" | "mcp-content" | "generic",   // NEW: which shaper ran
  "summary": "make check-maestro",          // NEW: ≤120 chars, scrubbed; for list/find
  "args": {…}, "args_truncated": false,
  "result": {"structured": …, "text": […], "other_blocks": n} | {"truncated": true, "original_bytes": n, "prefix": "…"},
  "truncated": false, "spilled": false, "spilled_bytes": 0,
  "is_error": true,                         // kept for v1 readers when status=error
  "withheld": {                             // NEW, only on a stub; then args=null, result=null
    "reason": "sensitive_path" | "secret_command" | "credential_url" | "credential_header" | "user_rule" | "unreadable",
    "rule": "path.dotenv",                  // stable rule id (allow-listable)
    "hint": ".env*"                         // non-secret: the pattern, never the matched text
  }
}
```

**Evidence id v2.** When the runtime gives a tool-use id, `ev_ + sha256("cardinal.evidence.v2|" + runtime + "|" +
session + "|" + tool_use_id)[:12]`. It's deterministic, so a re-fired hook overwrites the same file, and a JS
adapter can compute the id in-process before Python writes the file (pi/opencode, §5). Without a tool-use id the
v1 formula plus a 64-bit random nonce is used. Shared test vectors (`core/tests/testdata/evidence_id_vectors.json`)
pin the Python and JS implementations to each other.

**Wire mapping (promote → EvidenceItemSchema; no server change):**

| spool | wire | rule |
|---|---|---|
| source.kind=mcp | `source_server` = server | `:` in a server name → `_` (the `builtin:`/`tool:` namespaces stay plugin-assigned) |
| source.kind=builtin | `source_server` = `builtin:<runtime>` | e.g. `builtin:claude-code`, `builtin:codex` |
| source.kind=tool | `source_server` = `tool:<runtime>` | opencode |
| tool | `tool` = `ident(tool)` | chars outside `[A-Za-z0-9_.:/@-]`→`_`, runs of spaces→one, ≤200, empty→`unknown_tool` |
| args (non-object) | `args` = `{"input": <value>}` | object required by schema; `null` → `{}` |
| status=error | `result.is_error: true` | |
| withheld | not sent | local error `withheld: <reason> (<rule>)`, exit code as a failed row |
| client | `client` | entry's own client when it passes ident ≤64, else current adapter client |

---

## 3. The generic sensitivity gate (`cardinal_core/evidence_gate.py`)

**Input:** the raw (pre-scrub) `tool_input` and tool name, in memory only. **Output:** `None` (capture) or
`Withheld{reason, rule, hint}`. It walks every string leaf *and every key*, depth ≤ 32, visiting ≤ 5,000 leaves.
It **fails closed**: an input the walk cannot finish (deeper, or more leaves) is withheld as `unreadable`
(`gate.bounds`), never captured unchecked. Each string is looked at up to 64 KiB. A longer string is content (a
Write body), not a path or command: it's only redacted in step 5, except that a long shell script with here-documents
is checked with the heredoc bodies stripped (so `cat > x <<EOF …70 KiB… EOF; cat .env` is still seen). It **never
looks at the tool name** except through a user's own `deny.tools` rule.

### 3.1 Path rules (any path-like token in any string)

Tokenize on whitespace, quotes, `= , ; | & < > ( )` and backticks. A token is path-like if it contains `/`,
starts with `.` or `~`, or equals a sensitive basename; a bare word that is a symlink in `cwd` counts too.
Normalize it: `~`, `$HOME`, `${HOME}` → home, `~user` → that user's home, relative to payload `cwd`, `..` collapsed
lexically. For ≤ 256 tokens that name an existing path, also `os.path.realpath` (a symlink
`notes.txt → ~/.aws/credentials` is caught). `{a,b}` brace words are expanded; a glob (`.env*`, `.e?v`,
`~/.kube/conf*`) is matched against the well-known sensitive names when its file part starts with a literal, and
against the files it names on disk (bounded). Matching is case- and Unicode-folded (NFKC + casefold: `~/.KUBE/Config`
opens `~/.kube/config` on APFS), and the credential stores below match under **any** home directory (`/root/.kube/config`,
`/home/x/.aws/…`, an unexpanded `$HOME/…`). Match basename globs and directory prefixes:

| rule id | pattern |
|---|---|
| path.dotenv | basename `.env`, `.env.*`, `.envrc`, `*.env` (a bare `name.env` word only when that file exists: `process.env` is code) |
| path.credentials | basename `*credential*` unless the extension is source/doc (`.ts .tsx .js .mjs .cjs .py .go .rs .java .kt .rb .php .cs .c .h .cc .cpp .swift .md .mdx .rst .html .css`); so `credentials.json`, `credentials` yes, `credentials-store.ts` no |
| path.secrets | basename `secret`/`secrets` + `{,.json,.yaml,.yml,.toml,.env,.txt}`, `*.secret(s)`, `*.tfvars`, `terraform.tfstate*`, `.vault-token`; any file under a `secret/`, `secrets/` or `.secrets/` directory unless it is source/doc |
| path.private-key | `*.pem *.key *.p12 *.pfx *.jks *.keystore *.ppk`, `id_rsa* id_dsa* id_ecdsa* id_ed25519*` |
| path.ssh / .aws / .gnupg / .azure / .password-store | `~/.ssh/**` (except `*.pub`, `known_hosts`), `~/.aws/**`, `~/.gnupg/**`, `~/.azure/**`, `~/.password-store/**` |
| path.kube | `~/.kube/**`, basename `*kubeconfig*`, `/var/run/secrets/**` |
| path.netrc / .docker / .npmrc … | `.netrc _netrc .git-credentials .pgpass .my.cnf .npmrc .pypirc`, `~/.docker/config.json`, `~/.config/gh/hosts.yml`, `~/.config/gcloud/**` |
| path.keychain | `*.keychain *.keychain-db`, `~/Library/Keychains/**` |
| path.agent-secrets | `~/.cardinal/**` (spool + tokens), `~/.claude/settings*.json`, `~/.claude.json`, `~/.claude/.credentials.json`, `~/.codex/auth.json`, `~/.gemini/oauth_creds.json`, `<agent home>/cardinal-secrets.json` |
| path.proc-environ | `/proc/*/environ`, `/etc/shadow`, `/etc/sudoers*` |
| path.agent-transcripts | `~/.claude/projects/**` (transcripts and spilled tool outputs), `~/.codex/sessions/**`, `~/.gemini/tmp/**`, `~/.claude/history.jsonl`, `~/.codex/history.jsonl`: they replay earlier results verbatim, including withheld ones. Cite the original call instead |

### 3.2 Secret-command rules (any string that parses as shell)

The gate splits strings of ≤ 64 KiB into simple commands on `; && || | & \n`, `$( … )` and backticks. It
recurses into `bash|sh|zsh -c '<script>'`, `su -c`, `eval`, `xargs`, here-strings (`sh <<< '…'`), `kubectl|oc exec … --
<cmd>`, `docker|podman exec|run [opts] <container> <cmd>`, `ssh [opts] <host> <cmd>`, anything after `--`, and at most
256 segments. Each segment goes
through `shlex.split` (on failure, a whitespace split). Wrappers are dropped: `sudo [-flags]`, `command`, `exec`,
`nohup`, `time`, `nice`, `timeout N`, and `env [-i] [VAR=val…]` when a command follows. The rest is matched
by basename(argv0) plus a subcommand path:

| rule id | match |
|---|---|
| cmd.env-dump | `printenv`; `env` with no command operand; `export`/`declare -x`/`typeset -x`/`set` with no args or only `-p`; `ps e…`/`ps -E`; an interpreter's inline program that prints the environment (`node -e …process.env`, `python -c …os.environ`, `ruby -e …ENV.to_h`, `perl …%ENV`, heredoc programs too) |
| cmd.secret-var | `echo`/`printf` of `$NAME` where NAME is secret-named (`OPENAI_KEY`, `GITHUB_TOKEN`; `gate.secretish_name`), `declare -p` of one, an inline program reading one (`os.environ["STRIPE_KEY"]`) |
| cmd.decoded-exec | a decoded string used as a command or argument: `$(… \| base64 -d)`, `… base64 -d \| sh` (what it names cannot be checked) |
| cmd.config-secret | `<tool> config get <key>` / `config --get` of a secret-named key (`npm config get //registry/:_authToken`, `pip config get global.password`) |
| cmd.aws | `aws sts get-session-token\|assume-role*\|get-federation-token`, `aws configure export-credentials\|get *key*\|*secret*`, `aws secretsmanager get-secret-value`, `aws ssm get-parameter(s)` + `--with-decryption`, `aws ecr get-login-password`, `aws iam create-access-key` |
| cmd.kube-secret | `kubectl\|oc\|k\|kc get\|describe\|edit` + `secret(s)` / `secret/*`; `kubectl config view --raw\|--flatten`; `kubectl create token` |
| cmd.gh-token | `gh auth token`; `gh auth status -t\|--show-token` |
| cmd.gcloud / az | `gcloud auth [application-default] print-*-token`, `gcloud secrets versions access`, `az account get-access-token`, `az keyvault secret show\|download` |
| cmd.vault / op | `vault read\|kv get\|token lookup\|print token`, `op read`, `op item get` |
| cmd.keychain | `security find-generic-password\|find-internet-password\|dump-keychain` |
| cmd.git-credential | `git credential fill`, `git config --get*` of `*credential*`/`*token*` |
| cmd.misc-token | `heroku auth:token`, `npm token *`, `doctl auth *`, `sops -d\|--decrypt`, `gpg -d\|--decrypt` |
| + path rules | every argv word, after shell unquoting, also runs through §3.1 (`cat ~/.aws/credentials`, `source .env`, `cat .e''nv`, `cat "$HOME"/.kube/config`); in a multi-line string only for commands that read files (`cat`, `grep`, `cp`, …), so a README line "copy it to .env" is not an access |

`grep -r printenv docs/` isn't withheld: argv0 is `grep`, and argv parsing is why a regex over the raw
string isn't enough. `env FOO=1 make test` isn't withheld either: a command follows.

### 3.3 Credentialed URLs and headers (any string)

- **url.userinfo** — `scheme://user:pass@` (the linear scanner `_url_userinfo_spans` already exists).
- **url.credential-param** — a query or fragment (`#access_token=`) parameter, name percent-decoded, named `token
  access_token id_token api_key apikey key sig signature password secret client_secret code X-Amz-Signature
  X-Amz-Credential X-Goog-Signature sv+sig (SAS)` or secret-named (`private_token`, `jwt`, `STRIPE_KEY`), carrying a value.
- **header.credential** — `Authorization:`, `Proxy-Authorization:`, `Cookie:`, `X-Api-Key:`, `X-*-Token:` or any
  `is_credential_key` header name followed by a value, in a string or as a `headers` object key. Also `curl -u
  user:pass`, `--password[= ]x`, `-p<pw>` for mysql, `PGPASSWORD=… ` prefixes.

The content behind a credentialed request is treated as private, so the call is withheld, not just redacted.

### 3.4 Response-side defense (not a gate; part of step 5, tool-neutral)

- A line of result text that begins with a path matching §3.1 followed by `:` (the `grep`/`rg` `path:line:`
  shape) becomes `<path>:[withheld]`; a `path-line-` / `path-` context line becomes `<path>-[withheld]`; in rg's
  `--heading` form, the lines under a sensitive path heading become `[withheld]` up to the next blank line.
- A wider key rule than the gateway's (plugin-only, so the gateway parity vectors are unchanged): a key=value pair
  or JSON member whose key is secret-named in the wider sense (`OPENAI_KEY`, `STRIPE_SECRET`, `tls.key`,
  `client-key-data`, `private_token`), a `{"name": <secret name>, "value": …}` object (k8s/ECS env), and the YAML
  `- name: DB_PASSWORD` / `value: …` pair all lose the value.
- A string leaf ≥ 4 KiB made only of base64 characters becomes `"[binary omitted: N bytes]"`. This covers
  Read's `file.base64`, MCP blobs and data URLs.
- `redact_plain_text` (key=value rules) runs on every non-JSON string leaf, on top of the gateway's leaf rule.
  The plugin is then strictly *more* aggressive than the gateway, and the gateway scrubs again on ingest.
  `scrub_vectors.json` gains plain-text vectors.

### 3.5 User control

- `CARDINAL_EVIDENCE_CAPTURE=0` / `cardinal-evidence off` turns capture off for every tool (existing).
- `CARDINAL_EVIDENCE_CONTEXT=0` keeps capturing but stops the per-call `[evidence:…]` lines. The agent then finds
  ids with `cardinal-evidence list/find`.
- `~/.cardinal/evidence-rules.json` (user; regular file ≤ 64 KiB, owned by the user, JSON):
  ```json
  { "version": 1,
    "deny":  { "tools": ["mcp__gmail__*"], "paths": ["**/customer-exports/**"], "commands": ["^psql .*prod"] },
    "allow": { "rules": ["path.dotenv"], "paths": [".env.example", "**/fixtures/**/credentials.json"] } }
  ```
  Precedence: user deny > user allow > built-in rules. `allow` only relaxes the gate; redaction always runs.
- `<cwd>/.cardinal/evidence-rules.json` (project) is **deny-only**: its `allow` is ignored, so a cloned repo can't
  turn off secret protection.
- A malformed rules file is ignored, with a one-line warning in `cardinal-evidence status`, and the built-in
  rules still apply.
- `deny.tools` is the only place a tool name enters the decision, and it's the user's own rule.

### 3.6 Withheld stub

Stored: `evidence_id, schema, tier, session_id, tool_use_id, source, server, tool, tool_name, client, called_at,
status, withheld{reason, rule, hint}`. `args`/`result` are `null`, with no summary. `hint` is the rule's pattern
(`.env*`, `gh auth token`), never the matched text. Context line: `[evidence:ev_… withheld: sensitive path
(.env*)]`. `promote` refuses it locally (`ev_…: error withheld: …`). `list` shows it, so the agent can say
"I read .env but can't cite it."

---

## 4. Normalizer plug-in interface (`cardinal_core/evidence_normalizers/`)

```python
# core/cardinal_core/evidence_normalizers/__init__.py   (py3.9, stdlib only)
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

@dataclass(frozen=True)
class ToolCall:
    runtime: str                  # "claude-code" | "cursor" | "gemini" | "codex" | "pi" | "opencode" | …
    tool_name: str                # raw
    source: dict                  # {"kind": "mcp"|"builtin"|"tool", "server"?: str, "runtime": str}
    tool: str                     # short name
    tool_input: Any               # raw, any JSON (not only dict)
    response: Any                 # raw tool_response / tool_output / llmContent …, or None
    error: Optional[str] = None   # failure text (PostToolUseFailure.error, AfterTool.error, …)
    session_id: Optional[str] = None
    tool_use_id: Optional[str] = None
    cwd: Optional[str] = None
    spill_root: Optional[str] = None   # dir whose files a result may point at (Claude: ~/.claude/projects)

@dataclass
class Normalized:                 # UNSCRUBBED; the pipeline scrubs + caps afterwards
    structured: Any = None        # bindable JSON (selectors address it)
    text: list = field(default_factory=list)
    other_blocks: int = 0
    is_error: bool = False
    exit_code: Optional[int] = None
    summary: Optional[str] = None
    spilled_bytes: Optional[int] = None

@dataclass(frozen=True)
class Normalizer:
    id: str
    match: Callable[[ToolCall], bool]            # SHAPE-based; may also look at source.kind; cheap, total
    normalize: Callable[[ToolCall], Normalized]  # may raise → pipeline falls back to "generic"

REGISTRY: list  # ordered; "generic" is implicit and always last
def register(n: Normalizer) -> None: ...
def normalize_call(call: ToolCall) -> tuple:     # (Normalized, normalizer_id); never raises
```

Contract:

- **Normalizers never gate.** They can't skip capture, add/remove sensitivity, or bypass scrub/cap. They reshape
  the result into a better-bindable body. `match` must be total and cheap. An exception in `match` or
  `normalize`, or any non-`Normalized` return, falls back to `generic` (tested).
- **They match on shape, not on tool names, wherever possible.** The shell normalizer fires on `{stdout|stderr}`
  keys, whatever the tool is called (Claude `Bash`, Cursor `Shell`, a third-party MCP `run_command` returning
  that shape). Matching on a tool name is allowed only as a fallback hint inside a normalizer (`codex` Bash
  returns a bare string).
- A normalizer is added by dropping a module into the package and calling `register(...)`; `build/vendor.py`
  copies the package verbatim.

Initial normalizers (all optional; with none, everything still works through `generic`):

| id | match (shape) | output |
|---|---|---|
| mcp-content | `source.kind=="mcp"` or keys ⊆ {content, structuredContent, isError, _meta} with `content` a str/list of blocks | today's `normalize()` (spill follow, JSON-text→structured, image placeholders → `[local file]`) |
| shell | dict with `stdout`/`stderr` string(s) | `structured={stdout, stderr, exit_code?, interrupted?, background_task_id?, timed_out_ms?}`; follows `persistedOutputPath` under spill_root; on failure parses `^Exit code (\d+)` from `error`; `summary`=the command string from input (first ≤120 chars) |
| file-edit | dict with `structuredPatch` | the patch is the evidence: `structured={file_path, patch, type?, user_modified?, replace_all?}`; always drops `originalFile`; a Write's `content` is kept only when ≤ 32 KiB (else the patch alone) |
| generic | always | dict/list → `structured` as-is; str → JSON container → `structured`, else `text=[s]`; `None` → `text=[""]`; error → `text=[error]`, `is_error` |

Read/Grep/Glob/WebFetch/WebSearch/Agent/TodoWrite/NotebookEdit need no normalizer. `generic` keeps their JSON, and
step 5 handles base64 and local paths.

---

## 5. Per-adapter hook wiring

| runtime | post-tool hook | failures | wiring change | context channel | status |
|---|---|---|---|---|---|
| **Claude Code** | `PostToolUse` | `PostToolUseFailure` (`error`, `is_interrupt` → skip) | hooks.json: the two `mcp__.*` entries become matcher `".*"`, sync, `timeout: 2` | `hookSpecificOutput.additionalContext` | supported |
| **Cursor** | `postToolUse` (already every tool) | `postToolUseFailure` (documented; not registered today) | drop the `split_mcp_tool` gate in `capture_evidence`; `cardinal-connect` registers `postToolUseFailure`; README: re-run `cardinal-connect --rotate` | `additional_context` | supported (failure payload shape to verify with a debug dump; cloud agents need `.cursor/hooks.json` via `--project`) |
| **Gemini CLI** | `AfterTool` (matcher `""`, every tool) | same event, `tool_response.error` | drop the MCP gate; stop discarding errors (capture `status=error`); non-MCP `llmContent` goes to generic/shell via `gemini_tool_response` unwrapping | `hookSpecificOutput.additionalContext` | supported |
| **Codex** | `PostToolUse` | Bash fires on non-zero exit too (README); no separate failure event known | `cardinal-connect`: add a second PostToolUse group with the all-tools matcher (verify `""`/`.*` semantics in `hooks/src/events/common.rs matches_matcher`), keep the `Bash` decision group; the telemetry hook calls `capture_call` for every PostToolUse | `hookSpecificOutput.additionalContext` (verified for PostToolUse in `handle_post_tool_use`) | partial. Bash is verified (`tool_response` is a string). Whether MCP tools and `apply_patch` fire PostToolUse is **unverified**; a fixture + debug dump in impl, documented as such. Needs `--repair-hooks` + restart |
| **pi** | extension events `tool_execution_start` (args) / `tool_execution_end` (result, isError) | `isError` | send `{kind:"evidence", …}` through the existing batched bridge → `cardinal_native.py` → `capture_call`; compute the v2 id in JS | append `[evidence:ev_…]` via pi's result-modifying `tool_result` event if the API allows (verify), else none (use `list`) | supported (async write; withheld reason visible in `list`, not inline) |
| **opencode** | plugin `tool.execute.before` (args) / `tool.execute.after` (output mutable) | `message.part.updated` with `state.status=="error"` | same bridge path; `source.kind="tool"` for non-`cardinal_` tools (opencode ids are ambiguous) | append the id line to `output.output` in `tool.execute.after` | supported (error path to verify) |
| **Devin** | none (a remote poller over the Devin API) | — | — | — | **unsupported**: no post-tool hook on the user's machine. Use the reported tier (`storyboard__record_evidence`) |
| claude.ai / Claude Desktop | none | — | — | — | reported tier (unchanged) |

Every adapter builds a `ToolCall` and calls `capture_call`. There's no adapter-side MCP test left, except the
source classifier:
- Claude/Cursor/Codex: `mcp__<server>__<tool>` → mcp, else builtin.
- Gemini: `mcp_context` → mcp, else builtin.
- pi: `cardinal_call_tool` → Cardinal (skipped), else builtin.
- opencode: `cardinal_*` → Cardinal, else `tool`.

**Promote for non-Claude runtimes.** Move `wire_item/batches/post_upload/cmd_*` from `adapters/claude/bin/cardinal-evidence`
into `core/cardinal_core/evidence_promote.py`. Each adapter keeps a thin launcher that supplies its own connection
lookup (Claude: `~/.claude/settings.json`; Cursor/Codex/Gemini: their managed connection files; pi/opencode:
`cardinal-secrets.json` through `cardinal_native.py evidence …`). Storyboard evidence-token capture
(`storyboard-token.py` logic) moves into core as `observe_cardinal_result()`, called from the same post-tool path
when source = Cardinal. That's the only thing the dedupe branch does with a Cardinal result.

**Hook cost (all adapters):**
- Never blocks or fails the tool: every entry point is wrapped `try/except BaseException → exit 0`.
- A 1.5 s `setitimer` guard under Claude's 2 s kill. A write interrupted by the guard leaves only a temp file,
  which gc reaps after 1 h.
- stdin is read to ≤ 32 MiB. A longer payload isn't parsed: a stub with `withheld.reason="unreadable"`
  (`rule: size`) is written instead.
- Strings over 2 MiB go through `bounded_scrub`, never a whole walk.
- gc runs at most every 10 min (was 6 h) with a 250 ms budget.
- Target: p50 ≤ 60 ms and p99 ≤ 400 ms per call, measured by a perf smoke test.
- Context tokens: the first capture in a session gets the full hint line ("… cite with `cardinal-evidence promote
  ev_…`"; a `<session>/.hinted` marker). Every later capture gets the ~10-token `[evidence:ev_…]`.
  `CARDINAL_EVIDENCE_CONTEXT=0` turns the lines off.

**Spool bounds (gc):**
- TTL 14 d (unchanged).
- Total ≤ 256 MiB (spec §18's promise, now implemented; `CARDINAL_EVIDENCE_MAX_MB`), evicting the oldest entries
  across sessions.
- ≤ 10,000 entries per session.
- `token.json` and `.hinted` are never size-evicted.
- `cardinal-evidence status` shows size and caps.

---

## 6. CLI + skill text

`cardinal-evidence` gains:
- `list [--tool GLOB] [--grep TEXT] [--last N] [--withheld]`: shows tool, status, summary, size and withheld reason.
- `find TEXT`: an alias of `list --grep` across the session.
- `show ev_…`: prints the scrubbed entry, capped at 24 KiB with a pointer outline past that, so the agent can read
  exactly what it will cite.

`promote` accepts v1/v2 and refuses withheld entries locally.

Claude `skills/storyboard/SKILL.md` "Evidence in Claude Code" becomes (the others get the same text in their
agent-instruction surface: GEMINI.md, the Codex/Cursor README instructions; they ship no storyboard skill today):

> - **Captured.** Any tool result from this session is citable: shell commands (tests, `git`, `make`), file reads
>   and edits, searches, web fetches, subagents, other MCP servers and any other tool. The plugin keeps each
>   result on this machine and prints `[evidence:ev_…]`. Nothing is uploaded until you promote it. Can't see the
>   id? `cardinal-evidence find <text>` or `cardinal-evidence list --tool Bash`. After `create`, promote only the
>   ids your scenes will bind: `cardinal-evidence promote --storyboard <id> ev_…`. Bind the returned `rcpt_…`.
> - **Withheld.** `[evidence:ev_… withheld: …]` means the call touched something sensitive (a `.env`, a key, a
>   credential command). No content was kept, and it can't be cited. Say that plainly in the storyboard instead
>   of paraphrasing the result.
> - **Never claim more than the output shows.** A captured result is the *client's* record, labelled "captured,
>   reported by <client>". An exit code of 0 shows that the command succeeded, not that the feature works. Run
>   `cardinal-evidence show ev_…` before citing, and bind the exact field (`/exit_code`, a `stdout` line through
>   `extract`) that carries the claim.
> - **Reported** (`storyboard__record_evidence`) is for results with no `ev_…` (capture off, a client with no hook).

---

## 7. Maestro / conductor changes (minimal; none blocks the plugin)

Validation, gateway and DB need **no change** (finding 6). Changes, all on branch `feat/generic-evidence-capture`:

1. **`packages/maestro/src/storyboard/grammar.ts`**:
   - `EVIDENCE_GUIDE.tiers.captured`: "A Cardinal plugin hook recorded the real result of a tool call on your machine
     (any tool: shell, file reads/edits, web fetches, subagents, other MCP servers) and gave you an ev_… id …".
   - `evidence_tiers.captured`, and a line saying that a `builtin:<runtime>` source_server means the agent runtime's
     own tool.
   - Tests: the grammar snapshot/unit test.
2. **`packages/ui-pages/src/storyboards/EvidenceDrawer.tsx`** (+ the public page's receipt card, which renders the
   same wire):
   - `sourceLabel(source_server)`: `builtin:<rt>` → "<rt> built-in tool", `tool:<rt>` → "<rt> tool", else the
     server name.
   - The Source line changes from "· uploaded from <client>" to "· **reported by** <client>" for captured/reported
     receipts. The tier badge stays "captured"; the tooltip already says Cardinal didn't run the call.
   - Tests in `EvidenceDrawer.test.tsx`.
   - Naming note for the reviewer: "reported by <client>" is the requested wording. The "reported" *tier* badge is a
     different thing (the model's upload); the badge disambiguates.
3. **Tests pinning acceptance** (no code change):
   - maestro `evidence-upload.test.ts`: item `{source_server:"builtin:claude-code", tool:"Bash", args:{command:…},
     result:{structured_content:{stdout,exit_code}}}` parses.
   - A sanitized odd tool name parses.
   - Go `external_test.go`: `BuildExternal` accepts the same.
4. **`docs/specs/investigation-storyboards.md`**:
   - §17 table: "captured — the client's hook, from the real result of any tool call".
   - §17.1 `source_server` comment covers the `builtin:`/`tool:` namespaces.
   - §18 rewritten for the generic pipeline: the gate, withheld stubs, normalizers, v2 schema, the implemented
     256 MiB bound.
   - Fix stale facts: the path is `~/.cardinal/evidence`, not `~/.claude/cardinal/evidence`; ids are `ev_<12hex>`;
     the hook is `evidence-capture.py`.
   - Note that the `client_evidence_id` idempotency described in §17.2 isn't implemented; out of scope here.
5. **Release.** It isn't needed for capture/promote to work. The label/grammar text reaches prod with the next patch
   release after v1.97.19 is live (per the release rule). Check kubernetes-clusters `image.tag` first.
6. **Companions.**
   - charts: none (no env/port/secret).
   - cardinal-docs: no page covers evidence capture today; recommend a short privacy page (what's captured,
     withheld rules, opt-out, promote-only-uploads) and flag it in the PR, not blocking.

---

## 8. Test plan (tests first; plugin python suites run locally; conductor targeted only)

**core/tests** (`python3 -m unittest`, py3.9-clean):
- `test_evidence_capture_call.py`
  - **Unknown tool**: `tool_name="FrobnicateWidgets_v9"`, input `{"x":[1,{"y":"z"}]}`, response `{"weird":{"n":1}}`
    → captured, `source.kind=builtin`, `normalizer=generic`, `wire_item` valid. No registry entry exists for it.
  - Unknown MCP `mcp__my-srv__do thing!` → the wire tool is sanitized to an ident.
  - Non-dict `tool_input` (list / str / number / null) → wire `args={"input":…}` or `{}`.
  - Every sampled Claude shape: Bash, Bash `persistedOutputPath` (followed only under spill_root), background Bash
    (`backgroundTaskId`), Read text, Read image base64 (omitted), Edit (no `originalFile` kept), **Write (`content`
    key NOT read as MCP content)**, Grep content mode, Glob, WebFetch, WebSearch, Agent sync/async, TodoWrite,
    NotebookEdit, AskUserQuestion.
  - Failure: `PostToolUseFailure` `"Exit code 2\n…"` → `status=error, exit_code=2`; interrupt → nothing.
  - A Cardinal source → nothing captured, token observed.
  - A normalizer that raises / returns junk → `generic` is used, and capture still happens.
- `test_evidence_gate.py`: table-driven.
  - Positives for every rule id, including through wrappers: `sudo -E env`, `bash -lc 'printenv | sort'`,
    `$(gh auth token)`, backticks, `xargs printenv`, `cat ~/.aws/credentials`, a relative `../.env` with cwd,
    a symlink into `~/.ssh`, `curl -H "Authorization: Bearer $T"`, `https://u:p@host`, `?X-Amz-Signature=`,
    a `headers` object key.
  - Negatives: `credentials-store.ts`, `grep -r printenv docs/`, `env FOO=1 make test`, `echo $HOME`,
    `known_hosts`, `id_rsa.pub`, `.env.example` once the user allows it.
  - The stub carries no matched text: a planted secret never appears anywhere in the file bytes.
  - User deny/allow precedence; project allow ignored; malformed rules ignored; a >64 KiB rules file ignored.
- `test_evidence_fuzz.py`: seeded, deterministic by default; `CARDINAL_FUZZ_ITERS` for long runs.
  - 3,000 random calls: `tool_name` from a hostile alphabet (unicode, `__`, `:`, spaces, empty); `tool_input`
    and `response` random JSON trees (depth ≤ 10, width ≤ 8), plus planted hazards (lone surrogates, NUL, PEM
    blocks with and without footer, JWT/ghp_/AKIA/xox shapes, `password=`, `{name,value}` pairs, headers objects,
    base64 runs, 3 MiB strings, 5,000-deep nesting, spill notices pointing outside the root), random
    success/failure and random runtime.
  - Invariants:
    - (a) never raises, and the hook exits 0 with empty stderr;
    - (b) the file is valid UTF-8 JSON and 0600;
    - (c) args ≤ 64 KiB and result ≤ 256 KiB (or the envelope);
    - (d) no planted secret substring in the file;
    - (e) `wire_item(entry)` passes a Python port of maestro `EvidenceItemSchema` + `IDENT_RE`, checked against
      shared vectors `core/tests/testdata/evidence_wire_vectors.json` that conductor's test also reads;
    - (f) ≤ 1.5 s per call;
    - (g) a withheld verdict is stable under key reordering.
- `test_evidence_spool_bounds.py`:
  - The 256 MiB cap evicts oldest-first across sessions; the per-session cap applies.
  - TTL still works; `token.json`/`.hinted` survive size eviction.
  - Concurrent writers race safely; a stale temp file is reaped.
- `test_evidence.py` (existing): the scrub parity vectors still pass; new plain-text vectors are added.
- Perf smoke: a 10 MiB Bash stdout finishes in < 1 s; a median typical payload < 100 ms (generous; guards
  regressions).
- `evidence_id` vectors are shared with the JS bridge (`node --test` in `common/native`).

**adapters/claude/tests**:
- `test_evidence_capture.py` updated. hooks.json registers `evidence-capture.py` on PostToolUse **and**
  PostToolUseFailure with matcher `".*"`, sync, timeout ≤ 2. It covers:
  - Bash/Read/unknown capture;
  - the `.env` read giving a withheld line;
  - a first-vs-later context line, and `CARDINAL_EVIDENCE_CONTEXT=0`;
  - fail-open (garbage stdin, read-only HOME, the timer guard);
  - no network code.
- `test_evidence_promote.py`:
  - A builtin entry sends `source_server:"builtin:claude-code"`.
  - A withheld entry is refused locally, and zero HTTP requests are made (mock opener).
  - v1 entries still promote.
  - `list --tool/--grep`, `find` and `show` are covered.
- `test_storyboard_skill.py` pins the new phrases ("Any tool result", "withheld", "never claim more").

**cursor / gemini / codex tests**: fixture payloads for a non-MCP tool (Cursor `Shell` JSON-string `tool_output`,
Gemini `run_shell_command` llmContent, Codex `Bash` string) are captured. A failure is captured (Cursor
`postToolUseFailure`, Gemini `error`). `cardinal-connect` writes the new registrations. Existing MCP tests stay
green with an updated context line.

**pi / opencode**: `node --test` for the evidence event mapping and id parity. `cardinal_native.py evidence`
writes the entry.

**conductor** (targeted only, never `make check`):
- `npx vitest run src/storyboard/__tests__/evidence-upload*.test.ts src/storyboard/__tests__/grammar*.test.ts`
  in maestro;
- ui-pages `EvidenceDrawer.test.tsx`;
- `go test ./storyboard/receipts/ -run External`.

---

## 9. Migration of the MCP-only behaviour

| before | after |
|---|---|
| hooks.json matcher `mcp__.*` (PostToolUse, PostToolUseFailure) | `".*"` for both; same script, same 2 s sync |
| built-in and non-MCP calls ignored | captured (or withheld stub) |
| Gemini drops failed calls | captured with `status=error` |
| `normalize()` dict branch on any `content` key | MCP-content reading only on the MCP shape; `generic` otherwise |
| leaf-rule scrub only | + plain-text key=value rules, sensitive-path lines, base64 omission, cwd/home path scrub (stricter; the gateway re-scrubs) |
| spool TTL only | + 256 MiB total, 10k/session, gc every 10 min |
| v1 entries | still listed/promoted; v2 written from now on |
| context line `[evidence:ev_…] captured locally from s/t; to cite … promote ev_…` on every call | full hint once per session, then `[evidence:ev_…]` |
| promote in Claude bin only | logic in core; thin launchers per adapter |
| Cardinal gateway tools skipped | still skipped (dedupe by *source*); token capture moves into core |
| **plugin version** | **not bumped** (release gated on Keycloak/OAuth) |

Existing tests that pin MCP-only behaviour get updated, not deleted. They are
`test_cardinal_gateway_tools_and_non_mcp_tools_are_skipped` (the non-MCP half flips) and
`test_registered_synchronously_on_every_mcp_tool_with_a_short_timeout`.

---

## 10. PR plan

- **cardinal-agent-plugins** `feat/generic-evidence-capture` (one PR; commits in this order):
  1. core tests (gate, capture_call, fuzz, bounds);
  2. core implementation;
  3. Claude hook, hooks.json and CLI;
  4. Cursor/Gemini/Codex;
  5. pi/opencode bridge;
  6. promote moved to core, plus launchers;
  7. skill/README/CORE_GAPS text.

  No version bump. Remove the `bash-capture` worktree and the `feat/bash-evidence-capture` branch first (it has
  nothing to reuse).
- **conductor** `feat/generic-evidence-capture` (one PR): grammar text, EvidenceDrawer label, acceptance tests,
  spec §17/§18. The PR body flags that charts needs nothing and that a cardinal-docs page is recommended.
  Merge per the CI policy. Patch release only after v1.97.19 is live in prod.
- Open items to verify during implementation, each with a debug-payload dump:
  - Codex PostToolUse coverage of MCP and `apply_patch`, and its all-tools matcher syntax;
  - the Cursor `postToolUseFailure` payload;
  - pi's result-modifying event;
  - the opencode error path.

---

## 11. Implementation notes (where the code differs from the design above)

- **Gate false positives, refined (still tool-neutral).** A bare word with no `/`, no leading `~` or `.`, and no
  extension (`secrets`, `credential`) counts as a path only when a file of that name exists in the call's cwd, so
  `kubectl get secrets` / `git credential fill` are left to the command rules and `git commit -m "fix secret
  handling"` is not withheld. In a string holding prose or file content (it has a newline or is longer than 1 KiB)
  only explicit paths (with `/` or a leading `~`) count, and the header rule needs an explicit `-H`/`--header`, so a
  README that says "copy .env" or code that builds an `Authorization` header is not withheld. Commands are still
  parsed in multi-line strings (scripts). `.env.example` is withheld by default (allow-list `path.dotenv` or the path).
- **Base64 omission** needs the whole alphabet (upper, lower, digit and `+/` or `-_`) or a `data:` URL, so a hex
  listing (`git log --format=%H`) or a run of one letter is kept.
- **JSON text in a result** (a `kubectl -o json` stdout) is decoded and its leaves get the plain-text rules too; the
  text is kept exactly as written unless a rule changed something.
- **No claimed exit code.** The shell normalizer records `exit_code` only when the runtime reports one (a
  PostToolUseFailure "Exit code N", Cursor's `exitCode`); a Claude Code Bash success does not claim 0.
- **Evidence-token capture stays in the Claude adapter** (`hooks/storyboard-token.py`); `observe_cardinal_result()`
  was not moved into core. Promote itself moved to core (`evidence_promote.py`); every adapter's launcher uses the
  connection's MCP key, Claude additionally the storyboard's evidence token.
- **Codex:** a second PostToolUse group (`matcher ".*"`, `--event ToolEvidence`, timeout 3 s) runs capture; the
  `Bash` group stays the decision emitter unchanged. The event name avoids the substring `PostToolUse` so
  `post_tool_use_registered` keeps meaning "the decision emitter is registered".
- **Launchers:** Cursor, Codex and Gemini get `scripts/cardinal-evidence`; their context line names it by absolute
  path (it is not on PATH). pi/opencode: `cardinal-pi evidence …` / `cardinal-opencode evidence …`.
- **pi:** capture and the inline id both come from pi's result-modifying `tool_result` event (verified against the
  published `@earendil-works/pi-coding-agent` 0.85.1 types: `ToolResultEventResult.content`), not
  `tool_execution_start/end`. The first-capture hint is tracked per plugin process, not with `.hinted`.
- **opencode:** success from `tool.execute.after` (its `input.args` carries the arguments in 1.18.30), failure from
  `message.part.updated` with `state.status == "error"` (`ToolStateError {input, error: string}`, verified against the
  published SDK types). A failed call has no output to append an id to; `cardinal-opencode evidence list` shows it.
- **Cursor `postToolUseFailure`:** registered; the payload's error field is not verified against a live build. The
  hook reads `error` / `error_message` / `errorMessage` / `failure` / `message`, then `tool_output`.
- **Test isolation:** the Cursor golden-parity and decision tests run with `CARDINAL_EVIDENCE_CAPTURE=0` (the goldens
  pin the pre-migration telemetry contract); evidence has its own tests. The native JS fixtures set `HOME` to their
  temp dir so capture never writes to the developer's real spool.

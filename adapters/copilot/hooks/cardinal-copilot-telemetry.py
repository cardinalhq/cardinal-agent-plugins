#!/usr/bin/env python3
"""Emit Cardinal agent-session telemetry from GitHub Copilot CLI hooks.

Monorepo adapter over `cardinal_core` (vendored next to this file by
build/vendor.py). Core owns the algorithms (initiative resolution, bash
classification, OTLP record building/emission, limits primitives, session
counters, the convention prompt, the Investigation and storyboard layers).
This adapter owns everything Copilot-specific:

  * the hook contract of Copilot CLI's native (camelCase) events, as the
    plugin's hooks/hooks.json registers them (docs.github.com/en/copilot/
    reference/copilot-cli-reference/cli-hooks-reference): sessionStart,
    userPromptSubmitted, preToolUse, postToolUse, postToolUseFailure,
    agentStop, subagentStop, preCompact. Payloads are camelCase (sessionId,
    cwd, toolName, toolArgs, toolResult{resultType, textResultForLlm}); the
    VS Code-compatible snake_case spellings are read as fallbacks;
  * tool names: Copilot names MCP tools `<server>-<tool>` (the bundled
    `github-mcp-server-search_code`) and its own tools bash / view / edit /
    create / ...; canonical_tool_name rewrites an MCP name to the
    `mcp__<server>__<tool>` shape core parses, against the servers this
    machine configures;
  * context channels: only sessionStart and postToolUse /
    postToolUseFailure `additionalContext` reach the model.
    userPromptSubmitted output is dropped for config-file hooks, so what a
    prompt produces (a spend-limit warning, the discovery re-look, an edit
    lookup) is STAGED and surfaces at the next tool boundary, and agentStop
    never blocks (R1 of docs/plans/cross-agent-parity.md: Copilot gives no
    guarantee the reason re-enters as hook text);
  * the spend-limits gate: a block is enforced on preToolUse
    (`permissionDecision: deny`), the only Copilot hook that can stop work;
    warn/notify are staged on userPromptSubmitted and surface on the next
    postToolUse. CARDINAL_COPILOT_STRICT_WARN=1 escalates a warn to a deny;
  * hooks/hooks.json runs every entry as `python3 ... || true`: a non-zero
    exit of a preToolUse hook DENIES the tool on Copilot, so this script
    must never be the reason a tool call fails;
  * local evidence capture on postToolUse and postToolUseFailure, every tool
    call (except Cardinal's own gateway) through the shared pipeline
    (cardinal_core.evidence_capture), its id handed back as context;
  * the Investigation session (cardinal_core.investigation_agent) and
    storyboard authoring (cardinal_core.storyboard_authoring), as on Cursor;
  * unconnected mode: the hooks ship with the plugin and so run before
    `cardinal-connect`. Until then only local evidence capture and the
    one-time connect hint run: no telemetry, no network, no other context.

There is NO turn_usage / api_request emission: no hook payload carries token
counts, and Copilot's per-call `assistant.usage` events are ephemeral (never
written to the session's events.jsonl). Failures are best-effort and silent:
telemetry must not break the agent loop.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

START = time.monotonic()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import _decision_cli  # noqa: E402
    import _plugin_version  # noqa: E402
    from cardinal_core import decisions, evidence, limits, otlp, session  # noqa: E402
    from cardinal_core.bashclass import classify_bash_command  # noqa: E402
    from cardinal_core.initiative import (  # noqa: E402
        canonical_repo,
        detect_command,
        git,
        is_git_repo,
        resolve_initiative,
    )
    from cardinal_core.paths import AgentPaths  # noqa: E402
except Exception:
    # An install without its vendored core: exit 0 with no output, so a
    # preToolUse hook never denies a tool call on its account.
    sys.exit(0)

RUNTIME = "copilot"
PLUGIN_VERSION = _plugin_version.plugin_version()
SCOPE_NAME = "cardinal-copilot-plugin"

PATHS = AgentPaths(home=Path.home() / ".copilot")

STRICT_WARN_ENV = "CARDINAL_COPILOT_STRICT_WARN"

# Env-gated raw-payload dump for shape capture. Off by default; writes
# nothing unless CARDINAL_COPILOT_DEBUG_PAYLOADS=1 (to
# ~/.copilot/cardinal/telemetry/debug/<event>-<ns>.json).
DEBUG_PAYLOADS_ENV = "CARDINAL_COPILOT_DEBUG_PAYLOADS"

# Copilot's built-in tools -> the display name on cardinal.turn_tool /
# tool_result (the Claude name Copilot itself maps them to for PascalCase
# hooks) and the argument holding the file path.
TOOL_NAMES = {
    "bash": "Bash",
    "powershell": "Bash",
    "view": "Read",
    "create": "Write",
    "edit": "Edit",
    "str_replace_editor": "Edit",
    "apply_patch": "Edit",
    "grep": "Grep",
    "rg": "Grep",
    "glob": "Glob",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "ask_user": "AskUserQuestion",
    "update_todo": "TodoWrite",
    "task": "Agent",
}
SHELL_TOOLS = ("bash", "powershell")
EDIT_TOOLS = ("create", "edit", "str_replace_editor", "apply_patch")
TARGET_KEY = "path"
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)

# Upper bound on the `gh pr view` call made on the synchronous
# userPromptSubmitted path (cache miss only; hits are file reads).
PR_RESOLVE_TIMEOUT_SEC = 1.5

DECISION_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-decision"
EVIDENCE_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-evidence"

# postToolUse does only local work; its network sends run in a detached
# `--background <spool>` child. Test-only: run that job inline instead.
BACKGROUND_INLINE_ENV = "CARDINAL_COPILOT_BACKGROUND_INLINE"
BACKGROUND_EMIT_TIMEOUT_SEC = 3.0

EXIT_CODE_RE = re.compile(r"exit(?:ed)?(?: with)?(?: exit)? code[:= ]+(-?\d+)", re.IGNORECASE)

# Cardinal's own MCP servers as cardinal-connect registers them in
# ~/.copilot/mcp-config.json. The `cardinal` gateway mints a *witnessed*
# receipt for every call, so evidence capture skips it (the only tool it
# skips).
CARDINAL_MCP_SERVERS = ("cardinal",)
KNOWN_MCP_SERVERS = ("cardinal-behavior", "cardinal", "github-mcp-server")


# ---------------------------------------------------------------------------
# Common utilities
# ---------------------------------------------------------------------------

def silent_exit() -> None:
    sys.exit(0)


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = payload.get(k)
        if v is not None:
            return v
    return None


def session_id_from_payload(payload: dict[str, Any]) -> str | None:
    value = _first(payload, "sessionId", "session_id")
    return value if isinstance(value, str) and value else None


def cwd_from_payload(payload: dict[str, Any]) -> str:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return os.getcwd()


def connected() -> bool:
    """Whether `cardinal-connect` ran on this machine (its state file). The
    hooks ship with the plugin, so they also run before that."""
    return PATHS.state_path.is_file()


def resource_attrs(state: dict[str, Any]) -> dict[str, str]:
    return otlp.resource_attrs(
        service_name="copilot",
        agent_runtime=RUNTIME,
        deployment_environment=state.get("deployment_environment"),
        user_email=state.get("user_email"),
        org=state.get("org_slug") or state.get("org_id"),
        plugin_version=PLUGIN_VERSION,
    )


def emit_records(records: list[dict[str, Any]], timeout: float = otlp.DEFAULT_TIMEOUT_SEC) -> None:
    if not records:
        return
    connection = otlp.connection_from_paths(PATHS)
    if connection is None:
        return
    otlp.emit_records(
        records,
        connection,
        resource_attrs(PATHS.read_state()),
        scope_name=SCOPE_NAME,
        scope_version=PLUGIN_VERSION,
        timeout=timeout,
    )


def log_record(event_name: str, attrs: dict[str, Any], ts_ns: int) -> dict[str, Any]:
    return otlp.log_record(event_name, attrs, ts_ns)


def read_plan_stamp() -> dict[str, Any]:
    return session.read_plan_stamp(PATHS)


def dump_debug_payload(event: str, payload: dict[str, Any]) -> None:
    """Env-gated raw hook-payload dump. No-op unless
    CARDINAL_COPILOT_DEBUG_PAYLOADS=1."""
    if os.environ.get(DEBUG_PAYLOADS_ENV) != "1":
        return
    try:
        PATHS.debug_dir.mkdir(parents=True, exist_ok=True)
        path = PATHS.debug_dir / f"{event}-{time.time_ns()}.json"
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def write_context(text: str) -> None:
    if text:
        sys.stdout.write(json.dumps({"additionalContext": text}))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Copilot tool normalization
# ---------------------------------------------------------------------------

def _decode_json_container(value: Any) -> Any:
    """toolArgs may arrive as a JSON-encoded string (the VS Code-compatible
    tool_input is documented as "parsed from JSON string when possible").
    Decode an object or array; return anything else as is."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                decoded = json.loads(text)
            except ValueError:
                return value
            if isinstance(decoded, (dict, list)):
                return decoded
    return value


def configured_mcp_servers(cwd: str | None = None) -> list[str]:
    """MCP server names this machine configures for Copilot: the user's
    mcp-config.json ($COPILOT_HOME or ~/.copilot) and the workspace's
    .mcp.json / .github/mcp.json. Never raises."""
    files = []
    custom = os.environ.get("COPILOT_HOME")
    base = Path(custom) if custom and os.path.isabs(custom) else Path.home() / ".copilot"
    files.append(base / "mcp-config.json")
    if cwd:
        files.extend([Path(cwd) / ".mcp.json", Path(cwd) / ".github" / "mcp.json"])
    names: list[str] = []
    for f in files:
        try:
            servers = json.loads(f.read_text()).get("mcpServers")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(servers, dict):
            names.extend(k for k in servers if isinstance(k, str) and k)
    return names


def canonical_tool_name(raw: Any, cwd: str | None = None) -> str:
    """Copilot's tool name in the `mcp__<server>__<tool>` shape core parses
    when it is an MCP tool (`<server>-<tool>` for a server this machine
    configures, longest server name first, so `cardinal-behavior-x` is never
    taken for `cardinal`'s tool `behavior-x`); otherwise the name as is."""
    if not isinstance(raw, str) or not raw:
        return ""
    if raw.startswith("mcp__") or raw in TOOL_NAMES:
        return raw
    servers = set(KNOWN_MCP_SERVERS)
    servers.update(configured_mcp_servers(cwd))
    for server in sorted(servers, key=len, reverse=True):
        prefix = server + "-"
        if raw.startswith(prefix) and len(raw) > len(prefix):
            return f"mcp__{server}__{raw[len(prefix):]}"
    return raw


def tool_args(payload: dict[str, Any]) -> Any:
    return _decode_json_container(_first(payload, "toolArgs", "tool_input", "toolInput"))


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    value = _decode_json_container(_first(payload, "toolResult", "tool_result"))
    return value if isinstance(value, dict) else {}


def result_text(result: dict[str, Any]) -> str | None:
    text = _first(result, "textResultForLlm", "text_result_for_llm")
    return text if isinstance(text, str) else None


def result_success(result: dict[str, Any]) -> str:
    """"true"/"false" from toolResult.resultType ("success" / "failure" /
    "rejected" / "denied"), then a shell exit code in the text; missing ->
    "true" (fail-open on ambiguity)."""
    kind = _first(result, "resultType", "result_type")
    if isinstance(kind, str) and kind and kind != "success":
        return "false"
    text = result_text(result)
    m = EXIT_CODE_RE.search(text) if text else None
    if m:
        return "true" if m.group(1) == "0" else "false"
    return "true"


def normalize_tool(raw_name: str, canonical: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(display_name, extras) for cardinal.turn_tool / tool_result."""
    if raw_name in SHELL_TOOLS:
        cmd = str(args.get("command") or "")
        return "Bash", {"full_command": cmd, "bash_command": cmd.split(" ", 1)[0] if cmd else ""}
    if canonical.startswith("mcp__"):
        server, _, tool = canonical[len("mcp__"):].partition("__")
        return "mcp_tool", {"mcp_server_name": server, "mcp_tool_name": tool}
    return TOOL_NAMES.get(raw_name, raw_name), {}


def edited_paths(raw_name: str, args: Any, cwd: str) -> list[str]:
    """Absolute paths a successful file-editing tool call wrote: `path` of
    create / edit / str_replace_editor, the `*** Add|Update|Delete File:`
    lines of an apply_patch input."""
    if raw_name not in EDIT_TOOLS:
        return []
    out: list[str] = []
    if isinstance(args, dict):
        path = args.get(TARGET_KEY)
        if isinstance(path, str) and path:
            out.append(path)
        patch = args.get("input") or args.get("patch")
    else:
        patch = args
    if raw_name == "apply_patch" and isinstance(patch, str):
        out.extend(m.strip() for m in PATCH_FILE_RE.findall(patch))
    return [p if os.path.isabs(p) else os.path.join(cwd, p) for p in dict.fromkeys(out) if p]


# ---------------------------------------------------------------------------
# Evidence capture (postToolUse / postToolUseFailure)
# ---------------------------------------------------------------------------

def failure_text(payload: dict[str, Any]) -> str | None:
    v = payload.get("error")
    if isinstance(v, str) and v.strip():
        return v
    if isinstance(v, dict):
        m = v.get("message")
        if isinstance(m, str) and m.strip():
            return m
    return result_text(tool_result(payload))


def evidence_call(payload: dict[str, Any], failed: bool = False):
    """The cardinal_core.evidence_capture.ToolCall of one postToolUse /
    postToolUseFailure payload, or None. Never raises."""
    try:
        from cardinal_core import evidence_capture as cap

        raw_name = _first(payload, "toolName", "tool_name")
        if not isinstance(raw_name, str) or not raw_name:
            return None
        cwd = cwd_from_payload(payload)
        name = canonical_tool_name(raw_name, cwd)
        source, tool = cap.classify_mcp_name(name, RUNTIME, CARDINAL_MCP_SERVERS)
        error = failure_text(payload) if failed else None
        if failed and error is None:
            return None
        # The result's text (an MCP result's JSON decoded, so the spool keeps
        # its structure), else the whole toolResult.
        result = tool_result(payload)
        text = result_text(result)
        response = None
        if not failed:
            response = _decode_json_container(text) if text is not None else (result or None)
        tuid = _first(payload, "toolCallId", "tool_call_id", "toolUseId", "tool_use_id")
        return cap.ToolCall(
            runtime=RUNTIME,
            tool_name=raw_name,
            source=source,
            tool=tool,
            tool_input=tool_args(payload),
            response=response,
            error=error,
            session_id=session_id_from_payload(payload),
            tool_use_id=tuid if isinstance(tuid, str) else None,
            cwd=cwd,
            client=evidence.client_string(RUNTIME, None),
        )
    except Exception:
        return None


def capture_evidence(payload: dict[str, Any], failed: bool = False, call: Any = None) -> str | None:
    """Record one tool call in the local evidence spool; return its context
    line (`[evidence:ev_...]`), or None. Local file work only; never raises."""
    try:
        from cardinal_core import evidence_capture as cap

        if call is None:
            call = evidence_call(payload, failed)
        if call is None:
            return None
        got = cap.capture_call_guarded(call, Path.home(), promote_cmd=shlex.quote(str(EVIDENCE_CLI)))
        return got.line if got is not None else None
    except BaseException:
        return None


def failure_line(call: Any, *, succeeded: bool | None = None) -> str | None:
    """investigation_failures.observe: a throttled line when a shell call that
    writes to the Investigation could not reach Cardinal. Never raises."""
    try:
        if call is None:
            return None
        from cardinal_core import investigation_failures

        return investigation_failures.observe(Path.home(), call.session_id, call.tool_input, call.response,
                                              call.error, runtime=RUNTIME, tool_name=call.tool_name,
                                              succeeded=succeeded)
    except Exception:
        return None


def handle_post_tool_use_failure(payload: dict[str, Any]) -> None:
    """A failed tool call is evidence too: captured with status "error", its
    id returned as additionalContext with the tool boundary's deliveries."""
    dump_debug_payload("postToolUseFailure", payload)
    call = evidence_call(payload, failed=True)
    lines = [x for x in (failure_line(call) if connected() else None,
                         capture_evidence(payload, failed=True, call=call)) if x]
    sid = session_id_from_payload(payload)
    if sid and connected():
        lines.extend(boundary_contexts(payload, sid))
    write_context("\n\n".join(lines))


# ---------------------------------------------------------------------------
# Spend limits. Core's limits.gate_decision() owns the policy walk; this
# adapter renders it into Copilot's channels: a block (or a strict-warn
# escalation) denies the next tool call on preToolUse; warn/notify are staged
# (limits.stage_notify) on userPromptSubmitted and surface once on the next
# postToolUse.
# ---------------------------------------------------------------------------

def strict_warn_enabled() -> bool:
    return os.environ.get(STRICT_WARN_ENV) == "1"


def block_reason(sid: str) -> str | None:
    """The reason to deny tool calls with, or None (no block in force)."""
    d = limits.gate_decision(PATHS, sid)
    if d is None:
        return None
    if d.tier == "block":
        return d.reason or "A Cardinal spend limit for this work has been reached."
    if d.tier == "warn" and strict_warn_enabled():
        if d.user_message:
            return d.user_message
        verdict = limits.read_verdict(PATHS, sid) or {}
        fallback = verdict.get("block_reason") or "A Cardinal spend limit for this work has been reached."
        return f"[warn escalated to block via {STRICT_WARN_ENV}]\n{fallback}"
    return None


def stage_limits_notice(sid: str) -> None:
    """Stage a rising warn/notify band for the next postToolUse (hysteresis:
    only when the band rises; ack only what is staged)."""
    d = limits.gate_decision(PATHS, sid)
    if d is None or d.tier == "block" or (d.tier == "warn" and strict_warn_enabled()):
        return
    if not d.is_new_band:
        return
    parts: list[str] = []
    if d.agent_context:
        parts.append(d.agent_context)
    if d.tier == "warn" and d.user_message:
        parts.append(d.user_message)
    if parts:
        limits.stage_notify(PATHS, sid, "\n\n".join(parts), d.band)
        limits.ack_band(PATHS, sid, d.band)


def handle_pre_tool_use(payload: dict[str, Any]) -> None:
    """Deny the tool call while a Cardinal spend-limit block is in force
    (local file reads only: the cached verdict). Otherwise, on Cardinal's
    storyboard tools, fill an absent session_id / context
    (storyboard_stamp). Never answers "allow": Copilot's own permission
    flow still decides."""
    dump_debug_payload("preToolUse", payload)
    if not connected():
        return
    sid = session_id_from_payload(payload)
    try:
        reason = block_reason(sid) if sid else None
    except Exception:
        reason = None
    if reason:
        sys.stdout.write(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason}))
        sys.stdout.flush()
        return
    stamped = storyboard_stamp(payload, sid)
    if stamped is not None:
        sys.stdout.write(json.dumps({"modifiedArgs": stamped}))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# userPromptSubmitted: a new user turn, git_state, owner input, discovery
# re-look, the limits notice and the verdict refresh. Its output is dropped by
# Copilot, so nothing is printed.
# ---------------------------------------------------------------------------

def resolve_pr(cwd: str, repo: str | None, branch: str | None) -> tuple[int | None, str | None]:
    try:
        return decisions.resolve_pr(
            cwd, repo, branch, decisions.cache_dir(PATHS.runtime_dir),
            timeout=PR_RESOLVE_TIMEOUT_SEC,
        )
    except Exception:
        return None, None


def handle_user_prompt_submitted(payload: dict[str, Any]) -> None:
    dump_debug_payload("userPromptSubmitted", payload)
    if not connected():
        return
    sid = session_id_from_payload(payload)
    if not sid:
        return
    cwd = cwd_from_payload(payload)

    try:
        state = session.load_progress(PATHS, sid)
        session.begin_user_turn(state)
        session.save_progress(PATHS, sid, state)
    except Exception:
        pass
    try:
        stage_limits_notice(sid)
    except Exception:
        pass

    capture_owner_input(payload, sid, cwd)
    spawn_relook(sid, cwd)

    head_sha = git(["rev-parse", "HEAD"], cwd)
    branch = repo = None
    if head_sha:
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        remote_url = git(["remote", "get-url", "origin"], cwd)
        repo = canonical_repo(remote_url)
        initiative_name, initiative_type = resolve_initiative(branch)
        attrs: dict[str, Any] = {
            "session_id": sid,
            "cardinal_cwd": cwd,
            "cardinal_head_sha": head_sha,
            "cardinal_branch": branch,
            "cardinal_repo": repo,
            "cardinal_remote_url": remote_url,
            "cardinal_initiative_name": initiative_name,
            "cardinal_initiative_type": initiative_type,
            "cardinal_command": detect_command(payload.get("prompt")),
            **read_plan_stamp(),
        }
        pr_number, pr_url = resolve_pr(cwd, repo, branch)
        attrs["cardinal_pr_number"] = pr_number
        attrs["cardinal_pr_url"] = pr_url
        emit_records([log_record("cardinal.git_state", attrs, time.time_ns())])

    try:
        limits.maybe_refresh_verdict(PATHS, session_id=sid, repo=repo, branch=branch)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Decision capture (postToolUse). `cardinal-decision record` only validates
# and prints a marker line, so it works when Copilot's opt-in command sandbox
# denies network and writes; this hook gates, writes the ledger, resolves the
# PR and emits the one cardinal.decision event.
# ---------------------------------------------------------------------------

def _tool_output_texts(tool_output: Any) -> list[str]:
    texts: list[str] = []
    parsed: Any = tool_output
    if isinstance(tool_output, str):
        texts.append(tool_output)
        try:
            parsed = json.loads(tool_output)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict):
        for key in ("stdout", "output", "text", "textResultForLlm"):
            value = parsed.get(key)
            if isinstance(value, str):
                texts.append(value)
    return texts


_OPERATOR_CHARS = ";&|()\n"
_OPERATOR_SET = frozenset(_OPERATOR_CHARS)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_INTERPRETER_RE = re.compile(r"^python(\d+(\.\d+)*)?$")
_ENV_FLAGS_WITH_VALUE = frozenset({"-u", "--unset", "-C", "--chdir"})
_MENTIONS_RECORD_RE = re.compile(r"cardinal-decision[\"']?\s+record\b")


def _simple_commands(command: str) -> list[list[str]]:
    """Shell-aware split of `command` into the words of each simple
    command. `;`, `&`, `|`, `(`, `)` and newlines separate commands;
    quoting and escapes follow POSIX shlex; backslash-newline continues a
    line. Raises ValueError on unbalanced quotes."""
    text = command.replace("\\\r\n", " ").replace("\\\n", " ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_OPERATOR_CHARS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands: list[list[str]] = []
    words: list[str] = []
    for token in lexer:
        if token and set(token) <= _OPERATOR_SET:
            if words:
                commands.append(words)
                words = []
            continue
        words.append(token)
    if words:
        commands.append(words)
    return commands


def _record_argv(words: list[str]) -> list[str] | None:
    """argv (starting at `record`) when this simple command runs
    `cardinal-decision record`, directly or as `python3 [flags] <path>`,
    optionally behind `VAR=x` assignments and `env [flags] VAR=x`."""
    i, n = 0, len(words)
    while i < n:
        word = words[i]
        if _ASSIGNMENT_RE.match(word):
            i += 1
        elif os.path.basename(word) == "env":
            i += 1
            while i < n and words[i].startswith("-"):
                i += 2 if words[i] in _ENV_FLAGS_WITH_VALUE else 1
        else:
            break
    if i < n and _PYTHON_INTERPRETER_RE.match(os.path.basename(words[i])):
        i += 1
        while i < n and words[i].startswith("-") and words[i] != "-":
            flag = words[i]
            if flag.startswith("--"):
                i += 1
            elif flag[1] in "XW":
                i += 1 if len(flag) > 2 else 2
            elif "c" in flag[1:] or "m" in flag[1:]:
                return None  # python -c / -m: not running the script
            else:
                i += 1
    if i + 1 < n and os.path.basename(words[i]) == "cardinal-decision" and words[i + 1] == "record":
        return words[i + 1:]
    return None


def decision_invocations(command: str) -> tuple[list[list[str]], str | None]:
    """(argv per `cardinal-decision record` run in `command`, parse error)."""
    if "cardinal-decision" not in command:
        return [], None
    try:
        commands = _simple_commands(command)
    except ValueError as err:
        if _MENTIONS_RECORD_RE.search(command):
            return [], f"could not parse the shell command ({err})"
        return [], None
    return [argv for argv in (_record_argv(words) for words in commands) if argv], None


def confirmation_markers(tool_output: Any) -> list[dict[str, Any]]:
    """Every well-formed marker line in the tool output, in order."""
    out: list[dict[str, Any]] = []
    for text in _tool_output_texts(tool_output):
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith(_decision_cli.MARKER_PREFIX):
                continue
            try:
                obj = json.loads(line[len(_decision_cli.MARKER_PREFIX):])
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("v") == 1:
                out.append(obj)
    return out


def _marker_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _marker_list(value: Any, cap: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)][:cap]


def record_decision(sid: str, marker: dict[str, Any], payload: dict[str, Any]
                    ) -> tuple[str, dict[str, Any] | None]:
    """Local half of recording one confirmed decision: validate, assign the
    final id against the ledger, write the ledger. Returns the line for the
    agent and, when connected, the background job that resolves clusters +
    PR and emits the event."""
    cwd = _marker_str(marker.get("cwd"))
    if not cwd or not os.path.isdir(cwd):
        cwd = cwd_from_payload(payload)
    repo_root = git(["rev-parse", "--show-toplevel"], cwd)
    head_sha = branch = repo = None
    if repo_root:
        head_sha = git(["rev-parse", "HEAD"], cwd)
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        repo = canonical_repo(git(["remote", "get-url", "origin"], cwd))

    ledger = decisions.read_ledger(PATHS.runtime_dir, sid)
    by = marker.get("by")
    try:
        anchors = [
            decisions.parse_anchor(spec, repo_root, cwd)
            for spec in _marker_list(marker.get("anchor"), decisions.MAX_ANCHORS)
        ]
        decision = decisions.build_decision(
            choice=_marker_str(marker.get("choice")),
            question=_marker_str(marker.get("question")),
            rationale=_marker_str(marker.get("why")),
            decided_by=by if by in decisions.DECIDED_BY else "agent",
            alternatives=_marker_list(marker.get("alt"), decisions.MAX_ALTERNATIVES),
            follows_from=_marker_list(marker.get("follows"), decisions.MAX_LINKS),
            refines=_marker_list(marker.get("refines"), decisions.MAX_LINKS),
            supersedes=_marker_list(marker.get("supersedes"), decisions.MAX_LINKS),
            anchors=anchors,
            decision_id=_marker_str(marker.get("id")),
            existing=ledger,
        )
    except decisions.DecisionError as err:
        return f"Cardinal did NOT record the decision: {err}", None

    known = {entry["id"] for entry in ledger}
    unknown = [link["to"] for link in decision["links"] if link["to"] not in known]
    decisions.record_in_ledger(PATHS.runtime_dir, sid, decision)

    is_connected = otlp.connection_from_paths(PATHS) is not None
    job = None
    if is_connected:
        job = {
            "session_id": sid,
            "ts_ns": time.time_ns(),
            "decision": decision,
            "cwd": cwd,
            "repo_root": repo_root,
            "head_sha": head_sha,
            "repo": repo,
            "branch": branch,
        }
    lines = [f"Cardinal recorded decision {decision['id']}: {decision['choice']}"]
    if unknown:
        lines.append(
            f"Note: no earlier decision in this session has id {', '.join(unknown)}; "
            "the link was kept as given."
        )
    if not is_connected:
        lines.append("Cardinal telemetry isn't connected, so it was only saved locally.")
    return "\n".join(lines), job


def record_submitted_decisions(sid: str, command: str, tool_output: Any, payload: dict[str, Any]
                               ) -> tuple[list[str], list[dict[str, Any]]]:
    """(additionalContext lines, background decision jobs) for one tool call.
    The decision is derived from the argv of each real `cardinal-decision
    record` run in `command`; a marker line in the output only confirms that
    run, so spoofed markers are ignored wherever they appear."""
    invocations, parse_error = decision_invocations(command)
    if parse_error:
        return [f"Cardinal did NOT record the decision: {parse_error}."], []
    if not invocations:
        return [], []
    try:
        enabled = decisions.is_enabled(PATHS.runtime_dir, os.environ.get(decisions.ENABLE_ENV))
    except Exception as err:
        return [f"Cardinal did NOT record the decision: {_short_error(err)}"], []
    if not enabled:
        return [
            "Cardinal decision capture is off, so this decision was not recorded. "
            "Only the user can turn it on (`cardinal-decision on` in their own terminal)."
        ], []

    unused = confirmation_markers(tool_output)
    contexts: list[str] = []
    jobs: list[dict[str, Any]] = []
    for argv in invocations:
        try:
            expected = _decision_cli.expected_marker(argv)
        except (_decision_cli.ArgvError, decisions.DecisionError) as err:
            contexts.append(
                f"Cardinal did NOT record the decision: invalid cardinal-decision arguments "
                f"({_short_text(err)})."
            )
            continue
        label = (expected.get("choice") or "?")[:80]
        wanted = _decision_cli.comparable(expected)
        match = next((m for m in unused if _decision_cli.comparable(m) == wanted), None)
        if match is None:
            contexts.append(
                f'Cardinal did NOT record the decision "{label}": the command output has no '
                "matching confirmation line from cardinal-decision (did the command fail, or "
                "was its output cut off?)."
            )
            continue
        unused.remove(match)
        try:
            line, job = record_decision(sid, match, payload)
        except Exception as err:
            contexts.append(f'Cardinal did NOT record the decision "{label}": {_short_error(err)}')
            continue
        contexts.append(line)
        if job:
            jobs.append(job)
    return contexts, jobs


def _short_text(err: BaseException) -> str:
    return " ".join(str(err).split())[:160] or type(err).__name__


def _short_error(err: BaseException) -> str:
    text = " ".join(str(err).split())
    return f"{type(err).__name__}: {text[:160]}" if text else type(err).__name__


# ---------------------------------------------------------------------------
# Background work. The hook does only local work and hands every network
# send (turn_tool / tool_result, decision clusters + `gh` PR lookup + emit,
# the discovery re-look) to a detached child (own session, /dev/null stdio)
# via an atomically written 0600 spool file. Same pattern as Cursor/Gemini.
# ---------------------------------------------------------------------------

def spawn_background(job: dict[str, Any]) -> None:
    tmp = None
    try:
        spool_dir = PATHS.runtime_dir / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        base = f"{job.get('kind')}-{os.getpid()}-{time.time_ns()}"
        tmp = spool_dir / f".{base}.tmp"
        path = spool_dir / f"{base}.json"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(job, fh, default=str)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        return
    if os.environ.get(BACKGROUND_INLINE_ENV) == "1":
        run_background_job(path)
        return
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--background", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass


def run_background_job(path: Path) -> None:
    try:
        job = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if isinstance(job, dict) and job.get("kind") == "post_tool_use":
        post_tool_use_background(job)
    elif isinstance(job, dict) and job.get("kind") == "relook":
        relook_background(job)


def post_tool_use_background(job: dict[str, Any]) -> None:
    connection = otlp.connection_from_paths(PATHS)
    if connection is None:
        return
    resource = job.get("resource")
    if not isinstance(resource, dict):
        resource = resource_attrs(PATHS.read_state())

    def send(records: list[dict[str, Any]]) -> None:
        otlp.emit_records(
            records, connection, resource,
            scope_name=SCOPE_NAME, scope_version=PLUGIN_VERSION,
            timeout=BACKGROUND_EMIT_TIMEOUT_SEC,
        )

    records = job.get("records")
    if isinstance(records, list) and records:
        send(records)
    for item in job.get("decisions") or []:
        try:
            decision = item["decision"]
            cache = decisions.cache_dir(PATHS.runtime_dir)
            clusters, scheme = decisions.code_clusters(
                decision["anchors"], item.get("repo_root"), item.get("head_sha"), cache,
            )
            pr_number, pr_url = decisions.resolve_pr(
                item.get("cwd") or os.getcwd(), item.get("repo"), item.get("branch"), cache,
            )
            attrs = decisions.decision_attributes(
                session_id=item["session_id"],
                decision=decision,
                code_clusters=clusters,
                cluster_scheme=scheme,
                repo=item.get("repo"),
                branch=item.get("branch"),
                head_sha=item.get("head_sha"),
                pr_number=pr_number,
                pr_url=pr_url,
            )
            send([log_record(decisions.DECISION_EVENT, attrs, int(item.get("ts_ns") or time.time_ns()))])
        except Exception:
            continue


# ---------------------------------------------------------------------------
# postToolUse
# ---------------------------------------------------------------------------

# The edit lookup's one request ends by this (postToolUse has 15 s).
EDIT_LOOKUP_BUDGET_SEC = 1.5


def edit_contexts(sid: str | None, paths: list[str], cwd: str) -> list[str]:
    """Record the edited files in storyboard_files (`context.paths`), then the
    edit lookup for the first one (storyboards about that file's directory,
    the first edit there this session; one request, bounded). Never raises."""
    if not paths:
        return []
    try:
        from cardinal_core import storyboard_agent
        from cardinal_core import storyboard_authoring as sa

        wiring = storyboard_wiring()
        storyboard_agent.record_edits(wiring, sid, paths, cwd)
        if not sid:
            return []
        block = sa.edit_lookup(PATHS, RUNTIME, sid, paths[0], cwd, connection=wiring.connection,
                               client=wiring.client, deadline=START + EDIT_LOOKUP_BUDGET_SEC)
        return [block] if block else []
    except Exception:
        return []


def handle_post_tool_use(payload: dict[str, Any]) -> None:
    """Capture the tool call in the local evidence spool, then (connected)
    emit cardinal.turn_tool + tool_result, record submitted decisions and
    edited files, and return everything for the model as additionalContext
    (Copilot appends it after the tool output)."""
    dump_debug_payload("postToolUse", payload)
    call = evidence_call(payload)
    evidence_line = capture_evidence(payload, call=call)
    if not connected():
        write_context(evidence_line or "")
        return
    failure = failure_line(call)
    owned = authoring_entry_owns(payload)
    sid = session_id_from_payload(payload)
    cwd = cwd_from_payload(payload)
    if not sid:
        if not owned:
            write_context("\n\n".join(x for x in (failure, evidence_line) if x))
        return
    state = session.load_progress(PATHS, sid)

    raw_name = str(_first(payload, "toolName", "tool_name") or "")
    canonical = canonical_tool_name(raw_name, cwd)
    args_raw = tool_args(payload)
    args = args_raw if isinstance(args_raw, dict) else {}
    result = tool_result(payload)
    output_text = result_text(result)
    display_name, extra = normalize_tool(raw_name, canonical, args)
    target = args.get(TARGET_KEY) if display_name in ("Read", "Write", "Edit") else None
    if not (isinstance(target, str) and target):
        target = None

    plan_stamp = read_plan_stamp()
    now_ns = time.time_ns()
    records: list[dict[str, Any]] = []
    turn_tool_attrs: dict[str, Any] = {
        "session_id": sid,
        "ts": now_ns,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        "tool_name": display_name,
        "target": target,
        **plan_stamp,
    }
    if display_name == "mcp_tool":
        turn_tool_attrs["tool_name"] = canonical
        turn_tool_attrs["mcp_server_name"] = extra.get("mcp_server_name")
        turn_tool_attrs["mcp_tool_name"] = extra.get("mcp_tool_name")
    elif display_name == "Bash":
        classified = classify_bash_command(str(extra.get("full_command") or ""))
        if classified is not None:
            bash_class, bash_multi = classified
            turn_tool_attrs["bash_class"] = bash_class
            if bash_multi:
                turn_tool_attrs["bash_multi"] = True
    records.append(log_record("cardinal.turn_tool", turn_tool_attrs, now_ns))

    tool_result_attrs: dict[str, Any] = {
        "session_id": sid,
        "agent_runtime": RUNTIME,
        "tool_name": display_name,
        "success": result_success(result),
        "tool_input": json.dumps(args, separators=(",", ":")) if args else None,
    }
    if display_name == "mcp_tool":
        tool_result_attrs["mcp_server_name"] = extra.get("mcp_server_name")
        tool_result_attrs["mcp_tool_name"] = extra.get("mcp_tool_name")
    records.append(log_record("tool_result", tool_result_attrs, now_ns + 1))

    state["tool_seq"] += 1
    session.save_progress(PATHS, sid, state)

    contexts: list[str] = [x for x in (failure, evidence_line) if x]
    try:
        msg = None if owned else limits.consume_notify(PATHS, sid)
        if msg:
            contexts.append(msg)
    except Exception:
        pass
    decision_jobs: list[dict[str, Any]] = []
    if raw_name in SHELL_TOOLS:
        try:
            decision_contexts, decision_jobs = record_submitted_decisions(
                sid, str(args.get("command") or ""), output_text, payload
            )
            contexts.extend(decision_contexts)
        except Exception as err:
            contexts.append(f"Cardinal did NOT record the decision: {_short_error(err)}")
    if result_success(result) == "true":
        contexts.extend(edit_contexts(sid, edited_paths(raw_name, args_raw, cwd), cwd))
    observe_cardinal(payload, sid)
    if not owned:
        contexts.extend(boundary_contexts(payload, sid))
        write_context("\n\n".join(contexts))

    try:
        if otlp.connection_from_paths(PATHS) is not None:
            spawn_background({
                "kind": "post_tool_use",
                "resource": resource_attrs(PATHS.read_state()),
                "records": records,
                "decisions": decision_jobs,
            })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# preCompact, agentStop, subagentStop
# ---------------------------------------------------------------------------

def handle_pre_compact(payload: dict[str, Any]) -> None:
    """cardinal.plan_usage (compaction trigger). Copilot's preCompact
    carries only `trigger` (manual / auto) and `customInstructions`;
    downstream disambiguates on the presence of `plan.compact_trigger`."""
    dump_debug_payload("preCompact", payload)
    if not connected():
        return
    sid = session_id_from_payload(payload)
    if not sid:
        return
    attrs: dict[str, Any] = {
        "session_id": sid,
        "plan.compact_trigger": payload.get("trigger"),
        **read_plan_stamp(),
    }
    emit_records([log_record("cardinal.plan_usage", attrs, time.time_ns())])


# The agentStop entry's timeout is 10 s (hooks/hooks.json); the turn's end
# (sync check, owner-input flush) ends by this much after the hook started.
STOP_BUDGET_SEC = 7.0


def handle_agent_stop(payload: dict[str, Any]) -> None:
    """The turn's end of an Investigation session
    (investigation_agent.turn_end). Copilot can block here, but its reason
    is not documented to re-enter as hook-originated text, so (R1) this hook
    never blocks: turn_end STAGES what it would deliver for the next tool
    boundary or sessionStart, and the hook PRINTS NOTHING."""
    dump_debug_payload("agentStop", payload)
    if not connected():
        return
    sid = session_id_from_payload(payload)
    if not sid:
        return
    try:
        from cardinal_core import investigation_agent

        active = _first(payload, "stopHookActive", "stop_hook_active")
        investigation_agent.turn_end(storyboard_wiring(), sid, stop_hook_active=bool(active),
                                     deadline=START + STOP_BUDGET_SEC, cwd=cwd_from_payload(payload))
    except Exception:
        pass


def handle_subagent_stop(payload: dict[str, Any]) -> None:
    """cardinal.subagent_usage from Copilot's subagentStop (agentName,
    agentType, agentDisplayName, stopReason). No token or duration fields
    are documented."""
    dump_debug_payload("subagentStop", payload)
    if not connected():
        return
    sid = session_id_from_payload(payload)
    if not sid:
        return
    description = _first(payload, "agentDisplayName", "agent_display_name", "agentName", "agent_name")
    attrs = {
        "session_id": sid,
        "agent_runtime": RUNTIME,
        "subagent_type": _first(payload, "agentType", "agent_type", "agentName", "agent_name"),
        "subagent_description": description.strip()[:160] if isinstance(description, str) else None,
        "agent_id": _first(payload, "agentId", "agent_id"),
        "subagent_status": _first(payload, "stopReason", "stop_reason"),
        **read_plan_stamp(),
    }
    emit_records([log_record("cardinal.subagent_usage", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# sessionStart: initiative-convention prompt, budget standing, decision
# capture, the Investigation, storyboards that may relate, the Checks rule
# ---------------------------------------------------------------------------

CONVENTION_PROMPT = session.convention_prompt("Copilot CLI")


def decision_context(sid: str) -> str | None:
    """Decision-capture instructions for the session's start, or None when
    capture is off (the default). sessionStart is Copilot's only hook whose
    context reaches the model before the first prompt; postToolUse reports
    each recorded id back."""
    if not decisions.is_enabled(PATHS.runtime_dir, os.environ.get(decisions.ENABLE_ENV)):
        return None
    entries = decisions.read_ledger(PATHS.runtime_dir, sid)
    return (
        "Cardinal decision capture is on for this session. When you make a choice that "
        "constrains later work (picking between approaches, settling an open question, or "
        "the user deciding something), record it right away with one shell command:\n"
        f"python3 \"{DECISION_CLI}\" record --choice '<the option chosen, 2-7 words>' "
        "--question '<what had to be settled>' --why '<one sentence>' "
        "[--alt '<rejected option>']... [--by user] [--anchor <path>[::Symbol]]... "
        "[--follows|--refines|--supersedes <id>]\n"
        "Wrap every value in single quotes so the shell doesn't expand $ or backticks; "
        "if the hook sees different text than the command printed, it refuses to record. "
        "Run it from the workspace root. The command only checks the arguments and prints "
        "a line that Cardinal's hook records once the command finishes, so it needs no "
        "network or file access and works in Copilot's command sandbox. The hook then tells "
        "you the recorded id. Do not run `cardinal-decision on`, `off`, or `status`: those "
        "are for the user in their own terminal.\n"
        "Record choices, not progress, findings, or tool calls. Use --by user when the user "
        "made the call. Anchor the files or symbols the decision governs. Link a decision to "
        "an earlier one when it builds on, narrows, or replaces it.\n"
        "Decisions so far this session:\n"
        f"{decisions.render_ledger(entries)}"
    )


STORYBOARD_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-storyboard"
INVESTIGATION_POLLER = Path(__file__).resolve().parent / "investigation-poller.py"
# The plugin root the Checks preview template (checks/skill/) ships in.
CHECKS_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def storyboard_wiring():
    from cardinal_core import storyboard_agent

    return storyboard_agent.Wiring(RUNTIME, PATHS, PLUGIN_VERSION, cli=str(STORYBOARD_CLI))


# Context stamping (preToolUse): Copilot applies a returned `modifiedArgs` to
# the call. The four tools storyboard_agent.stamped_input fills (link's
# about refs are the model's).
STORYBOARD_STAMP_RE = re.compile(r"^mcp__cardinal__storyboard__(create|add_act|publish|find)$")


def storyboard_stamp(payload: dict[str, Any], sid: str | None) -> dict | None:
    """The call's arguments with an absent session_id / context filled
    (storyboard_agent.stamped_input) for Cardinal's storyboard__create /
    add_act / publish / find, else None (nothing added, another tool,
    CARDINAL_STORYBOARD_CONTEXT=0, or any failure)."""
    try:
        cwd = cwd_from_payload(payload)
        m = STORYBOARD_STAMP_RE.match(canonical_tool_name(_first(payload, "toolName", "tool_name"), cwd))
        if m is None:
            return None
        from cardinal_core import storyboard_agent

        return storyboard_agent.stamped_input(storyboard_wiring(), m.group(1), tool_args(payload), sid, cwd)
    except Exception:
        return None


# subagentStart's entry has 5 s; the stored discovery block needs no network.
SUBAGENT_START_BUDGET_SEC = 1.5


def handle_subagent_start(payload: dict[str, Any]) -> None:
    """The discovery block the session's last look stored, handed to the
    subagent (Copilot prepends subagentStart additionalContext to the
    subagent's prompt). No git, no network."""
    dump_debug_payload("subagentStart", payload)
    if not connected():
        return
    sid = session_id_from_payload(payload)
    if not sid:
        return
    try:
        from cardinal_core import storyboard_authoring as sa

        if sa.discovery_disabled(PATHS, RUNTIME):
            return
        wiring = storyboard_wiring()
        block = sa.relook_if_moved(PATHS, RUNTIME, sid, cwd_from_payload(payload), event="SubagentStart",
                                   connection=wiring.connection, client=wiring.client,
                                   deadline=START + SUBAGENT_START_BUDGET_SEC)
        write_context(block or "")
    except Exception:
        pass


def handle_session_start(payload: dict[str, Any]) -> None:
    dump_debug_payload("sessionStart", payload)
    started = time.monotonic()
    cwd = cwd_from_payload(payload)
    sid = session_id_from_payload(payload)
    # Copilot's sources are startup / resume / new (a /new session counts as
    # a startup for the once-per-machine hints).
    source = payload.get("source")
    source = "startup" if source in ("startup", "new") else source
    parts: list[str] = []
    try:
        from cardinal_core import storyboard_authoring as sa

        for line in (sa.stray_key_warning(PATHS, RUNTIME, sid=sid, source=source),
                     sa.connect_hint(PATHS, RUNTIME, source=source)):
            if line:
                parts.append(line)
    except Exception:
        pass
    if not connected():
        write_context("\n\n".join(parts))
        return
    if is_git_repo(cwd):
        context = CONVENTION_PROMPT
        try:
            standing = session.budget_standing(PATHS, sid, cwd)
            if standing:
                context = f"{CONVENTION_PROMPT}\n\n{standing}"
        except Exception:
            pass
        parts.append(context)
    if sid:
        try:
            decision = decision_context(sid)
            if decision:
                parts.append(decision)
        except Exception:
            pass
    try:
        # The Investigation (bootstrap: one request, its own 3 s bound), its
        # detached poller, what an earlier turn staged, then the session id
        # line (only without an Investigation: the guidance names it) + the
        # storyboards that may relate to this checkout, inside the hook's 10 s.
        from cardinal_core import investigation_agent, storyboard_agent

        wiring = storyboard_wiring()
        live = investigation_agent.start(wiring, sid, None, cwd=cwd) if sid else None
        if sid:
            investigation_agent.spawn_poller(wiring, sid, str(INVESTIGATION_POLLER))
        if live:
            parts.append(live)
        if sid:
            parts.extend(staged_contexts(wiring, sid))
        storyboard = storyboard_agent.session_start_text(
            wiring, cwd, sid, include_session_line=not bool(live),
            deadline=min(started + 8.0, time.monotonic() + 2.0))
        if storyboard:
            parts.append(storyboard)
    except Exception:
        pass
    try:
        # Checks preview (opt-in, `scripts/cardinal-checks on|off`): make the
        # installed skill match the setting (it takes effect at the next
        # session) and, while on and connected, the routing rule. Local only.
        from cardinal_core import checks_install
        from cardinal_core import storyboard_authoring as sa

        _, rule = checks_install.session_start(
            RUNTIME, CHECKS_PLUGIN_ROOT, connected=sa.is_connected(PATHS, RUNTIME))
        if rule:
            parts.append(rule)
    except Exception:
        pass
    write_context("\n\n".join(parts))


# ---------------------------------------------------------------------------
# Investigation sessions (cardinal_core.investigation_agent): bootstrap and
# guidance at sessionStart, deliveries at each tool boundary, the turn's end
# at agentStop. Whatever is produced where Copilot has no context channel is
# STAGED (investigation_turn.stage) and delivered at the next postToolUse /
# postToolUseFailure, or at a later sessionStart.
# ---------------------------------------------------------------------------

# Payload keys that would name a subagent. Copilot documents none on tool
# hooks; kept so a build that adds one keeps staged items and the inbox for
# the main thread.
SUBAGENT_KEYS = ("agentId", "agent_id", "parentToolCallId", "parent_tool_call_id")


def is_subagent_payload(payload: dict[str, Any]) -> bool:
    return any(isinstance(payload.get(k), str) and payload.get(k) for k in SUBAGENT_KEYS)


def stage_for_next_boundary(sid: str, text: str) -> None:
    try:
        from cardinal_core import investigation_turn

        investigation_turn.stage(Path.home(), sid, text)
    except Exception:
        pass


def staged_contexts(wiring: Any, sid: str) -> list[str]:
    try:
        from cardinal_core import investigation_agent, investigation_turn

        out = list(investigation_agent.prompt(wiring, sid).agent_context)
        out.extend(investigation_turn.consume_staged(Path.home(), sid))
        return out
    except Exception:
        return []


def boundary_contexts(payload: dict[str, Any], sid: str) -> list[str]:
    """A tool boundary: keep the poller running, then
    investigation_agent.deliver (staged items, the inbox, one re-surfaced
    challenge or directive) and whatever else is staged. A subagent's tool
    call gets nothing and consumes nothing. Local only. Never raises."""
    out: list[str] = []
    try:
        from cardinal_core import investigation_agent

        wiring = storyboard_wiring()
        investigation_agent.spawn_poller(wiring, sid, str(INVESTIGATION_POLLER))
        subagent = is_subagent_payload(payload)
        investigation_agent.deliver(wiring, sid, out.append, is_subagent=subagent)
        if not subagent:
            out.extend(staged_contexts(wiring, sid))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Owner input (userPromptSubmitted): cardinal_core.owner_input.capture.
# Copilot is in owner_input.CONSENT_REQUIRED: its sessionStart context reaches
# only the model and userPromptSubmitted output is dropped, so there is no
# user-visible disclosure per session, and nothing is captured until the user
# consented in their own terminal (`cardinal-connect --owner-input on`).
# Detached; nothing is printed and no prompt is blocked.
# ---------------------------------------------------------------------------

def capture_owner_input(payload: dict[str, Any], sid: str, cwd: str) -> str:
    """The capture status (a debug code, never the prompt). Never raises."""
    try:
        home = Path.home()
        sessions = home / ".cardinal" / "investigations" / "sessions"
        if "/" in sid or not (sessions / f"{sid}.json").exists():
            return "unbound"
        from cardinal_core import owner_input

        wiring = storyboard_wiring()
        agent_id = next((payload.get(k) for k in SUBAGENT_KEYS if payload.get(k)), None)
        got = owner_input.capture(home, sid, payload.get("prompt"), runtime=RUNTIME,
                                  conn=wiring.connection(), client=wiring.client, environ=wiring.environ,
                                  agent_id=agent_id if isinstance(agent_id, str) else None,
                                  deadline=START + owner_input.BUDGET, detached=True, cwd=cwd)
        return got.status
    except Exception:
        return "error"


# ---------------------------------------------------------------------------
# Discovery re-look (userPromptSubmitted): when the branch or HEAD moved since
# this session's last look, the storyboards that may relate to the checkout.
# Up to two requests, so it runs in the detached child and its block is STAGED.
# ---------------------------------------------------------------------------

RELOOK_BUDGET_SEC = 2.0


def spawn_relook(sid: str, cwd: str) -> None:
    try:
        from cardinal_core import storyboard_authoring as sa

        if sa.discovery_disabled(PATHS, RUNTIME) or not sa.is_connected(PATHS, RUNTIME):
            return
        spawn_background({"kind": "relook", "session_id": sid, "cwd": cwd})
    except Exception:
        pass


def relook_background(job: dict[str, Any]) -> None:
    sid, cwd = job.get("session_id"), job.get("cwd")
    if not isinstance(sid, str) or not sid:
        return
    from cardinal_core import storyboard_authoring as sa

    wiring = storyboard_wiring()
    block = sa.relook_if_moved(PATHS, RUNTIME, sid, cwd, connection=wiring.connection, client=wiring.client,
                               deadline=time.monotonic() + RELOOK_BUDGET_SEC)
    if block:
        stage_for_next_boundary(sid, block)


# ---------------------------------------------------------------------------
# Storyboard authoring (cardinal_core.storyboard_authoring). The preview render
# (Chromium: ~10 s for a short storyboard, up to core's 135 s budget) and the
# hero upload run in their own postToolUse entry: `--role authoring`, matcher
# `.*storyboard__(preview|publish)` (Copilot full-matches it against
# toolName), timeoutSec 150. Copilot joins every entry's additionalContext,
# so for those two tools the main entry stays silent (authoring_entry_owns)
# and the authoring entry carries the boundary's deliveries.
# ---------------------------------------------------------------------------

AUTHORING_ROLE = "authoring"
AUTHORING_TOOL_RE = re.compile(r"(?:^|__)storyboard__(preview|publish)$")


def authoring_entry_owns(payload: dict[str, Any]) -> bool:
    """A Cardinal storyboard__preview / storyboard__publish call (the plugin's
    hooks.json always registers the authoring entry)."""
    from cardinal_core import storyboard_authoring as sa

    name = canonical_tool_name(_first(payload, "toolName", "tool_name"), cwd_from_payload(payload))
    return bool(AUTHORING_TOOL_RE.search(name)) and sa.is_cardinal_tool(
        RUNTIME, name, ("storyboard__preview", "storyboard__publish"))


def observe_cardinal(payload: dict[str, Any], sid: str) -> None:
    """Store the evidence token of Cardinal's storyboard__create / add_act /
    preview, and hold this session's evidence after a successful
    storyboard__checkpoint. Local only; never raises."""
    try:
        from cardinal_core import storyboard_authoring as sa

        name = canonical_tool_name(_first(payload, "toolName", "tool_name"), cwd_from_payload(payload))
        sa.observe_cardinal_result(PATHS, RUNTIME, name,
                                   _decode_json_container(result_text(tool_result(payload))),
                                   session_id=sid)
    except Exception:
        pass


def handle_post_tool_use_authoring(payload: dict[str, Any]) -> None:
    """postToolUse `--role authoring`: after Cardinal's storyboard__preview,
    render the scenes, cover and link-preview mock locally (on_preview);
    after its storyboard__publish, upload the hero render (on_publish)."""
    dump_debug_payload("postToolUse-authoring", payload)
    if not connected():
        return
    from cardinal_core import storyboard_authoring as sa

    cwd = cwd_from_payload(payload)
    name = canonical_tool_name(_first(payload, "toolName", "tool_name"), cwd)
    if not AUTHORING_TOOL_RE.search(name):
        return
    sid = session_id_from_payload(payload)
    result = _decode_json_container(result_text(tool_result(payload)))
    text = None
    if sa.is_cardinal_tool(RUNTIME, name, "storyboard__preview"):
        args = tool_args(payload)
        text = sa.on_preview(PATHS, RUNTIME, result, session_id=sid,
                             tool_input=args if isinstance(args, dict) else None, started=START)
    elif sa.is_cardinal_tool(RUNTIME, name, "storyboard__publish"):
        wiring = storyboard_wiring()
        text = sa.on_publish(PATHS, RUNTIME, result, connection=wiring.connection, client=wiring.client)
    else:
        return
    contexts = [text] if text else []
    if sid:
        contexts.extend(boundary_contexts(payload, sid))
    write_context("\n\n".join(contexts))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

HANDLERS = {
    "sessionStart": handle_session_start,
    "userPromptSubmitted": handle_user_prompt_submitted,
    "preToolUse": handle_pre_tool_use,
    "postToolUse": handle_post_tool_use,
    "postToolUseFailure": handle_post_tool_use_failure,
    "agentStop": handle_agent_stop,
    "subagentStart": handle_subagent_start,
    "subagentStop": handle_subagent_stop,
    "preCompact": handle_pre_compact,
}
ROLE_HANDLERS = {
    ("postToolUse", AUTHORING_ROLE): handle_post_tool_use_authoring,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=False, default=None)
    parser.add_argument("--role", default=None)
    parser.add_argument("--background", default=None, help=argparse.SUPPRESS)
    try:
        args = parser.parse_args()
    except SystemExit:
        silent_exit()

    if args.background is not None:
        try:
            run_background_job(Path(args.background))
        except Exception:
            pass
        silent_exit()

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    event = args.event or payload.get("hookEventName") or payload.get("hook_event_name")
    if args.role is not None:
        handler = ROLE_HANDLERS.get((event, args.role)) if isinstance(event, str) else None
    else:
        handler = HANDLERS.get(event) if isinstance(event, str) else None
    if handler is None:
        silent_exit()

    try:
        handler(payload)
    except Exception:
        pass
    silent_exit()


if __name__ == "__main__":
    main()

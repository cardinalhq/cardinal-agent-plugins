#!/usr/bin/env python3
"""Emit Cardinal agent-session telemetry from Gemini CLI hooks.

Gemini CLI emits per-model-call and per-tool-call hook events directly
(unlike Codex which required transcript-JSONL scraping), so this hook
normalizes each event payload into the existing Cardinal/Lakerunner event
contract and POSTs OTLP/HTTP logs. Failures are best-effort and silent:
telemetry must not break the agent loop.

Monorepo adapter: all shared behavior (OTLP contract, initiative
resolution, bash classification, pricing, spend-limits delivery, session
counters) comes from the vendored `cardinal_core` package; this file keeps
only the Gemini-specific parts — payload-key probing (usage/usageMetadata
spellings), tool-name normalization, and event dispatch.

Event dispatch (see docs/specs/gemini-parity.md in the source repo for the
full mapping):

  SessionStart  → convention prompt + budget standing (additionalContext)
  BeforeAgent   → spend-limits gate + cardinal.git_state + verdict refresh
  AfterModel    → api_request + cardinal.turn_usage (per model call)
  AfterTool     → cardinal.turn_tool + tool_result (per tool call)
  AfterAgent    → cardinal.subagent_usage
  PreCompress   → cardinal.plan_usage (context-window slice)
  SessionEnd    → best-effort progress-file cleanup
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plugin_version  # noqa: E402
from cardinal_core import bashclass, initiative, limits, otlp, pricing, session  # noqa: E402
from cardinal_core import redaction  # noqa: E402
from cardinal_core.paths import AgentPaths  # noqa: E402
from cardinal_core.paths import dump_debug_payload as _core_dump_debug_payload  # noqa: E402


PLUGIN_VERSION = _plugin_version.plugin_version()
SCOPE_NAME = "cardinal-gemini-plugin"

PATHS = AgentPaths(home=Path.home() / ".gemini")
DEBUG_PAYLOADS_ENV = "CARDINAL_GEMINI_DEBUG_PAYLOADS"

TARGET_KEYS = {
    "read_file": "path",
    "write_file": "file_path",
    "edit": "file_path",
    "replace": "file_path",
    "read_many_files": "path",
    # Claude-style tool names sometimes appear via MCP passthrough:
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}


def silent_exit() -> None:
    sys.exit(0)


def session_id_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("session_id", "sessionId", "sessionID"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    value = os.environ.get("GEMINI_SESSION_ID")
    if value:
        return value
    return None


def emit_records(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    conn = otlp.connection_from_paths(PATHS)
    if conn is None:
        return
    state = PATHS.read_state()
    resource = otlp.resource_attrs(
        service_name="gemini-cli",
        agent_runtime="gemini",
        deployment_environment=state.get("deployment_environment"),
        user_email=state.get("user_email"),
        org=state.get("org_slug") or state.get("org_id"),
        plugin_version=PLUGIN_VERSION,
    )
    otlp.emit_records(
        records, conn, resource,
        scope_name=SCOPE_NAME, scope_version=PLUGIN_VERSION,
    )


def dump_debug_payload(event: str, payload: dict[str, Any]) -> None:
    """Env-gated raw hook-payload dump for shape capture. A no-op unless
    CARDINAL_GEMINI_DEBUG_PAYLOADS=1; best-effort like everything else.
    The write itself is the shared core.paths.dump_debug_payload
    primitive; this wrapper is just the env-var gate, which differs per
    adapter."""
    if os.environ.get(DEBUG_PAYLOADS_ENV) != "1":
        return
    _core_dump_debug_payload(event, payload, PATHS.debug_dir)


# ---------------------------------------------------------------------------
# BeforeAgent — closest analogue to Claude's UserPromptSubmit
# ---------------------------------------------------------------------------

def handle_before_agent(payload: dict[str, Any]) -> None:
    dump_debug_payload("BeforeAgent", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    cwd = str(payload.get("cwd") or os.getcwd())

    # Sync gate FIRST — its stdout is the hook's verdict channel and must
    # not wait on any network call below.
    try:
        gate_out = limits.gate_output(PATHS, session_id, hook_event_name="BeforeAgent")
        if gate_out:
            sys.stdout.write(json.dumps(gate_out))
            sys.stdout.flush()
    except Exception:
        pass

    # Turn boundary: user_turn_seq increments; per-turn counters reset.
    state = session.load_progress(PATHS, session_id)
    session.begin_user_turn(state)
    session.save_progress(PATHS, session_id, state)

    branch = None
    repo = None
    head_sha = initiative.git(["rev-parse", "HEAD"], cwd)
    if head_sha:
        branch = initiative.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        # PRIVACY (docs/privacy-redaction.md §3, §7): origin URLs can embed
        # live credentials — strip userinfo BEFORE canonical_repo() too,
        # since that regex has no userinfo awareness.
        raw_remote_url = initiative.git(["remote", "get-url", "origin"], cwd)
        remote_url = redaction.strip_url_userinfo(raw_remote_url) if raw_remote_url else None
        credential_scrubbed = bool(raw_remote_url and remote_url != raw_remote_url)
        repo = initiative.canonical_repo(remote_url)
        initiative_name, initiative_type = initiative.resolve_initiative(branch)
        attrs: dict[str, Any] = {
            "session_id": session_id,
            "cardinal_cwd": cwd,
            "cardinal_head_sha": head_sha,
            "cardinal_branch": branch,
            "cardinal_repo": repo,
            "cardinal_remote_url": remote_url,
            "cardinal_remote_url_credential_scrubbed": credential_scrubbed,
            "cardinal_initiative_name": initiative_name,
            "cardinal_initiative_type": initiative_type,
            "cardinal_command": initiative.detect_command(payload.get("prompt")),
            **session.read_plan_stamp(PATHS),
        }
        emit_records([otlp.log_record("cardinal.git_state", attrs, time.time_ns())])

    # Async half of the gate — refresh after the OTLP post, best-effort.
    try:
        limits.maybe_refresh_verdict(PATHS, session_id=session_id, repo=repo, branch=branch)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# AfterModel — per-model-call api_request + cardinal.turn_usage
# ---------------------------------------------------------------------------

def normalize_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Map Gemini's payload key spellings onto the Cardinal contract's
    canonical bucket names. Gemini variants observed / documented include
    `promptTokenCount`, `candidatesTokenCount`, `thoughtsTokenCount`,
    `cachedContentTokenCount`, `toolUsePromptTokenCount`.
    """
    def _int(*keys: str) -> int:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return {
        "input_tokens": _int("input_tokens", "prompt_tokens", "promptTokenCount"),
        "output_tokens": _int("output_tokens", "response_tokens", "candidatesTokenCount"),
        "thought_tokens": _int("thought_tokens", "thoughtsTokenCount"),
        "cached_input_tokens": _int(
            "cached_input_tokens", "cache_read_tokens",
            "cached_content_token_count", "cachedContentTokenCount",
        ),
        "tool_use_tokens": _int("tool_use_tokens", "toolUsePromptTokenCount"),
    }


def usage_attrs(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thought_tokens": usage.get("thought_tokens"),
        "cache_read_tokens": usage.get("cached_input_tokens"),
        "cache_read_input_tokens": usage.get("cached_input_tokens"),
    }


def handle_after_model(payload: dict[str, Any]) -> None:
    dump_debug_payload("AfterModel", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return

    # Gemini's AfterModel payload nests token usage under `usage` or
    # `usageMetadata` depending on version — probe both.
    raw_usage = payload.get("usage") or payload.get("usageMetadata") or {}
    if not isinstance(raw_usage, dict):
        return
    usage = normalize_usage(raw_usage)
    if not any(usage.values()):
        return

    model = payload.get("model") or payload.get("modelId") or payload.get("model_id")
    state = session.load_progress(PATHS, session_id)
    ts_ns = time.time_ns()

    # Update plan stamp if the payload surfaces plan/tier info.
    plan_type = payload.get("plan_type") or payload.get("planType")
    limit_tier = payload.get("rate_limit_tier") or payload.get("rateLimitTier")
    if isinstance(plan_type, str) or isinstance(limit_tier, str):
        stamp: dict[str, Any] = {}
        if isinstance(plan_type, str) and plan_type:
            stamp["plan_type"] = plan_type
        if isinstance(limit_tier, str) and limit_tier:
            stamp["rate_limit_tier"] = limit_tier
        if stamp:
            state["plan_stamp"] = stamp
            session.write_plan_stamp(PATHS, stamp)

    plan_stamp = state.get("plan_stamp") if isinstance(state.get("plan_stamp"), dict) else {}

    state_conn = PATHS.read_state()
    base = {
        "session_id": session_id,
        "user_email": state_conn.get("user_email"),
        "agent_runtime": "gemini",
        "model": str(model) if model else None,
        **usage_attrs(usage),
    }
    cost_usd = pricing.compute_cost_usd(
        str(model) if model else None, usage, pricing.GEMINI_PRICING_USD_PER_M
    )
    if cost_usd is not None:
        base["cost_usd"] = cost_usd

    records: list[dict[str, Any]] = []
    records.append(otlp.log_record("api_request", base, ts_ns))
    records.append(otlp.log_record("cardinal.turn_usage", {
        **base,
        "ts": ts_ns,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        **plan_stamp,
    }, ts_ns + 1))

    # plan_state: once per session; re-emit on value change.
    plan_sig = f"{plan_stamp.get('plan_type') or ''}|{plan_stamp.get('rate_limit_tier') or ''}"
    if plan_sig != "|" and plan_sig != state.get("plan_state_sig"):
        records.append(otlp.log_record("cardinal.plan_state", {
            "session_id": session_id,
            "agent_runtime": "gemini",
            "ts": ts_ns,
            "plan_type": plan_stamp.get("plan_type"),
            "rate_limit_tier": plan_stamp.get("rate_limit_tier"),
        }, ts_ns + 2))
        state["plan_state_sig"] = plan_sig

    emit_records(records)
    session.end_model_call(state)
    session.save_progress(PATHS, session_id, state)


# ---------------------------------------------------------------------------
# AfterTool — cardinal.turn_tool + tool_result
# ---------------------------------------------------------------------------

def parse_args_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def normalize_tool(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """Return (canonical tool_name, extra params, target)."""
    if name in {"run_shell_command", "shell", "bash"}:
        cmd = str(args.get("command") or args.get("cmd") or "")
        return "Bash", {"full_command": cmd, "bash_command": cmd.split(" ", 1)[0] if cmd else ""}, None
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else ""
        tool = parts[2] if len(parts) > 2 else name
        return "mcp_tool", {"mcp_server_name": server, "mcp_tool_name": tool}, None
    return name, {}, None


def handle_after_tool(payload: dict[str, Any]) -> None:
    dump_debug_payload("AfterTool", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    raw_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if not raw_name:
        return
    args = parse_args_json(payload.get("tool_input") or payload.get("toolInput") or payload.get("arguments"))
    tool_name, params, target = normalize_tool(raw_name, args)
    if target is None:
        key = TARGET_KEYS.get(tool_name) or TARGET_KEYS.get(raw_name)
        if key:
            v = args.get(key)
            if isinstance(v, str) and v:
                target = v
    # PRIVACY (docs/privacy-redaction.md §3 attributes.file_path): verbatim
    # only when the path resolves inside this session's cwd; hashed
    # placeholder otherwise.
    if target is not None:
        cwd = str(payload.get("cwd") or os.getcwd())
        target = redaction.redact_file_path(target, cwd)

    state = session.load_progress(PATHS, session_id)
    plan_stamp = state.get("plan_stamp") if isinstance(state.get("plan_stamp"), dict) else {}
    ts_ns = time.time_ns()

    attrs: dict[str, Any] = {
        "session_id": session_id,
        "ts": ts_ns,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        "tool_name": tool_name,
        "target": target,
        **plan_stamp,
    }
    if tool_name == "mcp_tool":
        # turn_tool carries the raw qualified MCP name (harvester's clustering
        # signal); tool_result keeps the normalized form.
        attrs["tool_name"] = raw_name
        attrs["mcp_server_name"] = params.get("mcp_server_name")
        attrs["mcp_tool_name"] = params.get("mcp_tool_name")
    elif tool_name == "Bash":
        classified = bashclass.classify_bash_command(str(params.get("full_command") or ""))
        if classified is not None:
            bash_class, bash_multi = classified
            attrs["bash_class"] = bash_class
            if bash_multi:
                attrs["bash_multi"] = True

    success = payload.get("success")
    if success is None:
        # Fall back to exit_code / status if present.
        exit_code = payload.get("exit_code") or payload.get("exitCode")
        if isinstance(exit_code, (int, float)):
            success = "true" if int(exit_code) == 0 else "false"
        else:
            status = payload.get("status")
            if isinstance(status, str):
                success = "true" if status.lower() in {"ok", "success", "completed"} else "false"
    if isinstance(success, bool):
        success_str = "true" if success else "false"
    elif isinstance(success, str):
        success_str = success.lower()
    else:
        success_str = "true"

    # PRIVACY REGRESSION FIX (docs/privacy-redaction.md §7): this event
    # used to serialize `tool_input`/`tool_parameters` as raw JSON of the
    # full tool-call arguments — for a shell tool the entire command
    # string, for a file-write tool the full path/content payload. Route
    # through the same redact_command/hash_field discipline
    # cardinal.turn_tool already uses, so the two events can't diverge
    # (spec §7 "must fix").
    result_attrs: dict[str, Any] = {
        "session_id": session_id,
        "agent_runtime": "gemini",
        "tool_name": tool_name,
        "success": success_str,
    }
    if tool_name == "mcp_tool":
        result_attrs["mcp_server_name"] = params.get("mcp_server_name")
        result_attrs["mcp_tool_name"] = params.get("mcp_tool_name")

    arg_redaction = redaction.redact_tool_args(tool_name, args)
    arg_patterns = arg_redaction.pop("secret_patterns", [])
    result_attrs.update(arg_redaction)

    tool_output = payload.get("output") or payload.get("result")
    out_redaction = redaction.redact_tool_output(tool_output)
    out_patterns = out_redaction.pop("secret_patterns", [])
    result_attrs.update(out_redaction)

    records = [
        otlp.log_record("cardinal.turn_tool", attrs, ts_ns),
        otlp.log_record("tool_result", result_attrs, ts_ns + 1),
    ]

    # Spec §3 "Secrets": human-auditable signal a known pattern was found
    # and scrubbed — never the matched text itself.
    detected: dict[str, list[str]] = {}
    if arg_patterns:
        detected["tool_input"] = arg_patterns
    if out_patterns:
        detected["output"] = out_patterns
    if detected:
        records.append(otlp.log_record("cardinal.secret_detected", {
            "session_id": session_id,
            "agent_runtime": "gemini",
            "event": "tool_result",
            "fields": ",".join(sorted(detected)),
            "secret_patterns": ",".join(sorted({p for ps in detected.values() for p in ps})),
        }, ts_ns + 1))

    emit_records(records)
    state["tool_seq"] += 1
    session.save_progress(PATHS, session_id, state)


# ---------------------------------------------------------------------------
# AfterAgent — subagent stop
# ---------------------------------------------------------------------------

def subagent_description_from_payload(payload: dict[str, Any]) -> str | None:
    """Best-effort extraction of the subagent's short task label. Task
    label only — free-text boundary widening capped at 160 chars, matching
    Claude v0.12.1's `subagent_description`."""
    candidates: list[Any] = [
        payload.get("description"),
        payload.get("task_description"),
        payload.get("taskDescription"),
        payload.get("prompt"),
        payload.get("label"),
    ]
    for input_key in ("tool_input", "toolInput"):
        tool_input = payload.get(input_key)
        if isinstance(tool_input, dict):
            candidates.append(tool_input.get("description"))
            candidates.append(tool_input.get("prompt"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:160]
    return None


def handle_after_agent(payload: dict[str, Any]) -> None:
    dump_debug_payload("AfterAgent", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return

    # Gemini's AfterAgent payload usage may nest under several keys.
    usage_block = (
        payload.get("usage")
        or payload.get("usageMetadata")
        or payload.get("tokens")
        or {}
    )
    if isinstance(usage_block, dict):
        total_tokens = (
            usage_block.get("total_tokens")
            or usage_block.get("totalTokenCount")
            or usage_block.get("total_token_count")
        )
    else:
        total_tokens = None
    if total_tokens is None:
        total_tokens = payload.get("total_tokens") or payload.get("totalTokens")

    attrs = {
        "session_id": session_id,
        "agent_runtime": "gemini",
        "subagent_type": (
            payload.get("subagent_type")
            or payload.get("subagentType")
            or payload.get("agent_type")
            or payload.get("agentType")
            or payload.get("matcher")
        ),
        "agent_id": payload.get("agent_id") or payload.get("agentId"),
        "subagent_description": subagent_description_from_payload(payload),
        # Cross-adapter contract key; best-effort — probe the payload and
        # its usage block for the child's model.
        "model": (
            payload.get("model")
            or payload.get("modelName")
            or payload.get("model_name")
            or (usage_block.get("model") if isinstance(usage_block, dict) else None)
        ),
        "total_tokens": total_tokens,
        "duration_ms": payload.get("duration_ms") or payload.get("durationMs"),
        "status": payload.get("status"),
        **session.read_plan_stamp(PATHS),
    }
    # Emit when we have ANY identifying facet — an untyped call with no
    # description, id, tokens, or duration is almost certainly a stray
    # payload (Gemini fires AfterAgent for the main agent too on some
    # versions); skipping those keeps the subagent_usage stream honest.
    identifying = any(
        attrs[k] is not None
        for k in ("subagent_type", "agent_id", "subagent_description",
                  "total_tokens", "duration_ms")
    )
    if not identifying:
        return
    emit_records([otlp.log_record("cardinal.subagent_usage", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# PreCompress — context-window compaction slice
# ---------------------------------------------------------------------------

def handle_pre_compress(payload: dict[str, Any]) -> None:
    dump_debug_payload("PreCompress", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    attrs = {
        "session_id": session_id,
        "agent_runtime": "gemini",
        "context_tokens": payload.get("context_tokens") or payload.get("contextTokens"),
        "context_window_size": payload.get("context_window_size") or payload.get("contextWindowSize"),
        "context_usage_percent": payload.get("context_usage_percent") or payload.get("contextUsagePercent"),
        "trigger": payload.get("trigger"),
        "messages_to_compact": payload.get("messages_to_compact") or payload.get("messagesToCompact"),
        "is_first_compaction": payload.get("is_first_compaction") or payload.get("isFirstCompaction"),
        "plan": {"compact_trigger": payload.get("trigger")} if payload.get("trigger") else None,
        **session.read_plan_stamp(PATHS),
    }
    # Flag disambiguation downstream: presence of `plan.compact_trigger`
    # distinguishes this from per-model-call plan_usage.
    if attrs["plan"]:
        attrs["plan.compact_trigger"] = attrs["plan"]["compact_trigger"]
    attrs.pop("plan", None)
    emit_records([otlp.log_record("cardinal.plan_usage", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# SessionStart — convention prompt + budget standing
# ---------------------------------------------------------------------------

def handle_session_start(payload: dict[str, Any]) -> None:
    dump_debug_payload("SessionStart", payload)
    cwd = str(payload.get("cwd") or os.getcwd())
    if not initiative.is_git_repo(cwd):
        return
    context = session.convention_prompt("Gemini CLI")
    try:
        standing = session.budget_standing(PATHS, session_id_from_payload(payload), cwd)
        if standing:
            context = f"{context}\n\n{standing}"
    except Exception:
        pass
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# SessionEnd — best-effort cleanup
# ---------------------------------------------------------------------------

def handle_session_end(payload: dict[str, Any]) -> None:
    dump_debug_payload("SessionEnd", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    # Retention: leave the per-session progress + verdict files behind.
    # cardinal-disconnect removes ~/.gemini/cardinal/ wholesale.
    return


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    args = parser.parse_args()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        if args.event == "SessionStart":
            handle_session_start(payload)
        elif args.event == "BeforeAgent":
            handle_before_agent(payload)
        elif args.event == "AfterModel":
            handle_after_model(payload)
        elif args.event == "AfterTool":
            handle_after_tool(payload)
        elif args.event == "AfterAgent":
            handle_after_agent(payload)
        elif args.event == "PreCompress":
            handle_pre_compress(payload)
        elif args.event == "SessionEnd":
            handle_session_end(payload)
    except Exception:
        pass
    silent_exit()


if __name__ == "__main__":
    main()

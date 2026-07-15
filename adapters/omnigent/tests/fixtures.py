"""Synthetic PolicyEvents matching the wire shape verified against the
pinned omnigent commit (docs/specs/omnigent-adapter.md §Verified
integration facts): the plain dict `FunctionPolicy._build_event`
produces — {type, target, data, context{actor, usage, user_daily_cost,
model, harness, labels, subtree_usage}, session_state, llm_client,
request_data}. There is NO session id in the contract; tests pass
`session_id=` to pre-seed `session_state["cardinal.session_id"]`
(simulating a persisted mint) so emissions are deterministic. Pass
`session_id=None` to exercise the minting path.

Also bootstraps sys.path for core + the adapter package and loads the
StubIngest golden harness from core/tests/harness.py by file path (no
package-name ambiguity with this tests/ directory).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

ADAPTER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ADAPTER_DIR.parents[1]
CORE_DIR = REPO_ROOT / "core"

for _p in (str(CORE_DIR), str(ADAPTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_by_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_stub_ingest():
    """core/tests/harness.py StubIngest, loaded by file path."""
    return _load_by_path("_cardinal_core_harness", CORE_DIR / "tests" / "harness.py").StubIngest


def load_contract_module():
    """Repo-root tests/test_contract.py — source of REQUIRED_KEYS."""
    return _load_by_path("_cardinal_contract", REPO_ROOT / "tests" / "test_contract.py")


# ---------------------------------------------------------------------------
# PolicyEvent fixtures (verified dict wire shape)
# ---------------------------------------------------------------------------

SESSION_ID_KEY = "cardinal.session_id"


def make_event(
    type: str,  # noqa: A002 — mirrors the wire key
    data: Any,
    *,
    target: str | None = None,
    session_id: str | None = "omni-sess-1",
    labels: dict[str, Any] | None = None,
    run_as: str | None = "dev@example.com",
    harness: str | None = "claude",
    model: str | None = None,
    total_cost_usd: float | None = None,
    session_state: dict[str, Any] | None = None,
    llm_client: Any = None,
    request_data: Any = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": 5000, "output_tokens": 900, "total_tokens": 5900,
    }
    if total_cost_usd is not None:
        usage["total_cost_usd"] = total_cost_usd
    state = dict(session_state or {})
    if session_id is not None:
        state.setdefault(SESSION_ID_KEY, session_id)
    event: dict[str, Any] = {
        "type": type,
        "target": target,
        "data": data,
        "context": {
            "actor": {"run_as": run_as, "client_id": "omnigent-runner"}
            if run_as else {},
            "usage": usage,
            "user_daily_cost": {},
            "model": model,
            "harness": harness,
            "labels": dict(labels or {}),
            "subtree_usage": {},
        },
        "session_state": state,
        "llm_client": llm_client,
    }
    if request_data is not None:
        event["request_data"] = request_data
    return event


def request_event(
    session_id: str | None = "omni-sess-1",
    *,
    labels: dict[str, Any] | None = None,
    prompt: str = "please fix the login crash",
    **kwargs: Any,
) -> dict[str, Any]:
    """`request` phase — data is the user message STRING (verified:
    the evaluate route passes the prompt text as content); labels carry
    the Cardinal attribution convention."""
    return make_event("request", prompt, session_id=session_id,
                      labels=labels, **kwargs)


DEFAULT_TURN_USAGE = {
    "input_tokens": 1200,
    "output_tokens": 300,
    "total_tokens": 1500,
    "context_tokens": 42000,
    "cache_read_input_tokens": 900,
    "cache_creation_input_tokens": 100,
    "model": "claude-sonnet-4-5",
}


def llm_response_event(
    session_id: str | None = "omni-sess-1",
    *,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """`llm_response` phase — data carries model / text_preview /
    tool_calls_count and optionally usage (per-TURN totals on the
    claude_sdk executor); context.usage is session-cumulative."""
    u = dict(DEFAULT_TURN_USAGE if usage is None else usage)
    if model is not None:
        u["model"] = model
    data = {
        "model": u.get("model"),
        "text_preview": "done.",
        "tool_calls_count": 0,
        "usage": u,
    }
    return make_event("llm_response", data, session_id=session_id, **kwargs)


def tool_call_shell_event(
    session_id: str | None = "omni-sess-1",
    *,
    command: str = "git status && ls -la",
    target: str = "bash",
    **kwargs: Any,
) -> dict[str, Any]:
    return make_event(
        "tool_call",
        {"name": target, "arguments": {"command": command}},
        target=target, session_id=session_id, **kwargs,
    )


def tool_call_mcp_event(
    session_id: str | None = "omni-sess-1",
    *,
    target: str = "mcp__lakerunner__execute_logs_query",
    **kwargs: Any,
) -> dict[str, Any]:
    return make_event(
        "tool_call",
        {"name": target, "arguments": {"query": "fingerprint=abc"}},
        target=target, session_id=session_id, **kwargs,
    )


def tool_result_event(
    session_id: str | None = "omni-sess-1",
    *,
    target: str = "bash",
    success: Any = True,
    exit_code: int | None = None,
    data: dict[str, Any] | None = None,
    request_data: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(data) if data is not None else {"result": "ok"}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    elif success is not None and "success" not in payload:
        payload["success"] = success
    return make_event("tool_result", payload, target=target,
                      session_id=session_id, request_data=request_data, **kwargs)


def sys_session_send_call_event(
    session_id: str = "omni-parent",
    *,
    agent: str = "claude_code",
    title: str = "task-1",
    input_text: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Parent-side ``sys_session_send`` tool_call — polly's dispatch. The
    child conversation id only appears on the paired tool_result; the call
    itself just names agent/title/args."""
    args: dict[str, Any] = {
        "agent": agent,
        "title": title,
        "args": {"purpose": "implement", "input": input_text},
    }
    return make_event(
        "tool_call",
        {"name": "sys_session_send", "arguments": args},
        target="sys_session_send", session_id=session_id, **kwargs,
    )


def sys_session_send_result_event(
    session_id: str = "omni-parent",
    *,
    child_conversation_id: str,
    input_text: str,
    agent: str = "claude_code",
    title: str = "task-1",
    **kwargs: Any,
) -> dict[str, Any]:
    """Parent-side ``sys_session_send`` tool_result — the JSON handle
    ``{task_id, kind, agent, title, conversation_id, status, message}``
    per omnigent's SysSessionSendTool. ``request_data`` echoes the call so
    the adapter can substring-match dispatch input against worktree paths
    registered on this session."""
    handle = {
        "task_id": "task-abc",
        "kind": "sub_agent",
        "agent": agent,
        "title": title,
        "conversation_id": child_conversation_id,
        "status": "running",
        "message": "dispatched",
    }
    request_data = {
        "arguments": {
            "agent": agent,
            "title": title,
            "args": {"purpose": "implement", "input": input_text},
        },
    }
    return tool_result_event(
        session_id, target="sys_session_send",
        data={"output": _dump_handle(handle)},
        request_data=request_data,
        **kwargs,
    )


def _dump_handle(handle: dict[str, Any]) -> str:
    import json as _json
    return _json.dumps(handle)


def response_event(
    session_id: str | None = "omni-sess-1",
    *,
    text: str = "All done.",
    labels: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """`response` phase — data is the assistant message STRING; sub-agent
    child conversations are recognized by their labels."""
    return make_event("response", text, session_id=session_id,
                      labels=labels, **kwargs)


def verdict(
    decision: str = "warn",
    band: int = 1,
    *,
    user_message: str | None = "Cardinal: 80% of the weekly budget is spent.",
    agent_context: str | None = "Budget standing: 80% consumed; work economically.",
    block_reason: str | None = None,
    fetched_at: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A server verdict blob as maybe_refresh_verdict would persist it."""
    v: dict[str, Any] = {
        "decision": decision,
        "band": band,
        "fetched_at": time.time() if fetched_at is None else fetched_at,
    }
    if user_message:
        v["user_message"] = user_message
    if agent_context:
        v["agent_context"] = agent_context
    if block_reason:
        v["block_reason"] = block_reason
    v.update(extra)
    return v

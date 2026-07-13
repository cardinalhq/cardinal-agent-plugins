"""Synthetic PolicyEvents matching the shapes verified against omnigent
commit 6e71197 (docs/specs/omnigent-adapter.md §Verified integration
facts): PolicyEvent{phase, target, data, context} with
EvaluationContext{session_id, usage, actor, labels, harness,
session_state}. Dataclasses (attribute access) exercise the accessors'
primary path; the adapter's dict path is covered separately in the
never-raises tests.

Also bootstraps sys.path for core + the adapter package and loads the
StubIngest golden harness from core/tests/harness.py by file path (no
package-name ambiguity with this tests/ directory).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from dataclasses import dataclass, field
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
# PolicyEvent fixtures
# ---------------------------------------------------------------------------

@dataclass
class Actor:
    run_as: str = "dev@example.com"
    client_id: str = "omnigent-runner"


@dataclass
class EvaluationContext:
    session_id: str = "omni-sess-1"
    usage: dict[str, Any] = field(default_factory=dict)
    actor: Actor | None = field(default_factory=Actor)
    labels: dict[str, Any] = field(default_factory=dict)
    harness: str | None = "claude"
    session_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyEvent:
    phase: str
    context: EvaluationContext = field(default_factory=EvaluationContext)
    data: dict[str, Any] = field(default_factory=dict)
    target: str | None = None


def _context(
    session_id: str = "omni-sess-1",
    *,
    labels: dict[str, Any] | None = None,
    run_as: str = "dev@example.com",
    harness: str | None = "claude",
    total_cost_usd: float | None = None,
    session_state: dict[str, Any] | None = None,
) -> EvaluationContext:
    usage: dict[str, Any] = {
        "input_tokens": 5000, "output_tokens": 900, "total_tokens": 5900,
    }
    if total_cost_usd is not None:
        usage["total_cost_usd"] = total_cost_usd
    return EvaluationContext(
        session_id=session_id,
        usage=usage,
        actor=Actor(run_as=run_as),
        labels=dict(labels or {}),
        harness=harness,
        session_state=dict(session_state or {}),
    )


def request_event(
    session_id: str = "omni-sess-1",
    *,
    labels: dict[str, Any] | None = None,
    prompt: str = "please fix the login crash",
    **ctx_kwargs: Any,
) -> PolicyEvent:
    """`request` phase — labels carry the Cardinal attribution convention."""
    return PolicyEvent(
        phase="request",
        context=_context(session_id, labels=labels, **ctx_kwargs),
        data={"prompt": prompt},
    )


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
    session_id: str = "omni-sess-1",
    *,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
    total_cost_usd: float | None = None,
    session_state: dict[str, Any] | None = None,
    **ctx_kwargs: Any,
) -> PolicyEvent:
    """`llm_response` phase — data.usage is per-TURN totals (cumulative
    across the turn's API calls); context.usage is session-cumulative."""
    u = dict(DEFAULT_TURN_USAGE if usage is None else usage)
    if model is not None:
        u["model"] = model
    return PolicyEvent(
        phase="llm_response",
        context=_context(
            session_id, total_cost_usd=total_cost_usd,
            session_state=session_state, **ctx_kwargs,
        ),
        data={"usage": u},
    )


def tool_call_shell_event(
    session_id: str = "omni-sess-1",
    *,
    command: str = "git status && ls -la",
    target: str = "bash",
    **ctx_kwargs: Any,
) -> PolicyEvent:
    return PolicyEvent(
        phase="tool_call",
        context=_context(session_id, **ctx_kwargs),
        data={"arguments": {"command": command}},
        target=target,
    )


def tool_call_mcp_event(
    session_id: str = "omni-sess-1",
    *,
    target: str = "mcp__lakerunner__execute_logs_query",
    **ctx_kwargs: Any,
) -> PolicyEvent:
    return PolicyEvent(
        phase="tool_call",
        context=_context(session_id, **ctx_kwargs),
        data={"arguments": {"query": "fingerprint=abc"}},
        target=target,
    )


def tool_result_event(
    session_id: str = "omni-sess-1",
    *,
    target: str = "bash",
    success: Any = True,
    exit_code: int | None = None,
    **ctx_kwargs: Any,
) -> PolicyEvent:
    data: dict[str, Any] = {}
    if exit_code is not None:
        data["exit_code"] = exit_code
    elif success is not None:
        data["success"] = success
    return PolicyEvent(
        phase="tool_result",
        context=_context(session_id, **ctx_kwargs),
        data=data,
        target=target,
    )


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

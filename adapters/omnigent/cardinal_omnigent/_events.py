"""Defensive PolicyEvent field accessors.

omnigent is alpha (contract verified at commit 6e71197 — see
OMNIGENT_VERIFIED_COMMIT); PolicyEvent may arrive as a pydantic model,
a dataclass, or a plain dict depending on engine version and transport.
Every field read in this package goes through these accessors: attribute
access first, mapping access second, a caller-supplied default on any
miss. Nothing here raises.

Verified shape (schema.py at the pinned commit):

    event.phase        "request" | "response" | "tool_call" | "tool_result"
                       | "llm_request" | "llm_response"
    event.target       resolved tool name on tool phases
    event.data         phase-specific payload (llm_response carries
                       data.usage per-TURN totals + data.model)
    event.context      EvaluationContext:
        .session_id
        .usage         cumulative session {input_tokens, output_tokens,
                       total_tokens, total_cost_usd} (server-maintained)
        .actor         {run_as: email, client_id}
        .labels        session labels (the Cardinal attribution channel)
        .harness       underlying agent harness ("claude", "codex", ...)
        .session_state engine session state (scoping is engine-dependent)
"""

from __future__ import annotations

from typing import Any

_MISSING = object()


def get(obj: Any, name: str, default: Any = None) -> Any:
    """obj.<name>, obj[<name>], or default — never raises."""
    if obj is None:
        return default
    try:
        value = getattr(obj, name, _MISSING)
        if value is not _MISSING:
            return value
    except Exception:
        pass
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
    except Exception:
        pass
    return default


def phase(event: Any) -> str:
    value = get(event, "phase")
    return str(value) if value else ""


def data(event: Any) -> dict[str, Any]:
    value = get(event, "data")
    return value if isinstance(value, dict) else {}


def context(event: Any) -> Any:
    return get(event, "context")


def target(event: Any) -> str | None:
    value = get(event, "target")
    return str(value) if isinstance(value, str) and value else None


def session_id(event: Any) -> str | None:
    ctx = context(event)
    value = get(ctx, "session_id") or get(event, "session_id")
    return str(value) if value else None


def actor_email(event: Any) -> str | None:
    actor = get(context(event), "actor")
    value = get(actor, "run_as")
    return str(value) if isinstance(value, str) and value else None


def labels(event: Any) -> dict[str, Any]:
    value = get(context(event), "labels")
    return value if isinstance(value, dict) else {}


def harness(event: Any) -> str | None:
    value = get(context(event), "harness")
    return str(value) if isinstance(value, str) and value else None


def session_usage(event: Any) -> dict[str, Any]:
    """Cumulative session usage from the EvaluationContext (may itself be
    an object or a dict)."""
    value = get(context(event), "usage")
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    out: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "total_cost_usd"):
        v = get(value, key, _MISSING)
        if v is not _MISSING:
            out[key] = v
    return out


def session_state(event: Any) -> dict[str, Any]:
    for key in ("session_state", "state"):
        value = get(context(event), key)
        if isinstance(value, dict):
            return value
    return {}


def as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None

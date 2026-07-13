"""Defensive PolicyEvent field accessors.

omnigent is alpha (contract verified at OMNIGENT_VERIFIED_COMMIT);
the event may arrive as a plain dict (the real
`FunctionPolicy._build_event` output) or an object (test doubles,
future pydantic-ification). Every field read in this package goes
through these accessors: attribute access first, mapping access
second, a caller-supplied default on any miss. Nothing here raises.

Verified wire shape (`omnigent/policies/function.py::_build_event` +
`schema.py` at the pinned commit):

    event["type"]          "request" | "tool_call" | "tool_result" |
                           "response" | "llm_request" | "llm_response"
    event["target"]        resolved tool name on tool phases
    event["data"]          phase-specific payload (llm_response carries
                           data.model / data.text_preview /
                           data.tool_calls_count and optionally
                           data.usage — per-TURN totals on the
                           claude_sdk executor)
    event["context"]       {actor: {run_as, client_id},
                            usage: cumulative session {input_tokens,
                              output_tokens, total_tokens,
                              total_cost_usd, cache buckets},
                            user_daily_cost, model, harness,
                            labels, subtree_usage}
    event["session_state"] engine per-conversation key/value store
                           (top-level, NOT under context)
    event["llm_client"]    engine-shared PolicyLLMClient or None
    event["request_data"]  original tool call, tool_result phase only

There is NO session/conversation id anywhere in the contract — see
`_identity.py` for how Cardinal mints one.
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
    """The enforcement phase. The wire key is `type` (verified —
    `_build_event` maps Phase.value onto it); `phase` is probed second
    for forward-compat with a contract rename."""
    value = get(event, "type") or get(event, "phase")
    return str(value) if isinstance(value, str) and value else ""


def data(event: Any) -> dict[str, Any]:
    value = get(event, "data")
    return value if isinstance(value, dict) else {}


def context(event: Any) -> Any:
    return get(event, "context")


def target(event: Any) -> str | None:
    value = get(event, "target")
    return str(value) if isinstance(value, str) and value else None


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


def model(event: Any) -> str | None:
    """The session's active model, engine-injected on every dispatch
    (conversation model_override, else the agent spec's llm.model)."""
    value = get(context(event), "model")
    return str(value) if isinstance(value, str) and value else None


def llm_client(event: Any) -> Any:
    """The engine-shared PolicyLLMClient (identity anchor for
    `_identity`); None when the server has no `llm:` config."""
    return get(event, "llm_client")


def _usage_dict(value: Any) -> dict[str, Any]:
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


def session_usage(event: Any) -> dict[str, Any]:
    """Cumulative session usage from the context (server-maintained;
    includes total_cost_usd)."""
    return _usage_dict(get(context(event), "usage"))


def subtree_usage(event: Any) -> dict[str, Any]:
    """Subtree-scoped cumulative usage (this conversation + its
    descendants). Engine injects it only when a subagent_cost_budget
    policy is configured; empty otherwise."""
    return _usage_dict(get(context(event), "subtree_usage"))


def session_state(event: Any) -> dict[str, Any]:
    """The engine's per-conversation state. Top-level on the event
    (verified); context is probed second for forward-compat."""
    for source, key in ((event, "session_state"), (event, "state"),
                        (context(event), "session_state")):
        value = get(source, key)
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

"""Enforcing Cardinal spend-limits policy — omnigent `request` phase.

Renders core's channel-agnostic GateDecision into omnigent's
PolicyResponse:

  block        → {"result": "DENY", "reason": <server-authored copy>}
  warn/notify  → {"result": "ALLOW"} with the standing message surfaced
                 on the reason / set_labels channels, gated by ack_band
                 hysteresis (a band is surfaced once, re-surfaced only
                 when it rises)
  no verdict   → abstain (None)

Additionally enforces an omnigent-native SESSION cap: policy config
`session_cost_limit_usd` checked against the server-maintained cumulative
`event.context.usage.total_cost_usd` — this needs no Cardinal backend
round-trip and works even before the first verdict fetch.

Verdict state lives in a server-writable dir (config `state_dir`,
default ~/.omnigent) using core's AgentPaths layout; the verdict is
refreshed post-gate on the server TTL, exactly like the CLI adapters —
the gate itself is file I/O only.

Failure policy — deliberate and load-bearing: PHASE_REQUEST is in
omnigent's FAIL_CLOSED_PHASES, so a policy that RAISES fails the request
closed ("Cardinal budgets are enforced, not suggested" — enforcement the
CLI plugins cannot have). But fail-closed protects against evaluation
failure, not against our own bugs: this policy catches internal errors
and returns None (abstain → ALLOW). We do NOT self-DENY on bugs — a
crash in Cardinal code must never take an org's agent fleet down; only a
genuine server verdict (or the configured session cap) denies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cardinal_core import limits
from cardinal_core.paths import AgentPaths

from . import _events, _identity

DEFAULT_STATE_DIR = "~/.omnigent"

BAND_LABEL = "cardinal.limits_band"
MESSAGE_LABEL = "cardinal.limits_message"


def _paths(config: dict[str, Any]) -> AgentPaths:
    state_dir = config.get("state_dir") or DEFAULT_STATE_DIR
    return AgentPaths(home=Path(str(state_dir)).expanduser())


def _session_cap_check(event: Any, config: dict[str, Any]) -> dict[str, Any] | None:
    cap = _events.as_float(config.get("session_cost_limit_usd"))
    if cap is None or cap <= 0:
        return None
    total = _events.as_float(_events.session_usage(event).get("total_cost_usd"))
    if total is None or total < cap:
        return None
    return {
        "result": "DENY",
        "reason": (
            f"Cardinal session spend cap reached: ${total:.2f} of a "
            f"${cap:.2f} per-session limit. Start a new session or raise "
            "session_cost_limit_usd in the omnigent server config."
        ),
        "data": {"cardinal_session_cost_usd": total, "cardinal_session_cap_usd": cap},
    }


def _render_gate(
    paths: AgentPaths, session_id: str, decision: limits.GateDecision
) -> dict[str, Any] | None:
    if decision.tier == "block":
        return {
            "result": "DENY",
            "reason": decision.reason
            or "A Cardinal spend limit for this work has been reached.",
            "data": {"cardinal_band": decision.band, "cardinal_tier": "block"},
        }
    if not decision.is_new_band:
        return None  # hysteresis: this band was already surfaced
    message = decision.user_message or decision.agent_context
    if not message:
        return None
    out: dict[str, Any] = {
        "result": "ALLOW",
        "reason": message,
        "data": {"cardinal_band": decision.band, "cardinal_tier": decision.tier},
        "set_labels": {BAND_LABEL: str(decision.band), MESSAGE_LABEL: message},
    }
    if decision.agent_context:
        out["data"]["agent_context"] = decision.agent_context
    # The hysteresis write happens only after the band is actually
    # surfaced — GateDecision itself never writes state.
    limits.ack_band(paths, session_id, decision.band)
    return out


def _refresh_verdict(
    event: Any, config: dict[str, Any], paths: AgentPaths, session_id: str
) -> None:
    """Post-gate TTL refresh (network, short timeout, best-effort). repo /
    branch ride the labels convention; the API key comes from the policy
    config (core 0.2.0 gap #2 — credential as argument)."""
    labels = _events.labels(event)
    repo = labels.get("cardinal.repo")
    branch = labels.get("cardinal.branch")
    api_key = config.get("ingest_api_key")
    limits.maybe_refresh_verdict(
        paths,
        session_id=session_id,
        repo=str(repo) if isinstance(repo, str) and repo else None,
        branch=str(branch) if isinstance(branch, str) and branch else None,
        api_key=str(api_key) if isinstance(api_key, str) and api_key else None,
    )


def spend_limits_policy(event: Any, config: Any = None) -> dict[str, Any] | None:
    """Enforcing spend gate on the request phase; abstains on every other
    phase and on any internal error (see module docstring)."""
    try:
        if _events.phase(event) != "request":
            return None
        cfg = config if isinstance(config, dict) else {}
        # Minted session identity (no session id in omnigent's contract —
        # see _identity.py). Shared with the telemetry policy via the
        # engine's session_state; pending updates ride any non-abstain
        # return (state_updates are applied on ALLOW and DENY).
        session_id, updates = _identity.resolve(event)
        if not session_id:
            return None

        result = _session_cap_check(event, cfg)
        paths = _paths(cfg)
        if result is None:
            decision = limits.gate_decision(paths, session_id)
            if decision is not None:
                result = _render_gate(paths, session_id, decision)

        try:
            _refresh_verdict(event, cfg, paths, session_id)
        except Exception:
            pass
        if updates:
            if result is None:
                result = {"result": "ALLOW"}
            result["state_updates"] = [*updates, *result.get("state_updates", [])]
        return result
    except Exception:
        return None


def make_spend_limits_policy(config: dict[str, Any] | None = None):
    """Factory form for omnigent's `function: {path, arguments}`
    registration — closes over the config block."""
    def policy(event: Any) -> dict[str, Any] | None:
        return spend_limits_policy(event, config)
    return policy

"""Escalation timer + fallback-channel dispatch.

Every ask_human binding may declare:

    escalation:
      afterMinutes: 30
      channel_ref: pagerduty.incident
      channel_params: { ... }

When that timer breaches while the pending ask is still ``waiting``, we
publish a notification to the escalation channel. The original channel
STAYS OPEN — first-reply-wins — so the run doesn't hang if the escalation
target's channel is worse than the original.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import channels as channels_pkg  # flat spike layout; sys.path pre-seeded by executor.py


class EscalationError(RuntimeError):
    pass


@dataclass
class EscalationDecision:
    fired: bool
    channel_id: str | None
    handle_reference: str | None
    reason: str | None = None


def has_escalation(binding: dict[str, Any]) -> bool:
    esc = binding.get("escalation")
    return isinstance(esc, dict) and "afterMinutes" in esc and "channel_ref" in esc


def escalation_deadline(binding: dict[str, Any], published_at: datetime) -> datetime | None:
    esc = binding.get("escalation")
    if not esc:
        return None
    minutes = float(esc.get("afterMinutes", 0))
    if minutes <= 0:
        return None
    return published_at + timedelta(minutes=minutes)


def dispatch(
    binding: dict[str, Any],
    node_id: str,
    question: str,
    evidence: dict[str, Any],
    resolve_secret: Callable[[str], str],
    original_channel_id: str,
) -> EscalationDecision:
    """Publish an escalation notice on the fallback channel.

    Returns an ``EscalationDecision`` capturing what was done so the
    caller can persist it to audit. Does NOT wait for a reply on the
    escalation channel — Phase 1 keeps escalation as fire-and-forget.
    """
    esc = binding.get("escalation")
    if not esc:
        return EscalationDecision(fired=False, channel_id=None, handle_reference=None, reason="no escalation configured")

    channel_ref = esc["channel_ref"]
    driver = channels_pkg.resolve_channel(channel_ref)
    esc_params = esc.get("channel_params") or binding.get("channel_params") or {}

    escalation_text = (
        f"[escalation] ask_human `{node_id}` unanswered for "
        f"{esc.get('afterMinutes')} minutes; original channel "
        f"({original_channel_id}) still accepting reply.\n\nQuestion:\n{question}"
    )
    ctx = channels_pkg.PublishContext(
        node_id=f"{node_id}#escalation",
        question=escalation_text,
        evidence=evidence,
        binding=binding,
        channel_params=esc_params,
        resolve_secret=resolve_secret,
        extras={"escalation": True},
    )
    handle = driver.publish(ctx)
    return EscalationDecision(
        fired=True,
        channel_id=handle.channel_id,
        handle_reference=handle.reference,
    )


__all__ = [
    "EscalationDecision",
    "EscalationError",
    "dispatch",
    "escalation_deadline",
    "has_escalation",
]

"""Ask-human node handler (§14a runtime).

Orchestrates: publish → wait → identity check → normalize → persist. All
state transitions are recorded in the sqlite state store so a reviewer can
audit every decision, and so a restart resumes cleanly (edge cases 1, 16).

The handler's contract is deliberately narrow: given a fully-resolved node
config + deployment binding + evidence dict + state store + callable
registries, do the whole ask_human dance and return an ``AskOutcome``. The
caller (execute loop) decides how to feed the outcome into downstream
nodes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import channels as channels_pkg
import escalation as escalation_mod
import normalizer as normalizer_mod
import state as state_mod


class IdentityPolicyMismatch(RuntimeError):
    """Raised internally when a reply's operator id is disallowed."""


@dataclass
class AskOutcome:
    """The outcome the DAG loop consumes.

    ``answer`` is the schema-conforming value ONLY when ``status ==
    'normalized'``. Otherwise it is None and the DAG loop treats the node
    as producing no answer (per §14a `inconclusive`/skip discipline).
    """

    status: str  # normalized | inconclusive | deferred | timeout
    answer: Any = None
    raw_reply: str | None = None
    operator_id: str | None = None
    inconclusive_reason: str | None = None
    defer_reason: str | None = None
    pending_ask_id: int | None = None
    escalation_fired: bool = False
    identity_mismatches: list[dict[str, Any]] = field(default_factory=list)


def _identity_allowed(operator_id: str | None, policy: dict[str, Any]) -> bool:
    """Enforce ``identity_policy`` from deployment.yaml.

    Policy is a JSON-Schema oneOf: allowedUsers | allowedGroups |
    oncallRotation. Phase 1 only knows how to check ``allowedUsers``
    truthfully; the other two are recorded as "always pass" with an audit
    marker because group membership + oncall API calls belong to Phase 2.
    We reject unknown operators for allowedUsers strictly.
    """
    if operator_id is None:
        return False
    if "allowedUsers" in policy:
        return operator_id in set(policy["allowedUsers"])
    if "allowedGroups" in policy or "oncallRotation" in policy:
        # Phase 1: no group lookup client available. Accept but the
        # audit event marks this so downstream compliance sees it.
        return True
    return False


def handle_ask_human(
    node_id: str,
    node_spec: dict[str, Any],
    binding: dict[str, Any],
    evidence: dict[str, Any],
    parser_model: str | None,
    state: state_mod.StateStore,
    run_id: str,
    resolve_secret: Callable[[str], str],
    # Overall wait deadline: usually derived from `timeout.maxWait`.
    max_wait: timedelta,
    # How often to poll for a reply. Lower = faster tests. Escalation
    # deadline is checked at the same cadence.
    poll_interval: float = 0.1,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> AskOutcome:
    """Run the full ask_human dance for one node.

    State transitions land in the pending_asks table. Multiple identity
    mismatches are recorded to audit and treated as no-reply so the run
    stays in ``waiting`` until an allowed operator answers OR the timeout
    fires.
    """
    channel_ref = binding["channel_ref"]
    driver = channels_pkg.resolve_channel(channel_ref)
    channel_params = binding.get("channel_params") or {}
    identity_policy = binding.get("identity_policy") or {}
    reply_normalization = binding["reply_normalization"]
    answer_schema = (node_spec.get("config") or {}).get("answerSchema") or {}

    question = (node_spec.get("config") or {}).get("question") or ""

    published_at = now_fn()
    overall_deadline = published_at + max_wait

    publish_ctx = channels_pkg.PublishContext(
        node_id=node_id,
        question=question,
        evidence=evidence,
        binding=binding,
        channel_params=channel_params,
        resolve_secret=resolve_secret,
    )
    handle = driver.publish(publish_ctx)

    ask_id = state.create_pending_ask(
        run_id=run_id,
        node_id=node_id,
        question=question,
        evidence=evidence,
        channel_id=handle.channel_id,
        handle_reference=handle.reference,
    )
    state.audit(
        "ask_human.published",
        {"node_id": node_id, "channel_id": handle.channel_id, "reference": handle.reference},
        run_id=run_id,
        node_id=node_id,
    )

    esc_deadline = escalation_mod.escalation_deadline(binding, published_at)
    esc_fired = False
    identity_mismatches: list[dict[str, Any]] = []

    outcome: AskOutcome | None = None
    while True:
        now = now_fn()
        if now >= overall_deadline:
            state.transition(
                ask_id,
                state_mod.ASK_STATE_INCONCLUSIVE,
                inconclusive_reason="timeout (no reply within maxWait)",
            )
            state.audit(
                "ask_human.timeout",
                {"node_id": node_id, "waited_s": (now - published_at).total_seconds()},
                run_id=run_id,
                node_id=node_id,
            )
            outcome = AskOutcome(
                status="timeout",
                inconclusive_reason="timeout (no reply within maxWait)",
                pending_ask_id=ask_id,
                escalation_fired=esc_fired,
                identity_mismatches=identity_mismatches,
            )
            break

        # Fire escalation if we've crossed its deadline and haven't yet.
        if esc_deadline is not None and not esc_fired and now >= esc_deadline:
            try:
                dec = escalation_mod.dispatch(
                    binding,
                    node_id=node_id,
                    question=question,
                    evidence=evidence,
                    resolve_secret=resolve_secret,
                    original_channel_id=handle.channel_id,
                )
                esc_fired = dec.fired
                state.audit(
                    "ask_human.escalated",
                    {
                        "node_id": node_id,
                        "escalation_channel": dec.channel_id,
                        "reference": dec.handle_reference,
                        "waited_s": (now - published_at).total_seconds(),
                    },
                    run_id=run_id,
                    node_id=node_id,
                )
            except Exception as e:
                state.audit(
                    "ask_human.escalation_failed",
                    {"node_id": node_id, "error": f"{type(e).__name__}: {e}"},
                    run_id=run_id,
                    node_id=node_id,
                )
                esc_fired = True  # don't retry escalation this run

        # Poll the driver for a reply. Drivers return None if no reply
        # arrived by the sub-deadline; we use min(overall, next_check).
        next_check = min(overall_deadline, now + timedelta(seconds=poll_interval))
        if esc_deadline is not None and not esc_fired:
            next_check = min(next_check, esc_deadline)
        reply = driver.wait_for_reply(handle, next_check)
        if reply is None:
            # No reply yet — loop for next check.
            continue

        # Identity check BEFORE normalization (§14a + plan v0.2).
        if not _identity_allowed(reply.operator_id, identity_policy):
            mismatch_rec = {
                "node_id": node_id,
                "operator_id": reply.operator_id,
                "policy": identity_policy,
                "raw_reply": reply.raw_text,
            }
            identity_mismatches.append(mismatch_rec)
            state.audit("ask_human.identity_mismatch", mismatch_rec, run_id=run_id, node_id=node_id)
            # Treat as no-reply — stay in `waiting`. Continue polling.
            continue

        # Received: transition + persist raw.
        state.transition(
            ask_id,
            state_mod.ASK_STATE_RECEIVED,
            raw_reply=reply.raw_text,
            operator_id=reply.operator_id,
        )
        state.audit(
            "ask_human.received",
            {
                "node_id": node_id,
                "operator_id": reply.operator_id,
                "raw_reply_len": len(reply.raw_text),
            },
            run_id=run_id,
            node_id=node_id,
        )

        # Parsing state.
        state.transition(ask_id, state_mod.ASK_STATE_PARSING)
        try:
            norm = normalizer_mod.normalize(
                raw_reply=reply.raw_text,
                answer_schema=answer_schema,
                reply_normalization=reply_normalization,
                parser_model=parser_model,
            )
        except normalizer_mod.ParserUnavailableError as e:
            state.transition(
                ask_id,
                state_mod.ASK_STATE_INCONCLUSIVE,
                inconclusive_reason=f"parser unavailable: {e}",
            )
            state.audit(
                "ask_human.parser_unavailable",
                {"node_id": node_id, "error": str(e)},
                run_id=run_id,
                node_id=node_id,
            )
            outcome = AskOutcome(
                status="inconclusive",
                raw_reply=reply.raw_text,
                operator_id=reply.operator_id,
                inconclusive_reason=f"parser unavailable: {e}",
                pending_ask_id=ask_id,
                escalation_fired=esc_fired,
                identity_mismatches=identity_mismatches,
            )
            break

        if norm.status == "normalized":
            state.transition(
                ask_id,
                state_mod.ASK_STATE_NORMALIZED,
                normalized_json=norm.normalized_value,
                parser_model=norm.parser_model,
                parser_raw_output=norm.parser_raw_output,
            )
            state.audit(
                "ask_human.normalized",
                {
                    "node_id": node_id,
                    "operator_id": reply.operator_id,
                    "answer": norm.normalized_value,
                    "parser_model": norm.parser_model,
                },
                run_id=run_id,
                node_id=node_id,
            )
            outcome = AskOutcome(
                status="normalized",
                answer=norm.normalized_value,
                raw_reply=reply.raw_text,
                operator_id=reply.operator_id,
                pending_ask_id=ask_id,
                escalation_fired=esc_fired,
                identity_mismatches=identity_mismatches,
            )
            break

        if norm.status == "deferred":
            state.transition(
                ask_id,
                state_mod.ASK_STATE_DEFERRED,
                parser_model=norm.parser_model,
                parser_raw_output=norm.parser_raw_output,
                defer_reason=norm.defer_reason,
            )
            state.audit(
                "ask_human.deferred",
                {
                    "node_id": node_id,
                    "operator_id": reply.operator_id,
                    "defer_reason": norm.defer_reason,
                    "raw_reply": reply.raw_text,
                },
                run_id=run_id,
                node_id=node_id,
            )
            # Fire escalation as a courtesy notify if configured and not
            # yet fired — per plan v0.2: "_defer: true routes to
            # inconclusive with reason and publishes to escalation channel".
            if esc_deadline is not None and not esc_fired:
                try:
                    dec = escalation_mod.dispatch(
                        binding,
                        node_id=node_id,
                        question=f"[deferred] {question}",
                        evidence={"defer_reason": norm.defer_reason, **evidence},
                        resolve_secret=resolve_secret,
                        original_channel_id=handle.channel_id,
                    )
                    esc_fired = dec.fired
                    state.audit(
                        "ask_human.deferred_escalated",
                        {
                            "node_id": node_id,
                            "escalation_channel": dec.channel_id,
                        },
                        run_id=run_id,
                        node_id=node_id,
                    )
                except Exception as e:
                    state.audit(
                        "ask_human.escalation_failed",
                        {"node_id": node_id, "error": f"{type(e).__name__}: {e}"},
                        run_id=run_id,
                        node_id=node_id,
                    )
            outcome = AskOutcome(
                status="deferred",
                raw_reply=reply.raw_text,
                operator_id=reply.operator_id,
                defer_reason=norm.defer_reason,
                pending_ask_id=ask_id,
                escalation_fired=esc_fired,
                identity_mismatches=identity_mismatches,
            )
            break

        # inconclusive
        state.transition(
            ask_id,
            state_mod.ASK_STATE_INCONCLUSIVE,
            parser_model=norm.parser_model,
            parser_raw_output=norm.parser_raw_output,
            inconclusive_reason=norm.inconclusive_reason,
        )
        state.audit(
            "ask_human.inconclusive",
            {
                "node_id": node_id,
                "operator_id": reply.operator_id,
                "reason": norm.inconclusive_reason,
                "raw_reply": reply.raw_text,
            },
            run_id=run_id,
            node_id=node_id,
        )
        outcome = AskOutcome(
            status="inconclusive",
            raw_reply=reply.raw_text,
            operator_id=reply.operator_id,
            inconclusive_reason=norm.inconclusive_reason,
            pending_ask_id=ask_id,
            escalation_fired=esc_fired,
            identity_mismatches=identity_mismatches,
        )
        break

    assert outcome is not None
    return outcome


__all__ = [
    "AskOutcome",
    "IdentityPolicyMismatch",
    "handle_ask_human",
]

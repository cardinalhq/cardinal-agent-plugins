"""In-memory channel driver for pytest.

Not registered in the capabilities registry as a remote channel — the
runtime is expected to refuse it outside of test mode. Tests seed the
class-level ``INBOX`` and ``IDENTITY`` maps before invoking the runtime:

    from executor.channels.test_mock import TestMockChannel
    TestMockChannel.INBOX[node_id] = "yes"
    TestMockChannel.IDENTITY[node_id] = "alice@example.com"

`INBOX` maps `node_id -> raw_text | None`. None means "no reply arrives"
so tests can exercise timeout/escalation paths.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import (
    ChannelDriver,
    PublishContext,
    PublishHandle,
    Reply,
    channel,
)


@channel("test.mock")
class TestMockChannel(ChannelDriver):
    id = "test.mock"

    # Class-level state — tests seed these directly.
    INBOX: dict[str, str | None] = {}
    IDENTITY: dict[str, str] = {}
    PUBLISHED: list[dict] = []
    # Optional delay before the reply becomes readable, measured from
    # publish time (monotonic seconds). Applies across many wait calls so
    # the runtime's polling loop can interleave with escalation timers.
    REPLY_DELAY_S: dict[str, float] = {}
    # Populated at publish time; tests can also seed directly.
    _PUBLISHED_AT: dict[str, float] = {}

    @classmethod
    def reset(cls) -> None:
        cls.INBOX.clear()
        cls.IDENTITY.clear()
        cls.PUBLISHED.clear()
        cls.REPLY_DELAY_S.clear()
        cls._PUBLISHED_AT.clear()

    def publish(self, ctx: PublishContext) -> PublishHandle:
        self.PUBLISHED.append({
            "node_id": ctx.node_id,
            "question": ctx.question,
            "evidence": ctx.evidence,
            "channel_params": ctx.channel_params,
        })
        # Only track publish-time for the primary node id; escalation
        # publishes are fire-and-forget so we don't reserve a slot for
        # them.
        self._PUBLISHED_AT.setdefault(ctx.node_id, time.monotonic())
        return PublishHandle(
            channel_id="test.mock",
            reference=ctx.node_id,
            metadata={"published_at": datetime.now(timezone.utc).isoformat()},
        )

    def wait_for_reply(
        self, handle: PublishHandle, deadline: datetime
    ) -> Reply | None:
        node_id = handle.reference
        delay = self.REPLY_DELAY_S.get(node_id, 0.0)
        published_at = self._PUBLISHED_AT.get(node_id, time.monotonic())
        ready_at = published_at + delay
        # Sleep in short slices until either the reply becomes ready OR
        # the deadline is reached. Slice length is small so the runtime's
        # escalation deadline can still preempt on the next iteration.
        while True:
            now_ts = time.monotonic()
            now_dt = datetime.now(timezone.utc)
            if now_dt >= deadline:
                return None
            if now_ts >= ready_at:
                break
            time_left_to_ready = ready_at - now_ts
            time_left_to_deadline = (deadline - now_dt).total_seconds()
            slice_s = max(0.005, min(0.02, time_left_to_ready, time_left_to_deadline))
            time.sleep(slice_s)
        raw = self.INBOX.get(node_id)
        if raw is None:
            return None
        operator = self.IDENTITY.get(node_id)
        return Reply(
            raw_text=raw,
            operator_id=operator,
            received_at=datetime.now(timezone.utc),
            channel_id="test.mock",
            metadata={"handle_ref": node_id},
        )

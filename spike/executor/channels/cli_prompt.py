"""stdin/stdout channel driver.

Publishes the question to stderr; reads the reply from stdin. For
structured `answerSchema`, one line is enough. For prose, we read until
EOF (Ctrl-D). Operator identity comes from ``$MECHANIZE_OPERATOR_ID`` or
falls back to ``$USER`` — the driver never invents identity.

This driver makes Phase 1 tryable locally without any Slack app.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from . import (
    ChannelDriver,
    PublishContext,
    PublishHandle,
    Reply,
    channel,
)


@channel("cli-prompt")
class CliPromptChannel(ChannelDriver):
    id = "cli-prompt"

    def _operator_id(self) -> str | None:
        return os.environ.get("MECHANIZE_OPERATOR_ID") or os.environ.get("USER")

    def publish(self, ctx: PublishContext) -> PublishHandle:
        sys.stderr.write("\n" + "=" * 72 + "\n")
        sys.stderr.write(f"ASK_HUMAN  node={ctx.node_id}\n")
        sys.stderr.write("-" * 72 + "\n")
        sys.stderr.write(ctx.question.strip() + "\n")
        if ctx.evidence:
            sys.stderr.write("\nEvidence:\n")
            for k, v in ctx.evidence.items():
                sys.stderr.write(f"  - {k}: {v!r}\n")
        sys.stderr.write("=" * 72 + "\n")
        sys.stderr.write(
            "Reply on stdin. One line for structured; Ctrl-D to end prose.\n"
        )
        sys.stderr.flush()
        return PublishHandle(
            channel_id="cli-prompt",
            reference=ctx.node_id,
            metadata={"mode": ctx.binding.get("reply_normalization", "structured")},
        )

    def wait_for_reply(
        self, handle: PublishHandle, deadline: datetime
    ) -> Reply | None:
        # Best-effort: we don't wire a select loop for stdin timeout in
        # Phase 1. Blocking read is fine for the local-dev use case; the
        # runtime enforces top-level deadline on the wall clock elsewhere.
        mode = handle.metadata.get("mode", "structured")
        try:
            if mode == "prose-llm-parse":
                text = sys.stdin.read()
            else:
                line = sys.stdin.readline()
                if line == "":
                    return None
                text = line.rstrip("\n")
        except (KeyboardInterrupt, EOFError):
            return None
        if not text:
            return None
        return Reply(
            raw_text=text,
            operator_id=self._operator_id(),
            received_at=datetime.now(timezone.utc),
            channel_id="cli-prompt",
            metadata={},
        )

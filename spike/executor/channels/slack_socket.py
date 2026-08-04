"""Slack socket-mode channel driver.

Publishes questions to a channel and reads replies from the message
thread. Filters bot-own posts by ``bot_id``/``app_id`` to avoid echo
(edge case 9). Reconnection is delegated to ``slack-sdk``'s built-in
retry; on reconnect we poll the thread for late replies (edge case 1).

Phase 1 requires this file to be importable and structurally valid but
does NOT require a working end-to-end run against real Slack — tests use
the ``test.mock`` driver. slack-sdk lookups happen lazily so importing
this module does not fail if creds are absent.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import (
    ChannelDriver,
    ChannelDriverError,
    PublishContext,
    PublishHandle,
    Reply,
    channel,
)


try:
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.web import WebClient
    _SLACK_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dep
    SocketModeClient = None  # type: ignore
    WebClient = None  # type: ignore
    SocketModeRequest = None  # type: ignore
    _SLACK_SDK_AVAILABLE = False


@channel("slack.socket-mode")
class SlackSocketChannel(ChannelDriver):
    id = "slack.socket-mode"

    def _client(self, ctx: PublishContext):
        if not _SLACK_SDK_AVAILABLE:
            raise ChannelDriverError(
                "slack-sdk not installed — add slack-sdk to requirements.txt "
                "or use cli-prompt / test.mock in Phase 1"
            )
        params = ctx.channel_params
        bot_token = ctx.resolve_secret(params["token_ref"])
        app_token_ref = params.get("app_token_ref")
        app_token = ctx.resolve_secret(app_token_ref) if app_token_ref else None
        web = WebClient(token=bot_token)
        socket = None
        if app_token:
            socket = SocketModeClient(app_token=app_token, web_client=web)
        return web, socket

    def publish(self, ctx: PublishContext) -> PublishHandle:
        web, _ = self._client(ctx)
        params = ctx.channel_params
        channel_id = params["channel_id"]
        text_blocks = [f"*ASK_HUMAN* `{ctx.node_id}`", ctx.question.strip()]
        if ctx.evidence:
            evid = "\n".join(f"• *{k}*: `{v!r}`" for k, v in ctx.evidence.items())
            text_blocks.append(f"_Evidence:_\n{evid}")
        text = "\n\n".join(text_blocks)
        resp = web.chat_postMessage(channel=channel_id, text=text)
        thread_ts = resp["ts"]
        auth = web.auth_test()
        return PublishHandle(
            channel_id=channel_id,
            reference=thread_ts,
            metadata={
                "bot_id": auth.get("bot_id"),
                "app_id": params.get("app_id"),
                "self_user_id": auth.get("user_id"),
            },
        )

    def wait_for_reply(
        self, handle: PublishHandle, deadline: datetime
    ) -> Reply | None:
        # Poll the thread for replies. slack-sdk's socket mode delivers
        # events too but polling the thread also catches late replies
        # after a socket reconnect (edge case 1).
        # This is intentionally simple for Phase 1; Phase 2 replaces
        # with a socket-mode event loop.
        # We do not run this path in tests; Slack creds required.
        web, _ = self._client(self._synthesize_ctx(handle))
        poll_interval = 2.0
        while datetime.now(timezone.utc) < deadline:
            resp = web.conversations_replies(
                channel=handle.channel_id, ts=handle.reference, limit=50
            )
            messages = resp.get("messages", [])
            for msg in messages[1:]:  # skip the ask itself
                # Filter our own echo.
                if handle.metadata.get("bot_id") and msg.get("bot_id") == handle.metadata["bot_id"]:
                    continue
                if handle.metadata.get("app_id") and msg.get("app_id") == handle.metadata["app_id"]:
                    continue
                if handle.metadata.get("self_user_id") and msg.get("user") == handle.metadata["self_user_id"]:
                    continue
                return Reply(
                    raw_text=msg.get("text", ""),
                    operator_id=msg.get("user"),
                    received_at=datetime.now(timezone.utc),
                    channel_id=handle.channel_id,
                    metadata={"slack_ts": msg.get("ts"), "thread_ts": handle.reference},
                )
            time.sleep(poll_interval)
        return None

    def _synthesize_ctx(self, handle: PublishHandle) -> PublishContext:
        # For the poll path we need a client but no fresh publish. Callers
        # of this driver in Phase 2 will keep a client around; in Phase 1
        # this shim reconstructs the minimum context. The test suite does
        # not exercise this branch.
        raise ChannelDriverError(
            "Phase 1 slack.socket-mode poll path requires the caller to "
            "hold onto the WebClient; use the daemon runtime (Phase 2)"
        )

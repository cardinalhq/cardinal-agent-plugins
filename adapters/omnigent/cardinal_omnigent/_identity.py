"""Session identity for omnigent policy events.

omnigent's policy contract carries NO session/conversation identifier:
`FunctionPolicy._build_event` exposes only {type, target, data, context
{actor, usage, user_daily_cost, model, harness, labels, subtree_usage},
session_state, llm_client, request_data} — verified at
OMNIGENT_VERIFIED_COMMIT. The engine knows its conversation_id
(runtime/policies/engine.py) but never surfaces it to callables.

Cardinal telemetry is session-keyed, so we mint one: a `cardinal.
session_id` entry in omnigent's per-conversation session_state,
persisted via `state_updates` (the server-side engine applies them to
its hot cache immediately on ALLOW/DENY composition and writes them
through to the conversation store, so the id is durable for the
conversation's lifetime and shared by every Cardinal policy).

Between the mint and the first state application — and on paths where
state persistence is unavailable (read-only evaluations, the
runner-local gate) — an in-process memo keyed by the engine-scoped
`llm_client` object identity keeps telemetry and spend_limits on the
same id: the PolicyLLMClient instance is shared across all policies of
one engine, and the server builds one engine per conversation. When
`llm_client` is None (no server `llm:` config) the memo is keyed by the
session_state dict's own object identity instead — weaker (the engine
copies it per dispatch), so each dispatch of a session that ALSO never
persists state mints fresh ids; that fragmentation is the documented
floor of what a policy can do without upstream contract help.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from . import _events

SESSION_ID_KEY = "cardinal.session_id"

# id() values are reused after garbage collection, so a dead engine's
# address could hand its session id to a brand-new conversation. The
# memo only needs to bridge the seconds between a mint and the engine
# persisting it (a persisted id always wins), so entries expire fast.
_MEMO: dict[int, tuple[str, float]] = {}
_MEMO_MAX = 4096
_MEMO_TTL_SEC = 600.0


def _memo_key(event: Any) -> int | None:
    client = _events.llm_client(event)
    if client is not None:
        return id(client)
    return None


def resolve(event: Any) -> tuple[str | None, list[dict[str, Any]]]:
    """(session_id, pending state_updates) for this event.

    The updates list is non-empty until the id is visible in the
    engine's session_state — callers attach it to any ALLOW they
    return so the mint eventually persists. None id only for events
    with no context at all (garbage — callers abstain).
    """
    state = _events.session_state(event)
    persisted = state.get(SESSION_ID_KEY)
    if isinstance(persisted, str) and persisted:
        return persisted, []

    if _events.context(event) is None:
        return None, []

    now = time.monotonic()
    key = _memo_key(event)
    entry = _MEMO.get(key) if key is not None else None
    if entry is not None and now - entry[1] < _MEMO_TTL_SEC:
        session_id = entry[0]
    else:
        session_id = "omni-" + uuid.uuid4().hex[:16]
        if key is not None:
            if len(_MEMO) >= _MEMO_MAX:
                _MEMO.pop(next(iter(_MEMO)))
            _MEMO[key] = (session_id, now)
    updates = [{"action": "set", "key": SESSION_ID_KEY, "value": session_id}]
    return session_id, updates

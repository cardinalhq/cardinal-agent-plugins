"""Spend-limits shim over cardinal_core.limits — Claude adapter.

Everything file-shaped delegates to core (AgentPaths(home=~/.claude)
matches the shipped layout byte-for-byte: cardinal.json state,
cardinal/limits/<session>.{verdict,ack,override}.json). What stays here
is the ONE Claude-specific fact: the ingest API key lives in
~/.claude/settings.json's OTEL_EXPORTER_OTLP_HEADERS, not in a
cardinal-secrets.json — so core's maybe_refresh_verdict (which sources
the key via paths.read_secrets()) can't be used as-is and its TTL loop
is reproduced here around core fetch_status. See CORE_GAPS.md.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_core.limits import (  # noqa: E402, F401  (re-exported)
    BLOCK_MAX_AGE_SEC,
    DEFAULT_TTL_SEC,
    FETCH_TIMEOUT_SEC,
    WARN_MAX_AGE_SEC,
    fetch_status,
    gate_output,
    read_verdict,
    standing_lines,
)
from cardinal_core.limits import limits_config as _core_limits_config  # noqa: E402
from cardinal_core.paths import AgentPaths, atomic_write_json_compact  # noqa: E402
import _otel_settings  # noqa: E402


# Bound at import time (hooks are one process per invocation) — the same
# semantics the pre-migration _limits_common module had, which the
# importlib-based tests rely on.
PATHS = AgentPaths(home=Path.home() / ".claude")


def paths() -> AgentPaths:
    return PATHS


def limits_config() -> dict | None:
    return _core_limits_config(paths())


def ingest_api_key(settings_env: dict[str, str] | None = None) -> str | None:
    """The plugin's ingest key, from OTEL_EXPORTER_OTLP_HEADERS — the same
    credential the status endpoint authenticates (and derives engineer
    identity from, server-side)."""
    return _otel_settings.ingest_api_key(settings_env)


def maybe_refresh_verdict(
    session_id: str,
    repo: str | None,
    branch: str | None,
    settings_env: dict[str, str] | None = None,
    force: bool = False,
    timeout: float = FETCH_TIMEOUT_SEC,
) -> dict | None:
    """core.limits.maybe_refresh_verdict's TTL loop with the Claude-side
    key sourcing (OTEL headers instead of cardinal-secrets.json)."""
    p = paths()
    cfg = limits_config()
    if not cfg:
        return None

    existing = read_verdict(p, session_id)
    if existing and not force:
        fetched_at = existing.get("fetched_at")
        ttl = existing.get("ttl_seconds") or DEFAULT_TTL_SEC
        if isinstance(fetched_at, (int, float)) and time.time() - fetched_at < float(ttl):
            return existing

    api_key = _otel_settings.ingest_api_key(settings_env)
    if not api_key:
        return existing

    verdict = fetch_status(
        cfg["status_url"], api_key, session_id, repo, branch, timeout=timeout
    )
    if verdict is None:
        return existing
    verdict["fetched_at"] = time.time()
    atomic_write_json_compact(p.verdict_path(session_id), verdict)
    return verdict

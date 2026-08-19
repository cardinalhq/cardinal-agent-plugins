#!/usr/bin/env python3
"""Env-gated raw hook-payload capture, shared by every Claude hook.

Phase 0.D of the Agent Execution Graph plan
(docs/local-notes/plans/agent-execution-graph.md) needs real Claude Code
session captures to replace the hand-written synthetic fixtures. The
fixture inventory (docs/local-notes/plans/phase0-fixture-inventory.md)
flagged Claude as the one adapter where this was **UNWIRED**:
`AgentPaths.debug_dir` existed, but no hook ever called a dump function.
This module is that wiring — every hook imports it and calls
`dump_if_enabled(event, payload)` as the FIRST thing after parsing
stdin, before any other processing (redaction, matcher checks, session-id
sourcing, ...), so even a hook that errors downstream still leaves a
capture behind.

Off by default; a no-op unless CARDINAL_CLAUDE_DEBUG_PAYLOADS=1 — mirrors
CARDINAL_CODEX_DEBUG_PAYLOADS / CARDINAL_CURSOR_DEBUG_PAYLOADS /
CARDINAL_GEMINI_DEBUG_PAYLOADS, which are already wired in their
respective adapters' single telemetry hook.

Unlike those three adapters' raw (unscrubbed) dumps, the payload here IS
scrubbed for known secret patterns before it touches disk
(cardinal_core.redaction.scrub_payload_recursively) before being handed
to the shared cardinal_core.paths.dump_debug_payload writer. Claude tool
payloads routinely carry file contents, command text, and transcript
excerpts that Codex/Cursor/Gemini's raw hook payloads historically have
not — a capture-time scrub is what makes these dumps safe to eventually
commit as fixtures. Structure (dict keys, list order, non-string leaf
types) is preserved; only string values change.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cardinal_core.paths import AgentPaths, dump_debug_payload
from cardinal_core.redaction import scrub_payload_recursively

DEBUG_PAYLOADS_ENV = "CARDINAL_CLAUDE_DEBUG_PAYLOADS"

# Bound at import time (hooks are one process per invocation), matching
# every other hook-scoped PATHS constant in this plugin.
PATHS = AgentPaths(home=Path.home() / ".claude")


def dump_if_enabled(event: str, payload: dict[str, Any]) -> None:
    """No-op unless CARDINAL_CLAUDE_DEBUG_PAYLOADS=1. Best-effort like
    every other telemetry side-channel in this plugin: a capture failure
    must never surface to the caller or block hook processing."""
    if os.environ.get(DEBUG_PAYLOADS_ENV) != "1":
        return
    try:
        scrubbed = scrub_payload_recursively(payload)
        dump_debug_payload(event, scrubbed, PATHS.debug_dir)
    except Exception:
        pass

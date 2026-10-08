"""Shared by the owner-input hooks: what is not a turn the owner took, and
the owner-input kill switch.

TASK_NOTIFICATION_RE: Claude Code re-enters UserPromptSubmit with a
synthetic body when a background task finishes ("<task-notification>…" or
"task-notification …"); that is not a turn the owner took, so it is never
recorded as owner input. decision-prompt.py keeps its own, narrower check
(unchanged).

CARDINAL_OWNER_INPUT=0 (false / off / no), in the environment or in
~/.claude/settings.json `env` (read through _otel_settings, as
decision-prompt.py reads its switch), turns owner input off on this
machine whatever the server advertises: nothing is captured, the outbox is
deleted instead of flushed at Stop, and SessionStart does not say prompts
are recorded. Never raises.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TASK_NOTIFICATION_RE = re.compile(r"^\s*<?task-notification[\s>]", re.IGNORECASE)

KILL_ENV = "CARDINAL_OWNER_INPUT"
OFF_VALUES = ("0", "false", "off", "no")


def is_task_notification(prompt) -> bool:
    return isinstance(prompt, str) and bool(TASK_NOTIFICATION_RE.match(prompt))


def disabled() -> bool:
    """The kill switch is set (either place counts)."""
    try:
        import _otel_settings
        sources = (_otel_settings.load_otel_settings(), os.environ)
    except Exception:
        sources = (os.environ,)
    for source in sources:
        v = source.get(KILL_ENV)
        if isinstance(v, str) and v.strip().lower() in OFF_VALUES:
            return True
    return False

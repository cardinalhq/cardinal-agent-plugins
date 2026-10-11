"""Hook-side visibility for investigation writes that could not reach Cardinal."""

from __future__ import annotations
import contextlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from . import evidence_capture as capture
from . import evidence
from . import investigation_events as ie

MARKER = "NOT RECORDED —"
THROTTLE = 600
WRITE = re.compile(r"cardinal-storyboard[\"']?\s+investigation\s+(checkpoint|question|post|ack)\b")
CODE = re.compile(
    r"(?:Process exited with code|Exit code:|exit(?:ed)?(?: with)?(?: code)?[: ]|exit_code[\"']?\s*:)\s*(-?\d+)",
    re.I,
)


def strings(value, depth=0):
    if depth > 12:
        return []
    if isinstance(value, str):
        return [value[:100000]]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v, depth + 1)]
    if isinstance(value, (list, tuple)):
        return [s for v in value[:100] for s in strings(v, depth + 1)]
    return []


def exit_code(value):
    if isinstance(value, dict):
        for key in ("exit_code", "exitCode", "returncode", "return_code"):
            if isinstance(value.get(key), int) and not isinstance(value[key], bool):
                return value[key]
        for item in value.values():
            code = exit_code(item)
            if code is not None:
                return code
    elif isinstance(value, list):
        for item in value:
            code = exit_code(item)
            if code is not None:
                return code
    elif isinstance(value, str):
        match = CODE.search(value)
        if match:
            return int(match.group(1))
    return None


def path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{sid}.failures.json"


def observe(home, sid, tool_input, response=None, error=None, *, now=None):
    """Detect before capture's opt-out/control-plane early returns. No network.

    Only reachability failures (3 or the fixed marker) count; usage/refusal
    errors do not. A successful write clears the state. Returns a throttled
    additionalContext message, independent of evidence context settings.
    """
    if not ie.valid_session(sid) or not capture.control_plane(tool_input):
        return None
    commands = strings(tool_input)
    if not any(WRITE.search(s) and not re.search(r"(?:^|\s)(?:--help|-h)(?:\s|$)", s) for s in commands):
        return None
    text = "\n".join(strings(response) + strings(error))
    code = exit_code(response)
    if code is None:
        code = exit_code(error)
    failed = code == 3 or MARKER in text
    success = (
        not failed
        and not error
        and (
            code == 0
            or (
                code is None
                and any(
                    word in text
                    for word in (
                        "checkpointed #",
                        "now asks:",
                        "posted: event #",
                        "acknowledged event #",
                        "already acknowledged (",
                        '"idempotency_key"',
                        '"question_status"',
                        '"event"',
                    )
                )
            )
        )
    )
    if not failed and not success:
        return None
    now = time.time() if now is None else now
    with ie.locked(home, sid) as locked:
        if not locked:
            return None
        if success:
            with contextlib.suppress(FileNotFoundError):
                path(home, sid).unlink()
            return None
        try:
            state = json.loads(path(home, sid).read_text())
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state["count"] = int(state.get("count") or 0) + 1
        state.setdefault("first_at", now)
        state["last_at"] = now
        reason = (
            text[text.index(MARKER) + len(MARKER) :].splitlines()[0]
            if MARKER in text
            else ie._one_line(text) or "command exited 3"
        )
        state["last_reason"] = ie._one_line(evidence.scrub_string(reason), 300)
        message = None
        if "notified_at" not in state or now - state["notified_at"] >= THROTTLE:
            since = datetime.fromtimestamp(state["first_at"]).strftime("%H:%M")
            message = (
                f"Cardinal could not record {state['count']} investigation updates since {since} "
                f"({state['last_reason']}). Tell the user now; the investigation record is not current."
            )
            state["notified_at"] = now
        ie.write_json(path(home, sid), state)
        return message

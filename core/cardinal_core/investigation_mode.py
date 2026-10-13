"""Whether sessions record to a Cardinal Investigation: the user's mode and a
per-session override.

  - The user's mode (`investigation mode record|off`): one file,
    ~/.cardinal/investigation-mode.json, shared by every runtime (Claude,
    Codex, Cursor, Gemini all bind sessions under ~/.cardinal/investigations).
    No file is `record`, the default: every connected session gets its
    Investigation and live Storyboard automatically.
  - This session only (`investigation off` / `investigation on`):
    ~/.cardinal/investigations/sessions/<sid>.mode, next to the binding (so
    investigation_events.prune removes it with the session's other files).
    It wins over the user's mode either way.

A session that does not record is never bootstrapped
(investigation_bootstrap.ensure answers "off") and reads as unbound
(investigation_events.read_binding answers None), so the poller stops,
advisory events are not delivered, owner input is not captured and
checkpoints are refused. Turning it off never deletes anything: the
Investigation stays as it was, a private draft, and `on` picks it up again.

Standard library only; never raises from the read side.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

RECORD = "record"
OFF = "off"
MODES = (RECORD, OFF)
DEFAULT = RECORD

_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def user_path(home: Path) -> Path:
    return Path(home) / ".cardinal" / "investigation-mode.json"


def session_path(home: Path, sid: str) -> Path:
    if not (isinstance(sid, str) and _SID_RE.match(sid)):
        raise ValueError(f"not a session id: {sid!r}")
    return Path(home) / ".cardinal" / "investigations" / "sessions" / f"{sid}.mode"


def _read(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = data.get("mode") if isinstance(data, dict) else None
    return mode if mode in MODES else None


def _write(path: Path, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "set_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
            f.write("\n")
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def user_mode(home: Path) -> Optional[str]:
    """The mode the user set, or None when they never set one (DEFAULT applies)."""
    return _read(user_path(home))


def set_user_mode(home: Path, mode: str) -> None:
    _write(user_path(home), mode)


def session_mode(home: Path, sid: str) -> Optional[str]:
    """This session's override, or None."""
    try:
        return _read(session_path(home, sid))
    except ValueError:
        return None


def set_session_mode(home: Path, sid: str, mode: str) -> None:
    _write(session_path(home, sid), mode)


def effective(home: Path, sid: Optional[str] = None) -> str:
    """The mode that applies to session `sid` (the user's mode without one)."""
    try:
        return (session_mode(home, sid) if sid else None) or user_mode(home) or DEFAULT
    except Exception:
        return DEFAULT


def recording(home: Path, sid: Optional[str] = None) -> bool:
    return effective(home, sid) == RECORD


def why_off(home: Path, sid: Optional[str] = None) -> str:
    """Which setting turned recording off: "this session" or "your investigation mode"."""
    return "this session" if sid and session_mode(home, sid) == OFF else "your investigation mode"


def off_line(home: Path, sid: str, cmd: str = "cardinal-storyboard investigation", target: str = "") -> str:
    """What a session that does not record is told at its start: its id
    (investigation_text.SESSION_ID_LINE, as the live guidance starts) and
    that recording is off, with the command that turns it on."""
    from .investigation_text import SESSION_ID_LINE
    on = f"{cmd} on" + (f" {target}" if target else "")
    return (SESSION_ID_LINE.format(sid=sid) + " "
            f"Cardinal investigation recording is off for this session ({why_off(home, sid)}): there is no "
            f"Investigation or Storyboard to checkpoint or link. If the user wants this session recorded, "
            f"`{on}` turns it on.")

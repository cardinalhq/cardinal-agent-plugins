"""Poll state: what has been emitted per session, so re-runs and restarts
never duplicate telemetry.

Written atomically (temp file + rename). A state file that exists but
cannot be parsed stops the poller instead of being silently reset —
resetting would re-emit every session in the lookback window.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from cardinal_core.paths import atomic_write

STATE_FILE = "devin-poll-state.json"
SCHEMA_VERSION = 2


class StateError(RuntimeError):
    pass


class PollState:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._fresh()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateError(
                f"poll state {self.path} is unreadable ({exc}); fix or move it aside. "
                "Deleting it re-emits every session in the lookback window."
            ) from None
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise StateError(f"poll state {self.path} has an unknown schema")
        data.setdefault("sessions", {})
        data.setdefault("users", {})
        data.setdefault("hwm", {})
        return data

    @staticmethod
    def _fresh() -> Dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "sessions": {}, "users": {}, "hwm": {}}

    @property
    def sessions(self) -> Dict[str, Dict[str, Any]]:
        return self.data["sessions"]

    @property
    def users(self) -> Dict[str, Dict[str, Any]]:
        return self.data["users"]

    def hwm(self, api: str) -> Optional[int]:
        value = self.data["hwm"].get(api)
        return int(value) if isinstance(value, (int, float)) else None

    def set_hwm(self, api: str, value: int) -> None:
        self.data["hwm"][api] = int(value)

    def save(self) -> None:
        if self.readonly:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(self.data, separators=(",", ":"), sort_keys=True) + "\n")

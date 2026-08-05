"""SQLite state store for the runtime.

Tables:

* ``runs`` — one row per Sentinel run.
* ``pending_asks`` — one row per ask_human node in flight, carrying the
  explicit state-machine column ``state`` (``waiting → received → parsing
  → normalized`` / ``inconclusive`` / ``deferred``).
* ``node_outputs`` — persisted node outputs (for restart-time resume).
* ``findings`` — spooled findings (Phase 2 uses ``deliveryStatus``).
* ``audit`` — append-only audit rows including late replies for gc'd runs.

Single-writer discipline: on ``open()`` we take an exclusive ``flock(2)``
on the DB file. A second ``serve`` invocation against the same DB refuses
to start and names the PID holding the lock (edge case 22).
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STATE_PATH = Path.home() / ".mechanize" / "runtime-state.db"


# Ask-human state machine.
ASK_STATE_WAITING = "waiting"
ASK_STATE_RECEIVED = "received"
ASK_STATE_PARSING = "parsing"
ASK_STATE_NORMALIZED = "normalized"
ASK_STATE_INCONCLUSIVE = "inconclusive"
ASK_STATE_DEFERRED = "deferred"

ASK_TERMINAL_STATES = {
    ASK_STATE_NORMALIZED,
    ASK_STATE_INCONCLUSIVE,
    ASK_STATE_DEFERRED,
}

# Legal transitions (source → allowed destinations). Enforced by
# ``transition()`` so bugs in orchestration surface loudly instead of
# silently corrupting audit trails.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    ASK_STATE_WAITING: {ASK_STATE_RECEIVED, ASK_STATE_INCONCLUSIVE, ASK_STATE_DEFERRED},
    ASK_STATE_RECEIVED: {ASK_STATE_PARSING, ASK_STATE_INCONCLUSIVE, ASK_STATE_DEFERRED},
    ASK_STATE_PARSING: {ASK_STATE_NORMALIZED, ASK_STATE_INCONCLUSIVE, ASK_STATE_DEFERRED},
}


class StateLockError(RuntimeError):
    """Raised when a second serve invocation loses the flock race."""


class InvalidTransitionError(RuntimeError):
    """Raised on an unexpected pending-ask state transition."""


_DDL = [
    """
    CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY,
      sentinel_digest TEXT NOT NULL,
      started_at TEXT NOT NULL,
      completed_at TEXT,
      status TEXT NOT NULL,
      inputs_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_asks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id TEXT NOT NULL,
      node_id TEXT NOT NULL,
      state TEXT NOT NULL,
      question TEXT,
      evidence_json TEXT,
      channel_id TEXT,
      handle_reference TEXT,
      raw_reply TEXT,
      operator_id TEXT,
      normalized_json TEXT,
      parser_model TEXT,
      parser_raw_output TEXT,
      inconclusive_reason TEXT,
      defer_reason TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_pending_asks_run_node
      ON pending_asks(run_id, node_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS node_outputs (
      run_id TEXT NOT NULL,
      node_id TEXT NOT NULL,
      output_json TEXT,
      state TEXT NOT NULL,
      PRIMARY KEY(run_id, node_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS findings (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id TEXT NOT NULL,
      dedupe_hash TEXT,
      finding_json TEXT NOT NULL,
      sink_id TEXT NOT NULL,
      delivery_status TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id TEXT,
      node_id TEXT,
      event_type TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Owns the sqlite DB and the flock.

    Use as a context manager to guarantee lock release:

        with StateStore.open(path) as state:
            ...
    """

    def __init__(self, path: Path, lock_fd: int, conn: sqlite3.Connection):
        self.path = path
        self._lock_fd = lock_fd
        self._conn = conn

    @classmethod
    def open(cls, path: Path | str | None = None) -> "StateStore":
        db_path = Path(path) if path else DEFAULT_STATE_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = db_path.with_suffix(db_path.suffix + ".lock")
        # Open (create if missing) the sidecar lock file. We keep the fd
        # for the lifetime of the process; closing it drops the lock.
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Read the holder's PID from the sidecar.
            try:
                with open(lock_path) as f:
                    holder_pid = f.read().strip() or "unknown"
            except OSError:
                holder_pid = "unknown"
            os.close(lock_fd)
            raise StateLockError(
                f"state DB at {db_path} is locked by PID {holder_pid}; "
                f"only one `serve` invocation is allowed per DB"
            )
        # Overwrite the lock file with our PID for the next contender to read.
        try:
            os.ftruncate(lock_fd, 0)
            os.lseek(lock_fd, 0, os.SEEK_SET)
            os.write(lock_fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for stmt in _DDL:
            conn.execute(stmt)
        return cls(db_path, lock_fd, conn)

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- runs ---------------------------------------------------------

    def start_run(
        self, run_id: str, sentinel_digest: str, inputs: dict[str, Any]
    ) -> None:
        self._conn.execute(
            "INSERT INTO runs(run_id, sentinel_digest, started_at, status, inputs_json) "
            "VALUES(?, ?, ?, 'running', ?)",
            (run_id, sentinel_digest, _utc(), json.dumps(inputs, default=str)),
        )

    def complete_run(self, run_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE runs SET completed_at=?, status=? WHERE run_id=?",
            (_utc(), status, run_id),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- pending asks -------------------------------------------------

    def create_pending_ask(
        self,
        run_id: str,
        node_id: str,
        question: str,
        evidence: dict[str, Any],
        channel_id: str,
        handle_reference: str,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO pending_asks(
              run_id, node_id, state, question, evidence_json,
              channel_id, handle_reference, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                node_id,
                ASK_STATE_WAITING,
                question,
                json.dumps(evidence, default=str),
                channel_id,
                handle_reference,
                _utc(),
                _utc(),
            ),
        )
        return int(cur.lastrowid)

    def transition(
        self,
        ask_id: int,
        to_state: str,
        **fields: Any,
    ) -> None:
        current = self._conn.execute(
            "SELECT state FROM pending_asks WHERE id=?", (ask_id,)
        ).fetchone()
        if current is None:
            raise InvalidTransitionError(f"pending_ask {ask_id} not found")
        cur_state = current["state"]
        allowed = _ALLOWED_TRANSITIONS.get(cur_state, set())
        if to_state not in allowed and to_state != cur_state:
            raise InvalidTransitionError(
                f"illegal pending_ask transition {cur_state} → {to_state} "
                f"(allowed: {sorted(allowed)})"
            )
        cols = ["state=?", "updated_at=?"]
        vals: list[Any] = [to_state, _utc()]
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
        vals.append(ask_id)
        self._conn.execute(
            f"UPDATE pending_asks SET {', '.join(cols)} WHERE id=?", vals
        )

    def get_pending_ask(self, ask_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM pending_asks WHERE id=?", (ask_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_pending_asks(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            rows = self._conn.execute(
                "SELECT * FROM pending_asks WHERE state=? ORDER BY id", (state,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM pending_asks ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- audit --------------------------------------------------------

    def audit(
        self,
        event_type: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO audit(run_id, node_id, event_type, payload_json, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (run_id, node_id, event_type, json.dumps(payload, default=str), _utc()),
        )
        # Mirror to stdout so `kubectl logs` shows the DAG play out node-by-node.
        # The SQLite row above is the authoritative record; this is for humans
        # watching the pod live. Prefixed for easy grep.
        mirror = {"type": event_type, "runId": run_id, "nodeId": node_id, **payload}
        print(f"[dag] {json.dumps(mirror, default=str)}", flush=True)

    def list_audit(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id:
            rows = self._conn.execute(
                "SELECT * FROM audit WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- findings -----------------------------------------------------

    def record_finding(
        self,
        run_id: str,
        finding: dict[str, Any],
        sink_id: str,
        delivery_status: str,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO findings(run_id, dedupe_hash, finding_json, sink_id, delivery_status, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                run_id,
                finding.get("dedupeHash"),
                json.dumps(finding, default=str),
                sink_id,
                delivery_status,
                _utc(),
            ),
        )
        return int(cur.lastrowid)


__all__ = [
    "ASK_STATE_WAITING",
    "ASK_STATE_RECEIVED",
    "ASK_STATE_PARSING",
    "ASK_STATE_NORMALIZED",
    "ASK_STATE_INCONCLUSIVE",
    "ASK_STATE_DEFERRED",
    "ASK_TERMINAL_STATES",
    "DEFAULT_STATE_PATH",
    "StateStore",
    "StateLockError",
    "InvalidTransitionError",
]

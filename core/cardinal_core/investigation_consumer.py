"""Private grant files and crash-safe review cursors for WAL consumers.

Reading persists a pending page, not a reviewed cursor. Explicit review commits
that exact batch. A retry replays the pending page, including after a crash.
These files are credentials/state, not an OS isolation boundary.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from . import investigation_events as ie
from . import investigation_grants as grants


def private_read(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        mode = os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o077 or mode.st_uid != os.getuid():
            raise ValueError("credential/state file must be owned by you and private (mode 0600)")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("credential/state file must contain a JSON object")
    return value


def private_write(path: Path, value: dict, *, create=False) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def credential_connection(path: Path) -> dict:
    raw = private_read(path)
    conn = grants.token_connection({grants.TOKEN_ENV: raw.get("token"),
                                    grants.ORIGIN_ENV: raw.get("origin")})
    if not conn:
        raise ValueError("credential file contains no investigation token")
    return conn


def identity(conn: dict) -> dict:
    return {k: conn.get(k) for k in ("origin", "org", "investigation_id", "grant_id")}


@contextlib.contextmanager
def locked(path: Path):
    import fcntl
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def load(path: Path, conn: dict) -> dict:
    try:
        state = private_read(path)
    except FileNotFoundError:
        return {"version": 1, "identity": identity(conn), "after": 0, "pending": None}
    if state.get("version") != 1 or state.get("identity") != identity(conn):
        raise ValueError("cursor belongs to another investigation, origin, or grant")
    after = state.get("after")
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise ValueError("invalid review cursor")
    return state


def committed(conn: dict, path: Path) -> int:
    """The reviewed position, read-only and unlocked (a review may hold the
    lock): where a watch starts, so re-arming it signals only unreviewed
    events instead of the whole history."""
    return load(path, conn)["after"]


def read(conn: dict, path: Path, *, client: str) -> dict:
    with locked(path):
        state = load(path, conn)
        if state.get("pending") is not None:
            return state["pending"]
        page = dict(ie.read_events(conn, conn["investigation_id"], after=state["after"], limit=100, client=client))
        if page["last_seq"] > page["head_seq"] or page["head_seq"] < state["after"]:
            raise ValueError("server returned an invalid WAL watermark")
        page["after"] = state["after"]
        page["next_after"] = page.pop("last_seq")
        page["batch"] = hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()
        if page["next_after"] > state["after"]:
            state["pending"] = page
            private_write(path, state)
        return page


def pending(conn: dict, path: Path):
    """The outstanding page (None when there is none), read under the lock."""
    with locked(path):
        return load(path, conn).get("pending")


def reviewed(conn: dict, path: Path, batch: str) -> dict:
    with locked(path):
        state = load(path, conn)
        pending = _check_review(state, conn, path, batch)
        state["after"] = pending["next_after"]
        state["pending"] = None
        state.pop("required_posts", None)
        private_write(path, state)
        return {"investigation_id": conn["investigation_id"], "after": state["after"]}


def require_posts(conn: dict, path: Path, batch: str, outboxes: list[Path]) -> None:
    """Declare intent before invoking the shell: even a quoting error must block review."""
    with locked(path):
        state = load(path, conn)
        if not state.get("pending") or state["pending"].get("batch") != batch:
            raise ValueError("--batch is not the outstanding read; cursor unchanged")
        required = state.setdefault("required_posts", [])
        for outbox in outboxes:
            name = str(outbox.absolute())
            if name not in required:
                required.append(name)
        private_write(path, state)


def _check_review(state: dict, conn: dict, path: Path, batch: str) -> dict:
    page = state.get("pending")
    if not page or page.get("batch") != batch:
        raise ValueError("--batch is not the outstanding read (cursor unchanged): pass the batch of the page you "
                         "read last, or read it again with --cursor-file")
    for name in state.get("required_posts", []):
        try:
            saved = private_read(Path(name))
            event = saved.get("result", {}).get("event", {})
            confirmed = (
                saved.get("intent", {}).get("identity") == identity(conn)
                and saved.get("review") == {"cursor": str(path.absolute()), "batch": batch}
                and isinstance(event.get("seq"), int) and not isinstance(event.get("seq"), bool)
                and event["seq"] > 0
                and event.get("investigation_id") == conn["investigation_id"]
            )
        except (OSError, ValueError, TypeError, AttributeError):
            confirmed = False
        if not confirmed:
            raise ValueError(f"required post is not confirmed: {name}; retry it with --cursor-file and --batch "
                             "before reviewing (cursor unchanged)")
    return page


def check_review(conn: dict, path: Path, batch: str) -> None:
    """Preflight before budget attribution; reviewed checks again under its commit lock."""
    with locked(path):
        _check_review(load(path, conn), conn, path, batch)


def post(conn: dict, type_: str, text: str, target: str, refs: list, *, client: str, outbox: Path,
         session_id=None, cursor: Path | None = None, batch: str | None = None) -> dict:
    if (cursor is None) != (batch is None):
        raise ValueError("a review post requires both --cursor-file and --batch")
    if cursor is not None:
        require_posts(conn, cursor, batch, [outbox])
    review = {"cursor": str(cursor.absolute()), "batch": batch} if cursor is not None else None
    payload = ie.text_payload(text, refs or None)
    # Content-addressed key survives a lost HTTP response and process restart.
    material = [identity(conn), type_, payload, target]
    key = "supervisor:" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    intent = {"identity": identity(conn), "type": type_, "payload": payload, "target": target, "key": key}
    with locked(outbox):
        try:
            saved = private_read(outbox)
        except FileNotFoundError:
            saved = {"intent": intent, "review": review}
            private_write(outbox, saved, create=True)
        if saved.get("intent") != intent or saved.get("review") != review:
            raise ValueError("outbox contains another advisory; retry its exact content or use a new file")
        # Always retry through the server: it checks revocation/expiry and deduplicates.
        producer = {"session_id": session_id} if not conn.get("token") and session_id else {}
        result = ie.append_event(conn, conn["investigation_id"], type_, payload,
                                 idempotency_key=key, to_session_id=target, client=client,
                                 producer_client=client, **producer)
        saved["result"] = result
        private_write(outbox, saved)
        return result

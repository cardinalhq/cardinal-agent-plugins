"""Owner input: what the session owner typed, recorded into the session's
Investigation by the adapter's prompt hook, and by nothing else.

maestro record-owner-input (same mcp-tools base as the other investigation
routes; the plugin's API key, the investigation's author only, never a
grantee): {investigation_id, session_id, payload: {text, truncated,
original_length, prompt_sha256, source: "user_prompt", turn, captured_at,
slash_command?}} -> {event, duplicate}. The server stamps type
owner_input.recorded, class owner_input and authority
owner_input_client_attested: it cannot tell this hook from the agent running
curl with the same key, so the record says "the author's plugin recorded
this as what the owner typed", never more. One row per (investigation,
session, turn): the same turn with the same prompt_sha256 is a replay (200
duplicate: true), with another one 409 owner_input_turn_conflict.

Capture happens only when ALL of these hold (enabled(); the adapter adds
its kill switch and skips task notifications):
  - the session's binding says it authors its Investigation (is_author true:
    a joined investigation never gets the joiner's prompts);
  - the server advertised capabilities.owner_input.enabled: true (on
    ensure-session-investigation; refreshed once per session start). An
    older Cardinal, a capability absent or off: nothing is captured, sent or
    queued.

What is sent: the prompt made wire-safe (NUL removed, an unpaired surrogate
becomes U+FFFD), scrubbed of credentials (evidence_capture.scrub_prompt) and
cut to 32 KiB of UTF-8, with the sha256 and length of the ORIGINAL prompt.

Posting (submit): synchronously within ~1.5 s. A transient failure
(network, timeout, 5xx, 408, 429) puts the prompt in the session's outbox
(<sid>.owner-input-outbox.json, 0600, bounded: the oldest entries go
first), posted oldest first by the next prompt or at Stop (flush), only
while the capability is enabled; later prompts queue without asking until a
short back-off passes. Anything else is dropped, never queued: 404 (an older
Cardinal without the route, or owner_input_disabled), 401, 403 and 405 drop
this prompt AND the whole outbox and turn the capability off in the binding
until the next session start; 400, 413, 422 or a 409 other than a turn
conflict drop that one entry. Every drop leaves one line in
<sid>.owner-input.log (0600, bounded): the code, the HTTP status and the
turn, never the prompt or its hash.

Turns: the binding's `owner_input_turn` counts the session's captured
prompts. A lost binding restarts it, so the server may already hold another
prompt at that turn (409 owner_input_turn_conflict): this session's
owner_input rows are read (class owner_input; its author may) and the
entry, with everything queued after it, is renumbered past the server's
highest turn, once; without time for that it stays queued.

When the capability turns off (refresh, ensure, a 404), the outbox is
deleted (drop_outbox).

No agent-facing path writes owner input: no MCP tool, no cardinal-storyboard
subcommand, and investigation_events.append_event refuses the type. Standard
library only. submit() and flush() never raise.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from . import investigation_events as ie
from . import investigation_state as ist
from . import investigation_state_sync as sync

TYPE = ie.OWNER_INPUT_TYPES[0]               # owner_input.recorded
CLASS = ie.OWNER_INPUT                       # owner_input
CAPABILITY = "owner_input"                   # capabilities.owner_input.enabled
ROUTE = "record-owner-input"
SOURCE = "user_prompt"
TURN_CONFLICT = "owner_input_turn_conflict"

MAX_TEXT_BYTES = 32768                       # payload.text, UTF-8 bytes, after scrubbing
SCRUB_CHARS = 2 * MAX_TEXT_BYTES             # a longer prompt is cut before the scrub
SCRUB_SLACK = 1024                           # then this much more, so a secret the cut split is gone
BUDGET = 1.5                                 # wall-clock of one prompt's requests (outbox + its own)
MIN_REQUEST = 0.2                            # less left than this: queue instead of asking
MAX_OUTBOX_ENTRIES = 50
MAX_OUTBOX_BYTES = 2 << 20
MAX_LOG_BYTES = 64 << 10
RETRY_FAILURE = 30.0                         # network, 5xx: later prompts queue without asking
RETRY_MAX = 600.0
SLASH_COMMAND_RE = re.compile(r"/[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}(?=\s|$)")  # match at the prompt's start


# ---------------------------------------------------------------------------
# Paths, gate
# ---------------------------------------------------------------------------

def outbox_path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.owner-input-outbox.json"


def log_path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{ie.binding_path(home, sid).stem}.owner-input.log"


def enabled(binding: Optional[dict]) -> bool:
    """Whether this session's prompts are captured into its binding's
    investigation: it authors it (is_author true) and the server advertised
    capabilities.owner_input.enabled: true. Anything else (no binding, a
    join, an older Cardinal, the capability absent or off) is off."""
    from . import investigation_bootstrap as boot
    return isinstance(binding, dict) and binding.get("is_author") is True \
        and boot.capability_enabled(binding.get("capabilities"), CAPABILITY)


def drop_outbox(home: Path, sid: str) -> bool:
    """Delete this session's outbox (the capability went off, the kill
    switch is set). Whether one was there. Never raises."""
    try:
        outbox_path(home, sid).unlink()
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def _iso_ms(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".%03dZ" % int((now % 1) * 1000)


def slash_command(prompt: str) -> Optional[str]:
    """The slash command the prompt starts with ("/foo", "/cardinal:connect"), or None."""
    m = SLASH_COMMAND_RE.match(prompt.lstrip()) if isinstance(prompt, str) else None
    return m.group(0) if m else None


def wire_safe(s: str) -> str:
    """`s` without what record-owner-input refuses in the text: NUL is
    removed (Postgres jsonb cannot store U+0000) and an unpaired surrogate
    becomes U+FFFD; a surrogate pair split over two code points is joined.
    Everything else stays as typed."""
    if "\x00" in s:
        s = s.replace("\x00", "")
    if any("\ud800" <= ch <= "\udfff" for ch in s):
        s = s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return s


def text_of(prompt: str) -> tuple:
    """(text, truncated): the prompt made wire-safe, scrubbed
    (evidence_capture.scrub_prompt) and cut to MAX_TEXT_BYTES of UTF-8
    without splitting a character. A very long prompt is cut before the
    scrub (bounded time), and then SCRUB_SLACK more, so nothing past a
    possibly split secret stays."""
    from .evidence_capture import scrub_prompt
    text, truncated = wire_safe(prompt), False
    if len(text) > SCRUB_CHARS:
        text, truncated = text[:SCRUB_CHARS], True
    text = scrub_prompt(text)
    if truncated:
        text = text[:max(0, len(text) - SCRUB_SLACK)]
    data = text.encode("utf-8", "replace")   # wire_safe left no lone surrogate
    if len(data) > MAX_TEXT_BYTES:
        data, truncated = data[:MAX_TEXT_BYTES], True
    return data.decode("utf-8", "ignore"), truncated


def payload(prompt: str, turn: int, now: Optional[float] = None) -> dict:
    """record-owner-input's payload for one prompt. prompt_sha256 and
    original_length are of the ORIGINAL prompt (UTF-8), before the scrub and
    the cut, so the record can be checked against the transcript."""
    if not isinstance(prompt, str):
        raise ValueError("the prompt is a string")
    if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
        raise ValueError("the turn is an integer >= 1")
    raw = prompt.encode("utf-8", "surrogatepass")
    text, truncated = text_of(prompt)
    out: dict = {"text": text, "truncated": truncated, "original_length": len(raw),
                 "prompt_sha256": hashlib.sha256(raw).hexdigest(), "source": SOURCE, "turn": turn,
                 "captured_at": _iso_ms(time.time() if now is None else now)}
    cmd = slash_command(prompt)
    if cmd:
        out["slash_command"] = cmd
    return out


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def record(conn: dict, investigation_id: str, session_id: str, body_payload: dict, *, client: str,
           opener=None, timeout: float = ie.HTTP_TIMEOUT) -> dict:
    """record-owner-input: {event, duplicate}. Raises sync.ServerError (a
    refusal) or ist.FetchError (network, or an answer without this event).
    Only with the plugin's key: never a grant token."""
    if not ie.valid_investigation(investigation_id):
        raise ist.FetchError(f"not an investigation id: {investigation_id!r}")
    if not ie.valid_session(session_id):
        raise ist.FetchError(f"not a session id: {session_id!r}")
    if conn.get("token") or not conn.get("key"):
        raise ist.FetchError("owner input is recorded only with this machine's connection")
    body = {"investigation_id": investigation_id, "session_id": session_id, "payload": body_payload}
    out = sync._post(conn, ROUTE, body, client=client, opener=opener, timeout=timeout)
    ev = out.get("event")
    if not isinstance(ev, dict) or ev.get("investigation_id") != investigation_id \
            or not isinstance(ev.get("seq"), int) or ev.get("type") != TYPE:
        raise ist.FetchError("record-owner-input answered without the event")
    return out


def disposition(err: Exception) -> str:
    """What a failed post means:
      conflict   409 owner_input_turn_conflict (renumber, once)
      transient  network, timeout, a malformed answer, 5xx, 408, 429: queue
      off        404 (no route, owner_input_disabled, no such investigation),
                 401, 403, 405: drop this and the whole outbox, capability off
      drop       any other refusal (400, 413, 422, a 409 cap): drop this entry"""
    if not isinstance(err, sync.ServerError):
        return "transient"
    if err.status == 409 and err.body.get("error") == TURN_CONFLICT:
        return "conflict"
    if err.status in (408, 429) or err.status >= 500:
        return "transient"
    if err.status in (401, 403, 404, 405):
        return "off"
    return "drop"


_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")   # fullmatch: a server error code, nothing else


def _code(err: Exception) -> str:
    """The server's error code when it is a plain identifier, else
    http_<status>: never server-supplied text."""
    if isinstance(err, sync.ServerError):
        code = err.body.get("error")
        return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else f"http_{err.status}"
    return "network"


def _retry_at(err: Exception, now: float) -> float:
    if isinstance(err, sync.ServerError) and err.status == 429 and err.retry_after:
        return now + min(max(err.retry_after, RETRY_FAILURE), RETRY_MAX)
    return now + RETRY_FAILURE


def _log(home: Path, sid: str, what: str, **fields: Any) -> None:
    """One JSON line in <sid>.owner-input.log (0600, at most ~64 KiB: the
    older half goes). Codes, statuses and turns only: never the prompt,
    never its hash."""
    with contextlib.suppress(Exception):
        ie._ensure_dirs(home)
        path = log_path(home, sid)
        line = json.dumps({"at": ie._now_iso(time.time()), "what": what, **fields}, default=str) + "\n"
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = ""
        if len(old) + len(line) > MAX_LOG_BYTES:
            old = old[len(old) // 2:].split("\n", 1)[-1]
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(old + line)
        os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------

def _entry_ok(e: Any) -> bool:
    if not isinstance(e, dict) or not isinstance(e.get("payload"), dict):
        return False
    turn = e["payload"].get("turn")
    return ie.valid_investigation(e.get("investigation_id")) and isinstance(turn, int) \
        and not isinstance(turn, bool) and turn >= 1 and isinstance(e["payload"].get("prompt_sha256"), str)


def read_outbox(home: Path, sid: str) -> dict:
    """{entries: [...oldest first], retry_after}; empty when there is none
    or it is malformed."""
    try:
        data = json.loads(outbox_path(home, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    raw = data.get("entries")
    entries = [e for e in raw if _entry_ok(e)] if isinstance(raw, list) else []
    return {"entries": entries, "retry_after": ie._num(data.get("retry_after"))}


def _entry_key(e: dict) -> tuple:
    return e.get("investigation_id"), e["payload"]["turn"], e["payload"]["prompt_sha256"]


def write_outbox(home: Path, sid: str, box: dict) -> None:
    """Atomic, 0600; no entries: the file goes. Bounded: past
    MAX_OUTBOX_ENTRIES / MAX_OUTBOX_BYTES the oldest entries go (logged)."""
    entries = list(box.get("entries") or [])
    sizes = [len(json.dumps(e, indent=2)) + 8 for e in entries]   # as write_json writes it (ASCII)
    while entries and (len(entries) > MAX_OUTBOX_ENTRIES or sum(sizes) > MAX_OUTBOX_BYTES):
        gone = entries.pop(0)
        sizes.pop(0)
        _log(home, sid, "dropped", code="outbox_full", turn=gone["payload"]["turn"])
    path = outbox_path(home, sid)
    if not entries:
        with contextlib.suppress(OSError):
            path.unlink()
        return
    ie._ensure_dirs(home)
    ie.write_json(path, {"entries": entries, "retry_after": ie._num(box.get("retry_after"))})


def server_turns(conn: dict, client: str, inv: str, sid: str, deadline: float, opener=None) -> Optional[dict]:
    """{turn: prompt_sha256} of every owner_input row the server holds for
    session `sid` in `inv` (class owner_input, page by page to the end);
    None when `deadline` (time.monotonic()) comes first. Raises what
    read_events raises."""
    rows: dict = {}
    after = 0
    while True:
        t = min(ie.HTTP_TIMEOUT, deadline - time.monotonic())
        if t < MIN_REQUEST:
            return None
        page = ie.read_events(conn, inv, after=after, limit=ie.PAGE_LIMIT, client=client, opener=opener, timeout=t,
                              class_=CLASS)
        for e in page["events"]:
            p, pr = e.get("payload"), e.get("producer")
            if ie.event_class(e) == CLASS and isinstance(p, dict) and isinstance(pr, dict) \
                    and pr.get("session_id") == sid and isinstance(p.get("turn"), int):
                rows[p["turn"]] = p.get("prompt_sha256")
        if page["page_size"] < ie.PAGE_LIMIT or page["last_seq"] >= page["head_seq"]:
            return rows
        after = page["last_seq"]


def _shift(home: Path, sid: str, pending: list, rows: dict) -> bool:
    """After a turn conflict (the binding was lost and its counter
    restarted): renumber `pending` (the entries not yet sent, oldest first;
    mutated in place) consecutively after the server's highest turn for this
    session, keeping their order; an entry the server already holds at its
    turn with its hash keeps it (a replay). The binding's counter moves past
    them. False when the session lock is busy."""
    with ie.locked(home, sid, wait=0.5) as got:
        if not got:
            return False
        turn = max(rows) if rows else 0
        for e in pending:
            p = e["payload"]
            if rows.get(p["turn"]) == p["prompt_sha256"]:
                continue
            turn += 1
            p["turn"] = turn
        b = ie.read_binding(home, sid)
        if b is not None and turn > int(ie._num(b.get("owner_input_turn"))):
            b["owner_input_turn"] = turn
            ie.write_binding(home, sid, b)
    return True


def _post_entry(conn: dict, client: str, entry: dict, inv: str, sid: str, deadline: float, opener) -> str:
    """One entry to the server: "posted", "duplicate" (a replay of a row the
    server has), "conflict" (409 owner_input_turn_conflict), "late" (no
    time left); any other failure raises."""
    t = min(ie.HTTP_TIMEOUT, deadline - time.monotonic())
    if t < MIN_REQUEST:
        return "late"
    try:
        out = record(conn, inv, sid, entry["payload"], client=client, opener=opener, timeout=t)
    except sync.ServerError as e:
        if disposition(e) == "conflict":
            return "conflict"
        raise
    return "duplicate" if out.get("duplicate") is True else "posted"


def _turn_off(home: Path, sid: str, inv: str) -> None:
    """The server does not take owner input from this session (404, 401,
    403): the outbox goes and the binding's owner_input capability is off
    until the next session start asks again."""
    drop_outbox(home, sid)
    with ie.locked(home, sid, wait=1.0) as got:
        if not got:
            return
        b = ie.read_binding(home, sid)
        if b is None or b["investigation_id"] != inv:
            return
        caps = b.get("capabilities") if isinstance(b.get("capabilities"), dict) else {}
        caps = dict(caps)
        caps[CAPABILITY] = {"enabled": False}
        b["capabilities"] = caps
        ie.write_binding(home, sid, b)
    drop_outbox(home, sid)


def _drain(home: Path, sid: str, conn: dict, client: str, deadline: float, now: float, opener,
           extra: Optional[dict] = None) -> tuple:
    """(outbox empty afterwards, what happened to `extra`, the back-off a
    transient failure set or None).

    The outbox, oldest first, then `extra` (the prompt being submitted, not
    in the outbox; mutated in place if renumbered): each is posted until one
    fails transiently, time runs out or the back-off of an earlier failure
    holds. `extra`'s outcome is "posted" / "duplicate" / "dropped", or None
    (the caller queues it). Off (not enabled(), or a 404 / 401 / 403):
    everything is dropped and the outbox deleted."""
    b = ie.read_binding(home, sid)
    if not enabled(b):
        drop_outbox(home, sid)
        return True, "dropped" if extra is not None else None, None
    box = read_outbox(home, sid)
    queued = box["entries"]
    if not queued and extra is None:
        drop_outbox(home, sid)
        return True, None, None
    entries = queued + ([extra] if extra is not None else [])
    keys = [_entry_key(e) for e in queued]   # as the outbox file has them
    inv = b["investigation_id"]
    done: set = set()      # indices settled now
    spent: list = []       # their turns as sent
    retry_after = None
    shifted = False
    status = None
    if now >= box["retry_after"]:
        i = 0
        while i < len(entries):
            e = entries[i]
            if e.get("investigation_id") != inv:
                _log(home, sid, "dropped", code="rebound", turn=e["payload"]["turn"])
                done.add(i)
                if i == len(queued):
                    status = "dropped"
                i += 1
                continue
            try:
                got = _post_entry(conn, client, e, inv, sid, deadline, opener)
            except Exception as err:
                d = disposition(err)
                if d == "transient":
                    retry_after = _retry_at(err, now)
                    break
                status_code = err.status if isinstance(err, sync.ServerError) else None
                if d == "off":
                    _log(home, sid, "dropped_all", code=_code(err), status=status_code, count=len(entries) - i)
                    _turn_off(home, sid, inv)
                    return True, "dropped" if extra is not None else None, None
                _log(home, sid, "dropped", code=_code(err), status=status_code, turn=e["payload"]["turn"])
                done.add(i)
                if i == len(queued):
                    status = "dropped"
                i += 1
                continue
            if got == "conflict" and not shifted:
                try:
                    rows = server_turns(conn, client, inv, sid, deadline, opener)
                except Exception:
                    rows = None
                if rows is not None and _shift(home, sid, entries[i:], rows):
                    shifted = True
                    _log(home, sid, "renumbered", turn=e["payload"]["turn"], server_top=max(rows or [0]),
                         count=len(entries) - i)
                    continue   # this entry again, with its new turn
            if got in ("late", "conflict"):
                break
            done.add(i)
            spent.append(e["payload"]["turn"])
            if i == len(queued):
                status = got
            i += 1
    if not queued:
        return True, status, retry_after   # only `extra`: the caller queues it with the back-off
    if not (done or shifted) and retry_after is None:
        return False, status, None   # nothing changed (the back-off holds, or no time): the outbox stays as it is
    with ie.locked(home, sid, wait=1.0) as got_lock:
        if not got_lock:
            return False, status, retry_after  # posted entries are replays next time (duplicate: true)
        box = read_outbox(home, sid)
        index = {k: i for i, k in enumerate(keys)}
        kept = []
        for e in box["entries"]:
            i = index.get(_entry_key(e))
            if i is None:
                kept.append(e)            # queued meanwhile by another hook
            elif i not in done:
                kept.append(queued[i])    # renumbered, perhaps
        box["entries"] = kept
        box["retry_after"] = retry_after if retry_after is not None else 0.0
        # The turns sent from here are spent: the binding's counter never
        # hands one of them out again.
        b2 = ie.read_binding(home, sid)
        if b2 is not None and spent and max(spent) > int(ie._num(b2.get("owner_input_turn"))):
            b2["owner_input_turn"] = max(spent)
            ie.write_binding(home, sid, b2)
        write_outbox(home, sid, box)
        return not box["entries"], status, retry_after


def _usable(conn: Any) -> bool:
    return isinstance(conn, dict) and bool(conn.get("origin") and conn.get("org") and conn.get("key")) \
        and not conn.get("token")


def flush(home: Path, sid: str, conn: dict, client: str, *, deadline: Optional[float] = None,
          now: Optional[float] = None, opener=None) -> bool:
    """Post this session's queued owner input, oldest first (at Stop),
    while the capability is enabled; when it is not, the outbox is deleted.
    Returns True when the outbox is empty afterwards. Never raises."""
    try:
        if not ie.valid_session(sid) or not outbox_path(home, sid).exists():
            return True
        if not enabled(ie.read_binding(home, sid)):
            drop_outbox(home, sid)
            return True
        if not _usable(conn):
            return False
        return _drain(home, sid, conn, client, time.monotonic() + BUDGET if deadline is None else deadline,
                      time.time() if now is None else now, opener)[0]
    except Exception:
        return False


def submit(home: Path, sid: str, prompt: str, conn: dict, client: str, *, budget: float = BUDGET,
           now: Optional[float] = None, opener=None, started: Optional[float] = None) -> str:
    """Record one owner prompt of session `sid` (the prompt hook). Returns a
    code for the debug log (never the prompt):

      posted / duplicate   recorded (a replay counts)
      queued               in the outbox (a transient failure, the back-off of
                           an earlier one, the outbox not yet empty, no time,
                           a turn conflict that could not be recovered now)
      dropped              refused for good (logged by code); nothing queued
      unbound / not_author / capability_off / no_connection / busy / invalid
                           nothing recorded, nothing queued

    The turn is taken from the binding (`owner_input_turn`, under the
    session lock) before anything is sent. Never raises."""
    started = time.monotonic() if started is None else started
    now = time.time() if now is None else now
    try:
        return _submit(home, sid, prompt, conn, client, started + budget, now, opener)
    except Exception:
        return "error"


def _submit(home, sid, prompt, conn, client, deadline, now, opener) -> str:
    if not ie.valid_session(sid) or not isinstance(prompt, str):
        return "invalid"
    b = ie.read_binding(home, sid)
    if b is None:
        return "unbound"     # no capability is known without a binding: nothing is kept
    if b.get("is_author") is not True:
        drop_outbox(home, sid)
        return "not_author"
    if not enabled(b):
        drop_outbox(home, sid)
        return "capability_off"
    if not _usable(conn):
        return "no_connection"
    with ie.locked(home, sid, wait=1.0) as got:
        if not got:
            return "busy"
        b = ie.read_binding(home, sid)
        if not enabled(b):
            drop_outbox(home, sid)
            return "capability_off"
        box = read_outbox(home, sid)
        top = max([int(ie._num(b.get("owner_input_turn")))] + [e["payload"]["turn"] for e in box["entries"]])
        turn = top + 1
        entry = {"investigation_id": b["investigation_id"], "payload": payload(prompt, turn, now),
                 "queued_at": ie._now_iso(now)}
        b["owner_input_turn"] = turn
        ie.write_binding(home, sid, b)
    try:
        _, status, retry_after = _drain(home, sid, conn, client, deadline, now, opener, extra=entry)
    except Exception:
        status, retry_after = None, None
    if status is not None:
        return status
    with ie.locked(home, sid, wait=1.0):
        if not enabled(ie.read_binding(home, sid)):
            drop_outbox(home, sid)
            return "capability_off"
        box = read_outbox(home, sid)
        box["entries"].append(entry)
        if retry_after is not None:
            box["retry_after"] = max(box["retry_after"], retry_after)
        write_outbox(home, sid, box)
    return "queued"

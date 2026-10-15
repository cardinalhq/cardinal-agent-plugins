"""Relayed owner decisions: the owner approves, in the supervisor session's
own terminal, a decision the supervisor drafted for the worker
(docs/specs/relayed-owner-decisions.md).

Two halves, and nothing else calls the route:

  propose (supervisor side, the CLI: `cardinal-storyboard investigation
      propose-decision`): stores a local PROPOSAL and prints its id. It never
      sends anything: a proposal never approves itself.
  approve (the prompt hook of every runtime, handle_prompt): the owner types
      `approve dp_xxxxxxxx` (bare `approve` when it is the only pending
      proposal) or `reject dp_xxxxxxxx` as a prompt in the supervisor
      session. The hook relays the proposal to maestro's
      relay-owner-decision with this machine's API key, removes it, and
      reports the outcome to the user and to the model. No CLI subcommand
      and no MCP tool calls relay().

Trust (say exactly this, never more): the server checks that the key
belongs to a person who authors the worker's investigation, that this
person turned on "accept owner decisions approved in my supervisor
sessions" from their signed-in web session, that the claimed supervisor
session is an author-claimed session of an investigation the same person
authors, and that the stored text is the text whose sha256 the relay
carries. It cannot verify that a human typed `approve`: the agent holds the
same key. The worker's event is authority `owner_relayed` (attested by the
owner's machine), weaker than `owner_decision` (web session).

Proposals, one file per supervisor session, next to its binding:

  ~/.cardinal/investigations/sessions/<sid>.proposals.json   (0600, under the binding's flock)
  {"proposals": [{id, investigation_id, to_session_id, text, sha256, answers?, refs,
                  created_at, expires_at}],
   "recent": [{id, outcome, message, prompt_sha256, at}]}

id = "dp_" + 8 random hex; a proposal expires after PROPOSAL_TTL (30 min);
at most MAX_PROPOSALS pending (the oldest goes). `recent` keeps the last
outcomes for RECENT_TTL: `investigation proposals` lists them (Cursor has no
user-visible channel on its prompt hook), and the same approving prompt
seen again within REFIRE_WINDOW (a runtime that re-fires its prompt hook)
repeats its outcome instead of acting twice.

The prompt hook (handle_prompt) runs before, and independently of,
owner-input capture: owner input may be off. It refuses a subagent's prompt
(a non-empty agent_id) and a machine prompt (owner_input.normalize_prompt
must reduce it to typed text), and acts only on a whole stripped prompt
matching APPROVAL_RE (case-insensitive). Anything else is left untouched
and the prompt always reaches the model.

Standard library only. handle_prompt never raises.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import investigation_events as ie
from . import investigation_state as ist
from . import investigation_state_sync as sync

ROUTE = "relay-owner-decision"
CAPABILITY = "owner_relay"            # capabilities.owner_relay.enabled
AUTHORITY = "owner_relayed"           # the authority the server stamps on a relayed owner.decided
PROPOSAL_TTL = 30 * 60.0
MAX_PROPOSALS = 5
RECENT_TTL = 30 * 60.0
MAX_RECENT = 10
REFIRE_WINDOW = 30.0
BUDGET = 3.0                          # wall-clock of the relay request, from the hook's start
MAX_TEXT = 4000
MAX_REFS = 20
MAX_REF = 300
ID_RE = re.compile(r"dp_[0-9a-f]{8}")                                              # fullmatch
APPROVAL_RE = re.compile(r"(approve|reject)(?:\s+(dp_[0-9a-f]{8}))?[.!]?", re.IGNORECASE)  # fullmatch, stripped
SETTINGS_URL_RE = re.compile(r"https?://[^\s\"'<>\\]{1,300}")                       # fullmatch

PROPOSE_HELP = ("Draft an owner decision for the worker session you supervise. Nothing is sent: the owner approves "
                "it by typing `approve <id>` (or `reject <id>`) as a prompt in THIS session, and only then does this "
                "session's prompt hook relay it to the worker (authority owner_relayed). It uses this machine's own "
                "key, never an access grant: no --credential-file.")
INSTRUCTION = ("Show the owner this text verbatim and ask them to reply `approve {id}` or `reject {id}`. Never approve "
               "it yourself and never call the relay route: only the owner's own prompt approves.")
NOT_CAPABLE = ("this Cardinal cannot relay owner decisions (it does not advertise owner_relay; it needs a newer "
               "Maestro): give the owner the decision to type into the worker's own terminal instead")
NOT_ENABLED = "owner_relay_not_enabled"
UNSUPPORTED = ("this Cardinal cannot relay owner decisions (it needs a newer Maestro); tell the owner to type the "
               "decision into the worker's own terminal instead")
_ROUTE_404S = ("investigation_not_found", "owner_decision_answer_not_found")

# The route's refusals, in words (never a raw status line or server text).
REFUSALS = {
    "owner_relay_requires_api_key": ("a relayed decision needs this machine's Cardinal API key (not a web session or "
                                     "an access grant)"),
    "owner_relay_requires_user_key": ("this machine's Cardinal API key acts for no person; a relayed decision needs a "
                                      "key that belongs to you (reconnect with cardinal-connect)"),
    "invalid_owner_relay": "Cardinal refused the relay as malformed",
    "investigation_not_found": "there is no such investigation in this org",
    "owner_relay_requires_investigation_author": ("only the worker investigation's author can relay a decision to "
                                                  "it, and this machine's key is not that person's"),
    "owner_relay_self": "a supervisor session cannot relay a decision to its own investigation",
    "owner_relay_supervisor_not_yours": ("Cardinal could not confirm this supervisor session belongs to an "
                                         "investigation you author"),
    "owner_relay_target_not_author_session": ("the target session is not a session the worker investigation's "
                                              "author claimed"),
    "owner_relay_text_mismatch": "the decision text did not match its hash",
    "owner_decision_answer_not_found": "the event it answers (--answers) is not in the worker's investigation",
    "idempotency_conflict": "another decision was already relayed under this proposal id",
    "owner_decision_limit_reached": "the worker's investigation reached its limit of owner decisions",
    "event_limit_reached": "the worker's investigation reached its limit of events",
}


# ---------------------------------------------------------------------------
# The proposal store
# ---------------------------------------------------------------------------

def proposals_path(home: Path, sid: str) -> Path:
    if not ie.valid_session(sid):
        raise ValueError(f"not a session id: {sid!r}")
    return ie.sessions_dir(home) / f"{sid}.proposals.json"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_ok(e: Any, now: float) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("id"), str) and bool(ID_RE.fullmatch(e["id"]))
            and ie.valid_investigation(e.get("investigation_id")) and ie.valid_session(e.get("to_session_id"))
            and isinstance(e.get("text"), str) and 0 < len(e["text"]) <= MAX_TEXT
            and e.get("sha256") == sha256_text(e["text"])
            and all(isinstance(e.get(k), (int, float)) and not isinstance(e[k], bool)
                    for k in ("created_at", "expires_at"))
            and now < e["expires_at"]
            and isinstance(e.get("refs", []), list)
            and (e.get("answers") is None or (isinstance(e["answers"], int) and not isinstance(e["answers"], bool))))


def _recent_ok(r: Any, now: float) -> bool:
    return (isinstance(r, dict) and isinstance(r.get("id"), str) and bool(ID_RE.fullmatch(r["id"]))
            and isinstance(r.get("message"), str) and isinstance(r.get("at"), (int, float))
            and 0 <= now - r["at"] < RECENT_TTL)


def _read(home: Path, sid: str, now: float) -> dict:
    """The store, without expired or malformed entries (an unreadable file
    is empty)."""
    try:
        data = json.loads(proposals_path(home, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    pending = [e for e in data.get("proposals") or [] if _entry_ok(e, now)] \
        if isinstance(data.get("proposals"), list) else []
    recent = [r for r in data.get("recent") or [] if _recent_ok(r, now)] \
        if isinstance(data.get("recent"), list) else []
    return {"proposals": pending[-MAX_PROPOSALS:], "recent": recent[-MAX_RECENT:]}


def _write(home: Path, sid: str, store: dict) -> None:
    path = proposals_path(home, sid)
    if not store["proposals"] and not store["recent"]:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    ie._ensure_dirs(home)
    ie.write_json(path, store)


def pending(home: Path, sid: str, now: Optional[float] = None) -> list:
    """The unexpired proposals of session `sid`, oldest first."""
    return _read(home, sid, time.time() if now is None else now)["proposals"]


def recent(home: Path, sid: str, now: Optional[float] = None) -> list:
    """The latest outcomes (approve / reject) of session `sid`, oldest first."""
    return _read(home, sid, time.time() if now is None else now)["recent"]


def validate(investigation_id: Any, to_session_id: Any, text: Any, answers: Any = None,
             refs: Any = None) -> None:
    """Raises ValueError (in words) when the proposal cannot be relayed as
    given."""
    from .owner_input import wire_safe
    if not ie.valid_investigation(investigation_id):
        raise ValueError(f"not an investigation id: {investigation_id!r} (inv_ followed by 24 hex characters)")
    if not ie.valid_session(to_session_id):
        raise ValueError("--to-session is the worker's session id (letters, digits, _ and -; at most 128)")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("--text is the decision, 1 to 4000 characters")
    if len(text) > MAX_TEXT:
        raise ValueError(f"--text is at most {MAX_TEXT} characters")
    if wire_safe(text) != text:
        raise ValueError("--text contains a NUL or an unpaired surrogate")
    if answers is not None and (not isinstance(answers, int) or isinstance(answers, bool) or answers < 1):
        raise ValueError("--answers is an event number (a positive integer)")
    refs = refs or []
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        raise ValueError(f"at most {MAX_REFS} --ref")
    for r in refs:
        if not isinstance(r, str) or not r.strip() or len(r) > MAX_REF or wire_safe(r) != r:
            raise ValueError(f"each --ref is 1 to {MAX_REF} characters")


def propose(home: Path, sid: str, investigation_id: str, to_session_id: str, text: str, *,
            answers: Optional[int] = None, refs: Optional[list] = None, now: Optional[float] = None) -> dict:
    """Store a proposal of supervisor session `sid` and return it. Sends
    nothing. Raises ValueError (invalid input) or OSError (the store is
    locked or cannot be written)."""
    validate(investigation_id, to_session_id, text, answers, refs)
    now = time.time() if now is None else now
    with ie.locked(home, sid, wait=2.0) as got:
        if not got:
            raise OSError("the session's proposals are locked by another process")
        store = _read(home, sid, now)
        taken = {e["id"] for e in store["proposals"]} | {r["id"] for r in store["recent"]}
        pid = "dp_" + secrets.token_hex(4)
        while pid in taken:
            pid = "dp_" + secrets.token_hex(4)
        entry: dict = {"id": pid, "investigation_id": investigation_id, "to_session_id": to_session_id,
                       "text": text, "sha256": sha256_text(text), "refs": list(refs or []),
                       "created_at": now, "expires_at": now + PROPOSAL_TTL}
        if answers is not None:
            entry["answers"] = answers
        store["proposals"] = (store["proposals"] + [entry])[-MAX_PROPOSALS:]
        _write(home, sid, store)
        return entry


def withdraw(home: Path, sid: str, pid: str, now: Optional[float] = None) -> Optional[dict]:
    """Remove pending proposal `pid` (nothing is sent); the entry, or None
    when it is not pending. Raises OSError when the store is locked."""
    return _take(home, sid, pid, time.time() if now is None else now)


def _take(home: Path, sid: str, pid: str, now: float, outcome: Optional[dict] = None) -> Optional[dict]:
    with ie.locked(home, sid, wait=1.0) as got:
        if not got:
            raise OSError("the session's proposals are locked by another process")
        store = _read(home, sid, now)
        entry = next((e for e in store["proposals"] if e["id"] == pid), None)
        store["proposals"] = [e for e in store["proposals"] if e["id"] != pid]
        if outcome is not None:
            store["recent"] = (store["recent"] + [outcome])[-MAX_RECENT:]
        if entry is not None or outcome is not None:
            _write(home, sid, store)
        return entry


def _note(home: Path, sid: str, outcome: dict, now: float) -> None:
    """Remember an outcome that left the proposal pending. Never raises."""
    with contextlib.suppress(Exception):
        with ie.locked(home, sid, wait=1.0) as got:
            if got:
                store = _read(home, sid, now)
                store["recent"] = (store["recent"] + [outcome])[-MAX_RECENT:]
                _write(home, sid, store)


# ---------------------------------------------------------------------------
# The route (called only by handle_prompt, i.e. the prompt hook)
# ---------------------------------------------------------------------------

def relay_body(entry: dict, *, supervisor_session_id: str, supervisor_investigation_id: str,
               prompt_sha256: str, approved_at: str, client: str) -> dict:
    payload: dict = {"text": entry["text"]}
    if entry.get("answers") is not None:
        payload["answers"] = entry["answers"]
    if entry.get("refs"):
        payload["refs"] = list(entry["refs"])
    return {"investigation_id": entry["investigation_id"], "to_session_id": entry["to_session_id"],
            "relay_key": entry["id"], "payload": payload,
            "relay": {"supervisor_session_id": supervisor_session_id,
                      "supervisor_investigation_id": supervisor_investigation_id,
                      "proposal_sha256": sha256_text(entry["text"]), "prompt_sha256": prompt_sha256,
                      "approved_at": approved_at},
            "client": client}


def relay(conn: dict, body: dict, *, client: str, opener=None, timeout: float = BUDGET) -> dict:
    """relay-owner-decision: {event, duplicate}. Only with this machine's
    key, never a grant token. Raises sync.ServerError (a refusal) or
    ist.FetchError (network, or an answer without the event)."""
    if conn.get("token") or not conn.get("key"):
        raise ist.FetchError("an owner decision is relayed only with this machine's connection")
    out = sync._post(conn, ROUTE, body, client=client, opener=opener, timeout=timeout)
    ev = out.get("event")
    if not isinstance(ev, dict) or ev.get("investigation_id") != body["investigation_id"] \
            or not isinstance(ev.get("seq"), int) or isinstance(ev.get("seq"), bool) \
            or ev.get("type") != ie.OWNER_DECIDED:
        raise ist.FetchError(f"{ROUTE} answered without the event")
    return out


def _bounded(fn: Callable[[], Any], deadline: float) -> tuple:
    """("done", value) | ("raised", exception) | ("timeout", None): fn in a
    daemon thread joined until `deadline` (time.monotonic())."""
    out: dict = {}

    def work() -> None:
        try:
            out["v"] = ("done", fn())
        except BaseException as e:   # noqa: BLE001 (reported to the caller)
            out["v"] = ("raised", e)

    t = threading.Thread(target=work, name="owner-relay", daemon=True)
    t.start()
    t.join(max(0.0, deadline - time.monotonic()))
    if t.is_alive() or "v" not in out:
        return "timeout", None
    return out["v"]


def transient(err: Exception) -> bool:
    """Kept for a retry: network, timeout, 5xx (503 owner_relay_unavailable:
    the server is not migrated yet), 408, 429."""
    if isinstance(err, ist.FetchError):
        return True
    return isinstance(err, sync.ServerError) and (err.status >= 500 or err.status in (408, 429))


def unsupported(err: "sync.ServerError") -> bool:
    code = err.body.get("error") if isinstance(err.body, dict) else None
    if err.status == 403 and code == "insufficient_scope":
        return True
    return err.status in (404, 405) and code not in _ROUTE_404S


def refusal(err: "sync.ServerError") -> str:
    """A refusal in words: inert, one line, never the server's own text."""
    if unsupported(err):
        return UNSUPPORTED
    code = err.body.get("error") if isinstance(err.body, dict) else None
    said = REFUSALS.get(code) if isinstance(code, str) else None
    if said:
        return said
    return f"HTTP {int(err.status)}, {ie._tok(code) if code else 'no reason given'}"


def settings_url(err: "sync.ServerError") -> Optional[str]:
    url = err.body.get("settings_url") if isinstance(err.body, dict) else None
    return url if isinstance(url, str) and SETTINGS_URL_RE.fullmatch(url) else None


# ---------------------------------------------------------------------------
# The prompt hook
# ---------------------------------------------------------------------------

@dataclass
class RelayResult:
    """What handle_prompt did. status: a debug code. message: the outcome to
    show the user (systemMessage, or the runtime's equivalent) AND to give
    the model (additional context), or None (the prompt is not an approval:
    nothing to say)."""
    status: str
    message: Optional[str] = None
    ids: list = field(default_factory=list)


def match(prompt: Any) -> Optional[tuple]:
    """(verb "approve"|"reject", id or None) when the whole stripped prompt
    is an approval, else None."""
    if not isinstance(prompt, str) or len(prompt) > 200:
        return None
    m = APPROVAL_RE.fullmatch(prompt.strip())
    if not m:
        return None
    return m.group(1).lower(), (m.group(2).lower() if m.group(2) else None)


def _short(sid: str) -> str:
    return sid[:8]


def _pending_line(entries: list) -> str:
    return ", ".join(e["id"] for e in entries)


def handle_prompt(home: Path, sid: str, prompt: Any, *, runtime: str, conn: Optional[dict], client: str,
                  agent_id: Optional[str] = None, deadline: float, now: Optional[float] = None,
                  opener=None) -> RelayResult:
    """The approval half of every runtime's prompt hook (see the module
    doc). Statuses:

      invalid / subagent / not_typed / no_match / none
                      nothing said: not an approval of anything here
      repeat          DEDUPE_REPEATS runtimes (Gemini) only: the same
                      approving prompt again within REFIRE_WINDOW, with no
                      newer proposal: its outcome is repeated, nothing is
                      sent again
      ambiguous       bare form, several pending: says which ids are
      unknown_id      the id is not pending (expired, withdrawn, handled)
      rejected        removed; nothing sent
      relayed / duplicate
                      relay-owner-decision answered 201 / 200; removed
      transient       network, timeout, 5xx, 408, 429: kept
      not_enabled     403 owner_relay_not_enabled: kept, with the settings link
      refused         any other refusal: removed, with the reason
      unbound / not_author / own_investigation / no_connection / busy
                      not sent; kept
    Never raises (error)."""
    try:
        return _handle(home, sid, prompt, runtime, conn, client, agent_id, deadline,
                       time.time() if now is None else now, opener)
    except Exception:
        return RelayResult("error")


def _handle(home, sid, prompt, runtime, conn, client, agent_id, deadline, now, opener) -> RelayResult:
    from . import owner_input
    if runtime not in owner_input.RUNTIMES or not ie.valid_session(sid):
        return RelayResult("invalid")
    if agent_id:
        return RelayResult("subagent")
    typed = owner_input.normalize_prompt(runtime, prompt)
    if typed is None:
        return RelayResult("not_typed")
    got = match(typed)
    if got is None:
        return RelayResult("no_match")
    verb, pid = got
    typed_sha = hashlib.sha256(owner_input.scrubbed_of(typed)[0].encode("utf-8", "replace")).hexdigest()
    store = _read(home, sid, now)
    entries = store["proposals"]
    if runtime in owner_input.DEDUPE_REPEATS:
        # A runtime that re-fires its prompt hook for one prompt: the same
        # approval seen again moments later repeats its outcome.
        again = next((r for r in reversed(store["recent"]) if r.get("prompt_sha256") == typed_sha
                      and 0 <= now - r["at"] < REFIRE_WINDOW and (pid is None or r["id"] == pid)), None)
        if again is not None and not any(e["id"] == again["id"] or e["created_at"] > again["at"]
                                         for e in entries):
            return RelayResult("repeat", again["message"], [again["id"]])
    if pid is None:
        if not entries:
            return RelayResult("none")
        if len(entries) > 1:
            return RelayResult("ambiguous", (
                f"Cardinal: {len(entries)} owner-decision proposals are pending ({_pending_line(entries)}); nothing "
                f"was {'relayed' if verb == 'approve' else 'rejected'}. Reply `{verb} <id>` with one of them."),
                [e["id"] for e in entries])
        entry = entries[0]
    else:
        entry = next((e for e in entries if e["id"] == pid), None)
        if entry is None:
            left = f" Pending: {_pending_line(entries)}." if entries else " None is pending."
            return RelayResult("unknown_id", (
                f"Cardinal: there is no pending owner-decision proposal {pid} in this session (it expired, was "
                f"withdrawn or was already handled); nothing was sent.{left}"), [pid])
    pid = entry["id"]
    to = _short(entry["to_session_id"])

    def outcome(status: str, message: str) -> dict:
        return {"id": pid, "outcome": status, "message": message, "prompt_sha256": typed_sha, "at": now}

    if verb == "reject":
        message = (f"Cardinal: rejected owner-decision proposal {pid} (for session {to}); nothing was sent to "
                   "the worker.")
        try:
            _take(home, sid, pid, now, outcome("rejected", message))
        except OSError:
            return RelayResult("busy", f"Cardinal: proposal {pid} could not be rejected now (its file is busy); "
                                       f"reply `reject {pid}` again.", [pid])
        return RelayResult("rejected", message, [pid])

    retry = f"retry with `approve {pid}`"
    b = ie.read_binding(home, sid)
    if not b:
        return RelayResult("unbound", f"Cardinal: owner decision {pid} was NOT relayed: this session has no "
                                      f"Investigation yet; {retry} once it has one.", [pid])
    if b.get("is_author") is not True:
        return RelayResult("not_author", f"Cardinal: owner decision {pid} was NOT relayed: this session does not "
                                         "author its Investigation, so it cannot relay from it.", [pid])
    if b.get("investigation_id") == entry["investigation_id"]:
        return RelayResult("own_investigation", f"Cardinal: owner decision {pid} was NOT relayed: it is addressed "
                                                "to this session's own Investigation.", [pid])
    if not (isinstance(conn, dict) and conn.get("origin") and conn.get("org") and conn.get("key")
            and not conn.get("token")):
        return RelayResult("no_connection", f"Cardinal: owner decision {pid} was NOT relayed: this machine is not "
                                            f"connected to Cardinal with its own key; {retry} once it is.", [pid])
    body = relay_body(entry, supervisor_session_id=sid, supervisor_investigation_id=b["investigation_id"],
                      prompt_sha256=typed_sha, approved_at=ie._now_iso(now), client=client)
    timeout = max(0.1, min(BUDGET, deadline - time.monotonic()))
    state, value = _bounded(lambda: relay(conn, body, client=client, opener=opener, timeout=timeout), deadline)
    if state == "done":
        ev = value["event"]
        dup = value.get("duplicate") is True
        message = (f"Cardinal: {'already relayed' if dup else 'relayed'} owner decision {pid} to session {to} as "
                   f"event #{ev['seq']} ({AUTHORITY}). The worker receives it at its next tool boundary.")
        with contextlib.suppress(OSError):
            _take(home, sid, pid, now, outcome("duplicate" if dup else "relayed", message))
        return RelayResult("duplicate" if dup else "relayed", message, [pid])
    err = value if state == "raised" else None
    if err is None or transient(err) or not isinstance(err, sync.ServerError):
        message = (f"Cardinal: owner decision {pid} was NOT relayed (Cardinal did not answer in time or could not be "
                   f"reached); it is still pending: {retry}.")
        _note(home, sid, outcome("transient", message), now)
        return RelayResult("transient", message, [pid])
    code = err.body.get("error") if isinstance(err.body, dict) else None
    if err.status == 403 and code == NOT_ENABLED:
        url = settings_url(err)
        where = f"at {ie._tok(url)}" if url else "in your Cardinal settings (Settings, Investigations)"
        message = (f"Cardinal: owner decision {pid} was NOT relayed: relayed owner decisions are off for your "
                   "account. Turn on \"Accept owner decisions I approve in my supervisor sessions\" once, from your "
                   f"signed-in Cardinal web session, {where}; then {retry}. It is still pending.")
        _note(home, sid, outcome("not_enabled", message), now)
        return RelayResult("not_enabled", message, [pid])
    message = f"Cardinal refused owner decision {pid} ({refusal(err)}); the proposal was dropped."
    with contextlib.suppress(OSError):
        _take(home, sid, pid, now, outcome("refused", message))
    return RelayResult("refused", message, [pid])

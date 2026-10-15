"""Relayed owner decisions: the owner decides, in the supervisor session's
own terminal, for the worker that session supervises
(docs/specs/relayed-owner-decisions.md).

Two ways, and nothing but the prompt hook calls the route:

  verbatim: the owner types `decide: <text>` (or `decide <name>: <text>`)
      as the whole prompt in the supervisor session. The prompt hook relays
      <text>, the owner's own words, unchanged, to the registered worker
      target (`cardinal-storyboard investigation supervising`): relay
      source "verbatim", relay key dv_ + 16 hex. No draft, no approve step.
  proposal: the supervisor drafts (`cardinal-storyboard investigation
      propose-decision`, a local PROPOSAL dp_ + 16 hex; nothing is sent: a
      proposal never approves itself) and the owner types `approve
      dp_xxxxxxxxxxxxxxxx` (bare `approve` while it is the only pending
      proposal) or `reject dp_xxxxxxxxxxxxxxxx`. The hook relays it (relay
      source "proposal", relay key = the proposal id) or drops it.

Either way the hook posts maestro's relay-owner-decision with this
machine's API key (bounded, BUDGET) and reports the outcome to the user and
to the model. No CLI subcommand and no MCP tool calls relay().

Trust (say exactly this, never more): the server checks that the key
belongs to a person who authors the worker's investigation, that this
person turned on "accept owner decisions approved in my supervisor
sessions" from their signed-in web session, that the claimed supervisor
session is an author-claimed session of an investigation the same person
authors, and that the stored text is the text whose sha256 the relay
carries. It cannot verify that a human typed the prompt: the agent holds
the same key. The worker's event is authority `owner_relayed` (attested by
the owner's machine), weaker than `owner_decision` (web session).

Local state, one file per supervisor session, next to its binding:

  ~/.cardinal/investigations/sessions/<sid>.proposals.json   (0600, under the binding's flock)
  {"proposals": [{id, investigation_id, to_session_id, text, sha256, answers?, refs,
                  created_at, expires_at}],
   "targets":   [{name?, investigation_id, to_session_id, created_at}],
   "verbatim":  [{key, investigation_id, to_session_id, sha256, at}],
   "recent":    [{id, outcome, message, prompt_sha256, at}]}

A proposal expires after PROPOSAL_TTL (30 min); at most MAX_PROPOSALS
pending (the oldest goes). Targets (at most MAX_TARGETS; the oldest goes)
are the workers this session supervises: `supervising` registers one,
propose-decision registers its own. `verbatim` remembers the relay key of a
`decide` that was not relayed (transient, not enabled) for RECENT_TTL, so
retyping the same decision for the same target reuses its key: the server
answers a duplicate instead of storing it twice. `recent` keeps the last
outcomes for RECENT_TTL: `investigation proposals` lists them (Cursor has
no user-visible channel on its prompt hook), and on a runtime that re-fires
its prompt hook (Gemini) the same prompt seen again within REFIRE_WINDOW
repeats a final outcome instead of acting twice.

The prompt hook (handle_prompt) runs before, and independently of,
owner-input capture: owner input may be off. It refuses a subagent's prompt
(a non-empty agent_id) and a machine prompt (owner_input.normalize_prompt
must reduce it to typed text), and acts only on a whole stripped prompt
matching APPROVAL_RE or DECIDE_RE (case-insensitive). Anything else is left
untouched and the prompt always reaches the model.

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
SOURCE_PROPOSAL = "proposal"          # relay.source: a supervisor's draft the owner approved
SOURCE_VERBATIM = "verbatim"          # relay.source: the owner's own words (`decide: …`)
PROPOSAL_TTL = 30 * 60.0
MAX_PROPOSALS = 5
MAX_TARGETS = 5
RECENT_TTL = 30 * 60.0
MAX_RECENT = 10
MAX_VERBATIM = 5
REFIRE_WINDOW = 30.0
BUDGET = 3.0                          # wall-clock of the relay request, from the hook's start
MAX_TEXT = 4000
MAX_REFS = 20
MAX_REF = 300
ID_RE = re.compile(r"dp_[0-9a-f]{16}")                                              # fullmatch
VERBATIM_KEY_RE = re.compile(r"dv_[0-9a-f]{16}")                                    # fullmatch
NAME_RE = re.compile(r"[a-z0-9-]{1,32}")                                            # fullmatch
APPROVAL_RE = re.compile(r"(approve|reject)(?:\s+(dp_[0-9a-f]{16}))?[.!]?", re.IGNORECASE)  # fullmatch, stripped
DECIDE_RE = re.compile(r"decide(?:[ \t]+([a-z0-9-]{1,32}))?[ \t]*:(.*)", re.IGNORECASE | re.DOTALL)  # fullmatch
MAYBE_RE = re.compile(r"\s*(?:approve|reject|decide)\b", re.IGNORECASE)            # match: a cheap pre-check
ORIGIN_RE = re.compile(r"https?://[A-Za-z0-9.:-]{1,253}")                            # fullmatch
FINAL_OUTCOMES = ("relayed", "duplicate", "refused", "rejected")

PROPOSE_HELP = ("Draft an owner decision for the worker session you supervise. Nothing is sent: the owner approves "
                "it by typing `approve <id>` (or `reject <id>`) as a prompt in THIS session, and only then does this "
                "session's prompt hook relay it to the worker (authority owner_relayed). It uses this machine's own "
                "key, never an access grant: no --credential-file. It also registers the worker as a target "
                "(see `supervising`).")
SUPERVISING_HELP = ("Register the worker this session supervises (a target), so the owner can type `decide: <text>` "
                    "(or `decide <name>: <text>`) as a prompt here to send the worker their own words as an owner "
                    "decision. Local only; nothing is sent. Without arguments: list the targets. --remove drops one "
                    "(by --name, or by investigation and --to-session).")
INSTRUCTION = ("Show the owner this text verbatim and ask them to reply `approve {id}` or `reject {id}`. Never approve "
               "it yourself and never call the relay route: only the owner's own prompt approves.")
NOT_CAPABLE = ("this Cardinal cannot send owner decisions from here yet (it needs a newer Maestro). Type your "
               "decision in the worker's own terminal, or post it from the Investigation page on the web.")
NOT_ENABLED = "owner_relay_not_enabled"
UNSUPPORTED = NOT_CAPABLE
TOGGLE = "Accept owner decisions I send from my supervisor sessions"   # the web setting's label
CREDENTIAL = ("Cardinal: your decision looks like it contains a credential, so nothing was sent. Remove it and type "
              "the decision again; give the worker secrets through its environment, not a decision.")
QUESTION = ("Cardinal: that reads as a question, so it was not sent to the worker. Rephrase it as a decision to send "
            "it.")
NO_DRAFT = "Cardinal: no draft is waiting for your approval (drafts expire after 30 minutes); nothing was sent."
NO_WORKER = ("Cardinal: no worker is registered here yet; ask the supervisor to register the one it watches. Nothing "
             "was sent.")
SEES_IT = ("The worker sees it after its next tool call; if it is waiting for you, when it next runs (any prompt in "
           "its terminal). To change it, send another decide:; the worker sees both.")
EXCERPT = 160
_ROUTE_404S = ("investigation_not_found", "owner_decision_answer_not_found")

# The route's refusals, in words for the owner (never a raw status line or
# server text; the code goes to the debug log only).
REFUSALS = {
    "owner_relay_requires_api_key": "it needs this machine's Cardinal API key, not a web session or an access grant",
    "owner_relay_requires_user_key": ("this machine's Cardinal API key belongs to no person; reconnect it with a key "
                                      "of your own"),
    "invalid_owner_relay": "Cardinal could not read the request",
    "investigation_not_found": "the worker's Investigation is not in this org",
    "owner_relay_requires_investigation_author": ("only the person who owns the worker's Investigation can send it "
                                                  "decisions, and this machine's key is not theirs"),
    "owner_relay_self": "it was addressed to this session's own Investigation",
    "owner_relay_supervisor_not_yours": "Cardinal could not confirm this supervisor session is yours",
    "owner_relay_target_not_author_session": "the worker session is not one of that Investigation's own sessions",
    "owner_relay_text_mismatch": "the text changed on the way",
    "owner_decision_answer_not_found": "the worker event it answers is not in the worker's Investigation",
    "idempotency_conflict": "this draft was already sent with different text",
    "owner_decision_limit_reached": "the worker's Investigation reached its limit of owner decisions",
    "event_limit_reached": "the worker's Investigation reached its limit of events",
}


# ---------------------------------------------------------------------------
# The local store
# ---------------------------------------------------------------------------

def proposals_path(home: Path, sid: str) -> Path:
    if not ie.valid_session(sid):
        raise ValueError(f"not a session id: {sid!r}")
    return ie.sessions_dir(home) / f"{sid}.proposals.json"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _entry_ok(e: Any, now: float) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("id"), str) and bool(ID_RE.fullmatch(e["id"]))
            and ie.valid_investigation(e.get("investigation_id")) and ie.valid_session(e.get("to_session_id"))
            and isinstance(e.get("text"), str) and 0 < len(e["text"]) <= MAX_TEXT
            and e.get("sha256") == sha256_text(e["text"])
            and _num(e.get("created_at")) and _num(e.get("expires_at")) and now < e["expires_at"]
            and isinstance(e.get("refs", []), list)
            and (e.get("answers") is None or (isinstance(e["answers"], int) and not isinstance(e["answers"], bool))))


def _target_ok(t: Any) -> bool:
    return (isinstance(t, dict) and ie.valid_investigation(t.get("investigation_id"))
            and ie.valid_session(t.get("to_session_id")) and _num(t.get("created_at"))
            and (t.get("name") is None or (isinstance(t["name"], str) and bool(NAME_RE.fullmatch(t["name"])))))


def _verbatim_ok(v: Any, now: float) -> bool:
    return (isinstance(v, dict) and isinstance(v.get("key"), str) and bool(VERBATIM_KEY_RE.fullmatch(v["key"]))
            and ie.valid_investigation(v.get("investigation_id")) and ie.valid_session(v.get("to_session_id"))
            and isinstance(v.get("sha256"), str) and _num(v.get("at")) and 0 <= now - v["at"] < RECENT_TTL)


def _recent_ok(r: Any, now: float) -> bool:
    return (isinstance(r, dict) and isinstance(r.get("id"), str)
            and bool(ID_RE.fullmatch(r["id"]) or VERBATIM_KEY_RE.fullmatch(r["id"]))
            and isinstance(r.get("message"), str) and _num(r.get("at")) and 0 <= now - r["at"] < RECENT_TTL)


def _read(home: Path, sid: str, now: float) -> dict:
    """The store, without expired or malformed entries (an unreadable file
    is empty)."""
    try:
        data = json.loads(proposals_path(home, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    def kept(name: str, ok: Callable[[Any], bool], cap: int) -> list:
        got = data.get(name)
        return [x for x in got if ok(x)][-cap:] if isinstance(got, list) else []

    prompt = data.get("prompt")
    prompt = prompt if isinstance(prompt, dict) and _num(prompt.get("at")) and _num(prompt.get("prev")) \
        and isinstance(prompt.get("sha"), str) else {}
    # Drafts that expired: their ids, kept for RECENT_TTL so "approve <id>"
    # can say it expired.
    gone = [{"id": e["id"], "at": e["expires_at"]} for e in data.get("proposals") or []
            if isinstance(e, dict) and isinstance(e.get("id"), str) and ID_RE.fullmatch(e["id"])
            and _num(e.get("expires_at")) and now >= e["expires_at"]] \
        if isinstance(data.get("proposals"), list) else []
    expired = kept("expired", lambda x: isinstance(x, dict) and isinstance(x.get("id"), str)
                   and bool(ID_RE.fullmatch(x["id"])) and _num(x.get("at")), MAX_RECENT)
    expired = [x for x in expired + gone if 0 <= now - x["at"] < RECENT_TTL][-MAX_RECENT:]
    return {"proposals": kept("proposals", lambda e: _entry_ok(e, now), MAX_PROPOSALS),
            "expired": expired,
            "targets": kept("targets", _target_ok, MAX_TARGETS),
            "verbatim": kept("verbatim", lambda v: _verbatim_ok(v, now), MAX_VERBATIM),
            "recent": kept("recent", lambda r: _recent_ok(r, now), MAX_RECENT),
            "prompt": prompt}


def _write(home: Path, sid: str, store: dict) -> None:
    path = proposals_path(home, sid)
    if not any(store.values()):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    ie._ensure_dirs(home)
    ie.write_json(path, store)


@contextlib.contextmanager
def _updating(home: Path, sid: str, now: float, wait: float = 1.0):
    """The store under the session's flock, written back on a clean exit.
    Raises OSError when the lock cannot be had."""
    with ie.locked(home, sid, wait=wait) as got:
        if not got:
            raise OSError("the session's proposals are locked by another process")
        store = _read(home, sid, now)
        yield store
        _write(home, sid, store)


def has_store(home: Path, sid: str) -> bool:
    """Whether session `sid` keeps owner-relay state (it supervises: a draft
    or a target was made here). Never raises."""
    try:
        return proposals_path(home, sid).exists()
    except (ValueError, OSError):
        return False


def _expired(store: dict, pid: str) -> bool:
    """Whether draft `pid` expired lately."""
    return any(x["id"] == pid for x in store["expired"])


def pending(home: Path, sid: str, now: Optional[float] = None) -> list:
    """The unexpired proposals of session `sid`, oldest first."""
    return _read(home, sid, time.time() if now is None else now)["proposals"]


def recent(home: Path, sid: str, now: Optional[float] = None) -> list:
    """The latest outcomes of session `sid`, oldest first."""
    return _read(home, sid, time.time() if now is None else now)["recent"]


def targets(home: Path, sid: str, now: Optional[float] = None) -> list:
    """The worker targets session `sid` registered, oldest first."""
    return _read(home, sid, time.time() if now is None else now)["targets"]


def validate(investigation_id: Any, to_session_id: Any, text: Any, answers: Any = None,
             refs: Any = None) -> None:
    """Raises ValueError (in words) when the proposal cannot be relayed as
    given."""
    from .owner_input import wire_safe
    validate_target(investigation_id, to_session_id)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("--text is the decision, 1 to 4000 characters")
    if len(text) > MAX_TEXT:
        raise ValueError(f"--text is at most {MAX_TEXT} characters")
    if wire_safe(text) != text:
        raise ValueError("--text contains a NUL or an unpaired surrogate")
    if has_credential(text):
        raise ValueError("not proposed: the decision looks like it contains a credential. Remove it; the worker gets "
                         "secrets through its environment, never through an owner decision")
    if answers is not None and (not isinstance(answers, int) or isinstance(answers, bool) or answers < 1):
        raise ValueError("--answers is an event number (a positive integer)")
    refs = refs or []
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        raise ValueError(f"at most {MAX_REFS} --ref")
    for r in refs:
        if not isinstance(r, str) or not r.strip() or len(r) > MAX_REF or wire_safe(r) != r:
            raise ValueError(f"each --ref is 1 to {MAX_REF} characters")


def has_credential(text: str) -> bool:
    """Whether the credential scrubber would change `text` (owner input's
    scrubbed_of): a decision never carries a secret."""
    from .owner_input import scrubbed_of
    return scrubbed_of(text)[0] != text


def validate_target(investigation_id: Any, to_session_id: Any, name: Any = None) -> None:
    if not ie.valid_investigation(investigation_id):
        raise ValueError(f"not an investigation id: {investigation_id!r} (inv_ followed by 24 hex characters)")
    if not ie.valid_session(to_session_id):
        raise ValueError("--to-session is the worker's session id (letters, digits, _ and -; at most 128)")
    if name is not None and not (isinstance(name, str) and NAME_RE.fullmatch(name)):
        raise ValueError("--name is 1 to 32 characters: a-z, 0-9 and -")


def _upsert_target(store: dict, investigation_id: str, to_session_id: str, name: Optional[str],
                   now: float) -> dict:
    """The target for (investigation, session), registered or renamed
    (no name given: an existing name is kept). A name names one target
    only: another target holding it is dropped. Keeps the newest
    MAX_TARGETS."""
    old = next((t for t in store["targets"] if t["investigation_id"] == investigation_id
                and t["to_session_id"] == to_session_id), None)
    entry = {"investigation_id": investigation_id, "to_session_id": to_session_id,
             "created_at": old["created_at"] if old else now}
    keep_name = name if name is not None else (old or {}).get("name")
    if keep_name is not None:
        entry["name"] = keep_name
    store["targets"] = [t for t in store["targets"] if t is not old
                        and not (keep_name is not None and t.get("name") == keep_name)] + [entry]
    store["targets"] = store["targets"][-MAX_TARGETS:]
    return entry


def add_target(home: Path, sid: str, investigation_id: str, to_session_id: str, name: Optional[str] = None,
               now: Optional[float] = None) -> dict:
    """Register (or rename) the worker target (investigation, session) of
    supervisor session `sid`. A name already on another target moves here
    (that target is dropped). Sends nothing. Raises ValueError / OSError."""
    validate_target(investigation_id, to_session_id, name)
    now = time.time() if now is None else now
    with _updating(home, sid, now, wait=2.0) as store:
        return _upsert_target(store, investigation_id, to_session_id, name, now)


def remove_target(home: Path, sid: str, *, name: Optional[str] = None, investigation_id: Optional[str] = None,
                  to_session_id: Optional[str] = None, now: Optional[float] = None) -> list:
    """Drop the targets named `name`, or matching (investigation_id,
    to_session_id); the dropped ones. Raises OSError when locked."""
    now = time.time() if now is None else now

    def hit(t: dict) -> bool:
        if name is not None:
            return t.get("name") == name
        return t["investigation_id"] == investigation_id and (to_session_id is None
                                                              or t["to_session_id"] == to_session_id)

    with _updating(home, sid, now) as store:
        gone = [t for t in store["targets"] if hit(t)]
        store["targets"] = [t for t in store["targets"] if not hit(t)]
        return gone


def propose(home: Path, sid: str, investigation_id: str, to_session_id: str, text: str, *,
            answers: Optional[int] = None, refs: Optional[list] = None, now: Optional[float] = None) -> dict:
    """Store a proposal of supervisor session `sid` (and register its
    target) and return it. Sends nothing. Raises ValueError (invalid input)
    or OSError (the store is locked or cannot be written)."""
    validate(investigation_id, to_session_id, text, answers, refs)
    now = time.time() if now is None else now
    with _updating(home, sid, now, wait=2.0) as store:
        taken = {e["id"] for e in store["proposals"]} | {r["id"] for r in store["recent"]}
        pid = "dp_" + secrets.token_hex(8)
        while pid in taken:
            pid = "dp_" + secrets.token_hex(8)
        entry: dict = {"id": pid, "investigation_id": investigation_id, "to_session_id": to_session_id,
                       "text": text, "sha256": sha256_text(text), "refs": list(refs or []),
                       "created_at": now, "expires_at": now + PROPOSAL_TTL}
        if answers is not None:
            entry["answers"] = answers
        store["proposals"] = (store["proposals"] + [entry])[-MAX_PROPOSALS:]
        _upsert_target(store, investigation_id, to_session_id, None, now)
        return entry


def withdraw(home: Path, sid: str, pid: str, now: Optional[float] = None) -> Optional[dict]:
    """Remove pending proposal `pid` (nothing is sent); the entry, or None
    when it is not pending. Raises OSError when the store is locked."""
    return _take(home, sid, pid, time.time() if now is None else now)


def _take(home: Path, sid: str, pid: str, now: float, outcome: Optional[dict] = None) -> Optional[dict]:
    with _updating(home, sid, now) as store:
        entry = next((e for e in store["proposals"] if e["id"] == pid), None)
        store["proposals"] = [e for e in store["proposals"] if e["id"] != pid]
        if outcome is not None:
            store["recent"] = (store["recent"] + [outcome])[-MAX_RECENT:]
        return entry


def _note(home: Path, sid: str, outcome: dict, now: float, verbatim: Optional[dict] = None,
          forget_key: Optional[str] = None) -> None:
    """Remember an outcome, and the relay key of an unsent `decide`
    (verbatim) or forget one (forget_key). Never raises."""
    with contextlib.suppress(Exception):
        with _updating(home, sid, now) as store:
            store["recent"] = (store["recent"] + [outcome])[-MAX_RECENT:]
            if verbatim is not None or forget_key is not None:
                key = verbatim["key"] if verbatim is not None else forget_key
                store["verbatim"] = [v for v in store["verbatim"] if v["key"] != key]
                if verbatim is not None:
                    store["verbatim"] = (store["verbatim"] + [verbatim])[-MAX_VERBATIM:]


# ---------------------------------------------------------------------------
# The route (called only by handle_prompt, i.e. the prompt hook)
# ---------------------------------------------------------------------------

def relay_body(entry: dict, *, relay_key: str, source: str, supervisor_session_id: str,
               supervisor_investigation_id: str, prompt_sha256: str, approved_at: str, client: str) -> dict:
    payload: dict = {"text": entry["text"]}
    if entry.get("answers") is not None:
        payload["answers"] = entry["answers"]
    if entry.get("refs"):
        payload["refs"] = list(entry["refs"])
    return {"investigation_id": entry["investigation_id"], "to_session_id": entry["to_session_id"],
            "relay_key": relay_key, "payload": payload,
            "relay": {"supervisor_session_id": supervisor_session_id,
                      "supervisor_investigation_id": supervisor_investigation_id,
                      "proposal_sha256": sha256_text(entry["text"]), "prompt_sha256": prompt_sha256,
                      "approved_at": approved_at, "source": source},
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


def refusal(err: "sync.ServerError") -> tuple:
    """(the refusal in words for the owner, the code for the debug log):
    inert, one line, never the server's own text."""
    code = err.body.get("error") if isinstance(err.body, dict) else None
    safe = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code) else f"http_{int(err.status)}"
    if unsupported(err):
        return "this Cardinal cannot send owner decisions from here yet (it needs a newer Maestro)", safe
    said = REFUSALS.get(code) if isinstance(code, str) else None
    return said or "the reason is one this plugin version does not know", safe


def settings_link(url: Any, origin: Any = None) -> Optional[str]:
    """A settings URL safe to show: a plain http(s) URL, or a site path made
    absolute with this connection's origin; None otherwise."""
    from .investigation_bootstrap import SETTINGS_URL_RE
    if not isinstance(url, str) or not SETTINGS_URL_RE.fullmatch(url):
        return None
    if url.startswith("/"):
        return origin.rstrip("/") + url if isinstance(origin, str) and ORIGIN_RE.fullmatch(origin.rstrip("/")) \
            else None
    return url


def _off_line(link: Optional[str]) -> str:
    where = f" at {link}" if link else " in your Cardinal settings on the web (Settings, Investigations)"
    return f"decisions from your supervisor sessions are off for your account. Turn on \"{TOGGLE}\" once{where}"


def _send(conn: dict, body: dict, client: str, deadline: float, opener) -> tuple:
    """("ok", answer) | ("transient", None) | ("not_enabled", the line for
    the owner) | ("refused", (reason in words, code))."""
    timeout = max(0.1, min(BUDGET, deadline - time.monotonic()))
    state, value = _bounded(lambda: relay(conn, body, client=client, opener=opener, timeout=timeout), deadline)
    if state == "done":
        return "ok", value
    err = value if state == "raised" else None
    if err is None or transient(err) or not isinstance(err, sync.ServerError):
        return "transient", None
    code = err.body.get("error") if isinstance(err.body, dict) else None
    if err.status == 403 and code == NOT_ENABLED:
        body_url = err.body.get("settings_url") if isinstance(err.body, dict) else None
        return "not_enabled", _off_line(settings_link(body_url, conn.get("origin")))
    return "refused", refusal(err)


_WHY = {
    "unbound": "this session has no Cardinal Investigation yet",
    "not_author": "this session joined someone else's Investigation, so it cannot send decisions",
    "own_investigation": "it is addressed to this session's own Investigation",
    "no_connection": "this machine is not connected to Cardinal",
}


def _precheck(home: Path, sid: str, worker_inv: str, conn: Any) -> tuple:
    """(status, None) when this session cannot send to `worker_inv` now
    (_WHY says why), else (None, its binding)."""
    b = ie.read_binding(home, sid)
    if not b:
        return "unbound", None
    if b.get("is_author") is not True:
        return "not_author", None
    if b.get("investigation_id") == worker_inv:
        return "own_investigation", None
    if not (isinstance(conn, dict) and conn.get("origin") and conn.get("org") and conn.get("key")
            and not conn.get("token")):
        return "no_connection", None
    return None, b


# ---------------------------------------------------------------------------
# The prompt hook
# ---------------------------------------------------------------------------

@dataclass
class RelayResult:
    """What handle_prompt did. status: a debug code (code: the server's
    refusal code, for the debug log only). message: the outcome to show the
    user (systemMessage, or the runtime's equivalent) AND to give the model
    (additional context), or None (the prompt is not an approval or a
    decision: nothing to say)."""
    status: str
    message: Optional[str] = None
    ids: list = field(default_factory=list)
    code: Optional[str] = None


def maybe(prompt: Any) -> bool:
    """A cheap pre-check before anything is read: could `prompt` be one?"""
    return isinstance(prompt, str) and bool(MAYBE_RE.match(prompt))


def wanted(home: Path, sid: str, prompt: Any) -> bool:
    """Whether an adapter should call handle_prompt for this prompt: it may
    be an approval or a decision, or this session keeps relay state (every
    owner prompt is then noted, for the stale-approve rule)."""
    return maybe(prompt) or has_store(home, sid)


def match(prompt: Any) -> Optional[tuple]:
    """(verb "approve"|"reject", id or None) when the whole stripped prompt
    is an approval, else None."""
    if not isinstance(prompt, str) or len(prompt) > 200:
        return None
    m = APPROVAL_RE.fullmatch(prompt.strip())
    if not m:
        return None
    return m.group(1).lower(), (m.group(2).lower() if m.group(2) else None)


def match_decide(prompt: Any) -> Optional[tuple]:
    """(name or None, text) when the whole stripped prompt is `decide:
    <text>` / `decide <name>: <text>`; text is what follows the colon,
    without surrounding whitespace (possibly empty), else verbatim."""
    if not isinstance(prompt, str):
        return None
    m = DECIDE_RE.fullmatch(prompt.strip())
    if not m:
        return None
    return (m.group(1).lower() if m.group(1) else None), m.group(2).strip()


def _short(sid: str) -> str:
    return sid[:8]


def _hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def excerpt(text: str, cap: int = EXCERPT) -> str:
    """`text` on one line (whitespace runs become one space) as an escaped
    JSON string of at most `cap` characters, … when cut."""
    lit, cut = ie.clipped_json_text(" ".join(text.split()), cap)
    return lit[:-1] + '…"' if cut else lit


def _who(store: dict, inv: str, to_sid: str) -> str:
    """How the owner knows the worker: its target name, and its session."""
    t = next((t for t in store["targets"] if t["investigation_id"] == inv and t["to_session_id"] == to_sid), None)
    name = t.get("name") if t else None
    return f"{name} (session {_short(to_sid)})" if name else f"the worker (session {_short(to_sid)})"


def _target_label(t: dict) -> str:
    return f"{t['name']} (session {_short(t['to_session_id'])})" if t.get("name") \
        else f"session {_short(t['to_session_id'])} (unnamed: ask the supervisor to name it)"


def _targets_line(ts: list) -> str:
    return "; ".join(_target_label(t) for t in ts)


def _drafts_line(entries: list) -> str:
    return "; ".join(f"{e['id']} {excerpt(e['text'], 40)} (expires {_hhmm(e['expires_at'])} local)" for e in entries)


def _seen_prompt(home: Path, sid: str, sha: str, runtime: str, now: float) -> float:
    """Note an owner prompt (the store's `prompt`: {at, sha, prev}) and
    return the time of the owner's PREVIOUS prompt (0: unknown). A re-fired
    prompt hook (DEDUPE_REPEATS) for the same prompt keeps the previous one.
    Only for a session that keeps relay state. Never raises."""
    from .owner_input import DEDUPE_REPEATS
    try:
        if not has_store(home, sid):
            return 0.0
        with _updating(home, sid, now) as store:
            last = store["prompt"]
            if last and runtime in DEDUPE_REPEATS and last["sha"] == sha and 0 <= now - last["at"] < REFIRE_WINDOW:
                return last["prev"]
            prev = last["at"] if last else 0.0
            store["prompt"] = {"at": now, "sha": sha, "prev": prev}
            return prev
    except Exception:
        return 0.0


def handle_prompt(home: Path, sid: str, prompt: Any, *, runtime: str, conn: Optional[dict], client: str,
                  agent_id: Optional[str] = None, deadline: float, now: Optional[float] = None,
                  opener=None) -> RelayResult:
    """The relay half of every runtime's prompt hook (see the module doc).
    Every owner prompt of a session with relay state is noted (the
    stale-approve rule: a bare `approve` / `reject` applies only to a draft
    made after the owner's previous prompt). Statuses:

      invalid / subagent / not_typed / no_match / none
                      nothing said: not an approval or a decision here (a
                      bare approve in a session that never had a draft or a
                      target; `decide <word>:` when no target has a name)
      repeat          DEDUPE_REPEATS runtimes (Gemini) only: the same prompt
                      again within REFIRE_WINDOW after a final outcome, with
                      no newer draft: the outcome is repeated, nothing is
                      sent again
      no_draft        approve / reject matched no waiting draft
      ambiguous       approve: several fresh drafts; decide: several targets
                      and no name. Says which
      unknown_id      approve: the id is not waiting (expired, rejected, sent)
      no_target / unknown_target
                      decide: no target registered, or none by that name
      bad_text / question / credential
                      decide: empty, too long, NUL; ends with "?"; the
                      scrubber would change it
      rejected        removed; nothing sent
      relayed / duplicate
                      sent (201 / 200); a draft is removed
      transient       network, timeout, 5xx, 408, 429: a draft is kept; a
                      decide's relay key is kept for its retry
      not_enabled     403 owner_relay_not_enabled: as transient, with the settings link
      refused         any other refusal: a draft is removed
      unbound / not_author / own_investigation / no_connection / busy
                      not sent; a draft is kept
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
    typed_sha = hashlib.sha256(owner_input.scrubbed_of(typed)[0].encode("utf-8", "replace")).hexdigest()
    previous = _seen_prompt(home, sid, typed_sha, runtime, now)
    approval, decision = match(typed), match_decide(typed)
    if approval is None and decision is None:
        return RelayResult("no_match")
    store = _read(home, sid, now)
    if decision is not None and decision[0] is not None and not any(t.get("name") for t in store["targets"]):
        return RelayResult("no_match")   # `decide later: …` with no named worker: an ordinary prompt
    if runtime in owner_input.DEDUPE_REPEATS:
        # A runtime that re-fires its prompt hook for one prompt: the same
        # prompt seen again moments later repeats its final outcome.
        pid = approval[1] if approval else None
        again = next((r for r in reversed(store["recent"]) if r.get("prompt_sha256") == typed_sha
                      and 0 <= now - r["at"] < REFIRE_WINDOW and r.get("outcome") in FINAL_OUTCOMES
                      and (pid is None or r["id"] == pid)), None)
        if again is not None and not any(e["id"] == again["id"] or e["created_at"] > again["at"]
                                         for e in store["proposals"]):
            return RelayResult("repeat", again["message"], [again["id"]])
    if decision is not None:
        return _decide(home, sid, decision, store, typed_sha, conn, client, deadline, now, opener)
    return _approve(home, sid, approval, store, previous, typed_sha, conn, client, deadline, now, opener)


def _decide(home, sid, decision, store, typed_sha, conn, client, deadline, now, opener) -> RelayResult:
    """`decide: <text>` / `decide <name>: <text>`: the owner's own words,
    sent verbatim to the registered target."""
    from .owner_input import wire_safe
    name, text = decision
    ts = store["targets"]
    if name is not None:
        target = next((t for t in ts if t.get("name") == name), None)
        if target is None:
            return RelayResult("unknown_target", f"Cardinal: no worker named {name} is registered here "
                                                 f"({_targets_line(ts)}); nothing was sent.")
    elif not ts:
        return RelayResult("no_target", NO_WORKER)
    elif len(ts) > 1:
        return RelayResult("ambiguous", f"Cardinal: {len(ts)} workers are registered here; say which with "
                                        f"`decide <name>: …`: {_targets_line(ts)}. Nothing was sent.")
    else:
        target = ts[0]
    if not text:
        return RelayResult("bad_text", "Cardinal: not sent: `decide:` needs your decision after the colon.")
    if len(text) > MAX_TEXT:
        return RelayResult("bad_text", f"Cardinal: not sent: a decision is at most {MAX_TEXT} characters (this one "
                                       f"has {len(text)}). Shorten it and type it again.")
    if wire_safe(text) != text:
        return RelayResult("bad_text", "Cardinal: not sent: the decision contains a character Cardinal cannot "
                                       "store. Type it again without it.")
    if text.endswith("?"):
        return RelayResult("question", QUESTION)
    if has_credential(text):
        return RelayResult("credential", CREDENTIAL)
    who = _who(store, target["investigation_id"], target["to_session_id"])
    retry = "Type the same decide: again to retry; it will not be sent twice."
    status, b = _precheck(home, sid, target["investigation_id"], conn)
    if status:
        return RelayResult(status, f"Cardinal: not sent: {_WHY[status]}.")
    sha = sha256_text(text)
    prior = next((v for v in store["verbatim"] if v["investigation_id"] == target["investigation_id"]
                  and v["to_session_id"] == target["to_session_id"] and v["sha256"] == sha), None)
    key = prior["key"] if prior else "dv_" + secrets.token_hex(8)
    entry = {"investigation_id": target["investigation_id"], "to_session_id": target["to_session_id"], "text": text}
    body = relay_body(entry, relay_key=key, source=SOURCE_VERBATIM, supervisor_session_id=sid,
                      supervisor_investigation_id=b["investigation_id"], prompt_sha256=typed_sha,
                      approved_at=ie._now_iso(now), client=client)
    got, detail = _send(conn, body, client, deadline, opener)

    def outcome(s: str, message: str, seq: Optional[int] = None) -> dict:
        out = {"id": key, "outcome": s, "message": message, "prompt_sha256": typed_sha, "at": now}
        if seq is not None:
            out["seq"] = seq
        return out

    unsent = {"key": key, "investigation_id": target["investigation_id"],
              "to_session_id": target["to_session_id"], "sha256": sha, "at": now}
    if got == "ok":
        ev, dup = detail["event"], detail.get("duplicate") is True
        message = (f"Cardinal: {'already sent' if dup else 'sent'} your decision to {who} as owner decision "
                   f"#{ev['seq']}: {excerpt(text)}. {SEES_IT}")
        status = "duplicate" if dup else "relayed"
        _note(home, sid, outcome(status, message, ev["seq"]), now, forget_key=key)
        return RelayResult(status, message, [key])
    if got == "transient":
        message = f"Cardinal: not sent: Cardinal did not answer in time or could not be reached. {retry}"
        _note(home, sid, outcome("transient", message), now, verbatim=unsent)
        return RelayResult("transient", message, [key])
    if got == "not_enabled":
        message = f"Cardinal: not sent: {detail}; then {retry[0].lower()}{retry[1:]}"
        _note(home, sid, outcome("not_enabled", message), now, verbatim=unsent)
        return RelayResult("not_enabled", message, [key])
    reason, code = detail
    if code == "idempotency_conflict":
        reason = "a decision with different text was already sent under the same key"
    message = (f"Cardinal refused your decision for {who}: {reason}. Nothing was sent; type it in the worker's "
               "terminal instead.")
    _note(home, sid, outcome("refused", message), now, forget_key=key)
    return RelayResult("refused", message, [key], code)


def _unknown_id(home, sid, pid, store, now) -> str:
    """Why draft `pid` is not waiting, as far as this session knows."""
    r = next((r for r in reversed(store["recent"]) if r["id"] == pid), None)
    if r is not None and r.get("outcome") == "rejected":
        return f"you rejected it at {_hhmm(r['at'])}"
    if r is not None and r.get("outcome") in ("relayed", "duplicate") and isinstance(r.get("seq"), int):
        return f"it was already sent as owner decision #{r['seq']}"
    if r is not None and r.get("outcome") == "refused":
        return f"Cardinal refused it at {_hhmm(r['at'])}"
    if _expired(store, pid):
        return "it expired (drafts expire after 30 minutes)"
    return "it is not waiting here (it expired, was withdrawn, or was never made in this session)"


def _approve(home, sid, approval, store, previous, typed_sha, conn, client, deadline, now, opener) -> RelayResult:
    """`approve [dp_…]` / `reject [dp_…]` of a supervisor's draft. A bare one
    applies only to a draft made after the owner's previous prompt."""
    verb, pid = approval
    entries = store["proposals"]
    known = has_store(home, sid)
    if pid is None:
        fresh = [e for e in entries if e["created_at"] > previous]
        if not fresh:
            return RelayResult("no_draft", NO_DRAFT) if known else RelayResult("none")
        if len(fresh) > 1:
            return RelayResult("ambiguous", (
                f"Cardinal: {len(fresh)} drafts are waiting; nothing was {'sent' if verb == 'approve' else 'discarded'}. "
                f"Reply `{verb} <id>` with one of: {_drafts_line(fresh)}."), [e["id"] for e in fresh])
        entry = fresh[0]
    else:
        entry = next((e for e in entries if e["id"] == pid), None)
        if entry is None:
            left = f" Waiting: {_drafts_line(entries)}." if entries else ""
            return RelayResult("unknown_id", f"Cardinal: not sent: draft {pid}: {_unknown_id(home, sid, pid, store, now)}."
                                             f"{left}", [pid])
    pid = entry["id"]
    who = _who(store, entry["investigation_id"], entry["to_session_id"])

    def outcome(status: str, message: str, seq: Optional[int] = None) -> dict:
        out = {"id": pid, "outcome": status, "message": message, "prompt_sha256": typed_sha, "at": now}
        if seq is not None:
            out["seq"] = seq
        return out

    if verb == "reject":
        message = f"Cardinal: discarded your draft {pid} for {who}; nothing was sent."
        try:
            _take(home, sid, pid, now, outcome("rejected", message))
        except OSError:
            return RelayResult("busy", f"Cardinal: draft {pid} could not be discarded just now; type `reject {pid}` "
                                       "again.", [pid])
        return RelayResult("rejected", message, [pid])

    retry = f"type `approve {pid}` to try again"
    status, b = _precheck(home, sid, entry["investigation_id"], conn)
    if status:
        return RelayResult(status, f"Cardinal: not sent: {_WHY[status]}. Your draft {pid} is still waiting.", [pid])
    body = relay_body(entry, relay_key=pid, source=SOURCE_PROPOSAL, supervisor_session_id=sid,
                      supervisor_investigation_id=b["investigation_id"], prompt_sha256=typed_sha,
                      approved_at=ie._now_iso(now), client=client)
    got, detail = _send(conn, body, client, deadline, opener)
    if got == "ok":
        ev, dup = detail["event"], detail.get("duplicate") is True
        message = (f"Cardinal: {'already sent' if dup else 'sent'} your approved draft {pid} to {who} as owner "
                   f"decision #{ev['seq']}: {excerpt(entry['text'])}. {SEES_IT}")
        with contextlib.suppress(OSError):
            _take(home, sid, pid, now, outcome("duplicate" if dup else "relayed", message, ev["seq"]))
        return RelayResult("duplicate" if dup else "relayed", message, [pid])
    if got == "transient":
        message = (f"Cardinal: not sent: Cardinal did not answer in time or could not be reached. Your draft {pid} "
                   f"is still waiting; {retry}.")
        _note(home, sid, outcome("transient", message), now)
        return RelayResult("transient", message, [pid])
    if got == "not_enabled":
        message = f"Cardinal: not sent: {detail}; then {retry}. Your draft {pid} is still waiting."
        _note(home, sid, outcome("not_enabled", message), now)
        return RelayResult("not_enabled", message, [pid])
    reason, code = detail
    message = (f"Cardinal refused your draft {pid} for {who}: {reason}. Nothing was sent; ask the supervisor to "
               "draft it again, or type it in the worker's terminal.")
    with contextlib.suppress(OSError):
        _take(home, sid, pid, now, outcome("refused", message))
    return RelayResult("refused", message, [pid], code)

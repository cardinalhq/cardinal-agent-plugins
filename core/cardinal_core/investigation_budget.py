"""Supervise step backs on a work budget (docs/specs/supervise-stepback.md).

The supervisor records a budget per direction of the worker's work
(milestones with P50 / P80 active hours) in a private budget file, marks
milestones done with a ref, and asks `--check` whether a step back is due:

- spent: the worker's active hours since the budget was set: the union of
  the timestamps of the worker session's events and, when its activity
  spool is on this machine, its captured tool calls; a gap over the idle
  gap between consecutive timestamps is not counted;
- earned: the P50 hours of the milestones marked done (times a fraction);
- lag: spent >= lag_start x P80 total and earned < lag_ratio x spent;
  while one lag episode persists it re-fires only once spent has grown by
  refire_growth since its last firing (a check without lag ends the
  episode); backstop: spent first crosses a multiple of the P80 total.

`--set --amend` corrects the open budget in place (same direction: its
start, done marks and firings stay; no history entry).

The budget file (0600, flock'd) holds the current budget and a bounded
history of closed ones. The worker never sees it. Read-only on the server;
the spool is read only through evidence.list_entries. Stdlib only.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import evidence
from . import investigation_consumer as consumer
from . import investigation_events as ie

VERSION = 1
HISTORY_MAX = 20
MAX_MILESTONES = 20
MAX_TEXT = 1000
MAX_TITLE = 200
MAX_PAGES = 200
MILESTONE_ID_RE = re.compile(r"[a-z0-9-]{1,32}")
REF_RE = re.compile(r"#[1-9][0-9]{0,17}|ev_[0-9a-f]{12}")
OUTCOMES = ("done", "stopped", "replaced")
DEFAULT_THRESHOLDS = {
    "lag_start": 0.15,          # fraction of the P80 total spent before lag can fire
    "lag_ratio": 0.5,           # lag while earned < lag_ratio x spent
    "refire_growth": 1.5,       # a persisting lag re-fires once spent grew by this factor
    "backstops": [1, 2, 4],     # multiples of the P80 total
    "idle_gap_minutes": 30,
}
_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:?\d{2})?$")


class BudgetError(ValueError):
    """A usage or budget-file problem (exit 2)."""


# -- time and spent ---------------------------------------------------------

def parse_time(value) -> Optional[float]:
    """An ISO-8601 UTC timestamp (Z or an offset; seconds and fraction
    optional) as epoch seconds; None when it is not one."""
    m = _TS_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    try:
        t = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    t += float("0." + frac) if frac else 0.0
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        t -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return t


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def active_hours(times: Iterable[float], idle_gap_minutes: float) -> float:
    """Sum of the gaps between consecutive timestamps (sorted, merged) that
    are at most the idle gap; a longer gap is idle and not counted."""
    ts = sorted(set(times))
    gap = idle_gap_minutes * 60
    return sum(b - a for a, b in zip(ts, ts[1:]) if b - a <= gap) / 3600


def worker_event(e: dict, worker_session: Optional[str]) -> bool:
    """An event the worker session wrote: its claims and acknowledgements,
    never owner input, owner decisions or a grantee's (a supervisor's) posts.
    Without a worker session: the investigation author's session events."""
    if not isinstance(e, dict) or ie.event_class(e) not in (ie.SEMANTIC, ie.CONTROL):
        return False
    p = e.get("producer") if isinstance(e.get("producer"), dict) else {}
    if ie.is_grantee(p) or not p.get("session_id"):
        return False
    if worker_session:
        return p.get("session_id") == worker_session
    return p.get("is_investigation_author") is True


def worker_times(events: Iterable[dict], worker_session: Optional[str], since_seq: int) -> list:
    """[(seq, epoch)] of the worker's events at or after since_seq."""
    out = []
    for e in events:
        seq = e.get("seq") if isinstance(e, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < since_seq or not worker_event(e, worker_session):
            continue
        t = parse_time(e.get("created_at"))
        if t is not None:
            out.append((seq, t))
    return out


def spool_times(entries: Iterable[dict], since: float) -> list:
    """[(evidence_id, epoch)] of captured calls at or after `since`."""
    out = []
    for e in entries:
        t = parse_time(e.get("called_at")) if isinstance(e, dict) else None
        if t is not None and t >= since:
            out.append((e.get("evidence_id"), t))
    return out


# -- the budget -------------------------------------------------------------

def _text(name: str, value, cap: int = MAX_TEXT, required: bool = True) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BudgetError(f"{name} is required (1 to {cap} characters)")
        return None
    if not isinstance(value, str) or len(value) > cap:
        raise BudgetError(f"{name} is 1 to {cap} characters")
    if any(ord(c) < 32 and c not in "\t" for c in value):
        raise BudgetError(f"{name} may not contain control characters or newlines")
    return value.strip()


def _hours(raw: str, what: str) -> float:
    try:
        h = float(raw)
    except ValueError:
        raise BudgetError(f"{what} is a number of hours, not {raw!r}") from None
    if not 0 < h <= 10000:
        raise BudgetError(f"{what} is more than 0 hours")
    return round(h, 3)


def parse_milestone(spec: str) -> dict:
    """"<id>:<P50h>:<P80h>:<title>" -> {id, p50, p80, title}."""
    parts = spec.split(":", 3) if isinstance(spec, str) else []
    if len(parts) != 4:
        raise BudgetError(f"--milestone is \"<id>:<P50h>:<P80h>:<title>\", not {spec!r}")
    mid, p50, p80, title = parts
    if not MILESTONE_ID_RE.fullmatch(mid):
        raise BudgetError(f"milestone id {mid!r}: 1-32 of a-z, 0-9 and -")
    lo, hi = _hours(p50, f"{mid} P50"), _hours(p80, f"{mid} P80")
    if hi < lo:
        raise BudgetError(f"{mid}: P80 ({hi} h) is below P50 ({lo} h)")
    return {"id": mid, "p50": lo, "p80": hi, "title": _text(f"{mid} title", title, MAX_TITLE)}


def new_budget(*, question: str, milestones: list, basis: str, set_at: float, since_seq: int,
               worker_session: Optional[str] = None, worker_estimate: Optional[str] = None) -> dict:
    ms = [parse_milestone(m) if isinstance(m, str) else m for m in milestones]
    if not 1 <= len(ms) <= MAX_MILESTONES:
        raise BudgetError(f"a budget has 1 to {MAX_MILESTONES} milestones (--milestone, repeated)")
    ids = [m["id"] for m in ms]
    if len(set(ids)) != len(ids):
        raise BudgetError("milestone ids must be distinct")
    if worker_session is not None and not ie.valid_session(worker_session):
        raise BudgetError(f"not a session id: {worker_session!r}")
    if not isinstance(since_seq, int) or since_seq < 1:
        raise BudgetError("--since-seq is a positive event seq")
    return {
        "question": _text("--question", question),
        "basis": _text("--basis", basis),
        "worker_estimate": _text("--worker-estimate", worker_estimate, required=False),
        "worker_session": worker_session,
        "milestones": ms,
        "thresholds": json.loads(json.dumps(DEFAULT_THRESHOLDS)),
        "set_at": iso(set_at),
        "since_seq": since_seq,
        "done": {},
        "firings": [],
    }


def totals(budget: dict) -> tuple:
    return (round(sum(m["p50"] for m in budget["milestones"]), 3),
            round(sum(m["p80"] for m in budget["milestones"]), 3))


def mark_done(budget: dict, mid: str, ref: str, fraction: float = 1.0, *, now: Optional[float] = None,
              correct: bool = False) -> dict:
    """`fraction` is cumulative: the part done so far, never an increment.
    Lowering a recorded fraction needs `correct`."""
    if mid not in {m["id"] for m in budget["milestones"]}:
        raise BudgetError(f"no milestone {mid!r} in this budget ({', '.join(m['id'] for m in budget['milestones'])})")
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        raise BudgetError("--done needs --ref \"#<seq>\" or ev_<12 hex>: the event or captured call proving it")
    if not 0 < fraction <= 1:
        raise BudgetError("--fraction is more than 0 and at most 1")
    before = budget["done"].get(mid)
    if before and round(fraction, 3) < before["fraction"] and not correct:
        raise BudgetError(f"{mid} is already {before['fraction']:.0%} done ({before['ref']}); --fraction is the total "
                          "done so far, not an increment. To lower it, add --correct")
    budget["done"][mid] = {"fraction": round(fraction, 3), "ref": ref, "at": iso(now if now is not None else time.time())}
    return budget


def earned_hours(budget: dict) -> float:
    by_id = {m["id"]: m for m in budget["milestones"]}
    return round(sum(by_id[k]["p50"] * d["fraction"] for k, d in budget["done"].items() if k in by_id), 4)


def evaluate(budget: dict, spent: float, earned: float) -> dict:
    """The trigger decision (no side effects): {fire: [reasons], lag, crossed:
    [backstop multiples newly crossed], next_lag, next_backstop}."""
    th = budget["thresholds"]
    _, p80 = totals(budget)
    fired_backstops = {m for f in budget["firings"] for m in f.get("backstops", [])}
    crossed = [m for m in th["backstops"] if spent >= m * p80 and m not in fired_backstops]
    lagging = spent >= th["lag_start"] * p80 and earned < th["lag_ratio"] * spent
    lag_spents = [f["spent_h"] for f in budget["firings"] if "lag" in f.get("reasons", [])]
    # Within one lag episode a re-fire needs refire_growth more spent; a check
    # without lag ends the episode, so the next lag fires at once.
    refire_at = th["refire_growth"] * lag_spents[-1] if lag_spents and lag_open(budget) else 0.0
    lag_fires = lagging and spent >= refire_at
    reasons = []
    if crossed:
        reasons.append(f"backstop {max(crossed)}x")
    if lag_fires:
        reasons.append("lag")
    unfired = [m for m in th["backstops"] if m not in fired_backstops and m not in crossed]
    return {
        "fire": reasons,
        "lagging": lagging,
        "crossed": crossed,
        # Assuming nothing more is marked done: lag needs spent past earned / ratio.
        "next_lag_h": round(max(th["lag_start"] * p80, earned / th["lag_ratio"], refire_at), 3),
        "next_backstop": (min(unfired), round(min(unfired) * p80, 3)) if unfired else None,
    }


def lag_open(budget: dict) -> bool:
    """A lag episode is open from a lag firing until a check finds no lag
    (files from before the key: open when lag ever fired)."""
    return budget.get("lag_open", any("lag" in f.get("reasons", []) for f in budget["firings"]))


def record_firing(budget: dict, decision: dict, spent: float, earned: float, *, now: float,
                  head_seq: Optional[int]) -> None:
    if "lag" in decision["fire"]:
        budget["lag_open"] = True
    budget["firings"].append({
        "reasons": ["lag" if r == "lag" else "backstop" for r in decision["fire"]],
        "backstops": decision["crossed"],
        "spent_h": round(spent, 4),
        "earned_h": round(earned, 4),
        "at": iso(now),
        "head_seq": head_seq,
    })


def milestone_actuals(budget: dict, wtimes: list, stimes: list) -> list:
    """Per milestone: its estimate, done mark, and the actual hours: the
    worker's cumulative spent when its ref happened, minus the cumulative
    spent at the previous done mark (in ref order)."""
    gap = budget["thresholds"]["idle_gap_minutes"]
    all_times = [t for _, t in wtimes] + [t for _, t in stimes]
    seq_time = dict(wtimes)
    ev_time = dict(stimes)

    def when(ref: str) -> Optional[float]:
        if ref.startswith("#"):
            seq = int(ref[1:])
            earlier = [t for s, t in wtimes if s <= seq]
            return seq_time.get(seq) or (max(earlier) if earlier else None)
        return ev_time.get(ref)

    marks = sorted(((when(d["ref"]), k) for k, d in budget["done"].items()), key=lambda x: (x[0] is None, x[0] or 0))
    actual, prev = {}, 0.0
    for t, k in marks:
        if t is None:
            continue
        cum = active_hours([x for x in all_times if x <= t], gap)
        actual[k] = round(cum - prev, 3)
        prev = cum
    return [{**m, "done": budget["done"].get(m["id"]), "actual_h": actual.get(m["id"])} for m in budget["milestones"]]


def close(state: dict, outcome: str, spent: float, earned: float, actuals: list, *, now: float) -> dict:
    if outcome not in OUTCOMES:
        raise BudgetError(f"--outcome is one of {', '.join(OUTCOMES)}")
    b = state.get("current")
    if not b:
        raise BudgetError("no open budget to close")
    p50, p80 = totals(b)
    entry = {k: b.get(k) for k in ("question", "basis", "worker_estimate", "worker_session", "set_at", "since_seq",
                                   "firings", "thresholds")}
    entry.update({"outcome": outcome, "closed_at": iso(now), "p50_total_h": p50, "p80_total_h": p80,
                  "spent_h": round(spent, 3), "earned_h": round(earned, 3), "milestones": actuals})
    state["history"] = (state.get("history") or [])[-(HISTORY_MAX - 1):] + [entry]
    state["current"] = None
    return entry


# -- the budget file ---------------------------------------------------------

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _shape_errors(state: dict) -> list:
    """What is missing or mistyped in a budget file (empty: usable)."""
    errs = []

    def need(cond, what):
        if not cond:
            errs.append(what)

    need(isinstance(state.get("investigation_id"), (str, type(None))), "investigation_id")
    need(isinstance(state.get("history", []), list), "history")
    for i, h in enumerate(state.get("history") or [] if isinstance(state.get("history"), list) else []):
        need(isinstance(h, dict) and all(isinstance(h.get(k), str) for k in ("outcome", "closed_at", "question"))
             and all(_num(h.get(k)) for k in ("p50_total_h", "p80_total_h", "spent_h", "earned_h")), f"history[{i}]")
    b = state.get("current")
    if b is None:
        return errs
    if not isinstance(b, dict):
        return errs + ["current"]
    for k in ("question", "basis", "set_at"):
        need(isinstance(b.get(k), str), f"current.{k}")
    need(parse_time(b.get("set_at")) is not None, "current.set_at (a UTC time)")
    need(isinstance(b.get("since_seq"), int) and not isinstance(b.get("since_seq"), bool) and b["since_seq"] >= 1,
         "current.since_seq")
    for k in ("worker_session", "worker_estimate"):
        need(isinstance(b.get(k), (str, type(None))), f"current.{k}")
    ms = b.get("milestones")
    need(isinstance(ms, list) and ms and all(
        isinstance(m, dict) and isinstance(m.get("id"), str) and isinstance(m.get("title"), str)
        and _num(m.get("p50")) and _num(m.get("p80")) for m in ms), "current.milestones")
    th = b.get("thresholds")
    need(isinstance(th, dict) and all(_num(th.get(k)) for k in ("lag_start", "lag_ratio", "refire_growth",
                                                                   "idle_gap_minutes"))
         and isinstance(th.get("backstops"), list) and all(_num(x) for x in th["backstops"]), "current.thresholds")
    done = b.get("done")
    need(isinstance(done, dict) and all(isinstance(d, dict) and _num(d.get("fraction"))
                                        and isinstance(d.get("ref"), str) and REF_RE.fullmatch(d["ref"])
                                        for d in done.values()), "current.done")
    fs = b.get("firings")
    need(isinstance(fs, list) and all(isinstance(f, dict) and isinstance(f.get("reasons"), list)
                                      and isinstance(f.get("backstops"), list) and _num(f.get("spent_h"))
                                      for f in fs), "current.firings")
    need(isinstance(b.get("lag_open", False), bool), "current.lag_open")
    return errs


def load(path: Path, investigation_id: Optional[str]) -> dict:
    try:
        state = consumer.private_read(path)
    except FileNotFoundError:
        return {"version": VERSION, "investigation_id": investigation_id, "current": None, "history": []}
    except OSError as e:
        raise BudgetError(f"{path}: cannot open the budget file ({e.strerror}); it must be a regular file "
                          "you own, not a link") from None
    if state.get("version") != VERSION:
        raise BudgetError(f"{path} is not a version {VERSION} budget file")
    errs = _shape_errors(state)
    if errs:
        raise BudgetError(f"{path} is not a valid budget file (bad or missing: {', '.join(errs[:5])})")
    if investigation_id and state.get("investigation_id") not in (None, investigation_id):
        raise BudgetError(f"{path} is the budget of {state.get('investigation_id')}, not {investigation_id}")
    return state


def save(path: Path, state: dict) -> None:
    consumer.private_write(path, state)


# -- reading the worker's activity ---------------------------------------------

def read_worker_events(conn: dict, inv: str, since_seq: int, *, client: str, reader: Callable = None) -> tuple:
    """(events at or after since_seq, head_seq), paging the grant's read."""
    reader = reader or ie.read_events
    events, after, head = [], max(0, since_seq - 1), None
    for _ in range(MAX_PAGES):
        page = reader(conn, inv, after=after, limit=ie.PAGE_LIMIT, client=client)
        events.extend(page["events"])
        head = page["head_seq"]
        if page["page_size"] < ie.PAGE_LIMIT or page["last_seq"] >= head or page["last_seq"] <= after:
            break
        after = page["last_seq"]
    return events, head


def measure(budget: dict, events: list, spool_entries: list, *, before_seq: Optional[int] = None,
            before: Optional[float] = None) -> dict:
    """Spent and earned; with before_seq / before, only the worker's events
    before that seq and captured calls before that time (a budget replaced
    by one that starts there)."""
    since = parse_time(budget["set_at"]) or 0.0
    wt = worker_times(events, budget.get("worker_session"), budget["since_seq"])
    st = spool_times(spool_entries, since)
    if before_seq is not None:
        wt = [x for x in wt if x[0] < before_seq]
    if before is not None:
        st = [x for x in st if x[1] < before]
    spent = active_hours([t for _, t in wt] + [t for _, t in st], budget["thresholds"]["idle_gap_minutes"])
    return {"spent": spent, "earned": earned_hours(budget), "worker_times": wt, "spool_times": st}


def spool_entries(home: Path, worker_session: Optional[str]) -> Optional[list]:
    """The worker session's captured calls on this machine, through the
    evidence module's listings only (None: no worker session, or its spool
    is not on this machine)."""
    if not worker_session:
        return None
    root = evidence.default_root(home)
    entries = evidence.list_entries(root, worker_session)
    if entries or any(name == worker_session for name, _ in evidence.session_dirs(root)):
        return entries
    return None


# -- rendering -----------------------------------------------------------------

def _h(x: float) -> str:
    return f"{x:.2f} h"


def check_report(inv: str, budget: dict, m: dict, decision: dict, spool: Optional[list]) -> dict:
    p50, p80 = totals(budget)
    spent, earned = m["spent"], m["earned"]
    lag_spents = [f["spent_h"] for f in budget["firings"] if "lag" in f["reasons"]]
    if decision["fire"]:
        why = []
        if decision["crossed"]:
            why.append(f"spent crossed {max(decision['crossed'])}x the P80 total ({_h(max(decision['crossed']) * p80)})")
        if "lag" in decision["fire"]:
            why.append(f"spent >= {budget['thresholds']['lag_start']:.0%} of P80 and earned {_h(earned)} < "
                       f"{budget['thresholds']['lag_ratio']} x spent")
        verdict = f"FIRE {' + '.join(decision['fire'])}: " + "; ".join(why)
    elif decision["lagging"] and lag_spents and lag_open(budget):
        verdict = f"ok (lag persists; fired at {_h(lag_spents[-1])}, re-fires at {_h(decision['next_lag_h'])})"
    else:
        verdict = "ok"
    return {
        "investigation_id": inv,
        "question": budget["question"],
        "p50_total_h": p50, "p80_total_h": p80,
        "since_seq": budget["since_seq"], "set_at": budget["set_at"],
        "worker_session": budget.get("worker_session"),
        "worker_events": len(m["worker_times"]),
        "captured_calls": None if spool is None else len(m["spool_times"]),
        "spent_h": round(spent, 3), "earned_h": round(earned, 3),
        "earned_over_spent": round(earned / spent, 3) if spent else None,
        "milestones": len(budget["milestones"]),
        "done": sorted(budget["done"]),
        "open": [x["id"] for x in budget["milestones"] if x["id"] not in budget["done"]
                 or budget["done"][x["id"]]["fraction"] < 1],
        "next_lag_h": decision["next_lag_h"],
        "next_backstop": ({"multiple": decision["next_backstop"][0], "at_h": decision["next_backstop"][1]}
                          if decision["next_backstop"] else None),
        "fire": decision["fire"],
        "verdict": verdict,
    }


def check_lines(r: dict) -> list:
    done, total = len(r["done"]), r["milestones"]
    act = ("no worker session: events only" if not r["worker_session"]
           else "activity spool not on this machine" if r["captured_calls"] is None
           else f"{r['captured_calls']} captured calls")
    ratio = "n/a" if r["earned_over_spent"] is None else f"{r['earned_over_spent']:.2f}"
    nb = r["next_backstop"]
    return [
        f"budget {r['investigation_id']}: {ie.json_text(r['question'])}; P50 {_h(r['p50_total_h'])}, "
        f"P80 {_h(r['p80_total_h'])}; {done} of {total} milestones done",
        f"spent {_h(r['spent_h'])} active since #{r['since_seq']} ({r['worker_events']} worker events; {act})",
        f"earned {_h(r['earned_h'])}" + (f" ({', '.join(r['done'])})" if r["done"] else "")
        + f"; earned/spent {ratio}",
        f"next: lag when spent > {_h(r['next_lag_h'])} with nothing more done"
        + (f"; backstop {nb['multiple']}x at {_h(nb['at_h'])}" if nb else "; all backstops fired"),
        r["verdict"],
    ]


def show_lines(state: dict) -> list:
    out = []
    b = state.get("current")
    if b:
        p50, p80 = totals(b)
        out.append(f"budget {state.get('investigation_id')}: {ie.json_text(b['question'])}; P50 {_h(p50)}, "
                   f"P80 {_h(p80)}; set {b['set_at']} from #{b['since_seq']}"
                   + (f"; worker session {b['worker_session']}" if b.get("worker_session") else ""))
        out.append(f"  basis: {ie.json_text(b['basis'])}")
        if b.get("worker_estimate"):
            out.append(f"  worker's own estimate: {ie.json_text(b['worker_estimate'])}")
        for x in b["milestones"]:
            d = b["done"].get(x["id"])
            mark = (f"done {d['fraction']:.0%} ({d['ref']})" if d else "open")
            out.append(f"  {x['id']}: P50 {x['p50']} h / P80 {x['p80']} h, {mark} — {ie.json_text(x['title'])}")
        for f in b["firings"]:
            out.append(f"  fired {'+'.join(f['reasons'])} at spent {_h(f['spent_h'])} ({f['at']})")
    else:
        out.append(f"no open budget for {state.get('investigation_id') or 'this budget file'}")
    for h in state.get("history") or []:
        out.append(f"closed {h['outcome']} {h['closed_at']}: {ie.json_text(h['question'])}; P50 {_h(h['p50_total_h'])}"
                   f" / P80 {_h(h['p80_total_h'])}; spent {_h(h['spent_h'])}, earned {_h(h['earned_h'])}")
    return out


# -- the CLI action (investigation budget) ------------------------------------------

@contextlib.contextmanager
def _locked(path: Path):
    lock = consumer.locked(path)
    try:
        lock.__enter__()
    except BlockingIOError:
        raise BudgetError("the budget file is in use by another command; retry") from None
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _measure_current(state: dict, conn: dict, inv: str, client: str, home: Path, reader) -> tuple:
    b = state["current"]
    events, head = read_worker_events(conn, inv, b["since_seq"], client=client, reader=reader)
    spool = spool_entries(home, b.get("worker_session"))
    return measure(b, events, spool or []), spool, head


def run(args, *, target: Callable, home: Path, client: str, out: Callable = print, now: Callable = time.time,
        reader: Callable = None) -> int:
    """`investigation budget`: one of --set [--amend] / --done / --check /
    --show / --close on args.budget_file. target() -> (conn, client, inv) or
    None, called only by the modes that read the worker's events."""
    path = Path(args.budget_file)
    reader = reader or ie.read_events
    as_json = bool(getattr(args, "json", False))
    hint = getattr(args, "investigation_id", None) or getattr(args, "investigation", None)
    _only(args)
    if args.show:
        state = load(path, hint)
        out(json.dumps(state, indent=2, ensure_ascii=False) if as_json else "\n".join(show_lines(state)))
        return 0
    if args.done:
        with _locked(path):
            state = load(path, hint)
            if not state.get("current"):
                raise BudgetError("no open budget: --set one first")
            mark_done(state["current"], args.done, args.ref, 1.0 if args.fraction is None else args.fraction,
                      now=now(), correct=bool(getattr(args, "correct", False)))
            save(path, state)
        e = earned_hours(state["current"])
        d = state["current"]["done"][args.done]
        out(json.dumps({"done": args.done, **d, "earned_h": e}) if as_json else
            f"marked {args.done} done ({d['fraction']:.0%}, {d['ref']}); earned now {_h(e)}")
        return 0
    if args.set and getattr(args, "amend", False):
        with _locked(path):
            state = load(path, hint)
            return _amend(args, state, path, out, as_json)
    got = target()
    if got is None:
        return 2
    conn, client, inv = got
    with _locked(path):
        state = load(path, inv)
        state["investigation_id"] = inv
        if args.set:
            return _set(args, state, path, conn, inv, client, home, reader, out, now(), as_json)
        if not state.get("current"):
            raise BudgetError("no open budget: --set one first")
        m, spool, head = _measure_current(state, conn, inv, client, home, reader)
        b = state["current"]
        if args.close:
            actuals = milestone_actuals(b, m["worker_times"], m["spool_times"])
            entry = close(state, args.outcome, m["spent"], m["earned"], actuals, now=now())
            save(path, state)
            out(json.dumps(entry, indent=2, ensure_ascii=False) if as_json else
                f"closed ({args.outcome}): spent {_h(entry['spent_h'])} against P50 {_h(entry['p50_total_h'])} / "
                f"P80 {_h(entry['p80_total_h'])}; earned {_h(entry['earned_h'])}; kept in the budget file's history")
            return 0
        decision = evaluate(b, m["spent"], m["earned"])
        if decision["fire"]:
            record_firing(b, decision, m["spent"], m["earned"], now=now(), head_seq=head)
            save(path, state)
            after = evaluate(b, m["spent"], m["earned"])   # the next trigger points, past this firing
            decision = {**decision, "next_lag_h": after["next_lag_h"], "next_backstop": after["next_backstop"]}
        elif not decision["lagging"] and lag_open(b):
            b["lag_open"] = False                           # the lag episode ended; the next lag fires at once
            save(path, state)
            decision = evaluate(b, m["spent"], m["earned"])
        r = check_report(inv, b, m, decision, spool)
        out(json.dumps(r, indent=2, ensure_ascii=False) if as_json else "\n".join(check_lines(r)))
        return 0


SET_FIELDS = ("question", "milestone", "basis", "worker_session", "worker_estimate", "since_seq", "amend")


def _only(args) -> None:
    mode = "set" if args.set else "done" if args.done else "check" if args.check else "show" if args.show else "close"
    allowed = {"set": set(SET_FIELDS), "done": {"ref", "fraction", "correct"}, "close": {"outcome"}}.get(mode, set())
    for name in SET_FIELDS + ("ref", "fraction", "correct", "outcome"):
        if getattr(args, name, None) not in (None, [], False) and name not in allowed:
            raise BudgetError(f"--{name.replace('_', '-')} does not go with --{mode}")
    if mode == "set" and getattr(args, "amend", False):
        for name in ("since_seq", "worker_session"):
            if getattr(args, name, None) is not None:
                raise BudgetError(f"--{name.replace('_', '-')} does not go with --amend (it keeps the direction's "
                                  "start and worker; a new direction is --set without --amend)")
    if mode == "done" and not args.ref:
        raise BudgetError("--done needs --ref \"#<seq>\" or ev_<12 hex>: the event or captured call proving it")
    if mode == "close" and not args.outcome:
        raise BudgetError(f"--close needs --outcome {'|'.join(OUTCOMES)}")


def _amend(args, state, path, out, as_json) -> int:
    """Correct the open budget: replace the given fields; keep its start,
    worker, thresholds, done marks (of milestones that remain) and firings;
    no history entry."""
    b = state.get("current")
    if not b:
        raise BudgetError("no open budget to amend: --set one first")
    if not (args.milestone or args.question or args.basis or args.worker_estimate):
        raise BudgetError("--amend needs at least one of --milestone, --question, --basis, --worker-estimate")
    fresh = new_budget(question=args.question or b["question"], milestones=args.milestone or b["milestones"],
                       basis=args.basis or b["basis"], set_at=parse_time(b["set_at"]), since_seq=b["since_seq"],
                       worker_session=b.get("worker_session"),
                       worker_estimate=args.worker_estimate or b.get("worker_estimate"))
    ids = {m["id"] for m in fresh["milestones"]}
    dropped = sorted(k for k in b["done"] if k not in ids)
    b.update({k: fresh[k] for k in ("question", "basis", "worker_estimate", "milestones")})
    b["done"] = {k: d for k, d in b["done"].items() if k in ids}
    save(path, state)
    p50, p80 = totals(b)
    if as_json:
        out(json.dumps({"budget": b, "dropped_done": dropped}, indent=2, ensure_ascii=False))
        return 0
    out(f"budget amended: P50 {_h(p50)} / P80 {_h(p80)} over {len(b['milestones'])} milestones; still counting from "
        f"#{b['since_seq']}; done marks and firings kept"
        + (f" (dropped the done marks of {', '.join(dropped)}: no such milestone now)" if dropped else ""))
    return 0


def _set(args, state, path, conn, inv, client, home, reader, out, t, as_json) -> int:
    if args.since_seq is not None:
        if args.since_seq < 1:
            raise BudgetError("--since-seq is a positive event seq")
        page = reader(conn, inv, after=args.since_seq - 1, limit=1, client=client)
        first = page["events"][0] if page["events"] else None
        since, set_at = args.since_seq, (parse_time(first.get("created_at")) if first else None) or t
    else:
        page = reader(conn, inv, after=0, limit=1, client=client)
        since, set_at = page["head_seq"] + 1, t
    b = new_budget(question=args.question, milestones=args.milestone or [], basis=args.basis, set_at=set_at,
                   since_seq=since, worker_session=args.worker_session, worker_estimate=args.worker_estimate)
    replaced = None
    if state.get("current"):
        # The replaced budget ends where the new one starts: no time counted twice.
        old = state["current"]
        events, _ = read_worker_events(conn, inv, old["since_seq"], client=client, reader=reader)
        spool = spool_entries(home, old.get("worker_session")) or []
        m = measure(old, events, spool, before_seq=since, before=set_at)
        actuals = milestone_actuals(old, m["worker_times"], m["spool_times"])
        replaced = close(state, "replaced", m["spent"], m["earned"], actuals, now=t)
    state["current"] = b
    save(path, state)
    p50, p80 = totals(b)
    if as_json:
        out(json.dumps({"budget": b, "replaced": replaced}, indent=2, ensure_ascii=False))
        return 0
    if replaced:
        out(f"closed the previous budget as replaced: spent {_h(replaced['spent_h'])} against P80 "
            f"{_h(replaced['p80_total_h'])}, earned {_h(replaced['earned_h'])} (a correction of this direction is "
            "--set --amend)")
    out(f"budget set: P50 {_h(p50)} / P80 {_h(p80)} over {len(b['milestones'])} milestones, counting the "
        f"worker's active time from #{since}" + (f" (worker session {b['worker_session']})" if b["worker_session"]
                                                 else " (no --worker-session: the author's session events)"))
    return 0

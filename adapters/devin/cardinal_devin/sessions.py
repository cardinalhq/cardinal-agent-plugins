"""Normalize documented Devin session payloads (v1 and v3) into one shape.

Only fields shown in docs.devin.ai are read:

v1 list item (`GET /v1/sessions`): session_id, status, status_enum,
created_at, updated_at (ISO date-time), requesting_user_email,
pull_request.url, structured_output. v1 detail has the same minus
requesting_user_email, plus messages[].

v3 (`GET /v3/organizations/{org_id}/sessions[/{devin_id}]`): session_id,
status, status_detail, created_at, updated_at (integers), user_id,
pull_requests[].pr_url, structured_output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

# v1 status_enum values: working, blocked, expired, finished,
# suspend_requested, suspend_requested_frontend, resume_requested,
# resume_requested_frontend, resumed.
V1_TERMINAL = frozenset({"finished", "expired"})
# v3 status values: new, claimed, running, exit, error, suspended, resuming.
# `suspended` can resume, so it is not terminal; status_detail `finished`
# marks a session Devin considers done whatever its status.
V3_TERMINAL_STATUS = frozenset({"exit", "error"})
V3_TERMINAL_DETAIL = frozenset({"finished"})

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FRACTION_RE = re.compile(r"\.(\d+)")


@dataclass
class Usage:
    session_id: str
    acu_total: float
    acu_cascade: Optional[float]
    acu_devin: Optional[float]
    acu_review: Optional[float]
    acu_terminal: Optional[float]
    period_start_ns: Optional[int]
    period_end_ns: Optional[int]
    pr_urls: List[str] = field(default_factory=list)
    user_email: Optional[str] = None


@dataclass
class Session:
    session_id: str
    api: str
    status: str
    terminal: bool
    updated_ns: Optional[int]
    created_ns: Optional[int]
    email: Optional[str]
    user_id: Optional[str]
    pr_urls: List[str] = field(default_factory=list)
    structured_output: Any = None
    has_structured_output: bool = False


@dataclass(frozen=True)
class PullRef:
    host: str
    owner: str
    repo: str
    number: int
    kind: str  # "github" or "gitlab"


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def to_ns(raw: Any) -> Optional[int]:
    """ISO-8601 string or unix number (s/ms/us/ns, by magnitude) → epoch ns."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        if raw <= 0:
            return None
        if raw < 10 ** 11:
            return raw * 10 ** 9
        if raw < 10 ** 14:
            return raw * 10 ** 6
        if raw < 10 ** 17:
            return raw * 10 ** 3
        return raw
    if isinstance(raw, float):
        return to_ns(int(raw)) if raw > 0 else None
    text = _str(raw)
    if text is None:
        return None
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python 3.9's fromisoformat rejects compact offsets like +0000.
    if len(text) > 5 and text[-5] in "+-" and text[-4:].isdigit():
        text = text[:-2] + ":" + text[-2:]
    # Python 3.9's fromisoformat only takes 3- or 6-digit fractions.
    text = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return ((when - _EPOCH) // timedelta(microseconds=1)) * 1000


def _unique(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def normalize(raw: Any, api: str, *, email: Optional[str] = None) -> Optional[Session]:
    """A Session, or None when the payload has no session_id."""
    if not isinstance(raw, dict):
        return None
    session_id = _str(raw.get("session_id"))
    if session_id is None:
        return None
    structured = raw.get("structured_output")
    common = {
        "session_id": session_id,
        "api": api,
        "updated_ns": to_ns(raw.get("updated_at")),
        "created_ns": to_ns(raw.get("created_at")),
        "structured_output": structured,
        "has_structured_output": structured is not None,
    }
    if api == "v1":
        status_enum = _str(raw.get("status_enum"))
        pr = raw.get("pull_request")
        pr_url = _str(pr.get("url")) if isinstance(pr, dict) else None
        return Session(
            status=status_enum or _str(raw.get("status")) or "",
            terminal=status_enum in V1_TERMINAL,
            email=_str(raw.get("requesting_user_email")) or email,
            user_id=None,
            pr_urls=[pr_url] if pr_url else [],
            **common,
        )
    status = _str(raw.get("status")) or ""
    detail = _str(raw.get("status_detail"))
    prs = raw.get("pull_requests")
    urls = [
        p["pr_url"] for p in (prs if isinstance(prs, list) else [])
        if isinstance(p, dict) and _str(p.get("pr_url"))
    ]
    return Session(
        status=f"{status}/{detail}" if detail else status,
        terminal=status in V3_TERMINAL_STATUS or detail in V3_TERMINAL_DETAIL,
        email=email,
        user_id=_str(raw.get("user_id")),
        pr_urls=_unique(urls),
        **common,
    )


def primary_pr_url(urls: List[str]) -> Optional[str]:
    """The PR decisions attach to. `pull_requests[]` has no documented
    order, so pick deterministically: the highest PR number, then the
    greatest URL; unparseable URLs rank last."""
    if not urls:
        return None

    def rank(url: str):
        ref = parse_pr_url(url)
        return (ref.number if ref else -1, url)

    return max(urls, key=rank)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def normalize_v1_consumption(row: Any) -> Optional[Usage]:
    """One `sessions[]` entry from `/v1/enterprise/consumption`. The v1
    surface reports only the aggregate `acu_used`, so per-product totals
    are None."""
    if not isinstance(row, dict):
        return None
    session_id = _str(row.get("session_name"))
    if session_id is None:
        return None
    total = _num(row.get("acu_used")) or 0.0
    prs = row.get("pull_requests")
    urls: List[str] = []
    if isinstance(prs, list):
        for entry in prs:
            if isinstance(entry, dict):
                url = _str(entry.get("pr_url"))
                if url:
                    urls.append(url)
    created_ns = to_ns(row.get("created_at"))
    return Usage(
        session_id=session_id,
        acu_total=total,
        acu_cascade=None,
        acu_devin=None,
        acu_review=None,
        acu_terminal=None,
        period_start_ns=created_ns,
        period_end_ns=None,
        pr_urls=_unique(urls),
        user_email=_str(row.get("user_email")),
    )


def normalize_v3_consumption(session_id: str, resp: Any) -> Optional[Usage]:
    """`/v3/.../consumption/daily/sessions/{devin_id}`. Sums the daily
    rows into aggregate totals; per-product totals are None when every
    contribution is missing so they never fingerprint as zero."""
    if not isinstance(resp, dict):
        return None
    days = resp.get("consumption_by_date")
    if not isinstance(days, list):
        days = []
    total = _num(resp.get("total_acus"))
    if total is None:
        total = 0.0
    products = {"cascade": None, "devin": None, "review": None, "terminal": None}
    period_start_ns: Optional[int] = None
    period_end_ns: Optional[int] = None
    aggregate = 0.0
    aggregate_seen = False
    for day in days:
        if not isinstance(day, dict):
            continue
        day_total = _num(day.get("acus"))
        if day_total is not None:
            aggregate += day_total
            aggregate_seen = True
        split = day.get("acus_by_product")
        if isinstance(split, dict):
            for key in products:
                value = _num(split.get(key))
                if value is None:
                    continue
                products[key] = (products[key] or 0.0) + value
        stamp = to_ns(day.get("date"))
        if stamp is not None:
            if period_start_ns is None or stamp < period_start_ns:
                period_start_ns = stamp
            if period_end_ns is None or stamp > period_end_ns:
                period_end_ns = stamp
    if aggregate_seen and total == 0.0:
        total = aggregate
    return Usage(
        session_id=session_id,
        acu_total=total,
        acu_cascade=products["cascade"],
        acu_devin=products["devin"],
        acu_review=products["review"],
        acu_terminal=products["terminal"],
        period_start_ns=period_start_ns,
        period_end_ns=period_end_ns,
        pr_urls=[],
        user_email=None,
    )


_GITHUB_PR_RE = re.compile(r"^https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")
_GITLAB_MR_RE = re.compile(r"^https?://([^/]+)/(.+)/([^/]+)/-/merge_requests/(\d+)(?:[/?#].*)?$")


def parse_pr_url(url: Optional[str]) -> Optional[PullRef]:
    """GitHub-style `/{owner}/{repo}/pull/{n}` or GitLab-style
    `/{group...}/{repo}/-/merge_requests/{n}`; None otherwise."""
    text = _str(url)
    if text is None:
        return None
    text = text.strip()
    m = _GITLAB_MR_RE.match(text)
    if m:
        return PullRef(m.group(1).lower(), m.group(2), m.group(3), int(m.group(4)), "gitlab")
    m = _GITHUB_PR_RE.match(text)
    if m:
        return PullRef(m.group(1).lower(), m.group(2), m.group(3), int(m.group(4)), "github")
    return None

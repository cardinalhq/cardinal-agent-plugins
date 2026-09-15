"""One poll cycle: list sessions, emit what changed, record it.

Per session:
- `cardinal.git_state` once per PR URL when the PR first appears, and
  again when the session reaches a terminal status only if the facts
  (branch, head sha, repo, ...) changed.
- `cardinal.decision` whenever `structured_output` holds a decision not
  yet sent (or one whose content, or attached PR, changed), whatever the
  session's status. Decisions attach to the primary PR
  (sessions.primary_pr_url).

State is saved after every session whose records ingest accepted, and
once at the end of the cycle. A crash between a successful POST and that
save can re-send that one session's records once.

v3's `updated_after` filter is an optimisation, never trusted blindly:
each cycle an unfiltered first page is compared with the filtered
listing. If the filter hides a session updated after the value sent, or
returns sessions older than it (a unit mismatch in either direction), it
is disabled for this poller (persisted) and the cycle falls back to the
lookback listing bounded by --max-pages.

State stays bounded: entries are small; finished sessions that never sent
anything are dropped once past the lookback window; finished entries
that did send are kept for the longest of state_retention_days, lookback
+ 1 day, and active TTL + 1 day, so a resumed session re-sends nothing;
above max_state_sessions the oldest finished entries outside the lookback
window are evicted (never one inside it, which could re-send).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from cardinal_core import otlp

from . import __version__, telemetry
from .client import ApiError, DevinClient, GitHubClient
from .sessions import (
    Session, Usage, normalize, normalize_v1_consumption, normalize_v3_consumption, primary_pr_url,
)
from .state import PollState

log = logging.getLogger("cardinal_devin")

NS = 1_000_000_000
DAY = 86_400
USER_CACHE_TTL = DAY
USER_NEGATIVE_TTL = 3_600
MS_THRESHOLD = 10 ** 11  # integer timestamps at or above this are milliseconds
DIGEST_LEN = 16
FILTER_DISABLED = "v3_filter_disabled"


def _short(value: Any) -> str:
    return telemetry.fingerprint(value)[:DIGEST_LEN]


@dataclass
class PollOptions:
    lookback_days: float = 7.0
    active_ttl_days: float = 14.0
    state_retention_days: float = 90.0
    max_state_sessions: int = 20_000
    overlap_seconds: int = 300
    not_found_limit: int = 3
    emit_decisions: bool = True
    emit_usage: bool = True
    retry_updated_after_filter: bool = False
    dry_run: bool = False
    emit_timeout: float = 10.0


@dataclass
class PollResult:
    listed: int = 0
    refreshed: int = 0
    git_state: int = 0
    decisions: int = 0
    skipped_decisions: int = 0
    usage: int = 0
    errors: int = 0
    truncated: bool = False
    bodies: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"listed={self.listed} refreshed={self.refreshed} git_state={self.git_state} "
            f"decisions={self.decisions} skipped_decisions={self.skipped_decisions} "
            f"usage={self.usage} errors={self.errors}"
            + (" truncated" if self.truncated else "")
        )


class Poller:
    def __init__(
        self,
        *,
        devin: DevinClient,
        state: PollState,
        connection: Optional[otlp.IngestConnection],
        connection_state: Dict[str, Any],
        github: Optional[GitHubClient] = None,
        options: Optional[PollOptions] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.devin = devin
        self.state = state
        self.connection = connection
        self.connection_state = connection_state
        self.github = github
        self.options = options or PollOptions()
        self.clock = clock
        if connection is None and not self.options.dry_run:
            raise ValueError("not connected to Cardinal; run `cardinal-devin connect`")

    # -- cycle ---------------------------------------------------------------

    def poll_once(self) -> PollResult:
        result = PollResult()
        now = self.clock()
        api = self.devin.api_version
        floor = int(now - self.options.lookback_days * DAY)
        cutoff_ns = floor * NS
        self._usage_disabled_this_cycle = False
        self._touched_sids: set = set()

        filter_validated = False
        if api == "v3" and self.options.retry_updated_after_filter and self.state.data.pop(FILTER_DISABLED, None):
            log.info("re-enabling the v3 updated_after filter")
        disabled = self.state.data.get(FILTER_DISABLED) if api == "v3" else None
        if api == "v3" and not disabled:
            listed, filter_validated = self._list_v3_filtered(floor, now)
        else:
            if disabled:
                log.warning(
                    "v3 updated_after filter is disabled (%s); listing sessions without it, bounded by "
                    "--max-pages. Retry with --retry-updated-after-filter.", disabled.get("reason"),
                )
            listed = list(self.devin.iter_sessions())
        result.truncated = self.devin.last_list_truncated

        seen = set()
        for raw in listed:
            result.listed += 1
            session = normalize(raw, api)
            if session is None:
                continue
            seen.add(session.session_id)
            if (
                session.session_id not in self.state.sessions
                and session.updated_ns is not None
                and session.updated_ns < cutoff_ns
            ):
                continue
            self._safe_handle(session, from_list=True, result=result)

        self._refresh_unlisted(seen, now, result)

        if self.options.emit_usage:
            self._emit_usage(now, result)

        # Only advance past what was actually seen through a filter that checked out.
        if filter_validated and not result.truncated and not result.errors:
            self.state.set_hwm(api, int(now) - self.options.overlap_seconds)
        self._prune(now, cutoff_ns)
        self.state.data["last_poll"] = int(now)
        self.state.save()
        return result

    # -- v3 updated_after filter ---------------------------------------------

    def _list_v3_filtered(self, floor: int, now: float) -> Tuple[List[Dict[str, Any]], bool]:
        since = max(self.state.hwm("v3") or floor, floor)
        probe = self.devin.first_page()  # before the filtered listing, so a later update can't race it
        for raw in probe:
            self._observe_timestamp_unit(raw)
        sent = since * 1000 if self.state.data.get("v3_timestamp_unit") == "ms" else since
        listed = list(self.devin.iter_sessions(updated_after=sent))
        for raw in listed:
            self._observe_timestamp_unit(raw)
        problem = self._filter_problem(since, sent, probe, listed, self.devin.last_list_truncated)
        if problem is None:
            return listed, True
        self.state.data[FILTER_DISABLED] = {"reason": problem, "at": int(now), "updated_after_sent": sent}
        log.warning(
            "v3 updated_after filter disabled: %s. Falling back to listing sessions without it, bounded by "
            "--max-pages; retry with --retry-updated-after-filter.", problem,
        )
        return list(self.devin.iter_sessions()), False

    @staticmethod
    def _filter_problem(
        since: int, sent: int, probe: List[Dict[str, Any]], listed: List[Dict[str, Any]], truncated: bool,
    ) -> Optional[str]:
        since_ns = since * NS
        listed_sessions = [s for s in (normalize(r, "v3") for r in listed) if s is not None]
        for session in listed_sessions:
            if session.updated_ns is not None and session.updated_ns < since_ns - NS:
                return (f"filtered listing returned session {session.session_id} last updated before "
                        f"updated_after={sent}")
        if not truncated:
            listed_ids = {s.session_id for s in listed_sessions}
            for session in (normalize(r, "v3") for r in probe):
                if (
                    session is not None
                    and session.updated_ns is not None
                    and session.updated_ns > since_ns
                    and session.session_id not in listed_ids
                ):
                    return (f"session {session.session_id} was updated after updated_after={sent} "
                            "but is missing from the filtered listing")
        return None

    def _observe_timestamp_unit(self, raw: Any) -> None:
        value = raw.get("updated_at") if isinstance(raw, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            self.state.data["v3_timestamp_unit"] = "ms" if value >= MS_THRESHOLD else "s"

    # -- tracked sessions ----------------------------------------------------

    def _refresh_unlisted(self, seen: set, now: float, result: PollResult) -> None:
        """Active sessions the listing did not return (v3's updated_after
        filter, or v1 past max_pages) are fetched individually."""
        ttl_ns = int(self.options.active_ttl_days * DAY * NS)
        for sid, entry in list(self.state.sessions.items()):
            if sid in seen or entry.get("terminal") or entry.get("stale") or entry.get("gone"):
                continue
            updated = entry.get("updated_ns")
            if isinstance(updated, int) and int(now * NS) - updated > ttl_ns:
                entry["stale"] = True
                log.info("session %s: no update in %.0f days; no longer polled", sid, self.options.active_ttl_days)
                continue
            try:
                raw = self.devin.get_session(sid)
            except ApiError as exc:
                log.warning("session %s: detail fetch failed: %s", sid, exc)
                result.errors += 1
                continue
            if raw is None:
                # One not-found is not proof: stop polling only after several
                # cycles in a row. Decisions were already sent when observed.
                misses = int(entry.get("not_found") or 0) + 1
                entry["not_found"] = misses
                if misses >= self.options.not_found_limit:
                    entry["gone"] = True
                    log.info("session %s: not found %d cycles in a row; no longer polled", sid, misses)
                else:
                    log.info("session %s: detail not found (%d/%d)", sid, misses, self.options.not_found_limit)
                continue
            session = normalize(raw, self.devin.api_version, email=entry.get("email"))
            if session is not None:
                result.refreshed += 1
                self._safe_handle(session, from_list=False, result=result)

    def _prune(self, now: float, cutoff_ns: int) -> None:
        sessions = self.state.sessions
        keep_days = max(
            self.options.state_retention_days,
            self.options.lookback_days + 1,
            self.options.active_ttl_days + 1,
        )
        horizon_ns = int((now - keep_days * DAY) * NS)

        def done(entry: Dict[str, Any]) -> bool:
            return bool(entry.get("terminal") or entry.get("stale") or entry.get("gone"))

        def old(entry: Dict[str, Any], limit_ns: int) -> bool:
            updated = entry.get("updated_ns")
            # Without a usable timestamp its age is unknown; pruning it would
            # make the session look new and re-send it next cycle.
            return isinstance(updated, int) and updated < limit_ns

        for sid in list(sessions):
            entry = sessions[sid]
            if not done(entry):
                continue
            sent_nothing = not entry.get("prs") and not entry.get("decisions")
            # Past the lookback an untracked session is ignored, so an entry
            # that never sent anything protects nothing once it is that old.
            if old(entry, horizon_ns) or (sent_nothing and old(entry, cutoff_ns)):
                del sessions[sid]

        excess = len(sessions) - self.options.max_state_sessions
        if excess > 0:
            evictable = sorted(
                (sid for sid, e in sessions.items() if done(e) and old(e, cutoff_ns)),
                key=lambda sid: sessions[sid].get("updated_ns") or 0,
            )
            for sid in evictable[:excess]:
                del sessions[sid]
            if len(sessions) > self.options.max_state_sessions:
                log.warning(
                    "poll state tracks %d sessions, above --max-state-sessions=%d; active sessions and "
                    "sessions inside the lookback window are never evicted",
                    len(sessions), self.options.max_state_sessions,
                )

        for user_id in list(self.state.users):
            cached = self.state.users[user_id]
            if not isinstance(cached, dict) or now - float(cached.get("at") or 0) >= USER_CACHE_TTL:
                del self.state.users[user_id]

    # -- one session ---------------------------------------------------------

    def _safe_handle(self, session: Session, *, from_list: bool, result: PollResult) -> None:
        try:
            self._handle(session, from_list=from_list, result=result)
        except (ApiError, telemetry.EmitError) as exc:
            log.warning("session %s: %s (will retry next poll)", session.session_id, exc)
            result.errors += 1

    def _handle(self, session: Session, *, from_list: bool, result: PollResult) -> None:
        sid = session.session_id
        prior = self.state.sessions.get(sid) or {}
        if (
            session.terminal
            and prior.get("terminal")
            and prior.get("finalized")
            and prior.get("updated_ns") == session.updated_ns
            and (not self.options.emit_decisions or prior.get("output_digest"))
        ):
            return  # finished, unchanged since we finalized it, decisions already processed

        email = session.email or prior.get("email")
        if session.terminal and from_list:
            # Read the final payload from the detail route before finalizing.
            detail = normalize(self.devin.get_session(sid), session.api, email=email)
            if detail is not None:
                detail.pr_urls = detail.pr_urls + [u for u in session.pr_urls if u not in detail.pr_urls]
                if not detail.has_structured_output and session.has_structured_output:
                    detail.structured_output = session.structured_output
                    detail.has_structured_output = True
                session = detail
        if session.user_id and not email:
            email = self._user_email(session.user_id)

        finalize = session.terminal
        sent_prs: Dict[str, str] = dict(prior.get("prs") or {})
        sent_decisions: Dict[str, str] = dict(prior.get("decisions") or {})
        ts_ns = session.updated_ns or int(self.clock() * NS)
        facts_cache: Dict[str, Dict[str, Any]] = {}

        def facts_for(url: Optional[str]) -> Dict[str, Any]:
            if not url:
                return {}
            if url not in facts_cache:
                facts_cache[url] = telemetry.pr_facts(url, self.github)
            return facts_cache[url]

        records: List[Dict[str, Any]] = []
        new_prs: Dict[str, str] = {}
        for url in session.pr_urls:
            if url in sent_prs and not finalize:
                continue
            attrs = telemetry.git_state_attrs(sid, facts_for(url))
            digest = _short(attrs)
            if sent_prs.get(url) == digest:
                continue
            records.append(otlp.log_record(telemetry.EVENT_GIT_STATE, attrs, ts_ns))
            new_prs[url] = digest

        primary = primary_pr_url(session.pr_urls)
        output = session.structured_output if session.has_structured_output else None
        output_digest = _short({"structured_output": output, "primary_pr": primary})
        new_decisions: Dict[str, str] = {}
        skipped = 0
        if self.options.emit_decisions:
            if finalize and not session.has_structured_output:
                log.info("session %s: no structured_output (session not created with the Cardinal "
                         "decision schema?)", sid)
            if session.has_structured_output and output_digest != prior.get("output_digest"):
                # Stored per decision: "<content digest>" for explicit ids,
                # "<content digest>:<choice key>" for ids derived from the choice.
                prior_auto = {i: v.split(":", 1)[1] for i, v in sent_decisions.items() if ":" in v}
                built, reasons, problem, auto_keys = telemetry.build_decisions_stable(
                    output, prior_auto=prior_auto, prior_ids=sent_decisions.keys(),
                )
                if problem:
                    log.info("session %s: %s", sid, problem)
                for reason in reasons:
                    log.warning("session %s: skipped %s", sid, reason)
                skipped = len(reasons)
                for decision in built:
                    ident = decision["id"]
                    marker = telemetry.decision_digest(decision, primary)[:DIGEST_LEN]
                    if ident in auto_keys:
                        marker = f"{marker}:{auto_keys[ident]}"
                    if sent_decisions.get(ident) == marker:
                        continue
                    attrs = telemetry.decision_attrs(sid, decision, facts_for(primary))
                    records.append(otlp.log_record(telemetry.EVENT_DECISION, attrs, ts_ns))
                    new_decisions[ident] = marker

        if records:
            resource = telemetry.resource(self.connection_state, email, __version__)
            if self.options.dry_run:
                result.bodies.append(telemetry.otlp_body(records, resource, __version__))
            else:
                assert self.connection is not None
                telemetry.send_logs(
                    records, self.connection, resource, __version__, timeout=self.options.emit_timeout,
                )
            self._touched_sids.add(sid)

        result.git_state += len(new_prs)
        result.decisions += len(new_decisions)
        result.skipped_decisions += skipped
        sent_prs.update(new_prs)
        sent_decisions.update(new_decisions)

        entry: Dict[str, Any] = {"updated_ns": session.updated_ns}
        if session.terminal:
            entry["terminal"] = True
            if finalize:
                entry["finalized"] = True
        else:
            # Only needed while the session is still polled.
            if email:
                entry["email"] = email
            if session.user_id:
                entry["user_id"] = session.user_id
        if sent_prs:
            entry["prs"] = sent_prs
        if sent_decisions:
            entry["decisions"] = sent_decisions
        kept_digest = output_digest if self.options.emit_decisions else prior.get("output_digest")
        if kept_digest:
            entry["output_digest"] = kept_digest
        if entry != prior:
            self.state.sessions[sid] = entry
            if records:
                self.state.save()  # narrows the crash window to this session

    # -- usage (ACU) ---------------------------------------------------------

    def _emit_usage(self, now: float, result: PollResult) -> None:
        if self.devin.api_version == "v1":
            self._emit_usage_v1(now, result)
        else:
            self._emit_usage_v3(now, result)

    def _emit_usage_v1(self, now: float, result: PollResult) -> None:
        try:
            rows = list(self.devin.iter_v1_enterprise_consumption())
        except ApiError as exc:
            if self._handle_usage_scope_error(exc):
                return
            log.warning("Devin enterprise consumption fetch failed: %s (will retry next poll)", exc)
            result.errors += 1
            return
        for row in rows:
            usage = normalize_v1_consumption(row)
            if usage is None:
                continue
            self._send_usage(usage, now, result)

    def _emit_usage_v3(self, now: float, result: PollResult) -> None:
        for sid in sorted(self._touched_sids):
            try:
                resp = self.devin.get_v3_session_consumption(sid)
            except ApiError as exc:
                if self._handle_usage_scope_error(exc):
                    return
                log.warning(
                    "Devin v3 consumption fetch failed for %s: %s (will retry next poll)", sid, exc,
                )
                result.errors += 1
                continue
            if resp is None:
                continue
            usage = normalize_v3_consumption(sid, resp)
            if usage is None:
                continue
            entry = self.state.sessions.get(sid) or {}
            prs = entry.get("prs")
            usage.pr_urls = list(prs) if isinstance(prs, dict) else []
            usage.user_email = entry.get("email")
            self._send_usage(usage, now, result)

    def _handle_usage_scope_error(self, exc: ApiError) -> bool:
        if exc.status not in (401, 403):
            return False
        self._usage_disabled_this_cycle = True
        if not self.state.data.get("usage_scope_warned"):
            log.warning("Devin key lacks consumption scope; ACU usage disabled this run")
            self.state.data["usage_scope_warned"] = True
        return True

    def _send_usage(self, usage: Usage, now: float, result: PollResult) -> None:
        if self._usage_disabled_this_cycle:
            return
        sid = usage.session_id
        primary = primary_pr_url(usage.pr_urls)
        facts = telemetry.pr_facts(primary, self.github) if primary else {}
        attrs = telemetry.usage_attrs(sid, usage, facts)
        digest = telemetry.usage_digest(usage, primary)
        if self.state.usage_digests.get(sid) == digest:
            return
        entry = self.state.sessions.get(sid) or {}
        email = usage.user_email or entry.get("email")
        ts_ns = usage.period_end_ns or usage.period_start_ns or int(now * NS)
        record = otlp.log_record(telemetry.EVENT_USAGE, attrs, ts_ns)
        resource = telemetry.resource(self.connection_state, email, __version__)
        try:
            if self.options.dry_run:
                result.bodies.append(telemetry.otlp_body([record], resource, __version__))
            else:
                assert self.connection is not None
                telemetry.send_logs(
                    [record], self.connection, resource, __version__, timeout=self.options.emit_timeout,
                )
        except telemetry.EmitError as exc:
            log.warning("session %s: usage emit failed: %s (will retry next poll)", sid, exc)
            result.errors += 1
            return
        self.state.usage_digests[sid] = digest
        result.usage += 1
        self.state.save()

    def _user_email(self, user_id: str) -> Optional[str]:
        now = self.clock()
        cached = self.state.users.get(user_id)
        if isinstance(cached, dict):
            ttl = USER_CACHE_TTL if cached.get("email") else USER_NEGATIVE_TTL
            if now - float(cached.get("at") or 0) < ttl:
                return cached.get("email")
        try:
            email = self.devin.get_user_email(user_id)
        except ApiError as exc:
            log.warning("could not resolve Devin user %s to an email (needs ViewOrgMembership): %s", user_id, exc)
            email = None
        self.state.users[user_id] = {"email": email, "at": int(now)}
        return email

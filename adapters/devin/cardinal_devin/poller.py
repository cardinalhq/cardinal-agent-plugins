"""One poll cycle: list sessions, emit what changed, record it.

Per session:
- `cardinal.git_state` once per PR URL when the PR first appears, and
  again when the session reaches a terminal status only if the facts
  (branch, head sha, repo, ...) changed.
- `cardinal.decision` whenever `structured_output` holds a decision not
  yet sent (or one whose content, or attached PR, changed), whatever the
  session's status. Decisions attach to the primary PR
  (sessions.primary_pr_url).

State is updated only after ingest accepts the records, so a failed POST
is retried on the next cycle. A crash between a successful POST and the
state save can re-send that one session's records once.

Untracked sessions last updated before the lookback window are ignored.
Finished, stale, and gone entries are kept for the longest of
state_retention_days, lookback + 1 day, and active TTL + 1 day, so a
session resumed within that time is recognised and re-sends nothing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from cardinal_core import otlp

from . import __version__, telemetry
from .client import ApiError, DevinClient, GitHubClient
from .sessions import Session, normalize, primary_pr_url
from .state import PollState

log = logging.getLogger("cardinal_devin")

NS = 1_000_000_000
DAY = 86_400
USER_CACHE_TTL = DAY
USER_NEGATIVE_TTL = 3_600
MS_THRESHOLD = 10 ** 11  # integer timestamps at or above this are milliseconds


@dataclass
class PollOptions:
    lookback_days: float = 7.0
    active_ttl_days: float = 14.0
    state_retention_days: float = 90.0
    overlap_seconds: int = 300
    not_found_limit: int = 3
    emit_decisions: bool = True
    dry_run: bool = False
    emit_timeout: float = 10.0


@dataclass
class PollResult:
    listed: int = 0
    refreshed: int = 0
    git_state: int = 0
    decisions: int = 0
    skipped_decisions: int = 0
    errors: int = 0
    truncated: bool = False
    bodies: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"listed={self.listed} refreshed={self.refreshed} git_state={self.git_state} "
            f"decisions={self.decisions} skipped_decisions={self.skipped_decisions} "
            f"errors={self.errors}" + (" truncated" if self.truncated else "")
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

        updated_after = None
        if api == "v3":
            since = max(self.state.hwm(api) or floor, floor)
            # The docs type updated_after as an integer with no unit. Send it in
            # the unit the API's own updated_at values use (seconds until seen
            # otherwise); a wrong guess only widens the window.
            updated_after = since * 1000 if self.state.data.get("v3_timestamp_unit") == "ms" else since

        seen = set()
        for raw in self.devin.iter_sessions(updated_after=updated_after):
            result.listed += 1
            if api == "v3":
                self._observe_timestamp_unit(raw)
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
        result.truncated = self.devin.last_list_truncated

        self._refresh_unlisted(seen, now, result)

        if api == "v3" and not result.truncated and not result.errors:
            self.state.set_hwm(api, int(now) - self.options.overlap_seconds)
        self._prune(now)
        self.state.data["last_poll"] = int(now)
        self.state.save()
        return result

    def _observe_timestamp_unit(self, raw: Any) -> None:
        value = raw.get("updated_at") if isinstance(raw, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            self.state.data["v3_timestamp_unit"] = "ms" if value >= MS_THRESHOLD else "s"

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

    def _prune(self, now: float) -> None:
        keep_days = max(
            self.options.state_retention_days,
            self.options.lookback_days + 1,
            self.options.active_ttl_days + 1,
        )
        horizon_ns = int((now - keep_days * DAY) * NS)
        for sid in list(self.state.sessions):
            entry = self.state.sessions[sid]
            updated = entry.get("updated_ns")
            done = entry.get("terminal") or entry.get("stale") or entry.get("gone")
            if done and isinstance(updated, int) and updated < horizon_ns:
                del self.state.sessions[sid]

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
            and prior.get("finalized_ns") is not None
            and prior.get("finalized_ns") == session.updated_ns
            and (not self.options.emit_decisions or prior.get("output_digest") is not None)
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
            digest = telemetry.fingerprint(attrs)
            if sent_prs.get(url) == digest:
                continue
            records.append(otlp.log_record(telemetry.EVENT_GIT_STATE, attrs, ts_ns))
            new_prs[url] = digest

        primary = primary_pr_url(session.pr_urls)
        output = session.structured_output if session.has_structured_output else None
        output_digest = telemetry.fingerprint({"structured_output": output, "primary_pr": primary})
        new_decisions: Dict[str, str] = {}
        skipped = 0
        if self.options.emit_decisions:
            if finalize and not session.has_structured_output:
                log.info("session %s: no structured_output (session not created with the Cardinal "
                         "decision schema?)", sid)
            if session.has_structured_output and output_digest != prior.get("output_digest"):
                built, reasons, problem = telemetry.build_decisions(output)
                if problem:
                    log.info("session %s: %s", sid, problem)
                for reason in reasons:
                    log.warning("session %s: skipped %s", sid, reason)
                skipped = len(reasons)
                for decision in built:
                    digest = telemetry.decision_digest(decision, primary)
                    if sent_decisions.get(decision["id"]) == digest:
                        continue
                    attrs = telemetry.decision_attrs(sid, decision, facts_for(primary))
                    records.append(otlp.log_record(telemetry.EVENT_DECISION, attrs, ts_ns))
                    new_decisions[decision["id"]] = digest

        if records:
            resource = telemetry.resource(self.connection_state, email, __version__)
            if self.options.dry_run:
                result.bodies.append(telemetry.otlp_body(records, resource, __version__))
            else:
                assert self.connection is not None
                telemetry.send_logs(
                    records, self.connection, resource, __version__, timeout=self.options.emit_timeout,
                )

        result.git_state += len(new_prs)
        result.decisions += len(new_decisions)
        result.skipped_decisions += skipped
        sent_prs.update(new_prs)
        sent_decisions.update(new_decisions)
        entry: Dict[str, Any] = {
            "status": session.status,
            "terminal": session.terminal,
            "updated_ns": session.updated_ns,
            "email": email,
            "user_id": session.user_id,
            "prs": sent_prs,
            "decisions": sent_decisions,
            "output_digest": output_digest if self.options.emit_decisions else prior.get("output_digest"),
        }
        if finalize:
            entry["finalized_ns"] = session.updated_ns
        if entry != prior:
            self.state.sessions[sid] = entry
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

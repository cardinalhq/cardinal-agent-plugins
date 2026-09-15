"""Cardinal record building for Devin sessions.

Only contract events are produced: `cardinal.git_state` (underscore keys,
the codex/cursor/gemini spelling) and `cardinal.decision` (attributes from
cardinal_core.decisions). Devin's `messages[]` has no matching contract
event, so it is not emitted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cardinal_core import decisions as core_decisions
from cardinal_core import initiative, otlp

from . import AGENT_RUNTIME, SCOPE_NAME, SERVICE_NAME
from .client import ApiError, GitHubClient
from .sessions import Usage, parse_pr_url

log = logging.getLogger("cardinal_devin")

EVENT_GIT_STATE = "cardinal.git_state"
EVENT_DECISION = core_decisions.DECISION_EVENT
EVENT_USAGE = "cardinal.turn_usage"
MAX_DECISIONS = 50


class EmitError(RuntimeError):
    pass


# --- git_state --------------------------------------------------------------


def pr_facts(pr_url: str, github: Optional[GitHubClient]) -> Dict[str, Any]:
    """git_state facts for one PR URL, in emission order.

    From the URL alone: repo, remote URL, PR number, PR URL. With a GitHub
    client whose API serves this PR's web host: head branch and head sha
    from the pull, the base repo's clone_url (only if it is on the same
    host), and the initiative classified from the head branch. PRs on any
    other host are never looked up, so a GHE PR can't pick up facts from a
    same-named github.com repo. Without a branch there is no initiative
    (resolving None would claim "research", which is wrong for a PR)."""
    facts: Dict[str, Any] = {
        "cardinal_head_sha": None,
        "cardinal_branch": None,
        "cardinal_repo": None,
        "cardinal_remote_url": None,
        "cardinal_pr_number": None,
        "cardinal_pr_url": pr_url,
        "cardinal_initiative_name": None,
        "cardinal_initiative_type": None,
    }
    ref = parse_pr_url(pr_url)
    if ref is None:
        return facts
    remote = f"https://{ref.host}/{ref.owner}/{ref.repo}.git"
    facts["cardinal_remote_url"] = remote
    facts["cardinal_repo"] = initiative.canonical_repo(remote)
    facts["cardinal_pr_number"] = ref.number
    if ref.kind != "github" or github is None:
        return facts
    if not github.serves(ref.host):
        log.debug("not looking up %s: GitHub API serves %s", pr_url, github.web_host)
        return facts
    try:
        pull = github.get_pull(ref.owner, ref.repo, ref.number)
    except ApiError as exc:
        log.warning("GitHub lookup failed for %s: %s", pr_url, exc)
        return facts
    if not pull:
        log.warning("GitHub has no pull request at %s (token scope?)", pr_url)
        return facts
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    clone_url = base_repo.get("clone_url")
    if (
        isinstance(clone_url, str)
        and (urllib.parse.urlsplit(clone_url).hostname or "").lower() == ref.host
        and initiative.canonical_repo(clone_url)
    ):
        facts["cardinal_remote_url"] = clone_url
        facts["cardinal_repo"] = initiative.canonical_repo(clone_url)
    branch = head.get("ref")
    sha = head.get("sha")
    if isinstance(sha, str) and sha:
        facts["cardinal_head_sha"] = sha
    if isinstance(branch, str) and branch:
        facts["cardinal_branch"] = branch
        name, kind = initiative.resolve_initiative(branch)
        facts["cardinal_initiative_name"] = name
        facts["cardinal_initiative_type"] = kind
    return facts


def git_state_attrs(session_id: str, facts: Dict[str, Any]) -> Dict[str, Any]:
    return {"session_id": session_id, **facts}


def fingerprint(attrs: Dict[str, Any]) -> str:
    """Stable hash of the non-empty attributes (what log_record would send)."""
    kept = {k: v for k, v in attrs.items() if v is not None and v != ""}
    return hashlib.sha256(
        json.dumps(kept, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


# --- decisions --------------------------------------------------------------


class InvalidDecision(ValueError):
    pass


def _optional_text(item: Dict[str, Any], key: str) -> Optional[str]:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidDecision(f"{key} must be a string")
    return value


def _string_list(item: Dict[str, Any], key: str) -> List[str]:
    value = item.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise InvalidDecision(f"{key} must be an array of strings")
    return value


def _clean_path(value: str) -> Optional[str]:
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    if not text:
        return None
    if ".." in PurePosixPath(text).parts:
        raise InvalidDecision(f"anchor path {value!r} must stay inside the repo")
    return str(PurePosixPath(text))


def _anchors(item: Dict[str, Any]) -> List[Dict[str, str]]:
    value = item.get("anchors")
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidDecision("anchors must be an array")
    out: List[Dict[str, str]] = []
    for position, anchor in enumerate(value):
        if not isinstance(anchor, dict):
            raise InvalidDecision(f"anchors[{position}] must be an object")
        kind = anchor.get("kind")
        if kind not in core_decisions.ANCHOR_KINDS:
            raise InvalidDecision(
                f"anchors[{position}].kind must be one of {', '.join(core_decisions.ANCHOR_KINDS)}"
            )
        identifier = anchor.get("identifier")
        if not isinstance(identifier, str) or not identifier.strip():
            raise InvalidDecision(f"anchors[{position}].identifier is required")
        raw_path = anchor.get("path")
        if raw_path is not None and not isinstance(raw_path, str):
            raise InvalidDecision(f"anchors[{position}].path must be a string")
        path = _clean_path(raw_path) if raw_path else None
        if kind in ("file", "directory"):
            path = path or _clean_path(identifier)
            if not path:
                raise InvalidDecision(f"anchors[{position}] needs a repo-relative path")
            built = {"kind": kind, "identifier": path, "path": path}
        else:
            built = {"kind": kind, "identifier": identifier.strip()}
            if path:
                built["path"] = path
        out.append(built)
    return out


def _build_one(item: Any, existing: Sequence[Dict[str, Any]], decision_id: Optional[str] = None) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise InvalidDecision("entry must be an object")
    choice = _optional_text(item, "choice")
    if not choice or not choice.strip():
        raise InvalidDecision("choice is required")
    decided_by = _optional_text(item, "decided_by") or "agent"
    if decided_by not in core_decisions.DECIDED_BY:
        raise InvalidDecision(f"decided_by must be one of {', '.join(core_decisions.DECIDED_BY)}")
    return core_decisions.build_decision(
        choice=choice,
        question=_optional_text(item, "question"),
        rationale=_optional_text(item, "rationale"),
        decided_by=decided_by,
        alternatives=_string_list(item, "alternatives"),
        follows_from=_string_list(item, "follows_from"),
        refines=_string_list(item, "refines"),
        supersedes=_string_list(item, "supersedes"),
        anchors=_anchors(item),
        decision_id=decision_id or _optional_text(item, "id"),
        existing=existing,
    )


def choice_key(choice: str) -> Optional[str]:
    """Short hash of the choice as core would store it; identifies an
    id-less decision across polls."""
    text = core_decisions.clip(choice, core_decisions.MAX_CHOICE)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else None


def _stable_auto_id(base: str, key: str, prior_auto: Dict[str, str], blocked: Set[str]) -> str:
    """An id for an id-less decision that doesn't depend on entry order: reuse
    the id this choice was sent under before; otherwise the first free
    `base`, `base-2`, ... never used by anything sent before."""
    family = [base] + [f"{base[:60]}-{n}" for n in range(2, 100)]
    for candidate in family:
        if prior_auto.get(candidate) == key and candidate not in blocked:
            return candidate
    for candidate in family:
        if candidate not in blocked and candidate not in prior_auto:
            return candidate
    return f"{base[:40]}-{key[:8]}"


def build_decisions_stable(
    structured_output: Any,
    *,
    prior_auto: Optional[Dict[str, str]] = None,
    prior_ids: Iterable[str] = (),
) -> Tuple[List[Dict[str, Any]], List[str], Optional[str], Dict[str, str]]:
    """(decisions, per-entry skip reasons, whole-output problem, auto ids).

    A later entry with the same explicit id revises the earlier one, the
    rule `cardinal-decision record --id` follows. Entries without an id get
    one from the choice; `prior_auto` ({id: choice_key} of id-less
    decisions already sent) keeps those ids stable when entries are
    reordered or inserted. The last value maps each derived id to its
    choice key."""
    if structured_output is None:
        return [], [], "no structured_output (session not created with the Cardinal decision schema?)", {}
    if not isinstance(structured_output, dict):
        return [], [], "structured_output is not an object", {}
    items = structured_output.get("decisions")
    if items is None:
        return [], [], "structured_output has no 'decisions' array", {}
    if not isinstance(items, list):
        return [], [], "structured_output.decisions is not an array", {}
    prior_auto = dict(prior_auto or {})
    blocked: Set[str] = {i for i in prior_ids if i not in prior_auto}  # explicit ids sent before
    for item in items[:MAX_DECISIONS]:
        raw_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(raw_id, str) and core_decisions.DECISION_ID_RE.match(raw_id.strip().lower()):
            blocked.add(raw_id.strip().lower())
    built: List[Dict[str, Any]] = []
    skipped: List[str] = []
    auto_keys: Dict[str, str] = {}
    for index, item in enumerate(items):
        if index >= MAX_DECISIONS:
            skipped.append(f"decisions[{index}:]: more than {MAX_DECISIONS} entries")
            break
        derived: Optional[str] = None
        key: Optional[str] = None
        if isinstance(item, dict) and item.get("id") is None and isinstance(item.get("choice"), str):
            key = choice_key(item["choice"])
            if key:
                base = core_decisions.slugify(core_decisions.clip(item["choice"], core_decisions.MAX_CHOICE) or "")
                derived = _stable_auto_id(base, key, prior_auto, blocked)
        try:
            decision = _build_one(item, built, decision_id=derived)
        except (InvalidDecision, core_decisions.DecisionError) as exc:
            skipped.append(f"decisions[{index}]: {exc}")
            continue
        blocked.add(decision["id"])
        if derived and key:
            auto_keys[decision["id"]] = key
        built = [d for d in built if d["id"] != decision["id"]] + [decision]
    return built, skipped, None, auto_keys


def build_decisions(
    structured_output: Any,
) -> Tuple[List[Dict[str, Any]], List[str], Optional[str]]:
    """(decisions, per-entry skip reasons, whole-output problem), with no
    prior decisions."""
    built, skipped, problem, _ = build_decisions_stable(structured_output)
    return built, skipped, problem


def decision_digest(decision: Dict[str, Any], pr_url: Optional[str]) -> str:
    """What makes a decision worth re-sending: its content, or the PR it is
    attached to. A later head sha alone does not re-send it."""
    return fingerprint({"decision": decision, "pr_url": pr_url})


def decision_attrs(session_id: str, decision: Dict[str, Any], facts: Dict[str, Any]) -> Dict[str, Any]:
    """No code clusters: they need the repo tree at HEAD, which the poller
    does not have."""
    return core_decisions.decision_attributes(
        session_id=session_id,
        decision=decision,
        repo=facts.get("cardinal_repo"),
        branch=facts.get("cardinal_branch"),
        head_sha=facts.get("cardinal_head_sha"),
        pr_number=facts.get("cardinal_pr_number"),
        pr_url=facts.get("cardinal_pr_url"),
    )


# --- usage (ACU) ------------------------------------------------------------


def usage_attrs(session_id: str, usage: Usage, facts: Dict[str, Any]) -> Dict[str, Any]:
    """Attributes for one cardinal.turn_usage record. cardinal_billing_unit
    distinguishes ACU rows from token-based ones. None/empty values are
    dropped by otlp.log_record; leaving them here keeps the shape stable."""
    attrs: Dict[str, Any] = {
        "session_id": session_id,
        "cardinal_billing_unit": "acu",
        "cardinal_acu_total": usage.acu_total,
        "cardinal_acu_cascade": usage.acu_cascade,
        "cardinal_acu_devin": usage.acu_devin,
        "cardinal_acu_review": usage.acu_review,
        "cardinal_acu_terminal": usage.acu_terminal,
        "period_start_ns": usage.period_start_ns,
        "period_end_ns": usage.period_end_ns,
    }
    for key in (
        "cardinal_repo", "cardinal_branch", "cardinal_pr_number", "cardinal_pr_url",
        "cardinal_initiative_name", "cardinal_initiative_type",
    ):
        if key in facts:
            attrs[key] = facts[key]
    return {k: v for k, v in attrs.items() if v is not None}


def usage_digest(usage: Usage, pr_url: Optional[str]) -> str:
    return fingerprint({
        "acu_total": usage.acu_total,
        "acu_cascade": usage.acu_cascade,
        "acu_devin": usage.acu_devin,
        "acu_review": usage.acu_review,
        "acu_terminal": usage.acu_terminal,
        "pr_url": pr_url,
    })


# --- OTLP -------------------------------------------------------------------


def resource(connection_state: Dict[str, Any], user_email: Optional[str], plugin_version: str) -> Dict[str, str]:
    return otlp.resource_attrs(
        service_name=SERVICE_NAME,
        agent_runtime=AGENT_RUNTIME,
        deployment_environment=connection_state.get("deployment_environment"),
        user_email=user_email,
        org=connection_state.get("org_slug") or connection_state.get("org_id"),
        plugin_version=plugin_version,
    )


def otlp_body(records: List[Dict[str, Any]], resource_attrs: Dict[str, str], plugin_version: str) -> Dict[str, Any]:
    """The same body cardinal_core.otlp.emit_records posts."""
    return {
        "resourceLogs": [{
            "resource": {"attributes": [otlp.kv(k, v) for k, v in resource_attrs.items()]},
            "scopeLogs": [{
                "scope": {"name": SCOPE_NAME, "version": plugin_version},
                "logRecords": records,
            }],
        }]
    }


def send_logs(
    records: List[Dict[str, Any]],
    connection: otlp.IngestConnection,
    resource_attrs: Dict[str, str],
    plugin_version: str,
    timeout: float = 10.0,
) -> None:
    """POST to /v1/logs. Unlike otlp.emit_records (best-effort, silent), a
    failure raises so the poller does not mark the records as sent."""
    body = otlp_body(records, resource_attrs, plugin_version)
    req = urllib.request.Request(
        connection.endpoint + "/v1/logs",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            **dict(connection.extra_headers),
            connection.api_header: connection.api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        exc.close()
        raise EmitError(f"ingest returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise EmitError(f"ingest unreachable: {exc}") from None
    if not 200 <= status < 300:
        raise EmitError(f"ingest returned HTTP {status}")

"""Codex-side decision capture: the sandbox handoff between the agent-run
CLI and the (unsandboxed) telemetry hook.

Why a handoff: Codex runs agent shell commands in the `workspace-write`
sandbox by default — network off, writable roots limited to cwd, /tmp and
$TMPDIR (openai/codex codex-rs/protocol/src/protocol.rs
SandboxPolicy::WorkspaceWrite / get_writable_roots_with_cwd) — so the CLI
can neither write ~/.codex/cardinal nor POST OTLP. Hooks are spawned by
Codex itself, outside the sandbox (codex-rs/hooks/src/engine/command_runner.rs
build_command).

So `cardinal-decision record` only validates and prints one marker line,
the same wire format the Cursor adapter uses:

    cardinal-decision-record:v1 {"v":1,"session":...,"cwd":...,"id":...,
      "choice":...,"question":...,"why":...,"alt":[...],"by":...,
      "anchor":[...],"follows":[...],"refines":[...],"supersedes":[...]}

and the telemetry hook's PostToolUse handler (tool_name "Bash",
tool_input.command, tool_response = command output; codex-rs/core/src/
tools/handlers/unified_exec.rs post_unified_exec_tool_use_payload) parses
it and calls `apply_spec`, which is the ONLY emitter: gate, re-validation,
final id against the ledger, ledger write + one `cardinal.decision` log.
`record --emit` (a human in their own terminal) calls `apply_spec`
directly and prints no marker, so the hook never emits it a second time.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Optional

from cardinal_core import decisions, otlp
from cardinal_core.initiative import canonical_repo, git
from cardinal_core.paths import AgentPaths, read_json

MARKER = "cardinal-decision-record:v1 "
MARKER_VERSION = 1
SPEC_KEYS = (
    "session", "cwd", "id", "choice", "question", "why", "alt", "by", "anchor",
    "follows", "refines", "supersedes",
)
LIST_KEYS = ("alt", "anchor", "follows", "refines", "supersedes")
MAX_ANCHOR_SPEC = 500
EMIT_TIMEOUT_SEC = 3.0
SCOPE_NAME = "cardinal-codex-plugin"


def codex_paths() -> AgentPaths:
    return AgentPaths(home=Path.home() / ".codex")


# --- marker ------------------------------------------------------------------


def normalize_spec(raw: Any) -> Optional[dict[str, Any]]:
    """Coerce a decoded marker to the spec shape; None when unusable."""
    if not isinstance(raw, dict) or raw.get("v", MARKER_VERSION) != MARKER_VERSION:
        return None
    spec: dict[str, Any] = {}
    for key in SPEC_KEYS:
        value = raw.get(key)
        if key in LIST_KEYS:
            spec[key] = [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []
        else:
            spec[key] = value if isinstance(value, str) and value else None
    if not spec["choice"] or not spec["cwd"]:
        return None
    spec["by"] = spec["by"] or "agent"
    return spec


def marker_from_decision(spec: dict[str, Any], decision: dict[str, Any], explicit_id: bool) -> str:
    """Cursor-compatible marker line from a validated decision: cleaned
    field values, `id` only when the agent chose one (the hook derives the
    final id against the ledger otherwise)."""

    def ids(relation: str) -> list[str]:
        return [link["to"] for link in decision["links"] if link["relation"] == relation]

    body = {
        "v": MARKER_VERSION,
        "session": spec.get("session"),
        "cwd": spec["cwd"],
        "id": decision["id"] if explicit_id else None,
        "choice": decision["choice"],
        "question": decision["question"],
        "why": decision["rationale"],
        "alt": decision["alternatives"],
        "by": decision["decided_by"],
        "anchor": spec["anchor"],
        "follows": ids("follows_from"),
        "refines": ids("refines"),
        "supersedes": ids("supersedes"),
    }
    return MARKER + json.dumps(body, separators=(",", ":"), ensure_ascii=True)


def extract_specs(text: str, seen: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """Specs from marker lines (line must start with the marker). `seen`
    dedupes identical markers that appear in more than one output field."""
    seen = set() if seen is None else seen
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(MARKER):
            continue
        raw = line[len(MARKER):]
        if raw in seen:
            continue
        seen.add(raw)
        try:
            spec = normalize_spec(json.loads(raw))
        except ValueError:
            continue
        if spec is not None:
            out.append(spec)
    return out


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


_SHELL_OPERATORS = frozenset({";", "&&", "||", "|", "|&", "&", "(", ")", ";;", "{", "}"})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_RE = re.compile(r"^python(\d+(\.\d+)*)?$")
_COMMAND_PREFIXES = frozenset({"exec", "command", "env", "nohup", "time"})
CLI_NAME = "cardinal-decision"


def invokes_record(command: str) -> bool:
    """True when the shell command itself runs `cardinal-decision record`:
    a command-position token (after an operator, env assignments, or a
    `python3 [-flags]` interpreter) whose basename is cardinal-decision,
    followed by `record`. A substring test is spoofable — the marker prefix
    contains "cardinal-decision", so `echo`/`grep`/`cat`/`printf` of marker
    lines would otherwise be recorded."""
    for line in command.splitlines():
        try:
            tokens = list(shlex.shlex(line, posix=True, punctuation_chars=True))
        except ValueError:
            continue
        at_command = True
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in _SHELL_OPERATORS:
                at_command = True
                i += 1
                continue
            if not at_command:
                i += 1
                continue
            if _ASSIGNMENT_RE.match(token) or token in _COMMAND_PREFIXES:
                i += 1
                continue
            j = i
            if _PYTHON_RE.match(os.path.basename(token)):
                j += 1
                while j < len(tokens) and tokens[j].startswith("-") and tokens[j] not in _SHELL_OPERATORS:
                    j += 1
            if (j + 1 < len(tokens) and os.path.basename(tokens[j]) == CLI_NAME
                    and tokens[j + 1] == "record"):
                return True
            at_command = False
            i += 1
    return False


def specs_from_post_tool_use(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Marker specs from a Codex PostToolUse payload for a Bash call whose
    command invoked `cardinal-decision record`. Anything else yields []."""
    if payload.get("tool_name") != "Bash":
        return []
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not invokes_record(command):
        return []
    seen: set[str] = set()
    return [spec for text in _strings(payload.get("tool_response")) for spec in extract_specs(text, seen)]


CAPTURE_OFF_REPORT = (
    "Cardinal decision capture is off, so this decision was not recorded. "
    "Only the user can turn it on (`cardinal-decision on` in their own terminal)."
)


def report_line(result: dict[str, Any]) -> str:
    """What the PostToolUse hook tells the agent after recording (same
    wording as the Cursor adapter)."""
    decision = result["decision"]
    tags = [f"PR #{result['pr_number']}"] if result["pr_number"] else []
    if result["clusters"]:
        tags.append("clusters " + ", ".join(result["clusters"]))
    detail = f" ({'; '.join(tags)})" if tags else ""
    lines = [f"Cardinal recorded decision {decision['id']}: {decision['choice']}{detail}"]
    if result["unknown_links"]:
        lines.append(
            f"Note: no earlier decision in this session has id {', '.join(result['unknown_links'])}; "
            "the link was kept as given."
        )
    if not result["connected"]:
        lines.append("Cardinal telemetry isn't connected, so it was only saved locally.")
    return "\n".join(lines)


# --- validation + application ----------------------------------------------


def _git_facts(cwd: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    repo_root = git(["rev-parse", "--show-toplevel"], cwd)
    if not repo_root:
        return None, None, None, None
    head_sha = git(["rev-parse", "HEAD"], cwd)
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    repo = canonical_repo(git(["remote", "get-url", "origin"], cwd))
    return repo_root, head_sha, branch, repo


def build_from_spec(
    spec: dict[str, Any], ledger: list[dict[str, Any]], repo_root: Optional[str],
) -> dict[str, Any]:
    """decisions.build_decision over a spec. Pure apart from path
    resolution; raises decisions.DecisionError on invalid input."""
    anchors = [decisions.parse_anchor(a, repo_root, spec["cwd"]) for a in spec["anchor"]]
    return decisions.build_decision(
        choice=spec["choice"],
        question=spec["question"],
        rationale=spec["why"],
        decided_by=spec["by"],
        alternatives=spec["alt"],
        follows_from=spec["follows"],
        refines=spec["refines"],
        supersedes=spec["supersedes"],
        anchors=anchors,
        decision_id=spec["id"],
        existing=ledger,
    )


def preview(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate with no side effects and no ~/.codex access (the sandbox
    may deny reads there too): anchors are parsed without a repo root and
    no ledger is consulted — the hook re-validates against both."""
    spec = dict(spec, anchor=[a[:MAX_ANCHOR_SPEC] for a in spec["anchor"]][: decisions.MAX_ANCHORS])
    return build_from_spec(spec, [], None)


def resource_attrs(paths: AgentPaths, plugin_version: str) -> dict[str, Any]:
    state = paths.read_state()
    return otlp.resource_attrs(
        service_name="codex",
        agent_runtime="codex",
        deployment_environment=state.get("deployment_environment"),
        user_email=state.get("user_email"),
        org=state.get("org_slug") or state.get("org_id"),
        plugin_version=plugin_version,
    )


PrLookup = Callable[[str, Optional[str], Optional[str]], "tuple[Optional[int], Optional[str]]"]


def bounded_code_clusters(
    anchors: list[dict[str, str]], repo_root: Optional[str], head_sha: Optional[str],
    cache: Path, timeout: float,
) -> tuple[list[str], Optional[str]]:
    """decisions.code_clusters with a caller-set `git ls-tree` budget
    (code_clusters itself uses load_domains' 5s default)."""
    paths = [(a["path"], a.get("kind") == "directory") for a in anchors if a.get("path") is not None]
    if not paths or not repo_root or not head_sha:
        return [], None
    loaded = decisions.load_domains(repo_root, head_sha, cache, timeout=timeout)
    if loaded is None:
        return [], None
    domains, scheme = loaded
    ids: list[str] = []
    for path, is_dir in paths:
        for cluster_id in decisions.match_clusters(domains, path, is_dir):
            if cluster_id not in ids:
                ids.append(cluster_id)
    return ids[: decisions.MAX_CLUSTERS], scheme


def apply_spec(
    paths: AgentPaths,
    spec: dict[str, Any],
    session_id: str,
    plugin_version: str,
    *,
    pr_lookup: Optional[PrLookup] = None,
    cluster_timeout: float = 5.0,
    emit_timeout: float = EMIT_TIMEOUT_SEC,
    now_ns: Callable[[], int] = time.time_ns,
) -> dict[str, Any]:
    """Build the decision, write the ledger, emit one cardinal.decision.
    Must run unsandboxed (hook, or a human's terminal). `pr_lookup`
    defaults to a live `gh` resolution; the hook passes a cache-only one."""
    cwd = spec["cwd"]
    repo_root, head_sha, branch, repo = _git_facts(cwd)
    ledger = decisions.read_ledger(paths.runtime_dir, session_id)
    decision = build_from_spec(spec, ledger, repo_root)
    cache = decisions.cache_dir(paths.runtime_dir)
    clusters, scheme = bounded_code_clusters(decision["anchors"], repo_root, head_sha, cache, cluster_timeout)
    if pr_lookup is None:
        pr_number, pr_url = decisions.resolve_pr(cwd, repo, branch, cache)
    else:
        pr_number, pr_url = pr_lookup(cwd, repo, branch)
    known = {entry["id"] for entry in ledger}
    unknown = [link["to"] for link in decision["links"] if link["to"] not in known]
    decisions.record_in_ledger(paths.runtime_dir, session_id, decision)

    connection = otlp.connection_from_paths(paths)
    if connection is not None:
        attrs = decisions.decision_attributes(
            session_id=session_id,
            decision=decision,
            code_clusters=clusters,
            cluster_scheme=scheme,
            repo=repo,
            branch=branch,
            head_sha=head_sha,
            pr_number=pr_number,
            pr_url=pr_url,
        )
        otlp.emit_records(
            [otlp.log_record(decisions.DECISION_EVENT, attrs, now_ns())],
            connection,
            resource_attrs(paths, plugin_version),
            scope_name=SCOPE_NAME,
            scope_version=plugin_version,
            timeout=emit_timeout,
        )
    return {
        "decision": decision,
        "pr_number": pr_number,
        "clusters": clusters,
        "unknown_links": unknown,
        "connected": connection is not None,
    }


# --- hook registration -------------------------------------------------------


def post_tool_use_registered(paths: AgentPaths) -> bool:
    """True when ~/.codex/hooks.json has the managed PostToolUse handler
    that performs the hook-side emission. Without it a marker would never
    be picked up, so the prompt must not ask the agent to record."""
    groups = read_json(paths.home / "hooks.json").get("hooks", {})
    groups = groups.get("PostToolUse") if isinstance(groups, dict) else None
    if not isinstance(groups, list):
        return False
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        for handler in handlers if isinstance(handlers, list) else []:
            command = str(handler.get("command") or "") if isinstance(handler, dict) else ""
            if "cardinal-codex-plugin" in command and "PostToolUse" in command:
                return True
    return False

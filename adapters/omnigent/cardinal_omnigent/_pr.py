"""PR linkage for ``cardinal.git_state`` without a local checkout.

The contract (docs/specs/adapter-parity.md §1) resolves the branch's PR
with ``cardinal_core.decisions.resolve_pr`` — ``gh pr view`` run in the
session's checkout. That is structurally impossible here: policies run
in the omnigent SERVER process and the event carries no cwd
(``FunctionPolicy._build_event``); the session workspace is a path on the
runner's host, where the host — not the server — runs git
(``omnigent/server/routes/_host_worktree.py``). Running ``gh`` in the
server's own cwd would attribute whatever repo the server happens to sit
in, so this module never shells out.

Two reliable signals instead, in precedence order:

1. Labels convention — ``cardinal.pr_url`` / ``cardinal.pr_number`` on the
   session labels, alongside ``cardinal.repo`` / ``cardinal.branch``.
2. Observed ``gh pr create`` — the agent's own shell tool_result. ``gh``
   prints the new PR's URL on success; the URL is only trusted when the
   originating command was ``gh pr create`` (a PR URL in ``gh pr list`` or
   ``cat`` output says nothing about THIS branch), and only when its
   ``owner/repo`` agrees with the ``cardinal.repo`` label if one is set.

``head_sha`` has no reliable signal (it moves on every commit and nothing
in the policy contract reports it) and is deliberately not emitted.
"""

from __future__ import annotations

import json
import re
from typing import Any

PR_URL_LABEL = "cardinal.pr_url"
PR_NUMBER_LABEL = "cardinal.pr_number"

# `gh pr create` anywhere in a (possibly compound) shell command.
_GH_PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b")

# https://<host>/<owner>/<repo>/pull/<n> — github.com and GHES alike.
_PR_URL_RE = re.compile(
    r"https://[A-Za-z0-9.-]+/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<number>[0-9]+)\b"
)

_OUTPUT_KEYS = ("result", "output", "stdout", "content", "text")


def is_pr_create(command: str) -> bool:
    return bool(command) and _GH_PR_CREATE_RE.search(command) is not None


def parse_pr_url(text: str) -> tuple[int, str, str] | None:
    """(number, url, 'owner/repo') for the LAST PR URL in ``text`` —
    ``gh pr create`` prints the URL as its final line."""
    if not isinstance(text, str) or not text:
        return None
    last = None
    for last in _PR_URL_RE.finditer(text):
        pass
    if last is None:
        return None
    number = int(last.group("number"))
    if number < 1:
        return None
    return number, last.group(0), last.group("repo")


def command_from_request_data(request_data: Any) -> str:
    """The shell command of the originating tool call. ``request_data``
    is ``{"name", "arguments"}`` (arguments a dict or a JSON string)."""
    if not isinstance(request_data, dict):
        return ""
    args = request_data.get("arguments")
    if args is None:
        args = request_data.get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return ""
    if not isinstance(args, dict):
        return ""
    cmd = args.get("command") or args.get("cmd")
    return cmd if isinstance(cmd, str) else ""


def output_text(data: Any, _depth: int = 0) -> str:
    """Best-effort tool output text from a tool_result payload: a bare
    string, or ``{"result"|"output"|"stdout"|"content"|"text": ...}``
    nested at most a couple of levels deep."""
    if isinstance(data, str):
        return data
    if _depth > 2:
        return ""
    if isinstance(data, dict):
        parts = [output_text(data.get(k), _depth + 1) for k in _OUTPUT_KEYS]
        return "\n".join(p for p in parts if p)
    if isinstance(data, list):
        parts = [output_text(item, _depth + 1) for item in data]
        return "\n".join(p for p in parts if p)
    return ""


def repo_matches(url_repo: str, label_repo: str | None) -> bool:
    if not label_repo:
        return True
    return url_repo.lower() == label_repo.strip().lower()


def from_labels(labels: dict[str, Any]) -> tuple[int | None, str | None]:
    """(number, url) from the labels convention; number is derived from
    the URL when only the URL is stamped. Labels are strings."""
    url = labels.get(PR_URL_LABEL)
    url = url if isinstance(url, str) and url else None
    raw = labels.get(PR_NUMBER_LABEL)
    number: int | None = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        number = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        number = int(raw.strip())
    if number is not None and number < 1:
        number = None
    if number is None and url:
        parsed = parse_pr_url(url)
        if parsed is not None:
            number = parsed[0]
    return number, url

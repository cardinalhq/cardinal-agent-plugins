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

Two signals instead, in precedence order:

1. Labels convention — ``cardinal.pr_url`` / ``cardinal.pr_number`` on the
   session labels, alongside ``cardinal.repo`` / ``cardinal.branch``.
2. Observed ``gh pr create`` — the agent's own shell tool_result. Every
   gate is deliberately conservative (a missed PR is better than a wrong
   one):

   - the command, tokenized shell-aware, must contain exactly one
     ``gh pr create`` invocation (optionally behind ``VAR=value`` / ``env``
     prefixes); every other ``&&``/``||``/``;`` segment must be a plain
     ``cd`` or ``git`` command. Pipes, redirects, subshells and command
     substitution are rejected — so ``grep "gh pr create"`` or
     ``gh pr create; gh pr view <url>`` never match;
   - the output must contain exactly ONE distinct PR URL;
   - the URL's ``owner/repo`` must equal a KNOWN repo: the
     ``cardinal.repo`` label, else a repo sniffed from this session's
     ``git push`` / ``git remote -v`` output. No known repo → ignored
     (``gh pr create --repo someone/fork`` is never trusted blind).

``head_sha`` has no reliable signal (it moves on every commit and nothing
in the policy contract reports it) and is deliberately not emitted.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

PR_URL_LABEL = "cardinal.pr_url"
PR_NUMBER_LABEL = "cardinal.pr_number"

# https://<host>/<owner>/<repo>/pull/<n> — github.com and GHES alike.
_PR_URL_RE = re.compile(
    r"https://[A-Za-z0-9.-]+/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<number>[0-9]+)\b"
)

_OUTPUT_KEYS = ("result", "output", "stdout", "content", "text")

_SEPARATORS = frozenset({"&&", "||", ";", "&"})
_PUNCTUATION = frozenset("();<>|&")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = frozenset({"bash", "sh", "zsh"})
_SHELL_C_FLAGS = frozenset({"-c", "-lc", "-ic", "-lic"})

# `git push` prints "To <remote-url>"; `git remote -v` prints
# "<name>\t<remote-url> (fetch|push)".
_PUSH_TO_RE = re.compile(r"^\s*To\s+(\S+)\s*$", re.M)
_REMOTE_V_RE = re.compile(r"^\S+\s+(\S+)\s+\((?:fetch|push)\)\s*$", re.M)
_REMOTE_URL_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://(?:[^@/\s]+@)?[^/\s]+/|(?:[^@/:\s]+@)?[^/:\s]+:)"
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


# --- command parsing ---------------------------------------------------------

def split_segments(command: str) -> list[list[str]] | None:
    """Shell-aware split into simple-command token lists on ``&&``,
    ``||``, ``;``, ``&`` and newlines. None (reject) on unbalanced quotes,
    pipes, redirects, subshells or command substitution — anything whose
    output provenance this parser cannot vouch for."""
    if not isinstance(command, str) or not command.strip():
        return None
    if "`" in command or "$(" in command:
        return None
    segments: list[list[str]] = []
    for line in command.splitlines():
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return None
        current: list[str] = []
        for token in tokens:
            if token in _SEPARATORS:
                if current:
                    segments.append(current)
                    current = []
                continue
            if token and all(ch in _PUNCTUATION for ch in token):
                return None
            current.append(token)
        if current:
            segments.append(current)
    return segments or None


def _strip_prefix(tokens: list[str]) -> list[str]:
    """Drop leading ``VAR=value`` assignments and a bare ``env``."""
    i = 0
    while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
        i += 1
    if i < len(tokens) and tokens[i] == "env":
        i += 1
        while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
            i += 1
    return tokens[i:]


def _is_create(tokens: list[str]) -> bool:
    return tokens[:3] == ["gh", "pr", "create"]


def _is_benign(tokens: list[str]) -> bool:
    return bool(tokens) and tokens[0] in ("cd", "git") and "gh" not in tokens


def is_pr_create(command: str) -> bool:
    """True when the command executes exactly one ``gh pr create`` and
    nothing else but ``cd`` / ``git`` segments."""
    segments = split_segments(command)
    if segments is None:
        return False
    creates = 0
    for segment in segments:
        tokens = _strip_prefix(segment)
        if _is_create(tokens):
            creates += 1
        elif not _is_benign(tokens):
            return False
    return creates == 1


def repo_from_git_output(command: str, output: str) -> str | None:
    """'owner/repo' (lowercased) from ``git push`` / ``git remote -v``
    output when the command runs one of those and nothing but ``cd`` /
    ``git`` / ``gh pr create`` segments, and exactly one distinct remote
    repo appears. None otherwise."""
    segments = split_segments(command)
    if segments is None or not isinstance(output, str) or not output:
        return None
    saw_remote_cmd = False
    for segment in segments:
        tokens = _strip_prefix(segment)
        if tokens[:2] in (["git", "push"], ["git", "remote"]):
            saw_remote_cmd = True
        elif not (_is_create(tokens) or _is_benign(tokens)):
            return None
    if not saw_remote_cmd:
        return None
    repos: set[str] = set()
    for regex in (_PUSH_TO_RE, _REMOTE_V_RE):
        for match in regex.finditer(output):
            url = _REMOTE_URL_RE.match(match.group(1))
            if url is not None:
                repos.add(url.group("repo").lower())
    return next(iter(repos)) if len(repos) == 1 else None


def command_from_request_data(request_data: Any) -> str:
    """The shell command of the originating tool call. ``request_data``
    is ``{"name", "arguments"}`` (arguments a dict or a JSON string). An
    argv list is accepted too (``["bash", "-lc", "<script>"]`` → the
    script; any other list → shell-joined) — omnigent passes harness tool
    arguments through verbatim, and some harnesses use argv form."""
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
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list) and cmd and all(isinstance(part, str) for part in cmd):
        if (len(cmd) == 3 and os.path.basename(cmd[0]) in _SHELLS
                and cmd[1] in _SHELL_C_FLAGS):
            return cmd[2]
        return shlex.join(cmd)
    return ""


# --- output parsing ----------------------------------------------------------

def parse_single_pr_url(text: str) -> tuple[int, str, str] | None:
    """(number, url, 'owner/repo') when ``text`` holds exactly one
    distinct PR URL; None for zero or several."""
    if not isinstance(text, str) or not text:
        return None
    found = {m.group(0): m for m in _PR_URL_RE.finditer(text)}
    if len(found) != 1:
        return None
    match = next(iter(found.values()))
    number = int(match.group("number"))
    if number < 1:
        return None
    return number, match.group(0), match.group("repo")


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


def repo_matches(url_repo: str, known_repo: str | None) -> bool:
    """Strict: an unknown repo never matches."""
    if not known_repo or not url_repo:
        return False
    return url_repo.lower() == known_repo.strip().lower()


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
        parsed = parse_single_pr_url(url)
        if parsed is not None:
            number = parsed[0]
    return number, url

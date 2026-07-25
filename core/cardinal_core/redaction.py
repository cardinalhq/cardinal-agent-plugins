"""Redaction & secret-scrubbing helpers — docs/privacy-redaction.md §4.

Every adapter hook builds attribute values (Bash commands, file paths,
tool arguments, tool output, git remote URLs) that MUST NOT reach the
ingest endpoint verbatim when they carry command/content-shaped or
credential-shaped data. This module is the single place that decision
lives: adapters call these helpers instead of hand-rolling their own
truncation/hashing, so the redaction discipline documented in the spec
cannot silently drift apart between Claude/Codex/Cursor/Gemini.

All helpers are pure and side-effect-free (spec §4 preamble) — no I/O, no
network, no imports beyond the stdlib and cardinal_core.bashclass.

Config knobs are read from the environment ONCE at import time
(`CARDINAL_REDACT_MODE`, `CARDINAL_REDACT_MAX_ATTR_BYTES`), matching every
other hook-scoped config read in this package — hooks are one process per
invocation, so "import time" and "process start" are the same moment. A
test that needs a different value reimports the module after setting the
env var.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from .bashclass import classify_bash_command

# ---------------------------------------------------------------------------
# Config knobs (spec §5)
# ---------------------------------------------------------------------------

_VALID_MODES = ("strict", "standard", "permissive")


def _read_mode() -> str:
    raw = (os.environ.get("CARDINAL_REDACT_MODE") or "standard").strip().lower()
    return raw if raw in _VALID_MODES else "standard"


def _read_max_attr_bytes() -> int:
    raw = os.environ.get("CARDINAL_REDACT_MAX_ATTR_BYTES")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return 4096


REDACT_MODE = _read_mode()
MAX_ATTR_BYTES = _read_max_attr_bytes()

# `permissive` is a local-debugging-only escape hatch (spec §5): it must
# never activate outside an explicit dev environment, regardless of
# CARDINAL_REDACT_MODE — a config flag alone is not sufficient gating.
# CARDINAL_ENV=dev is the explicit second signal required to arm it.
PERMISSIVE_ACTIVE = REDACT_MODE == "permissive" and os.environ.get("CARDINAL_ENV") == "dev"


# ---------------------------------------------------------------------------
# Secret patterns (spec §4 "Common secret regex list")
# ---------------------------------------------------------------------------

# (pattern_name, regex) — the name is the only thing ever reported on a
# match; the matched text itself is never returned by any helper below.
# AWS_SECRET_ACCESS_KEY is deliberately omitted (spec Open Questions #1):
# it has no fixed prefix, so a context-free regex either over-matches any
# 40-char token or under-matches. GENERIC_ENV_SECRET_ASSIGNMENT already
# catches the common `AWS_SECRET_ACCESS_KEY=...` env-assignment shape.
KNOWN_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("AWS_ACCESS_KEY_ID", r"\bAKIA[0-9A-Z]{16}\b"),
    # gh[pousr]_ covers classic (ghp_), OAuth (gho_), user-to-server (ghu_),
    # server-to-server (ghs_), and refresh (ghr_) tokens; github_pat_ is the
    # fine-grained two-segment form.
    ("GITHUB_PAT", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22}_[A-Za-z0-9]{59})\b"),
    ("SLACK_TOKEN", r"\bxox[abpsr]-[A-Za-z0-9-]+"),
    ("GENERIC_ENV_SECRET_ASSIGNMENT", r"\b[A-Z_][A-Z0-9_]*_(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S+"),
    ("JWT_LIKE", r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    ("PRIVATE_KEY_BLOCK", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("BEARER_TOKEN", r"[Bb]earer\s+[A-Za-z0-9._-]{20,}"),
    ("REMOTE_URL_USERINFO", r"://[^/@\s]+@"),
    ("NPM_TOKEN", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("PYPI_TOKEN", r"\bpypi-AgE[A-Za-z0-9_-]{50,}"),
    ("STRIPE_KEY", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{24,}\b"),
    ("GCP_API_KEY", r"\bAIza[A-Za-z0-9_-]{35}\b"),
)

_COMPILED_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pattern)) for name, pattern in KNOWN_SECRET_PATTERNS
)

# Shared with strip_url_userinfo below — same shape as the
# REMOTE_URL_USERINFO entry above, kept as its own compiled object so
# strip_url_userinfo doesn't depend on list ordering in
# KNOWN_SECRET_PATTERNS.
_USERINFO_RE = re.compile(r"://[^/@\s]+@")


# ---------------------------------------------------------------------------
# Core helpers (spec §4 signatures)
# ---------------------------------------------------------------------------

def hash_field(value: str | None, max_bytes: int = 4096) -> dict:
    """SHA-256 over the first `max_bytes` bytes of `value` (UTF-8 encoded).

    Returns {"hash": <hex sha256>, "length": <int, full byte length of
    value>, "truncated": <bool, True if value exceeded max_bytes>}.

    `length` is always the FULL length, not the hashed-prefix length —
    truncation must not hide how big the original value was.
    """
    text = value or ""
    data = text.encode("utf-8", "surrogateescape")
    truncated = len(data) > max_bytes
    digest = hashlib.sha256(data[:max_bytes]).hexdigest()
    return {"hash": digest, "length": len(data), "truncated": truncated}


def scrub_secrets(value: str | None) -> tuple[str, list[str]]:
    """Replace every substring matching a known secret pattern (§ secret
    regex list above) with a fixed placeholder ("<redacted:PATTERN_NAME>").

    Returns (cleaned_value, detected_pattern_names) — detected_pattern_names
    is the list of pattern names matched (possibly empty), always returned
    even when the caller intends to drop the field outright, so the caller
    can still emit a secret_detected signal.
    """
    if not value:
        return value or "", []
    cleaned = value
    detected: list[str] = []
    for name, pattern in _COMPILED_PATTERNS:
        cleaned, count = pattern.subn(f"<redacted:{name}>", cleaned)
        if count:
            detected.append(name)
    return cleaned, detected


def scrub_payload_recursively(value: Any) -> Any:
    """Walk a JSON-shaped value (nested dicts/lists) and run every string
    leaf through scrub_secrets, replacing detected secrets with the same
    `<redacted:PATTERN_NAME>` placeholder scrub_secrets uses elsewhere.
    Structure — dict keys, list order/length, non-string leaf types
    (int/float/bool/None) — is left untouched; only string VALUES change.

    Built for debug-payload capture (CARDINAL_*_DEBUG_PAYLOADS): unlike
    the mode-aware wrappers above, which hash/drop whole fields for the
    live telemetry wire, a raw session-shape dump needs to stay
    human-readable for fixture work while still being safe to commit —
    so this scrubs in place rather than hashing.
    """
    if isinstance(value, str):
        cleaned, _ = scrub_secrets(value)
        return cleaned
    if isinstance(value, dict):
        return {k: scrub_payload_recursively(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_payload_recursively(v) for v in value]
    return value


def redact_command(cmd: str | None) -> dict:
    """Bash/shell-specific redaction. Wraps classify_bash_command
    (cardinal_core.bashclass) + scrub_secrets + hash_field.

    Returns {"bash_class": <enum|None>, "bash_multi": <bool>,
    "command_hash": <hash_field() dict>, "secret_patterns": <list[str]>}.
    Never returns the command text itself in any field.
    """
    text = cmd or ""
    cleaned, patterns = scrub_secrets(text)
    classified = classify_bash_command(text)
    bash_class, bash_multi = classified if classified is not None else (None, False)
    # Hash the SCRUBBED text, not the raw command: hashing a value that
    # still contains a live secret would make the hash a 1:1-derivable
    # proxy for that secret (spec §2 — why secrets are dropped, not
    # hashed, when found inside another field).
    return {
        "bash_class": bash_class,
        "bash_multi": bash_multi,
        "command_hash": hash_field(cleaned, MAX_ATTR_BYTES),
        "secret_patterns": patterns,
    }


def redact_file_path(path: str | None, cwd: str | None) -> str | None:
    """Path-scoping redaction (spec §3 attributes.file_path).

    If `path` resolves (after normalization) inside `cwd`: return it
    relative to `cwd`, verbatim.
    If `path` resolves outside `cwd`, or `cwd` is unknown/unavailable:
    return a hashed placeholder, e.g. "outside-cwd:<sha256[:16]>" —
    stable across calls with the same path+cwd (so the same excluded file
    touched twice clusters as "same file", without revealing where it is).

    The placeholder is keyed on the fully-resolved ABSOLUTE path once one
    is known (either `path` was already absolute, or `cwd` made it so) —
    deliberately cwd-independent in that case, so the same external file
    (e.g. `~/.ssh/config`) clusters as "same file" across sessions/repos,
    not just within one. Only a still-relative path with no `cwd` to
    resolve against falls back to hashing the literal string as given.
    """
    if not path:
        return path
    norm_path = os.path.normpath(os.path.expanduser(str(path)))
    abs_path = norm_path
    try:
        if cwd:
            norm_cwd = os.path.normpath(os.path.expanduser(str(cwd)))
            abs_path = (
                norm_path
                if os.path.isabs(norm_path)
                else os.path.normpath(os.path.join(norm_cwd, norm_path))
            )
            rel = os.path.relpath(abs_path, norm_cwd)
            inside = (
                rel != os.pardir
                and not rel.startswith(os.pardir + os.sep)
                and not os.path.isabs(rel)
            )
            if inside:
                return rel
    except (ValueError, OSError):
        abs_path = norm_path
    key = abs_path if os.path.isabs(abs_path) else str(path)
    digest = hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return f"outside-cwd:{digest}"


def strip_url_userinfo(url: str | None) -> str | None:
    """Remove `user:pass@` / `user@` credential prefixes from a URL's
    authority component (spec §3, §7 git-state.py gap — origin URLs can
    embed `https://x-access-token:TOKEN@host/...` credentials).

    SCP-style git remotes with no `://` (`git@github.com:org/repo.git`)
    are left untouched: that `git@` is a fixed SSH login name, not a
    credential, and canonical_repo() depends on that exact shape.
    Malformed input (no match) passes through unchanged.
    """
    if not url:
        return url
    return _USERINFO_RE.sub("://", url)


# ---------------------------------------------------------------------------
# Mode-aware wrappers (spec §5)
# ---------------------------------------------------------------------------

def redact_prompt(value: str | None, max_bytes: int = 4096) -> dict | None:
    """Prompt-class fields (spec §2 "Prompts" / §3 attributes.prompt) are
    `never` verbatim in every mode; hash_field() is what `never` means in
    `standard`. `strict` upgrades this one step further — even the hash
    is dropped (returns None), so a leaked payload can't be correlated
    against a known-prompt hash database.

    No adapter emits a prompt field today (spec §3: "this rule exists so
    a future emitter doesn't regress it") — this wrapper is that guard.
    """
    if REDACT_MODE == "strict":
        return None
    return hash_field(value, max_bytes)


def permissive_verbatim(field_name: str, value: str | None, allowlist: frozenset[str]) -> str | None:
    """Verbatim emission for local-debugging only (spec §5 `permissive`).

    Returns `value` unchanged when permissive mode is active AND
    `field_name` is in the caller-supplied allowlist; otherwise returns
    None so the caller falls back to its normal (hashed/never) treatment.

    `PERMISSIVE_ACTIVE` already folds in the CARDINAL_ENV=dev check —
    permissive must never activate in prod even if
    CARDINAL_REDACT_MODE=permissive leaks into a prod config.
    """
    if PERMISSIVE_ACTIVE and field_name in allowlist:
        return value
    return None


# ---------------------------------------------------------------------------
# Adapter-facing convenience wrappers
#
# Codex/Cursor/Gemini's `tool_result`-style events all face the same
# shape of problem (spec §7: "the two events must not diverge in
# redaction discipline") — a raw tool-call argument dict and a raw
# output blob that must never cross the wire verbatim. These two
# helpers are the shared implementation so the three adapters can't
# independently drift on how "never verbatim" is enforced. Not part of
# the five §4 signatures; pure convenience over hash_field/scrub_secrets/
# redact_command above.
# ---------------------------------------------------------------------------

# Bash-shaped tool names across adapters (Claude/Codex normalize to
# "Bash"; Cursor/Gemini's normalize_tool functions do the same).
_BASH_ARG_KEYS = ("cmd", "command", "full_command")


def redact_tool_args(tool_name: str | None, args: dict[str, Any] | None) -> dict[str, Any]:
    """'Never verbatim' treatment for a tool call's raw argument dict
    (spec §3 "attributes.command" / "MCP tool call arguments"). Bash
    gets bash_class + command_hash via redact_command; every other tool
    (Edit/patch content, MCP args, arbitrary tool inputs) gets a single
    hash + length over the whole serialized argument dict — same
    treatment spec §3 prescribes for MCP args, generalized to any tool
    since none of them are safe to distinguish by content shape alone.

    Returns a flat dict of extra attributes to merge into the event,
    always including `secret_patterns` (possibly empty) so the caller
    can emit a secret_detected signal.
    """
    args = args or {}
    if tool_name == "Bash":
        cmd = ""
        for key in _BASH_ARG_KEYS:
            v = args.get(key)
            if isinstance(v, str) and v:
                cmd = v
                break
        redacted = redact_command(cmd)
        out: dict[str, Any] = {
            "bash_class": redacted["bash_class"],
            "command_hash": redacted["command_hash"]["hash"],
            "command_length": redacted["command_hash"]["length"],
            "secret_patterns": redacted["secret_patterns"],
        }
        if redacted["bash_multi"]:
            out["bash_multi"] = True
        if redacted["command_hash"]["truncated"]:
            out["command_truncated"] = True
        return out
    if not args:
        return {"secret_patterns": []}
    try:
        serialized = json.dumps(args, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        serialized = str(args)
    cleaned, patterns = scrub_secrets(serialized)
    hashed = hash_field(cleaned, MAX_ATTR_BYTES)
    out = {
        "args_hash": hashed["hash"],
        "args_length": hashed["length"],
        "secret_patterns": patterns,
    }
    if hashed["truncated"]:
        out["args_truncated"] = True
    return out


def redact_tool_output(output: Any) -> dict[str, Any]:
    """'Never verbatim' treatment for a tool call's raw result (stdout/
    stderr text, or a structured result blob) — spec §3 "Tool result
    stdout/stderr": length + hash only, never the content.

    Returns a flat dict of extra attributes (`output_hash`,
    `output_length`, optionally `output_truncated`), always including
    `secret_patterns` (possibly empty).
    """
    if output is None or output == "":
        return {"secret_patterns": []}
    if isinstance(output, str):
        text = output
    else:
        try:
            text = json.dumps(output, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(output)
    cleaned, patterns = scrub_secrets(text)
    hashed = hash_field(cleaned, MAX_ATTR_BYTES)
    out = {
        "output_hash": hashed["hash"],
        "output_length": hashed["length"],
        "secret_patterns": patterns,
    }
    if hashed["truncated"]:
        out["output_truncated"] = True
    return out

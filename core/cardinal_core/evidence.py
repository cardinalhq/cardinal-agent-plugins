"""Local evidence spool: captured MCP tool results, kept on this machine.

A storyboard cites evidence. Cardinal's own gateway mints a *witnessed*
receipt for every read-only call it serves; a call to any other MCP server
is invisible to it. A client hook (the Claude adapter's
hooks/evidence-capture.py) records such a result here instead: one JSON file
per call, under

    <root>/<session_id>/ev_<12 hex>.json      (root: ~/.cardinal/evidence)

Nothing here touches the network. Only a result the author later cites in a
storyboard is uploaded (as a *captured* receipt), by a separate, explicit
step (`cardinal-evidence promote`). Everything else ages out after
RETENTION_S.

What this module owns:
  - normalize(): Claude Code's PostToolUse tool_response for an MCP tool (a
    string, {content, structuredContent}, a list of content blocks, or the
    "Output has been saved to <file>" spill notice) -> the same
    {structured, text, other_blocks} body a gateway receipt stores as
    model_result.
  - scrub(): the credential scrub conductor's gateway applies to a receipt
    (packages/mcp-gateway/storyboard/receipts/receipts.go scrub and
    errors.go redactValueShapes), ported so a secret a tool echoes never
    reaches disk. Keep the two in step: tests/testdata/scrub_vectors.json
    holds vectors both must satisfy.
  - cap_body(): results over MAX_RESULT_BYTES become conductor's
    truncatedBody ({truncated, original_bytes, prefix}).
  - write_entry(): atomic write, directories 0700, files 0600.
  - gc(): opportunistic, time-bounded removal of entries past retention.
  - spill_path()/read_spill(): Claude Code's spill-file follower, shared
    with the storyboard preview hook.

No module-level path constants (spec §omnigent constraints): every function
that touches disk takes the spool root (default_root(home)) or an allowed
spill root as an argument.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = "cardinal.evidence.v1"
TIER = "captured"

RETENTION_S = 14 * 24 * 3600
# gc() runs at most once per GC_INTERVAL_S (stamp file) and for at most
# GC_BUDGET_S, so a hook that calls it on every tool call stays cheap.
GC_INTERVAL_S = 6 * 3600
GC_BUDGET_S = 0.25
# A temp file this old belongs to a write that died.
STALE_TMP_S = 3600

# Mirrors conductor's receipts.MaxModelResultBytes.
MAX_RESULT_BYTES = 256 << 10
MAX_ARGS_BYTES = 64 << 10
# A result whose serialization is larger than this is not decoded and
# scrubbed structurally (too slow for a 2 s hook): its leading
# MAX_RESULT_BYTES are scrubbed as plain text with the stricter error-text
# rules (key=value pairs included), which err toward redacting more.
MAX_STRUCTURAL_SCRUB_BYTES = 2 << 20
MAX_SPILL_BYTES = 64 * 1024 * 1024

DISABLED_FLAG = "disabled"
GC_STAMP = ".last-gc"

EVIDENCE_ID_RE = re.compile(r"^ev_[0-9a-f]{12}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
NO_SESSION = "no-session"


# ---------------------------------------------------------------------------
# Paths and opt-out
# ---------------------------------------------------------------------------

def default_root(home: Path) -> Path:
    """The spool root under a user's home directory: ~/.cardinal/evidence."""
    return Path(home) / ".cardinal" / "evidence"


def capture_disabled(root: Path, env: Optional[dict] = None) -> bool:
    """True when the user opted out: CARDINAL_EVIDENCE_CAPTURE=0 (or
    false/off/no), or the flag file <root>/disabled exists."""
    env = os.environ if env is None else env
    v = str(env.get("CARDINAL_EVIDENCE_CAPTURE", "")).strip().lower()
    if v in ("0", "false", "off", "no"):
        return True
    try:
        return (Path(root) / DISABLED_FLAG).exists()
    except OSError:
        return True


def session_dir_name(session_id: Any) -> str:
    if isinstance(session_id, str) and SESSION_ID_RE.match(session_id):
        return session_id
    return NO_SESSION


def split_mcp_tool(tool_name: Any) -> Optional[tuple]:
    """"mcp__<server>__<tool>" -> (server, tool); None for anything else.
    The server is the run up to the next "__" (a tool name may itself hold
    "__": mcp__plugin_cardinal_cardinal__storyboard__preview)."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return None
    rest = tool_name[len("mcp__"):]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


# ---------------------------------------------------------------------------
# Claude Code's spill notice
# ---------------------------------------------------------------------------

# Claude Code's notice for a result too large to keep inline, e.g.
# "Error: result (71,204 characters) exceeds maximum allowed tokens. Output
# has been saved to /Users/me/.claude/projects/<p>/<s>/tool-results/x.txt.\n..."
# The path runs to the end of its line (it may contain spaces, e.g. a HOME of
# "/Users/John Doe"), minus the sentence's closing period.
SPILL_RE = re.compile(r"Output has been saved to (.+?)\.?[ \t]*$", re.MULTILINE)


def spill_candidates(text: str) -> list:
    """Paths the spill notice may name: its whole line, then (for a notice that
    goes on after the path on the same line) each prefix ending before ". "."""
    if not isinstance(text, str):
        return []
    m = SPILL_RE.search(text)
    if not m:
        return []
    line = m.group(1).strip()
    out = [line]
    for i in range(len(line)):
        if line.startswith(". ", i) and line[:i] not in out:
            out.append(line[:i])
    return out[:8]


def spill_path(text: str, allowed_root: Path, max_bytes: int = MAX_SPILL_BYTES) -> Optional[Path]:
    """The file `text` (Claude Code's spill notice) names, resolved, when it
    is a regular file under `allowed_root` (for Claude Code,
    ~/.claude/projects) of at most max_bytes. A link that resolves outside
    the root, a relative or `..` path that escapes it, or a larger file is
    refused (None)."""
    for cand in spill_candidates(text):
        try:
            root = Path(allowed_root).resolve()
            path = Path(cand).expanduser()
            if not path.is_absolute():
                continue
            path = path.resolve()
            path.relative_to(root)
            if not path.is_file() or path.stat().st_size > max_bytes:
                continue
            return path
        except (OSError, ValueError, RuntimeError):
            continue
    return None


def read_spill(text: str, allowed_root: Path, max_bytes: int = MAX_SPILL_BYTES,
               read_limit: Optional[int] = None) -> Optional[str]:
    """The saved result, when `text` is Claude Code's spill notice and the file
    passes spill_path(). With read_limit, only the file's leading read_limit
    bytes are read (cut on a UTF-8 boundary)."""
    path = spill_path(text, allowed_root, max_bytes)
    if path is None:
        return None
    try:
        if read_limit is None:
            return path.read_text(encoding="utf-8")
        with open(path, "rb") as f:
            return f.read(read_limit).decode("utf-8", errors="ignore")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Credential scrub — a port of conductor's receipt scrub.
#   packages/mcp-gateway/storyboard/receipts/receipts.go  (scrub, isCredentialKey, ...)
#   packages/mcp-gateway/storyboard/receipts/errors.go    (redactValueShapes, RedactErrorText, ...)
# Change both together; tests/testdata/scrub_vectors.json pins the shared
# behaviour.
# ---------------------------------------------------------------------------

REDACTED = "[redacted]"
MAX_NESTED_JSON = 4

_CREDENTIAL_WORD = {
    "secret", "password", "passwd", "passphrase", "pwd", "pass",
    "cookie", "authorization", "authentication", "auth", "credential",
    "apikey", "bearer", "dsn", "jwt",
    "apitoken", "authtoken", "accesstoken", "connstr", "databaseurl", "secretkeybase",
}
_DIGEST_WORD = {"hash", "hashes", "digest", "salt"}
_DIGEST_OF = {"password", "passwd", "pwd", "passphrase", "token", "secret"}
_KEY_QUALIFIER = ("api", "access", "private", "secret", "session", "signing", "encryption", "ssh", "routing",
                  "integration", "master", "client", "app", "subscription", "account", "storage", "shared", "hmac",
                  "license")
_TOKEN_QUALIFIER = {"api", "access", "auth", "bearer", "refresh", "id", "session", "service"}
_NON_SECRET_TOKEN = {
    "next", "page", "continuation", "continue", "cursor", "pagination",
    "sync", "resume", "prev", "previous",
    "max", "input", "output", "prompt", "completion", "total",
    "eos", "bos", "pad", "unk", "sep", "cls", "mask", "special",
}
_ENCODING_SUFFIX = {"pem", "b64", "base64", "hex", "raw", "value"}
_CREDENTIAL_SUFFIX = ("connection string", "connection str", "conn string", "conn str", "database url", "db url",
                      "secret key base")
_REFERENCE_WORD = {"name", "ref", "type", "kind", "path", "optional", "mode"}

_ACRONYM_RUN = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Go's \s (and so RE2's) is ASCII [\t\n\f\r ]; spelled out to match exactly.
_WS = r"\t\n\f\r "
# Go's RE2 is linear; Python's re backtracks. Two of conductor's patterns
# (errors.go urlUserinfo and keySep) can start a match anywhere inside a long
# unbroken run and are quadratic on one under re, so they are matched here by
# scanning from their fixed anchor ("://", or the separator) instead:
# _url_userinfo_spans and _key_sep_matches. Their results equal the Go
# patterns' (FindAll, leftmost-first); tests/test_evidence.py fuzzes both
# against the literal Go patterns (GO_URL_USERINFO, GO_KEY_SEP).
GO_URL_USERINFO = r"([A-Za-z][A-Za-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"
GO_KEY_SEP = r"""(?:\\[nrt]|\\*["'])?([A-Za-z_][A-Za-z0-9_.\-]*)\\*["']?[ \t]*(?:=>|[:=])[ \t]*"""
_USERINFO_TAIL = re.compile(r"[^" + _WS + r"/:@]+:[^" + _WS + r"/@]+@")
_SEP = re.compile(r"=>|[:=]")
_SCHEME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-")
_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_KEY_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|token|apikey)([" + _WS + r"]+)([A-Za-z0-9._~+/=\-]{8,})", re.ASCII)
# Anchored by .match(s, pos) (a "^" would only match at the string's start).
_SCHEME_WORD = re.compile(r"(?i)(?:bearer|basic|token|apikey|digest|negotiate)[ \t]+")
_TOKEN_SHAPES = [re.compile(p, re.ASCII) for p in (
    r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*",                                   # JWT
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",                                                                   # GitHub
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",                                                                # Slack
    r"\bAKIA[0-9A-Z]{16}\b",                                                                           # AWS access key id
    r"\bsk-[A-Za-z0-9_\-]{16,}",                                                                       # sk- API keys
    r"\$2[abxy]?\$\d{1,2}\$[./A-Za-z0-9]{20,}",                                                        # bcrypt
    r"\$argon2(?:id|i|d)\$(?:v=\d+\$)?m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/.=\-]+(?:\$[A-Za-z0-9+/.=\-]+)?",  # argon2
    r"\$(?:1|5|6|md5)\$(?:rounds=\d+\$)?[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{20,}",                      # md5/sha-crypt
    r"\$g?y\$[./A-Za-z0-9]+\$[./A-Za-z0-9]+\$[./A-Za-z0-9]{20,}",                                      # yescrypt
    r"\$7\$[./A-Za-z0-9]+\$[./A-Za-z0-9]{20,}",                                                        # crypt scrypt
    r"\$scrypt\$[A-Za-z0-9+/.=,$\-]{20,}",                                                             # PHC scrypt
    r"\bpbkdf2_sha(?:1|256|512)\$\d+\$[^" + _WS + r"$]+\$[A-Za-z0-9+/=]{20,}",                         # Django
)]
_SK_HOST_NAME = re.compile(r"^sk(?:-[a-z0-9]+){2,}$")
_VALUE_STOP = " \t\r\n\"'&,;)}]<>"
_VALUE_ESCAPE_STOP = "\"'nrt"


# key_words looks at a key's last words only; a pathological key is cut to
# its tail first (the acronym split backtracks on a long capital run).
_MAX_KEY_CHARS = 256


def key_words(k: str) -> list:
    k = k[-_MAX_KEY_CHARS:]
    k = _ACRONYM_RUN.sub(r"\1 \2", k)
    k = _CAMEL_BOUNDARY.sub(r"\1 \2", k)
    return _NON_ALNUM.sub(" ", k.lower()).split()


def is_headers_key(k: str) -> bool:
    w = key_words(k)
    return bool(w) and w[-1] == "headers"


def is_reference_key(k: str) -> bool:
    w = key_words(k)
    return bool(w) and w[-1] in _REFERENCE_WORD


def is_credential_key(k: str) -> bool:
    words = key_words(k)
    if len(words) >= 2 and words[-1] in _ENCODING_SUFFIX:
        words = words[:-1]
    if not words:
        return False
    raw = words[-1]
    prev = words[-2] if len(words) >= 2 else ""
    joined = " " + " ".join(words)
    for suf in _CREDENTIAL_SUFFIX:
        if joined.endswith(" " + suf):
            return True
    last = raw[:-1] if raw.endswith("s") else raw
    prev_single = prev[:-1] if prev.endswith("s") else prev
    if (raw in _DIGEST_WORD or last in _DIGEST_WORD) and prev_single in _DIGEST_OF:
        return True
    if last == "token":
        if raw == "token":
            return prev not in _NON_SECRET_TOKEN
        return prev in _TOKEN_QUALIFIER
    if last == "key":
        return any(prev == q or (len(prev) > len(q) and prev.endswith(q)) for q in _KEY_QUALIFIER)
    return raw in _CREDENTIAL_WORD or last in _CREDENTIAL_WORD


def _nul(s: str) -> str:
    return s.replace("\x00", "\ufffd")


def scrub(v: Any) -> Any:
    """A copy of a decoded JSON value that is safe to persist (conductor
    receipts.go scrub): credential-keyed strings, header values and the
    value of a {name, value} credential pair become REDACTED; every other
    string leaf is scrubbed by value (scrub_string); NULs become U+FFFD."""
    return _scrub_at(v, 0)


def _scrub_at(v: Any, depth: int) -> Any:
    if isinstance(v, str):
        return scrub_string(v, depth)
    if isinstance(v, list):
        return [_scrub_at(e, depth) for e in v]
    if isinstance(v, dict):
        name = v.get("name")
        secret_pair = isinstance(name, str) and "value" in v and is_credential_key(name)
        out = {}
        for k, val in v.items():
            key = _nul(k) if isinstance(k, str) else k
            ks = k if isinstance(k, str) else str(k)
            if is_headers_key(ks):
                out[key] = _redact_all(val)
            elif is_credential_key(ks) or (secret_pair and ks == "value"):
                out[key] = _redact_secret(val, depth)
            else:
                out[key] = _scrub_at(val, depth)
        return out
    return v


def _reject_constant(name: str):
    raise ValueError("not JSON: " + name)


def _decode_json(t: str) -> Any:
    # Go's json.Valid rejects NaN/Infinity; so must this.
    return json.loads(t, parse_constant=_reject_constant)


def encode_json(v: Any) -> str:
    """Go's json.Encoder output with SetEscapeHTML(false): compact, map keys
    sorted, U+2028/U+2029 escaped. (Number literals may differ: Go keeps
    1.50 as written, Python writes 1.5.)"""
    s = json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return s.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def scrub_string(s: str, depth: int = 0) -> str:
    """One string leaf (conductor scrubString): a JSON document is scrubbed
    structurally and re-serialized only when that changed something; any
    other string has URL userinfo, Authorization-style credentials and
    well-known token shapes replaced."""
    s = _nul(s)
    t = s.strip()
    if depth < MAX_NESTED_JSON and t and t[0] in "{[":
        try:
            v = _decode_json(t)
        except (ValueError, RecursionError):
            v = None
        else:
            sv = _scrub_at(v, depth + 1)
            if sv != v:
                try:
                    return encode_json(sv)
                except (ValueError, TypeError, RecursionError):
                    return REDACTED
            return s
    return redact_value_shapes(s)


scrub_text = scrub_string


def _redact_secret(v: Any, depth: int) -> Any:
    if isinstance(v, str):
        return REDACTED
    if isinstance(v, list):
        return [_redact_secret(e, depth) for e in v]
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            ks = k if isinstance(k, str) else str(k)
            key = _nul(k) if isinstance(k, str) else k
            out[key] = _scrub_at(val, depth) if is_reference_key(ks) else _redact_secret(val, depth)
        return out
    return v


def _redact_all(v: Any) -> Any:
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            key = _nul(k) if isinstance(k, str) else k
            if k == "name" and isinstance(val, str):
                out[key] = _nul(val)
                continue
            out[key] = _redact_all(val)
        return out
    if isinstance(v, list):
        return [_redact_all(e) for e in v]
    if isinstance(v, str):
        return REDACTED
    return v


def _auth_credential(run: str) -> bool:
    return (len(run) >= 16 or any(c in run for c in "0123456789+/=_~")
            or any("A" <= c <= "Z" for c in run[1:]))


def _leaf_auth_credential(run: str) -> bool:
    return any(c in run for c in "0123456789+/") or run.endswith("=")


def _url_userinfo_spans(s: str) -> list:
    """[(start, end)) of the userinfo "user:pass@" of every GO_URL_USERINFO
    match, in order. The scheme is the id run before "://"; it needs a
    letter to start a match (the leftmost letter in that run starts it)."""
    spans = []
    i = s.find("://")
    while i >= 0:
        j = i
        letter = False
        while j > 0 and s[j - 1] in _SCHEME_CHARS:
            j -= 1
            if s[j].isalpha():
                letter = True
        m = _USERINFO_TAIL.match(s, i + 3) if letter else None
        if m:
            spans.append((i + 3, m.end()))
            i = s.find("://", m.end())
        else:
            i = s.find("://", i + 1)
    return spans


def _redact_url_and_scheme(s: str, credential) -> str:
    if "://" in s:
        spans = _url_userinfo_spans(s)
        if spans:
            out, last = [], 0
            for a, b in spans:
                out.append(s[last:a])
                out.append(REDACTED + "@")
                last = b
            out.append(s[last:])
            s = "".join(out)

    def repl(m):
        if not credential(m.group(3)):
            return m.group(0)
        return m.group(1) + m.group(2) + REDACTED

    return _AUTH_SCHEME.sub(repl, s)


def _redact_token_shapes(s: str) -> str:
    for rx in _TOKEN_SHAPES:
        s = rx.sub(lambda m: m.group(0) if _SK_HOST_NAME.match(m.group(0)) else REDACTED, s)
    return s


def redact_value_shapes(s: str) -> str:
    """conductor redactValueShapes: URL userinfo, an Authorization-style
    scheme + credential (the leaf rule: the run needs a digit, + or /, or
    trailing =), and self-identifying token / password-hash shapes."""
    return _redact_token_shapes(_redact_url_and_scheme(s, _leaf_auth_credential))


def _value_span(s: str, i: int) -> tuple:
    n = 0
    while i + n < len(s) and s[i + n] == "\\":
        n += 1
    if n > 0 and i + n < len(s) and s[i + n] in "\"'":
        q = s[i + n]
        vs = i + n + 1
        ve = vs
        while ve < len(s):
            c = s[ve]
            if c in "\n\r":
                return vs, ve
            if c == q:
                r = 0
                while ve - r - 1 >= vs and s[ve - r - 1] == "\\":
                    r += 1
                if r <= n:
                    return vs, ve - r
            ve += 1
        return vs, ve
    if i < len(s) and s[i] in "\"'":
        q = s[i]
        vs = i + 1
        ve = vs
        while ve < len(s):
            c = s[ve]
            if c == "\\" and ve + 1 < len(s) and s[ve + 1] != "\n":
                ve += 2
                continue
            if c == q or c in "\n\r":
                return vs, ve
            ve += 1
        return vs, min(ve, len(s))
    vs = ve = i
    while ve < len(s) and s[ve] not in _VALUE_STOP:
        if s[ve] == "\\" and ve + 1 < len(s) and s[ve + 1] in _VALUE_ESCAPE_STOP:
            break
        ve += 1
    return vs, ve


def _key_sep_matches(s: str):
    """(start, key, end) of every GO_KEY_SEP match (FindAll: leftmost-first,
    non-overlapping), found from each separator backwards: [ \\t]*, an
    optional quote and backslashes, then the key: the id run ending there,
    from its first letter or underscore. A key that starts its run may take
    the optional prefix: backslashes and a quote, or a backslash escape
    (\\n, \\r, \\t) whose letter is the run's first byte."""
    prev_end = 0
    for m in _SEP.finditer(s):
        j = m.start()
        k = j
        while k > 0 and s[k - 1] in " \t":
            k -= 1
        if k > 0 and s[k - 1] in "\"'":
            k -= 1
        while k > 0 and s[k - 1] == "\\":
            k -= 1
        run_end = k
        run_start = run_end
        while run_start > 0 and s[run_start - 1] in _KEY_CHARS:
            run_start -= 1
        key_start = run_start
        while key_start < run_end and s[key_start] not in _KEY_START:
            key_start += 1
        if key_start >= run_end:
            continue
        start = key_start
        if key_start == run_start and run_start > 0:
            c = s[run_start - 1]
            if c in "\"'":
                start = run_start - 1
                while start > 0 and s[start - 1] == "\\":
                    start -= 1
            elif (c == "\\" and s[run_start] in "nrt" and run_start + 1 < run_end
                  and s[run_start + 1] in _KEY_START):
                start = run_start - 1
                key_start = run_start + 1
        if start < prev_end:
            continue
        end = m.end()
        while end < len(s) and s[end] in " \t":
            end += 1
        prev_end = end
        yield start, s[key_start:run_end], end


def _redact_key_values(s: str) -> str:
    out = []
    last = 0
    for start, key, end in _key_sep_matches(s):
        if start < last or not is_credential_key(key):
            continue
        vs, ve = _value_span(s, end)
        if vs == end:
            sw = _SCHEME_WORD.match(s, vs)
            if sw:
                vs, ve = _value_span(s, sw.end())
        if ve == vs or s.startswith(REDACTED, vs):
            continue
        out.append(s[last:vs])
        out.append(REDACTED)
        last = ve
    if last == 0:
        return s
    out.append(s[last:])
    return "".join(out)


def redact_plain_text(text: str) -> str:
    """conductor redactPlainText (error text): the value-shape rules with the
    looser scheme test, plus the value of every key=value / key: value /
    "key": "value" pair whose key is a credential key. Used on a prefix that
    could not be scrubbed structurally."""
    s = _nul(text)
    s = _redact_url_and_scheme(s, _auth_credential)
    s = _redact_key_values(s)
    return _redact_token_shapes(s)


# ---------------------------------------------------------------------------
# tool_response -> model_result body
# ---------------------------------------------------------------------------

def _same_json(text: str, want: Any) -> bool:
    t = text.strip()
    if not t or t[0] not in "{[":
        return False
    try:
        return _decode_json(t) == want
    except (ValueError, RecursionError):
        return False


def normalize(tool_response: Any, spill_root: Optional[Path] = None) -> dict:
    """Claude Code's PostToolUse tool_response for an MCP tool -> an
    UNSCRUBBED {structured?, text?, other_blocks?, spilled?} body (the shape
    of conductor's model_result). Shapes: a string (the result text, or the
    spill notice, followed when the file resolves under spill_root);
    {content: str | [blocks], structuredContent?}; a list of content blocks;
    any other JSON value (kept as structured)."""
    body: dict = {}
    texts: list = []
    other = 0

    def add_text(t: str) -> None:
        if spill_root is not None and "Output has been saved to" in t:
            # Past MAX_STRUCTURAL_SCRUB_BYTES only a prefix is kept anyway
            # (scrub_and_cap), so a 64 MiB spill is not read whole.
            spilled = read_spill(t, spill_root, read_limit=MAX_STRUCTURAL_SCRUB_BYTES + 1)
            if spilled is not None:
                body["spilled"] = True
                texts.append(spilled)
                return
        texts.append(t)

    def add_blocks(blocks: list) -> None:
        nonlocal other
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                add_text(b["text"])
            elif isinstance(b, str):
                add_text(b)
            else:
                other += 1

    if isinstance(tool_response, str):
        add_text(tool_response)
    elif isinstance(tool_response, list):
        add_blocks(tool_response)
    elif isinstance(tool_response, dict) and ("content" in tool_response or "structuredContent" in tool_response):
        sc = tool_response.get("structuredContent")
        if sc is not None:
            body["structured"] = sc
        content = tool_response.get("content")
        if isinstance(content, str):
            add_text(content)
        elif isinstance(content, list):
            add_blocks(content)
    elif tool_response is not None:
        body["structured"] = tool_response
    if "structured" in body:
        texts = [t for t in texts if not _same_json(t, body["structured"])]
    if texts:
        body["text"] = texts
    if other:
        body["other_blocks"] = other
    return body


def _utf8_prefix(s: str, max_bytes: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max(0, max_bytes)].decode("utf-8", errors="ignore")


# A token-ish run cut at the end of a prefix is dropped (up to
# _MAX_TRAILING_RUN chars): a half token no longer matches its shape and
# would otherwise survive the scrub.
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~+/=$-")
_MAX_TRAILING_RUN = 4096


def _drop_trailing_run(s: str) -> str:
    i = len(s)
    stop = max(0, i - _MAX_TRAILING_RUN)
    while i > stop and s[i - 1] in _TOKEN_CHARS:
        i -= 1
    return s[:i]


def _truncated(original_bytes: int, prefix: str, max_bytes: int, scrubbed_as: Optional[str] = None) -> dict:
    """conductor truncatedBody, shrunk until its serialization fits."""
    budget = max_bytes - 128
    while True:
        p = _utf8_prefix(prefix, budget)
        env = {"truncated": True, "original_bytes": original_bytes, "prefix": p}
        if scrubbed_as:
            env["scrubbed_as"] = scrubbed_as
        n = len(encode_json(env).encode("utf-8"))
        if n <= max_bytes or budget <= 0:
            return env
        budget -= (n - max_bytes) + 16


def scrub_and_cap(value: Any, max_bytes: int = MAX_RESULT_BYTES,
                  structural_limit: int = MAX_STRUCTURAL_SCRUB_BYTES) -> tuple:
    """-> (stored value, truncated). Scrubs first, then caps, like a gateway
    receipt: a value that serializes to more than max_bytes becomes
    {truncated: true, original_bytes, prefix}. A value too large to scrub
    structurally in a hook's budget has only its leading bytes kept, scrubbed
    as plain text with the stricter error-text rules (scrubbed_as:
    "plain_text")."""
    try:
        raw = encode_json(value)
    except (ValueError, TypeError, RecursionError):
        return {"truncated": True, "original_bytes": 0, "prefix": ""}, True
    raw_len = len(raw.encode("utf-8"))
    if raw_len > structural_limit:
        head = _utf8_prefix(raw, max_bytes)
        head = _drop_trailing_run(head)
        return _truncated(raw_len, redact_plain_text(head), max_bytes, "plain_text"), True
    scrubbed = scrub(value)
    out = encode_json(scrubbed)
    if len(out.encode("utf-8")) <= max_bytes:
        return scrubbed, False
    return _truncated(len(out.encode("utf-8")), out, max_bytes), True


# ---------------------------------------------------------------------------
# Spool entries
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def evidence_id(server: str, tool: str, args: Any, called_at: str) -> str:
    """ev_ + the first 12 hex of sha256(server|tool|args|called_at), args as
    compact sorted-key JSON (the stored, scrubbed args, so the id can be
    recomputed from the entry)."""
    try:
        a = encode_json(args)
    except (ValueError, TypeError, RecursionError):
        a = "null"
    h = hashlib.sha256("|".join((server, tool, a, called_at)).encode("utf-8")).hexdigest()
    return "ev_" + h[:12]


def build_entry(*, server: str, tool: str, tool_name: str, tool_input: Any, tool_response: Any,
                session_id: Any, spill_root: Optional[Path] = None, called_at: Optional[str] = None,
                tool_use_id: Any = None, agent: str = "") -> dict:
    """A spool entry (scrubbed, capped) for one MCP call."""
    called_at = called_at or now_iso()
    args, args_truncated = scrub_and_cap(tool_input if tool_input is not None else {}, MAX_ARGS_BYTES)
    body = normalize(tool_response, spill_root)
    spilled = bool(body.pop("spilled", False))
    result, truncated = scrub_and_cap(body, MAX_RESULT_BYTES)
    entry = {
        "schema": SCHEMA,
        "evidence_id": evidence_id(server, tool, args, called_at),
        "tier": TIER,
        "session_id": session_id if isinstance(session_id, str) and SESSION_ID_RE.match(session_id) else None,
        "server": server,
        "tool": tool,
        "tool_name": tool_name,
        "called_at": called_at,
        "args": args,
        "result": result,
        "truncated": truncated,
    }
    if args_truncated:
        entry["args_truncated"] = True
    if spilled:
        entry["spilled"] = True
    if isinstance(tool_use_id, str) and len(tool_use_id) <= 256:
        entry["tool_use_id"] = tool_use_id
    if agent:
        entry["agent"] = agent
    return entry


def _ensure_private_dir(path: Path) -> None:
    """mkdir 0700 (or tighten an existing one we own); refuse a symlink."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, 0o700)
        st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(f"not a private directory: {path}")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def ensure_root(root: Path) -> None:
    """Create the spool root (and a missing parent, e.g. ~/.cardinal) 0700."""
    root = Path(root)
    if not root.parent.exists():
        os.makedirs(root.parent, mode=0o700, exist_ok=True)
    _ensure_private_dir(root)


def entry_path(root: Path, session_id: Any, ev_id: str) -> Path:
    if not EVIDENCE_ID_RE.match(ev_id):
        raise ValueError("bad evidence id")
    return Path(root) / session_dir_name(session_id) / f"{ev_id}.json"


def write_entry(root: Path, entry: dict) -> Path:
    """Atomically write entry to <root>/<session>/<evidence_id>.json:
    directories 0700, the file 0600 (mkstemp creates it 0600; os.replace
    swaps it in whole)."""
    root = Path(root)
    ensure_root(root)
    path = entry_path(root, entry.get("session_id"), entry["evidence_id"])
    _ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ev_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_entry(root: Path, ev_id: str) -> Optional[dict]:
    """Find an entry by id in any session directory."""
    if not isinstance(ev_id, str) or not EVIDENCE_ID_RE.match(ev_id):
        return None
    try:
        for sdir in Path(root).iterdir():
            p = sdir / f"{ev_id}.json"
            if sdir.is_dir() and not sdir.is_symlink() and p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------

def gc(root: Path, now: Optional[float] = None, retention_s: float = RETENTION_S,
       budget_s: float = GC_BUDGET_S, interval_s: float = GC_INTERVAL_S, force: bool = False) -> int:
    """Remove spool entries older than retention_s (by mtime), temp files of
    dead writes and emptied session directories. Opportunistic: at most once
    per interval_s (a stamp file under root) unless force, and stops after
    budget_s. Never raises. Returns the number of files removed."""
    now = time.time() if now is None else now
    deadline = time.monotonic() + budget_s
    root = Path(root)
    removed = 0
    try:
        st = os.lstat(root)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return 0
        stamp = root / GC_STAMP
        if not force:
            try:
                if now - stamp.stat().st_mtime < interval_s:
                    return 0
            except OSError:
                pass
        try:
            fd = os.open(str(stamp), os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
            os.utime(str(stamp), (now, now))
        except OSError:
            pass
        with os.scandir(root) as sessions:
            for sdir in sessions:
                if time.monotonic() > deadline:
                    break
                if not sdir.is_dir(follow_symlinks=False):
                    continue
                left = 0
                with os.scandir(sdir.path) as files:
                    for f in files:
                        if time.monotonic() > deadline:
                            left += 1
                            continue
                        try:
                            fst = f.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        name = f.name
                        old = now - fst.st_mtime
                        stale = ((name.startswith("ev_") and name.endswith(".json") and old > retention_s)
                                 or (name.startswith(".ev_") and name.endswith(".tmp") and old > STALE_TMP_S))
                        if stale and not stat.S_ISDIR(fst.st_mode):
                            try:
                                os.unlink(f.path)
                                removed += 1
                                continue
                            except OSError:
                                pass
                        left += 1
                if left == 0:
                    try:
                        os.rmdir(sdir.path)
                    except OSError:
                        pass
    except OSError:
        pass
    return removed

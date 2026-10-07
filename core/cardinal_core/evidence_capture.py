"""Generic evidence capture: ONE pipeline for every tool call.

Any tool call an agent makes (a built-in tool, any MCP server's tool, a tool
that does not exist yet) is recorded the same way, in the local spool
(cardinal_core.evidence, ~/.cardinal/evidence/<session>/ev_<12 hex>.json), so
a storyboard can later cite it. Nothing here touches the network: a result
leaves the machine only when the author promotes it
(cardinal_core.evidence_promote).

    capture_call(ToolCall)
      0  (adapter) budget guard: time_guard(), read_stdin_bounded()
      1  opt-out?  CARDINAL_EVIDENCE_CAPTURE=0 | <spool>/disabled  -> nothing
      2  dedupe:   source kind "cardinal" (Cardinal's own gateway already
                   minted a witnessed receipt)                      -> nothing
         control plane: any input string runs `cardinal-storyboard
                   investigation …` (the investigation's control log is
                   never evidence)                                  -> nothing,
                   unless it is one statement of a larger shell command
                   that can be kept without it (salvage_control_plane)
      3  sensitivity gate over every key and string of tool_input
         (cardinal_core.evidence_gate; tool-neutral)          -> withheld stub
      4  normalize: the first matching pluggable normalizer, else generic
         (cardinal_core.evidence_normalizers; shape only, never a gate)
      5  redact every field: the gateway's scrub (evidence.scrub) plus
         plain-text key=value rules, sensitive-path lines, base64 blobs and
         local paths (Claude's session temp dir -> [session tmp], spill
         root -> [local file], the dash-encoded cwd/$HOME -> [cwd]/[home],
         cwd -> ".", $HOME -> "~"); a summary is scrubbed before it is clipped
      6  cap: args <= 64 KiB, result <= 256 KiB, else conductor's
         {truncated, original_bytes, prefix} envelope
      7  write (atomic, 0600 in a 0700 dir) + gc (TTL, 256 MiB, 10k/session)

Steps 1-3 are the whole capture decision; none of them names a tool.

Entry schema: cardinal.evidence.v2 (see SCHEMA_V2 below and
docs/specs/generic-evidence-capture.md). v1 entries stay readable.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import evidence
from . import evidence_gate as gate
from .evidence_normalizers import MAX_SUMMARY_CHARS, Normalized, ToolCall, normalize_call

SCHEMA_V2 = "cardinal.evidence.v2"

# Budget: under the 2 s a Claude Code hook gets, leaving room for
# interpreter start-up and, when the pipeline runs out of time, the withheld
# stub capture_call_guarded writes instead (FALLBACK_BUDGET_S).
TIME_BUDGET_S = 1.4
MAX_STDIN_BYTES = 32 << 20
MAX_TOOL_NAME = 256
HINTED = ".hinted"
BASE64_MIN = 4096
LOCAL_FILE = evidence.LOCAL_FILE

CONTEXT_ENV = "CARDINAL_EVIDENCE_CONTEXT"

SOURCE_MCP = "mcp"
SOURCE_BUILTIN = "builtin"
SOURCE_TOOL = "tool"
SOURCE_CARDINAL = "cardinal"

# The characters maestro accepts in an identifier (evidence-upload.ts ident /
# gateway external.go checkIdent): ^[A-Za-z0-9_.:/@-]+(?: [A-Za-z0-9_.:/@-]+)*$
IDENT_RE = re.compile(r"^[A-Za-z0-9_.:/@-]+(?: [A-Za-z0-9_.:/@-]+)*$")
_IDENT_BAD = re.compile(r"[^A-Za-z0-9_.:/@ -]")
_RUNTIME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def mcp_source(server: str, runtime: str) -> dict:
    return {"kind": SOURCE_MCP, "server": server, "runtime": runtime}


def builtin_source(runtime: str) -> dict:
    return {"kind": SOURCE_BUILTIN, "runtime": runtime}


def classify_mcp_name(tool_name: Any, runtime: str, cardinal_servers=()) -> tuple:
    """(source, short tool) for a runtime that names MCP tools
    mcp__<server>__<tool> (Claude Code, Cursor, Codex): an MCP source for
    such a name (kind "cardinal" for Cardinal's own gateway), else a
    built-in source with the whole name as the tool."""
    name = tool_name if isinstance(tool_name, str) else ""
    parts = evidence.split_mcp_tool(name)
    if parts is not None:
        server, tool = parts
        if server in cardinal_servers:
            return {"kind": SOURCE_CARDINAL, "server": server, "runtime": runtime}, tool
        return mcp_source(server, runtime), tool
    return builtin_source(runtime), name


def ident(s: Any, max_len: int, empty: str) -> str:
    """s as a maestro identifier: characters outside [A-Za-z0-9_.:/@-] ->
    "_", runs of spaces -> one, trimmed, at most max_len characters."""
    t = _IDENT_BAD.sub("_", s if isinstance(s, str) else "")
    t = re.sub(r" +", " ", t).strip()[:max_len].strip()
    return t if t and IDENT_RE.match(t) else empty


def wire_source_server(source: Any, fallback: Any = None) -> str:
    """The upload's source_server for an entry's source: the MCP server
    (":" -> "_", so the builtin:/tool: namespaces stay the plugin's own),
    "builtin:<runtime>", or "tool:<runtime>"."""
    if isinstance(source, dict):
        kind = source.get("kind")
        rt = ident(source.get("runtime"), 60, "agent")
        if kind == SOURCE_BUILTIN:
            return "builtin:" + rt
        if kind == SOURCE_TOOL:
            return "tool:" + rt
        if kind == SOURCE_MCP:
            return ident(str(source.get("server") or "").replace(":", "_"), 128, "unknown_server")
    return ident(fallback, 128, "unknown_server")


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------

def evidence_id_v2(runtime: str, session_id: Any, tool_use_id: Any) -> Optional[str]:
    """ev_ + sha256("cardinal.evidence.v2|runtime|session|tool_use_id")[:12]
    when the runtime gives a tool-use id (deterministic: a re-fired hook
    rewrites the same file, and a JS adapter computes the same id;
    core/tests/testdata/evidence_id_vectors.json pins both). None
    otherwise."""
    if not isinstance(tool_use_id, str) or not tool_use_id or len(tool_use_id) > 256:
        return None
    s = "|".join((SCHEMA_V2, runtime or "", evidence.session_dir_name(session_id), tool_use_id))
    return "ev_" + hashlib.sha256(s.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]


def _random_id(server: str, tool: str, called_at: str) -> str:
    s = "|".join((SCHEMA_V2, server, tool, called_at, os.urandom(8).hex()))
    return "ev_" + hashlib.sha256(s.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Step 5: redaction beyond the gateway's leaf rule
# ---------------------------------------------------------------------------

_B64_RUN = re.compile(r"[A-Za-z0-9+/=_\-\r\n]+")
_DATA_URL = re.compile(r"data:[^,;\s]{0,100}(?:;[^,;\s]{0,100}){0,4};base64,", re.IGNORECASE)


# Claude Code's per-session temp dir: <TMPDIR>/claude-<uid>/<cwd with every
# character outside [A-Za-z0-9] as "-">/... (e.g.
# /private/tmp/claude-501/-Users-alice-git-app/<session uuid>/scratchpad).
# The encoded segment names the user; a shape rule, not a list of TMPDIRs.
_SESSION_TMP = re.compile(r"(?<=/)(claude-\d+/)-[A-Za-z0-9._-]+(?![A-Za-z0-9._-])")
SESSION_TMP = "[session tmp]"
# An encoded path (see _SESSION_TMP) shorter than this is too generic to
# rewrite ("-root" would hit "--root-dir").
MIN_ENCODED = 6
_ENCODED_LEFT = r"(?:^|(?<=[/\s\"'=`]))"
_ENCODED_RIGHT = r"(?=[-/\u2026]|$|[^\w])"


def encode_path(p: str) -> str:
    """A path as Claude Code encodes it into a directory name: every
    character outside [A-Za-z0-9] becomes "-" (/Users/alice -> -Users-alice)."""
    return re.sub(r"[^A-Za-z0-9]", "-", p.rstrip("/"))


class _Local:
    """Local path rewrites, in this order: Claude's session temp dir ->
    claude-<uid>/[session tmp]; spill root -> [local file] (whole path, so
    the encoded project dir, session id and file name under it all go);
    the dash-encoded cwd/home (-Users-alice-app) -> [cwd]/[home], longest
    first; cwd -> ".", home -> "~". The spill rule must precede the encoded
    rules: a spill path holds the encoded cwd, and rewriting that first
    would end the spill match at the "]" of "[cwd]"."""

    def __init__(self, spill_root: Optional[str], cwd: Optional[str], home: Optional[str]):
        self.rules = [("claude-", _SESSION_TMP, lambda m: m.group(1) + SESSION_TMP)]
        roots = set()
        if spill_root:
            roots.add(str(spill_root))
            try:
                roots.add(str(Path(spill_root).resolve()))
            except (OSError, RuntimeError):
                pass
        for r in sorted(roots, key=len, reverse=True):
            if len(r) > 1:
                self.rules.append((r, re.compile(re.escape(r) + r"(?:/[^\s\]\"'`)]*)?"), LOCAL_FILE))
        encoded = {}
        for p, repl in ((cwd, "[cwd]"), (home, "[home]")):
            if not isinstance(p, str) or not p.startswith("/"):
                continue
            paths = {p}
            try:
                paths.add(os.path.realpath(p))
            except (OSError, ValueError):
                pass
            for q in paths:
                enc = encode_path(q)
                if len(enc) >= MIN_ENCODED and enc not in encoded:
                    encoded[enc] = repl
        for enc in sorted(encoded, key=len, reverse=True):
            rx = re.compile(_ENCODED_LEFT + re.escape(enc) + _ENCODED_RIGHT)
            self.rules.append((enc, rx, encoded[enc]))
        for p, repl in ((cwd, "."), (home, "~")):
            if isinstance(p, str) and len(p.rstrip("/")) > 1 and p.startswith("/"):
                q = p.rstrip("/")
                self.rules.append((q, re.compile(re.escape(q) + r"(?=/|$|[\s\"'`:;,)\]}])"), repl))

    def apply(self, s: str) -> str:
        for needle, rx, repl in self.rules:
            if needle in s:
                s = rx.sub(repl, s)
        return s


_LINE_HEAD = re.compile(r"^([^\s:]{1,1024})")
_MAX_HEAD_CUTS = 16


def _sensitive(p: str, home: Optional[str], cwd: Optional[str]) -> bool:
    q = gate._norm(p, cwd, home)
    return bool(q and gate.full_path_rule(q, home))


def _path_lines(s: str, home: Optional[str], cwd: Optional[str]) -> str:
    """grep/rg output naming a sensitive file keeps only `<path>:[withheld]`
    for that file's lines: `path:line:text` and `path:text` match lines,
    `path-line-text` / `path-text` context lines, and rg's --heading form
    (a line that is only the path, then its lines up to a blank line)."""
    if ":" not in s and "-" not in s and "/" not in s and "." not in s:
        return s
    out = []
    in_block = False
    seen: dict = {}

    def sensitive(p: str) -> bool:
        v = seen.get(p)
        if v is None:
            if len(seen) > 4096:
                seen.clear()
            v = seen[p] = _sensitive(p, home, cwd)
        return v

    for line in s.split("\n"):
        if in_block:
            if line.strip() == "":
                in_block = False
                out.append(line)
            else:
                out.append("[withheld]")
            continue
        m = _LINE_HEAD.match(line)
        if m and (":" in line or "-" in m.group(1)):
            head = m.group(1)
            cut = None
            if len(head) < len(line) and line[len(head)] == ":" and not line.startswith("//", len(head) + 1) \
                    and sensitive(head):
                cut = len(head)
            else:
                n = 0
                for i, c in enumerate(head):
                    if c == "-" and i > 0:
                        n += 1
                        if n > _MAX_HEAD_CUTS:
                            break
                        if sensitive(head[:i]):
                            cut = i
                            break
            if cut is not None:
                out.append(line[:cut] + line[cut] + "[withheld]")
                continue
        stripped = line.strip()
        if stripped and " " not in stripped and sensitive(stripped):
            # rg --heading: the path alone, then its lines. The heading
            # itself is kept only when it has no separator in it.
            out.append(line if ":" not in stripped else "[withheld]")
            in_block = True
            continue
        out.append(line)
    return "\n".join(out)


def _is_base64_blob(s: str) -> bool:
    """A string of at least BASE64_MIN characters that is a base64 (or
    base64url) run, or a data: URL of one. Encoded bytes use the whole
    alphabet (upper and lower case, digits and "+/" or "-_"), which a hex
    listing or a run of one letter does not."""
    if len(s) < BASE64_MIN:
        return False
    m = _DATA_URL.match(s)
    body = s[m.end():] if m else s
    if len(body) < BASE64_MIN or _B64_RUN.fullmatch(body) is None:
        return False
    if m:
        return True
    sample = body[:BASE64_MIN]
    return (any(c.isupper() for c in sample) and any(c.islower() for c in sample)
            and any(c.isdigit() for c in sample) and any(c in "+/-_" for c in sample))


_YAML_NAME_VALUE = re.compile(
    r"(?m)^([ \t]*-?[ \t]*)name:[ \t]*[\"']?([A-Za-z_][A-Za-z0-9_.\-]{0,255})[\"']?[ \t]*\r?\n"
    r"([ \t]*)value:[ \t]*(?!\[redacted\])(\S[^\n]*)$")


def redact_text_wide(s: str) -> str:
    """The plugin's stricter text pass on top of the gateway's rules: a
    key=value / key: value pair whose key is secret-ish in the wider sense
    (OPENAI_KEY=..., tls.key: ...; gate.secretish_name) and a YAML
    `- name: DB_PASSWORD` / `value: ...` pair (kubectl -o yaml env)."""
    s = evidence._redact_key_values(s, gate.secretish_name)
    if "value:" in s and "name:" in s:
        s = _YAML_NAME_VALUE.sub(
            lambda m: (m.group(0)[:m.start(4) - m.start(0)] + evidence.REDACTED)
            if gate.secretish_name(m.group(2)) else m.group(0), s)
    return s


def _looks_json(s: str) -> bool:
    t = s.lstrip()
    return bool(t) and t[0] in "{["


class _Hardener:
    """Walks a value in the order encode_json serializes it (sorted keys) and
    applies the stricter text rules to every string leaf. Past `budget`
    characters of output the rest of the value cannot fit in the capped
    prefix, so later strings are dropped (and the caller marks the value
    truncated)."""

    def __init__(self, budget: int, local: _Local, home: Optional[str], cwd: Optional[str]):
        self.left = budget
        self.local = local
        self.home = home
        self.cwd = cwd
        self.cut = False

    def string(self, s: str, depth: int = 0) -> str:
        if self.left <= 0:
            self.cut = True
            return ""
        s = evidence._nul(s)
        decoded = None
        if (not _is_base64_blob(s) and depth < evidence.MAX_NESTED_JSON and _looks_json(s)
                and len(s) <= evidence.MAX_STRUCTURAL_SCRUB_BYTES):
            decoded = evidence._json_container(s)
        if _is_base64_blob(s):
            out = f"[binary omitted: {len(s)} bytes]"
        elif decoded is not None:
            # A JSON document in a string: its own leaves get the same rules;
            # evidence.scrub_string scrubs it structurally afterwards.
            # The text is kept as written unless a rule changed a leaf.
            try:
                hardened = self.walk(decoded, depth + 1)
                out = self.local.apply(s) if hardened == decoded else evidence.encode_json(hardened)
            except (ValueError, TypeError):
                out = evidence.redact_json_text(s)
            self.left -= 2
            return out
        else:
            if len(s) > self.left + 1024:
                s = evidence._drop_trailing_run(s[: self.left + 1024])
                self.cut = True
            if _looks_json(s):
                out = evidence.redact_json_text(s)
            else:
                out = redact_text_wide(evidence.redact_plain_text(_path_lines(s, self.home, self.cwd)))
            out = self.local.apply(out)
        self.left -= len(out) + 2
        return out

    def walk(self, v: Any, depth: int = 0) -> Any:
        if isinstance(v, str):
            return self.string(v, depth)
        # Past the budget nothing more can reach the stored prefix: the rest
        # of a container is dropped (cut), not walked, so the scrub after
        # this pass only sees what can be kept. (A 4 MiB pod list used to
        # be walked and scrubbed whole: over a second per call.)
        if isinstance(v, list):
            out_l = []
            for e in v:
                if self.left <= 0:
                    self.cut = True
                    break
                out_l.append(self.walk(e, depth))
            return out_l
        if isinstance(v, dict):
            try:
                items = sorted(v.items(), key=lambda kv: kv[0])
            except TypeError:
                items = sorted(v.items(), key=lambda kv: str(kv[0]))
            out = {}
            # {"name": "OPENAI_KEY", "value": "..."} (k8s env, ECS, docker)
            nv_secret = isinstance(v.get("name"), str) and gate.secretish_name(v["name"]) and "value" in v
            for k, e in items:
                if self.left <= 0:
                    self.cut = True
                    break
                if isinstance(k, str):
                    self.left -= len(k) + 3
                if ((isinstance(k, str) and gate.secretish_name(k)) or (nv_secret and k == "value")) \
                        and isinstance(e, (str, int, float)) and not isinstance(e, bool) and e != "":
                    out[k] = evidence.REDACTED
                    self.left -= len(evidence.REDACTED)
                    continue
                out[k] = self.walk(e, depth)
            return out
        if v is not None and not isinstance(v, (bool, int, float)):
            return str(v)
        self.left -= 8
        return v


def redact_and_cap(value: Any, max_bytes: int, local: _Local, home: Optional[str], cwd: Optional[str]) -> tuple:
    """-> (stored value, truncated). The hardening pass, then the gateway's
    scrub and cap (evidence.scrub_and_cap). A value whose tail had to be
    dropped to stay in budget is always stored as the truncation envelope,
    with the original size."""
    try:
        raw_len = len(evidence.encode_json(value).encode("utf-8", errors="surrogatepass"))
    except (ValueError, TypeError, RecursionError):
        raw_len = 0
    h = _Hardener(max_bytes + (max_bytes >> 2), local, home, cwd)
    try:
        hardened = h.walk(value)
    except RecursionError:
        return {"truncated": True, "original_bytes": raw_len, "prefix": ""}, True
    stored, truncated = evidence.scrub_and_cap(hardened, max_bytes)
    if not h.cut:
        return stored, truncated
    if truncated and isinstance(stored, dict):
        stored["original_bytes"] = max(raw_len, int(stored.get("original_bytes") or 0))
        return stored, True
    try:
        text = evidence.encode_json(stored)
    except (ValueError, TypeError, RecursionError):
        text = ""
    return evidence._truncated(raw_len, text, max_bytes), True


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Captured:
    entry: dict
    line: Optional[str]    # the context line for the agent, or None
    withheld: bool = False


# The run of characters a path segment or word can end in (see _clip_scrubbed).
_PARTIAL_TAIL = re.compile(r"[^\s/\\:;,=\"'`()\[\]{}<>|&]+$")


def _clip_scrubbed(s: Any, n: int, local: _Local) -> Optional[str]:
    """s scrubbed, THEN clipped to one line of at most n characters. This is
    the only clip: a cut made before the scrub can land inside a path
    (/Users/mgr…) that the local rules no longer recognise. The scrub sees
    at most n*4 characters; when s is longer (or was already clipped
    upstream, ending in "…"), its partial last segment is dropped first for
    the same reason, and the result ends in "…"."""
    if not isinstance(s, str) or not s:
        return None
    raw = evidence._nul(s[: n * 4])
    cut = len(s) > n * 4 or (len(s) == n * 4 and raw.endswith("…"))
    if cut:
        # A single run with no separator at all is kept (it holds no path).
        raw = _PARTIAL_TAIL.sub("", raw.rstrip("…")).rstrip() or raw
    t = local.apply(redact_text_wide(evidence.redact_plain_text(raw)))
    t = " ".join(t.split())
    if len(t) > n:
        return t[: n - 1] + "…"
    if cut:
        return (t[: n - 1] if len(t) >= n else t) + "…"
    return t or None


def _base_record(call: ToolCall, local: _Local, called_at: str) -> dict:
    source = dict(call.source) if isinstance(call.source, dict) else builtin_source(call.runtime)
    source["runtime"] = call.runtime
    if source.get("kind") == SOURCE_MCP:
        source["server"] = _clip_scrubbed(source.get("server"), 128, local) or "unknown_server"
    else:
        source.pop("server", None)
    tool_name = _clip_scrubbed(call.tool_name, MAX_TOOL_NAME, local) or ""
    tool = _clip_scrubbed(call.tool, MAX_TOOL_NAME, local) or tool_name or "unknown_tool"
    rec = {
        "schema": SCHEMA_V2,
        "evidence_id": "",
        "tier": evidence.TIER,
        "session_id": call.session_id if isinstance(call.session_id, str)
        and evidence.SESSION_ID_RE.match(call.session_id) else None,
        "source": source,
        "server": wire_source_server(source),
        "tool": tool,
        "tool_name": tool_name,
        "client": _clip_scrubbed(call.client, 64, local) or call.runtime,
        "called_at": called_at,
    }
    if isinstance(call.tool_use_id, str) and 0 < len(call.tool_use_id) <= 256:
        rec["tool_use_id"] = call.tool_use_id
    return rec


def build_record(call: ToolCall, *, home: Optional[str] = None, withheld: Optional[gate.Withheld] = None) -> dict:
    """The v2 spool entry for one call (scrubbed, capped), or its withheld
    stub (no args, no result, no summary)."""
    local = _Local(call.spill_root, call.cwd, home)
    called_at = call.called_at or evidence.now_iso()
    rec = _base_record(call, local, called_at)
    failed = call.error is not None
    if withheld is not None:
        rec.update({"status": "error" if failed else "ok", "args": None, "result": None, "truncated": False,
                    "withheld": withheld.as_dict()})
        if failed:
            rec["is_error"] = True
    else:
        norm, nid = normalize_call(call)
        if not isinstance(norm, Normalized):  # pragma: no cover - normalize_call guarantees it
            norm = Normalized(text=[""])
        args_in = call.tool_input if call.tool_input is not None else {}
        args, args_truncated = redact_and_cap(evidence.well_formed(args_in), evidence.MAX_ARGS_BYTES, local, home,
                                              call.cwd)
        body: dict = {}
        if norm.has_structured or norm.structured is not None:
            body["structured"] = norm.structured
        if norm.text:
            body["text"] = [t if isinstance(t, str) else str(t) for t in norm.text]
        if norm.other_blocks:
            body["other_blocks"] = int(norm.other_blocks)
        result, truncated = redact_and_cap(evidence.well_formed(body), evidence.MAX_RESULT_BYTES, local, home,
                                           call.cwd)
        is_error = failed or bool(norm.is_error)
        rec.update({
            "status": "error" if is_error else "ok",
            "normalizer": nid,
            "args": args,
            "result": result,
            "truncated": truncated,
        })
        if norm.spilled_bytes is not None:
            rec["spilled"] = True
            rec["spilled_bytes"] = int(norm.spilled_bytes)
        if args_truncated:
            rec["args_truncated"] = True
        if is_error:
            rec["is_error"] = True
        if isinstance(norm.exit_code, int) and not isinstance(norm.exit_code, bool):
            rec["exit_code"] = norm.exit_code
        summary = _clip_scrubbed(norm.summary, MAX_SUMMARY_CHARS, local)
        if summary:
            rec["summary"] = summary
    ev_id = evidence_id_v2(call.runtime, call.session_id, call.tool_use_id)
    rec["evidence_id"] = ev_id or _random_id(rec["server"], rec["tool"], called_at)
    return rec


def head_field(head: bytes, name: str, rx: str = r"[^\"\\]{1,256}") -> Optional[str]:
    """A top-level string field read from a payload's first 64 KiB without
    parsing it (a payload too large or too deep to parse)."""
    text = head[: 64 << 10].decode("utf-8", errors="ignore")
    m = re.search(r'"' + re.escape(name) + r'"\s*:\s*"(' + rx + r')"', text)
    return m.group(1) if m else None


def unreadable_record(runtime: str, client: str, head: bytes, spill_root: Optional[str] = None, *,
                      rule: str = "size", hint: Optional[str] = None) -> Optional[dict]:
    """A payload that cannot be read (too large: rule "size"; nested deeper
    than the parser goes: rule "depth"): a withheld stub (reason
    "unreadable"), with the session and tool pulled from the payload's head
    when they are there. None when not even the tool name is readable."""
    tool_name = head_field(head, "tool_name")
    if not tool_name:
        return None
    source, tool = classify_mcp_name(tool_name, runtime)
    call = ToolCall(runtime=runtime, tool_name=tool_name, source=source, tool=tool,
                    session_id=head_field(head, "session_id", r"[A-Za-z0-9_-]{1,128}"),
                    tool_use_id=head_field(head, "tool_use_id", r"[A-Za-z0-9_.:-]{1,256}"), client=client,
                    spill_root=spill_root)
    return build_record(call, withheld=gate.Withheld(gate.REASON_UNREADABLE, rule,
                                                     hint or f"> {MAX_STDIN_BYTES >> 20} MiB"))


# ---------------------------------------------------------------------------
# Context lines
# ---------------------------------------------------------------------------

def context_enabled(env: Optional[dict] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(CONTEXT_ENV, "")).strip().lower() not in ("0", "false", "off", "no")


def _first_in_session(root: Path, session_id: Any) -> bool:
    """True once per session: creates <session>/.hinted."""
    try:
        sdir = Path(root) / evidence.session_dir_name(session_id)
        fd = os.open(str(sdir / HINTED), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def context_line(entry: dict, first: bool, promote_cmd: str = "cardinal-evidence") -> str:
    ev_id = entry["evidence_id"]
    w = entry.get("withheld")
    if isinstance(w, dict):
        text = gate.describe(gate.Withheld(str(w.get("reason")), str(w.get("rule")), str(w.get("hint"))))
        line = f"[evidence:{ev_id} withheld: {text}]"
        if first:
            line += (" Nothing from this call was kept, so it cannot be cited; say so plainly instead of "
                     "paraphrasing its result.")
        return line
    if not first:
        return f"[evidence:{ev_id}]"
    return (f"[evidence:{ev_id}] Cardinal kept this result on this machine. Every tool result in this session gets "
            f"an id like this and can be cited in a storyboard: `{promote_cmd} promote ev_...` uploads only what "
            f"you promote (only what a scene cites; a repeat prints the receipt it already has); "
            f"`{promote_cmd} find <text>` looks an id up.")


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

# A command line that runs the investigation control-log CLI (any path to
# it, any shell separator before it).
_CONTROL_PLANE_RE = re.compile(r"(?:^|[\s/;&|(`'\"])cardinal-storyboard[\s'\"]+investigation(?:[\s'\"]|$)")


def control_plane(tool_input: Any, _depth: int = 0) -> bool:
    """Whether any string in a tool call's input runs `cardinal-storyboard
    investigation …`: the investigation's control log (events, acks, the
    question, links) is never evidence, so such a call is never captured
    as is (salvage_control_plane may keep the rest of a shell command).
    Tool-neutral, like the sensitivity gate."""
    if _depth > 20:
        return False
    if isinstance(tool_input, str):
        return "cardinal-storyboard" in tool_input and bool(_CONTROL_PLANE_RE.search(tool_input))
    if isinstance(tool_input, dict):
        return any(control_plane(v, _depth + 1) for v in tool_input.values())
    if isinstance(tool_input, list):
        return any(control_plane(v, _depth + 1) for v in tool_input)
    return False


# ---------------------------------------------------------------------------
# A control-log command inside a larger shell command
# ---------------------------------------------------------------------------
#
# `cardinal-storyboard investigation question "…"; cat src/*.py` runs the
# control-log CLI AND reads code. Dropping the whole call (as control_plane
# alone does) loses the code read, which may be the evidence for the
# mechanism. Such a call is captured WITHOUT its control-log part, and only
# when every step below is certain; any doubt skips the whole call, as before:
#   1. the CLI is named only in one shell `command` string of a dict input;
#   2. that command splits into top-level statements (quotes, $( ), ( ),
#      backticks, comments, heredocs; anything else -> skip);
#   3. each statement that names the CLI runs it as the last element of its
#      pipeline (its output is printed as is, not transformed), with a
#      subcommand that writes or links (never `events` / `show`, whose
#      output IS the log), and another statement remains;
#   4. those statements (with their stdin: a checkpoint's heredoc of claims)
#      become CONTROL_OMITTED in the recorded command;
#   5. the CLI recorded what it printed (record_control_output, by the CLI
#      itself, for every `investigation` subcommand): those lines are removed
#      from the output; no record -> skip;
#   6. nothing left looks like control-log traffic (the patterns of
#      conductor's isControlLogItem, which refuses such an upload) -> else skip.
# So the control log (claims, other principals' events, acks) never becomes
# evidence, and what the rest of the command showed does.

CONTROL_OMITTED = "[Cardinal control-log command omitted]"
CONTROL_OMITTED_NOTE = "(Kept without its cardinal-storyboard investigation part, which is never evidence.)"
CONTROL_OUTPUT_DIR = Path(".cardinal") / "investigations" / "control-output"
CONTROL_OUTPUT_TTL_S = 30 * 60
CONTROL_OUTPUT_MAX_FILES = 256
CONTROL_OUTPUT_MAX_BYTES = 256 << 10
# Subcommands whose output is a short status line, never the event stream.
_CONTROL_SALVAGE_SUBS = frozenset(("link", "question", "checkpoint", "ack", "post", "create", "attach", "bind",
                                   "-h", "--help"))
_CLI_NAMES = frozenset(("cardinal-storyboard", "cardinal-storyboard.py"))
_WRAPPERS = frozenset(("env", "command", "exec", "nohup", "time"))
# A statement that prints nothing worth citing on its own.
_INERT = frozenset(("cd", "pushd", "popd", "export", "unset", "set", "true", ":", "wait", "sleep", "umask",
                    "shopt", "alias", "source", "."))
_PYTHON_RE = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# conductor maestro storyboard/evidence-upload.ts CONTROL_LOG_TEXT_RE.
_CONTROL_LOG_TEXT_RE = re.compile(
    r"cardinal-storyboard(\.py)?[\"']?\s+investigation\b"
    r"|/mcp-tools/((append|read)-investigation-events?|checkpoint-investigation)\b"
    r"|\"is_investigation_author\"\s*:|\"authority\"\s*:\s*\"producer_claim\""
    r"|^(is_investigation_author|ack_of|producer_claim)$", re.IGNORECASE)
_MAX_GROUP_DEPTH = 20


def control_output_dir(home: Path) -> Path:
    return Path(home) / CONTROL_OUTPUT_DIR


def record_control_output(home: Path, text: str, now: Optional[float] = None,
                          env: Optional[dict] = None, overflow: bool = False) -> None:
    """For the control-log CLI: record the lines it printed (stdout and
    stderr), so a capture of the shell command it ran in can leave them out
    (step 5 above). 0600 in a 0700 directory, kept CONTROL_OUTPUT_TTL_S.
    More than CONTROL_OUTPUT_MAX_BYTES records only an overflow mark (such a
    call is then never captured). Nothing when capture is off. Never raises."""
    import json
    import time
    try:
        home = Path(home)
        if evidence.capture_disabled(evidence.default_root(home), env):
            return
        now = time.time() if now is None else now
        d = control_output_dir(home)
        os.makedirs(str(d.parent), mode=0o700, exist_ok=True)
        evidence._ensure_private_dir(d)
        overflow = overflow or not isinstance(text, str) or len(text) > CONTROL_OUTPUT_MAX_BYTES
        lines = [] if overflow else sorted({ln.strip() for ln in text.splitlines() if ln.strip()})
        body = json.dumps({"at": now, "overflow": overflow, "lines": lines}, ensure_ascii=False)
        name = f"out_{int(now * 1e9)}_{os.getpid()}.json"
        fd = os.open(str(d / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        _gc_control_output(d, now)
    except BaseException:
        pass


def _gc_control_output(d: Path, now: float) -> list:
    """Remove expired records; -> the live ones, newest first, at most
    CONTROL_OUTPUT_MAX_FILES (older ones beyond that are removed)."""
    live = []
    with os.scandir(str(d)) as it:
        for e in it:
            if not (e.name.startswith("out_") and e.name.endswith(".json")) or not e.is_file(follow_symlinks=False):
                continue
            try:
                mt = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if now - mt > CONTROL_OUTPUT_TTL_S:
                try:
                    os.unlink(e.path)
                except OSError:
                    pass
            else:
                live.append((mt, e.path))
    live.sort(reverse=True)
    for _, p in live[CONTROL_OUTPUT_MAX_FILES:]:
        try:
            os.unlink(p)
        except OSError:
            pass
    return [p for _, p in live[:CONTROL_OUTPUT_MAX_FILES]]


def _control_lines(home: Path, now: Optional[float] = None) -> Optional[set]:
    """The lines the control-log CLI printed recently, or None (fail
    closed): no record, an overflow, or an unreadable one."""
    import json
    import time
    now = time.time() if now is None else now
    d = control_output_dir(home)
    try:
        if os.path.islink(str(d)) or not d.is_dir():
            return None
        paths = _gc_control_output(d, now)
    except OSError:
        return None
    if not paths:
        return None
    out: set = set()
    for p in paths:
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                rec = json.loads(f.read(CONTROL_OUTPUT_MAX_BYTES * 2))
        except (OSError, ValueError):
            return None
        if not isinstance(rec, dict) or rec.get("overflow") is not False or not isinstance(rec.get("lines"), list):
            return None
        out.update(ln for ln in rec["lines"] if isinstance(ln, str))
    return out


def _skip_bq(cmd: str, i: int) -> int:
    """Index after the backtick closing a `…` that starts at i (after the
    opening one), or -1."""
    n = len(cmd)
    while i < n:
        if cmd[i] == "\\":
            i += 2
        elif cmd[i] == "`":
            return i + 1
        else:
            i += 1
    return -1


def _skip_dq(cmd: str, i: int, depth: int = 0) -> int:
    """Index after the quote closing a "…" that starts at i, or -1."""
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif c == "`":
            i = _skip_bq(cmd, i + 1)
            if i < 0:
                return -1
        elif cmd.startswith("$(", i):
            i = _skip_group(cmd, i + 2, depth + 1)
            if i < 0:
                return -1
        else:
            i += 1
    return -1


def _skip_group(cmd: str, i: int, depth: int = 0) -> int:
    """Index after the ")" closing a ( / $( group whose body starts at i, or
    -1 (unbalanced, too deep, or a heredoc inside it)."""
    if depth > _MAX_GROUP_DEPTH:
        return -1
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\":
            i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                return -1
            i = j + 1
        elif c == '"':
            i = _skip_dq(cmd, i + 1, depth)
            if i < 0:
                return -1
        elif c == "`":
            i = _skip_bq(cmd, i + 1)
            if i < 0:
                return -1
        elif c == "(":
            i = _skip_group(cmd, i + 1, depth + 1)
            if i < 0:
                return -1
        elif c == ")":
            return i + 1
        elif cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            return -1
        else:
            i += 1
    return -1


def _heredoc_word(cmd: str, j: int) -> tuple:
    """A heredoc delimiter at j, quotes removed: (word, index after it)."""
    n = len(cmd)
    word = []
    while j < n and cmd[j] not in " \t\n;&|<>()":
        c = cmd[j]
        if c in "'\"":
            k = cmd.find(c, j + 1)
            if k < 0:
                return "", n
            word.append(cmd[j + 1:k])
            j = k + 1
        elif c == "\\" and j + 1 < n:
            word.append(cmd[j + 1])
            j += 2
        else:
            word.append(c)
            j += 1
    return "".join(word), j


def shell_statements(cmd: str) -> Optional[list]:
    """The top-level statements of a shell command, as (start, end,
    element_starts): cmd[start:end] is the statement (with its heredoc
    bodies), element_starts the start of each pipeline element after the
    first. Statements are separated by ; && || & and newlines. None when the
    command cannot be split with certainty (an unterminated quote, group or
    heredoc; a heredoc whose statement does not end at its line; `case`)."""
    if not isinstance(cmd, str):
        return None
    n = len(cmd)
    out: list = []
    start, pipes, pending = 0, [], []
    i = 0
    while i < n:
        c = cmd[i]
        if c == "\\":
            i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                return None
            i = j + 1
        elif c == '"':
            i = _skip_dq(cmd, i + 1)
            if i < 0:
                return None
        elif c == "`":
            i = _skip_bq(cmd, i + 1)
            if i < 0:
                return None
        elif c == "(" or cmd.startswith("$(", i):
            i = _skip_group(cmd, i + (2 if c == "$" else 1))
            if i < 0:
                return None
        elif c == ")":
            return None
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|"):
            j = cmd.find("\n", i)
            i = n if j < 0 else j
        elif cmd.startswith("<<<", i):
            i += 3
        elif cmd.startswith("<<", i):
            j = i + 2
            strip = j < n and cmd[j] == "-"
            j += 1 if strip else 0
            while j < n and cmd[j] in " \t":
                j += 1
            word, j = _heredoc_word(cmd, j)
            if not word:
                return None
            pending.append((word, strip))
            i = j
        elif c == "\n":
            end = i
            i += 1
            for word, strip in pending:
                while True:
                    if i >= n:
                        return None
                    j = cmd.find("\n", i)
                    line_end = n if j < 0 else j
                    line = cmd[i:line_end]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == word:
                        end = line_end
                        break
            pending = []
            out.append((start, end, pipes))
            start, pipes = i, []
        elif c in ";&|":
            two = cmd[i:i + 2]
            if c == ";" and two == ";;":
                return None
            if c == "&" and (two == "&>" or (i > 0 and cmd[i - 1] in "<>")):
                i += 2 if two == "&>" else 1
                continue
            if c == "|" and two != "||":
                if i > 0 and cmd[i - 1] == ">":  # >| (clobber)
                    i += 1
                    continue
                i += 2 if two == "|&" else 1
                pipes.append(i)
                continue
            if pending:
                return None
            width = 2 if two in ("&&", "||") else 1
            out.append((start, i, pipes))
            i += width
            start, pipes = i, []
        else:
            i += 1
    if pending:
        return None
    out.append((start, n, pipes))
    return out


def _salvageable_control(cmd: str, start: int, end: int, pipes: list) -> bool:
    """Whether a statement naming the CLI runs it as its pipeline's last
    element, with a subcommand whose output is a status line (step 3)."""
    last = pipes[-1] if pipes else start
    if any(_CONTROL_PLANE_RE.search(cmd[a:b]) for a, b in zip([start] + pipes, [p - 1 for p in pipes])):
        return False
    first_line = cmd[last:end].split("\n", 1)[0]
    import shlex
    try:
        words = shlex.split(first_line, comments=True)
    except ValueError:
        return False
    while words and _ASSIGN_RE.match(words[0]):
        words.pop(0)
    while words and (words[0] in _WRAPPERS or _PYTHON_RE.match(os.path.basename(words[0]))):
        words.pop(0)
        while words and words[0].startswith("-"):
            words.pop(0)
    return (len(words) >= 3 and os.path.basename(words[0]) in _CLI_NAMES and words[1] == "investigation"
            and words[2] in _CONTROL_SALVAGE_SUBS)


def _shows_something(text: str, pipes: list) -> bool:
    """Whether a statement kept beside a control-log command can show
    anything: not empty, a comment, or a lone `cd` / `export` / … (`cd repo
    && cardinal-storyboard investigation checkpoint …` is not evidence)."""
    t = text.strip()
    if not t or t.startswith("#"):
        return False
    if pipes:
        return True
    import shlex
    try:
        words = shlex.split(t.split("\n", 1)[0], comments=True)
    except ValueError:
        return True
    while words and _ASSIGN_RE.match(words[0]):
        words.pop(0)
    return bool(words) and words[0] not in _INERT


def _subtract_lines(text: Any, lines: set) -> Any:
    if not isinstance(text, str) or not text or not lines:
        return text
    return "\n".join(ln for ln in text.split("\n") if ln.strip() not in lines)


def salvage_control_plane(call: ToolCall, home: Path, now: Optional[float] = None) -> Optional[ToolCall]:
    """A call whose input runs the control-log CLI, as the same call without
    its control-log part (steps 1-5 above), or None: skip the whole call."""
    import dataclasses
    ti = call.tool_input
    if not isinstance(ti, dict) or not home:
        return None
    hits = [k for k, v in ti.items() if control_plane(v)]
    if len(hits) != 1 or hits[0] not in ("command", "cmd") or not isinstance(ti[hits[0]], str):
        return None
    key = hits[0]
    cmd = ti[key]
    stmts = shell_statements(cmd)
    if stmts is None:
        return None
    pieces, last, kept = [], 0, 0
    for start, end, pipes in stmts:
        text = cmd[start:end]
        if "cardinal-storyboard" in text and _CONTROL_PLANE_RE.search(text):
            if not _salvageable_control(cmd, start, end, pipes):
                return None
            lead = len(text) - len(text.lstrip())
            trail = len(text) - len(text.rstrip())
            pieces += [cmd[last:start + lead], CONTROL_OMITTED]
            last = end - trail
        elif _shows_something(text, pipes):
            kept += 1
    if not kept:
        return None
    pieces.append(cmd[last:])
    new_cmd = "".join(pieces)
    if control_plane(new_cmd):
        return None
    lines = _control_lines(Path(home), now)
    if lines is None:
        return None
    response = call.response
    if isinstance(response, dict):
        response = dict(response)
        for k in ("stdout", "stderr"):
            response[k] = _subtract_lines(response.get(k), lines)
    return dataclasses.replace(call, tool_input=dict(ti, **{key: new_cmd}), response=response,
                               error=_subtract_lines(call.error, lines))


def _names_control_log(v: Any, depth: int = 0) -> bool:
    """Whether any key or string of v looks like control-log traffic (step 6)."""
    if depth > 50:
        return True
    if isinstance(v, str):
        return bool(_CONTROL_LOG_TEXT_RE.search(v))
    if isinstance(v, dict):
        return any(_names_control_log(k, depth + 1) or _names_control_log(x, depth + 1) for k, x in v.items())
    if isinstance(v, list):
        return any(_names_control_log(x, depth + 1) for x in v)
    return False


def capture_call(call: ToolCall, home: Path, *, env: Optional[dict] = None, rules: Optional[gate.Rules] = None,
                 promote_cmd: str = "cardinal-evidence", write: bool = True) -> Optional[Captured]:
    """Record one tool call. None when capture is off or the call is
    Cardinal's own (already witnessed). Otherwise the written entry (or its
    withheld stub) and the context line for the agent (None when
    CARDINAL_EVIDENCE_CONTEXT=0). A failed write raises: hooks call this
    inside their fail-open guard."""
    env = os.environ if env is None else env
    if not isinstance(call, ToolCall) or not _RUNTIME_RE.match(call.runtime or ""):
        return None
    root = evidence.default_root(home)
    if evidence.capture_disabled(root, env):
        return None
    if isinstance(call.source, dict) and call.source.get("kind") == SOURCE_CARDINAL:
        return None
    salvaged = False
    if control_plane(call.tool_input):
        call = salvage_control_plane(call, home)
        if call is None:
            return None
        salvaged = True
    home_s = str(home) if home else None
    if rules is None:
        rules = gate.load_rules(Path(home) if home else None, call.cwd)
    verdict = gate.check(call.tool_name, call.tool_input, cwd=call.cwd, home=home_s, rules=rules)
    entry = build_record(call, home=home_s, withheld=verdict)
    if salvaged:
        if _names_control_log([entry.get("args"), entry.get("result"), entry.get("summary")]):
            return None
        entry["control_log_omitted"] = True
    if not write:
        return Captured(entry, None, verdict is not None)
    evidence.write_entry(root, entry)
    # The entry is on disk: from here nothing (not even the budget timer,
    # see capture_call_guarded) may turn it into a failed capture.
    try:
        evidence.gc(root)
    except BaseException:
        pass
    line = None
    try:
        if context_enabled(env):
            line = context_line(entry, _first_in_session(root, entry.get("session_id")), promote_cmd)
            if salvaged:
                line += " " + CONTROL_OMITTED_NOTE
    except BaseException:
        line = f"[evidence:{entry['evidence_id']}]"
    return Captured(entry, line, verdict is not None)


# What a call that could not be processed in time is recorded as.
FALLBACK_BUDGET_S = 0.3


def capture_call_guarded(call: ToolCall, home: Path, *, budget_s: float = TIME_BUDGET_S, env: Optional[dict] = None,
                         rules: Optional[gate.Rules] = None, promote_cmd: str = "cardinal-evidence",
                         ) -> Optional[Captured]:
    """capture_call under a wall-clock budget, for hooks. Never raises.

    Whatever the tool and whatever its input or result, the call is not
    lost: when the pipeline runs out of budget (a pathological input the
    gate or the scrub cannot finish in time) or fails, the call is recorded
    as a withheld stub (reason "unreadable", rule "budget" or "error"; no
    arguments, no result), so the agent still gets an id and a reason
    instead of silence. Fail closed: nothing unchecked is ever kept."""
    rule = hint = None
    try:
        with time_guard(budget_s):
            return capture_call(call, home, env=env, rules=rules, promote_cmd=promote_cmd)
    except BudgetExceeded:
        rule, hint = "budget", "too large to check in time"
    except Exception:
        rule, hint = "error", "could not be processed"
    except BaseException:
        return None
    try:
        with time_guard(FALLBACK_BUDGET_S):
            return write_stub(call, home, gate.Withheld(gate.REASON_UNREADABLE, rule, hint), env, promote_cmd)
    except BaseException:
        return None


def write_stub(call: ToolCall, home: Path, withheld: "gate.Withheld", env: Optional[dict] = None,
               promote_cmd: str = "cardinal-evidence") -> Optional[Captured]:
    """Record a call as a withheld stub only (no arguments, no result): for
    a call whose content cannot be checked (too large to send or read, or
    the pipeline could not finish). Same opt-outs and skips as
    capture_call. A deterministic id already on disk is left alone."""
    env = os.environ if env is None else env
    if not isinstance(call, ToolCall) or not _RUNTIME_RE.match(call.runtime or ""):
        return None
    root = evidence.default_root(home)
    if evidence.capture_disabled(root, env):
        return None
    if isinstance(call.source, dict) and call.source.get("kind") == SOURCE_CARDINAL:
        return None
    entry = build_record(call, home=str(home) if home else None, withheld=withheld)
    path = evidence.entry_path(root, entry.get("session_id"), entry["evidence_id"])
    if os.path.lexists(str(path)):
        # A deterministic id already on disk is the full entry, written just
        # before the budget ran out: keep it.
        return Captured(entry, f"[evidence:{entry['evidence_id']}]" if context_enabled(env) else None, False)
    evidence.write_entry(root, entry)
    line = None
    if context_enabled(env):
        line = context_line(entry, False, promote_cmd)
    return Captured(entry, line, True)


# ---------------------------------------------------------------------------
# Hook budget helpers
# ---------------------------------------------------------------------------

class BudgetExceeded(BaseException):
    """Raised by time_guard's timer; a hook's fail-open guard swallows it."""


@contextmanager
def time_guard(seconds: float = TIME_BUDGET_S):
    """Abort the block after `seconds` of wall time (SIGALRM/setitimer, main
    thread, POSIX). A write it interrupts leaves only a temp file, which gc
    reaps. A no-op where timers are unavailable."""
    armed = False
    old = None
    try:
        if hasattr(signal, "setitimer") and seconds > 0:
            def _fire(signum, frame):  # noqa: ARG001
                raise BudgetExceeded()
            old = signal.signal(signal.SIGALRM, _fire)
            signal.setitimer(signal.ITIMER_REAL, seconds)
            armed = True
    except (ValueError, OSError):
        armed = False
    try:
        yield
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            try:
                signal.signal(signal.SIGALRM, old if old is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass


def read_stdin_bounded(limit: int = MAX_STDIN_BYTES) -> tuple:
    """-> (bytes, complete). Reads at most limit + 1 bytes of stdin."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        data = sys.stdin.read(limit + 1).encode("utf-8", errors="ignore")
    else:
        data = stream.read(limit + 1)
    if len(data) > limit:
        return data[:limit], False
    return data, True

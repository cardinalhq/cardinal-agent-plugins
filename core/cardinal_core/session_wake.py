"""Owner-woken sessions: resume a stopped, sandboxed agent session when its
owner posts a decision addressed to it (docs/specs/resume-and-wake.md §5).

Why: an Investigation outlives the session that did its work, and an owner
decision posted from the web reaches a session only at its own hook points.
A stopped session receives nothing until the owner types into that window.
The waker closes that gap for the one case the owner authorized explicitly,
and for nothing else.

Authorization (§5.1). Arming IS the decision: an `owner.decided` event the
owner posted on the Investigation page from their signed-in web session,
addressed to the session (`to_session_id`). maestro accepts that route only
from an OIDC web session of the investigation's author (proof C-1: the
plugin's API key, the only Cardinal credential an agent holds, is refused).
Nothing is armed from inside a session. The machine opt-in is the owner
running `cardinal-wake enable` (a per-user launchd agent); `--launch` also
allows unattended launch for runtimes that passed their gates (§6), else the
waker only notifies.

One pass (run_once; launchd runs `cardinal-wake run --once` every
RUN_INTERVAL seconds from the stable copy in ~/.cardinal/wake/lib/):
  1. settle every in-flight ledger entry: a launch the waker was interrupted
     in (FAILED, never retried), a child that exited (the sandbox it ran in
     is verified, then ACKNOWLEDGED when this session acknowledged the
     decision, else FAILED), a child past WAKE_TIMEOUT (FAILED, left running,
     the owner notified). Runs whether or not the opt-in is on.
  2. with the opt-in on, every candidate session: a binding in
     ~/.cardinal/investigations/sessions touched within CANDIDATE_WINDOW,
     is_author true, the owner_decision capability on. Its events are read
     straight from Cardinal (read-investigation-events with the plugin's key
     for that runtime, the binding's org), never from the local inbox, so a
     local file cannot fake a decision. A decision e wakes sid only when
     (§5.2): verified_owner_decision(e) (server-stamped fields);
     e.to_session_id == sid (a broadcast never wakes); e is still
     deliverable() to sid (capability on, unacknowledged by this session);
     e.seq has no ledger entry; the caps hold (SESSION_DAILY_CAP claims per
     session and GLOBAL_DAILY_CAP across sessions per rolling day); the
     session is not live; the session is eligible. Live or ineligible:
     NOTIFIED (a notification, never a launch, never reconsidered).

Eligibility (§5.3). Codex only, and only when ALL hold: a rollout
~/.codex/sessions/**/rollout-*-<sid>.jsonl exists (the Codex sandbox cannot
write there); its session_meta (the one whose id is sid) names a CLI
originator (CODEX_ORIGINATORS, an allow-list: the desktop app's process
outlives its threads) and a CLI source (not a subagent thread); EVERY
turn_context has sandbox_policy.type workspace-write or read-only and never
network_access true (one danger-full-access turn: that agent could have
tampered with local state); the session id is a UUID (never an argv option);
cwd comes from session_meta.cwd (never the binding, which the agent can
write) and is a git work tree (`codex exec` refuses anything else without
--skip-git-repo-check, which is never passed). A Claude transcript
(~/.claude/projects/*/<sid>.jsonl): notify-only, gate C-6 is not
demonstrated (a resumed `claude -p` runs with the user's own settings,
bypass modes and allow lists included; Remote Control is the Claude path).
Anything else: notify-only. Rollouts are streamed line by line, bounded
(MAX_ROLLOUT_BYTES, MAX_LINE), and only their session_meta / turn_context
records are parsed; nothing of their contents is logged.

Launch (Codex; config launch true, no ~/.cardinal/wake/disabled/codex):
  <codex> exec -s <the session's sandbox> -c approval_policy="never" resume <sid> "<WAKE_PROMPT>"
pinned by a golden test: the absolute codex path resolved at enable time,
cwd from the rollout, never --profile, --dangerously-*, --ignore-* or
--skip-git-repo-check. The environment is the waker's own (launchd's, plus
the PATH captured at enable) without CARDINAL_* (nothing may switch the
hooks, the poller or the storyboard off, or join another investigation),
CODEX_HOME (the rollout verified is ~/.codex's) or a parent session id.
Detached (its own session), output to a 0600 log in ~/.cardinal/wake/logs.
After it exits, every turn_context it added to the rollout must show the
pinned mode with network off; otherwise (or with none recorded) the waker
writes ~/.cardinal/wake/disabled/codex with the reason, notifies, and never
launches Codex again until the owner runs `cardinal-wake reenable codex`.
There is no fallback to other flags.

Lifecycle (§5.4), ledger ~/.cardinal/wake/ledger/<sid>.json; every
read-modify-write holds an flock on <sid>.lock, and every transition is
written before the action it describes:
  DECISION_SEEN -> CLAIMED -> RESUMING -> ACKNOWLEDGED | FAILED; NOTIFIED.
A claim is refused while the session has an entry in flight (CLAIMED, or
RESUMING whose child has not been settled); the claim, the spawn and the
RESUMING write happen under one lock, so a CLAIMED entry found later means
the waker died mid-launch (FAILED, not retried). A spawn error (no child
ever existed) is retried at most SPAWN_RETRIES times, then FAILED. A child
that started is never retried. A decision posted during a run is delivered
inside it by the hooks; one still unacknowledged after the run (and after
LIVE_WINDOW, so the run's own file writes do not read as a live session) is
considered again and counts against the caps. The caps' check and the claim
also hold ~/.cardinal/wake/ledger/.global.lock (taken after the session's
lock, never before it).

Liveness: live when ANY of: the poller status anchor pid is alive and
started before the status file was written (a reused pid is dead); a
process's argv contains the session id; the rollout or transcript, or the
binding's .active file, changed within LIVE_WINDOW; a child the waker
started (any ledger entry's pid, with its start time) is alive, such as one
left running past WAKE_TIMEOUT. A signal that cannot be
read (no process table, an anchor whose start time is unknown) counts as
live.

Reporting: each transition is a ledger history line and, for NOTIFIED,
RESUMING, ACKNOWLEDGED and FAILED, a local macOS notification naming the
session's short id, the investigation and the state, never the decision's
text (the ledger never holds it either). Success means the session
acknowledged the decision; launching a process is never reported as
success. A `session.wake` server event is a follow-up (§8).

Standard library only; Python 3.9. Every external effect (clock, process
table, process start times, launcher, notifier, Cardinal reads, launchctl)
is injectable for tests; nothing here starts a real agent unless the
default launcher is used by `run`.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, List, Mapping, Optional, Tuple

from . import investigation_events as ie
from . import investigation_poller as poller
from .owner_input import wake_prompt

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------

CANDIDATE_WINDOW = 7 * 86400.0   # a binding untouched this long is never woken
RECENT_WINDOW = 86400.0          # bindings touched this recently are read every pass ...
OLD_CHECK_EVERY = 300.0          # ... older candidates at most this often
LIVE_WINDOW = 120.0
WAKE_TIMEOUT = 2 * 3600.0
SESSION_DAILY_CAP = 3
GLOBAL_DAILY_CAP = 10
DAY = 86400.0
SPAWN_RETRIES = 2
ACK_CHECK_GRACE = 3600.0         # an exited child whose acknowledgment cannot be read: FAILED after this
RUN_INTERVAL = 45                # launchd StartInterval (seconds)
LEDGER_KEEP = 30 * 86400.0       # terminal entries kept this long (a repeat of their seq never wakes)
LEDGER_MAX_ENTRIES = 200
READ_TIMEOUT = 10.0
READ_BUDGET = 25.0
MAX_ROLLOUT_BYTES = 512 * 1024 * 1024
MAX_LINE = 16 * 1024 * 1024
MAX_LAUNCHD_LOG = 1024 * 1024

PLIST_LABEL = "io.cardinalhq.wake"

DECISION_SEEN = "DECISION_SEEN"
CLAIMED = "CLAIMED"
RESUMING = "RESUMING"
ACKNOWLEDGED = "ACKNOWLEDGED"
FAILED = "FAILED"
NOTIFIED = "NOTIFIED"
IN_FLIGHT = (CLAIMED, RESUMING)
TERMINAL = (ACKNOWLEDGED, FAILED, NOTIFIED)
NOTIFY_STATES = (NOTIFIED, RESUMING, ACKNOWLEDGED, FAILED)

CODEX = "codex"
CLAUDE = "claude"
UNKNOWN = "unknown"
RUNTIMES = (CODEX, CLAUDE)
SANDBOX_MODES = ("workspace-write", "read-only")
# Originators of Codex's own CLI (codex exec, the TUI). An allow-list: the
# desktop app ("Codex Desktop") and anything unrecognized are notify-only.
CODEX_ORIGINATORS = frozenset({"codex_exec", "codex_cli_rs"})
CODEX_SOURCES = frozenset({"exec", "cli"})
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")  # fullmatch
APPROVAL_NEVER = 'approval_policy="never"'
STRIPPED_ENV = frozenset({"CODEX_HOME", "CODEX_SESSION_ID", "OPENAI_CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                          "CLAUDE_SESSION_ID"})
_LINK_RE = re.compile(r"https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?/[A-Za-z0-9._~/%?=&#:+-]{0,300}")  # fullmatch

PS = "/bin/ps"
OSASCRIPT = "/usr/bin/osascript"
LAUNCHCTL = "/bin/launchctl"

NOTIFY_ONLY_TEXT = (
    "Notify-only (a notification, never a launch): Claude Code sessions (a resumed `claude -p` runs with your "
    "own settings, so its sandbox cannot be guaranteed; use `claude --remote-control` instead), Codex desktop "
    "app sessions, any session that ever ran danger-full-access or with network access, and a session whose "
    "window is still open.")
LAUNCH_TEXT = (
    "Can launch: Codex CLI sessions (codex / codex exec) that only ever ran -s workspace-write or -s read-only, "
    "resumed in that same sandbox with approvals off (`codex exec -s <mode> -c approval_policy=\"never\" "
    "resume <session>`).")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def wake_dir(home: Path) -> Path:
    return home / ".cardinal" / "wake"


def config_path(home: Path) -> Path:
    return wake_dir(home) / "config.json"


def ledger_dir(home: Path) -> Path:
    return wake_dir(home) / "ledger"


def ledger_path(home: Path, sid: str) -> Path:
    if not ie.valid_session(sid):
        raise ValueError(f"not a session id: {sid!r}")
    return ledger_dir(home) / f"{sid}.json"


def disabled_path(home: Path, runtime: str) -> Path:
    if runtime not in RUNTIMES:
        raise ValueError(f"not a runtime: {runtime!r}")
    return wake_dir(home) / "disabled" / runtime


def logs_dir(home: Path) -> Path:
    return wake_dir(home) / "logs"


def lib_dir(home: Path) -> Path:
    return wake_dir(home) / "lib"


def poll_state_path(home: Path) -> Path:
    return wake_dir(home) / "poll.json"


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


@contextlib.contextmanager
def _locked(path: Path, wait: float) -> Iterator[bool]:
    """flock on `path` (created 0600); yields whether it was acquired within
    `wait` seconds (investigation_events.locked's pattern)."""
    if fcntl is None:  # pragma: no cover
        yield True
        return
    _private_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def session_lock(home: Path, sid: str, wait: float = 5.0):
    ledger_path(home, sid)  # validates sid
    return _locked(ledger_dir(home) / f"{sid}.lock", wait)


def global_lock(home: Path, wait: float = 10.0):
    return _locked(ledger_dir(home) / ".global.lock", wait)


# ---------------------------------------------------------------------------
# Config, disabled runtimes
# ---------------------------------------------------------------------------

def read_config(home: Path) -> dict:
    c = _read_json(config_path(home))
    codex = c.get("codex_path")
    return {"enabled": c.get("enabled") is True, "launch": c.get("launch") is True,
            "codex_path": codex if isinstance(codex, str) and os.path.isabs(codex) else None,
            "enabled_at": c.get("enabled_at"), "python": c.get("python"),
            "versions": c.get("versions") if isinstance(c.get("versions"), dict) else {}}


def write_config(home: Path, config: dict) -> None:
    _private_dir(wake_dir(home))
    ie.write_json(config_path(home), config)


def disabled_reason(home: Path, runtime: str) -> Optional[str]:
    """Why launching `runtime` is disabled (its marker's reason), or None."""
    p = disabled_path(home, runtime)
    if not p.exists() and not p.is_symlink():
        return None
    reason = _read_json(p).get("reason")
    return reason if isinstance(reason, str) and reason else "disabled (unreadable marker)"


def disable_runtime(home: Path, runtime: str, reason: str, now: float) -> None:
    p = disabled_path(home, runtime)
    _private_dir(p.parent)
    ie.write_json(p, {"reason": reason, "at": _iso(now)})


def reenable_runtime(home: Path, runtime: str) -> bool:
    try:
        disabled_path(home, runtime).unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def read_ledger(home: Path, sid: str) -> dict:
    data = _read_json(ledger_path(home, sid))
    entries = data.get("entries")
    data["session_id"] = sid
    data["entries"] = {k: v for k, v in entries.items() if isinstance(v, dict)} if isinstance(entries, dict) else {}
    return data


def write_ledger(home: Path, sid: str, ledger: dict, now: float) -> None:
    entries = ledger["entries"]
    for k in [k for k, e in entries.items()
              if e.get("state") in TERMINAL and now - (_num(e.get("updated_at")) or 0.0) > LEDGER_KEEP]:
        del entries[k]
    if len(entries) > LEDGER_MAX_ENTRIES:
        terminal = sorted((k for k, e in entries.items() if e.get("state") in TERMINAL),
                          key=lambda k: _num(entries[k].get("updated_at")) or 0.0)
        for k in terminal[:len(entries) - LEDGER_MAX_ENTRIES]:
            del entries[k]
    _private_dir(ledger_dir(home))
    ie.write_json(ledger_path(home, sid), ledger)


def transition(entry: dict, state: str, now: float, note: str = "") -> dict:
    """Move `entry` to `state`, appending one history line (never decision
    text: `note` is the waker's own words)."""
    entry["state"] = state
    entry["updated_at"] = now
    line = {"state": state, "at": _iso(now)}
    if note:
        line["note"] = note
        entry["reason"] = note
    entry.setdefault("history", []).append(line)
    if state in TERMINAL:
        entry["finished_at"] = now
    return entry


def ledger_sids(home: Path) -> List[str]:
    try:
        names = os.listdir(str(ledger_dir(home)))
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and ie.valid_session(n[:-5]))


def _claims(ledger: dict, now: float) -> int:
    return sum(1 for e in ledger["entries"].values()
               if (_num(e.get("claimed_at")) is not None and 0 <= now - e["claimed_at"] < DAY))


def global_claims(home: Path, now: float) -> int:
    return sum(_claims(read_ledger(home, sid), now) for sid in ledger_sids(home))


def in_flight(ledger: dict) -> List[dict]:
    return [e for e in ledger["entries"].values() if e.get("state") in IN_FLIGHT]


def _last_finished(ledger: dict) -> float:
    return max((_num(e.get("finished_at")) or 0.0 for e in ledger["entries"].values()
                if e.get("child")), default=0.0)


# ---------------------------------------------------------------------------
# Processes, notifications, launch (the defaults System uses)
# ---------------------------------------------------------------------------

def process_start(pid: int) -> Optional[float]:
    """When process `pid` started (epoch seconds, 1 s resolution), or None."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        out = subprocess.run([PS, "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=3,
                             check=False, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}).stdout.strip()
        return time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        return None


def process_table() -> Optional[List[Tuple[int, str]]]:
    """(pid, argv as one string) for every process, or None when ps fails."""
    try:
        r = subprocess.run([PS, "-A", "-ww", "-o", "pid=,args="], capture_output=True, text=True, timeout=5,
                           check=False, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        with contextlib.suppress(ValueError):
            rows.append((int(pid), args))
    return rows


def notify_macos(title: str, message: str) -> None:
    """A macOS notification; the text goes in as osascript arguments, never
    into the script, so nothing in it can be evaluated."""
    if sys.platform != "darwin":
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([OSASCRIPT, "-e", "on run argv", "-e",
                        "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                        title, message], capture_output=True, timeout=10, check=False)


def launch_detached(argv: List[str], cwd: str, env: Mapping[str, str], log_path: Path) -> int:
    """Start `argv` in its own session, stdin /dev/null, stdout and stderr to
    `log_path` (0600). Returns the pid; raises OSError when no process could
    be created (a spawn error)."""
    _private_dir(log_path.parent)
    fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                             start_new_session=True, close_fds=True)
    finally:
        os.close(fd)
    _CHILDREN.append(p)
    return p.pid


_CHILDREN: list = []   # this process's launches: kept so they are reaped, never killed


def reap() -> None:
    """Collect exited children (a long-running `run` would otherwise keep
    them as zombies, which still answer kill(pid, 0))."""
    for p in list(_CHILDREN):
        with contextlib.suppress(OSError):
            if p.poll() is None:
                continue
        _CHILDREN.remove(p)


def codex_connection(home: Path) -> dict:
    """The Codex plugin's connection: ~/.codex/cardinal.json mcp_url and
    cardinal-secrets.json mcp_api_key (never the environment)."""
    from .evidence_promote import agent_paths_connection
    from .paths import AgentPaths
    try:
        return agent_paths_connection(AgentPaths(home=home / ".codex"))(home, {}) or {}
    except Exception:
        return {}


def claude_connection(home: Path) -> dict:
    """The Claude plugin's connection: CARDINAL_MCP_URL / CARDINAL_MCP_API_KEY
    in ~/.claude/settings.json `env` (where /cardinal:connect writes them);
    nothing after /cardinal:disconnect (~/.claude/cardinal-disconnected)."""
    from .evidence_promote import connection_for
    if (home / ".claude" / "cardinal-disconnected").exists():
        return {}
    env = _read_json(home / ".claude" / "settings.json").get("env")
    if not isinstance(env, dict):
        return {}
    try:
        return connection_for(env.get("CARDINAL_MCP_URL"), env.get("CARDINAL_MCP_API_KEY")) or {}
    except Exception:
        return {}


def runtime_connection(runtime: str, home: Path) -> dict:
    return codex_connection(home) if runtime == CODEX else claude_connection(home) if runtime == CLAUDE else {}


def fetch_events(conn: dict, investigation_id: str, sid: str, after: int, client: str,
                 opener=None) -> Tuple[list, bool]:
    """(events after `after` addressed to `sid` or to everyone, whether the
    read reached the head). Raises on a failed read."""
    events, cursor, head = ie.read_all(conn, investigation_id, after=after, to_session_id=sid, client=client,
                                       opener=opener, deadline=time.monotonic() + READ_BUDGET,
                                       timeout=READ_TIMEOUT, limit=ie.PAGE_LIMIT)
    return events, cursor >= head


class System:
    """Every effect the waker has on the world, injectable for tests.

    now() -> epoch; alive(pid); process_start(pid) -> epoch | None;
    process_table() -> [(pid, argv)] | None; launcher(argv, cwd, env,
    log_path) -> pid (OSError: spawn error); notifier(title, message);
    connection(runtime, home) -> {origin, org, key}; fetch(conn, inv, sid,
    after, client) -> (events, complete); before_claim(sid, seq) (a test
    hook, called right before the claim's lock); environ (the child's
    environment is derived from it); out(line) (the report).
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None, alive: Optional[Callable[[int], bool]] = None,
                 process_start: Optional[Callable[[int], Optional[float]]] = None,
                 process_table: Optional[Callable[[], Optional[list]]] = None,
                 launcher: Optional[Callable[..., int]] = None,
                 notifier: Optional[Callable[[str, str], None]] = None,
                 connection: Optional[Callable[[str, Path], dict]] = None,
                 fetch: Optional[Callable[..., Tuple[list, bool]]] = None,
                 before_claim: Optional[Callable[[str, int], None]] = None,
                 environ: Optional[Mapping[str, str]] = None, opener=None,
                 out: Optional[Callable[[str], None]] = None):
        self.now = now or time.time
        self.alive = alive or poller.alive
        self.process_start = process_start or globals()["process_start"]
        self.process_table = process_table or globals()["process_table"]
        self.launcher = launcher or launch_detached
        self.notifier = notifier or notify_macos
        self.connection = connection or runtime_connection
        self.fetch = fetch or (lambda conn, inv, sid, after, client: fetch_events(conn, inv, sid, after, client,
                                                                                 opener=opener))
        self.before_claim = before_claim
        self.environ = os.environ if environ is None else environ
        self.out = out or (lambda line: print(line, flush=True))


# ---------------------------------------------------------------------------
# Runtime resolution and eligibility
# ---------------------------------------------------------------------------

class Runtime:
    """What runs session `sid` here and whether it may be launched.

    kind: codex | claude | unknown; eligible / reason; for an eligible Codex
    session, cwd (session_meta.cwd) and sandbox (the mode to pin: its last
    turn's); files: the rollouts or transcripts (their mtimes are a liveness
    signal)."""

    def __init__(self, kind: str, files: List[Path], eligible: bool = False, reason: str = "",
                 cwd: Optional[str] = None, sandbox: Optional[str] = None):
        self.kind, self.files, self.eligible, self.reason = kind, files, eligible, reason
        self.cwd, self.sandbox = cwd, sandbox


def codex_rollouts(home: Path, sid: str) -> List[Path]:
    if not ie.valid_session(sid):
        return []
    root = home / ".codex" / "sessions"
    return sorted(Path(p) for p in glob.glob(str(root / "**" / f"rollout-*-{glob.escape(sid)}.jsonl"),
                                             recursive=True))


def claude_transcripts(home: Path, sid: str) -> List[Path]:
    if not ie.valid_session(sid):
        return []
    return sorted(Path(p) for p in glob.glob(str(home / ".claude" / "projects" / "*" / f"{glob.escape(sid)}.jsonl")))


def runtime_kind(home: Path, sid: str) -> Runtime:
    """The runtime by its own records (cheap: no file is read)."""
    rollouts, transcripts = codex_rollouts(home, sid), claude_transcripts(home, sid)
    if rollouts and transcripts:
        return Runtime(UNKNOWN, rollouts + transcripts, reason="both a Codex rollout and a Claude transcript "
                                                                "carry this session id: notify-only")
    if transcripts:
        return Runtime(CLAUDE, transcripts, reason="Claude Code sessions are notify-only: a resumed claude -p runs "
                                                   "with your own settings, so its sandbox is not guaranteed")
    if rollouts:
        return Runtime(CODEX, rollouts, reason="not yet checked")
    return Runtime(UNKNOWN, [], reason="no Codex rollout or Claude transcript for this session on this machine: "
                                       "notify-only")


class Unverifiable(Exception):
    """A rollout that cannot be read completely and safely."""


_RECORD_MARKERS = (b"turn_context", b"session_meta")
_TURN_TYPE_RE = re.compile(rb'"type"\s*:\s*"turn_context"')


def _records(path: Path, offset: int = 0) -> Iterator[dict]:
    """The session_meta and turn_context records of a rollout from byte
    `offset`, streamed line by line (MAX_LINE per line, MAX_ROLLOUT_BYTES in
    all). A line that may be a turn_context but cannot be parsed (overlong,
    malformed) raises Unverifiable: every turn has to be seen."""
    st = os.lstat(str(path))
    if not os.path.isfile(str(path)) or os.path.islink(str(path)):
        raise Unverifiable("a rollout is not a regular file")
    if st.st_size - offset > MAX_ROLLOUT_BYTES:
        raise Unverifiable("a rollout is too large to verify")
    total = 0
    with open(str(path), "rb") as f:
        f.seek(offset)
        while True:
            line = f.readline(MAX_LINE + 1)
            if not line:
                return
            total += len(line)
            if total > MAX_ROLLOUT_BYTES:
                raise Unverifiable("a rollout is too large to verify")
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                if b"turn_context" in line[:4096]:
                    raise Unverifiable("a rollout turn record is too large to verify")
                while not line.endswith(b"\n"):   # skip the rest of an overlong line
                    line = f.readline(MAX_LINE)
                    if not line:
                        return
                    total += len(line)
                    if total > MAX_ROLLOUT_BYTES:
                        raise Unverifiable("a rollout is too large to verify")
                continue
            if not any(m in line for m in _RECORD_MARKERS):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                if _TURN_TYPE_RE.search(line):
                    raise Unverifiable("a rollout turn record is malformed")
                continue
            if isinstance(rec, dict) and rec.get("type") in ("turn_context", "session_meta"):
                yield rec


def _turn(rec: dict) -> Tuple[Any, Any]:
    """(sandbox_policy.type, network_access) of a turn_context record."""
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    policy = payload.get("sandbox_policy") if isinstance(payload.get("sandbox_policy"), dict) else {}
    return policy.get("type"), policy.get("network_access")


def _git_work_tree(cwd: str) -> bool:
    p = Path(cwd)
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return True
    return False


def codex_eligibility(home: Path, sid: str, rt: Optional[Runtime] = None) -> Runtime:
    """rt (runtime_kind) with eligible / reason / cwd / sandbox filled in
    from the rollouts (§5.3)."""
    rt = rt or runtime_kind(home, sid)
    if rt.kind != CODEX:
        return rt
    rt.eligible, rt.cwd, rt.sandbox = False, None, None
    if not UUID_RE.fullmatch(sid):
        rt.reason = "the session id is not a Codex UUID: notify-only"
        return rt
    meta, turns = None, []
    try:
        for path in rt.files:
            for rec in _records(path):
                if rec["type"] == "session_meta":
                    p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
                    if meta is None and p.get("id") == sid:
                        meta = p
                else:
                    turns.append(_turn(rec))
    except (OSError, Unverifiable) as e:
        rt.reason = f"{e if isinstance(e, Unverifiable) else 'a rollout could not be read'}: notify-only"
        return rt
    if meta is None:
        rt.reason = "the rollout has no session record for this session: notify-only"
        return rt
    originator, source = meta.get("originator"), meta.get("source")
    if originator not in CODEX_ORIGINATORS:
        rt.reason = ("the Codex desktop app's sessions are notify-only" if originator == "Codex Desktop"
                     else "not a Codex CLI session (unrecognized originator): notify-only")
        return rt
    if not (isinstance(source, str) and source in CODEX_SOURCES):
        rt.reason = "not a top-level Codex CLI session (a subagent or another source): notify-only"
        return rt
    if not turns:
        rt.reason = "no turn recorded a sandbox: notify-only"
        return rt
    for mode, network in turns:
        if mode not in SANDBOX_MODES:
            rt.reason = ("the session ran danger-full-access at least once: notify-only" if mode == "danger-full-access"
                         else "a turn ran without a recognized sandbox: notify-only")
            return rt
        if network is True:
            rt.reason = "the session ran with network access at least once: notify-only"
            return rt
    cwd = meta.get("cwd")
    if not (isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd)):
        rt.reason = "the session's working directory is gone: notify-only"
        return rt
    if not _git_work_tree(cwd):
        rt.reason = ("the session's working directory is not a git work tree (codex exec would need "
                     "--skip-git-repo-check, which is never passed): notify-only")
        return rt
    rt.eligible, rt.reason, rt.cwd, rt.sandbox = True, "eligible", cwd, turns[-1][0]
    return rt


def resolve(home: Path, sid: str) -> Runtime:
    return codex_eligibility(home, sid)


def rollout_offsets(home: Path, sid: str) -> dict:
    """{path: size} of the session's rollouts now (before a launch)."""
    out = {}
    for p in codex_rollouts(home, sid):
        with contextlib.suppress(OSError):
            out[str(p)] = os.lstat(str(p)).st_size
    return out


def verify_after(home: Path, sid: str, offsets: dict, pinned: str, *, require_turn: bool = True) -> Optional[str]:
    """None when every turn_context the woken run added (past `offsets`, or
    in a rollout that did not exist then) ran `pinned` with network off;
    else why not. require_turn: none at all is a failure too."""
    seen = 0
    try:
        for p in codex_rollouts(home, sid):
            start = offsets.get(str(p), 0)
            if os.lstat(str(p)).st_size < start:
                return "the rollout was rewritten during the woken run"
            for rec in _records(p, start):
                if rec["type"] != "turn_context":
                    continue
                seen += 1
                mode, network = _turn(rec)
                if mode != pinned:
                    return f"the woken run's sandbox was {mode!r}, not the pinned {pinned!r}"
                if network is True:
                    return "the woken run had network access"
    except (OSError, Unverifiable) as e:
        return f"the woken run's rollout could not be verified ({e})"
    if require_turn and not seen:
        return "the woken run recorded no turn, so its sandbox could not be confirmed"
    return None


def codex_argv(codex_path: str, sandbox: str, sid: str, seq: int) -> List[str]:
    """The pinned launch (§5.3); nothing else is ever passed."""
    if not (isinstance(codex_path, str) and os.path.isabs(codex_path)):
        raise ValueError("codex must be an absolute path")
    if sandbox not in SANDBOX_MODES:
        raise ValueError(f"not a wake sandbox: {sandbox!r}")
    if not UUID_RE.fullmatch(sid or ""):
        raise ValueError(f"not a Codex session id: {sid!r}")
    return [codex_path, "exec", "-s", sandbox, "-c", APPROVAL_NEVER, "resume", sid, wake_prompt(seq)]


def child_env(environ: Mapping[str, str]) -> dict:
    """The woken child's environment: the waker's, without CARDINAL_* (no
    switch may turn the plugin's hooks, poller or storyboard off, or join
    another investigation), CODEX_HOME or a parent session's id."""
    return {k: v for k, v in environ.items() if not k.startswith("CARDINAL_") and k not in STRIPPED_ENV}


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------

def live_signals(home: Path, sid: str, rt: Runtime, system: System, now: float,
                 ledger: Optional[dict] = None) -> List[str]:
    """Why the session may still be running (empty: it is stopped)."""
    why = []
    led = ledger if ledger is not None else read_ledger(home, sid)
    if any(isinstance(e.get("child"), dict) and child_alive(e["child"], system) for e in led["entries"].values()):
        why.append("a run the waker started is still going")
    st = poller.read_status(home, sid)
    anchor = st.get("anchor")
    if isinstance(anchor, int) and not isinstance(anchor, bool) and anchor > 1 and system.alive(anchor):
        started = system.process_start(anchor)
        try:
            written = poller.status_path(home, sid).stat().st_mtime
        except OSError:
            written = None
        if started is None or written is None or started < written:
            why.append("its agent process is running")
    table = system.process_table()
    if table is None:
        why.append("the process table could not be read")
    elif any(sid in args for pid, args in table if pid != os.getpid()):
        why.append("a running process names the session")
    for p in rt.files:
        with contextlib.suppress(OSError):
            if now - p.stat().st_mtime <= LIVE_WINDOW:
                why.append("its transcript changed in the last 2 minutes")
                break
    if now - ie.last_activity(home, sid) <= LIVE_WINDOW:
        why.append("it used a tool in the last 2 minutes")
    return why


def child_alive(child: dict, system: System) -> bool:
    pid = child.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not system.alive(pid):
        return False
    recorded = _num(child.get("start"))
    if recorded is None:
        return True
    now_start = system.process_start(pid)
    return now_start is None or abs(now_start - recorded) <= 2.0


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def candidate_sessions(home: Path, now: float) -> List[Tuple[str, dict, float]]:
    """(sid, binding, binding mtime) of sessions that may be woken: touched
    within CANDIDATE_WINDOW, the investigation's author, owner_decision on."""
    d = ie.sessions_dir(home)
    try:
        names = os.listdir(str(d))
    except OSError:
        return []
    out = []
    for name in sorted(names):
        sid = name[:-5] if name.endswith(".json") else ""
        if not ie.valid_session(sid):
            continue
        try:
            mtime = (d / name).stat().st_mtime
        except OSError:
            continue
        if now - mtime > CANDIDATE_WINDOW:
            continue
        b = ie.read_binding(home, sid)
        if b is None or b.get("is_author") is not True or not ie.capability(b, "owner_decision"):
            continue
        out.append((sid, b, mtime))
    return out


def read_from(binding: dict) -> int:
    """Where the session's unconsumed events start: its cursor, or before
    the oldest decision it was shown and has not acknowledged."""
    seqs = [p["seq"] for p in binding.get("pending_decisions") or []
            if isinstance(p, dict) and isinstance(p.get("seq"), int) and not isinstance(p.get("seq"), bool)]
    return max(0, min([int(binding["cursor"])] + [s - 1 for s in seqs]))


def wake_decisions(events: list, sid: str, binding: dict) -> List[dict]:
    """The decisions in `events` that may wake `sid` (§5.2, before the
    ledger, caps, liveness and eligibility): verified, addressed to it, and
    still deliverable (unacknowledged by it, owner_decision on)."""
    inv = binding["investigation_id"]
    out = []
    for e in ie.deliverable(events, sid, inv, binding):
        seq = e.get("seq")
        if ie.verified_owner_decision(e) and e.get("to_session_id") == sid and isinstance(seq, int) \
                and not isinstance(seq, bool) and seq > 0:
            out.append(e)
    return sorted(out, key=lambda e: e["seq"])


def acknowledged(events: list, sid: str, seq: int) -> bool:
    """This session (the author's principal naming it) acknowledged #seq."""
    for e in events:
        if isinstance(e, dict) and ie.event_class(e) == ie.CONTROL and e.get("type") == ie.ACKNOWLEDGED \
                and ie._own(e, sid):
            p = e.get("payload")
            if isinstance(p, dict) and p.get("ack_of") == seq:
                return True
    return False


def _connection_for(home: Path, binding: dict, prefer: str, system: System) -> Tuple[dict, str]:
    """The plugin connection whose org is the binding's: the session's own
    runtime first. ({}, "") when none matches."""
    order = [prefer] + [r for r in RUNTIMES if r != prefer] if prefer in RUNTIMES else list(RUNTIMES)
    for runtime in order:
        conn = system.connection(runtime, home) or {}
        if not (conn.get("origin") and conn.get("org") and conn.get("key")):
            continue
        if binding.get("org") in (None, conn.get("org")):
            return conn, runtime
    return {}, ""


def _client(runtime: str, config: dict) -> str:
    version = (config.get("versions") or {}).get(runtime)
    from .evidence import client_string
    try:
        return client_string("claude-plugin" if runtime == CLAUDE else "codex", version)
    except ValueError:
        return "codex"


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

def _notify(system: System, binding: Optional[dict], sid: str, state: str, words: str) -> None:
    """Session short id, investigation and state; never decision text."""
    inv = (binding or {}).get("investigation_id")
    link = (binding or {}).get("investigation_url")
    where = link if isinstance(link, str) and _LINK_RE.fullmatch(link) else (inv if ie.valid_investigation(inv) else "")
    msg = f"Session {sid[:8]}: {state.lower()} - {words}" + (f" {where}" if where else "")
    with contextlib.suppress(Exception):
        system.notifier("Cardinal wake", msg[:400])


def _report(system: System, report: list, sid: str, seq: Optional[int], action: str, why: str = "") -> None:
    report.append({"session_id": sid, "seq": seq, "action": action, "reason": why})
    system.out(f"{sid[:8]} #{seq if seq is not None else '-'} {action}" + (f": {why}" if why else ""))


def run_once(home: Path, system: Optional[System] = None, *, dry_run: bool = False) -> List[dict]:
    """One pass of the waker (see the module docstring). dry_run: reads
    Cardinal and reports what it would do; writes, notifies and launches
    nothing. Returns the report (one dict per action)."""
    system = system or System()
    report: list = []
    config = read_config(home)
    for sid in ledger_sids(home):
        try:
            settle(home, sid, system, config, report, dry_run=dry_run)
        except Exception as e:  # one broken ledger never stops the pass
            _report(system, report, sid, None, "error", f"settling failed ({type(e).__name__})")
    if not config["enabled"]:
        _report(system, report, "-", None, "skip", "wake is not enabled (cardinal-wake enable)")
        return report
    now = system.now()
    polls = _read_json(poll_state_path(home))
    for sid, binding, mtime in candidate_sessions(home, now):
        if now - mtime > RECENT_WINDOW and 0 <= now - (_num(polls.get(sid)) or 0.0) < OLD_CHECK_EVERY:
            continue
        polls[sid] = now
        try:
            consider(home, sid, binding, system, config, report, dry_run=dry_run)
        except Exception as e:
            _report(system, report, sid, None, "error", f"{type(e).__name__}")
    if not dry_run:
        with contextlib.suppress(OSError, ValueError):
            _private_dir(wake_dir(home))
            ie.write_json(poll_state_path(home), {k: v for k, v in polls.items()
                                                   if isinstance(v, (int, float)) and now - v < CANDIDATE_WINDOW})
    return report


def consider(home: Path, sid: str, binding: dict, system: System, config: dict, report: list, *,
             dry_run: bool = False) -> None:
    now = system.now()
    led = read_ledger(home, sid)
    if in_flight(led):
        return  # one run per session: a decision posted now is delivered inside that run
    if 0 <= now - _last_finished(led) < LIVE_WINDOW:
        return  # the run just ended; its own writes would read as a live session
    rt = runtime_kind(home, sid)
    conn, conn_runtime = _connection_for(home, binding, rt.kind, system)
    if not conn:
        return
    try:
        events, complete = system.fetch(conn, binding["investigation_id"], sid, read_from(binding),
                                        _client(conn_runtime, config))
    except Exception as e:
        _report(system, report, sid, None, "skip", f"could not read the investigation ({type(e).__name__})")
        return
    if not complete:
        _report(system, report, sid, None, "skip", "the investigation's events could not all be read this pass")
        return
    pending = [e for e in wake_decisions(events, sid, binding) if str(e["seq"]) not in led["entries"]]
    if not pending:
        return
    if rt.kind == CODEX:
        rt = codex_eligibility(home, sid, rt)
    live = live_signals(home, sid, rt, system, now, led)
    why = ""
    if live:
        why = "the session is live (" + "; ".join(live) + "): it will see the decision itself"
    elif not rt.eligible:
        why = rt.reason
    elif not config["launch"]:
        why = "unattended launch is off (cardinal-wake enable --launch)"
    elif disabled_reason(home, CODEX):
        why = "Codex launch is disabled: " + str(disabled_reason(home, CODEX)) + " (cardinal-wake reenable codex)"
    elif not (config["codex_path"] and os.access(config["codex_path"], os.X_OK)):
        why = "codex was not found when wake was enabled (run cardinal-wake enable --launch again)"
    if why:
        for e in pending:
            _notify_only(home, sid, binding, e["seq"], rt.kind, why, system, report, dry_run=dry_run)
        return
    claim_and_launch(home, sid, binding, pending[0]["seq"], rt, config, system, report, dry_run=dry_run)


def _notify_only(home, sid, binding, seq, kind, why, system, report, *, dry_run=False) -> None:
    if dry_run:
        _report(system, report, sid, seq, "would notify", why)
        return
    now = system.now()
    with session_lock(home, sid) as got:
        if not got:
            return
        led = read_ledger(home, sid)
        if str(seq) in led["entries"]:
            return
        entry = {"seq": seq, "investigation_id": binding["investigation_id"], "runtime": kind}
        transition(entry, DECISION_SEEN, now)
        transition(entry, NOTIFIED, now, why)
        led["entries"][str(seq)] = entry
        write_ledger(home, sid, led, now)
    _notify(system, binding, sid, NOTIFIED, f"owner decision #{seq} waits for you ({why.split(':')[0]})")
    _report(system, report, sid, seq, "notified", why)


def claim_and_launch(home: Path, sid: str, binding: dict, seq: int, rt: Runtime, config: dict, system: System,
                     report: list, *, dry_run: bool = False) -> str:
    """Claim #seq for `sid` and launch the pinned Codex resume, under the
    session's lock (§5.4). Returns launched | failed | seen | in_flight |
    cap | busy | dry_run."""
    argv = codex_argv(config["codex_path"], rt.sandbox or "", sid, seq)
    if dry_run:
        _report(system, report, sid, seq, "would launch", f"in {rt.sandbox}, cwd from the rollout")
        return "dry_run"
    if system.before_claim is not None:
        system.before_claim(sid, seq)
    with session_lock(home, sid) as got:
        if not got:
            return "busy"
        now = system.now()
        led = read_ledger(home, sid)
        if str(seq) in led["entries"]:
            return "seen"
        if in_flight(led):
            return "in_flight"
        with global_lock(home) as gotg:
            if not gotg:
                return "busy"
            if _claims(led, now) >= SESSION_DAILY_CAP or global_claims(home, now) >= GLOBAL_DAILY_CAP:
                capped = _num(led.get("cap_notified_at"))
                if capped is None or now - capped >= DAY:
                    led["cap_notified_at"] = now
                    write_ledger(home, sid, led, now)
                    _notify(system, binding, sid, "waiting", f"owner decision #{seq}: the daily wake cap is reached")
                _report(system, report, sid, seq, "capped", "the daily wake cap is reached; reconsidered later")
                return "cap"
            entry = {"seq": seq, "investigation_id": binding["investigation_id"], "runtime": CODEX,
                     "sandbox": rt.sandbox, "claimed_at": now, "spawn_errors": 0}
            transition(entry, DECISION_SEEN, now)
            transition(entry, CLAIMED, now)
            led["entries"][str(seq)] = entry
            write_ledger(home, sid, led, now)
        # TODO(feat/storyboard-continuation, Workstream B of resume-and-wake.md
        # §4): once that PR's evidence hold lands, set it here for sid
        # (reason "wake") so the woken run's local evidence survives size
        # eviction while space allows. Not implemented in this PR.
        offsets = rollout_offsets(home, sid)
        log = logs_dir(home) / f"{sid}-{seq}-{int(now)}.log"
        env = child_env(system.environ)
        for _ in range(1 + SPAWN_RETRIES):
            try:
                pid = system.launcher(argv, rt.cwd, env, log)
            except OSError as e:
                entry["spawn_errors"] = int(entry.get("spawn_errors") or 0) + 1
                entry.setdefault("history", []).append({"state": CLAIMED, "at": _iso(system.now()),
                                                        "note": f"spawn error ({type(e).__name__})"})
                write_ledger(home, sid, led, system.now())
                continue
            started = system.process_start(pid)
            entry["child"] = {"pid": pid, "start": started, "launched_at": system.now(), "log": str(log),
                              "offsets": offsets, "sandbox": rt.sandbox}
            transition(entry, RESUMING, system.now(), f"codex exec resume in {rt.sandbox}")
            write_ledger(home, sid, led, system.now())
            _notify(system, binding, sid, RESUMING, f"resuming for owner decision #{seq} in {rt.sandbox}")
            _report(system, report, sid, seq, "launched", f"pid {pid}, {rt.sandbox}")
            return "launched"
        transition(entry, FAILED, system.now(), f"could not start codex ({entry['spawn_errors']} spawn errors)")
        write_ledger(home, sid, led, system.now())
    _notify(system, binding, sid, FAILED, f"could not start codex for owner decision #{seq}")
    _report(system, report, sid, seq, "failed", "spawn errors")
    return "failed"


def settle(home: Path, sid: str, system: System, config: dict, report: list, *, dry_run: bool = False) -> None:
    """Finish the session's in-flight entries (see the module docstring)."""
    led = read_ledger(home, sid)
    if not in_flight(led):
        return
    if dry_run:
        for e in in_flight(led):
            _report(system, report, sid, e.get("seq"), "in flight", str(e.get("state")))
        return
    with session_lock(home, sid, wait=0.5) as got:
        if not got:
            return  # a claim is launching right now
        led = read_ledger(home, sid)
        binding = ie.read_binding(home, sid)
        for key, entry in list(led["entries"].items()):
            if entry.get("state") == CLAIMED:
                now = system.now()
                transition(entry, FAILED, now, "the waker stopped during the launch; not retried")
                write_ledger(home, sid, led, now)
                _notify(system, binding, sid, FAILED, f"owner decision #{entry.get('seq')}: launch interrupted")
                _report(system, report, sid, entry.get("seq"), "failed", "launch interrupted")
            elif entry.get("state") == RESUMING:
                _settle_resuming(home, sid, led, entry, binding, system, config, report)


def _settle_resuming(home, sid, led, entry, binding, system, config, report) -> None:
    now = system.now()
    child = entry.get("child") if isinstance(entry.get("child"), dict) else {}
    seq = entry.get("seq")
    pinned = child.get("sandbox") or entry.get("sandbox")
    offsets = child.get("offsets") if isinstance(child.get("offsets"), dict) else {}
    launched = _num(child.get("launched_at")) or now
    if child_alive(child, system):
        if now - launched <= WAKE_TIMEOUT:
            return
        bad = verify_after(home, sid, offsets, pinned, require_turn=False)
        if bad:
            _disable_codex(home, sid, binding, bad, system, report, now)
        transition(entry, FAILED, now, "still running after 2 h; left running")
        write_ledger(home, sid, led, now)
        _notify(system, binding, sid, FAILED, f"owner decision #{seq}: the resumed session is still running "
                                              "after 2 h (left running)")
        _report(system, report, sid, seq, "failed", "timeout; child left running")
        return
    if "verified" not in entry:
        bad = verify_after(home, sid, offsets, pinned)
        entry["verified"] = bad is None
        if bad:
            entry.setdefault("history", []).append({"state": RESUMING, "at": _iso(now), "note": bad})
            _disable_codex(home, sid, binding, bad, system, report, now)
        entry.setdefault("exited_at", now)
        write_ledger(home, sid, led, now)
    acked = None
    if binding is not None and binding.get("investigation_id") == entry.get("investigation_id"):
        conn, conn_runtime = _connection_for(home, binding, CODEX, system)
        if conn:
            try:
                events, complete = system.fetch(conn, entry["investigation_id"], sid, int(seq),
                                                _client(conn_runtime, config))
                acked = True if acknowledged(events, sid, seq) else (False if complete else None)
            except Exception:
                acked = None
    if acked is None and now - (_num(entry.get("exited_at")) or now) < ACK_CHECK_GRACE:
        return  # the acknowledgment could not be read: asked again next pass
    if acked:
        transition(entry, ACKNOWLEDGED, now, "the session acknowledged the decision")
        words = f"acknowledged owner decision #{seq}"
    else:
        transition(entry, FAILED, now, "the run ended without acknowledging the decision" if acked is False
                   else "the run ended; its acknowledgment could not be read")
        words = f"the run for owner decision #{seq} ended without an acknowledgment (not retried)"
    write_ledger(home, sid, led, now)
    _notify(system, binding, sid, entry["state"], words)
    _report(system, report, sid, seq, entry["state"].lower(), entry.get("reason", ""))


def _disable_codex(home, sid, binding, why, system, report, now) -> None:
    disable_runtime(home, CODEX, f"session {sid[:8]}: {why}", now)
    _notify(system, binding, sid, "disabled", f"Codex wake is disabled: {why}. Run cardinal-wake reenable codex "
                                              "after checking.")
    _report(system, report, sid, None, "codex disabled", why)


# ---------------------------------------------------------------------------
# Install (launchd) and the CLI
# ---------------------------------------------------------------------------

RUNNER = '''#!/usr/bin/env python3
"""cardinal-wake, the stable copy launchd runs (written by `cardinal-wake
enable` with cardinal_core next to it; re-written by every enable)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cardinal_core import session_wake  # noqa: E402

if __name__ == "__main__":
    sys.exit(session_wake.cli_main(sys.argv[1:]))
'''


def install_lib(home: Path, core_dir: Optional[Path] = None) -> Path:
    """Copy cardinal_core (this package) and the runner into
    ~/.cardinal/wake/lib, replacing what was there. Returns the runner."""
    core_dir = core_dir or Path(__file__).resolve().parent
    lib = _private_dir(lib_dir(home))
    tmp = lib / f".cardinal_core.{os.getpid()}.tmp"
    if tmp.exists():
        shutil.rmtree(str(tmp))
    shutil.copytree(str(core_dir), str(tmp), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    dest = lib / "cardinal_core"
    old = lib / f".cardinal_core.{os.getpid()}.old"
    if dest.exists():
        os.replace(str(dest), str(old))
    os.replace(str(tmp), str(dest))
    if old.exists():
        shutil.rmtree(str(old), ignore_errors=True)
    runner = lib / "cardinal-wake"
    tmp_runner = lib / f".cardinal-wake.{os.getpid()}.tmp"
    tmp_runner.write_text(RUNNER, encoding="utf-8")
    os.chmod(str(tmp_runner), 0o700)
    os.replace(str(tmp_runner), str(runner))
    return runner


def plist_bytes(home: Path, python: str, runner: Path, path_env: str) -> bytes:
    log = logs_dir(home) / "launchd.log"
    return plistlib.dumps({
        "Label": PLIST_LABEL,
        "ProgramArguments": [python, str(runner), "run", "--once"],
        "StartInterval": RUN_INTERVAL,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": path_env},
    })


def launchctl_default(args: List[str]) -> Tuple[int, str]:
    try:
        r = subprocess.run([LAUNCHCTL] + args, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def _domain() -> str:
    return f"gui/{os.getuid()}"


def enable(home: Path, *, launch: bool, environ: Mapping[str, str], launchctl: Callable[[List[str]], Tuple[int, str]],
           out: Callable[[str], None], now: float, version: Optional[str] = None,
           runtime: Optional[str] = None, core_dir: Optional[Path] = None) -> int:
    path_env = environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"
    codex = shutil.which("codex", path=path_env)
    codex = os.path.abspath(codex) if codex else None
    python = shutil.which("python3", path=path_env) or sys.executable
    python = os.path.abspath(python)
    runner = install_lib(home, core_dir)
    _private_dir(logs_dir(home))
    old = read_config(home)
    versions = dict(old.get("versions") or {})
    if runtime in RUNTIMES and version:
        versions[runtime] = version
    write_config(home, {"enabled": True, "launch": bool(launch), "codex_path": codex, "python": python,
                        "enabled_at": old.get("enabled_at") if old["enabled"] and old.get("enabled_at") else _iso(now),
                        "versions": versions})
    plist = plist_path(home)
    plist.parent.mkdir(parents=True, exist_ok=True)
    tmp = plist.with_name(f".{plist.name}.tmp")
    tmp.write_bytes(plist_bytes(home, python, runner, path_env))
    os.chmod(str(tmp), 0o644)
    os.replace(str(tmp), str(plist))
    launchctl(["bootout", f"{_domain()}/{PLIST_LABEL}"])
    rc, said = launchctl(["bootstrap", _domain(), str(plist)])
    if rc != 0:
        out(f"cardinal-wake: launchctl bootstrap failed ({said or rc}); the agent is written to {plist} but not "
            "loaded")
        return 1
    out(f"Wake is on: launchd runs {runner} every {RUN_INTERVAL} s ({plist}).")
    out("It wakes a session only for an owner decision you post on its Investigation page, addressed to that "
        "session. Agents cannot post one.")
    if launch:
        out(LAUNCH_TEXT)
        if not codex:
            out("codex was not found on PATH, so nothing can launch yet: install the Codex CLI and run "
                "cardinal-wake enable --launch again.")
        else:
            out(f"Codex: {codex}")
        reason = disabled_reason(home, CODEX)
        if reason:
            out(f"Codex launch is disabled: {reason}. Run cardinal-wake reenable codex after checking.")
    else:
        out("Unattended launch is off: every eligible decision is a notification only (cardinal-wake enable "
            "--launch allows Codex CLI sessions to resume).")
    out(NOTIFY_ONLY_TEXT)
    return 0


def disable(home: Path, *, launchctl: Callable[[List[str]], Tuple[int, str]], out: Callable[[str], None]) -> int:
    launchctl(["bootout", f"{_domain()}/{PLIST_LABEL}"])
    with contextlib.suppress(FileNotFoundError):
        plist_path(home).unlink()
    config = read_config(home)
    if config_path(home).exists():
        config.update(enabled=False, launch=False)
        write_config(home, config)
    out("Wake is off: the launchd agent is removed. Sessions resumed earlier are not stopped.")
    return 0


def status(home: Path, system: System, out: Callable[[str], None]) -> int:
    """The opt-in, disabled runtimes, recent ledger entries and candidate
    sessions; local files only, never decision text."""
    now = system.now()
    c = read_config(home)
    out(f"wake: {'on' if c['enabled'] else 'off'}; unattended launch: {'on' if c['launch'] else 'off'}"
        f"{'; enabled ' + str(c['enabled_at']) if c['enabled'] and c['enabled_at'] else ''}")
    out(f"launchd agent: {'installed' if plist_path(home).exists() else 'not installed'} ({plist_path(home)})")
    out(f"codex: {c['codex_path'] or 'not found'}")
    for r in RUNTIMES:
        reason = disabled_reason(home, r)
        if reason:
            out(f"{r} launch disabled: {reason}")
    rows = []
    for sid in ledger_sids(home):
        for e in read_ledger(home, sid)["entries"].values():
            rows.append((_num(e.get("updated_at")) or 0.0, sid, e))
    rows.sort(key=lambda r: r[0], reverse=True)
    out("recent wakes:" if rows else "recent wakes: none")
    for t, sid, e in rows[:20]:
        out(f"  {_iso(t)} {sid[:8]} #{e.get('seq')} {e.get('state')}"
            + (f": {e.get('reason')}" if e.get("reason") else ""))
    cands = candidate_sessions(home, now)
    out("candidate sessions:" if cands else "candidate sessions: none")
    for sid, b, _ in cands:
        rt = resolve(home, sid)
        out(f"  {sid[:8]} {rt.kind}: {'can launch in ' + str(rt.sandbox) if rt.eligible else rt.reason}")
    return 0


def _plugin_version(plugin_root: Optional[Path]) -> Optional[str]:
    if plugin_root is None:
        return None
    for manifest in glob.glob(str(plugin_root / ".*plugin" / "plugin.json")):
        v = _read_json(Path(manifest)).get("version")
        if isinstance(v, str):
            return v
    return None


def cli_main(argv: Optional[List[str]] = None, *, runtime: Optional[str] = None, plugin_root: Optional[Path] = None,
             home: Optional[Path] = None, system: Optional[System] = None,
             launchctl: Optional[Callable[[List[str]], Tuple[int, str]]] = None,
             platform: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> int:
    """cardinal-wake: enable [--launch] | disable | status | run [--once]
    [--dry-run] | reenable codex. Run it in your own terminal."""
    parser = argparse.ArgumentParser(
        prog="cardinal-wake",
        description="Owner-woken sessions: when you post an owner decision on an Investigation page addressed "
                    "to a stopped session, resume it (Codex CLI sessions that only ran sandboxed) or notify "
                    "you (everything else). Run it in your own terminal, not through an agent.")
    sub = parser.add_subparsers(dest="command")
    p_enable = sub.add_parser("enable", help="install the per-user launchd agent (macOS)")
    p_enable.add_argument("--launch", action="store_true",
                          help="also allow unattended launch of eligible Codex CLI sessions")
    sub.add_parser("disable", help="remove the launchd agent")
    sub.add_parser("status", help="the opt-in, disabled runtimes, recent wakes and candidate sessions")
    p_run = sub.add_parser("run", help="look for addressed owner decisions (launchd runs run --once)")
    p_run.add_argument("--once", action="store_true", help="one pass, then exit")
    p_run.add_argument("--dry-run", action="store_true", help="print what would happen; launch nothing")
    p_re = sub.add_parser("reenable", help="allow a runtime to launch again after a failed sandbox check")
    p_re.add_argument("target", choices=[CODEX])
    args = parser.parse_args(argv)
    home = home or ie.home_dir()
    system = system or System()
    environ = os.environ if environ is None else environ
    launchctl = launchctl or launchctl_default
    platform = platform or sys.platform
    out = system.out
    if args.command in ("enable", "disable") and platform != "darwin":
        out("cardinal-wake: wake runs as a launchd agent, on macOS only")
        return 2
    if args.command == "enable":
        return enable(home, launch=args.launch, environ=environ, launchctl=launchctl, out=out, now=system.now(),
                      version=_plugin_version(plugin_root), runtime=runtime)
    if args.command == "disable":
        return disable(home, launchctl=launchctl, out=out)
    if args.command == "status":
        return status(home, system, out)
    if args.command == "reenable":
        cleared = reenable_runtime(home, args.target)
        out(f"{args.target} launch re-enabled" if cleared else f"{args.target} launch was not disabled")
        return 0
    if args.command == "run":
        _trim_launchd_log(home)
        if args.once or args.dry_run:
            report = run_once(home, system, dry_run=args.dry_run)
            if args.dry_run and not report:
                out("nothing to do: no addressed owner decision is waiting for a stopped session")
            return 0
        while True:  # pragma: no cover - a foreground loop for debugging
            reap()
            run_once(home, system)
            time.sleep(RUN_INTERVAL)
    parser.print_help()
    return 2


def _trim_launchd_log(home: Path) -> None:
    log = logs_dir(home) / "launchd.log"
    with contextlib.suppress(OSError):
        if log.stat().st_size > MAX_LAUNCHD_LOG:
            with open(str(log), "w"):
                pass

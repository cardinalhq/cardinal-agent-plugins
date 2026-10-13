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
     straight from Cardinal (read-investigation-events with the plugin's key,
     that runtime's first, the binding's org), never from the local inbox, so a
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
--skip-git-repo-check, which is never passed); no directory from cwd up to
the work-tree root holds Codex project config (.codex/*.toml or
.codex/hooks.json, a .codex that is a symlink or unreadable: its MCP
servers would start outside the sandbox on resume, proof C-8). Checked
again before every launch.

Other runtimes are notify-only. Each is recognized by its own records
(RECORDS, a glob): a Claude transcript (~/.claude/projects/*/<sid>.jsonl;
gate C-6 not demonstrated: a resumed `claude -p` runs with the user's own
settings; Remote Control is the Claude path); a Gemini CLI chat
(~/.gemini/tmp/*/chats/session-*-<sid[:8]>.json[l] whose head names the
sessionId; its launch is built and off, GEMINI_LAUNCH_PROVEN, see "Gemini
CLI launch" below);
Cursor's records (~/.cursor/projects/*/agent-transcripts/<sid>.*,
~/.cursor/chats/*/<sid>/, best effort). Records of none or of several
runtimes: unknown, still notified. Notifications name the runtime. The
only runtimes that may launch are LAUNCHERS, an explicit allow-list (one
Launcher per runtime: eligibility, pinned argv, snapshot, post-run
verify); adding one is that entry, after its gates are demonstrated.
Rollouts are streamed line by line, bounded
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

Gemini CLI launch (§5.3 Gemini; built, shipped off until GEMINI_LAUNCH_PROVEN):
  <gemini> --resume <ident> -s --approval-mode yolo --skip-trust -p "<WAKE_PROMPT>"
with GEMINI_SANDBOX=sandbox-exec, SEATBELT_PROFILE=restrictive-proxied and
GEMINI_SANDBOX_PROXY_COMMAND = the stable allowlist_proxy.py (CONNECT, 443,
exact hosts; the profile allows only localhost:8877), everything else that
moves the sandbox stripped (GEMINI_SANDBOX_ENV). Eligible only in an
approved root (enable --root; the cwd comes from Gemini's agent-writable
project record), every recorded turn sandboxed (agent-writable: a hint),
a supported auth type, no Gemini configuration that changes the sandbox
(gemini_project_config, fail closed), port 8877 free, no other Gemini wake
in flight. Within SPAWN_VERIFY_WINDOW the child's tree must run
sandbox-exec with the proxied profile, else the tree is killed and Gemini
launch disabled. Inside the sandbox the hooks keep their state under the
relocated home (paths.state_home); the waker reads those bindings as hints
and never writes there.

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
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, List, Mapping, Optional, Tuple

from . import investigation_events as ie
from . import investigation_poller as poller
from . import paths
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
CURSOR = "cursor"
GEMINI = "gemini"
UNKNOWN = "unknown"
RUNTIMES = (CODEX, CLAUDE, CURSOR, GEMINI)   # every adapter with the Investigation layer (all notify)
LABELS = {CODEX: "Codex", CLAUDE: "Claude Code", CURSOR: "Cursor", GEMINI: "Gemini CLI", UNKNOWN: "Unknown-runtime"}
# Why a runtime without a launcher (LAUNCHERS, below) is notify-only.
NOTIFY_ONLY_REASONS = {
    CLAUDE: "Claude Code sessions are notify-only: a resumed claude -p runs with your own settings, so its "
            "sandbox is not guaranteed",
    CURSOR: "Cursor sessions are notify-only: no unattended resume has been shown to pass the wake gates",
    # Proof C-7 (2026-10-12, gemini-cli 0.63.0): its sandbox wraps the whole
    # CLI, hooks included, and every bundled macOS profile allows network.
    GEMINI: "Gemini CLI sessions are notify-only until its sandboxed launch (restrictive-proxied seatbelt behind "
            "an allow-list proxy) passes its live proofs",
}
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
    "own settings, so its sandbox cannot be guaranteed; use `claude --remote-control` instead), Cursor and "
    "Gemini CLI sessions, sessions whose runtime is not recognized, Codex desktop app sessions, any session that "
    "ever ran danger-full-access or with network access, and a session whose window is still open.")
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
    roots = c.get("roots") if isinstance(c.get("roots"), list) else []
    out = {"enabled": c.get("enabled") is True, "launch": c.get("launch") is True,
           "enabled_at": c.get("enabled_at"), "python": c.get("python"),
           "versions": c.get("versions") if isinstance(c.get("versions"), dict) else {},
           "roots": [r for r in roots if isinstance(r, str) and os.path.isabs(r)]}
    for launcher in LAUNCHERS.values():   # <runtime>_path: an absolute executable, or None
        exe = c.get(launcher.executable)
        out[launcher.executable] = exe if isinstance(exe, str) and os.path.isabs(exe) else None
    return out


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


def runtime_in_flight(home: Path, runtime: str) -> bool:
    """Any session's ledger holds an in-flight launch of `runtime`."""
    return any(e.get("runtime") == runtime for sid in ledger_sids(home) for e in in_flight(read_ledger(home, sid)))


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


def process_rows() -> Optional[List[Tuple[int, int, str]]]:
    """(pid, ppid, argv as one string) for every process, or None."""
    try:
        r = subprocess.run([PS, "-A", "-ww", "-o", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5,
                           check=False, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"})
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        fields = line.split(None, 2)
        with contextlib.suppress(ValueError, IndexError):
            rows.append((int(fields[0]), int(fields[1]), fields[2] if len(fields) > 2 else ""))
    return rows


def port_busy(port: int) -> bool:
    """Something on this machine accepts connections on `port` (IPv4 or IPv6
    loopback)."""
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port))):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex(addr) == 0:
                    return True
        except OSError:
            continue
    return False


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


def agent_connection(runtime: str, home: Path) -> dict:
    """A plugin's connection from its managed files under ~/.<runtime>
    (cardinal.json mcp_url + cardinal-secrets.json mcp_api_key; Codex,
    Cursor and Gemini CLI), never the environment."""
    from .evidence_promote import agent_paths_connection
    from .paths import AgentPaths
    try:
        return agent_paths_connection(AgentPaths(home=home / f".{runtime}"))(home, {}) or {}
    except Exception:
        return {}


def runtime_connection(runtime: str, home: Path) -> dict:
    if runtime == CLAUDE:
        return claude_connection(home)
    return agent_connection(runtime, home) if runtime in RUNTIMES else {}


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
    environment is derived from it); out(line) (the report);
    process_rows() -> [(pid, ppid, argv)] | None; kill(pid, signal);
    sleep(seconds); port_busy(port) -> bool.
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
                 out: Optional[Callable[[str], None]] = None,
                 process_rows: Optional[Callable[[], Optional[list]]] = None,
                 kill: Optional[Callable[[int, int], None]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 port_busy: Optional[Callable[[int], bool]] = None):
        self.process_rows = process_rows or globals()["process_rows"]
        self.kill = kill or os.kill
        self.sleep = sleep or time.sleep
        self.port_busy = port_busy or globals()["port_busy"]
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
        self.extra: dict = {}   # launcher-specific facts (Gemini: the resume ident, the proxy's hosts)


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


GEMINI_HEAD = 64 * 1024   # bytes of a Gemini CLI chat file read to find its sessionId


def gemini_chats(home: Path, sid: str) -> List[Path]:
    """Gemini CLI's chat records for `sid`:
    ~/.gemini/tmp/<project>/chats/session-<time>-<sid[:8]>.json[l] whose
    head names sessionId == sid (only GEMINI_HEAD bytes are read)."""
    if not ie.valid_session(sid) or len(sid) < 8:
        return []
    pattern = str(home / ".gemini" / "tmp" / "*" / "chats" / f"session-*-{glob.escape(sid[:8])}.json*")
    want = re.compile(rb'"sessionId"\s*:\s*"' + re.escape(sid.encode()) + rb'"')
    out = []
    for p in sorted(glob.glob(pattern)):
        if not p.endswith((".json", ".jsonl")):
            continue
        with contextlib.suppress(OSError):
            with open(p, "rb") as f:
                if want.search(f.read(GEMINI_HEAD)):
                    out.append(Path(p))
    return out


def cursor_records(home: Path, sid: str) -> List[Path]:
    """Cursor's records for `sid` (best effort: Cursor documents no stable
    store): ~/.cursor/projects/*/agent-transcripts/<sid>.* and the files of
    ~/.cursor/chats/*/<sid>/. Not found: the runtime is unknown (still a
    notification)."""
    if not ie.valid_session(sid):
        return []
    s = glob.escape(sid)
    found = glob.glob(str(home / ".cursor" / "projects" / "*" / "agent-transcripts" / f"{s}.*"))
    found += glob.glob(str(home / ".cursor" / "chats" / "*" / s / "*"))
    return sorted(Path(p) for p in found if os.path.isfile(p))


# Each runtime's own records of a session (cheap: a glob; Gemini reads a
# file head). The records name the runtime and their mtimes are a liveness
# signal; nothing here makes a session launchable (LAUNCHERS does).
RECORDS = {CODEX: codex_rollouts, CLAUDE: claude_transcripts, CURSOR: cursor_records, GEMINI: gemini_chats}


def runtime_kind(home: Path, sid: str) -> Runtime:
    """The runtime by its own records. None found, or records of more than
    one runtime: unknown (notify-only)."""
    found = {r: files for r, files in ((r, find(home, sid)) for r, find in RECORDS.items()) if files}
    if len(found) > 1:
        return Runtime(UNKNOWN, [p for files in found.values() for p in files],
                       reason=f"records of {' and '.join(LABELS[r] for r in found)} carry this session id: "
                              "notify-only")
    if not found:
        return Runtime(UNKNOWN, [], reason="no Codex, Claude Code, Cursor or Gemini CLI records for this session "
                                           "on this machine: notify-only")
    (kind, files), = found.items()
    return Runtime(kind, files, reason=NOTIFY_ONLY_REASONS.get(kind, "not yet checked"))


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


def _git_root(cwd: str) -> Optional[Path]:
    """The git work tree's root containing `cwd` (the nearest directory with
    a .git entry, cwd included), or None."""
    p = Path(cwd)
    for d in (p, *p.parents):
        if os.path.lexists(str(d / ".git")):
            return d
    return None


def _git_work_tree(cwd: str) -> bool:
    return _git_root(cwd) is not None


def codex_project_config(cwd: str) -> Optional[str]:
    """Where Codex project config sits between `cwd` and its git work-tree
    root (both included), or None. A .codex/ holding any *.toml (config.toml
    can declare MCP servers, which a resumed `codex exec` starts OUTSIDE its
    sandbox: proof C-8) or hooks.json counts; so does a .codex that is not a
    plain directory (a symlink is never followed) or cannot be read (fail
    closed). A sandboxed agent cannot create .codex inside workspace-write;
    a cloned repo or an unsandboxed tool can."""
    root = _git_root(cwd)
    if root is None:
        return "no git work tree"
    d = Path(cwd)
    for current in (d, *d.parents):
        entry = current / ".codex"
        try:
            st = os.lstat(str(entry))
        except FileNotFoundError:
            st = None
        except OSError:
            return str(entry)
        if st is not None:
            if not stat.S_ISDIR(st.st_mode):
                return str(entry)
            try:
                names = os.listdir(str(entry))
            except OSError:
                return str(entry)
            if any(n.endswith(".toml") or n == "hooks.json" for n in names):
                return str(entry)
        if current == root:
            return None
    return None


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
    found = codex_project_config(cwd)
    if found:
        rt.reason = ("the project has its own Codex config (a .codex with config or hooks between the working "
                     "directory and the repository root; its MCP servers would run outside the sandbox): "
                     "notify-only")
        return rt
    rt.eligible, rt.reason, rt.cwd, rt.sandbox = True, "eligible", cwd, turns[-1][0]
    return rt


def resolve(home: Path, sid: str) -> Runtime:
    """The session's runtime and, for a runtime with a launcher, whether it
    is eligible; any other runtime is notify-only."""
    rt = runtime_kind(home, sid)
    launcher = active_launcher(rt.kind)
    return launcher.eligibility(home, sid, rt, read_config(home)) if launcher else rt


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
# Gemini CLI launch (built, shipped disabled: GEMINI_LAUNCH_PROVEN)
# ---------------------------------------------------------------------------

# The one switch: True only once every Gemini gate in §6 has passed live
# (C-9 is proven; C-10..C-14 are pending). While False the Gemini launcher is
# never used and Gemini CLI sessions are notify-only.
GEMINI_LAUNCH_PROVEN = False
GEMINI_PROFILE = "restrictive-proxied"   # seatbelt: writes to the workspace/temp/cache only, network via :8877 only
GEMINI_PROXY_PORT = 8877                 # the only outbound port the proxied profiles allow
GEMINI_API_HOST = "generativelanguage.googleapis.com"
# The hosts each supported auth type needs besides the Gemini API (gemini-cli
# 0.63.0 bundle: Code Assist and the OAuth token refresh). Anything else
# (Vertex AI, Cloud Shell, ADC) is not supported for an unattended wake.
GEMINI_AUTH_HOSTS = {
    "gemini-api-key": (),
    "oauth-personal": ("cloudcode-pa.googleapis.com", "oauth2.googleapis.com"),
}
SPAWN_VERIFY_WINDOW = 20.0
SPAWN_VERIFY_POLL = 0.5
# Environment that changes Gemini CLI's sandbox, its proxy, where it reads
# settings, or opens a debugger: never inherited by a woken Gemini (the
# waker sets the first three itself), and an env file Gemini may load that
# sets any of them makes the session notify-only (dotenv fills only unset
# variables, so a stripped one could come back from a .env: SANDBOX set
# there would make Gemini skip its sandbox entirely).
GEMINI_SANDBOX_ENV = frozenset({
    "GEMINI_SANDBOX", "SEATBELT_PROFILE", "GEMINI_SANDBOX_PROXY_COMMAND", "SANDBOX", "SANDBOX_FLAGS",
    "GEMINI_SANDBOX_IMAGE", "BUILD_SANDBOX", "SANDBOX_PORTS", "SANDBOX_MOUNTS", "SANDBOX_ENV", "GEMINI_CLI_HOME",
    "GEMINI_CLI_SYSTEM_SETTINGS_PATH", "GEMINI_CLI_SYSTEM_DEFAULTS_PATH", "GEMINI_CLI_TRUST_WORKSPACE",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    "NODE_OPTIONS", "DEBUG", "DEBUG_PORT",
})
GEMINI_SANDBOX_ENV_PREFIXES = ("SEATBELT_", "GEMINI_SANDBOX")
# Project files that change the sandbox (gemini-cli 0.63.0: a custom
# seatbelt profile is looked up as .gemini/sandbox-macos-<name>.sb; the
# container sandbox reads sandbox.Dockerfile / sandbox.bashrc).
GEMINI_PROJECT_SANDBOX_FILES = re.compile(r"sandbox-macos-.*\.sb|sandbox\.Dockerfile|sandbox\.bashrc")
# Settings keys that change the sandbox or what it may write, in any
# settings file between cwd and the work-tree root (tools.sandbox* and
# security.* are any value; context.includeDirectories become writable
# INCLUDE_DIR_n inside the sandbox). The command keys (MCP servers, hooks,
# tool discovery) are refused too until a live proof shows Gemini's outer,
# unsandboxed process runs none of them before it enters the sandbox (C-13).
GEMINI_PROJECT_SETTINGS_KEYS = (
    ("tools", "sandbox"), ("tools", "sandboxAllowedPaths"), ("tools", "sandboxNetworkAccess"), ("security",),
    ("context", "includeDirectories"), ("mcpServers",), ("hooks",), ("tools", "discoveryCommand"),
    ("tools", "callCommand"), ("mcp", "serverCommand"),
)
# In the user's and the system's settings only what widens the sandbox.
GEMINI_USER_SETTINGS_KEYS = (
    ("context", "includeDirectories"), ("tools", "sandboxAllowedPaths"), ("tools", "sandboxNetworkAccess"),
    ("security", "toolSandboxing"),
)
GEMINI_SYSTEM_SETTINGS = ("/Library/Application Support/GeminiCli/settings.json",
                          "/Library/Application Support/GeminiCli/system-defaults.json")
MAX_CONFIG_READ = 256 * 1024
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _read_small(path: Path) -> Optional[bytes]:
    """A regular, non-symlink file's bytes (at most MAX_CONFIG_READ), or None
    when it is anything else or cannot be read (callers fail closed)."""
    try:
        st = os.lstat(str(path))
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CONFIG_READ:
            return None
        with open(str(path), "rb") as f:
            return f.read(MAX_CONFIG_READ + 1)
    except OSError:
        return None


def _env_file_sets_sandbox(path: Path) -> Optional[str]:
    """Why env file `path` disqualifies a Gemini wake, or None (absent, or
    sets nothing in GEMINI_SANDBOX_ENV)."""
    if not os.path.lexists(str(path)):
        return None
    data = _read_small(path)
    if data is None:
        return f"{path} cannot be checked (a symlink, too large or unreadable)"
    for line in data.decode("utf-8", "replace").splitlines():
        m = _ENV_LINE_RE.match(line)
        if m and (m.group(1) in GEMINI_SANDBOX_ENV or m.group(1).startswith(GEMINI_SANDBOX_ENV_PREFIXES)):
            return f"{path} sets {m.group(1)}"
    return None


def _strip_json_comments(text: str) -> str:
    """JSON with // and /* */ comments (Gemini's settings) as plain JSON."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _settings_sets(path: Path, keys, *, any_value: bool) -> Optional[str]:
    """Why settings file `path` disqualifies a Gemini wake, or None.
    any_value: a key present at all counts; else only a non-empty / true
    value."""
    if not os.path.lexists(str(path)):
        return None
    data = _read_small(path)
    if data is None:
        return f"{path} cannot be checked (a symlink, too large or unreadable)"
    try:
        settings = json.loads(_strip_json_comments(data.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        return f"{path} cannot be parsed"
    if not isinstance(settings, dict):
        return f"{path} is not a settings object"
    for key in keys:
        node: Any = settings
        for part in key:
            node = node.get(part, None) if isinstance(node, dict) else None
        if node is None:
            continue
        if any_value or (node not in (False, [], {}, "")):
            return f"{path} sets {'.'.join(key)}"
    return None


def gemini_project_config(cwd: str, home: Path) -> Optional[str]:
    """Why Gemini CLI's own configuration around `cwd` disqualifies an
    unattended wake (fail closed), or None. Checked: from cwd up to the git
    work-tree root, any .gemini/ that is not a plain directory or cannot be
    read, holds a custom sandbox file (GEMINI_PROJECT_SANDBOX_FILES), or a
    settings.json setting GEMINI_PROJECT_SETTINGS_KEYS; every env file
    Gemini may load (.gemini/.env and .env from cwd up to /, then
    ~/.gemini/.env and ~/.env: a superset of the one it picks) setting
    GEMINI_SANDBOX_ENV; the user's and system settings widening the sandbox
    (GEMINI_USER_SETTINGS_KEYS)."""
    root = _git_root(cwd)
    if root is None:
        return "no git work tree"
    d = Path(cwd)
    for current in (d, *d.parents):
        if current == root or root in current.parents:
            entry = current / ".gemini"
            try:
                st = os.lstat(str(entry))
            except FileNotFoundError:
                st = None
            except OSError:
                return f"{entry} cannot be checked"
            if st is not None:
                if not stat.S_ISDIR(st.st_mode):
                    return f"{entry} is not a plain directory"
                try:
                    names = os.listdir(str(entry))
                except OSError:
                    return f"{entry} cannot be read"
                for name in names:
                    if GEMINI_PROJECT_SANDBOX_FILES.fullmatch(name):
                        return f"{entry / name} customizes Gemini's sandbox"
                why = _settings_sets(entry / "settings.json", GEMINI_PROJECT_SETTINGS_KEYS, any_value=True)
                if why:
                    return why
        for env_file in (current / ".gemini" / ".env", current / ".env"):
            why = _env_file_sets_sandbox(env_file)
            if why:
                return why
    for env_file in (Path(home) / ".gemini" / ".env", Path(home) / ".env"):
        why = _env_file_sets_sandbox(env_file)
        if why:
            return why
    for settings in [Path(home) / ".gemini" / "settings.json"] + [Path(p) for p in GEMINI_SYSTEM_SETTINGS]:
        why = _settings_sets(settings, GEMINI_USER_SETTINGS_KEYS, any_value=False)
        if why:
            return why
    return None


def gemini_auth(home: Path, environ: Mapping[str, str]) -> Tuple[Optional[tuple], str]:
    """(the extra hosts the user's Gemini auth needs, "") or (None, why it
    is not supported for an unattended wake). Inside the sandbox Gemini
    cannot read its API key from the macOS keychain, so an API-key user
    needs GEMINI_API_KEY in ~/.gemini/.env (only the name is checked, never
    the value)."""
    settings_path = Path(home) / ".gemini" / "settings.json"
    data = _read_small(settings_path) if os.path.lexists(str(settings_path)) else None
    selected = None
    if data is not None:
        with contextlib.suppress(ValueError, UnicodeDecodeError, AttributeError):
            selected = (json.loads(_strip_json_comments(data.decode("utf-8"))).get("security") or {}) \
                .get("auth", {}).get("selectedType")
    if selected not in GEMINI_AUTH_HOSTS:
        return None, (f"Gemini auth {selected!r} is not supported for an unattended wake (Gemini API key or "
                      "Sign in with Google only)")
    if selected == "gemini-api-key" and not environ.get("GEMINI_API_KEY"):
        env = _read_small(Path(home) / ".gemini" / ".env") or b""
        names = {m.group(1) for m in (_ENV_LINE_RE.match(line) for line in env.decode("utf-8", "replace")
                                      .splitlines()) if m}
        if "GEMINI_API_KEY" not in names:
            return None, ("Gemini reads its API key from the keychain, which its sandbox cannot reach: put "
                          "GEMINI_API_KEY in ~/.gemini/.env")
    return GEMINI_AUTH_HOSTS[selected], ""


def gemini_hosts(home: Path, auth_hosts: tuple) -> List[str]:
    """The proxy's allow-list: the Gemini API, the auth hosts, and the
    Cardinal hosts of the Gemini plugin's connection (~/.gemini/cardinal.json
    mcp_url and ingest_endpoint)."""
    import urllib.parse
    from .allowlist_proxy import valid_host
    hosts = [GEMINI_API_HOST, *auth_hosts]
    state = _read_json(Path(home) / ".gemini" / "cardinal.json")
    for key in ("mcp_url", "ingest_endpoint"):
        url = state.get(key)
        if isinstance(url, str) and url.startswith("https://"):
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
            if valid_host(host):
                hosts.append(host)
    return list(dict.fromkeys(hosts))


def gemini_project_root(chat: Path) -> Optional[str]:
    """The project directory Gemini recorded for a chat file
    (~/.gemini/tmp/<project>/.project_root): agent-writable, hence the
    approved roots."""
    data = _read_small(chat.parent.parent / ".project_root")
    if data is None:
        return None
    text = data.decode("utf-8", "replace").strip()
    return text if text and "\n" not in text and os.path.isabs(text) else None


def resolve_gemini_resume_ident(sid: str, cwd: str) -> Optional[str]:
    """What `gemini --resume` is given for session `sid` started in `cwd`.

    LIVE-PROOF HOOK (C-10): gemini-cli 0.63.0's --resume help names "latest"
    or an index; whether it takes the session UUID is unproven. This passes
    the UUID. If the live run shows it does not, implement the fallback
    here: run `<gemini> --list-sessions` in `cwd` (read-only), find the line
    naming `sid`, and return its index as a string of digits; return None
    when it is not listed (the session is then not eligible). Whatever this
    returns is never an option (never starts with "-")."""
    return sid if UUID_RE.fullmatch(sid or "") else None


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def approved_root_problem(path: str, home: Path) -> Optional[str]:
    """Why `path` (a realpath) may not be an approved root or a Gemini wake
    directory: the filesystem root, the home directory or any ancestor of it."""
    real_home = os.path.realpath(str(home))
    if path == os.sep or path == real_home or _within(real_home, path):
        return "it is your home directory or above it"
    return None


def gemini_eligibility(home: Path, sid: str, rt: Optional[Runtime] = None, config: Optional[dict] = None) -> Runtime:
    """rt (runtime_kind) with eligible / reason / cwd / sandbox / extra
    filled in for a Gemini CLI session (§5.3 Gemini). Its records are
    agent-writable, so what bounds a wake is the approved root, the proxy
    and the runtime sandbox check, not the session history."""
    rt = rt or runtime_kind(home, sid)
    config = config or read_config(home)
    if rt.kind != GEMINI:
        return rt
    rt.eligible, rt.cwd, rt.sandbox = False, None, None
    if not UUID_RE.fullmatch(sid):
        rt.reason = "the session id is not a Gemini CLI UUID: notify-only"
        return rt
    roots = [r for r in config.get("roots") or [] if isinstance(r, str) and os.path.isabs(r)]
    if not roots:
        rt.reason = "no approved root for Gemini wakes (cardinal-wake enable --root PATH): notify-only"
        return rt
    projects = {gemini_project_root(p) for p in rt.files}
    if len(projects) != 1 or None in projects:
        rt.reason = "Gemini's project record for this session is missing or ambiguous: notify-only"
        return rt
    cwd = projects.pop()
    if not os.path.isdir(cwd):
        rt.reason = "the session's working directory is gone: notify-only"
        return rt
    real = os.path.realpath(cwd)
    if approved_root_problem(real, home):
        rt.reason = "the session's working directory is your home directory or above it: notify-only"
        return rt
    if not any(_within(real, root) for root in roots):
        rt.reason = "the session's working directory is not inside an approved root: notify-only"
        return rt
    if not _git_work_tree(real):
        rt.reason = "the session's working directory is not a git work tree: notify-only"
        return rt
    plain_home, boxed_home = state_homes(home)
    plain, boxed = ie.sandbox_turns(plain_home, sid), ie.sandbox_turns(boxed_home, sid)
    if plain["unsandboxed"] or plain["sandboxed"] or boxed["unsandboxed"] or ie.read_binding(plain_home, sid):
        rt.reason = "the session ran outside Gemini's sandbox at least once: notify-only"
        return rt
    if boxed["sandboxed"] < 1:
        rt.reason = "no sandboxed turn is recorded for this session: notify-only"
        return rt
    auth_hosts, why = gemini_auth(home, os.environ)
    if auth_hosts is None:
        rt.reason = why + ": notify-only"
        return rt
    found = gemini_project_config(real, home)
    if found:
        rt.reason = f"Gemini configuration changes its sandbox ({found}): notify-only"
        return rt
    ident = resolve_gemini_resume_ident(sid, real)
    if not ident or ident.startswith("-"):
        rt.reason = "Gemini cannot resume this session by id: notify-only"
        return rt
    rt.eligible, rt.reason, rt.cwd, rt.sandbox = True, "eligible", real, GEMINI_PROFILE
    rt.extra = {"ident": ident, "hosts": gemini_hosts(home, auth_hosts)}
    return rt


def gemini_argv(gemini_path: str, rt: Runtime, sid: str, seq: int) -> List[str]:
    """The pinned Gemini launch: resumed in its seatbelt sandbox (-s, the
    proxied profile from the environment), approvals off inside it (yolo:
    the sandbox and the allow-list proxy are the boundary, as workspace-write
    with approval never is for Codex), workspace trust skipped (headless; the
    project guard has checked what trust would load). Never --yolo / -y
    without -s, --sandbox-image or --include-directories."""
    if not (isinstance(gemini_path, str) and os.path.isabs(gemini_path)):
        raise ValueError("gemini must be an absolute path")
    if not UUID_RE.fullmatch(sid or ""):
        raise ValueError(f"not a Gemini session id: {sid!r}")
    ident = rt.extra.get("ident")
    if not (isinstance(ident, str) and (UUID_RE.fullmatch(ident) or ident.isdigit())):
        raise ValueError(f"not a resume ident: {ident!r}")
    if rt.sandbox != GEMINI_PROFILE:
        raise ValueError(f"not the wake profile: {rt.sandbox!r}")
    return [gemini_path, "--resume", ident, "-s", "--approval-mode", "yolo", "--skip-trust", "-p", wake_prompt(seq)]


def proxy_script(home: Path) -> Path:
    return lib_dir(home) / "cardinal_core" / "allowlist_proxy.py"


def gemini_env(environ: Mapping[str, str], config: dict, home: Path, log: Path, rt: Runtime) -> dict:
    """The woken Gemini's environment: child_env without anything in
    GEMINI_SANDBOX_ENV (or a SEATBELT_* / GEMINI_SANDBOX* name), plus the
    seatbelt sandbox, the proxied profile and the allow-list proxy command
    (the stable copy in ~/.cardinal/wake/lib, logging to this wake's log)."""
    env = {k: v for k, v in child_env(environ).items()
           if k not in GEMINI_SANDBOX_ENV and not k.startswith(GEMINI_SANDBOX_ENV_PREFIXES)}
    hosts = rt.extra.get("hosts") or [GEMINI_API_HOST]
    python = config.get("python") if isinstance(config.get("python"), str) and os.path.isabs(config["python"]) \
        else sys.executable
    env["GEMINI_SANDBOX"] = "sandbox-exec"
    env["SEATBELT_PROFILE"] = GEMINI_PROFILE
    env["GEMINI_SANDBOX_PROXY_COMMAND"] = " ".join(shlex.quote(a) for a in (
        [python, str(proxy_script(home)), "--log", str(log), "--port", str(GEMINI_PROXY_PORT)] + list(hosts)))
    return env


def gemini_preflight(home: Path, config: dict, system: "System") -> Optional[str]:
    """Why a Gemini launch cannot start now (a notification instead), or None."""
    if not proxy_script(home).is_file():
        return "the allow-list proxy is not installed (run cardinal-wake enable again)"
    if system.port_busy(GEMINI_PROXY_PORT):
        return f"port {GEMINI_PROXY_PORT} is in use, so the allow-list proxy cannot start"
    return None


_SEATBELT_RE = re.compile(r"(?:^|\s)-f\s+(\S*" + re.escape(GEMINI_PROFILE) + r"\S*\.sb)(?:\s|$)")


def _descendants(rows: list, pid: int) -> List[Tuple[int, int, str]]:
    children: dict = {}
    for row in rows:
        children.setdefault(row[1], []).append(row)
    out, todo, seen = [], [pid], {pid}
    while todo:
        for row in children.get(todo.pop(), []):
            if row[0] not in seen:
                seen.add(row[0])
                out.append(row)
                todo.append(row[0])
    return out


def seatbelt_observed(rows: list, pid: int) -> bool:
    """`pid`'s process tree runs sandbox-exec with the wake profile."""
    for _, _, args in [r for r in rows if r[0] == pid] + _descendants(rows, pid):
        first = args.split(None, 1)[0] if args.strip() else ""
        if os.path.basename(first) == "sandbox-exec" and _SEATBELT_RE.search(args):
            return True
    return False


def gemini_verify_spawn(home: Path, sid: str, pid: int, system: "System") -> Optional[str]:
    """None once `pid`'s tree runs sandbox-exec -f <...restrictive-proxied...>
    (within SPAWN_VERIFY_WINDOW); else the whole tree is killed and the
    reason returned (the caller disables Gemini launch). No fallback."""
    deadline = system.now() + SPAWN_VERIFY_WINDOW
    seen: List[Tuple[int, int, str]] = []
    while True:
        rows = system.process_rows()
        if rows:
            seen = [r for r in rows if r[0] == pid] + _descendants(rows, pid) or seen
            if seatbelt_observed(rows, pid):
                return None
        if system.now() >= deadline:
            break
        system.sleep(SPAWN_VERIFY_POLL)
    for victim in [pid] + [r[0] for r in seen if r[0] != pid]:
        with contextlib.suppress(OSError):
            system.kill(victim, signal.SIGKILL)
    with contextlib.suppress(OSError):
        system.kill(-pid, signal.SIGKILL)   # its process group (it was started in its own session)
    return (f"Gemini's {GEMINI_PROFILE} seatbelt sandbox was not observed within {int(SPAWN_VERIFY_WINDOW)} s; "
            "the woken process tree was killed")


def gemini_snapshot(home: Path, sid: str) -> dict:
    plain_home, boxed_home = state_homes(home)
    plain, boxed = ie.sandbox_turns(plain_home, sid), ie.sandbox_turns(boxed_home, sid)
    return {"sandboxed": boxed["sandboxed"], "unsandboxed": plain["unsandboxed"] + boxed["unsandboxed"]
            + plain["sandboxed"]}


def gemini_verify_after(home: Path, sid: str, before: dict, pinned: str, *, require_turn: bool = True) -> Optional[str]:
    """None when the woken run recorded only sandboxed turns (and, with
    require_turn, at least one). The record is agent-writable: a hint
    beside the runtime check, never the boundary."""
    now = gemini_snapshot(home, sid)
    if now["unsandboxed"] > int(before.get("unsandboxed") or 0):
        return "the woken run recorded a turn outside Gemini's sandbox"
    if require_turn and now["sandboxed"] <= int(before.get("sandboxed") or 0):
        return "the woken run recorded no sandboxed turn, so its sandbox could not be confirmed"
    return None


class Launcher:
    """How the waker may launch one runtime once all three gates of §6 are
    demonstrated for it (cannot arm itself, sandbox enforced on resume, one
    execution per decision). Everything runtime-specific about a launch:

      executable: the config key of its absolute path (resolved on PATH at
        enable time; `binary` is the name looked up);
      eligibility(home, sid, rt, config) -> rt with eligible / reason / cwd
        / sandbox / extra;
      argv(executable, rt, sid, seq) -> the pinned argv (golden-tested);
      env(environ, config, home, log, rt) -> the child's environment;
      preflight(home, config, system) -> None, or why it cannot start now
        (a notification);
      exclusive: at most one in-flight launch of this runtime machine-wide;
      snapshot(home, sid) -> what verify needs from before the launch;
      after_spawn(home, sid, pid, system) -> None, or why the started child
        is not in its sandbox (it has been killed; the runtime is disabled);
      verify(home, sid, snapshot, pinned, require_turn) -> None, or why the
        woken run did not stay in the pinned sandbox (that disables the
        runtime: ~/.cardinal/wake/disabled/<runtime>);
      proven: False keeps the launcher off (its sessions notify-only).

    Adding a runtime is one Launcher in LAUNCHERS; it launches only once
    `proven` is True, after its gates are demonstrated."""

    def __init__(self, runtime: str, binary: str, eligibility, argv, snapshot, verify, can_launch: str, *,
                 env=None, preflight=None, after_spawn=None, exclusive: bool = False, proven: bool = True):
        self.runtime, self.binary = runtime, binary
        self.executable = f"{runtime}_path"
        self.eligibility, self.argv, self.snapshot, self.verify = eligibility, argv, snapshot, verify
        self.can_launch = can_launch
        self.env = env or (lambda environ, config, home, log, rt: child_env(environ))
        self.preflight = preflight or (lambda home, config, system: None)
        self.after_spawn = after_spawn or (lambda home, sid, pid, system: None)
        self.exclusive, self.proven = exclusive, proven


GEMINI_LAUNCH_TEXT = (
    "Gemini CLI sessions in an approved root (cardinal-wake enable --root PATH) that only ever ran in Gemini's "
    "sandbox, resumed with -s in the restrictive-proxied seatbelt profile behind an allow-list proxy.")

# The runtimes that may be launched unattended, an explicit allow-list.
# Codex passed its gates (§6 C-1..C-5, C-8). Gemini CLI's launch is built
# and off (GEMINI_LAUNCH_PROVEN) until its live gates pass. Claude Code did
# not pass (C-6) and Cursor has no demonstrated launch: notify-only.
LAUNCHERS = {
    CODEX: Launcher(CODEX, "codex", lambda home, sid, rt, config: codex_eligibility(home, sid, rt),
                    lambda exe, rt, sid, seq: codex_argv(exe, rt.sandbox or "", sid, seq),
                    rollout_offsets, verify_after, LAUNCH_TEXT),
    GEMINI: Launcher(GEMINI, "gemini", gemini_eligibility, gemini_argv, gemini_snapshot, gemini_verify_after,
                     GEMINI_LAUNCH_TEXT, env=gemini_env, preflight=gemini_preflight,
                     after_spawn=gemini_verify_spawn, exclusive=True, proven=GEMINI_LAUNCH_PROVEN),
}


def active_launcher(runtime: str) -> Optional[Launcher]:
    """The runtime's launcher when it may launch (proven), else None."""
    launcher = LAUNCHERS.get(runtime)
    return launcher if launcher is not None and launcher.proven else None


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------

def live_signals(home: Path, sid: str, rt: Runtime, system: System, now: float,
                 ledger: Optional[dict] = None, state: Optional[Path] = None) -> List[str]:
    """Why the session may still be running (empty: it is stopped). The
    poller status and .active of both state homes count (state: unused,
    kept for callers; any home saying live is enough)."""
    why = []
    led = ledger if ledger is not None else read_ledger(home, sid)
    if any(isinstance(e.get("child"), dict) and child_alive(e["child"], system) for e in led["entries"].values()):
        why.append("a run the waker started is still going")
    homes = state_homes(home)
    for h in homes:
        st = poller.read_status(h, sid)
        anchor = st.get("anchor")
        if isinstance(anchor, int) and not isinstance(anchor, bool) and anchor > 1 and system.alive(anchor):
            started = system.process_start(anchor)
            try:
                written = poller.status_path(h, sid).stat().st_mtime
            except OSError:
                written = None
            if started is None or written is None or started < written:
                why.append("its agent process is running")
                break
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
    if any(now - ie.last_activity(h, sid) <= LIVE_WINDOW for h in homes):
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

def state_homes(home: Path) -> Tuple[Path, Path]:
    """Where hooks keep per-session Cardinal state: the user's home
    (~/.cardinal) and the relocated home of sessions run inside a seatbelt
    sandbox (paths.sandboxed_home). Both hold agent-writable hints."""
    home = paths.unsandboxed_home(Path(home))
    return home, paths.sandboxed_home(home)


def candidate_sessions(home: Path, now: float) -> List[Tuple[str, dict, float, Path]]:
    """(sid, binding, binding mtime, state home) of sessions that may be
    woken: touched within CANDIDATE_WINDOW, the investigation's author,
    owner_decision on. Bindings under the relocated sandboxed home count too
    (as untrusted as any binding); a session id bound in both is taken from
    ~/.cardinal only."""
    out, seen = [], set()
    for state in state_homes(home):
        d = ie.sessions_dir(state)
        try:
            names = os.listdir(str(d))
        except OSError:
            continue
        for name in sorted(names):
            sid = name[:-5] if name.endswith(".json") else ""
            if not ie.valid_session(sid) or sid in seen:
                continue
            try:
                mtime = (d / name).stat().st_mtime
            except OSError:
                continue
            if now - mtime > CANDIDATE_WINDOW:
                continue
            b = ie.read_binding(state, sid)
            if b is None or b.get("is_author") is not True or not ie.capability(b, "owner_decision"):
                continue
            seen.add(sid)
            out.append((sid, b, mtime, state))
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
    """The X-Cardinal-Client of the plugin whose key reads: <name>/<version
    of that plugin when it ran enable>, the name alone otherwise."""
    version = (config.get("versions") or {}).get(runtime)
    from .evidence import client_string
    try:
        return client_string("claude-plugin" if runtime == CLAUDE else runtime, version)
    except ValueError:
        return "cardinal-wake"


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

def _notify(system: System, binding: Optional[dict], sid: str, state: str, words: str,
            runtime: str = UNKNOWN) -> None:
    """The runtime, the session's short id, the investigation and the state;
    never decision text."""
    inv = (binding or {}).get("investigation_id")
    link = (binding or {}).get("investigation_url")
    where = link if isinstance(link, str) and _LINK_RE.fullmatch(link) else (inv if ie.valid_investigation(inv) else "")
    label = LABELS.get(runtime, LABELS[UNKNOWN])
    msg = f"{label} session {sid[:8]}: {state.lower()} - {words}" + (f" {where}" if where else "")
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
    for sid, binding, mtime, state in candidate_sessions(home, now):
        if now - mtime > RECENT_WINDOW and 0 <= now - (_num(polls.get(sid)) or 0.0) < OLD_CHECK_EVERY:
            continue
        polls[sid] = now
        try:
            consider(home, sid, binding, system, config, report, dry_run=dry_run, state=state)
        except Exception as e:
            _report(system, report, sid, None, "error", f"{type(e).__name__}")
    if not dry_run:
        with contextlib.suppress(OSError, ValueError):
            _private_dir(wake_dir(home))
            ie.write_json(poll_state_path(home), {k: v for k, v in polls.items()
                                                   if isinstance(v, (int, float)) and now - v < CANDIDATE_WINDOW})
    return report


def consider(home: Path, sid: str, binding: dict, system: System, config: dict, report: list, *,
             dry_run: bool = False, state: Optional[Path] = None) -> None:
    """One candidate session (§5.2). state: the home its binding was found
    under (state_homes); the waker's own files always live under `home`."""
    state = state or home
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
    launcher = active_launcher(rt.kind)   # None: notify-only (no launch has passed, or is proven for, its gates)
    if launcher is not None:
        rt = launcher.eligibility(home, sid, rt, config)
    live = live_signals(home, sid, rt, system, now, led, state=state)
    why = ""
    if live:
        why = "the session is live (" + "; ".join(live) + "): it will see the decision itself"
    elif launcher is None or not rt.eligible:
        why = rt.reason
    elif not config["launch"]:
        why = "unattended launch is off (cardinal-wake enable --launch)"
    elif disabled_reason(home, rt.kind):
        why = (f"{LABELS[rt.kind]} launch is disabled: {disabled_reason(home, rt.kind)} "
               f"(cardinal-wake reenable {rt.kind})")
    elif not (config.get(launcher.executable) and os.access(config[launcher.executable], os.X_OK)):
        why = f"{launcher.binary} was not found when wake was enabled (run cardinal-wake enable --launch again)"
    else:
        why = launcher.preflight(home, config, system) or ""
    if why:
        for e in pending:
            _notify_only(home, sid, binding, e["seq"], rt.kind, why, system, report, dry_run=dry_run)
        return
    claim_and_launch(home, sid, binding, pending[0]["seq"], rt, config, system, report, dry_run=dry_run,
                     state=state)


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
    _notify(system, binding, sid, NOTIFIED, f"owner decision #{seq} waits for you ({why.split(':')[0]})", kind)
    _report(system, report, sid, seq, "notified", why)


def wake_hold(home: Path, sid: str, state: Path) -> None:
    """Before a launch: hold the session's local evidence (evidence.set_hold,
    reason "wake"; resume-and-wake.md §4) so the woken run's captures
    survive size eviction while space allows. Only the user's own spool
    (~/.cardinal/evidence) and only an existing session directory; never a
    relocated sandboxed spool, which the agent can write (the waker writes
    nothing there). Nothing while capture is off. Fail open."""
    from . import evidence
    try:
        if paths.is_sandboxed_home(Path(state)):
            return
        root = Path(paths.unsandboxed_home(Path(home))) / ".cardinal" / "evidence"
        if evidence.capture_disabled(root):
            return
        evidence.set_hold(root, sid, reason="wake")
    except Exception:
        return


def claim_and_launch(home: Path, sid: str, binding: dict, seq: int, rt: Runtime, config: dict, system: System,
                     report: list, *, dry_run: bool = False, state: Optional[Path] = None) -> str:
    """Claim #seq for `sid` and launch its runtime's pinned resume
    (LAUNCHERS, proven; only an eligible session of such a runtime gets
    here), under the session's lock (§5.4). Returns launched | failed |
    seen | in_flight | runtime_busy | cap | busy | dry_run."""
    launcher = LAUNCHERS[rt.kind]
    if not launcher.proven:
        raise ValueError(f"{rt.kind} launch is not proven")
    if not rt.eligible:
        raise ValueError("not an eligible session")
    argv = launcher.argv(config.get(launcher.executable), rt, sid, seq)
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
            if launcher.exclusive and runtime_in_flight(home, rt.kind):
                _report(system, report, sid, seq, "waiting", f"another {LABELS[rt.kind]} wake is running")
                return "runtime_busy"   # one at a time machine-wide (Gemini: the proxy's port); reconsidered later
            if _claims(led, now) >= SESSION_DAILY_CAP or global_claims(home, now) >= GLOBAL_DAILY_CAP:
                capped = _num(led.get("cap_notified_at"))
                if capped is None or now - capped >= DAY:
                    led["cap_notified_at"] = now
                    write_ledger(home, sid, led, now)
                    _notify(system, binding, sid, "waiting", f"owner decision #{seq}: the daily wake cap is reached",
                            rt.kind)
                _report(system, report, sid, seq, "capped", "the daily wake cap is reached; reconsidered later")
                return "cap"
            entry = {"seq": seq, "investigation_id": binding["investigation_id"], "runtime": rt.kind,
                     "sandbox": rt.sandbox, "claimed_at": now, "spawn_errors": 0,
                     "sandboxed_state": paths.is_sandboxed_home(state or home)}
            transition(entry, DECISION_SEEN, now)
            transition(entry, CLAIMED, now)
            led["entries"][str(seq)] = entry
            write_ledger(home, sid, led, now)
        wake_hold(home, sid, state or home)
        offsets = launcher.snapshot(home, sid)
        log = logs_dir(home) / f"{sid}-{seq}-{int(now)}.log"
        env = launcher.env(system.environ, config, home, log, rt)
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
            transition(entry, RESUMING, system.now(), f"{launcher.binary} resumed in {rt.sandbox}")
            write_ledger(home, sid, led, system.now())
            bad = launcher.after_spawn(home, sid, pid, system)
            if bad:   # the started child was not in its sandbox: killed; the runtime is disabled; never retried
                entry["verified"] = False
                transition(entry, FAILED, system.now(), bad)
                write_ledger(home, sid, led, system.now())
                _disable_launch(home, rt.kind, sid, binding, bad, system, report, system.now())
                return "failed"
            _notify(system, binding, sid, RESUMING, f"resuming for owner decision #{seq} in {rt.sandbox}", rt.kind)
            _report(system, report, sid, seq, "launched", f"pid {pid}, {rt.sandbox}")
            return "launched"
        transition(entry, FAILED, system.now(),
                   f"could not start {launcher.binary} ({entry['spawn_errors']} spawn errors)")
        write_ledger(home, sid, led, system.now())
    _notify(system, binding, sid, FAILED, f"could not start {launcher.binary} for owner decision #{seq}", rt.kind)
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
        plain_home, boxed_home = state_homes(home)
        for key, entry in list(led["entries"].items()):
            binding = ie.read_binding(boxed_home if entry.get("sandboxed_state") else plain_home, sid)
            if entry.get("state") == CLAIMED:
                now = system.now()
                transition(entry, FAILED, now, "the waker stopped during the launch; not retried")
                write_ledger(home, sid, led, now)
                _notify(system, binding, sid, FAILED, f"owner decision #{entry.get('seq')}: launch interrupted",
                        str(entry.get("runtime")))
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
    runtime = entry.get("runtime") if entry.get("runtime") in RUNTIMES else CODEX
    launcher = LAUNCHERS.get(runtime)

    def verify(require_turn: bool) -> Optional[str]:
        if launcher is None:   # a launch by a runtime no longer allowed: never trusted
            return f"{LABELS.get(runtime, runtime)} has no launcher to verify the woken run"
        return launcher.verify(home, sid, offsets, pinned, require_turn=require_turn)

    if child_alive(child, system):
        if now - launched <= WAKE_TIMEOUT:
            return
        bad = verify(False)
        if bad:
            _disable_launch(home, runtime, sid, binding, bad, system, report, now)
        transition(entry, FAILED, now, "still running after 2 h; left running")
        write_ledger(home, sid, led, now)
        _notify(system, binding, sid, FAILED, f"owner decision #{seq}: the resumed session is still running "
                                              "after 2 h (left running)", runtime)
        _report(system, report, sid, seq, "failed", "timeout; child left running")
        return
    if "verified" not in entry:
        bad = verify(True)
        entry["verified"] = bad is None
        if bad:
            entry.setdefault("history", []).append({"state": RESUMING, "at": _iso(now), "note": bad})
            _disable_launch(home, runtime, sid, binding, bad, system, report, now)
        entry.setdefault("exited_at", now)
        write_ledger(home, sid, led, now)
    acked = None
    if binding is not None and binding.get("investigation_id") == entry.get("investigation_id"):
        conn, conn_runtime = _connection_for(home, binding, runtime, system)
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
    _notify(system, binding, sid, entry["state"], words, runtime)
    _report(system, report, sid, seq, entry["state"].lower(), entry.get("reason", ""))


def _disable_launch(home, runtime, sid, binding, why, system, report, now) -> None:
    disable_runtime(home, runtime, f"session {sid[:8]}: {why}", now)
    _notify(system, binding, sid, "disabled", f"{LABELS.get(runtime, runtime)} wake is disabled: {why}. Run "
                                              f"cardinal-wake reenable {runtime} after checking.", runtime)
    _report(system, report, sid, None, f"{runtime} disabled", why)


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
           runtime: Optional[str] = None, core_dir: Optional[Path] = None,
           roots: Optional[List[str]] = None) -> int:
    """Install or update the launchd agent and the config. roots: the
    approved roots for Gemini wakes (realpath'd; None keeps the current
    ones, given ones replace them)."""
    old = read_config(home)
    if roots is not None:
        approved = []
        for r in roots:
            real = os.path.realpath(os.path.expanduser(r))
            problem = None if os.path.isdir(real) else "it is not a directory"
            problem = problem or approved_root_problem(real, home)
            if problem:
                out(f"cardinal-wake: --root {r} is not allowed: {problem}")
                return 2
            approved.append(real)
        roots = list(dict.fromkeys(approved))
    else:
        roots = list(old.get("roots") or [])
    path_env = environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"
    executables = {}
    for launcher in LAUNCHERS.values():   # each launchable runtime's binary, absolute, resolved now
        found = shutil.which(launcher.binary, path=path_env)
        executables[launcher.executable] = os.path.abspath(found) if found else None
    python = shutil.which("python3", path=path_env) or sys.executable
    python = os.path.abspath(python)
    runner = install_lib(home, core_dir)
    _private_dir(logs_dir(home))
    versions = dict(old.get("versions") or {})
    if runtime in RUNTIMES and version:
        versions[runtime] = version
    write_config(home, {"enabled": True, "launch": bool(launch), **executables, "python": python,
                        "enabled_at": old.get("enabled_at") if old["enabled"] and old.get("enabled_at") else _iso(now),
                        "versions": versions, "roots": roots})
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
    if runtime in NOTIFY_ONLY_REASONS and active_launcher(runtime) is None:
        out(f"{LABELS[runtime]} sessions are notify-only: an addressed decision shows a notification and never "
            f"resumes a {LABELS[runtime]} session" + (", with or without --launch." if launch else "."))
    if roots:
        out("Approved roots (Gemini wakes): " + ", ".join(roots))
    if launch:
        for r, launcher in LAUNCHERS.items():
            if not launcher.proven:
                continue
            out(launcher.can_launch)
            exe = executables.get(launcher.executable)
            if not exe:
                out(f"{launcher.binary} was not found on PATH, so {LABELS[r]} sessions cannot launch yet: install "
                    f"it and run cardinal-wake enable --launch again.")
            else:
                out(f"{LABELS[r]}: {exe}")
            reason = disabled_reason(home, r)
            if reason:
                out(f"{LABELS[r]} launch is disabled: {reason}. Run cardinal-wake reenable {r} after checking.")
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
    for launcher in LAUNCHERS.values():
        out(f"{launcher.binary}: {c.get(launcher.executable) or 'not found'}"
            + ("" if launcher.proven else " (launch built, off until its live proofs pass)"))
    out("approved roots: " + (", ".join(c.get("roots") or []) or "none (cardinal-wake enable --root PATH)"))
    out("notify-only runtimes: " + ", ".join(LABELS[r] for r in RUNTIMES if active_launcher(r) is None)
        + ", and any session whose runtime is not recognized")
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
    for sid, b, _, _ in cands:
        rt = resolve(home, sid)
        out(f"  {sid[:8]} {LABELS.get(rt.kind, rt.kind)}: "
            f"{'can launch in ' + str(rt.sandbox) if rt.eligible else rt.reason}")
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
    p_enable.add_argument("--root", action="append", default=None, metavar="PATH",
                          help="an approved root for Gemini wakes (repeatable; replaces the current list; never "
                               "your home directory or above it)")
    sub.add_parser("disable", help="remove the launchd agent")
    sub.add_parser("status", help="the opt-in, disabled runtimes, recent wakes and candidate sessions")
    p_run = sub.add_parser("run", help="look for addressed owner decisions (launchd runs run --once)")
    p_run.add_argument("--once", action="store_true", help="one pass, then exit")
    p_run.add_argument("--dry-run", action="store_true", help="print what would happen; launch nothing")
    p_re = sub.add_parser("reenable", help="allow a runtime to launch again after a failed sandbox check")
    p_re.add_argument("target", choices=sorted(LAUNCHERS))
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
                      version=_plugin_version(plugin_root), runtime=runtime, roots=args.root)
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

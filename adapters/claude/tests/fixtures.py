"""Shared scenario builder for the claude adapter's golden capture and
parity tests.

Each scenario builds a synthetic Claude Code environment (fake HOME with
~/.claude/settings.json OTel env, git repo, transcript JSONL, plan cache,
limits verdicts), runs ONE hook script as a subprocess exactly the way
Claude Code invokes it (payload JSON on stdin), and returns the
normalized OTLP batches the hook POSTed plus its stdout.

The SAME scenarios run twice:
  - capture_goldens.py runs them against the SHIPPED plugin's hooks
    (pre-migration source of truth) and writes tests/goldens/*.json.
  - test_parity.py runs them against the migrated adapter hooks and
    asserts equality with the goldens.

Determinism: git commits use pinned author/committer identity + dates so
head SHAs are stable; transcripts carry fixed ISO timestamps; the OAuth
stub serves fixed profile/usage JSON. Volatile fields (timeUnixNano, ts,
cardinal.core_version — via the core harness; plus cardinal.plugin_version,
scope version, and cardinal.cwd — local) are normalized.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "core" / "tests"))
from harness import StubIngest  # noqa: E402

ADAPTER_HOOKS_DIR = REPO_ROOT / "adapters" / "claude" / "hooks"
GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"

SESSION_ID = "sess-golden-0001"
API_KEY = "test-ingest-key-123"
RESOURCE_ATTRS_CSV = ",".join([
    "service.name=claude-code",
    "agent.runtime=claude-code",
    "deployment.environment=dogfood",
    "user.email=golden@cardinalhq.io",
    "cardinal.org=cardinalhq",
])

_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Golden Fixture",
    "GIT_AUTHOR_EMAIL": "golden@cardinalhq.io",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_NAME": "Golden Fixture",
    "GIT_COMMITTER_EMAIL": "golden@cardinalhq.io",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_CONFIG_NOSYSTEM": "1",
}


# ---------------------------------------------------------------------------
# Environment builders
# ---------------------------------------------------------------------------

def write_settings(home: Path, endpoint: str) -> None:
    """~/.claude/settings.json with the OTel env block cardinal-connect
    writes — the hooks' source of truth for the OTLP connection."""
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "env": {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
            "OTEL_EXPORTER_OTLP_HEADERS": f"x-cardinalhq-api-key={API_KEY}",
            "OTEL_RESOURCE_ATTRIBUTES": RESOURCE_ATTRS_CSV,
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2))


def seed_plan_cache(home: Path) -> None:
    """Pre-populated ~/.claude/cardinal/plan.json so _plan_cache.stamp_attrs()
    stamps plan_type + rate_limit_tier onto downstream events."""
    cache = home / ".claude" / "cardinal" / "plan.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({
        "token_fingerprint": None,
        "profile_fetched_at": "2026-01-01T00:00:00Z",
        "usage_fetched_at": None,
        "plan_type": "max",
        "rate_limit_tier": "default_claude_max_20x",
        "usage": {},
    }))


def write_credentials(home: Path) -> None:
    """~/.claude/.credentials.json — the Linux/file fallback token source
    (USER='' in the hook env disables the macOS keychain probe)."""
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "fake-oauth-token-abc"},
    }))


def make_git_repo(root: Path, branch: str) -> Path:
    """Deterministic single-commit repo on `branch` with a github remote.
    Pinned identity + dates make the head SHA byte-stable across runs."""
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **_GIT_IDENTITY_ENV, "HOME": str(root)}

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=root, env=env, check=True,
            capture_output=True, text=True,
        )

    _git("init", "-q", "-b", branch)
    (root / "README.md").write_text("golden fixture\n")
    _git("add", "README.md")
    _git("commit", "-q", "-m", "golden: initial commit")
    _git("remote", "add", "origin", "git@github.com:cardinalhq/golden-repo.git")
    return root


def make_main_transcript(proj: Path, session_id: str) -> Path:
    """A two-turn Claude Code transcript. The final (current) turn holds
    three assistant model calls exercising: per-call usage, tool targets
    (Read/Write/NotebookEdit), Bash classification (multi + single),
    MCP tool names, and mixed models."""
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_id}.jsonl"

    def usage(i: int, o: int, cc: int, cr: int) -> dict:
        return {
            "input_tokens": i,
            "output_tokens": o,
            "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr,
        }

    lines = [
        {"type": "user", "timestamp": "2026-01-02T03:00:00.000Z",
         "message": {"role": "user", "content": "first question"}},
        {"type": "assistant", "timestamp": "2026-01-02T03:00:05.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "usage": usage(10, 20, 100, 500),
                     "content": [{"type": "text", "text": "answer one"}]}},
        # ---- current turn boundary (real user message #2) ----
        {"type": "user", "timestamp": "2026-01-02T03:01:00.000Z",
         "message": {"role": "user", "content": "please refactor the loader"}},
        {"type": "assistant", "timestamp": "2026-01-02T03:01:05.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "usage": usage(12, 340, 4500, 91000),
                     "content": [
                         {"type": "text", "text": "on it"},
                         {"type": "tool_use", "id": "t1", "name": "Read",
                          "input": {"file_path": "/work/src/app.py"}},
                         {"type": "tool_use", "id": "t2", "name": "Bash",
                          "input": {"command": "git status && npm test"}},
                     ]}},
        {"type": "user", "timestamp": "2026-01-02T03:01:10.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
             {"type": "tool_result", "tool_use_id": "t2", "content": "ok"},
         ]}},
        {"type": "assistant", "timestamp": "2026-01-02T03:01:20.000Z",
         "message": {"role": "assistant", "model": "claude-haiku-4-5",
                     "usage": usage(8, 120, 0, 95000),
                     "content": [
                         {"type": "tool_use", "id": "t3", "name": "Write",
                          "input": {"file_path": "/work/src/app.py",
                                    "content": "new"}},
                         {"type": "tool_use", "id": "t4", "name": "NotebookEdit",
                          "input": {"notebook_path": "/work/nb.ipynb"}},
                         {"type": "tool_use", "id": "t5",
                          "name": "mcp__cardinal__lakerunner__list_services",
                          "input": {}},
                         {"type": "tool_use", "id": "t6", "name": "Bash",
                          "input": {"command": "ls -la"}},
                     ]}},
        {"type": "assistant", "timestamp": "2026-01-02T03:01:30.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "usage": usage(5, 60, 0, 96000),
                     "content": [{"type": "text", "text": "done"}]}},
    ]
    path.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n")
    return path


def make_subagent_transcript(proj: Path, session_id: str, agent_id: str) -> Path:
    """<proj>/<session_id>/subagents/agent-<id>.jsonl with mixed models and
    tool_use blocks — exercises the dominant-model + tool-histogram pass."""
    sub = proj / session_id / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    path = sub / f"agent-{agent_id}.jsonl"

    lines = [
        {"type": "assistant", "timestamp": "2026-01-02T03:02:00.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "usage": {"input_tokens": 100, "output_tokens": 50,
                               "cache_creation_input_tokens": 2000,
                               "cache_read_input_tokens": 3000},
                     "content": [
                         {"type": "tool_use", "id": "s1", "name": "Read",
                          "input": {"file_path": "/work/a.py"}},
                         {"type": "tool_use", "id": "s2", "name": "Grep",
                          "input": {"pattern": "loader"}},
                     ]}},
        {"type": "user", "timestamp": "2026-01-02T03:02:05.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "s1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-01-02T03:02:10.000Z",
         "message": {"role": "assistant", "model": "claude-haiku-4-5",
                     "usage": {"input_tokens": 5, "output_tokens": 10,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 100},
                     "content": [
                         {"type": "tool_use", "id": "s3", "name": "Read",
                          "input": {"file_path": "/work/b.py"}}]}},
        {"type": "assistant", "timestamp": "2026-01-02T03:02:20.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "usage": {"input_tokens": 40, "output_tokens": 200,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 5200},
                     "content": [
                         {"type": "tool_use", "id": "s4", "name": "Bash",
                          "input": {"command": "pytest -q"}}]}},
    ]
    path.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n")
    return path


class OAuthStub:
    """Mock api.anthropic.com: /api/oauth/profile + /api/oauth/usage with
    fixed responses (reached via CARDINAL_PLAN_OAUTH_BASE_URL)."""

    PROFILE = {
        "account": {"has_claude_max": True, "email": "never-emitted@example.com"},
        "organization": {
            "organization_type": "individual",
            "billing_type": "stripe_subscription",
            "rate_limit_tier": "default_claude_max_20x",
            "has_extra_usage_enabled": False,
            "name": "never-emitted-org",
        },
    }
    USAGE = {
        "five_hour": {"utilization": 12.5, "resets_at": "2026-01-02T05:00:00Z"},
        "seven_day": {"utilization": 40.0, "resets_at": "2026-01-08T00:00:00Z"},
        "seven_day_sonnet": None,
        "seven_day_opus": {"utilization": 3.0, "resets_at": "2026-01-08T00:00:00Z"},
    }

    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "OAuthStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/oauth/profile":
                    body = json.dumps(stub.PROFILE).encode()
                elif self.path == "/api/oauth/usage":
                    body = json.dumps(stub.USAGE).encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# ---------------------------------------------------------------------------
# Hook runner + normalization
# ---------------------------------------------------------------------------

def base_env(home: Path, **extra: str) -> dict[str, str]:
    """Minimal hook subprocess env: HOME is the fake home; USER='' disables
    the macOS keychain probe (security CLI errors on an empty account, so
    _plan_cache falls through to the file path we control); no OTEL_* vars
    (settings.json is the source of truth, mirroring Claude Code)."""
    env = {
        "HOME": str(home),
        "USER": "",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    env.update(extra)
    return env


def run_hook(
    hook_path: Path,
    payload: dict,
    env: dict[str, str],
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def _normalize_local(node: Any) -> Any:
    """On top of the core harness normalization (timestamps, ts,
    cardinal.core_version): pin cardinal.plugin_version, cardinal.cwd
    (a per-run temp path), and the scope version (sourced from
    plugin.json, which may legitimately drift post-migration)."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if (
                k == "scope"
                and isinstance(v, dict)
                and "version" in v
            ):
                out[k] = {**_normalize_local(v), "version": "<normalized>"}
            elif k == "attributes" and isinstance(v, list):
                out[k] = [
                    {**a, "value": {"stringValue": "<normalized>"}}
                    if isinstance(a, dict)
                    and a.get("key") in ("cardinal.plugin_version", "cardinal.cwd")
                    else _normalize_local(a)
                    for a in v
                ]
            else:
                out[k] = _normalize_local(v)
        return out
    if isinstance(node, list):
        return [_normalize_local(x) for x in node]
    return node


def collect(stub: StubIngest, proc: subprocess.CompletedProcess) -> dict:
    stdout = proc.stdout.strip()
    try:
        stdout_val: Any = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        stdout_val = stdout
    return {
        "batches": _normalize_local(stub.normalized_batches()),
        "stdout": stdout_val,
        "returncode": proc.returncode,
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def _git_state_scenario(hooks_dir: Path, tmp: Path, *, branch: str,
                        prompt: str, with_plan_cache: bool) -> dict:
    stub = StubIngest().start()
    try:
        home = tmp / "home"
        write_settings(home, stub.endpoint)
        if with_plan_cache:
            seed_plan_cache(home)
        repo = make_git_repo(tmp / "repo", branch)
        payload = {
            "session_id": SESSION_ID,
            "transcript_path": str(tmp / "proj" / f"{SESSION_ID}.jsonl"),
            "cwd": str(repo),
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }
        proc = run_hook(hooks_dir / "git-state.py", payload, base_env(home))
        return collect(stub, proc)
    finally:
        stub.stop()


def scenario_git_state_feature_branch(hooks_dir: Path, tmp: Path) -> dict:
    return _git_state_scenario(
        hooks_dir, tmp, branch="feat/golden-fixture",
        prompt="/code-review --fix please", with_plan_cache=True)


def scenario_git_state_worktree_branch(hooks_dir: Path, tmp: Path) -> dict:
    return _git_state_scenario(
        hooks_dir, tmp, branch="worktree-fix-1018-github-app-repo-picker",
        prompt="hello there", with_plan_cache=False)


def scenario_git_state_main_branch(hooks_dir: Path, tmp: Path) -> dict:
    return _git_state_scenario(
        hooks_dir, tmp, branch="main", prompt="ship it", with_plan_cache=False)


def scenario_turn_usage_stop(hooks_dir: Path, tmp: Path) -> dict:
    stub = StubIngest().start()
    try:
        home = tmp / "home"
        write_settings(home, stub.endpoint)
        seed_plan_cache(home)
        transcript = make_main_transcript(tmp / "proj", SESSION_ID)
        payload = {
            "session_id": SESSION_ID,
            "transcript_path": str(transcript),
            "cwd": str(tmp),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
        proc = run_hook(hooks_dir / "turn-usage.py", payload, base_env(home))
        return collect(stub, proc)
    finally:
        stub.stop()


def _subagent_scenario(hooks_dir: Path, tmp: Path, *, with_transcript: bool) -> dict:
    stub = StubIngest().start()
    try:
        home = tmp / "home"
        write_settings(home, stub.endpoint)
        seed_plan_cache(home)
        proj = tmp / "proj"
        transcript = proj / f"{SESSION_ID}.jsonl"
        proj.mkdir(parents=True, exist_ok=True)
        transcript.write_text("")
        if with_transcript:
            make_subagent_transcript(proj, SESSION_ID, "abc123")
        payload = {
            "session_id": SESSION_ID,
            "transcript_path": str(transcript),
            "cwd": str(tmp),
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "Explore",
                "description": "Scan config loaders",
                "prompt": "find every config loader (never emitted)",
            },
            "tool_response": {
                "agentId": "abc123",
                "agentType": "Explore",
                "totalTokens": 5432,
                "totalToolUseCount": 4,
                "totalDurationMs": 9876,
            },
        }
        proc = run_hook(hooks_dir / "subagent-usage.py", payload, base_env(home))
        return collect(stub, proc)
    finally:
        stub.stop()


def scenario_subagent_usage_full(hooks_dir: Path, tmp: Path) -> dict:
    return _subagent_scenario(hooks_dir, tmp, with_transcript=True)


def scenario_subagent_usage_missing_transcript(hooks_dir: Path, tmp: Path) -> dict:
    return _subagent_scenario(hooks_dir, tmp, with_transcript=False)


def scenario_plan_state_oauth(hooks_dir: Path, tmp: Path) -> dict:
    stub = StubIngest().start()
    oauth = OAuthStub().start()
    try:
        home = tmp / "home"
        write_settings(home, stub.endpoint)
        write_credentials(home)
        payload = {
            "session_id": SESSION_ID,
            "cwd": str(tmp),
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
        proc = run_hook(
            hooks_dir / "plan-state.py", payload,
            base_env(home, CARDINAL_PLAN_OAUTH_BASE_URL=oauth.base_url),
        )
        return collect(stub, proc)
    finally:
        oauth.stop()
        stub.stop()


def scenario_plan_state_no_token(hooks_dir: Path, tmp: Path) -> dict:
    stub = StubIngest().start()
    try:
        home = tmp / "home"
        write_settings(home, stub.endpoint)
        payload = {
            "session_id": SESSION_ID,
            "cwd": str(tmp),
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
        # No credentials file + USER='' → no-token branch (plan_type=api).
        proc = run_hook(hooks_dir / "plan-state.py", payload, base_env(home))
        return collect(stub, proc)
    finally:
        stub.stop()


def scenario_plan_usage_stale_cache(hooks_dir: Path, tmp: Path) -> dict:
    stub = StubIngest().start()
    oauth = OAuthStub().start()
    try:
        home = tmp / "home"
        write_settings(home, stub.endpoint)
        write_credentials(home)
        cache = home / ".claude" / "cardinal" / "plan.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({
            "token_fingerprint": "0123456789abcdef",
            "profile_fetched_at": "2026-01-01T00:00:00Z",
            "usage_fetched_at": "2020-01-01T00:00:00Z",  # stale → refetch
            "plan_type": "max",
            "rate_limit_tier": "default_claude_max_20x",
            "usage": {"five_hour": {"utilization": 1.0}},
        }))
        payload = {
            "session_id": SESSION_ID,
            "transcript_path": str(tmp / "proj" / f"{SESSION_ID}.jsonl"),
            "cwd": str(tmp),
            "hook_event_name": "Stop",
        }
        proc = run_hook(
            hooks_dir / "plan-usage.py", payload,
            base_env(home, CARDINAL_PLAN_OAUTH_BASE_URL=oauth.base_url),
        )
        return collect(stub, proc)
    finally:
        oauth.stop()
        stub.stop()


def scenario_initiative_convention(hooks_dir: Path, tmp: Path) -> dict:
    stub = StubIngest().start()  # unused by the hook; keeps collect() uniform
    try:
        home = tmp / "home"
        write_settings(home, stub.endpoint)
        repo = make_git_repo(tmp / "repo", "feat/golden-fixture")
        payload = {
            "session_id": SESSION_ID,
            "cwd": str(repo),
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
        proc = run_hook(
            hooks_dir / "initiative-convention.py", payload, base_env(home))
        return collect(stub, proc)
    finally:
        stub.stop()


def _write_verdict(home: Path, verdict: dict) -> None:
    limits_dir = home / ".claude" / "cardinal" / "limits"
    limits_dir.mkdir(parents=True, exist_ok=True)
    (limits_dir / f"{SESSION_ID}.verdict.json").write_text(json.dumps(verdict))


def scenario_limits_gate_warn(hooks_dir: Path, tmp: Path) -> dict:
    stub = StubIngest().start()
    try:
        home = tmp / "home"
        write_settings(home, stub.endpoint)
        _write_verdict(home, {
            "decision": "warn",
            "band": 2,
            "ttl_seconds": 120,
            "agent_context": "Cardinal budget standing: 90% consumed. Economize.",
            "user_message": "You're at 90% of the weekly Cardinal budget.",
            "fetched_at": time.time(),
        })
        payload = {
            "session_id": SESSION_ID,
            "cwd": str(tmp),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "keep going",
        }
        proc = run_hook(hooks_dir / "limits-gate.py", payload, base_env(home))
        result = collect(stub, proc)
        # Hysteresis side effect: the surfaced band must be acked.
        ack = json.loads(
            (home / ".claude" / "cardinal" / "limits"
             / f"{SESSION_ID}.ack.json").read_text()
        )
        result["ack_band"] = ack.get("band")
        return result
    finally:
        stub.stop()


def scenario_limits_gate_block(hooks_dir: Path, tmp: Path) -> dict:
    stub = StubIngest().start()
    try:
        home = tmp / "home"
        write_settings(home, stub.endpoint)
        _write_verdict(home, {
            "decision": "block",
            "band": 3,
            "ttl_seconds": 120,
            "block_reason": "Cardinal spend limit reached — set by your lead. "
                            "Run /cardinal:override to proceed anyway.",
            "fetched_at": time.time(),
        })
        payload = {
            "session_id": SESSION_ID,
            "cwd": str(tmp),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "keep going",
        }
        proc = run_hook(hooks_dir / "limits-gate.py", payload, base_env(home))
        return collect(stub, proc)
    finally:
        stub.stop()


SCENARIOS: dict[str, Callable[[Path, Path], dict]] = {
    "git_state_feature_branch": scenario_git_state_feature_branch,
    "git_state_worktree_branch": scenario_git_state_worktree_branch,
    "git_state_main_branch": scenario_git_state_main_branch,
    "turn_usage_stop": scenario_turn_usage_stop,
    "subagent_usage_full": scenario_subagent_usage_full,
    "subagent_usage_missing_transcript": scenario_subagent_usage_missing_transcript,
    "plan_state_oauth": scenario_plan_state_oauth,
    "plan_state_no_token": scenario_plan_state_no_token,
    "plan_usage_stale_cache": scenario_plan_usage_stale_cache,
    "initiative_convention": scenario_initiative_convention,
    "limits_gate_warn": scenario_limits_gate_warn,
    "limits_gate_block": scenario_limits_gate_block,
}

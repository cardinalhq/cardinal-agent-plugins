"""Shared golden-fixture scenarios for the Codex adapter migration.

The SAME scenario code drives both sides of the parity proof:

  capture_goldens.py  — runs the pre-migration hook script from the shipped
                        cardinal-codex-plugin repo and freezes the normalized
                        OTLP batches (and hook stdout) into tests/goldens/.
  test_parity.py      — runs the migrated hook in this repo against the same
                        fixtures and asserts normalized output == goldens.

Everything a scenario touches is sandboxed under a temp HOME with a
prefabricated ~/.codex/cardinal.json + cardinal-secrets.json pointing at a
StubIngest, so no scenario ever needs a real connection.

Normalization: core/tests/harness.py zeroes timestamps and pins the `ts`
attr and cardinal.core_version. This module additionally (locally — core is
frozen):
  - pins the cardinal.plugin_version resource attr and the OTel scope
    version (old code stamps 0.5.2; the migrated adapter stamps its own),
  - DROPS the cardinal.core_version resource attr (the pre-migration code
    never emitted it; the core-backed adapter adds it by design),
  - replaces the sandbox HOME path in attribute values with "<HOME>"
    (cwd-shaped values differ per temp dir).
Git-derived values (head SHA) are made deterministic instead of normalized:
fixture repos are built with pinned author/committer identity and dates, so
the commit SHA is byte-stable across capture and parity runs.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]

# Import the frozen core harness by file path — both this adapter's tests/
# and core/tests/ are top-level "tests" packages, so a package import would
# collide.
_spec = importlib.util.spec_from_file_location(
    "cardinal_core_test_harness", _ROOT / "core" / "tests" / "harness.py"
)
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
StubIngest = _harness.StubIngest

SESSION_ID = "sess-golden-1"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "golden",
    "GIT_AUTHOR_EMAIL": "golden@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_NAME": "golden",
    "GIT_COMMITTER_EMAIL": "golden@example.com",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}
REMOTE_URL = "git@github.com:cardinalhq/golden-fixture.git"


class LimitsStub:
    """Answers GET /api/agent-limits/status with a fixed verdict — enough
    for the SessionStart budget-standing scenario."""

    VERDICT = {
        "decision": "allow",
        "band": 0,
        "ttl_seconds": 120,
        "evaluations": [
            {
                "scope": "session",
                "spent_usd": 1.25,
                "limit_usd": 100.0,
                "fraction": 0.0125,
                "set_by": {"self": True},
            }
        ],
    }

    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.port = 0

    @property
    def status_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/agent-limits/status"

    def start(self) -> "LimitsStub":
        verdict = self.VERDICT

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/agent-limits/status"):
                    payload = json.dumps(verdict).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def write_connection(home: Path, ingest_endpoint: str, limits_url: str | None = None) -> None:
    codex = home / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "schema_version": 2,
        "host": "https://golden.invalid",
        "mode": "telemetry-only",
        "org_id": "org-uuid-1",
        "org_slug": "golden-org",
        "user_email": "golden@example.com",
        "deployment_environment": "dogfood",
        "ingest_endpoint": ingest_endpoint,
        "telemetry": {"enabled": True},
    }
    if limits_url:
        state["limits"] = {"status_url": limits_url, "enabled": True}
    (codex / "cardinal.json").write_text(json.dumps(state, indent=2) + "\n")
    (codex / "cardinal-secrets.json").write_text(json.dumps({
        "schema_version": 1,
        "ingest_api_key": "GOLDENINGESTKEY" + "x" * 48,
        "ingest_api_header": "x-cardinalhq-api-key",
    }, indent=2) + "\n")


def make_repo(parent: Path, branch: str) -> Path:
    """Deterministic git repo: pinned identity + dates on an empty commit
    give a byte-stable HEAD SHA across capture and parity runs."""
    repo = parent / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **GIT_ENV}
    for cmd in (
        ["git", "init", "-q", "-b", branch],
        ["git", "commit", "--allow-empty", "-q", "-m", "golden"],
        ["git", "remote", "add", "origin", REMOTE_URL],
    ):
        subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)
    return repo


def run_hook(hook: Path, event: str, home: Path, stdin: dict,
             env_extra: dict[str, str] | None = None,
             timeout: int = 30) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    # Session-id and debug-capture env must never leak in from the caller.
    for k in ("CODEX_SESSION_ID", "OPENAI_CODEX_SESSION_ID",
              "CARDINAL_CODEX_DEBUG_PAYLOADS"):
        env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(hook), "--event", event],
        env=env,
        input=json.dumps(stdin),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Local normalization on top of the core harness (see module docstring)
# ---------------------------------------------------------------------------

def _scrub(node: Any, home_str: str) -> Any:
    if isinstance(node, dict):
        return {k: _scrub(v, home_str) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub(x, home_str) for x in node]
    if isinstance(node, str) and home_str in node:
        return node.replace(home_str, "<HOME>")
    return node


def _normalize_resource_and_scope(batch: dict) -> dict:
    for rl in batch.get("resourceLogs", []):
        attrs = rl.get("resource", {}).get("attributes", [])
        kept = []
        for a in attrs:
            if a.get("key") == "cardinal.core_version":
                continue  # pre-migration code never emitted it
            if a.get("key") == "cardinal.plugin_version":
                a = {**a, "value": {"stringValue": "<normalized>"}}
            kept.append(a)
        rl["resource"]["attributes"] = kept
        for sl in rl.get("scopeLogs", []):
            scope = sl.get("scope")
            if isinstance(scope, dict) and "version" in scope:
                scope["version"] = "<normalized>"
    return batch


def normalized_batches(stub: "StubIngest", home: Path) -> list[dict]:
    out = []
    for batch in stub.normalized_batches():
        out.append(_scrub(_normalize_resource_and_scope(batch), str(home)))
    return out


def parse_stdout(raw: str, home: Path) -> Any:
    if not raw.strip():
        return ""
    return _scrub(json.loads(raw), str(home))


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def transcript_rows_first(session_id: str, cwd: str) -> list[dict]:
    def call(ts: str, name: str, arguments: dict, call_id: str) -> dict:
        return {"timestamp": ts, "type": "response_item",
                "payload": {"type": "function_call", "name": name,
                            "arguments": json.dumps(arguments), "call_id": call_id}}

    def output(ts: str, call_id: str, text: str) -> dict:
        return {"timestamp": ts, "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": call_id,
                            "output": text}}

    return [
        {"timestamp": "2026-07-01T00:00:00.000Z", "type": "session_meta",
         "payload": {"id": session_id, "cwd": cwd}},
        {"timestamp": "2026-07-01T00:00:01.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5-codex", "cwd": cwd}},
        {"timestamp": "2026-07-01T00:00:02.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "run the tests"}},
        call("2026-07-01T00:00:03.000Z", "exec_command",
             {"cmd": "go test ./... && git status"}, "c1"),
        output("2026-07-01T00:00:04.000Z", "c1", "Process exited with code 0\n"),
        call("2026-07-01T00:00:05.000Z", "Read",
             {"file_path": "/workspace/src/main.py"}, "c2"),
        output("2026-07-01T00:00:06.000Z", "c2", "file contents"),
        call("2026-07-01T00:00:07.000Z", "apply_patch",
             {"patch": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n*** End Patch"},
             "c3"),
        output("2026-07-01T00:00:08.000Z", "c3", "Done!"),
        call("2026-07-01T00:00:09.000Z", "mcp__lakerunner__list_services",
             {"query": "checkout"}, "c4"),
        output("2026-07-01T00:00:10.000Z", "c4", "Process exited with code 2\n"),
        {"timestamp": "2026-07-01T00:00:11.000Z", "type": "event_msg",
         "payload": {
             "type": "token_count",
             "info": {"last_token_usage": {
                 "input_tokens": 1200,
                 "cached_input_tokens": 800,
                 "output_tokens": 350,
                 "total_tokens": 1550,
             }},
             "rate_limits": {
                 "limit_id": "codex",
                 "plan_type": "team",
                 "primary": {"used_percent": 3.5, "resets_at": 1780000000},
                 "secondary": {"used_percent": 8.25, "resets_at": 1780001000},
             },
         }},
    ]


def transcript_rows_second(session_id: str) -> list[dict]:
    return [
        {"timestamp": "2026-07-01T00:01:00.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "now lint"}},
        {"timestamp": "2026-07-01T00:01:01.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "arguments": json.dumps({"cmd": "pytest -q"}), "call_id": "c5"}},
        {"timestamp": "2026-07-01T00:01:02.000Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c5",
                     "output": "Process exited with code 0\n"}},
        {"timestamp": "2026-07-01T00:01:03.000Z", "type": "event_msg",
         "payload": {
             "type": "token_count",
             "info": {"last_token_usage": {
                 "input_tokens": 300,
                 "cached_input_tokens": 100,
                 "output_tokens": 40,
             }},
             "rate_limits": {
                 "limit_id": "codex",
                 "plan_type": "team",
                 "primary": {"used_percent": 3.6, "resets_at": 1780000000},
                 "secondary": {"used_percent": 8.5, "resets_at": 1780001000},
             },
         }},
    ]


def scenario_telemetry(hook: Path) -> dict[str, Any]:
    """Stop (first + resumed), UserPromptSubmit git_state, SubagentStop —
    one sandbox so the plan stamp persists across events like production."""
    out: dict[str, Any] = {}
    stub = StubIngest().start()
    try:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_connection(home, stub.endpoint)
            transcript = home / "transcript.jsonl"
            rows = transcript_rows_first(SESSION_ID, str(home))
            transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            stdin = {"session_id": SESSION_ID, "transcript_path": str(transcript)}

            r = run_hook(hook, "Stop", home, stdin)
            assert r.returncode == 0, r.stderr
            batches = normalized_batches(stub, home)
            assert len(batches) == 1, f"expected 1 batch, got {len(batches)}"
            out["stop_first"] = batches[0]

            with transcript.open("a") as f:
                for row in transcript_rows_second(SESSION_ID):
                    f.write(json.dumps(row) + "\n")
            r = run_hook(hook, "Stop", home, stdin)
            assert r.returncode == 0, r.stderr
            batches = normalized_batches(stub, home)
            assert len(batches) == 2, f"expected 2 batches, got {len(batches)}"
            out["stop_second"] = batches[1]

            repo = make_repo(home, "feat/worktree-fix-99-cool-thing")
            r = run_hook(hook, "UserPromptSubmit", home, {
                "session_id": SESSION_ID,
                "cwd": str(repo),
                "prompt": "/cardinal:status check ingest",
            })
            assert r.returncode == 0, r.stderr
            assert r.stdout.strip() == "", "no gate verdict expected"
            batches = normalized_batches(stub, home)
            assert len(batches) == 3, f"expected 3 batches, got {len(batches)}"
            out["user_prompt_submit"] = batches[2]

            r = run_hook(hook, "SubagentStop", home, {
                "session_id": SESSION_ID,
                "subagent_type": "explorer",
                "agent_id": "agent-7",
                "description": "Scan for credential leaks",
                "total_tokens": 4321,
            })
            assert r.returncode == 0, r.stderr
            batches = normalized_batches(stub, home)
            assert len(batches) == 4, f"expected 4 batches, got {len(batches)}"
            out["subagent_stop"] = batches[3]
    finally:
        stub.stop()
    return out


def scenario_session_start(hook: Path) -> dict[str, Any]:
    """SessionStart additionalContext: convention prompt + budget standing
    (forced limits fetch against the stub)."""
    out: dict[str, Any] = {}
    stub = StubIngest().start()
    limits = LimitsStub().start()
    try:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_connection(home, stub.endpoint, limits_url=limits.status_url)
            repo = make_repo(home, "main")
            r = run_hook(hook, "SessionStart", home,
                         {"session_id": "sess-start-1", "cwd": str(repo)})
            assert r.returncode == 0, r.stderr
            out["session_start_stdout"] = parse_stdout(r.stdout, home)

            outside = home / "not-a-repo"
            outside.mkdir()
            r = run_hook(hook, "SessionStart", home,
                         {"session_id": "sess-start-2", "cwd": str(outside)})
            assert r.returncode == 0, r.stderr
            out["session_start_outside_repo_stdout"] = parse_stdout(r.stdout, home)
    finally:
        limits.stop()
        stub.stop()
    return out


def write_verdict(home: Path, session_id: str, verdict: dict) -> None:
    limits_dir = home / ".codex" / "cardinal" / "limits"
    limits_dir.mkdir(parents=True, exist_ok=True)
    (limits_dir / f"{session_id}.verdict.json").write_text(
        json.dumps({"fetched_at": time.time(), **verdict})
    )


def scenario_gate(hook: Path) -> dict[str, Any]:
    """UserPromptSubmit spend-limits gate: block, override downgrade, warn
    with band hysteresis. cwd is not a git repo so no git_state is emitted."""
    out: dict[str, Any] = {}
    stub = StubIngest().start()
    try:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_connection(home, stub.endpoint)
            cwd = home / "plain"
            cwd.mkdir()

            write_verdict(home, "sess-block-1", {
                "decision": "block", "band": 3,
                "block_reason": "Session budget of $100 reached.",
            })
            stdin = {"session_id": "sess-block-1", "cwd": str(cwd), "prompt": "hi"}
            r = run_hook(hook, "UserPromptSubmit", home, stdin)
            assert r.returncode == 0, r.stderr
            out["gate_block_stdout"] = parse_stdout(r.stdout, home)

            (home / ".codex" / "cardinal" / "limits" /
             "sess-block-1.override.json").write_text("{}")
            r = run_hook(hook, "UserPromptSubmit", home, stdin)
            assert r.returncode == 0, r.stderr
            out["gate_block_override_stdout"] = parse_stdout(r.stdout, home)

            write_verdict(home, "sess-warn-1", {
                "decision": "warn", "band": 2,
                "agent_context": "Economize: budget at 90%.",
                "user_message": "Cardinal: session budget at 90%.",
            })
            stdin = {"session_id": "sess-warn-1", "cwd": str(cwd), "prompt": "hi"}
            r = run_hook(hook, "UserPromptSubmit", home, stdin)
            assert r.returncode == 0, r.stderr
            out["gate_warn_stdout"] = parse_stdout(r.stdout, home)

            r = run_hook(hook, "UserPromptSubmit", home, stdin)
            assert r.returncode == 0, r.stderr
            out["gate_warn_repeat_stdout"] = parse_stdout(r.stdout, home)
    finally:
        stub.stop()
    return out


def run_all(hook: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out.update(scenario_telemetry(hook))
    out.update(scenario_session_start(hook))
    out.update(scenario_gate(hook))
    return out

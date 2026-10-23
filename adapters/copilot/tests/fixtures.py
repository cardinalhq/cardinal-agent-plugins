"""Shared fixtures for the Copilot adapter's tests.

Every hook test runs hooks/cardinal-copilot-telemetry.py as a subprocess,
the way Copilot CLI does (`--event <name>`, the payload on stdin), with HOME
pointed at a temp dir, so nothing touches the developer's ~/.copilot. The
child's environment drops every CARDINAL_* variable the developer's shell
may export (CARDINAL_MCP_URL / CARDINAL_MCP_API_KEY would make an
unconnected sandbox look connected) and sets
CARDINAL_COPILOT_BACKGROUND_INLINE=1, so postToolUse's sends run inline
against the stub ingest (core/tests/harness.py StubIngest).

Payloads follow Copilot CLI's documented hook input
(docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference):
camelCase sessionId / cwd / toolName / toolArgs / toolResult{resultType,
textResultForLlm}. No live Copilot CLI payload was captured for these tests.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER = TESTS_DIR.parent
REPO_ROOT = ADAPTER.parent.parent
HOOK = ADAPTER / "hooks" / "cardinal-copilot-telemetry.py"
SCRIPTS = ADAPTER / "scripts"
CONNECT = SCRIPTS / "cardinal-connect"
DISCONNECT = SCRIPTS / "cardinal-disconnect"
STATUS = SCRIPTS / "cardinal-status"
DECISION_CLI = SCRIPTS / "cardinal-decision"
STORYBOARD_CLI = SCRIPTS / "cardinal-storyboard"
EVIDENCE_CLI = SCRIPTS / "cardinal-evidence"
VERSION = json.loads((ADAPTER / ".plugin" / "plugin.json").read_text())["version"]

SID = "5b7c1d2e-3f40-4a5b-8c6d-7e8f9a0b1c2d"

if not (ADAPTER / "hooks" / "cardinal_core" / "storyboard_agent.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "copilot"],
                   check=True, capture_output=True)

# core/tests/harness.py provides StubIngest (read-only import).
sys.path.insert(0, str(REPO_ROOT / "core" / "tests"))
from harness import StubIngest  # noqa: E402

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Copilot Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Copilot Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def load_script(path: Path, name: str):
    """Import a hook or script file (no .py suffix needed) as a module."""
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def clean_env(home: Path, **extra: str) -> dict:
    """The developer's environment minus every CARDINAL_* / COPILOT_HOME
    variable, with HOME sandboxed."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CARDINAL_") and k != "COPILOT_HOME"}
    env["HOME"] = str(home)
    env.update(extra)
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=repo, check=True,
                          capture_output=True, text=True, env={**os.environ, **_GIT_ENV}).stdout.strip()


def make_repo(repo: Path, branch: str = "feat/copilot-thing",
              remote: str = "git@github.com:acme/widgets.git") -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "checkout", "-q", "-b", branch)
    git(repo, "remote", "add", "origin", remote)
    (repo / "README.md").write_text("fixture\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def attrs_of(kvs: list) -> dict:
    out = {}
    for kv in kvs:
        v = kv["value"]
        out[kv["key"]] = v.get("stringValue", v.get("intValue", v.get("boolValue")))
    return out


def records_in(batches: list, event_name: str | None = None) -> list:
    """(resource attrs, record attrs) for every log record in the batches,
    optionally only those whose event_name matches."""
    out = []
    for body in batches:
        for rl in body.get("resourceLogs", []):
            resource = attrs_of(rl["resource"]["attributes"])
            for sl in rl.get("scopeLogs", []):
                for rec in sl.get("logRecords", []):
                    attrs = attrs_of(rec["attributes"])
                    if event_name is None or attrs.get("event_name") == event_name:
                        out.append((resource, attrs))
    return out


class FakeMaestro:
    """Records every POST; answers by the request path's last segment
    (`routes[segment] = (status, body)`), 404 otherwise."""

    def __init__(self) -> None:
        self.requests: list = []
        self.routes: dict = {}

    def start(self) -> "FakeMaestro":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {}
                fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "body": body})
                status, out = fake.routes.get(self.path.rsplit("/", 1)[-1], (404, {}))
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def paths(self, suffix: str) -> list:
        return [r for r in self.requests if r["path"].endswith(suffix)]


class HookCase(unittest.TestCase):
    """A temp HOME (+ a git repo and a stub `gh` on PATH), a StubIngest,
    and helpers to run the hook and read what reached the stub."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.base = base
        self.home = base / "home"
        self.copilot = self.home / ".copilot"
        self.copilot.mkdir(parents=True)
        self.repo = make_repo(base / "repo")
        self.bin = base / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text("#!/bin/sh\nexit 1\n")
        gh.chmod(0o755)
        self.path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
        self.stub = StubIngest().start()
        self.addCleanup(self.stub.stop)

    # --- connection -----------------------------------------------------------

    def connect(self, **state: Any) -> None:
        """A fake connection: ingest to the stub, no MCP URL unless given."""
        body = {"schema_version": 1, "ingest_endpoint": self.stub.endpoint, "deployment_environment": "test",
                "user_email": "dev@example.com", "org_slug": "acme"}
        body.update(state)
        (self.copilot / "cardinal.json").write_text(json.dumps(body))
        (self.copilot / "cardinal-secrets.json").write_text(json.dumps({"ingest_api_key": "test-ingest-key"}))

    # --- running --------------------------------------------------------------

    def env(self, **extra: str) -> dict:
        return clean_env(self.home, PATH=self.path, CARDINAL_COPILOT_BACKGROUND_INLINE="1",
                         CARDINAL_INVESTIGATION_POLLER="0", **extra)

    def run_hook(self, event: str | None, payload: Any = None, *args: str, raw: str | None = None,
                 script: Path = HOOK, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess:
        argv = [sys.executable, str(script)]
        if event is not None:
            argv += ["--event", event]
        argv += list(args)
        stdin = raw if raw is not None else json.dumps(payload if payload is not None else {})
        res = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=self.env(**env),
                             cwd=str(cwd or self.repo), timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def context(self, res: subprocess.CompletedProcess) -> str:
        out = res.stdout.strip()
        if not out:
            return ""
        parsed = json.loads(out)
        self.assertEqual(set(parsed), {"additionalContext"}, parsed)
        return parsed["additionalContext"]

    def records(self, event_name: str | None = None) -> list:
        return records_in(list(self.stub.log_batches), event_name)

    # --- payloads -------------------------------------------------------------

    def payload(self, **over: Any) -> dict:
        body: dict = {"sessionId": SID, "timestamp": 1760000000000, "cwd": str(self.repo)}
        body.update(over)
        return body

    def tool(self, name: str = "bash", args: Any = None, text: str | None = "ok",
             result_type: str = "success", **over: Any) -> dict:
        """A postToolUse payload."""
        result: dict = {"resultType": result_type}
        if text is not None:
            result["textResultForLlm"] = text
        body = {"toolName": name, "toolArgs": {"command": "ls"} if args is None else args, "toolResult": result}
        body.update(over)
        return self.payload(**body)

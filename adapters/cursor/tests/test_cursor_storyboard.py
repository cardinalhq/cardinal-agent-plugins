"""Storyboard associations in the Cursor adapter (cardinal_core.storyboard_agent):

  - sessionStart: this conversation's id + `cardinal-storyboard context`, and
    the discovery block (X-Cardinal-Client: cursor/<plugin version>), as
    additional_context.
  - afterFileEdit (registered by cardinal-connect): the edited file is
    recorded for `context.paths`.
  - No automatic context stamping: Cursor's beforeMCPExecution returns a
    permission only and cannot rewrite the call's input, so nothing is
    registered for it; the agent passes `cardinal-storyboard context`.

NO Cursor payload was captured (cursor-agent is not installed where the
capture ran). The afterFileEdit and sessionStart payloads below are from
Cursor's hooks documentation: FROM DOCS, UNVERIFIED.

Run: cd adapters/cursor && python3 -m unittest tests.test_cursor_storyboard -v
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
HOOK = ADAPTER / "hooks" / "cardinal-cursor-telemetry.py"
CLI = ADAPTER / "scripts" / "cardinal-storyboard"
CONNECT = ADAPTER / "scripts" / "cardinal-connect"
SB1 = "sb_0123456789abcdef01234567"
CONV = "6f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5"
VERSION = json.loads((ADAPTER / ".cursor-plugin" / "plugin.json").read_text())["version"]

if not (ADAPTER / "hooks" / "cardinal_core" / "storyboard_agent.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "cursor"],
                   check=True, capture_output=True)

# --- from Cursor's hooks docs, UNVERIFIED -------------------------------------
SESSION_START = {"conversation_id": CONV, "generation_id": "g-1", "hook_event_name": "sessionStart",
                 "workspace_roots": ["<cwd>"]}
AFTER_FILE_EDIT = {"conversation_id": CONV, "generation_id": "g-2", "hook_event_name": "afterFileEdit",
                   "workspace_roots": ["<cwd>"], "file_path": "<cwd>/hello.txt",
                   "edits": [{"old_string": "hi", "new_string": "hello"}]}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c",
                           "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


class FakeMaestro:
    def __init__(self) -> None:
        self.requests: list = []
        self.routes: dict = {}

    def start(self) -> "FakeMaestro":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
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


class CursorStoryboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.home, self.repo, stub = base / "home", base / "work", base / "stub"
        for d in (self.home / ".cursor", self.repo, stub):
            d.mkdir(parents=True)
        (stub / "gh").write_text("#!/bin/sh\nexit 1\n")
        (stub / "gh").chmod(0o755)
        self.path = f"{stub}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
        _git(self.repo, "init", "-q")
        _git(self.repo, "checkout", "-q", "-b", "fix/checkout")
        _git(self.repo, "remote", "add", "origin", "https://github.com/Acme/Widgets.git")
        (self.repo / "README.md").write_text("x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.fake = FakeMaestro().start()
        self.fake.routes["find"] = (200, {"matches": [{
            "storyboard_id": SB1, "question": "Why did checkout p99 regress?", "status": "published",
            "act_count": 1, "match": "branch", "match_role": "written_from", "match_act": 1,
            "context": {"repo": "acme/widgets", "branch": "fix/checkout"},
            "view_url": f"https://app.cardinalhq.io/storyboards/{SB1}"}], "rule": "r", "associations_api": 1})
        self.fake.routes["get"] = (200, {"storyboard_id": SB1, "scenes": []})

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def connect(self):
        cursor = self.home / ".cursor"
        (cursor / "cardinal.json").write_text(json.dumps({"mcp_url": f"{self.fake.origin}/api/orgs/org-1/mcp",
                                                          "user_email": "dev@example.com"}))
        (cursor / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "ck_cursor_test"}))

    def env(self) -> dict:
        return {"HOME": str(self.home), "PATH": self.path, "CARDINAL_CURSOR_BACKGROUND_INLINE": "1"}

    def run_hook(self, payload: dict) -> subprocess.CompletedProcess:
        payload = json.loads(json.dumps(payload).replace("<cwd>", str(self.repo)))
        res = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                             text=True, env=self.env(), cwd=str(self.repo), timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def test_session_start_names_the_conversation_and_discovers(self):
        self.connect()
        ctx = json.loads(self.run_hook(SESSION_START).stdout)["additional_context"]
        self.assertIn(f"Cardinal session id for this session: {CONV}.", ctx)
        self.assertIn(f'pass as context the object `python3 "{CLI}" context --session-id {CONV}`', ctx)
        self.assertNotIn("the plugin fills", ctx)
        self.assertIn(f"{SB1} · written from branch fix/checkout (subject not confirmed)", ctx)
        self.assertEqual(self.fake.requests[0]["headers"]["x-cardinal-client"], f"cursor/{VERSION}")

    def test_after_file_edit_reaches_cli_context(self):
        (self.repo / "hello.txt").write_text("hello\n")
        self.assertEqual(self.run_hook(AFTER_FILE_EDIT).stdout, "")
        res = subprocess.run([sys.executable, str(CLI), "context", "--cwd", str(self.repo), "--session-id", CONV],
                             capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        ctx = json.loads(res.stdout)["context"]
        self.assertEqual((ctx["repo"], ctx["branch"], ctx["paths"]), ("acme/widgets", "fix/checkout", ["hello.txt"]))
        self.assertEqual(ctx["client"], f"cursor/{VERSION}")

    def test_connect_registers_after_file_edit_not_before_mcp(self):
        loader = SourceFileLoader("cursor_connect_sb", str(CONNECT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        path = self.home / ".cursor" / "hooks.json"
        mod.write_hooks_config(path)
        hooks = json.loads(path.read_text())["hooks"]
        self.assertIn("afterFileEdit", hooks)
        self.assertNotIn("beforeMCPExecution", hooks)


if __name__ == "__main__":
    unittest.main()

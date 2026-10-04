"""Storyboard associations in the Gemini CLI adapter
(cardinal_core.storyboard_agent):

  - SessionStart: this session's id + `cardinal-storyboard context`, and the
    discovery block (X-Cardinal-Client: gemini/<plugin version>).
  - AfterTool: the file a successful write_file / replace edited is recorded
    for `context.paths`.
  - BeforeTool: an absent session_id / context is filled on Cardinal's
    storyboard tools by returning hookSpecificOutput.tool_input (Gemini
    applies it to the call). The tool is told by mcp_context {server_name,
    tool_name}; Gemini's internal tools (update_topic) are ignored.
  - scripts/cardinal-storyboard context prints repo / branch / head_sha /
    paths.

The payloads are the ones captured from gemini 0.50.0 (2026-10-03, a
project-level .gemini/settings.json with capture-only hooks and a stub
"cardinal" MCP server); only cwd / paths / transcript_path are replaced.

Run: cd adapters/gemini && python3 -m unittest tests.test_gemini_storyboard -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER = TESTS_DIR.parent
REPO_ROOT = ADAPTER.parent.parent
HOOK = ADAPTER / "hooks" / "cardinal-gemini-telemetry.py"
CLI = ADAPTER / "scripts" / "cardinal-storyboard"
SB1 = "sb_0123456789abcdef01234567"
SESSION = "845f7b2e-62a5-424b-8ae9-164d65ba509c"
VERSION = json.loads((ADAPTER / ".gemini-plugin" / "plugin.json").read_text())["version"]

if not (ADAPTER / "hooks" / "cardinal_core" / "storyboard_agent.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "gemini"],
                   check=True, capture_output=True)

# --- captured (gemini 0.50.0) -----------------------------------------------
COMMON = {"session_id": SESSION, "transcript_path": "<transcript>", "cwd": "<cwd>",
          "timestamp": "2026-10-04T02:19:57.900Z"}
SESSION_START = {"session_id": SESSION, "transcript_path": "<transcript>", "cwd": "<cwd>",
                 "hook_event_name": "SessionStart", "timestamp": "2026-10-04T02:19:36.691Z", "source": "startup"}
AFTER_WRITE = {**COMMON, "hook_event_name": "AfterTool", "tool_name": "write_file",
               "tool_input": {"content": "hi", "file_path": "hello.txt"},
               "tool_response": {
                   "llmContent": "Successfully created and wrote to new file: <cwd>/hello.txt. Here is the "
                                 "updated code:\nhi",
                   "returnDisplay": {"fileDiff": "Index: hello.txt\n===\n--- hello.txt\tOriginal\n+++ "
                                                 "hello.txt\tWritten\n@@ -0,0 +1,1 @@\n+hi\n",
                                     "fileName": "hello.txt", "filePath": "<cwd>/hello.txt",
                                     "originalContent": "", "newContent": "hi", "isNewFile": True}}}
AFTER_REPLACE = {**COMMON, "hook_event_name": "AfterTool", "tool_name": "replace",
                 "tool_input": {"old_string": "hi", "instruction": "Change contents from hi to hello",
                                "new_string": "hello", "file_path": "hello.txt"},
                 "tool_response": {
                     "llmContent": "Successfully modified file: <cwd>/hello.txt (1 replacements). Here is the "
                                   "updated code:\nhello",
                     "returnDisplay": {"fileDiff": "Index: hello.txt\n", "fileName": "hello.txt",
                                       "filePath": "<cwd>/hello.txt", "originalContent": "hi",
                                       "newContent": "hello", "isNewFile": False}}}
MCP_CONTEXT = {"server_name": "cardinal", "tool_name": "storyboard__create", "command": "python3",
               "args": ["<stub>/mcp.py"]}
BEFORE_CREATE = {**COMMON, "hook_event_name": "BeforeTool", "tool_name": "mcp_cardinal_storyboard__create",
                 "tool_input": {"title": "[ASSOC-TEST] capture",
                                "context": {"session_id": "4b85836f-8618-4edc-a72f-80bdbcfe72e8"}},
                 "mcp_context": MCP_CONTEXT}
# The call the rewrite was proven on: no context ("x" in the capture).
BEFORE_CREATE_BARE = {**BEFORE_CREATE, "tool_input": {"title": "[ASSOC-TEST] x"}}
BEFORE_TOPIC = {**COMMON, "hook_event_name": "BeforeTool", "tool_name": "update_topic",
                "tool_input": {"title": "Executing Requested Steps", "summary": "s", "strategic_intent": "i"}}


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


class GeminiStoryboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.home, self.repo, stub = base / "home", base / "work", base / "stub"
        for d in (self.home / ".gemini", self.repo, stub):
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
        gemini = self.home / ".gemini"
        (gemini / "cardinal.json").write_text(json.dumps({"mcp_url": f"{self.fake.origin}/api/orgs/org-1/mcp",
                                                          "user_email": "dev@example.com"}))
        (gemini / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "ck_gemini_test"}))

    def env(self, **extra: str) -> dict:
        env = {"HOME": str(self.home), "PATH": self.path, "CARDINAL_GEMINI_INLINE_BACKGROUND": "1"}
        env.update(extra)
        return env

    def run_hook(self, event: str, payload: dict, **env: str) -> subprocess.CompletedProcess:
        payload = json.loads(json.dumps(payload).replace("<cwd>", str(self.repo)))
        res = subprocess.run([sys.executable, str(HOOK), "--event", event], input=json.dumps(payload),
                             capture_output=True, text=True, env=self.env(**env), cwd=str(self.repo), timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def cli_context(self) -> dict:
        res = subprocess.run([sys.executable, str(CLI), "context", "--cwd", str(self.repo), "--session-id", SESSION],
                             capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["context"]

    def test_session_start_names_the_session_and_discovers(self):
        self.connect()
        out = json.loads(self.run_hook("SessionStart", SESSION_START).stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        ctx = out["additionalContext"]
        self.assertIn("one branch = one initiative", ctx)
        self.assertIn(f"Cardinal session id for this session: {SESSION}.", ctx)
        self.assertIn(f'`python3 "{CLI}" context --session-id {SESSION}`', ctx)
        self.assertIn(f"{SB1} · written from branch fix/checkout (subject not confirmed)", ctx)
        find = self.fake.requests[0]
        self.assertEqual(find["headers"]["x-cardinal-client"], f"gemini/{VERSION}")
        self.assertEqual(find["headers"]["x-cardinalhq-api-key"], "ck_gemini_test")

    def test_session_start_outside_a_repo_is_the_session_line_only(self):
        self.connect()
        outside = self.home / "scratch"
        outside.mkdir()
        payload = {**SESSION_START, "cwd": str(outside)}
        ctx = json.loads(self.run_hook("SessionStart", payload).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith(f"Cardinal session id for this session: {SESSION}."), ctx)
        self.assertNotIn("one branch = one initiative", ctx)

    def test_write_and_replace_reach_cli_context(self):
        (self.repo / "hello.txt").write_text("hello\n")
        self.run_hook("AfterTool", AFTER_WRITE, CARDINAL_EVIDENCE_CAPTURE="0")
        self.run_hook("AfterTool", AFTER_REPLACE, CARDINAL_EVIDENCE_CAPTURE="0")
        ctx = self.cli_context()
        head = _git(self.repo, "rev-parse", "HEAD")
        self.assertEqual((ctx["repo"], ctx["branch"], ctx["head_sha"]), ("acme/widgets", "fix/checkout", head))
        self.assertEqual(ctx["paths"], ["hello.txt"])

    def test_failed_edit_is_not_recorded(self):
        failed = {**AFTER_WRITE, "tool_response": {"llmContent": "Error: permission denied",
                                                   "returnDisplay": "Error", "error": {"message": "denied"}}}
        self.run_hook("AfterTool", failed, CARDINAL_EVIDENCE_CAPTURE="0")
        self.assertNotIn("paths", self.cli_context())

    def stamped(self, payload: dict, **env: str):
        out = self.run_hook("BeforeTool", payload, **env).stdout
        return json.loads(out)["hookSpecificOutput"] if out.strip() else None

    def test_bare_create_is_stamped(self):
        out = self.stamped(BEFORE_CREATE_BARE)
        self.assertEqual(out["hookEventName"], "BeforeTool")
        self.assertNotIn("decision", out)
        got = out["tool_input"]
        self.assertEqual(got["title"], "[ASSOC-TEST] x")
        self.assertEqual(got["session_id"], SESSION)
        self.assertEqual(got["context"]["repo"], "acme/widgets")
        self.assertEqual(got["context"]["client"], f"gemini/{VERSION}")

    def test_model_context_is_kept(self):
        got = self.stamped(BEFORE_CREATE)["tool_input"]
        self.assertEqual(got["context"], {"session_id": "4b85836f-8618-4edc-a72f-80bdbcfe72e8"})
        self.assertEqual(got["session_id"], SESSION)

    def test_other_tools_and_servers_are_untouched(self):
        self.assertIsNone(self.stamped(BEFORE_TOPIC))
        other = {**BEFORE_CREATE_BARE, "tool_name": "mcp_other_storyboard__create",
                 "mcp_context": {**MCP_CONTEXT, "server_name": "other"}}
        self.assertIsNone(self.stamped(other))
        self.assertIsNone(self.stamped(BEFORE_CREATE_BARE, CARDINAL_STORYBOARD_CONTEXT="0"))


if __name__ == "__main__":
    unittest.main()

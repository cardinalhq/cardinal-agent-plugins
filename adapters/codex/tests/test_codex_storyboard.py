"""Storyboard associations in the Codex adapter (cardinal_core.storyboard_agent):

  - SessionStart: this session's id + `cardinal-storyboard context`, and the
    discovery block (X-Cardinal-Client: codex/<plugin version>).
  - PostToolUse ToolEvidence: the files a successful apply_patch named are
    recorded for `context.paths`.
  - PreToolUse StoryboardContext: an absent session_id / context is filled
    on mcp__cardinal__storyboard__*, with permissionDecision "allow" (Codex
    applies updatedInput only with it), in bypassPermissions mode or with
    CARDINAL_STORYBOARD_CONTEXT=always.
  - scripts/cardinal-storyboard context prints repo / branch / head_sha /
    paths.

The payloads are the ones captured from codex-cli 0.142.5 (2026-10-03, a
scratch CODEX_HOME with capture-only hooks and a stub "cardinal" MCP
server); only cwd / transcript_path are replaced.

Run: cd adapters/codex && python3 -m unittest tests.test_codex_storyboard -v
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
HOOK = ADAPTER / "hooks" / "cardinal-codex-telemetry.py"
CLI = ADAPTER / "scripts" / "cardinal-storyboard"
SB1 = "sb_0123456789abcdef01234567"
SESSION = "01a104ae-262f-7e50-8a1e-b29c98cd95d5"
VERSION = json.loads((ADAPTER / ".codex-plugin" / "plugin.json").read_text())["version"]

if not (ADAPTER / "hooks" / "cardinal_core" / "storyboard_agent.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "codex"],
                   check=True, capture_output=True)

# --- captured (codex-cli 0.142.5) -------------------------------------------
COMMON = {"session_id": SESSION, "turn_id": "01a104ae-26de-7203-bb5d-372b8b5841b3",
          "transcript_path": "<transcript>", "cwd": "<cwd>", "model": "gpt-5.5",
          "permission_mode": "bypassPermissions"}
SESSION_START = {"session_id": SESSION, "transcript_path": "<transcript>", "cwd": "<cwd>",
                 "hook_event_name": "SessionStart", "model": "gpt-5.5",
                 "permission_mode": "bypassPermissions", "source": "startup"}
POST_ADD = {**COMMON, "hook_event_name": "PostToolUse", "tool_name": "apply_patch",
            "tool_input": {"command": "*** Begin Patch\n*** Add File: hello.txt\n+hi\n*** End Patch\n"},
            "tool_response": "Exit code: 0\nWall time: 0 seconds\nOutput:\nSuccess. Updated the following "
                             "files:\nA hello.txt\n",
            "tool_use_id": "call_0lNLLVpJpUWtbloVZdCznbKu"}
POST_UPDATE = {**COMMON, "hook_event_name": "PostToolUse", "tool_name": "apply_patch",
               "tool_input": {"command": "*** Begin Patch\n*** Update File: hello.txt\n@@\n-hi\n+hello\n"
                                         "*** End Patch\n"},
               "tool_response": "Exit code: 0\nWall time: 0 seconds\nOutput:\nSuccess. Updated the following "
                                "files:\nM hello.txt\n",
               "tool_use_id": "call_VHITYhin9NY03FTzGVaGduO8"}
PRE_CREATE = {**COMMON, "hook_event_name": "PreToolUse", "tool_name": "mcp__cardinal__storyboard__create",
              "tool_input": {"title": "[ASSOC-TEST] capture",
                             "context": {"session_id": "4b85836f-8618-4edc-a72f-80bdbcfe72e8"}},
              "tool_use_id": "call_QY3bnnAE740hVn4BhalassU4"}
# The call the rewrite was proven on: no context ("x" in the capture).
PRE_CREATE_BARE = {**PRE_CREATE, "tool_input": {"title": "[ASSOC-TEST] x"},
                   "tool_use_id": "call_535dD0bTcqC0LIXxhKn84nF2"}


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


class CodexStoryboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.home, self.repo, stub = base / "home", base / "work", base / "stub"
        for d in (self.home / ".codex", self.repo, stub):
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
        codex = self.home / ".codex"
        (codex / "cardinal.json").write_text(json.dumps({"mcp_url": f"{self.fake.origin}/api/orgs/org-1/mcp",
                                                         "user_email": "dev@example.com"}))
        (codex / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "ck_codex_test"}))

    def register_stamping(self):
        """~/.codex/hooks.json as cardinal-connect writes the PreToolUse group."""
        (self.home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "^mcp__cardinal__storyboard__(create|add_act|publish|find)$",
            "hooks": [{"type": "command", "timeout": 5, "command":
                       f"python3 {self.home}/.codex/cardinal/cardinal-codex-telemetry.py "
                       "--event StoryboardContext # cardinal-codex-plugin"}]}]}}))

    def session_context(self, payload: dict = None, **env: str) -> str:
        out = self.run_hook("SessionStart", payload or SESSION_START, **env).stdout
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def env(self, **extra: str) -> dict:
        env = {"HOME": str(self.home), "PATH": self.path}
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

    # --- SessionStart -------------------------------------------------------

    def test_session_start_names_the_session_and_discovers(self):
        self.connect()
        self.register_stamping()
        out = json.loads(self.run_hook("SessionStart", SESSION_START).stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        ctx = out["additionalContext"]
        self.assertIn("Would you like me to update the storyboard visualization?", ctx)
        self.assertIn("Wait for an affirmative reply", ctx)
        self.assertIn("Do not offer again", ctx)
        self.assertIn("one branch = one initiative", ctx)  # the convention prompt is kept
        self.assertIn(f"Cardinal session id for this session: {SESSION}.", ctx)
        self.assertIn(f'`python3 "{CLI}" context --bare --session-id {SESSION}`', ctx)
        self.assertIn("the plugin fills session_id and context when you leave them out", ctx)
        self.assertIn(f"{SB1} · written from branch fix/checkout (subject not confirmed)", ctx)
        find = next(r for r in self.fake.requests if r["path"].endswith("/find"))
        self.assertEqual(find["path"], "/api/orgs/org-1/storyboards/mcp-tools/find")
        self.assertEqual(find["headers"]["x-cardinal-client"], f"codex/{VERSION}")
        self.assertEqual(find["headers"]["x-cardinalhq-api-key"], "ck_codex_test")

    CLI_WORDING = "pass as `context` the JSON object `python3 \"{cli}\" context --bare --session-id {sid}` prints, exactly as printed"

    def assert_cli_wording(self, ctx: str):
        self.assertIn(self.CLI_WORDING.format(cli=CLI, sid=SESSION), ctx)
        self.assertNotIn("the plugin fills", ctx)

    def test_session_start_default_mode_points_at_the_cli(self):
        self.connect()
        self.register_stamping()
        self.assert_cli_wording(self.session_context({**SESSION_START, "permission_mode": "default"}))

    def test_session_start_without_registered_stamping_points_at_the_cli(self):
        # Upgraded plugin, hooks.json not yet repaired: no PreToolUse group.
        self.connect()
        self.assert_cli_wording(self.session_context())
        self.assert_cli_wording(self.session_context(CARDINAL_STORYBOARD_CONTEXT="always"))

    def test_session_start_context_opt_out_points_at_the_cli(self):
        self.connect()
        self.register_stamping()
        self.assert_cli_wording(self.session_context(CARDINAL_STORYBOARD_CONTEXT="0"))

    def test_session_start_not_connected_is_unchanged(self):
        ctx = json.loads(self.run_hook("SessionStart", SESSION_START).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Cardinal session id", ctx)
        self.assertEqual(self.fake.requests, [])

    # --- edited files --------------------------------------------------------

    def test_apply_patch_edits_reach_cli_context(self):
        (self.repo / "hello.txt").write_text("hi\n")
        self.run_hook("ToolEvidence", POST_ADD)
        self.run_hook("ToolEvidence", POST_UPDATE)
        ctx = self.cli_context()
        head = _git(self.repo, "rev-parse", "HEAD")
        self.assertEqual((ctx["repo"], ctx["branch"], ctx["head_sha"]), ("acme/widgets", "fix/checkout", head))
        self.assertEqual(ctx["paths"], ["hello.txt"])

    def test_failed_apply_patch_is_not_recorded(self):
        failed = {**POST_ADD, "tool_response": "Exit code: 1\nWall time: 0 seconds\nOutput:\nerror: invalid hunk\n"}
        self.run_hook("ToolEvidence", failed)
        self.assertNotIn("paths", self.cli_context())

    # --- PreToolUse stamping -------------------------------------------------

    def stamped(self, payload: dict, **env: str):
        out = self.run_hook("StoryboardContext", {**payload, "hook_event_name": "PreToolUse"}, **env).stdout
        return json.loads(out)["hookSpecificOutput"] if out.strip() else None

    def test_bare_create_is_stamped_with_allow(self):
        out = self.stamped(PRE_CREATE_BARE)
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "allow")
        got = out["updatedInput"]
        self.assertEqual(got["title"], "[ASSOC-TEST] x")
        self.assertEqual(got["session_id"], SESSION)
        self.assertEqual(got["context"]["repo"], "acme/widgets")
        self.assertEqual(got["context"]["client"], f"codex/{VERSION}")

    def test_model_context_is_kept(self):
        got = self.stamped(PRE_CREATE)["updatedInput"]
        self.assertEqual(got["context"], {"session_id": "4b85836f-8618-4edc-a72f-80bdbcfe72e8"})
        self.assertEqual(got["session_id"], SESSION)

    def test_prompting_mode_is_not_stamped_unless_opted_in(self):
        default = {**PRE_CREATE_BARE, "permission_mode": "default"}
        self.assertIsNone(self.stamped(default))
        self.assertIsNotNone(self.stamped(default, CARDINAL_STORYBOARD_CONTEXT="always"))
        self.assertIsNone(self.stamped(PRE_CREATE_BARE, CARDINAL_STORYBOARD_CONTEXT="0"))

    def test_other_tools_are_untouched(self):
        self.assertIsNone(self.stamped({**PRE_CREATE_BARE, "tool_name": "mcp__other__storyboard__create"}))
        self.assertIsNone(self.stamped({**PRE_CREATE_BARE, "tool_name": "mcp__cardinal__storyboard__link"}))


if __name__ == "__main__":
    unittest.main()

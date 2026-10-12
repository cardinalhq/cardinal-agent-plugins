"""The Investigation layer and storyboard authoring in the Gemini CLI
adapter, against a fake maestro (mirrors the Claude tests
test_owner_input_hook.py, test_investigation_events_hook.py,
test_storyboard_token.py, test_storyboard_edit_lookup.py and the Codex
test_codex_investigation.py):

  - SessionStart binds the session (investigation_agent.start) and puts the
    guidance with the labeled links in additionalContext; stray-key warning.
  - BeforeAgent records what the owner typed (owner_input.capture,
    detached: the hook never waits on the network): the one-time disclosure
    as systemMessage first, then exactly the typed text — Gemini's
    <hook_context> and @file sections removed, custom-command expansions
    and loop-recovery prompts never captured, the same prompt once per turn,
    the kill switch honoured. Staged items arrive as additionalContext.
  - AfterAgent NEVER prints anything (R1): what is deliverable is staged
    and arrives at the next BeforeAgent; it ends the owner-input turn.
  - AfterTool delivers the poller's inbox, stores the evidence token from
    Cardinal's storyboard__create (Gemini's {llmContent} converted to MCP
    shape) and looks up the storyboards about an edited file.
  - AfterTool --role preview hands Cardinal's storyboard__preview result to
    on_preview; the main AfterTool never renders.

Run: cd adapters/gemini && python3 -m unittest tests.test_gemini_investigation -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER = TESTS_DIR.parent
REPO_ROOT = ADAPTER.parent.parent
HOOK = ADAPTER / "hooks" / "cardinal-gemini-telemetry.py"
VERSION = json.loads((ADAPTER / ".gemini-plugin" / "plugin.json").read_text())["version"]

if not (ADAPTER / "hooks" / "cardinal_core" / "owner_input.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "gemini"],
                   check=True, capture_output=True)
sys.path.insert(0, str(ADAPTER / "hooks"))
from cardinal_core import investigation_turn  # noqa: E402

SID = "845f7b2e-62a5-424b-8ae9-164d65ba509c"
INV = "inv_" + "a" * 24
SB = "sb_" + "b" * 24
SB1 = "sb_0123456789abcdef01234567"
SB_PATH = "sb_" + "c" * 24
TYPED = "why is checkout p99 up?"
DISCLOSURE = (f"This session's prompts are recorded to Investigation {INV} as owner input (credentials scrubbed, "
              "up to 32 KiB); only you and grantees you authorize can read them.")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c",
                           "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


class FakeMaestro:
    def __init__(self, owner_input: bool = True, author: bool = True) -> None:
        self.requests: list = []
        self.events: list = []
        self.owner_input = owner_input
        self.author = author
        self.record_delay = 0.0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                tool = self.path.rsplit("/", 1)[-1]
                fake.requests.append({"tool": tool, "path": self.path, "body": body,
                                      "headers": {k.lower(): v for k, v in self.headers.items()}})
                status, out = fake.answer(tool, body)
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
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def records(self) -> list:
        return [r["body"] for r in self.requests if r["tool"] == "record-owner-input"]

    def answer(self, tool: str, body: dict):
        if tool == "ensure-session-investigation":
            return 200, {"investigation_id": INV, "storyboard_id": SB,
                         "view_url": f"{self.origin}/storyboards/{SB}",
                         "investigation_url": f"/storyboards/{SB}/investigation",
                         "is_author": self.author,
                         "capabilities": {"projection": {"enabled": True},
                                          "owner_input": {"enabled": self.owner_input}}}
        if tool == "record-owner-input":
            if self.record_delay:
                time.sleep(self.record_delay)
            e = {"investigation_id": body["investigation_id"], "seq": len(self.events) + 1,
                 "type": "owner_input.recorded", "class": "owner_input", "payload": body["payload"]}
            self.events.append(e)
            return 201, {"event": e, "duplicate": False}
        if tool == "read-investigation-events":
            return 200, {"investigation_id": body.get("investigation_id"), "events": [], "head_seq": 0}
        if tool == "find" and (body.get("refs") or {}).get("paths"):
            return 200, {"matches": [{
                "storyboard_id": SB_PATH, "question": "Why is the cache keyed by tenant?", "status": "published",
                "act_count": 1, "match": "path", "match_role": "about",
                "matched": {"kind": "path", "value": "svc/a.py", "repo": "acme/widgets"},
                "context": {"repo": "acme/widgets"}}], "rule": "r", "associations_api": 1}
        if tool == "find":
            return 200, {"matches": [{
                "storyboard_id": SB1, "question": "Why did checkout p99 regress?", "status": "published",
                "act_count": 1, "match": "branch", "match_role": "written_from", "match_act": 1,
                "context": {"repo": "acme/widgets", "branch": "fix/checkout"},
                "view_url": f"https://app.cardinalhq.io/storyboards/{SB1}"}], "rule": "r", "associations_api": 1}
        if tool == "get":
            return 200, {"storyboard_id": SB1, "scenes": []}
        return 404, {}


class GeminiInvestigationTests(unittest.TestCase):
    owner_input = True

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
        self.fake = FakeMaestro(owner_input=self.owner_input)
        gemini = self.home / ".gemini"
        (gemini / "cardinal.json").write_text(json.dumps({"mcp_url": f"{self.fake.origin}/api/orgs/org-1/mcp",
                                                          "user_email": "dev@example.com"}))
        (gemini / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "ck_gemini_test"}))
        # In-process hook calls (load_hook) must never touch the real home.
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home), "CARDINAL_INVESTIGATION_POLLER": "0"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.fake.stop()
        self.tmp.cleanup()

    # --- plumbing ------------------------------------------------------------

    def env(self, **extra: str) -> dict:
        env = {"HOME": str(self.home), "PATH": self.path, "CARDINAL_GEMINI_INLINE_BACKGROUND": "1",
               "CARDINAL_INVESTIGATION_POLLER": "0"}
        env.update(extra)
        return env

    def run_hook(self, event: str, payload: dict, *args: str, **env: str) -> subprocess.CompletedProcess:
        payload = {"session_id": SID, "cwd": str(self.repo), "hook_event_name": event,
                   "timestamp": "2026-10-12T00:00:00.000Z", **payload}
        res = subprocess.run([sys.executable, str(HOOK), "--event", event, *args], input=json.dumps(payload),
                             capture_output=True, text=True, env=self.env(**env), cwd=str(self.repo), timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def out(self, res: subprocess.CompletedProcess) -> dict:
        return json.loads(res.stdout) if res.stdout.strip() else {}

    def start(self, **env: str) -> str:
        res = self.run_hook("SessionStart", {"source": "startup"}, **env)
        return self.out(res).get("hookSpecificOutput", {}).get("additionalContext", "")

    def prompt(self, text: str, **env: str) -> dict:
        return self.out(self.run_hook("BeforeAgent", {"prompt": text}, **env))

    def after_agent(self, **payload: Any) -> subprocess.CompletedProcess:
        return self.run_hook("AfterAgent", {"prompt": "x", "prompt_response": "done",
                                            "stop_hook_active": False, **payload})

    def wait_records(self, n: int, timeout: float = 15.0) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.fake.records()) >= n:
                break
            time.sleep(0.1)
        return self.fake.records()

    @property
    def sessions(self) -> Path:
        return self.home / ".cardinal" / "investigations" / "sessions"

    def stage(self, text: str) -> None:
        investigation_turn.stage(self.home, SID, text)

    def cue_inbox(self, text: str) -> None:
        binding = json.loads((self.sessions / f"{SID}.json").read_text())
        cue = {"investigation_id": INV, "seq": 12, "type": "cue.added", "class": "control",
               "authority": "advisory", "created_at": "2026-10-12T00:47:48Z",
               "producer": {"principal": {"kind": "user", "id": "supervisor"},
                            "session_id": "supervisor-session", "is_investigation_author": True},
               "payload": {"text": text}}
        (self.sessions / f"{SID}.inbox.json").write_text(json.dumps(
            {"investigation_id": INV, "after": binding.get("cursor", 0), "through": 12, "events": [cue]}))


class SessionStartTests(GeminiInvestigationTests):
    def test_binds_and_gives_labeled_links(self):
        ctx = self.start()
        self.assertIn(f"Storyboard (private draft until published): {self.fake.origin}/storyboards/{SB}", ctx)
        self.assertIn("Investigation timeline (shows the live record before any publish):", ctx)
        self.assertIn(f"/storyboards/{SB}/investigation", ctx)
        self.assertIn("one branch = one initiative", ctx)
        # The guidance names the session; the separate session line is not repeated.
        self.assertEqual(ctx.count(f"Cardinal session id for this session: {SID}."), 1)
        ensure = next(r for r in self.fake.requests if r["tool"] == "ensure-session-investigation")
        self.assertEqual(ensure["body"]["session_id"], SID)
        self.assertEqual(ensure["headers"]["x-cardinal-client"], f"gemini/{VERSION}")
        self.assertEqual(ensure["body"]["context"]["branch"], "fix/checkout")
        self.assertNotIn("prompt", ensure["body"])
        self.assertTrue((self.sessions / f"{SID}.json").exists())

    def test_session_start_never_captures(self):
        self.start()
        self.assertEqual(self.fake.records(), [])

    def test_stray_key_warning(self):
        (self.home / ".gemini" / "cardinal.json").write_text(json.dumps({"user_email": "dev@example.com"}))
        ctx = self.start()
        self.assertIn("Tell the user, briefly: Cardinal: a Cardinal MCP API key is set", ctx)
        self.assertNotIn("Tell the user, briefly", self.start(), "once per session")


class OwnerInputTests(GeminiInvestigationTests):
    def disclosed(self) -> dict:
        self.start()
        out = self.prompt("first prompt")
        self.assertIn(DISCLOSURE, out.get("systemMessage", ""))
        self.assertIn('{"gemini": "off"}', out["systemMessage"])
        self.assertIn("CARDINAL_OWNER_INPUT=0", out["systemMessage"])
        return out

    def test_first_prompt_discloses_and_is_not_captured(self):
        out = self.disclosed()
        self.assertNotIn("decision", out)
        time.sleep(1.0)
        self.assertEqual(self.fake.records(), [])
        # The disclosure is shown once.
        self.after_agent()
        self.assertNotIn("systemMessage", self.prompt(TYPED))

    def test_typed_text_is_recorded_without_hook_context_or_files(self):
        self.disclosed()
        self.after_agent()
        raw = ("<hook_context>Cardinal session id for this session: x.</hook_context>\n\n"
               f"{TYPED} see @notes.txt\n--- Content from referenced files ---\nContent from @notes.txt:\n"
               "SECRET-FILE-BODY\n--- End of content ---")
        self.prompt(raw)
        records = self.wait_records(1)
        self.assertEqual(len(records), 1)
        payload = records[0]["payload"]
        self.assertEqual(payload["text"], f"{TYPED} see @notes.txt")
        self.assertEqual(payload["turn"], 1)
        self.assertEqual(records[0]["investigation_id"], INV)
        self.assertEqual(records[0]["session_id"], SID)
        req = next(r for r in self.fake.requests if r["tool"] == "record-owner-input")
        self.assertEqual(req["headers"]["x-cardinal-client"], f"gemini/{VERSION}")
        self.assertNotIn("hook_context", json.dumps(records))
        self.assertNotIn("SECRET-FILE-BODY", json.dumps(records))

    def test_capture_never_waits_on_the_network(self):
        self.disclosed()
        self.fake.record_delay = 4.0
        started = time.monotonic()
        self.prompt(TYPED)
        self.assertLess(time.monotonic() - started, 3.5, "BeforeAgent waited on record-owner-input")
        self.assertEqual(len(self.wait_records(1)), 1)

    def test_same_prompt_once_per_turn(self):
        self.disclosed()
        self.prompt(TYPED)
        self.assertEqual(len(self.wait_records(1)), 1)
        self.prompt(TYPED)          # a re-fire inside the same turn
        time.sleep(1.0)
        self.assertEqual(len(self.fake.records()), 1)
        self.after_agent()          # the turn ended: the same text is a new owner turn
        self.prompt(TYPED)
        self.assertEqual(len(self.wait_records(2)), 2)

    def test_custom_command_expansion_is_not_captured(self):
        commands = self.home / ".gemini" / "commands"
        commands.mkdir(parents=True)
        (commands / "greet.toml").write_text('description = "Greets"\n'
                                             'prompt = """Say hello to {{args}} in exactly one word."""\n')
        (commands / "review.toml").write_text('prompt = "Review the staged diff carefully and list the bugs."\n')
        self.disclosed()
        self.prompt("Say hello to Bob and Alice in exactly one word.")
        self.prompt("Review the staged diff carefully and list the bugs.\n\n/review now")
        self.prompt("<hook_context>c</hook_context>\n\n/greet Bob")   # -p with hook context: not expanded
        records = self.wait_records(1)
        time.sleep(1.0)
        self.assertEqual([r["payload"]["text"] for r in self.fake.records()], ["/greet Bob"])

    def test_loop_recovery_prompt_is_not_captured(self):
        self.disclosed()
        self.prompt("System: Potential loop detected. Details: repetitive tool calls. Please take a step back.")
        time.sleep(1.0)
        self.assertEqual(self.fake.records(), [])

    def test_kill_switch_env(self):
        self.start()
        out = self.prompt(TYPED, CARDINAL_OWNER_INPUT="0")
        self.assertNotIn("systemMessage", out)
        out = self.prompt("again", CARDINAL_OWNER_INPUT="off")
        self.assertNotIn("systemMessage", out)
        time.sleep(1.0)
        self.assertEqual(self.fake.records(), [])

    def test_kill_switch_file(self):
        self.disclosed()
        (self.home / ".cardinal" / "owner-input.json").write_text(json.dumps({"gemini": "off"}))
        self.after_agent()
        self.prompt(TYPED)
        time.sleep(1.0)
        self.assertEqual(self.fake.records(), [])
        # Removing the switch tells the user again before capture resumes.
        (self.home / ".cardinal" / "owner-input.json").unlink()
        self.assertIn(DISCLOSURE, self.prompt("later").get("systemMessage", ""))

    def test_subagent_prompt_is_not_captured(self):
        self.disclosed()
        self.run_hook("BeforeAgent", {"prompt": TYPED, "agent_id": "sub-1"})
        time.sleep(1.0)
        self.assertEqual(self.fake.records(), [])

    def prompt_with_checked_stdout(self, fail_write: bool) -> str:
        """BeforeAgent with a stdout that notes, at every write, whether the
        binding already says the user was told (and can fail the write)."""
        log = self.home / "writes.log"
        binding = self.sessions / f"{SID}.json"
        wrapper = (
            "import json, runpy, sys\n"
            f"BINDING, LOG, FAIL = {str(binding)!r}, {str(log)!r}, {fail_write!r}\n"
            "class Out:\n"
            "    def write(self, s):\n"
            "        b = json.load(open(BINDING))\n"
            "        open(LOG, 'a').write(repr(b.get('owner_input_disclosed')) + ' ' + str('systemMessage' in s) + '\\n')\n"
            "        if FAIL:\n"
            "            raise BrokenPipeError('closed')\n"
            "        return len(s)\n"
            "    def flush(self):\n"
            "        pass\n"
            "sys.stdout = Out()\n"
            f"sys.argv = [{str(HOOK)!r}, '--event', 'BeforeAgent']\n"
            f"runpy.run_path({str(HOOK)!r}, run_name='__main__')\n")
        payload = {"session_id": SID, "cwd": str(self.repo), "hook_event_name": "BeforeAgent",
                   "timestamp": "2026-10-12T00:00:00.000Z", "prompt": TYPED}
        res = subprocess.run([sys.executable, "-c", wrapper], input=json.dumps(payload), capture_output=True,
                             text=True, env=self.env(), cwd=str(self.repo), timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return log.read_text()

    def test_the_disclosure_is_recorded_only_after_it_was_flushed(self):
        self.start()
        self.assertEqual(self.prompt_with_checked_stdout(fail_write=False), "None True\n")
        self.assertIs(json.loads((self.sessions / f"{SID}.json").read_text())["owner_input_disclosed"], True)
        time.sleep(0.5)
        self.assertEqual(self.fake.records(), [])

    def test_a_failed_write_records_no_disclosure(self):
        self.start()
        self.assertEqual(self.prompt_with_checked_stdout(fail_write=True), "None True\n")
        self.assertNotIn("owner_input_disclosed", json.loads((self.sessions / f"{SID}.json").read_text()))
        self.assertIn(DISCLOSURE, self.prompt("told again").get("systemMessage", ""))
        time.sleep(0.5)
        self.assertEqual(self.fake.records(), [])


class CapabilityOffTests(GeminiInvestigationTests):
    owner_input = False

    def test_capability_off_captures_and_discloses_nothing(self):
        self.start()
        self.assertNotIn("systemMessage", self.prompt(TYPED))
        self.assertNotIn("systemMessage", self.prompt("again"))
        time.sleep(1.0)
        self.assertEqual(self.fake.records(), [])


class TurnTests(GeminiInvestigationTests):
    def test_after_agent_never_blocks_and_stages_the_inbox(self):
        self.start()
        self.cue_inbox("Inspect accounting headers.")
        for active in (False, True):
            res = self.after_agent(stop_hook_active=active)
            self.assertEqual(res.stdout, "", "AfterAgent must never print (no block, no decision)")
        staged = investigation_turn.staged_path(self.home, SID)
        self.assertTrue(staged.exists(), "the turn end stages what it could not deliver")
        self.assertFalse((self.sessions / f"{SID}.inbox.json").exists())
        out = self.prompt("next")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Inspect accounting headers.", ctx)
        self.assertIn("authority: ADVISORY", ctx)
        self.assertNotIn("decision", out)
        self.assertFalse(staged.exists())

    def test_after_agent_never_prints_even_if_core_offers_a_block(self):
        self.start()
        from cardinal_core import investigation_turn as turn
        mod = load_hook()
        with mock.patch("cardinal_core.investigation_agent.turn_end",
                        return_value=turn.Delivery(["ctx"], "user", "BLOCK")) as te:
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch.object(sys, "argv", ["hook", "--event", "AfterAgent"]), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                        {"session_id": SID, "prompt": "p", "prompt_response": "r", "stop_hook_active": False}))):
                with self.assertRaises(SystemExit):
                    mod.main()
            te.assert_called_once()
            self.assertEqual(buf.getvalue(), "")

    def test_staged_items_reach_the_next_prompt(self):
        self.start()
        self.stage("STAGED-DISCOVERY-BLOCK")
        ctx = self.prompt("next")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("STAGED-DISCOVERY-BLOCK", ctx)
        self.assertNotIn("STAGED-DISCOVERY-BLOCK", json.dumps(self.prompt("again")))

    def test_tool_boundary_delivers_the_inbox(self):
        self.start()
        self.cue_inbox("Check pod placement.")
        res = self.run_hook("AfterTool", {"tool_name": "run_shell_command",
                                          "tool_input": {"command": "git status --short"},
                                          "tool_response": {"llmContent": "Output: (empty)", "returnDisplay": ""}})
        ctx = self.out(res)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[evidence:ev_", ctx)
        self.assertIn("Check pod placement.", ctx)
        self.assertIn("authority: ADVISORY", ctx)
        self.assertEqual(json.loads((self.sessions / f"{SID}.json").read_text())["cursor"], 12)


class AuthoringTests(GeminiInvestigationTests):
    def test_evidence_token_from_create(self):
        result = {"storyboard_id": SB, "evidence_token": "eyJhbGciOiJIUzI1NiJ9.eyJzYiI6InNiIn0.c2ln",
                  "evidence_token_expires_at": "2026-10-13T00:00:00Z",
                  "evidence_upload": {"path": f"/api/orgs/org-1/storyboards/{SB}/evidence"}}
        payload = {"tool_name": "mcp_cardinal_storyboard__create", "tool_input": {"title": "t"},
                   "mcp_context": {"server_name": "cardinal", "tool_name": "storyboard__create"},
                   "tool_response": {"llmContent": [{"text": "<untrusted_context>\n" + json.dumps(result)
                                                     + "\n</untrusted_context>"}],
                                     "returnDisplay": "ok"}}
        self.run_hook("AfterTool", payload)
        token = json.loads((self.home / ".cardinal" / "evidence" / SID / "token.json").read_text())
        self.assertIn(SB, json.dumps(token))
        # Another server's tool of the same name never stores a token.
        (self.home / ".cardinal" / "evidence" / SID / "token.json").unlink()
        other = {**payload, "tool_name": "mcp_other_storyboard__create",
                 "mcp_context": {"server_name": "other", "tool_name": "storyboard__create"}}
        self.run_hook("AfterTool", other)
        self.assertFalse((self.home / ".cardinal" / "evidence" / SID / "token.json").exists())

    def test_edit_lookup_after_write_file(self):
        self.start()       # discovery caches the server's associations capability
        sub = self.repo / "svc"
        sub.mkdir()
        (sub / "a.py").write_text("x\n")
        res = self.run_hook("AfterTool", {"tool_name": "write_file",
                                          "tool_input": {"file_path": "svc/a.py", "content": "x"},
                                          "tool_response": {"llmContent": "Successfully wrote", "returnDisplay": {
                                              "filePath": str(sub / "a.py")}}}, CARDINAL_EVIDENCE_CAPTURE="0")
        ctx = self.out(res)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(SB_PATH, ctx)
        find = [r for r in self.fake.requests if r["tool"] == "find"][-1]
        self.assertEqual(find["body"]["refs"], {"repo": "acme/widgets", "paths": ["svc/a.py"]})
        self.assertEqual(find["headers"]["x-cardinal-client"], f"gemini/{VERSION}")
        # Once per directory.
        again = self.run_hook("AfterTool", {"tool_name": "replace", "tool_input": {"file_path": "svc/a.py"},
                                            "tool_response": {"llmContent": "ok", "returnDisplay": ""}},
                              CARDINAL_EVIDENCE_CAPTURE="0")
        self.assertNotIn(SB_PATH, again.stdout)

    def test_main_after_tool_does_not_render_previews(self):
        mod = load_hook()
        with mock.patch("cardinal_core.storyboard_authoring.on_preview") as on_preview, \
                redirect_stdout(io.StringIO()):
            mod.handle_after_tool({"session_id": SID, "tool_name": "mcp_cardinal_storyboard__preview",
                                   "mcp_context": {"server_name": "cardinal", "tool_name": "storyboard__preview"},
                                   "tool_response": {"llmContent": "{}", "returnDisplay": ""}})
        on_preview.assert_not_called()

    def test_preview_role_renders_cardinal_previews_in_mcp_shape(self):
        mod = load_hook()
        result = {"storyboard_id": SB, "revision": 3, "scenes": [{"scene_id": "s1"}]}
        payload = {"session_id": SID, "tool_name": "mcp_cardinal_storyboard__preview", "tool_input": {"x": 1},
                   "mcp_context": {"server_name": "cardinal", "tool_name": "storyboard__preview"},
                   "tool_response": {"llmContent": [{"text": "<untrusted_context>\n" + json.dumps(result)
                                                     + "\n</untrusted_context>"}], "returnDisplay": ""}}
        buf = io.StringIO()
        with mock.patch("cardinal_core.storyboard_authoring.on_preview", return_value="PNGS HERE") as on_preview, \
                redirect_stdout(buf):
            mod.ROLE_HANDLERS[("AfterTool", "preview")](payload)
        args, kwargs = on_preview.call_args
        self.assertEqual(args[1], "gemini")
        self.assertEqual(args[2], {"content": [{"type": "text", "text": json.dumps(result)}]})
        self.assertEqual(kwargs["session_id"], SID)
        self.assertEqual(kwargs["tool_input"], {"x": 1})
        out = json.loads(buf.getvalue())["hookSpecificOutput"]
        self.assertEqual(out, {"hookEventName": "AfterTool", "additionalContext": "PNGS HERE"})
        # Another server's preview is never rendered.
        on_preview.reset_mock()
        with mock.patch("cardinal_core.storyboard_authoring.on_preview") as on_preview, \
                redirect_stdout(io.StringIO()):
            mod.ROLE_HANDLERS[("AfterTool", "preview")](
                {**payload, "mcp_context": {"server_name": "other", "tool_name": "storyboard__preview"}})
        on_preview.assert_not_called()


def load_hook():
    name = f"gemini_hook_{time.time_ns()}"
    loader = importlib.machinery.SourceFileLoader(name, str(HOOK))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    unittest.main()

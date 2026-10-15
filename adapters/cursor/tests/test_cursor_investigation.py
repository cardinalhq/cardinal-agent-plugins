"""Investigation sessions, owner input and storyboard authoring in the Cursor
adapter (docs/plans/cross-agent-parity.md, Lane D):

  - sessionStart: investigation_agent.start + the poller, the session
    guidance (labeled links) as additional_context; what an earlier turn
    staged; the connect hint and the stray-key warning.
  - postToolUse / postToolUseFailure: investigation_agent.deliver (staged
    items, the inbox, re-surfacing), the plugin's own staged items (edit
    lookup, discovery re-look); a subagent's tool call gets nothing.
  - stop: investigation_agent.turn_end. It STAGES and prints nothing: never
    followup_message (R1: Cursor submits it as the next user message).
  - beforeSubmitPrompt: owner input only with the user's connect-time
    consent (R3), detached; never blocks, never prints.
  - afterMCPExecution + the `--role authoring` postToolUse entry: Cardinal's
    storyboard__preview / __publish, whichever MCP name shape Cursor sends.

NO Cursor payload was captured (Cursor is not installed where these were
written): every payload below follows Cursor's hooks documentation
(cursor.com/docs/agent/hooks) and is UNVERIFIED on a live Cursor.

Run: cd adapters/cursor && python3 -m unittest discover tests -p test_cursor_investigation.py -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path

import test_cursor_storyboard as fixtures
from test_cursor_storyboard import ADAPTER, CONNECT, CONV, HOOK, SB1, VERSION

INV = "inv_" + "a" * 24
SB = "sb_" + "b" * 24
SB2 = "sb_" + "c" * 24
STATUS = ADAPTER / "scripts" / "cardinal-status"
DISCONNECT = ADAPTER / "scripts" / "cardinal-disconnect"

sys.path.insert(0, str(ADAPTER / "hooks"))
from cardinal_core import owner_input, investigation_turn  # noqa: E402

# --- from Cursor's hooks docs, UNVERIFIED -------------------------------------
SESSION_START = {"conversation_id": CONV, "session_id": CONV, "generation_id": "g-1",
                 "hook_event_name": "sessionStart", "workspace_roots": ["<cwd>"],
                 "is_background_agent": False, "composer_mode": "agent"}
PROMPT = {"conversation_id": CONV, "generation_id": "g-2", "hook_event_name": "beforeSubmitPrompt",
          "workspace_roots": ["<cwd>"], "prompt": "why is checkout slow?", "attachments": []}
SHELL = {"conversation_id": CONV, "generation_id": "g-2", "hook_event_name": "postToolUse",
         "workspace_roots": ["<cwd>"], "cwd": "<cwd>", "tool_name": "Shell",
         "tool_input": {"command": "git status --short"},
         "tool_output": json.dumps({"exitCode": 0, "stdout": ""}), "tool_use_id": "call_status",
         "duration": 12}
SHELL_FAILURE = {"conversation_id": CONV, "generation_id": "g-2", "hook_event_name": "postToolUseFailure",
                 "workspace_roots": ["<cwd>"], "tool_name": "Shell", "tool_input": {"command": "make test"},
                 "error_message": "exit status 2", "failure_type": "error", "duration": 40,
                 "is_interrupt": False, "tool_use_id": "call_test"}
STOP = {"conversation_id": CONV, "generation_id": "g-2", "hook_event_name": "stop", "workspace_roots": ["<cwd>"],
        "status": "completed", "loop_count": 0}
AFTER_FILE_EDIT = {"conversation_id": CONV, "generation_id": "g-2", "hook_event_name": "afterFileEdit",
                   "workspace_roots": ["<cwd>"], "file_path": "<cwd>/svc/handler.go",
                   "edits": [{"old_string": "a", "new_string": "b"}]}


def preview_output(storyboard_id: str = SB1) -> str:
    """storyboard__preview's result as Cursor documents tool_output: a
    JSON-stringified MCP result."""
    result = {"storyboard_id": storyboard_id, "revision": 3, "scenes": [{"id": "s1"}]}
    return json.dumps({"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result})


def load_script(path: Path, name: str):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TTYIn(io.StringIO):
    def isatty(self) -> bool:
        return True


class CursorInvestigationTests(unittest.TestCase):
    setUp = fixtures.CursorStoryboardTests.setUp
    tearDown = fixtures.CursorStoryboardTests.tearDown
    connect = fixtures.CursorStoryboardTests.connect

    # --- helpers --------------------------------------------------------------

    def env(self, **extra: str) -> dict:
        env = {"HOME": str(self.home), "PATH": self.path, "CARDINAL_CURSOR_BACKGROUND_INLINE": "1",
               "CARDINAL_INVESTIGATION_POLLER": "0"}
        env.update(extra)
        return env

    def run_hook(self, payload: dict, *args: str, **env: str) -> subprocess.CompletedProcess:
        payload = json.loads(json.dumps(payload).replace("<cwd>", str(self.repo)))
        res = subprocess.run([sys.executable, str(HOOK), *args], input=json.dumps(payload), capture_output=True,
                             text=True, env=self.env(**env), cwd=str(self.repo), timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def context(self, res: subprocess.CompletedProcess) -> str:
        return json.loads(res.stdout)["additional_context"] if res.stdout.strip() else ""

    def setup_live(self, owner_input_on: bool = True, author: bool = True):
        self.connect()
        self.fake.routes["ensure-session-investigation"] = (200, {
            "investigation_id": INV, "storyboard_id": SB,
            "view_url": f"{self.fake.origin}/storyboards/{SB}",
            "investigation_url": f"{self.fake.origin}/storyboards/{SB}/investigation",
            "is_author": author, "capabilities": {"projection": {"enabled": True},
                                                   "owner_input": {"enabled": owner_input_on}}})
        self.fake.routes["record-owner-input"] = (201, {"event": {
            "investigation_id": INV, "seq": 1, "type": owner_input.TYPE, "class": "owner_input",
            "authority": "owner_input_client_attested"}, "duplicate": False})

    def start(self, **env: str) -> str:
        return self.context(self.run_hook(SESSION_START, **env))

    @property
    def sessions(self) -> Path:
        return self.home / ".cardinal" / "investigations" / "sessions"

    def binding(self) -> dict:
        return json.loads((self.sessions / f"{CONV}.json").read_text())

    def put_inbox(self, text: str = "Inspect accounting headers and pod placement.", seq: int = 12) -> Path:
        cue = {"investigation_id": INV, "seq": seq, "type": "cue.added", "class": "control",
               "authority": "advisory", "created_at": "2026-10-12T00:47:48Z",
               "producer": {"principal": {"kind": "user", "id": "supervisor"},
                            "session_id": "supervisor-session", "is_investigation_author": True},
               "payload": {"text": text}}
        inbox = self.sessions / f"{CONV}.inbox.json"
        inbox.write_text(json.dumps({"investigation_id": INV, "after": self.binding()["cursor"],
                                     "through": seq, "events": [cue]}))
        return inbox

    def owner_records(self) -> list:
        return [r for r in self.fake.requests if r["path"].endswith("/record-owner-input")]

    def wait_for_records(self, n: int, timeout: float = 10.0) -> list:
        give_up = time.monotonic() + timeout
        while time.monotonic() < give_up:
            got = self.owner_records()
            if len(got) >= n:
                return got
            time.sleep(0.1)
        return self.owner_records()

    def consent(self, on: bool = True):
        connect = load_script(CONNECT, "cursor_connect_oi")
        out = io.StringIO()
        code = connect.owner_input_consent(on, home=self.home, stdin=TTYIn("yes\n"), stdout=out)
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    # --- sessionStart ---------------------------------------------------------

    def test_session_start_bootstraps_and_gives_labeled_guidance(self):
        self.setup_live()
        ctx = self.start()
        self.assertIn(f"Storyboard (private draft until published): {self.fake.origin}/storyboards/{SB}", ctx)
        self.assertIn("Investigation timeline (shows the live record before any publish)", ctx)
        self.assertIn("--session", ctx)
        self.assertIn(str(ADAPTER / "scripts" / "cardinal-storyboard"), ctx)
        self.assertIn("first progress update", ctx)
        ensures = [r for r in self.fake.requests if r["path"].endswith("/ensure-session-investigation")]
        self.assertEqual(len(ensures), 1)
        self.assertEqual(ensures[0]["body"]["session_id"], CONV)
        self.assertEqual(ensures[0]["headers"]["x-cardinal-client"], f"cursor/{VERSION}")
        self.assertNotIn("prompt", ensures[0]["body"])
        # Owner input is off until the user consents: the guidance does not say prompts are recorded.
        self.assertNotIn("recorded to Investigation", ctx)
        self.assertEqual(self.binding()["investigation_id"], INV)

    def test_session_start_says_prompts_are_recorded_only_with_consent(self):
        self.setup_live()
        self.consent()
        ctx = self.start()
        self.assertIn("recorded to Investigation", ctx)

    def test_session_start_optout_does_not_bootstrap(self):
        self.setup_live()
        self.start(CARDINAL_STORYBOARD_SESSION_START="0")
        self.assertFalse(any(r["path"].endswith("/ensure-session-investigation") for r in self.fake.requests))

    def test_session_start_delivers_what_was_staged(self):
        self.setup_live()
        self.start()
        investigation_turn.stage(self.home, CONV, "STAGED-AT-LAST-TURN-END")
        ctx = self.start()
        self.assertIn("STAGED-AT-LAST-TURN-END", ctx)
        self.assertFalse(investigation_turn.staged_path(self.home, CONV).exists())

    def test_connect_hint_on_an_unconnected_machine(self):
        first = self.start()
        self.assertIn("Tell the user once, briefly: Cardinal is not connected", first)
        self.assertIn("scripts/cardinal-connect", first)
        second = self.start()
        self.assertNotIn("Tell the user once", second)
        self.assertIn("Mention it only if the user asks", second)

    def test_stray_key_warning_once_per_conversation(self):
        (self.home / ".cursor" / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "ck_stray"}))
        first = self.start()
        self.assertIn("Tell the user, briefly: Cardinal: a Cardinal MCP API key is set", first)
        self.assertNotIn("Tell the user, briefly: Cardinal: a Cardinal MCP API key", self.start())

    # --- tool boundaries ------------------------------------------------------

    def test_post_tool_use_delivers_the_inbox(self):
        self.setup_live()
        self.start()
        inbox = self.put_inbox()
        ctx = self.context(self.run_hook(SHELL))
        self.assertIn("authority: ADVISORY", ctx)
        self.assertIn("Inspect accounting headers", ctx)
        self.assertEqual(self.binding()["cursor"], 12)
        self.assertFalse(inbox.exists())

    def test_post_tool_use_failure_delivers_staged_items(self):
        self.setup_live()
        self.start()
        investigation_turn.stage(self.home, CONV, "STAGED-FOR-NEXT-TOOL")
        ctx = self.context(self.run_hook(SHELL_FAILURE))
        self.assertIn("STAGED-FOR-NEXT-TOOL", ctx)
        self.assertIn("[evidence:ev_", ctx)

    def test_subagent_tool_call_gets_nothing_and_consumes_nothing(self):
        self.setup_live()
        self.start()
        inbox = self.put_inbox()
        investigation_turn.stage(self.home, CONV, "STAGED-FOR-MAIN-THREAD")
        ctx = self.context(self.run_hook({**SHELL, "agent_id": "sub-1"}))
        self.assertNotIn("STAGED-FOR-MAIN-THREAD", ctx)
        self.assertNotIn("Inspect accounting headers", ctx)
        self.assertTrue(inbox.exists())
        main = self.context(self.run_hook(SHELL))
        self.assertIn("STAGED-FOR-MAIN-THREAD", main)
        self.assertIn("Inspect accounting headers", main)

    def test_unbound_conversation_still_gets_the_plugins_staged_items(self):
        self.connect()
        investigation_turn.stage(self.home, CONV, "EDIT-LOOKUP-BLOCK")
        self.assertIn("EDIT-LOOKUP-BLOCK", self.context(self.run_hook(SHELL)))

    def test_after_file_edit_lookup_is_staged_and_surfaced_at_the_next_tool(self):
        self.connect()
        (self.repo / "svc").mkdir()
        (self.repo / "svc" / "handler.go").write_text("package svc\n")
        # The edit lookup needs the server's associations capability (cached by discovery).
        self.start()
        self.fake.requests.clear()
        self.fake.routes["find"] = (200, {"matches": [{
            "storyboard_id": SB2, "question": "Why does the handler retry twice?", "status": "published",
            "act_count": 1, "match": "path", "match_role": "about",
            "matched": {"kind": "path", "value": "svc/handler.go", "repo": "acme/widgets"},
            "context": {"repo": "acme/widgets"}}], "rule": "r", "associations_api": 1})
        res = self.run_hook(AFTER_FILE_EDIT)
        self.assertEqual(res.stdout, "")
        finds = [r for r in self.fake.requests if r["path"].endswith("/find")]
        self.assertEqual(len(finds), 1)
        self.assertTrue(investigation_turn.staged_path(self.home, CONV).exists())
        ctx = self.context(self.run_hook(SHELL))
        self.assertIn(SB2, ctx)
        self.assertFalse(investigation_turn.staged_path(self.home, CONV).exists())

    # --- stop -----------------------------------------------------------------

    def test_stop_never_emits_followup_message_and_stages_instead(self):
        self.setup_live()
        self.start()
        self.put_inbox("Re-check the p99 window before you conclude.")
        res = self.run_hook(STOP)
        self.assertEqual(res.stdout, "")
        self.assertNotIn("followup_message", res.stdout)
        staged = investigation_turn.staged_path(self.home, CONV)
        self.assertTrue(staged.exists())
        self.assertIn("Re-check the p99 window", staged.read_text())
        # Delivered at the next tool boundary, as context.
        ctx = self.context(self.run_hook(SHELL))
        self.assertIn("Re-check the p99 window", ctx)

    def test_stop_prints_nothing_for_any_state(self):
        for setup in (lambda: None, self.connect, self.setup_live):
            setup()
            for payload in (STOP, {**STOP, "loop_count": 3}, {**STOP, "status": "aborted"}):
                res = self.run_hook(payload)
                self.assertEqual(res.stdout, "")
                self.assertNotIn("followup_message", res.stdout)

    # --- relayed owner decisions (owner_relay) ----------------------------------

    def test_a_relay_form_is_blocked_and_the_owner_sees_the_outcome(self):
        from cardinal_core import owner_relay
        self.setup_live(owner_input_on=False)
        self.start()
        entry = owner_relay.propose(self.home, CONV, "inv_" + "e" * 24, "9a8b7c6d-worker", "Only the cache layer.")
        self.fake.routes["relay-owner-decision"] = (201, {"event": {
            "investigation_id": "inv_" + "e" * 24, "seq": 57, "type": "owner.decided"}, "duplicate": False})
        res = self.run_hook({**PROMPT, "prompt": f"approve {entry['id']}"})
        want = (f"Cardinal: sent your approved draft {entry['id']} to the worker (session 9a8b7c6d) as owner "
                'decision #57: "Only the cache layer.".')
        out = json.loads(res.stdout)
        self.assertIs(out["continue"], False)                  # the only channel Cursor shows the owner
        self.assertIn(want, out["user_message"])
        self.assertEqual(set(out), {"continue", "user_message"})
        [req] = [r for r in self.fake.requests if r["path"].endswith("/relay-owner-decision")]
        self.assertEqual(req["body"]["relay"]["supervisor_session_id"], CONV)
        staged = "\n".join(investigation_turn.consume_staged(self.home, CONV))
        self.assertIn("The owner's last prompt went to Cardinal, not to you: " + want, staged)
        self.assertIn(want, owner_relay.recent(self.home, CONV)[-1]["message"])
        self.assertEqual(self.owner_records(), [])
        # decide: the owner's own words to the registered target (the proposal registered it).
        res = self.run_hook({**PROMPT, "prompt": "decide: Stop after the cache fix."})
        self.assertIn("sent your decision to the worker", json.loads(res.stdout)["user_message"])
        reqs = [r for r in self.fake.requests if r["path"].endswith("/relay-owner-decision")]
        self.assertEqual((len(reqs), reqs[-1]["body"]["relay"]["source"]), (2, "verbatim"))
        # A refusal is shown the same way; nothing is sent.
        res = self.run_hook({**PROMPT, "prompt": "decide: should we stop?"})
        self.assertEqual(json.loads(res.stdout)["user_message"], owner_relay.QUESTION)
        # Not the owner's, or not a relay form: never blocked, nothing sent.
        owner_relay.propose(self.home, CONV, "inv_" + "e" * 24, "9a8b7c6d-worker", "Second.")
        for payload in ({**PROMPT, "prompt": "approve", "agent_id": "sub-1"}, {**PROMPT, "prompt": "approve the plan"}):
            res = self.run_hook(payload)
            self.assertNotIn("continue", res.stdout)
        self.assertEqual(len([r for r in self.fake.requests if r["path"].endswith("/relay-owner-decision")]), 2)

    # --- owner input ----------------------------------------------------------

    def test_no_consent_captures_nothing_and_blocks_nothing(self):
        self.setup_live()
        self.start()
        res = self.run_hook(PROMPT)
        self.assertEqual(res.stdout, "")                       # never {continue: false}
        time.sleep(0.5)
        self.assertEqual(self.owner_records(), [])
        self.assertFalse(owner_input.outbox_path(self.home, CONV).exists())

    def test_consented_prompt_is_recorded_each_time_it_is_typed(self):
        self.setup_live()
        self.consent()
        self.start()
        res = self.run_hook(PROMPT)
        self.assertEqual(res.stdout, "")
        records = self.wait_for_records(1)
        self.assertEqual(len(records), 1)
        body = records[0]["body"]
        self.assertEqual(body["session_id"], CONV)
        self.assertEqual(body["investigation_id"], INV)
        self.assertEqual(body["payload"]["text"], "why is checkout slow?")
        self.assertEqual(body["payload"]["source"], "user_prompt")
        self.assertEqual(records[0]["headers"]["x-cardinal-client"], f"cursor/{VERSION}")
        # Cursor fires beforeSubmitPrompt once per prompt: the same prompt typed
        # again, before the turn ended or after, is the owner's (no repeat skip;
        # DEDUPE_REPEATS is Gemini only).
        self.run_hook(PROMPT)
        self.assertEqual(len(self.wait_for_records(2)), 2)
        self.run_hook(STOP)
        self.run_hook(PROMPT)
        self.assertEqual(len(self.wait_for_records(3)), 3)

    def test_kill_switch_wins_over_consent(self):
        self.setup_live()
        self.consent()
        self.start()
        self.run_hook(PROMPT, CARDINAL_OWNER_INPUT="0")
        (self.home / ".cardinal" / "owner-input.json").write_text(json.dumps({"cursor": "off"}))
        self.run_hook(PROMPT)
        time.sleep(0.5)
        self.assertEqual(self.owner_records(), [])

    def test_subagent_prompt_is_not_owner_input(self):
        self.setup_live()
        self.consent()
        self.start()
        self.run_hook({**PROMPT, "agent_id": "sub-1"})
        time.sleep(0.5)
        self.assertEqual(self.owner_records(), [])

    def test_consent_to_an_earlier_text_does_not_count(self):
        self.setup_live()
        path = owner_input.consent_path("cursor", self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"on": True, "text_sha256": "0" * 64, "at": "2026-10-01T00:00:00Z"}))
        self.start()
        self.run_hook(PROMPT)
        time.sleep(0.5)
        self.assertEqual(self.owner_records(), [])

    def test_limits_block_keeps_priority_and_skips_capture(self):
        self.setup_live()
        self.consent()
        self.start()
        from cardinal_core.paths import AgentPaths
        verdict = AgentPaths(home=self.home / ".cursor").verdict_path(CONV)
        verdict.parent.mkdir(parents=True, exist_ok=True)
        verdict.write_text(json.dumps({"decision": "block", "band": 3, "block_reason": "Spend limit reached.",
                                       "fetched_at": time.time()}))
        res = self.run_hook(PROMPT)
        self.assertEqual(json.loads(res.stdout), {"continue": False, "user_message": "Spend limit reached."})
        time.sleep(0.5)
        self.assertEqual(self.owner_records(), [])

    # --- connect --owner-input -------------------------------------------------

    def test_connect_owner_input_prints_the_full_text_and_records_consent(self):
        out = self.consent()
        self.assertIn(owner_input.consent_text("cursor"), out)
        self.assertTrue(owner_input.consent("cursor", self.home))
        path = owner_input.consent_path("cursor", self.home)
        self.assertEqual(path, self.home / ".cursor" / "cardinal" / "owner-input-consent.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        off = self.consent(on=False)
        self.assertIn(owner_input.consent_text("cursor"), off)
        self.assertFalse(owner_input.consent("cursor", self.home))

    def test_connect_owner_input_on_needs_a_typed_yes_in_a_terminal(self):
        connect = load_script(CONNECT, "cursor_connect_oi2")
        out = io.StringIO()
        self.assertEqual(connect.owner_input_consent(True, home=self.home, stdin=io.StringIO("yes\n"), stdout=out), 2)
        self.assertIn(owner_input.consent_text("cursor"), out.getvalue())
        self.assertEqual(connect.owner_input_consent(True, home=self.home, stdin=TTYIn("no\n"),
                                                     stdout=io.StringIO()), 1)
        self.assertFalse(owner_input.consent("cursor", self.home))

    def test_connect_owner_input_cli_runs_without_a_connection(self):
        res = subprocess.run([sys.executable, str(CONNECT), "--owner-input", "off"], capture_output=True, text=True,
                             env=self.env(), timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(owner_input.consent_text("cursor"), res.stdout)
        self.assertNotIn("device", res.stdout.lower())
        piped = subprocess.run([sys.executable, str(CONNECT), "--owner-input", "on"], input="yes\n",
                               capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(piped.returncode, 2)
        self.assertFalse(owner_input.consent("cursor", self.home))

    def test_status_shows_consent_and_disconnect_clears_it(self):
        self.connect()
        (self.home / ".cursor" / "cardinal.json").write_text(json.dumps({"user_email": "dev@example.com"}))

        def status() -> str:
            res = subprocess.run([sys.executable, str(STATUS)], capture_output=True, text=True, env=self.env(),
                                 timeout=30)
            return next(line for line in res.stdout.splitlines() if line.startswith("Owner input:"))

        self.assertIn("off (cardinal-connect --owner-input on", status())
        self.consent()
        self.assertIn("on (you consented", status())
        killed = subprocess.run([sys.executable, str(STATUS)], capture_output=True, text=True,
                                env=self.env(CARDINAL_OWNER_INPUT="0"), timeout=30).stdout
        self.assertIn("Owner input:    off (kill switch", killed)
        res = subprocess.run([sys.executable, str(DISCONNECT)], capture_output=True, text=True, env=self.env(),
                             timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Cleared the owner-input consent", res.stdout)
        self.assertFalse(owner_input.consent("cursor", self.home))

    # --- hook registration ----------------------------------------------------

    def test_connect_registers_the_new_hooks(self):
        connect = load_script(CONNECT, "cursor_connect_hooks")
        hook = load_script(HOOK, "cursor_hook_consts")
        path = self.home / ".cursor" / "hooks.json"
        connect.write_hooks_config(path)
        connect.write_hooks_config(path)  # idempotent
        hooks = json.loads(path.read_text())["hooks"]
        for event in ("sessionStart", "beforeSubmitPrompt", "postToolUse", "postToolUseFailure", "afterFileEdit",
                      "afterMCPExecution", "stop"):
            self.assertIn(event, hooks)
        self.assertNotIn("beforeMCPExecution", hooks)
        post = hooks["postToolUse"]
        self.assertEqual(len(post), 2)
        main = next(e for e in post if "--role" not in e["command"])
        authoring = next(e for e in post if "--role authoring" in e["command"])
        self.assertNotIn("matcher", main)
        self.assertEqual(authoring["matcher"], hook.AUTHORING_MATCHER)
        self.assertEqual(authoring["matcher"], connect.AUTHORING_MATCHER)
        self.assertEqual(authoring["timeout"], hook_core_preview_timeout())
        self.assertIn(hook.AUTHORING_NEEDLE, authoring["command"])
        self.assertEqual(hooks["sessionStart"][0]["timeout"], 10)
        self.assertEqual(hooks["stop"][0]["timeout"], 10)
        import re
        for name in ("MCP:storyboard__preview", "MCP:storyboard__publish", "mcp__cardinal__storyboard__preview"):
            self.assertTrue(re.fullmatch(authoring["matcher"], name), name)
            self.assertTrue(re.search(authoring["matcher"], name), name)
        for name in ("Shell", "MCP:storyboard__create", "Write"):
            self.assertIsNone(re.search(authoring["matcher"], name), name)

    def test_disconnect_removes_every_managed_entry(self):
        connect = load_script(CONNECT, "cursor_connect_rm")
        disconnect = load_script(DISCONNECT, "cursor_disconnect_rm")
        path = self.home / ".cursor" / "hooks.json"
        connect.write_hooks_config(path)
        data, changed = disconnect.strip_managed_hooks(json.loads(path.read_text()))
        self.assertTrue(changed)
        self.assertEqual(data.get("hooks"), {})


def hook_core_preview_timeout() -> int:
    from cardinal_core import storyboard_authoring
    return storyboard_authoring.PREVIEW_HOOK_TIMEOUT_S


FAKE_CHROME = """#!{python}
import sys
if "--version" in sys.argv:
    print("Google Chrome 150.0.7000.1")
    sys.exit(0)
sys.exit(1)
"""


class CursorAuthoringTests(unittest.TestCase):
    """The `--role authoring` postToolUse entry over Cardinal's
    storyboard__preview / __publish, and afterMCPExecution's server record
    (Cursor's postToolUse names no MCP server)."""

    setUp = fixtures.CursorStoryboardTests.setUp
    tearDown = fixtures.CursorStoryboardTests.tearDown
    connect = fixtures.CursorStoryboardTests.connect
    env = CursorInvestigationTests.env
    run_hook = CursorInvestigationTests.run_hook
    context = CursorInvestigationTests.context

    def chrome_env(self) -> dict:
        chrome = self.home / "fake-chrome"
        chrome.write_text(FAKE_CHROME.format(python=sys.executable))
        chrome.chmod(0o755)
        return {"CARDINAL_CHROMIUM": str(chrome), "CARDINAL_STORYBOARD_PREVIEW_BUDGET_S": "20"}

    def post(self, name: str, output: str, **extra) -> dict:
        return {"conversation_id": CONV, "generation_id": "g-3", "hook_event_name": "postToolUse",
                "workspace_roots": ["<cwd>"], "tool_name": name, "tool_input": json.dumps({"storyboard_id": SB1}),
                "tool_output": output, "tool_use_id": "call_preview", "duration": 900, **extra}

    def after_mcp(self, name: str, server: str = "cardinal") -> dict:
        return {"conversation_id": CONV, "generation_id": "g-3", "hook_event_name": "afterMCPExecution",
                "workspace_roots": ["<cwd>"], "tool_name": name, "tool_input": json.dumps({"storyboard_id": SB1}),
                "mcp_server_name": server, "mcp_server_url": "https://app.cardinalhq.io/mcp",
                "result_json": preview_output(), "duration": 900}

    def test_preview_named_mcp__cardinal__renders(self):
        self.connect()
        ctx = self.context(self.run_hook(self.post("mcp__cardinal__storyboard__preview", preview_output()),
                                         "--role", "authoring", **self.chrome_env()))
        # The renderer ran on the result (this preview names no bundle, so it says so).
        self.assertIn("Cardinal storyboard preview", ctx)

    def test_preview_named_MCP_colon_renders_after_afterMCPExecution_records_cardinal(self):
        self.connect()
        self.assertEqual(self.run_hook(self.after_mcp("storyboard__preview")).stdout, "")
        ctx = self.context(self.run_hook(self.post("MCP:storyboard__preview", preview_output()),
                                         "--role", "authoring", **self.chrome_env()))
        self.assertIn("Cardinal storyboard preview", ctx)

    def test_another_servers_tool_of_the_same_name_is_not_cardinals(self):
        self.connect()
        self.run_hook(self.after_mcp("storyboard__preview", server="lookalike"))
        started = time.monotonic()
        res = self.run_hook(self.post("MCP:storyboard__preview", preview_output()), "--role", "authoring",
                            **self.chrome_env())
        self.assertEqual(res.stdout, "")
        self.assertLess(time.monotonic() - started, 15)
        res = self.run_hook(self.post("mcp__lookalike__storyboard__preview", preview_output()), "--role",
                            "authoring", **self.chrome_env())
        self.assertEqual(res.stdout, "")

    def test_authoring_entry_ignores_other_tools(self):
        self.connect()
        self.assertEqual(self.run_hook(SHELL, "--role", "authoring").stdout, "")

    def test_main_entry_stays_silent_for_the_authoring_entrys_tools(self):
        self.connect()
        load_script(CONNECT, "cursor_connect_auth").write_hooks_config(self.home / ".cursor" / "hooks.json")
        investigation_turn.stage(self.home, CONV, "WAITS-FOR-A-TOOL-ONLY-ONE-ENTRY-ANSWERS")
        main = self.run_hook(self.post("mcp__cardinal__storyboard__publish", json.dumps({"published": False})))
        self.assertEqual(main.stdout, "")
        self.assertTrue(investigation_turn.staged_path(self.home, CONV).exists())
        # Without the authoring entry registered, the main entry answers as before.
        (self.home / ".cursor" / "hooks.json").unlink()
        main = self.run_hook(self.post("mcp__cardinal__storyboard__publish", json.dumps({"published": False})))
        self.assertIn("WAITS-FOR-A-TOOL-ONLY-ONE-ENTRY-ANSWERS", self.context(main))

    def test_create_stores_the_evidence_token_for_either_name_shape(self):
        self.connect()
        result = {"storyboard_id": SB1, "revision": 1, "evidence_token": "eyJhbGciOiJIUzI1NiJ9.eyJzYiI6MX0.c2ln",
                  "evidence_upload": {"path": f"/api/orgs/org-1/storyboards/{SB1}/evidence"}}
        out = json.dumps({"content": [{"type": "text", "text": json.dumps(result)}]})
        stored = self.home / ".cardinal" / "evidence" / CONV / "token.json"
        self.run_hook(self.post("mcp__cardinal__storyboard__create", out))
        self.assertTrue(stored.exists())
        stored.unlink()
        # MCP:<tool> with no afterMCPExecution record: not known to be Cardinal's.
        self.run_hook(self.post("MCP:storyboard__create", out))
        self.assertFalse(stored.exists())
        self.run_hook(self.after_mcp("storyboard__create"))
        self.run_hook(self.post("MCP:storyboard__create", out))
        self.assertTrue(stored.exists())


if __name__ == "__main__":
    unittest.main()

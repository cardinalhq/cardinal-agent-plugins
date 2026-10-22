"""Investigation sessions, storyboards, owner input and storyboard authoring
in the Copilot adapter:

  - sessionStart: investigation_agent.start + the session guidance (labeled
    links), what an earlier turn staged, the session id line + discovery
    (X-Cardinal-Client: copilot/<plugin version>), as additionalContext.
  - postToolUse / postToolUseFailure: investigation_agent.deliver (staged
    items, the inbox), the plugin's own staged items; a subagent's tool call
    gets nothing. create / edit / apply_patch record `edited_paths` for
    `context.paths` and run the edit lookup inline.
  - subagentStart: the discovery block the session's last look stored, as
    the subagent's additionalContext (no git, no network).
  - preToolUse: context stamping on Cardinal's storyboard__create /
    add_act / publish / find (`modifiedArgs`); a spend-limit deny wins.
  - agentStop: investigation_agent.turn_end. It STAGES and prints nothing
    (R1: Copilot gives no guarantee a block reason re-enters as hook text).
  - userPromptSubmitted: owner input only with the user's connect-time
    consent, detached; Copilot drops its output, so it never prints. (No
    relayed owner decisions: Copilot cannot block a prompt.)
  - the `--role authoring` postToolUse entry: Cardinal's
    storyboard__preview / __publish, named `cardinal-storyboard__preview`.

No live Copilot CLI payload was captured: payloads follow Copilot's hooks
reference and are UNVERIFIED on a live Copilot CLI.

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_investigation.py -v
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import unittest

from fixtures import (ADAPTER, CONNECT, HOOK, SID, STORYBOARD_CLI, VERSION, FakeMaestro, HookCase,
                      load_script)

sys.path.insert(0, str(ADAPTER / "hooks"))
from cardinal_core import investigation_turn, owner_input  # noqa: E402

INV = "inv_" + "a" * 24
SB = "sb_" + "b" * 24
SB1 = "sb_0123456789abcdef01234567"
SB2 = "sb_" + "c" * 24


class TTYIn(io.StringIO):
    def isatty(self) -> bool:
        return True


class CopilotSessionCase(HookCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeMaestro().start()
        self.addCleanup(self.fake.stop)
        self.fake.routes["find"] = (200, {"matches": [{
            "storyboard_id": SB1, "question": "Why did checkout p99 regress?", "status": "published",
            "act_count": 1, "match": "branch", "match_role": "written_from", "match_act": 1,
            "context": {"repo": "acme/widgets", "branch": "feat/copilot-thing"},
            "view_url": f"https://app.cardinalhq.io/storyboards/{SB1}"}], "rule": "r", "associations_api": 1})
        self.fake.routes["get"] = (200, {"storyboard_id": SB1, "scenes": []})

    def connect_mcp(self):
        (self.copilot / "cardinal.json").write_text(json.dumps({
            "mcp_url": f"{self.fake.origin}/api/orgs/org-1/mcp", "user_email": "dev@example.com"}))
        (self.copilot / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "ck_copilot_test"}))

    def setup_live(self, owner_input_on: bool = True, author: bool = True):
        self.connect_mcp()
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
        return self.context(self.run_hook("sessionStart", self.payload(source="startup", initialPrompt=""), **env))

    def shell(self, **over) -> dict:
        return self.tool(args={"command": "git status --short"}, text="", toolCallId="call_status", **over)

    def failure(self) -> dict:
        return self.payload(toolName="bash", toolArgs={"command": "make test"}, error="exit status 2")

    @property
    def sessions(self):
        return self.home / ".cardinal" / "investigations" / "sessions"

    def binding(self) -> dict:
        return json.loads((self.sessions / f"{SID}.json").read_text())

    def put_inbox(self, text: str = "Inspect accounting headers and pod placement.", seq: int = 12):
        cue = {"investigation_id": INV, "seq": seq, "type": "cue.added", "class": "control",
               "authority": "advisory", "created_at": "2026-10-12T00:47:48Z",
               "producer": {"principal": {"kind": "user", "id": "supervisor"},
                            "session_id": "supervisor-session", "is_investigation_author": True},
               "payload": {"text": text}}
        inbox = self.sessions / f"{SID}.inbox.json"
        inbox.write_text(json.dumps({"investigation_id": INV, "after": self.binding()["cursor"],
                                     "through": seq, "events": [cue]}))
        return inbox

    def owner_records(self) -> list:
        return self.fake.paths("/record-owner-input")

    def wait_for_records(self, n: int, timeout: float = 10.0) -> list:
        give_up = time.monotonic() + timeout
        while time.monotonic() < give_up:
            if len(self.owner_records()) >= n:
                break
            time.sleep(0.1)
        return self.owner_records()

    def consent(self, on: bool = True) -> None:
        connect = load_script(CONNECT, f"copilot_connect_inv_{id(self)}")
        out = io.StringIO()
        self.assertEqual(connect.owner_input_consent(on, home=self.home, stdin=TTYIn("yes\n"), stdout=out), 0,
                         out.getvalue())


class StoryboardTests(CopilotSessionCase):
    def test_session_start_names_the_session_and_discovers(self):
        self.connect_mcp()
        ctx = self.start()
        self.assertIn(f"Cardinal session id for this session: {SID}.", ctx)
        self.assertIn(f'`python3 "{STORYBOARD_CLI}" context --bare --session-id {SID}`', ctx)
        self.assertIn(f"{SB1} · written from branch feat/copilot-thing (subject not confirmed)", ctx)
        self.assertIn("Cardinal-instrumented Copilot CLI session", ctx)
        self.assertNotIn("Cardinal is not connected", ctx)
        finds = self.fake.paths("/find")
        self.assertEqual(finds[0]["headers"]["x-cardinal-client"], f"copilot/{VERSION}")

    def test_edit_tools_record_paths_for_cli_context(self):
        self.connect_mcp()
        (self.repo / "hello.txt").write_text("hello\n")
        (self.repo / "svc").mkdir()
        self.run_hook("postToolUse", self.tool(name="edit", args={"path": str(self.repo / "hello.txt"),
                                                                  "old_str": "hi", "new_str": "hello"}))
        patch = "*** Begin Patch\n*** Add File: svc/new.go\n+package svc\n*** End Patch\n"
        self.run_hook("postToolUse", self.tool(name="apply_patch", args=patch))
        # A failed edit wrote nothing: not recorded.
        self.run_hook("postToolUse", self.tool(name="create", args={"path": "svc/failed.go"}, result_type="failure"))
        res = subprocess.run([sys.executable, str(STORYBOARD_CLI), "context", "--cwd", str(self.repo),
                              "--session-id", SID], capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        ctx = json.loads(res.stdout)["context"]
        self.assertEqual((ctx["repo"], ctx["branch"]), ("acme/widgets", "feat/copilot-thing"))
        self.assertEqual(sorted(ctx["paths"]), ["hello.txt", "svc/new.go"])
        self.assertEqual(ctx["client"], f"copilot/{VERSION}")

    def test_edit_lookup_surfaces_at_the_same_tool_boundary(self):
        self.connect_mcp()
        (self.repo / "svc").mkdir()
        (self.repo / "svc" / "handler.go").write_text("package svc\n")
        self.start()  # caches the server's associations capability
        self.fake.requests.clear()
        self.fake.routes["find"] = (200, {"matches": [{
            "storyboard_id": SB2, "question": "Why does the handler retry twice?", "status": "published",
            "act_count": 1, "match": "path", "match_role": "about",
            "matched": {"kind": "path", "value": "svc/handler.go", "repo": "acme/widgets"},
            "context": {"repo": "acme/widgets"}}], "rule": "r", "associations_api": 1})
        ctx = self.context(self.run_hook("postToolUse", self.tool(
            name="edit", args={"path": "svc/handler.go", "old_str": "a", "new_str": "b"})))
        self.assertIn(SB2, ctx)
        self.assertEqual(len(self.fake.paths("/find")), 1)


class InvestigationTests(CopilotSessionCase):
    def test_session_start_bootstraps_and_gives_labeled_guidance(self):
        self.setup_live()
        ctx = self.start()
        self.assertIn(f"Storyboard (private draft until published): {self.fake.origin}/storyboards/{SB}", ctx)
        self.assertIn("Investigation timeline (shows the live record before any publish)", ctx)
        self.assertIn(str(STORYBOARD_CLI), ctx)
        [ensure] = self.fake.paths("/ensure-session-investigation")
        self.assertEqual(ensure["body"]["session_id"], SID)
        self.assertEqual(ensure["headers"]["x-cardinal-client"], f"copilot/{VERSION}")
        self.assertNotIn("prompt", ensure["body"])
        self.assertNotIn("recorded to Investigation", ctx)
        self.assertEqual(self.binding()["investigation_id"], INV)

    def test_session_start_says_prompts_are_recorded_only_with_consent(self):
        self.setup_live()
        self.consent()
        self.assertIn("recorded to Investigation", self.start())

    def test_session_start_optout_does_not_bootstrap(self):
        self.setup_live()
        self.start(CARDINAL_STORYBOARD_SESSION_START="0")
        self.assertEqual(self.fake.paths("/ensure-session-investigation"), [])

    def test_session_start_delivers_what_was_staged(self):
        self.setup_live()
        self.start()
        investigation_turn.stage(self.home, SID, "STAGED-AT-LAST-TURN-END")
        self.assertIn("STAGED-AT-LAST-TURN-END", self.start())
        self.assertFalse(investigation_turn.staged_path(self.home, SID).exists())

    def test_stray_key_warning_once_per_session(self):
        (self.copilot / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "ck_stray"}))
        first = self.start()
        self.assertIn("Tell the user, briefly: Cardinal: a Cardinal MCP API key is set", first)
        self.assertNotIn("Tell the user, briefly: Cardinal: a Cardinal MCP API key", self.start())

    # --- tool boundaries ------------------------------------------------------

    def test_post_tool_use_delivers_the_inbox(self):
        self.setup_live()
        self.start()
        inbox = self.put_inbox()
        ctx = self.context(self.run_hook("postToolUse", self.shell()))
        self.assertIn("authority: ADVISORY", ctx)
        self.assertIn("Inspect accounting headers", ctx)
        self.assertEqual(self.binding()["cursor"], 12)
        self.assertFalse(inbox.exists())

    def test_post_tool_use_failure_delivers_staged_items(self):
        self.setup_live()
        self.start()
        investigation_turn.stage(self.home, SID, "STAGED-FOR-NEXT-TOOL")
        ctx = self.context(self.run_hook("postToolUseFailure", self.failure()))
        self.assertIn("STAGED-FOR-NEXT-TOOL", ctx)
        self.assertIn("[evidence:ev_", ctx)

    def test_subagent_tool_call_gets_nothing_and_consumes_nothing(self):
        self.setup_live()
        self.start()
        inbox = self.put_inbox()
        investigation_turn.stage(self.home, SID, "STAGED-FOR-MAIN-THREAD")
        ctx = self.context(self.run_hook("postToolUse", self.shell(agentId="sub-1")))
        self.assertNotIn("STAGED-FOR-MAIN-THREAD", ctx)
        self.assertNotIn("Inspect accounting headers", ctx)
        self.assertTrue(inbox.exists())
        main = self.context(self.run_hook("postToolUse", self.shell()))
        self.assertIn("STAGED-FOR-MAIN-THREAD", main)
        self.assertIn("Inspect accounting headers", main)

    def test_unbound_session_still_gets_the_plugins_staged_items(self):
        self.connect_mcp()
        investigation_turn.stage(self.home, SID, "EDIT-LOOKUP-BLOCK")
        self.assertIn("EDIT-LOOKUP-BLOCK", self.context(self.run_hook("postToolUse", self.shell())))

    def test_unconnected_tool_boundary_delivers_nothing_staged(self):
        investigation_turn.stage(self.home, SID, "NOT-WHILE-UNCONNECTED")
        ctx = self.context(self.run_hook("postToolUse", self.shell()))
        self.assertNotIn("NOT-WHILE-UNCONNECTED", ctx)
        self.assertTrue(investigation_turn.staged_path(self.home, SID).exists())

    # --- agentStop ------------------------------------------------------------

    def test_agent_stop_stages_and_prints_nothing(self):
        self.setup_live()
        self.start()
        self.put_inbox("Re-check the p99 window before you conclude.")
        res = self.run_hook("agentStop", self.payload(stopReason="end_turn"))
        self.assertEqual(res.stdout, "")
        staged = investigation_turn.staged_path(self.home, SID)
        self.assertTrue(staged.exists())
        self.assertIn("Re-check the p99 window", staged.read_text())
        self.assertIn("Re-check the p99 window", self.context(self.run_hook("postToolUse", self.shell())))

    def test_agent_stop_prints_nothing_for_any_state(self):
        for setup in (lambda: None, self.connect_mcp, self.setup_live):
            setup()
            for payload in (self.payload(stopReason="end_turn"), self.payload(stopHookActive=True),
                            self.payload(stopReason="aborted")):
                self.assertEqual(self.run_hook("agentStop", payload).stdout, "")

    # --- owner input ----------------------------------------------------------

    def prompt(self, **over) -> dict:
        return self.payload(**{"prompt": "why is checkout slow?", **over})

    def test_no_consent_captures_nothing(self):
        self.setup_live()
        self.start()
        self.assertEqual(self.run_hook("userPromptSubmitted", self.prompt()).stdout, "")
        time.sleep(0.5)
        self.assertEqual(self.owner_records(), [])
        self.assertFalse(owner_input.outbox_path(self.home, SID).exists())

    def test_consented_prompt_is_recorded(self):
        self.setup_live()
        self.consent()
        self.start()
        self.assertEqual(self.run_hook("userPromptSubmitted", self.prompt()).stdout, "")
        records = self.wait_for_records(1)
        self.assertEqual(len(records), 1)
        body = records[0]["body"]
        self.assertEqual(body["session_id"], SID)
        self.assertEqual(body["investigation_id"], INV)
        self.assertEqual(body["payload"]["text"], "why is checkout slow?")
        self.assertEqual(body["payload"]["source"], "user_prompt")
        self.assertEqual(records[0]["headers"]["x-cardinal-client"], f"copilot/{VERSION}")

    def test_kill_switch_wins_over_consent(self):
        self.setup_live()
        self.consent()
        self.start()
        self.run_hook("userPromptSubmitted", self.prompt(), CARDINAL_OWNER_INPUT="0")
        (self.home / ".cardinal" / "owner-input.json").write_text(json.dumps({"copilot": "off"}))
        self.run_hook("userPromptSubmitted", self.prompt())
        time.sleep(0.5)
        self.assertEqual(self.owner_records(), [])

    def test_subagent_prompt_is_not_owner_input(self):
        self.setup_live()
        self.consent()
        self.start()
        self.run_hook("userPromptSubmitted", self.prompt(agentId="sub-1"))
        time.sleep(0.5)
        self.assertEqual(self.owner_records(), [])

    def test_relay_forms_are_ordinary_prompts(self):
        # Copilot cannot block a prompt, so owner_relay is not wired: an
        # `approve` prompt is neither blocked nor relayed.
        self.setup_live(owner_input_on=False)
        self.start()
        res = self.run_hook("userPromptSubmitted", self.prompt(prompt="approve d1"))
        self.assertEqual(res.stdout, "")
        self.assertEqual(self.fake.paths("/relay-owner-decision"), [])


class SubagentStartTests(CopilotSessionCase):
    def subagent_start(self, **over) -> dict:
        return self.payload(**{"agentName": "explore", "agentDisplayName": "Explore", **over})

    def test_hands_the_stored_discovery_block_to_the_subagent(self):
        self.connect_mcp()
        self.start()  # the session's look stores the block
        self.fake.requests.clear()
        ctx = self.context(self.run_hook("subagentStart", self.subagent_start()))
        self.assertIn(f"{SB1} · written from branch feat/copilot-thing (subject not confirmed)", ctx)
        self.assertEqual(self.fake.requests, [], "no network at subagentStart")

    def test_nothing_before_any_look(self):
        self.connect_mcp()
        self.assertEqual(self.run_hook("subagentStart", self.subagent_start()).stdout, "")
        self.assertEqual(self.fake.requests, [])

    def test_silent_unconnected_without_a_session_or_with_discovery_off(self):
        self.assertEqual(self.run_hook("subagentStart", self.subagent_start()).stdout, "")
        self.connect_mcp()
        self.start()
        payload = self.subagent_start()
        payload.pop("sessionId")
        self.assertEqual(self.run_hook("subagentStart", payload).stdout, "")
        self.assertEqual(self.run_hook("subagentStart", self.subagent_start(),
                                       CARDINAL_STORYBOARD_DISCOVERY="0").stdout, "")


class ContextStampingTests(CopilotSessionCase):
    """preToolUse on Cardinal's storyboard__create / add_act / publish / find:
    `modifiedArgs` with an absent session_id / context filled; never
    `permissionDecision: allow`."""

    def pre(self, name: str, args, **env) -> subprocess.CompletedProcess:
        return self.run_hook("preToolUse", self.payload(toolName=name, toolArgs=args), **env)

    def modified(self, res: subprocess.CompletedProcess) -> dict:
        out = json.loads(res.stdout)
        self.assertEqual(set(out), {"modifiedArgs"}, out)
        return out["modifiedArgs"]

    def test_create_gets_the_session_and_context(self):
        self.connect_mcp()
        args = self.modified(self.pre("cardinal-storyboard__create", {"question": "Why is p99 up?"}))
        self.assertEqual(args["question"], "Why is p99 up?")
        self.assertEqual(args["session_id"], SID)
        self.assertEqual(args["context"]["repo"], "acme/widgets")
        self.assertEqual(args["context"]["branch"], "feat/copilot-thing")
        self.assertEqual(args["context"]["client"], f"copilot/{VERSION}")

    def test_tool_args_as_a_json_string(self):
        self.connect_mcp()
        args = self.modified(self.pre("cardinal-storyboard__add_act",
                                      json.dumps({"storyboard_id": SB1, "question": "Next?"})))
        self.assertEqual((args["storyboard_id"], args["session_id"]), (SB1, SID))
        self.assertIn("context", args)

    def test_given_values_are_kept(self):
        self.connect_mcp()
        given = {"question": "q", "session_id": "the-models-own", "context": {"repo": "x/y"}}
        self.assertEqual(self.pre("cardinal-storyboard__create", given).stdout, "")
        args = self.modified(self.pre("cardinal-storyboard__find", {"session_id": "", "context": {}}))
        self.assertEqual(args["session_id"], SID)
        self.assertEqual(args["context"]["branch"], "feat/copilot-thing")

    def test_publish_waits_for_the_server_capability(self):
        self.connect_mcp()
        self.assertEqual(self.pre("cardinal-storyboard__publish", {"storyboard_id": SB1}).stdout, "")
        self.start()  # discovery caches associations_api: 1
        args = self.modified(self.pre("cardinal-storyboard__publish", {"storyboard_id": SB1}))
        self.assertNotIn("session_id", args)
        self.assertEqual(args["context"]["repo"], "acme/widgets")

    def test_other_tools_get_nothing(self):
        self.connect_mcp()
        (self.copilot / "mcp-config.json").write_text(json.dumps({"mcpServers": {"lookalike": {"command": "x"}}}))
        for name, args in (("cardinal-storyboard__link", {"storyboard_id": SB1}),
                           ("cardinal-storyboard__preview", {"storyboard_id": SB1}),
                           ("lookalike-storyboard__create", {"question": "q"}),
                           ("bash", {"command": "ls"}), ("edit", {"path": "README.md"})):
            with self.subTest(name=name):
                res = self.pre(name, args)
                self.assertEqual(res.stdout, "")

    def test_never_answers_allow(self):
        self.connect_mcp()
        res = self.pre("cardinal-storyboard__create", {"question": "q"})
        self.assertNotIn("permissionDecision", res.stdout)

    def test_opt_out_and_unconnected(self):
        self.connect_mcp()
        self.assertEqual(self.pre("cardinal-storyboard__create", {"question": "q"},
                                  CARDINAL_STORYBOARD_CONTEXT="0").stdout, "")
        (self.copilot / "cardinal.json").unlink()
        self.assertEqual(self.pre("cardinal-storyboard__create", {"question": "q"}).stdout, "")

    def test_non_object_args_get_nothing(self):
        self.connect_mcp()
        for args in ("not json", ["a"], None):
            self.assertEqual(self.pre("cardinal-storyboard__create", args).stdout, "")

    def test_a_spend_limit_block_wins(self):
        self.connect_mcp()
        verdict = self.copilot / "cardinal" / "limits" / f"{SID}.verdict.json"
        verdict.parent.mkdir(parents=True)
        verdict.write_text(json.dumps({"decision": "block", "band": 3, "block_reason": "Spend limit reached.",
                                       "fetched_at": time.time()}))
        out = json.loads(self.pre("cardinal-storyboard__create", {"question": "q"}).stdout)
        self.assertEqual(out, {"permissionDecision": "deny", "permissionDecisionReason": "Spend limit reached."})


FAKE_CHROME = """#!{python}
import sys
if "--version" in sys.argv:
    print("Google Chrome 150.0.7000.1")
    sys.exit(0)
sys.exit(1)
"""


def preview_text(storyboard_id: str = SB1) -> str:
    return json.dumps({"storyboard_id": storyboard_id, "revision": 3, "scenes": [{"id": "s1"}]})


class AuthoringTests(CopilotSessionCase):
    def chrome_env(self) -> dict:
        chrome = self.home / "fake-chrome"
        chrome.write_text(FAKE_CHROME.format(python=sys.executable))
        chrome.chmod(0o755)
        return {"CARDINAL_CHROMIUM": str(chrome), "CARDINAL_STORYBOARD_PREVIEW_BUDGET_S": "20"}

    def post(self, name: str, text: str, **over) -> dict:
        return self.tool(name=name, args=json.dumps({"storyboard_id": SB1}), text=text, toolCallId="call_preview",
                         **over)

    def test_preview_renders_in_the_authoring_entry(self):
        self.connect_mcp()
        ctx = self.context(self.run_hook("postToolUse", self.post("cardinal-storyboard__preview", preview_text()),
                                         "--role", "authoring", **self.chrome_env()))
        self.assertIn("Cardinal storyboard preview", ctx)

    def test_authoring_entry_is_silent_unconnected(self):
        res = self.run_hook("postToolUse", self.post("cardinal-storyboard__preview", preview_text()),
                            "--role", "authoring", **self.chrome_env())
        self.assertEqual(res.stdout, "")

    def test_another_servers_tool_of_the_same_name_is_not_cardinals(self):
        self.connect_mcp()
        (self.copilot / "mcp-config.json").write_text(json.dumps({"mcpServers": {"lookalike": {"command": "x"}}}))
        res = self.run_hook("postToolUse", self.post("lookalike-storyboard__preview", preview_text()),
                            "--role", "authoring", **self.chrome_env())
        self.assertEqual(res.stdout, "")

    def test_authoring_entry_ignores_other_tools(self):
        self.connect_mcp()
        self.assertEqual(self.run_hook("postToolUse", self.shell(), "--role", "authoring").stdout, "")
        self.assertEqual(self.run_hook("postToolUse", self.post("cardinal-storyboard__find", "{}"),
                                       "--role", "authoring").stdout, "")

    def test_main_entry_stays_silent_for_the_authoring_entrys_tools(self):
        self.connect_mcp()
        self.start()
        investigation_turn.stage(self.home, SID, "WAITS-FOR-THE-AUTHORING-ENTRY")
        main = self.run_hook("postToolUse", self.post("cardinal-storyboard__publish", json.dumps({"published": False})))
        self.assertEqual(main.stdout, "")
        self.assertTrue(investigation_turn.staged_path(self.home, SID).exists())
        # The authoring entry delivers the boundary's items instead.
        authoring = self.run_hook("postToolUse", self.post("cardinal-storyboard__publish",
                                                           json.dumps({"published": False})), "--role", "authoring")
        self.assertIn("WAITS-FOR-THE-AUTHORING-ENTRY", self.context(authoring))
        # Any other tool: the main entry answers.
        investigation_turn.stage(self.home, SID, "MAIN-ENTRY-ANSWERS")
        self.assertIn("MAIN-ENTRY-ANSWERS", self.context(self.run_hook("postToolUse", self.shell())))

    def test_create_stores_the_evidence_token(self):
        self.connect_mcp()
        result = {"storyboard_id": SB1, "revision": 1, "evidence_token": "eyJhbGciOiJIUzI1NiJ9.eyJzYiI6MX0.c2ln",
                  "evidence_upload": {"path": f"/api/orgs/org-1/storyboards/{SB1}/evidence"}}
        stored = self.home / ".cardinal" / "evidence" / SID / "token.json"
        self.run_hook("postToolUse", self.post("lookalike-storyboard__create", json.dumps(result)))
        self.assertFalse(stored.exists())
        self.run_hook("postToolUse", self.post("cardinal-storyboard__create", json.dumps(result)))
        self.assertTrue(stored.exists())


if __name__ == "__main__":
    unittest.main()

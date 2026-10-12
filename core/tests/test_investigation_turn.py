"""cardinal_core.investigation_turn (what a boundary delivers; R1: Gemini and
Cursor never block at the turn's end, they stage), the investigation_agent
wrappers, and the per-runtime owner-input contract of owner_input.capture
(R2-R4: a subagent's prompt is refused, Cursor needs consent, Gemini's
prompt is normalized, a re-fired repeat is skipped, the kill switch)."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cardinal_core import investigation_agent as agent
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_poller as poller
from cardinal_core import investigation_turn as turn
from cardinal_core import owner_input as oi

from tests.test_owner_input import Base as OwnerInputBase

INV = "inv_" + "a" * 24
SID = "11111111-2222-3333-4444-555555555555"
CONN = {"origin": "https://test.invalid", "org": "org", "key": "test-key"}
SUPERVISOR = {"principal": {"kind": "grantee", "id": "grt_" + "d" * 24}, "session_id": None,
              "is_investigation_author": False, "granted_by": "user:owner",
              "_resolved": {"label": "reviewer", "granted_by_name": "Pat"}}


def challenge(seq, text="Check the negative case."):
    return {"investigation_id": INV, "seq": seq, "type": "challenge.added", "class": "control",
            "authority": "advisory", "payload": {"text": text}, "producer": dict(SUPERVISOR),
            "to_session_id": None, "created_at": "2026-10-12T12:00:00Z"}


class TurnBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        ie.write_binding(self.home, SID, {"investigation_id": INV, "cursor": 0, "org": "org", "is_author": True,
                                          "storyboard_id": "sb_" + "b" * 24, "bootstrap": {"status": "ok"},
                                          "capabilities": {}})

    def arrive(self, *events):
        """The poller leaves `events` in the inbox."""
        last = max(e["seq"] for e in events)
        with patch.object(ie, "read_all", return_value=(list(events), last, last)):
            self.assertEqual(ie.poll_once(self.home, SID, CONN, "test"), "inbox")

    def turn_end(self, runtime, **kw):
        kw.setdefault("stop_hook_active", False)
        kw.setdefault("deadline", time.monotonic() + 2.6)
        return turn.at_turn_end(self.home, SID, CONN, "test", runtime, environ={}, **kw)


class TurnEndBlock(TurnBase):
    def test_who_may_block(self):
        self.assertEqual(turn.TURN_END_BLOCK, {"claude": True, "codex": True, "gemini": False, "cursor": False})
        self.assertFalse(turn.can_block("opencode"))

    def test_claude_and_codex_block_with_the_inbox(self):
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                self.setUp()
                self.arrive(challenge(1))
                d = self.turn_end(runtime)
                self.assertIn("challenge.added", d.turn_end_block)
                self.assertEqual(d.agent_context, [])
                self.assertFalse(turn.staged_path(self.home, SID).exists())

    def test_gemini_and_cursor_never_block_they_stage_for_the_next_boundary(self):
        for runtime in ("gemini", "cursor"):
            with self.subTest(runtime=runtime):
                self.setUp()
                self.arrive(challenge(1))
                d = self.turn_end(runtime)
                self.assertIsNone(d.turn_end_block)
                self.assertEqual((d.agent_context, d.user_message), ([], None))
                self.assertEqual(stat.S_IMODE(turn.staged_path(self.home, SID).stat().st_mode), 0o600)
                got = turn.at_prompt(self.home, SID, runtime).agent_context
                self.assertEqual(len(got), 1)
                self.assertIn("challenge.added", got[0])
                self.assertEqual(turn.at_prompt(self.home, SID, runtime).agent_context, [], "delivered once")

    def test_no_deliverable_of_any_kind_ever_blocks_gemini_or_cursor(self):
        def emitting(text):
            def fn(*a, **k):
                emit = next(x for x in a if callable(x))
                emit(text)
                return True
            return fn
        cases = {
            "check": [patch.object(poller, "fresh", return_value=False),
                      patch.object(ie, "check", side_effect=emitting("from the sync check"))],
            "directive": [patch.object(ie, "supervision_stop", side_effect=emitting("open directive"))],
            "reminder": [patch.object(ie, "supervision_stop", return_value=False),
                         patch.object(ie, "remind_decisions", side_effect=emitting("pending decision"))],
        }
        for runtime in ("gemini", "cursor", "claude"):
            for name, patches in cases.items():
                with self.subTest(runtime=runtime, case=name):
                    self.setUp()
                    if name != "check":
                        patches = patches + [patch.object(poller, "fresh", return_value=True)]
                    for p in patches:
                        p.start()
                    try:
                        d = self.turn_end(runtime)
                    finally:
                        for p in patches:
                            p.stop()
                    if turn.can_block(runtime):
                        self.assertTrue(d.turn_end_block)
                    else:
                        self.assertIsNone(d.turn_end_block)
                        self.assertEqual(len(turn.consume_staged(self.home, SID)), 1)

    def test_the_owner_input_flush_runs_with_the_deadline_whatever_happened(self):
        seen = []
        deadline = time.monotonic() + 2.6
        with patch.object(poller, "fresh", return_value=True):
            self.turn_end("gemini", deadline=deadline, owner_input_flush=seen.append)
        self.assertEqual(seen, [deadline])
        with patch.object(ie, "deliver_inbox", side_effect=RuntimeError("boom")):
            self.turn_end("claude", deadline=deadline, owner_input_flush=seen.append)
        self.assertEqual(seen, [deadline, deadline])

    def test_the_turn_end_ends_the_owner_input_turn(self):
        b = ie.read_binding(self.home, SID)
        b["owner_input_last"] = {"sha": "x", "ended": False}
        ie.write_binding(self.home, SID, b)
        with patch.object(poller, "fresh", return_value=True):
            self.turn_end("gemini")
        self.assertIs(ie.read_binding(self.home, SID)["owner_input_last"]["ended"], True)

    def test_another_orgs_binding_gets_no_supervision_block(self):
        b = ie.read_binding(self.home, SID)
        b["org"] = "other"
        ie.write_binding(self.home, SID, b)
        with patch.object(poller, "fresh", return_value=True), \
                patch.object(ie, "supervision_stop", return_value=True) as stop:
            self.turn_end("claude")
        stop.assert_not_called()


class AtTool(TurnBase):
    def test_staged_first_then_the_inbox(self):
        turn.stage(self.home, SID, "staged one")
        self.arrive(challenge(1))
        got = turn.at_tool(self.home, SID, "org", "gemini", is_subagent=False).agent_context
        self.assertEqual(got[0], "staged one")
        self.assertIn("challenge.added", got[1])
        self.assertFalse(ie.inbox_path(self.home, SID).exists())

    def test_a_subagent_gets_nothing_and_changes_nothing(self):
        turn.stage(self.home, SID, "staged one")
        self.arrive(challenge(1))
        d = turn.at_tool(self.home, SID, "org", "codex", is_subagent=True)
        self.assertEqual(d.agent_context, [])
        self.assertTrue(ie.inbox_path(self.home, SID).exists())
        self.assertTrue(turn.staged_path(self.home, SID).exists())
        self.assertEqual(ie.read_binding(self.home, SID)["cursor"], 0)

    def test_another_org_gets_nothing_none_is_not_checked(self):
        self.arrive(challenge(1))
        self.assertEqual(turn.at_tool(self.home, SID, "other", "codex", is_subagent=False).agent_context, [])
        self.assertEqual(len(turn.at_tool(self.home, SID, None, "claude", is_subagent=False).agent_context), 1)


class Staging(TurnBase):
    def test_bounded_oldest_first_out(self):
        for i in range(turn.MAX_STAGED + 5):
            turn.stage(self.home, SID, f"item {i}")
        got = turn.consume_staged(self.home, SID)
        self.assertEqual(len(got), turn.MAX_STAGED)
        self.assertEqual(got[-1], f"item {turn.MAX_STAGED + 4}")
        self.assertEqual(turn.consume_staged(self.home, SID), [])

    def test_a_huge_item_is_kept_alone(self):
        turn.stage(self.home, SID, "small")
        turn.stage(self.home, SID, "x" * (turn.MAX_STAGED_CHARS + 1))
        self.assertEqual([len(x) for x in turn.consume_staged(self.home, SID)], [turn.MAX_STAGED_CHARS + 1])


class AgentWrappers(TurnBase):
    def wiring(self, runtime):
        return SimpleNamespace(runtime=runtime, paths=SimpleNamespace(home=self.home / f".{runtime}"), environ={},
                               client=f"{runtime}/test", cli="/opt/cardinal/cardinal-storyboard",
                               connection=lambda: dict(CONN), _off=lambda name: False)

    def test_deliver_for_a_subagent_does_nothing(self):
        self.arrive(challenge(1))
        out = []
        agent.deliver(self.wiring("codex"), SID, out.append, is_subagent=True)
        self.assertEqual(out, [])
        self.assertTrue(ie.inbox_path(self.home, SID).exists())
        agent.deliver(self.wiring("codex"), SID, out.append)
        self.assertEqual(len(out), 1)

    def test_gemini_turn_end_stages_and_prompt_delivers_with_the_adapters_command(self):
        w = self.wiring("gemini")
        self.arrive(challenge(1))
        d = agent.turn_end(w, SID, stop_hook_active=False, deadline=time.monotonic() + 2.6)
        self.assertIsNone(d.turn_end_block)
        got = agent.prompt(w, SID).agent_context
        self.assertEqual(len(got), 1)
        self.assertIn(f"Acknowledge: {agent.command(w)} ack", got[0])
        self.assertNotIn("Acknowledge: cardinal-storyboard investigation", got[0])

    def test_codex_turn_end_blocks_with_the_adapters_command(self):
        w = self.wiring("codex")
        self.arrive(challenge(1))
        d = agent.turn_end(w, SID, stop_hook_active=False, deadline=time.monotonic() + 2.6)
        self.assertIn(f"{agent.command(w)} ack", d.turn_end_block)

    def test_nothing_for_another_org(self):
        w = self.wiring("gemini")
        w.connection = lambda: {**CONN, "org": "other"}
        self.arrive(challenge(1))
        self.assertIsNone(agent.turn_end(w, SID, stop_hook_active=False, deadline=time.monotonic() + 1).turn_end_block)
        self.assertEqual(agent.prompt(w, SID).agent_context, [])
        self.assertTrue(ie.inbox_path(self.home, SID).exists())


# ---------------------------------------------------------------------------
# The owner-input contract, per runtime
# ---------------------------------------------------------------------------

OI_SID = "11111111-2222-3333-4444-555555555555"   # tests.test_owner_input's SID


class OwnerInputContract(OwnerInputBase):
    def cap(self, prompt, runtime="gemini", **kw):
        kw.setdefault("environ", {})
        kw.setdefault("deadline", time.monotonic() + oi.BUDGET)
        kw.setdefault("now", self.now)
        return oi.capture(self.home, OI_SID, prompt, runtime=runtime, conn=self.fake.conn, client="t", **kw)

    def texts(self):
        return [b["payload"]["text"] for b in self.fake.records()]

    def assert_nothing(self):
        self.assertEqual(self.fake.requests, [])
        self.assertNotIn("owner_input_turn", self.binding())
        self.assertFalse(self.outbox().exists())

    # R2: a subagent's prompt is never the owner's ------------------------------
    def test_a_non_empty_agent_id_refuses_capture_on_every_runtime(self):
        self.bind()
        for runtime in oi.RUNTIMES:
            self.assertEqual(self.cap("hello", runtime, agent_id="agent-7").status, "subagent", runtime)
        self.assert_nothing()
        self.assertEqual(self.cap("hello", "codex", agent_id="").status, "posted")

    # R3: Cursor captures only with connect-time consent ----------------------
    def test_cursor_capture_requires_consent(self):
        self.bind(owner_input_disclosed=False)
        got = self.cap("hello", "cursor")
        self.assertEqual((got.status, got.disclosure), ("no_consent", None))
        self.assert_nothing()
        oi.set_consent("cursor", self.home, True)
        path = oi.consent_path("cursor", self.home)
        self.assertEqual(path, self.home / ".cursor" / "cardinal" / "owner-input-consent.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        got = self.cap("now with consent", "cursor")
        # Consent stands in for the per-session disclosure Cursor cannot show.
        self.assertEqual((got.status, got.disclosure), ("posted", None))
        self.assertIs(self.binding()["owner_input_disclosed"], True)
        oi.set_consent("cursor", self.home, False)
        self.assertEqual(self.cap("off again", "cursor").status, "no_consent")
        self.assertEqual(self.texts(), ["now with consent"])

    def test_consent_is_versioned_by_its_text(self):
        oi.set_consent("cursor", self.home, True)
        self.assertTrue(oi.consent("cursor", self.home))
        with patch.object(oi, "CONSENT_TEXT", oi.CONSENT_TEXT + " (changed)"):
            self.assertFalse(oi.consent("cursor", self.home))
        self.assertTrue(oi.consent("gemini", self.home), "per-session disclosure instead")
        with self.assertRaises(ValueError):
            oi.set_consent("vim", self.home, True)

    def test_the_others_disclose_on_the_first_prompt_which_is_not_captured(self):
        for runtime in ("claude", "codex", "gemini"):
            with self.subTest(runtime=runtime):
                self.fake.requests.clear()
                self.fake.events.clear()
                self.bind(owner_input_disclosed=False)
                got = self.cap(f"first {runtime}", runtime)
                self.assertEqual(got.status, "disclosed")
                self.assertIn(oi.DISCLOSURE.format(inv=INV), got.disclosure)
                self.assertIn("CARDINAL_OWNER_INPUT=0", got.disclosure)
                self.assertEqual(self.fake.records(), [])
                self.assertEqual(self.cap(f"second {runtime}", runtime).status, "posted")
                self.assertEqual(self.texts(), [f"second {runtime}"])

    # R2: Gemini's prompt reduced to what was typed ----------------------------
    def test_gemini_normalization_strips_hook_context_and_referenced_files(self):
        n = lambda p: oi.normalize_prompt("gemini", p)   # noqa: E731
        self.assertEqual(n("fix the cache\n<hook_context>Cardinal session id …</hook_context>"), "fix the cache")
        self.assertEqual(n("<hook_context>a</hook_context>\nfix it <hook_context>b\nc</hook_context>"), "fix it")
        files = ("explain @src/a.py\n--- Content from referenced files ---\nContent from @src/a.py:\n"
                 "print('secret plan')\n--- End of content ---")
        self.assertEqual(n(files), "explain @src/a.py")
        self.assertEqual(n(files + "\n<hook_context>x</hook_context>"), "explain @src/a.py")
        # gemini-cli appends the section after the typed text: cut to the end
        # even without the end marker (a file read cut short).
        self.assertEqual(n("explain @a.py\n--- Content from referenced files ---\nContent from @a.py:\nno end"),
                         "explain @a.py")

    def test_gemini_referenced_files_holding_the_markers_never_leak(self):
        n = lambda p: oi.normalize_prompt("gemini", p)   # noqa: E731
        start, end = oi.REFERENCE_CONTENT_START, oi.REFERENCE_CONTENT_END
        # A file that itself contains the end marker: what follows it is still the file.
        holds_end = (f"review @notes.md\n{start}\nContent from @notes.md:\nbefore\n{end}\n"
                     f"SECRET after the marker\n{end}")
        self.assertEqual(n(holds_end), "review @notes.md")
        # A file that contains the start marker.
        holds_start = (f"review @notes.md\n{start}\nContent from @notes.md:\nx\n{start}\nSECRET\n{end}")
        self.assertEqual(n(holds_start), "review @notes.md")
        # Several files, one of them holding the end marker.
        many = (f"compare @a.py and @b.py\n{start}\nContent from @a.py:\nA {end} SECRET-A\n"
                f"Content from @b.py:\nSECRET-B\n{end}")
        self.assertEqual(n(many), "compare @a.py and @b.py")
        # The prompt is only the section: nothing typed.
        self.assertIsNone(n(f"{start}\nContent from @a.py:\nA\n{end}"))
        # A stray end marker in what is left fails closed.
        self.assertIsNone(n(f"explain {end} this"))

    def test_gemini_normalization_returns_none_when_unsure(self):
        n = lambda p: oi.normalize_prompt("gemini", p)   # noqa: E731
        for prompt in ("fix it <hook_context>never closed", "fix it </hook_context>",
                       "<hook_context>a <hook_context>b</hook_context> c</hook_context>",
                       "<hook_context>only context</hook_context>", "   ", None, 7,
                       "System: Potential loop detected. Details: x. Please take a step back"):
            self.assertIsNone(n(prompt), prompt)

    def test_gemini_built_in_prompts_that_are_not_typed(self):
        n = lambda p: oi.normalize_prompt("gemini", p)   # noqa: E731
        init = ("\nYou are an AI agent that brings the power of Gemini directly into the terminal. Your task is to "
                "analyze the current directory and generate a comprehensive GEMINI.md file to be used as "
                "instructional context for future interactions.\n\n**Analysis Process:**\n")
        self.assertIsNone(n(init), "/init's template")
        # An MCP server's prompt: JSON.stringify(text).
        self.assertIsNone(n(json.dumps("Summarize the open incidents\nfor team \"core\"")))
        self.assertIsNone(n('  "x"  '))
        self.assertIsNone(n(json.dumps("p") + "\n<hook_context>c</hook_context>"))
        # Not a JSON string: recorded as typed.
        for typed in ('"quoted" and more', '"unterminated', '"a" "b"', '"', "/plan add a cache layer"):
            self.assertEqual(n(typed), typed.strip(), typed)
        # Other runtimes are not touched by Gemini's rules.
        self.assertEqual(oi.normalize_prompt("codex", '"x"'), '"x"')

    def test_other_runtimes_are_not_rewritten(self):
        typed = "keep <hook_context>this</hook_context> as typed"
        for runtime in ("claude", "codex", "cursor"):
            self.assertEqual(oi.normalize_prompt(runtime, typed), typed)
        self.assertIsNone(oi.normalize_prompt("claude", "<task-notification>done</task-notification>"))
        self.assertEqual(oi.normalize_prompt("gemini", "<task-notification>x"), "<task-notification>x")

    def test_capture_sends_the_normalized_text_and_skips_the_unsure(self):
        self.bind()
        self.assertEqual(self.cap("fix it\n<hook_context>ctx</hook_context>").status, "posted")
        self.assertEqual(self.cap("fix <hook_context>unclosed").status, "not_typed")
        self.assertEqual(self.texts(), ["fix it"])

    # R2: a re-fired repeat within a turn ---------------------------------------
    def test_the_same_prompt_again_within_a_turn_is_deduped(self):
        self.bind()
        self.assertEqual(self.cap("run the tests").status, "posted")
        self.assertEqual(self.cap("run the tests").status, "repeat")
        # The same text after a hook-context wrapper is the same scrubbed sha.
        self.assertEqual(self.cap("run the tests\n<hook_context>x</hook_context>").status, "repeat")
        self.assertEqual(self.cap("something else").status, "posted")
        self.assertEqual(self.cap("run the tests").status, "posted", "only the last prompt counts")
        oi.turn_ended(self.home, OI_SID)
        self.assertEqual(self.cap("run the tests").status, "posted", "a new turn: the owner typed it again")
        self.assertEqual([b["payload"]["turn"] for b in self.fake.records()], [1, 2, 3, 4])

    def test_a_repeat_is_skipped_only_within_the_window(self):
        self.bind()
        self.assertEqual(self.cap("run the tests", now=self.now).status, "posted")
        self.assertEqual(self.cap("run the tests", now=self.now + oi.REPEAT_WINDOW - 1).status, "repeat")
        # The window runs from when it was first seen, not from the last skip.
        self.assertEqual(self.cap("run the tests", now=self.now + oi.REPEAT_WINDOW).status, "posted")
        self.assertEqual(self.cap("run the tests", now=self.now + oi.REPEAT_WINDOW + 1).status, "repeat")
        # A clock that went backwards is not within the window.
        self.assertEqual(self.cap("run the tests", now=self.now).status, "posted")

    def test_only_gemini_skips_repeats(self):
        self.assertEqual(oi.DEDUPE_REPEATS, frozenset({"gemini"}))
        for runtime in ("claude", "codex", "cursor"):
            with self.subTest(runtime=runtime):
                self.fake.requests.clear()
                self.fake.events.clear()
                self.bind()
                if runtime == "cursor":
                    oi.set_consent("cursor", self.home, True)
                self.assertEqual([self.cap("yes", runtime).status for _ in range(2)], ["posted", "posted"])

    # R4: the kill switch, env and file ------------------------------------------
    def test_kill_switch_env_and_file(self):
        ks = lambda rt, env=None: oi.kill_switch(rt, self.home, env or {})   # noqa: E731
        self.assertFalse(ks("gemini"))
        for v in ("0", "false", "OFF", "no", 0, False):
            self.assertTrue(ks("gemini", {"CARDINAL_OWNER_INPUT": v}), v)
        self.assertFalse(ks("gemini", {"CARDINAL_OWNER_INPUT": "1"}))
        f = oi.kill_switch_path(self.home)
        self.assertEqual(f, self.home / ".cardinal" / "owner-input.json")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"gemini": "off", "codex": "on"}))
        self.assertEqual((ks("gemini"), ks("codex"), ks("cursor")), (True, False, False))
        f.write_text(json.dumps({"cursor": False}))
        self.assertTrue(ks("cursor"))
        f.write_text("not json")
        self.assertTrue(ks("codex"), "cannot tell: off")
        f.write_text("[]")
        self.assertTrue(ks("codex"))

    def test_claude_settings_count_for_claude_only(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {"CARDINAL_OWNER_INPUT": "0"}}))
        self.assertTrue(oi.kill_switch("claude", self.home, {}))
        self.assertFalse(oi.kill_switch("codex", self.home, {}))
        project = self.home / "repo"
        (project / ".claude").mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text("{}")
        (project / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"CARDINAL_OWNER_INPUT": False}}))
        self.assertTrue(oi.kill_switch("claude", self.home, {}, cwd=str(project)))
        self.assertTrue(oi.kill_switch("claude", self.home, {"CLAUDE_PROJECT_DIR": str(project)}))
        self.assertFalse(oi.kill_switch("claude", self.home, {}))

    def test_the_kill_switch_drops_the_outbox_and_forgets_the_disclosure(self):
        self.bind()
        self.queue(1)
        f = oi.kill_switch_path(self.home)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"gemini": "off"}))
        n = len(self.fake.requests)
        self.assertEqual(self.cap("hello").status, "disabled")
        self.assertFalse(self.outbox().exists())
        self.assertNotIn("owner_input_disclosed", self.binding())
        self.assertEqual(len(self.fake.requests), n)
        f.unlink()
        self.assertEqual(self.cap("told first").status, "disclosed")

    # Detached capture (no network in the hook) --------------------------------
    def test_detached_takes_the_turn_now_and_queues_for_the_detached_post(self):
        self.bind()
        with patch.object(oi, "spawn_flush", return_value=True) as spawn:
            self.assertEqual(self.cap("hello", detached=True).status, "detached")
        spawn.assert_called_once()
        self.assertEqual(self.fake.requests, [], "nothing on the network in the hook")
        self.assertEqual(self.binding()["owner_input_turn"], 1)
        box = oi.read_outbox(self.home, OI_SID)
        self.assertEqual([e["payload"]["turn"] for e in box["entries"]], [1])
        oi._flush_detached(json.dumps({"home": str(self.home), "sid": OI_SID, "conn": self.fake.conn,
                                       "client": "t"}))
        self.assertEqual(self.texts(), ["hello"])
        self.assertFalse(self.outbox().exists())

    def test_the_connection_travels_on_the_pipe_never_in_argv_or_env(self):
        class FakeStdin:
            data = b""
            def write(self, b):
                FakeStdin.data += b
            def close(self):
                pass
        calls = []

        def popen(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(stdin=FakeStdin())
        with patch.object(oi.subprocess, "Popen", side_effect=popen):
            self.assertTrue(oi.spawn_flush(self.home, OI_SID, self.fake.conn, "t"))
        argv, kw = calls[0]
        self.assertEqual(argv[:2], [sys.executable, "-I"])
        self.assertNotIn(self.fake.conn["key"], " ".join(argv))
        self.assertNotIn("env", kw)
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(json.loads(FakeStdin.data)["conn"], self.fake.conn)

    def test_a_real_detached_process_posts_it(self):
        self.bind()
        self.assertEqual(self.cap("posted from afar", detached=True).status, "detached")
        end = time.monotonic() + 15
        while not self.fake.records() and time.monotonic() < end:
            time.sleep(0.1)
        self.assertEqual(self.texts(), ["posted from afar"])

    def test_detached_without_a_process_stays_queued(self):
        self.bind()
        with patch.object(oi, "spawn_flush", return_value=False):
            self.assertEqual(self.cap("hello", detached=True).status, "queued")
        self.assertTrue(self.outbox().exists())

    # The turn-end flush -------------------------------------------------------
    def flush(self, runtime="gemini", environ=None):
        oi.flush_at_turn_end(self.home, OI_SID, time.monotonic() + 2, runtime=runtime, conn=self.fake.conn,
                             client="t", environ=environ or {})

    def test_turn_end_flush_posts_or_deletes(self):
        self.bind()
        self.queue(1)
        self.flush()
        self.assertEqual(self.texts()[-1:], ["queued 0"])
        self.assertFalse(self.outbox().exists())
        self.queue(1)
        self.flush(environ={"CARDINAL_OWNER_INPUT": "0"})
        self.assertFalse(self.outbox().exists())
        self.assertNotIn("owner_input_disclosed", self.binding())
        self.bind()
        self.queue(1)
        n = len(self.fake.requests)
        self.flush(runtime="cursor")   # no consent
        self.assertFalse(self.outbox().exists())
        self.assertEqual(len(self.fake.requests), n)


if __name__ == "__main__":
    unittest.main()

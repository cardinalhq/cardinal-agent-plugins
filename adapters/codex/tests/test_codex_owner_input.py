"""Codex prompt and turn-end hooks over the shared core (cross-agent parity, Lane B).

UserPromptSubmit (cardinal-codex-telemetry.py handle_user_prompt_submit):
  - owner input (owner_input.capture, runtime "codex"): the first prompt that
    would be recorded is not; the disclosure is shown as a user-visible
    systemMessage; later prompts are recorded exactly as typed. Never a
    subagent's prompt (agent_id), never a prompt Codex generated for /review
    or /init, never under the kill switch, never without the capability;
  - everything synchronous on the network ends by START + 3.5 s, so the
    limits gate's verdict still reaches Codex (5 s timeout); a block verdict
    wins outright and nothing is recorded;
  - staged items (investigation_agent.prompt) and, only when it changed, the
    session guidance go into additionalContext.
Stop: investigation_agent.turn_end: the inbox, the synchronous check when the
poller is stale, then the owner-input outbox flush.

Mirrors adapters/claude/tests/test_owner_input_hook.py and
test_investigation_events_hook.py. Payload shapes: captured from codex-cli
0.160.1 / 0.162.1 (Lane B payload capture).

Run: cd adapters/codex/tests && python3 -m unittest test_codex_owner_input -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import test_codex_storyboard as fixtures
from test_codex_storyboard import ADAPTER, HOOK, SESSION, SESSION_START, VERSION
from test_codex_investigation import INV, SB

sys.path.insert(0, str(ADAPTER / "hooks"))
from cardinal_core import investigation_turn  # noqa: E402

# The UserPromptSubmit payload codex-cli 0.162.1 sent (cwd replaced).
PROMPT = {"session_id": SESSION, "turn_id": "01a12f79-adab-7df3-b186-3b8749c73bff",
          "transcript_path": "<transcript>", "cwd": "<cwd>", "hook_event_name": "UserPromptSubmit",
          "model": "gpt-6.1-sol", "permission_mode": "bypassPermissions",
          "prompt": "Run `cat notes.md` and tell me the TTL in one short line."}
# /review (uncommitted changes preset) and /init: the prompt Codex submits.
REVIEW = "Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings."
REVIEW_BRANCH = ("Review the code changes against the base branch 'main'. The merge base commit for this comparison "
                 "is 0123abc. Run `git diff 0123abc` to inspect the changes relative to main. Provide prioritized, "
                 "actionable findings.")
INIT = ("Generate a file named AGENTS.md that serves as a contributor guide for this repository.\nBefore writing, "
        "check whether AGENTS.md already exists in the current working directory.")
RECORDED = {"event": {"investigation_id": INV, "seq": 7, "type": "owner_input.recorded"}, "duplicate": False}
CUE = {"investigation_id": INV, "seq": 1, "type": "cue.added", "class": "control", "authority": "advisory",
       "created_at": "2026-10-12T00:47:48Z",
       "producer": {"principal": {"kind": "user", "id": "supervisor"}, "session_id": "supervisor-session",
                    "is_investigation_author": True},
       "payload": {"text": "Check the TTL against the runbook."}}


class OwnerInputBase(unittest.TestCase):
    setUp = fixtures.CodexStoryboardTests.setUp
    tearDown = fixtures.CodexStoryboardTests.tearDown
    connect = fixtures.CodexStoryboardTests.connect

    def env(self, **extra: str) -> dict:
        # Detached children (the PR-cache refresh) may outlive a test: no
        # bytecode caches under the temporary HOME for them to write while
        # it is removed (macOS's Python 3.9 caches under ~/Library/Caches).
        return fixtures.CodexStoryboardTests.env(self, PYTHONDONTWRITEBYTECODE="1", **extra)

    run_hook = fixtures.CodexStoryboardTests.run_hook
    session_context = fixtures.CodexStoryboardTests.session_context

    def setup_live(self, owner_input=True, author=True):
        self.connect()
        self.fake.routes["ensure-session-investigation"] = (200, {
            "investigation_id": INV, "storyboard_id": SB,
            "view_url": f"{self.fake.origin}/storyboards/{SB}",
            "investigation_url": f"/storyboards/{SB}/investigation",
            "is_author": author, "capabilities": {"projection": {"enabled": True},
                                                   "owner_input": {"enabled": owner_input}}})
        self.fake.routes["record-owner-input"] = (200, RECORDED)
        self.fake.routes["read-investigation-events"] = (200, {
            "investigation_id": INV, "events": [], "next_after": 0, "head_seq": 0})

    def start(self, **env):
        return self.session_context(CARDINAL_INVESTIGATION_POLLER="0", **env)

    def prompt(self, text=PROMPT["prompt"], *, payload=None, **env):
        payload = dict(payload or PROMPT, prompt=text)
        return self.run_hook("UserPromptSubmit", payload, CARDINAL_INVESTIGATION_POLLER="0",
                             CARDINAL_STORYBOARD_DISCOVERY="0", **env)

    def out(self, res) -> dict:
        return json.loads(res.stdout) if res.stdout.strip() else {}

    def context(self, res) -> str:
        return (self.out(res).get("hookSpecificOutput") or {}).get("additionalContext") or ""

    def records(self) -> list:
        return [r["body"] for r in self.fake.requests if r["path"].endswith("/record-owner-input")]

    @property
    def sessions(self) -> Path:
        return self.home / ".cardinal" / "investigations" / "sessions"

    def binding(self) -> dict:
        return json.loads((self.sessions / f"{SESSION}.json").read_text())

    def outbox(self) -> Path:
        return self.sessions / f"{SESSION}.owner-input-outbox.json"

    def disclosed(self):
        """SessionStart, then the first prompt: the disclosure."""
        self.start()
        res = self.prompt("first prompt, only disclosed")
        self.assertIn("systemMessage", self.out(res))
        return res


class PromptCaptureTests(OwnerInputBase):
    def test_discloses_first_then_records_exactly_what_was_typed(self):
        self.setup_live()
        self.start()
        first = self.out(self.prompt("first prompt, only disclosed"))
        message = first["systemMessage"]
        self.assertIn(f"recorded to Investigation {INV} as owner input", message)
        self.assertIn('{"codex": "off"}', message)
        self.assertIn("CARDINAL_OWNER_INPUT=0", message)
        self.assertEqual(self.records(), [], "the disclosing prompt is not captured")
        self.assertTrue(self.binding()["owner_input_disclosed"])
        res = self.prompt()
        self.assertNotIn("systemMessage", self.out(res))
        [body] = self.records()
        self.assertEqual(body["payload"]["text"], PROMPT["prompt"])
        self.assertEqual((body["investigation_id"], body["session_id"], body["payload"]["turn"]), (INV, SESSION, 1))
        req = next(r for r in self.fake.requests if r["path"].endswith("/record-owner-input"))
        self.assertEqual(req["headers"]["x-cardinal-client"], f"codex/{VERSION}")
        self.assertEqual(req["headers"]["x-cardinalhq-api-key"], "ck_codex_test")

    def test_a_subagent_prompt_is_never_recorded(self):
        self.setup_live()
        self.disclosed()
        res = self.prompt("a subagent's task", payload={**PROMPT, "agent_id": "01a12f88-a66f-7161-a44d-890bf33ab468",
                                                        "agent_type": "default"})
        self.assertEqual(self.records(), [])
        self.assertNotIn(INV, self.context(res), "a subagent gets no guidance and consumes nothing")

    def test_review_and_init_prompts_are_not_owner_input(self):
        self.setup_live()
        self.disclosed()
        for text in (REVIEW, REVIEW_BRANCH, INIT):
            self.prompt(text)
        self.assertEqual(self.records(), [])
        # A /review with custom instructions is what the owner typed.
        self.prompt("Only check app.py for the TTL value; one line.")
        self.assertEqual([b["payload"]["text"] for b in self.records()],
                         ["Only check app.py for the TTL value; one line."])

    def test_the_kill_switch_in_the_environment_or_the_cardinal_file(self):
        self.setup_live()
        self.disclosed()
        self.prompt(CARDINAL_OWNER_INPUT="0")
        self.assertEqual(self.records(), [])
        self.assertNotIn("owner_input_disclosed", self.binding(), "turning it off forgets the disclosure")
        kill = self.home / ".cardinal" / "owner-input.json"
        kill.write_text(json.dumps({"codex": "off"}))
        self.assertNotIn("systemMessage", self.out(self.prompt()))
        self.assertEqual(self.records(), [])
        kill.write_text(json.dumps({"claude": "off"}))   # another runtime's switch: not this one
        self.assertIn("systemMessage", self.out(self.prompt()), "told again before capture resumes")
        self.prompt("and now it is recorded")
        self.assertEqual([b["payload"]["text"] for b in self.records()], ["and now it is recorded"])

    def test_capability_off_records_nothing_and_says_nothing(self):
        self.setup_live(owner_input=False)
        self.start()
        for _ in range(2):
            self.assertNotIn("systemMessage", self.out(self.prompt()))
        self.assertNotIn("record-owner-input", str(self.fake.requests))

    def test_a_joined_session_never_records(self):
        self.setup_live(author=False)
        self.start()
        self.prompt()
        self.prompt()
        self.assertEqual(self.records(), [])

    def test_the_same_prompt_typed_again_is_recorded_again(self):
        # Codex fires UserPromptSubmit once per prompt: no repeat skip
        # (owner_input.DEDUPE_REPEATS is Gemini only).
        self.setup_live()
        self.disclosed()
        self.prompt()
        self.prompt()
        self.assertEqual(len(self.records()), 2, "the owner typed it twice")
        self.run_hook("Stop", {**PROMPT, "hook_event_name": "Stop", "stop_hook_active": False},
                      CARDINAL_INVESTIGATION_POLLER="0")
        self.prompt()
        self.assertEqual(len(self.records()), 3)

    def write_verdict(self, verdict: dict) -> None:
        limits_dir = self.home / ".codex" / "cardinal" / "limits"
        limits_dir.mkdir(parents=True, exist_ok=True)
        (limits_dir / f"{SESSION}.verdict.json").write_text(json.dumps({"fetched_at": time.time(), **verdict}))

    def test_an_unreachable_server_queues_within_the_deadline_and_the_verdict_still_lands(self):
        self.setup_live()
        self.disclosed()
        # The spend gate has a new warn verdict: its systemMessage must reach
        # Codex although record-owner-input never answers in time.
        self.write_verdict({"decision": "warn", "band": 2, "agent_context": "Economize: budget at 90%.",
                            "user_message": "Cardinal: session budget at 90%."})
        self.fake.delays["record-owner-input"] = 8
        began = time.monotonic()
        res = self.prompt()
        took = time.monotonic() - began
        self.assertLess(took, 5.0, "inside Codex's 5 s UserPromptSubmit timeout")
        out = self.out(res)
        self.assertEqual(out["systemMessage"], "Cardinal: session budget at 90%.")
        self.assertIn("Economize: budget at 90%.", out["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.outbox().exists(), "queued for the turn end")
        self.assertEqual(os.stat(self.outbox()).st_mode & 0o777, 0o600)

    def test_a_block_verdict_wins_and_nothing_is_recorded_or_consumed(self):
        self.setup_live()
        self.disclosed()
        investigation_turn.stage(self.home, SESSION, "STAGED: waits for a prompt that reaches the model")
        self.write_verdict({"decision": "block", "band": 3, "block_reason": "Session budget of $100 reached."})
        out = self.out(self.prompt())
        self.assertEqual(out, {"decision": "block", "reason": "Session budget of $100 reached."})
        self.assertEqual(self.records(), [])
        self.assertEqual(investigation_turn.consume_staged(self.home, SESSION),
                         ["STAGED: waits for a prompt that reaches the model"])


class OwnerRelayTests(OwnerInputBase):
    """The owner's `approve` of a proposed owner decision (owner_relay): relayed
    with this machine's key whether or not owner input is on; the outcome is
    the systemMessage AND model context."""

    def propose(self):
        from cardinal_core import owner_relay
        return owner_relay.propose(self.home, SESSION, "inv_" + "e" * 24, "9a8b7c6d-worker", "Only the cache layer.")

    def test_approve_relays_and_reports_to_user_and_model(self):
        self.setup_live(owner_input=False)
        self.start()
        entry = self.propose()
        self.fake.routes["relay-owner-decision"] = (201, {"event": {
            "investigation_id": "inv_" + "e" * 24, "seq": 57, "type": "owner.decided"}, "duplicate": False})
        res = self.prompt(f"approve {entry['id']}")
        out = self.out(res)
        want = f"Cardinal: relayed owner decision {entry['id']} to session 9a8b7c6d as event #57 (owner_relayed)."
        self.assertIn(want, out["systemMessage"])
        self.assertIn(want, self.context(res))
        [req] = [r for r in self.fake.requests if r["path"].endswith("/relay-owner-decision")]
        self.assertEqual(req["body"]["relay"]["supervisor_session_id"], SESSION)
        self.assertEqual(req["headers"]["x-cardinalhq-api-key"], "ck_codex_test")
        self.assertEqual(self.records(), [])

    def test_decide_relays_the_owners_words(self):
        from cardinal_core import owner_relay
        self.setup_live(owner_input=False)
        self.start()
        owner_relay.add_target(self.home, SESSION, "inv_" + "e" * 24, "9a8b7c6d-worker")
        self.fake.routes["relay-owner-decision"] = (201, {"event": {
            "investigation_id": "inv_" + "e" * 24, "seq": 58, "type": "owner.decided"}, "duplicate": False})
        out = self.out(self.prompt("decide: Stop after the cache fix."))
        self.assertIn("relayed your decision verbatim to session 9a8b7c6d as event #58", out["systemMessage"])
        [req] = [r for r in self.fake.requests if r["path"].endswith("/relay-owner-decision")]
        self.assertEqual(req["body"]["relay"]["source"], "verbatim")

    def test_a_subagent_or_unrelated_prompt_relays_nothing(self):
        self.setup_live(owner_input=False)
        self.start()
        self.propose()
        self.prompt("approve", payload={**PROMPT, "agent_id": "01a12f88-a66f-7161-a44d-890bf33ab468"})
        res = self.prompt("approve the plan")
        self.assertNotIn("systemMessage", self.out(res))
        self.assertFalse([r for r in self.fake.requests if r["path"].endswith("/relay-owner-decision")])


class PromptContextTests(OwnerInputBase):
    def test_guidance_is_sent_once_and_again_only_when_it_changes(self):
        self.setup_live(owner_input=False)
        start = self.start()
        self.assertIn(f"Investigation timeline (shows the live record before any publish)", start)
        self.assertNotIn(f"Investigation {INV}", self.context(self.prompt()), "SessionStart already sent it")
        self.assertNotIn(f"Investigation {INV}", self.context(self.prompt()))
        # The poller refreshed the capabilities: owner input is now recorded,
        # and the guidance (which says so) is sent once more.
        b = self.binding()
        b["capabilities"]["owner_input"] = {"enabled": True}
        (self.sessions / f"{SESSION}.json").write_text(json.dumps(b))
        ctx = self.context(self.prompt())
        self.assertIn(f"Investigation {INV}", ctx)
        self.assertIn("recorded to Investigation", ctx)
        self.assertNotIn(f"Investigation {INV}", self.context(self.prompt()))

    def test_a_compaction_session_start_sends_it_again(self):
        self.setup_live(owner_input=False)
        self.start()
        again = self.session_context({**SESSION_START, "source": "compact"}, CARDINAL_INVESTIGATION_POLLER="0")
        self.assertIn(f"Investigation {INV}", again)
        self.assertNotIn(f"Investigation {INV}", self.context(self.prompt()))

    def test_a_pending_bootstrap_completed_later_is_described_at_the_next_prompt(self):
        self.setup_live(owner_input=False)
        self.fake.routes["ensure-session-investigation"] = (503, {"error": "unavailable"})
        self.assertNotIn(INV, self.start())
        self.assertEqual(self.context(self.prompt()), "")
        # The poller's retry completes the bootstrap (the same core call).
        self.setup_live(owner_input=False)
        from cardinal_core import investigation_bootstrap as boot
        boot.ensure(self.home, SESSION, {"origin": self.fake.origin, "org": "org-1", "key": "ck_codex_test"},
                    f"codex/{VERSION}", force=True)
        self.assertIn(f"Investigation {INV}", self.context(self.prompt()))
        self.assertNotIn(f"Investigation {INV}", self.context(self.prompt()))

    def test_staged_items_reach_the_next_prompt_once(self):
        self.setup_live(owner_input=False)
        self.start()
        investigation_turn.stage(self.home, SESSION, "STAGED: a late discovery block")
        self.assertIn("STAGED: a late discovery block", self.context(self.prompt()))
        self.assertNotIn("STAGED", self.context(self.prompt()))

    def test_the_relook_runs_detached_and_stages_what_it_finds(self):
        self.setup_live(owner_input=False)
        self.start()
        # The branch moved since SessionStart's look: the re-look finds the
        # storyboard again, and stages its block for the next boundary.
        fixtures._git(self.repo, "checkout", "-q", "-b", "fix/other")
        began = time.monotonic()
        self.run_hook("UserPromptSubmit", PROMPT, CARDINAL_INVESTIGATION_POLLER="0",
                      CARDINAL_CODEX_BACKGROUND_INLINE="1")
        self.assertLess(time.monotonic() - began, 10)
        staged = investigation_turn.consume_staged(self.home, SESSION)
        self.assertTrue(any(fixtures.SB1 in s for s in staged), staged)

    def test_prompt_is_silent_for_an_unbound_session(self):
        self.connect()
        res = self.prompt()
        self.assertEqual(self.context(res), "")
        self.assertEqual(self.records(), [])


class StopTests(OwnerInputBase):
    def stop(self, active=False, **env):
        return self.run_hook("Stop", {**PROMPT, "hook_event_name": "Stop", "stop_hook_active": active,
                                      "last_assistant_message": "done"}, **env)

    def test_stop_reads_synchronously_when_the_poller_is_stale_and_blocks(self):
        self.setup_live(owner_input=False)
        self.start()
        self.fake.routes["read-investigation-events"] = (200, {
            "investigation_id": INV, "events": [CUE], "next_after": 1, "head_seq": 1})
        out = json.loads(self.stop().stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("Check the TTL against the runbook.", out["reason"])
        self.assertIn("authority: ADVISORY", out["reason"])
        # The ack command is this install's launcher, never Claude's bare name.
        self.assertIn(f"{fixtures.CLI} investigation ack {INV} 1 --session {SESSION}", out["reason"])
        self.assertEqual(self.binding()["cursor"], 1)

    def test_stop_with_nothing_waiting_is_silent(self):
        self.setup_live(owner_input=False)
        self.start()
        self.assertEqual(self.stop().stdout, "")

    def test_stop_flushes_the_owner_input_outbox(self):
        self.setup_live()
        self.disclosed()
        self.fake.routes["record-owner-input"] = (503, {"error": "unavailable"})
        self.prompt()
        self.assertTrue(self.outbox().exists())
        self.fake.routes["record-owner-input"] = (200, RECORDED)
        b = self.binding()
        b.pop("owner_input_retry_after", None)
        (self.sessions / f"{SESSION}.json").write_text(json.dumps(b))
        box = json.loads(self.outbox().read_text())
        box["retry_after"] = 0
        self.outbox().write_text(json.dumps(box))
        self.stop()
        self.assertFalse(self.outbox().exists(), "posted at the turn end")
        self.assertEqual(self.records()[-1]["payload"]["text"], PROMPT["prompt"])

    def test_stop_deletes_the_outbox_under_the_kill_switch(self):
        self.setup_live()
        self.disclosed()
        self.fake.routes["record-owner-input"] = (503, {"error": "unavailable"})
        self.prompt()
        self.assertTrue(self.outbox().exists())
        n = len(self.records())
        self.stop(CARDINAL_OWNER_INPUT="off")
        self.assertFalse(self.outbox().exists())
        self.assertEqual(len(self.records()), n)


class DisclosureFlushTests(OwnerInputBase):
    """R3: the disclosure counts as shown only once it was flushed to Codex."""

    def prompt_with_checked_stdout(self, fail_write: bool) -> str:
        """UserPromptSubmit with a stdout that notes, at every write, whether
        the binding already says the user was told (and can fail the write)."""
        log = self.home / "writes.log"
        binding = self.sessions / f"{SESSION}.json"
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
            f"sys.argv = [{str(HOOK)!r}, '--event', 'UserPromptSubmit']\n"
            f"runpy.run_path({str(HOOK)!r}, run_name='__main__')\n")
        payload = json.loads(json.dumps(PROMPT).replace("<cwd>", str(self.repo)))
        res = subprocess.run([sys.executable, "-c", wrapper], input=json.dumps(payload), capture_output=True,
                             text=True, timeout=30, cwd=str(self.repo),
                             env=self.env(CARDINAL_INVESTIGATION_POLLER="0", CARDINAL_STORYBOARD_DISCOVERY="0"))
        self.assertEqual(res.returncode, 0, res.stderr)
        return log.read_text()

    def test_the_disclosure_is_recorded_only_after_it_was_flushed(self):
        self.setup_live()
        self.start()
        self.assertEqual(self.prompt_with_checked_stdout(fail_write=False), "None True\n")
        self.assertIs(self.binding()["owner_input_disclosed"], True)
        self.assertEqual(self.records(), [])

    def test_a_failed_write_records_no_disclosure(self):
        self.setup_live()
        self.start()
        self.assertEqual(self.prompt_with_checked_stdout(fail_write=True), "None True\n")
        self.assertNotIn("owner_input_disclosed", self.binding())
        self.assertIn("systemMessage", self.out(self.prompt()), "told again: the first one never reached Codex")
        self.assertEqual(self.records(), [])


def load_hook_module():
    """The hook as a module (its own sys.path.insert finds the vendored core)."""
    import importlib.util
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cardinal_codex_telemetry_budget", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class PostOutputBudgetTests(unittest.TestCase):
    """Everything after the prompt's output (git, git_state, the verdict
    refresh) ends by START + PROMPT_EXIT_S: each git call gets what is left,
    and the rest is skipped once it is spent."""

    def run_prompt(self, left: float, git_takes: float):
        from unittest import mock

        mod = load_hook_module()
        calls: dict = {"git": [], "emit": [], "refresh": []}

        def fake_git(args, cwd, timeout=1.0):
            calls["git"].append(timeout)
            time.sleep(min(timeout, git_takes))
            return None if git_takes >= timeout else "x"

        mod.START = time.monotonic() - mod.PROMPT_EXIT_S + left
        with mock.patch.object(mod.initiative, "git", fake_git), \
                mock.patch.object(mod.limits, "gate_output", return_value=None), \
                mock.patch.object(mod.limits, "maybe_refresh_verdict",
                                  side_effect=lambda *a, **k: calls["refresh"].append(k["timeout"])), \
                mock.patch.object(mod, "decision_context", return_value=None), \
                mock.patch.object(mod, "prompt_investigation", return_value=([], None)), \
                mock.patch.object(mod, "spawn_relook"), \
                mock.patch.object(mod, "cached_pr", return_value=(None, None, True)), \
                mock.patch.object(mod, "emit_records",
                                  side_effect=lambda recs, timeout: calls["emit"].append(timeout)):
            began = time.monotonic()
            mod.handle_user_prompt_submit({"session_id": SESSION, "cwd": str(ADAPTER), "prompt": "hi"})
            took = time.monotonic() - began
        return calls, took

    def test_slow_git_is_bounded_by_what_is_left_and_the_rest_is_skipped(self):
        calls, took = self.run_prompt(left=0.3, git_takes=5.0)
        self.assertTrue(calls["git"], "git was asked")
        self.assertTrue(all(t <= 0.31 for t in calls["git"]), calls["git"])
        self.assertLess(took, 0.3 + 0.25, "no git call outlives the budget")
        self.assertEqual((calls["emit"], calls["refresh"]), ([], []), "the budget is spent: the rest is skipped")

    def test_with_time_left_git_state_is_posted_within_it(self):
        calls, _ = self.run_prompt(left=3.0, git_takes=0.0)
        self.assertEqual(len(calls["emit"]), 1)
        self.assertLessEqual(calls["emit"][0], 3.0)
        self.assertTrue(all(t <= 3.0 for t in calls["git"]), calls["git"])


if __name__ == "__main__":
    unittest.main()

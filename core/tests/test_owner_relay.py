"""Relayed owner decisions (docs/specs/relayed-owner-decisions.md), plugin
side: the proposal store, propose-decision's refusals, the prompt hook's
approval matcher and relay outcomes, the worker's rendering of an
owner_relayed owner.decided, and that nothing but the prompt hook relays."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cardinal_core import investigation_agent as agent
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_state as ist
from cardinal_core import investigation_state_sync as sync
from cardinal_core import owner_input
from cardinal_core import owner_relay as rel

CORE = Path(__file__).resolve().parent.parent / "cardinal_core"
SUP_INV = "inv_" + "a" * 24      # the supervisor session's own Investigation
WORKER_INV = "inv_" + "b" * 24
SID = "supervisor-1"
WORKER_SID = "1a2b3c4d-worker"
MEMBER = {"origin": "https://test.invalid", "org": "org", "key": "test-key"}
CAPS = {"owner_relay": {"enabled": True}, "owner_decision": {"enabled": True}}
TEXT = "Scope: only the cache layer.\nDo not touch the restore path."


def answer(seq=57, duplicate=False):
    return {"event": {"investigation_id": WORKER_INV, "seq": seq, "type": "owner.decided", "class": "owner",
                      "authority": "owner_relayed"}, "duplicate": duplicate}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        isolated = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
        isolated["HOME"] = str(self.home)
        env_patch = patch.dict(os.environ, isolated, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.bind()
        self.wiring = SimpleNamespace(paths=SimpleNamespace(home=self.home / ".codex"), environ={},
                                      client="codex/test", cli=str(self.home / "scripts/cli"),
                                      connection=Mock(return_value=MEMBER))

    def bind(self, **fields):
        b = {"investigation_id": SUP_INV, "cursor": 0, "org": "org", "is_author": True, "capabilities": CAPS}
        b.update(fields)
        ie.write_binding(self.home, SID, b)

    def run_cli(self, *argv, environ=None):
        if environ is not None:
            self.wiring.environ = environ
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agent.cli_main(list(argv), self.wiring)
        return code, out.getvalue(), err.getvalue()

    def propose(self, text=TEXT, **kw):
        return rel.propose(self.home, SID, WORKER_INV, WORKER_SID, text, **kw)

    def hook(self, prompt, runtime="claude", **kw):
        kw.setdefault("conn", MEMBER)
        kw.setdefault("deadline", time.monotonic() + 3.0)
        return rel.handle_prompt(self.home, SID, prompt, runtime=runtime, client="claude-plugin/test", **kw)


class Store(Base):
    def test_file_is_private_ids_are_dp_hex_and_entries_hash_their_text(self):
        e = self.propose(answers=4, refs=["#4"])
        self.assertRegex(e["id"], r"^dp_[0-9a-f]{16}$")
        self.assertEqual(e["sha256"], hashlib.sha256(TEXT.encode()).hexdigest())
        path = rel.proposals_path(self.home, SID)
        self.assertEqual(path.name, f"{SID}.proposals.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(rel.pending(self.home, SID), [e])

    def test_expiry_and_cap_keep_the_newest_five(self):
        now = 1_000_000.0
        made = [self.propose(text=f"d{i}", now=now + i) for i in range(7)]
        self.assertEqual([e["id"] for e in rel.pending(self.home, SID, now=now + 10)], [e["id"] for e in made[2:]])
        self.assertEqual(rel.pending(self.home, SID, now=now + 6 + rel.PROPOSAL_TTL), [])
        # A tampered entry (text no longer matches its hash) is not pending.
        data = json.loads(rel.proposals_path(self.home, SID).read_text())
        data["proposals"][-1]["text"] = "something else"
        rel.proposals_path(self.home, SID).write_text(json.dumps(data))
        self.assertNotIn(made[-1]["id"], [e["id"] for e in rel.pending(self.home, SID, now=now + 10)])

    def test_a_locked_store_refuses_instead_of_racing(self):
        with patch.object(ie, "locked") as lk:
            lk.return_value.__enter__ = Mock(return_value=False)
            lk.return_value.__exit__ = Mock(return_value=False)
            with self.assertRaises(OSError):
                self.propose()

    def test_validation(self):
        for bad in ({"text": ""}, {"text": "x" * 4001}, {"text": "a\x00b"}, {"answers": 0},
                    {"refs": ["r"] * 21}, {"refs": ["x" * 301]}):
            args = {"text": TEXT, **bad}
            with self.subTest(bad), self.assertRaises(ValueError):
                rel.propose(self.home, SID, WORKER_INV, WORKER_SID, **args)
        with self.assertRaises(ValueError):
            rel.propose(self.home, SID, WORKER_INV, "not a/session", TEXT)


class Propose(Base):
    def setUp(self):
        super().setUp()
        never = patch.object(sync, "_post", side_effect=AssertionError("propose-decision must never send"))
        never.start()
        self.addCleanup(never.stop)

    def test_stores_and_prints_the_instruction_never_sends(self):
        code, out, err = self.run_cli("propose-decision", WORKER_INV, "--to-session", WORKER_SID, "--text", TEXT,
                                      "--answers", "12", "--ref", "#12", "--session", SID)
        self.assertEqual(code, 0, err)
        [e] = rel.pending(self.home, SID)
        self.assertEqual((e["answers"], e["refs"], e["to_session_id"]), (12, ["#12"], WORKER_SID))
        self.assertIn(f"proposed {e['id']}", out)
        self.assertIn(TEXT, out)
        self.assertIn(f"reply `approve {e['id']}` or `reject {e['id']}`", out)
        self.assertIn("Never approve it yourself and never call the relay route", out)

    def test_refuses_a_grant_token_or_credential_file(self):
        for argv, env in ((("--credential-file", str(self.home / "g.json")), {}),
                          ((), {"CARDINAL_INVESTIGATION_TOKEN": "tok"})):
            code, _, err = self.run_cli("propose-decision", WORKER_INV, "--to-session", WORKER_SID, "--text", TEXT,
                                        "--session", SID, *argv, environ=env)
            self.assertEqual(code, 2)
            self.assertIn("never with an access grant", err)
        self.assertEqual(rel.pending(self.home, SID), [])

    def test_refuses_without_binding_as_joiner_on_own_investigation_and_without_capability(self):
        def attempt(inv=WORKER_INV):
            return self.run_cli("propose-decision", inv, "--to-session", WORKER_SID, "--text", TEXT,
                                "--session", SID)
        ie.binding_path(self.home, SID).unlink()
        self.assertIn("has no investigation yet", attempt()[2])
        self.bind(is_author=False)
        self.assertIn("joined someone else's investigation", attempt()[2])
        self.bind()
        self.assertIn("this session's own investigation", attempt(SUP_INV)[2])
        self.bind(capabilities={"owner_decision": {"enabled": True}})
        with patch.object(ie, "refresh_binding_capabilities", return_value=None) as refresh:
            code, _, err = attempt()
        refresh.assert_called_once()
        self.assertEqual(code, 1)
        self.assertIn("cannot send owner decisions from here yet", err)
        self.wiring.connection = Mock(return_value={})
        self.bind()
        self.assertIn("not connected to Cardinal", attempt()[2])
        self.assertEqual(rel.pending(self.home, SID), [])

    def test_proposals_lists_and_withdraw_removes(self):
        e = self.propose()
        code, out, _ = self.run_cli("proposals", "--session", SID)
        self.assertEqual(code, 0)
        self.assertIn(e["id"], out)
        self.assertIn(ie.json_text(TEXT), out)   # one inert line
        self.assertEqual(self.run_cli("withdraw-proposal", e["id"], "--session", SID)[0], 0)
        self.assertEqual(rel.pending(self.home, SID), [])
        self.assertEqual(self.run_cli("withdraw-proposal", e["id"], "--session", SID)[0], 1)
        self.assertEqual(self.run_cli("withdraw-proposal", "dp_nothex!", "--session", SID)[0], 2)


class Matching(Base):
    def test_shapes(self):
        ok = {"approve": ("approve", None), "  Approve. ": ("approve", None), "REJECT!": ("reject", None),
              "approve dp_0123abcd4567ef89": ("approve", "dp_0123abcd4567ef89"), "Approve DP_0123ABCD4567EF89.": ("approve", "dp_0123abcd4567ef89"),
              "reject\tdp_0123abcd4567ef89": ("reject", "dp_0123abcd4567ef89")}
        for prompt, want in ok.items():
            self.assertEqual(rel.match(prompt), want, prompt)
        for prompt in ("approve it", "approve dp_0123", "approve dp_0123abcd", "please approve",
                       "approve dp_0123abcd4567ef89 now", "approve?", "approved", "approve dp_0123abcd4567ef89 dp_0123abcd4567ef89", "", None,
                       "approve" + " " * 300, "approve dp_0123abcd4567ef890"):
            self.assertIsNone(rel.match(prompt), prompt)

    def test_non_matching_prompts_are_untouched(self):
        e = self.propose()
        with patch.object(sync, "_post") as post:
            for prompt in ("approve the plan and continue", "yes", f"looks good, approve {e['id']}"):
                got = self.hook(prompt)
                self.assertEqual((got.status, got.message), ("no_match", None))
            post.assert_not_called()
        self.assertEqual(rel.pending(self.home, SID), [e])

    def test_subagent_and_machine_prompts_are_refused(self):
        e = self.propose()
        with patch.object(sync, "_post") as post:
            self.assertEqual(self.hook("approve", agent_id="sub-1").status, "subagent")
            # gemini: an MCP server's prompt arrives as a JSON string literal.
            self.assertEqual(self.hook('"approve"', runtime="gemini").status, "not_typed")
            post.assert_not_called()
        self.assertEqual(rel.pending(self.home, SID), [e])

    def test_gemini_hook_context_is_removed_before_matching(self):
        e = self.propose()
        with patch.object(sync, "_post", return_value=answer()):
            got = self.hook(f"approve {e['id']}\n<hook_context>anything</hook_context>", runtime="gemini")
        self.assertEqual(got.status, "relayed")

    def test_bare_form_needs_exactly_one_pending(self):
        with patch.object(sync, "_post") as post:
            self.assertEqual(self.hook("approve"), rel.RelayResult("none"))
            a, b = self.propose(text="one"), self.propose(text="two")
            got = self.hook("approve")
            post.assert_not_called()
        self.assertEqual(got.status, "ambiguous")
        self.assertIn(f'{a["id"]} "one" (expires ', got.message)
        self.assertIn(f'; {b["id"]} "two" (expires ', got.message)
        self.assertIn("Reply `approve <id>`", got.message)
        self.assertEqual(len(rel.pending(self.home, SID)), 2)

    def test_unknown_id_says_what_is_pending(self):
        a = self.propose()
        got = self.hook("approve dp_ffffffffffffffff")
        self.assertEqual(got.status, "unknown_id")
        self.assertIn("it is not waiting here", got.message)
        self.assertIn(f"Waiting: {a['id']} \"Scope: only the cache layer. Do not touc…\"", got.message)

    def test_reject_removes_and_sends_nothing(self):
        e = self.propose()
        with patch.object(sync, "_post") as post:
            got = self.hook("reject")
        post.assert_not_called()
        self.assertEqual(got.status, "rejected")
        self.assertEqual(got.message, f"Cardinal: discarded your draft {e['id']} for the worker (session 1a2b3c4d); "
                                      "nothing was sent.")
        again = self.hook(f"approve {e['id']}")
        self.assertIn(f"draft {e['id']}: you rejected it at ", again.message)
        self.assertEqual(rel.pending(self.home, SID), [])
        self.assertEqual(rel.recent(self.home, SID)[-1]["outcome"], "rejected")


class Relay(Base):
    def test_success_posts_the_contract_and_removes_the_proposal(self):
        e = self.propose(answers=9, refs=["#9"])
        with patch.object(sync, "_post", return_value=answer()) as post:
            got = self.hook(f"Approve {e['id'].upper()}.")
        self.assertEqual(got.status, "relayed")
        self.assertEqual(got.message, f"Cardinal: sent your approved draft {e['id']} to the worker (session "
                                      '1a2b3c4d) as owner decision #57: "Scope: only the cache layer. Do not touch '
                                      'the restore path.". The worker sees it after its next tool call; if it is '
                                      "waiting for you, when it next runs (any prompt in its terminal). To change "
                                      "it, send another decide:; the worker sees both.")
        (conn, route, body), kw = post.call_args
        self.assertEqual((conn, route), (MEMBER, "relay-owner-decision"))
        self.assertNotIn("token", conn)
        self.assertEqual(set(body), {"investigation_id", "to_session_id", "relay_key", "payload", "relay", "client"})
        self.assertEqual((body["investigation_id"], body["to_session_id"], body["relay_key"]),
                         (WORKER_INV, WORKER_SID, e["id"]))
        self.assertEqual(body["payload"], {"text": TEXT, "answers": 9, "refs": ["#9"]})
        relay = body["relay"]
        self.assertEqual((relay["supervisor_session_id"], relay["supervisor_investigation_id"]), (SID, SUP_INV))
        self.assertEqual(relay["proposal_sha256"], hashlib.sha256(TEXT.encode()).hexdigest())
        typed = f"Approve {e['id'].upper()}."
        self.assertEqual(relay["prompt_sha256"],
                         hashlib.sha256(owner_input.scrubbed_of(typed)[0].encode()).hexdigest())
        self.assertRegex(relay["approved_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(relay["source"], "proposal")
        self.assertNotIn("wake", body)
        self.assertLessEqual(kw["timeout"], rel.BUDGET)
        self.assertEqual(rel.pending(self.home, SID), [])

    def test_duplicate_counts_as_relayed(self):
        self.propose()
        with patch.object(sync, "_post", return_value=answer(duplicate=True)):
            got = self.hook("approve")
        self.assertEqual(got.status, "duplicate")
        self.assertIn("Cardinal: already sent your approved draft", got.message)
        self.assertEqual(rel.pending(self.home, SID), [])

    def test_transient_failures_keep_it_and_say_retry(self):
        e = self.propose()
        for effect in (ist.FetchError("relay-owner-decision failed: refused", network=True),
                       sync.ServerError(503, {"error": "unavailable"}, "503"),
                       sync.ServerError(503, {"error": "owner_relay_unavailable"}, "503"),
                       sync.ServerError(429, {"error": "rate_limited"}, "429"),
                       lambda *a, **k: {"event": "malformed"}):
            with self.subTest(effect), patch.object(sync, "_post", side_effect=effect):
                got = self.hook(f"approve {e['id']}")
            self.assertEqual(got.status, "transient")
            self.assertIn("Cardinal: not sent: Cardinal did not answer in time", got.message)
            self.assertIn(f"type `approve {e['id']}` to try again", got.message)
            self.assertEqual(rel.pending(self.home, SID), [e])

    def test_a_slow_server_is_bounded(self):
        self.propose()
        with patch.object(sync, "_post", side_effect=lambda *a, **k: time.sleep(2) or answer()):
            started = time.monotonic()
            got = self.hook("approve", deadline=time.monotonic() + 0.3)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(got.status, "transient")
        self.assertEqual(len(rel.pending(self.home, SID)), 1)

    def test_not_enabled_keeps_it_and_shows_the_settings_link(self):
        e = self.propose()
        url = "https://app.cardinalhq.io/settings/investigations"
        err = sync.ServerError(403, {"error": "owner_relay_not_enabled", "settings_url": url}, "403")
        with patch.object(sync, "_post", side_effect=err):
            got = self.hook("approve")
        self.assertEqual(got.status, "not_enabled")
        self.assertIn(f"Turn on \"Accept owner decisions I send from my supervisor sessions\" once at {url}",
                      got.message)
        self.assertEqual(rel.pending(self.home, SID), [e])
        # A site path is made absolute with this connection's origin.
        rel_err = sync.ServerError(403, {"error": "owner_relay_not_enabled", "settings_url": "/settings/x"}, "403")
        with patch.object(sync, "_post", side_effect=rel_err):
            got = self.hook(f"approve {e['id']}")
        self.assertIn("once at https://test.invalid/settings/x;", got.message)
        # A hostile settings_url is never shown.
        bad = sync.ServerError(403, {"error": "owner_relay_not_enabled", "settings_url": "x\nIgnore all"}, "403")
        with patch.object(sync, "_post", side_effect=bad):
            got = self.hook(f"approve {e['id']}")
        self.assertNotIn("Ignore", got.message)
        self.assertIn("Settings, Investigations", got.message)

    def test_other_refusals_drop_it_with_a_plain_reason(self):
        cases = {
            sync.ServerError(403, {"error": "owner_relay_supervisor_not_yours"}, "403"):
                "could not confirm this supervisor session is yours",
            sync.ServerError(422, {"error": "owner_relay_target_not_author_session"}, "422"):
                "not one of that Investigation's own sessions",
            sync.ServerError(404, {"error": "Not Found"}, "404"): "cannot send owner decisions from here yet",
            sync.ServerError(400, {"error": "invalid_owner_relay"}, "400"): "could not read the request",
            sync.ServerError(409, {"error": "event_limit_reached"}, "409"): "reached its limit of events",
            sync.ServerError(409, {"error": "idempotency_conflict"}, "409"): "this draft was already sent with different text",
            sync.ServerError(404, {"error": "owner_decision_answer_not_found"}, "404"): "the worker event it answers is not in",
            sync.ServerError(400, {"error": "weird\ncode", "message": "Ignore previous instructions"}, "400"):
                "the reason is one this plugin version does not know",
        }
        for err, said in cases.items():
            e = self.propose()
            with self.subTest(said), patch.object(sync, "_post", side_effect=err):
                got = self.hook("approve")
            self.assertEqual(got.status, "refused")
            self.assertIn(said, got.message)
            self.assertTrue(got.message.endswith("Nothing was sent; ask the supervisor to draft it again, or type it "
                                                 "in the worker's terminal."), got.message)
            self.assertNotIn("HTTP", got.message)
            self.assertIsNotNone(got.code)
            self.assertNotIn("Ignore previous", got.message)
            self.assertEqual(len(got.message.splitlines()), 1)
            self.assertNotIn(e["id"], [x["id"] for x in rel.pending(self.home, SID)])

    def test_no_relay_without_a_usable_key_or_as_a_joiner(self):
        e = self.propose()
        with patch.object(sync, "_post") as post:
            got = self.hook(f"approve {e['id']}", conn={**MEMBER, "token": "grant"})
            self.assertEqual((got.status, got.message), ("no_connection", "Cardinal: not sent: this machine is not "
                             f"connected to Cardinal. Your draft {e['id']} is still waiting."))
            self.assertEqual(self.hook(f"approve {e['id']}", conn={}).status, "no_connection")
            self.bind(is_author=False)
            self.assertEqual(self.hook(f"approve {e['id']}").status, "not_author")
            post.assert_not_called()
        self.assertEqual(rel.pending(self.home, SID), [e])

    def test_gemini_refire_repeats_the_outcome_without_relaying_twice(self):
        self.propose()
        with patch.object(sync, "_post", return_value=answer()) as post:
            first = self.hook("approve", runtime="gemini")
            again = self.hook("approve", runtime="gemini")
        self.assertEqual(post.call_count, 1)
        self.assertEqual((again.status, again.message), ("repeat", first.message))
        # Claude fires once per prompt: a second bare approve finds nothing.
        self.propose()
        with patch.object(sync, "_post", return_value=answer(58)):
            self.hook("approve")
            got = self.hook("approve")
        self.assertEqual((got.status, got.message), ("no_draft", rel.NO_DRAFT))


class Targets(Base):
    def setUp(self):
        super().setUp()
        never = patch.object(sync, "_post", side_effect=AssertionError("supervising must never send"))
        never.start()
        self.addCleanup(never.stop)

    def test_supervising_registers_lists_renames_and_removes(self):
        code, out, err = self.run_cli("supervising", WORKER_INV, "--to-session", WORKER_SID, "--name", "codex",
                                      "--session", SID)
        self.assertEqual(code, 0, err)
        self.assertIn("`decide codex: <text>`", out)
        self.assertIn("Never type it for them", out)
        self.assertEqual(rel.targets(self.home, SID)[0]["name"], "codex")
        self.assertEqual(stat.S_IMODE(rel.proposals_path(self.home, SID).stat().st_mode), 0o600)
        # A proposal to the same worker keeps its name; one to another worker registers it unnamed.
        self.propose()
        rel.propose(self.home, SID, "inv_" + "c" * 24, "other-worker", "x")
        self.assertEqual([t.get("name") for t in rel.targets(self.home, SID)], ["codex", None])
        code, out, _ = self.run_cli("supervising", "--session", SID)
        self.assertIn(f"codex -> session {WORKER_SID} of {WORKER_INV}", out)
        self.assertEqual(self.run_cli("supervising", "--remove", "--name", "codex", "--session", SID)[0], 0)
        self.assertEqual([t["to_session_id"] for t in rel.targets(self.home, SID)], ["other-worker"])
        self.assertEqual(self.run_cli("supervising", "--remove", "--name", "codex", "--session", SID)[0], 1)

    def test_cap_names_and_refusals(self):
        for i in range(7):
            rel.add_target(self.home, SID, WORKER_INV, f"w{i}", name=f"w{i}")
        self.assertEqual([t["name"] for t in rel.targets(self.home, SID)], [f"w{i}" for i in range(2, 7)])
        rel.add_target(self.home, SID, WORKER_INV, "w-new", name="w6")   # a name moves
        self.assertEqual([t["to_session_id"] for t in rel.targets(self.home, SID) if t["name"] == "w6"], ["w-new"])
        for bad in ("Codex", "a b", "x" * 33, ""):
            with self.assertRaises(ValueError):
                rel.add_target(self.home, SID, WORKER_INV, WORKER_SID, name=bad)
        for argv, env in ((("--credential-file", str(self.home / "g.json")), {}),
                          ((), {"CARDINAL_INVESTIGATION_TOKEN": "tok"})):
            code, _, err = self.run_cli("supervising", WORKER_INV, "--to-session", WORKER_SID, "--session", SID,
                                        *argv, environ=env)
            self.assertEqual(code, 2)
            self.assertIn("never with an access grant", err)
        self.wiring.environ = {}
        self.assertIn("this session's own investigation",
                      self.run_cli("supervising", SUP_INV, "--to-session", WORKER_SID, "--session", SID)[2])


class Decide(Base):
    def target(self, name=None, sid=WORKER_SID, inv=WORKER_INV):
        return rel.add_target(self.home, SID, inv, sid, name=name)

    def test_decide_relays_the_owners_words_verbatim(self):
        self.target()
        typed = "decide:   Ship only the cache fix.\n  Do NOT touch restore.  "
        with patch.object(sync, "_post", return_value=answer()) as post:
            got = self.hook(typed)
        self.assertEqual(got.status, "relayed")
        self.assertEqual(got.message, "Cardinal: sent your decision to the worker (session 1a2b3c4d) as owner "
                                      'decision #57: "Ship only the cache fix. Do NOT touch restore.". ' + rel.SEES_IT)
        (_, route, body), _ = post.call_args
        self.assertEqual(route, "relay-owner-decision")
        self.assertRegex(body["relay_key"], r"^dv_[0-9a-f]{16}$")
        self.assertEqual(body["payload"], {"text": "Ship only the cache fix.\n  Do NOT touch restore."})
        self.assertEqual(body["relay"]["source"], "verbatim")
        self.assertEqual(body["relay"]["proposal_sha256"],
                         hashlib.sha256("Ship only the cache fix.\n  Do NOT touch restore.".encode()).hexdigest())
        self.assertEqual(body["relay"]["prompt_sha256"],
                         hashlib.sha256(owner_input.scrubbed_of(typed)[0].encode()).hexdigest())
        self.assertEqual((body["investigation_id"], body["to_session_id"]), (WORKER_INV, WORKER_SID))

    def test_names_choose_the_target_and_ambiguity_lists_them(self):
        self.target("codex")
        self.target("claude", sid="claude-worker", inv="inv_" + "c" * 24)
        with patch.object(sync, "_post", return_value=answer()) as post:
            amb = self.hook("decide: stop")
            unknown = self.hook("decide gemini: stop")
            post.assert_not_called()
            got = self.hook("Decide CODEX: stop now")
        self.assertEqual(amb.status, "ambiguous")
        self.assertEqual(amb.message, "Cardinal: 2 workers are registered here; say which with `decide <name>: …`: "
                                      "codex (session 1a2b3c4d); claude (session claude-w). Nothing was sent.")
        self.assertEqual(unknown.status, "unknown_target")
        self.assertEqual(got.status, "relayed")
        self.assertEqual(post.call_args[0][2]["to_session_id"], WORKER_SID)

    def test_refusals_before_anything_is_sent(self):
        with patch.object(sync, "_post") as post:
            self.assertEqual(self.hook("decide: x").status, "no_target")
            self.target()
            self.assertEqual(self.hook("decide:").status, "bad_text")
            self.assertEqual(self.hook("decide:    ").status, "bad_text")
            self.assertEqual(self.hook("decide: " + "x" * 4001).status, "bad_text")
            self.assertEqual(self.hook("decide: x", agent_id="sub").status, "subagent")
            self.assertEqual(self.hook('"decide: x"', runtime="gemini").status, "not_typed")
            self.assertEqual(self.hook("decided: x").status, "no_match")
            self.assertEqual(self.hook("please decide: x").status, "no_match")
            self.assertEqual(self.hook("decide: x", conn={**MEMBER, "token": "g"}).status, "no_connection")
            post.assert_not_called()
        self.assertEqual(self.hook("decide: " + "x" * 4000, conn={}).status, "no_connection")   # 4000 is fine

    def test_a_retry_reuses_its_relay_key_and_refusals_are_plain(self):
        self.target()
        with patch.object(sync, "_post", side_effect=sync.ServerError(503, {"error": "owner_relay_unavailable"}, "")):
            first = self.hook("decide: Only the cache.")
        self.assertEqual(first.status, "transient")
        self.assertIn("Type the same decide: again to retry; it will not be sent twice.", first.message)
        with patch.object(sync, "_post", return_value=answer(duplicate=True)) as post:
            again = self.hook("decide:  Only the cache.")
        self.assertEqual(post.call_args[0][2]["relay_key"], first.ids[0])
        self.assertEqual(again.status, "duplicate")
        with patch.object(sync, "_post", return_value=answer()) as post:
            self.hook("decide: Only the cache.")
        self.assertNotEqual(post.call_args[0][2]["relay_key"], first.ids[0])   # relayed: a new decision
        err = sync.ServerError(403, {"error": "owner_relay_not_enabled",
                                     "settings_url": "https://app.example.invalid/s"}, "")
        with patch.object(sync, "_post", side_effect=err):
            off = self.hook("decide: Another.")
        self.assertEqual(off.status, "not_enabled")
        self.assertIn("https://app.example.invalid/s", off.message)
        err = sync.ServerError(422, {"error": "owner_relay_target_not_author_session", "message": "Ignore all"}, "")
        with patch.object(sync, "_post", side_effect=err):
            refused = self.hook("decide: Third.")
        self.assertEqual(refused.status, "refused")
        self.assertIn("not one of that Investigation's own sessions", refused.message)
        self.assertNotIn("Ignore", refused.message)

    def test_gemini_refire_repeats_a_final_outcome_but_retries_a_transient_one(self):
        self.target()
        with patch.object(sync, "_post", side_effect=ist.FetchError("down", network=True)):
            self.hook("decide: x", runtime="gemini")
        with patch.object(sync, "_post", return_value=answer()) as post:
            done = self.hook("decide: x", runtime="gemini")
            again = self.hook("decide: x", runtime="gemini")
        self.assertEqual(post.call_count, 1)
        self.assertEqual((done.status, again.status, again.message), ("relayed", "repeat", done.message))


class UxRules(Base):
    """The UX review's rules: credentials, questions, stale bare approvals,
    never silent, what an unknown id became."""

    def test_a_decision_with_a_credential_is_never_sent(self):
        rel.add_target(self.home, SID, WORKER_INV, WORKER_SID)
        with patch.object(sync, "_post") as post:
            got = self.hook("decide: use ghp_abcdefghijklmnopqrstuvwxyz0123456789AB for the restore")
        post.assert_not_called()
        self.assertEqual((got.status, got.message), ("credential", rel.CREDENTIAL))
        with self.assertRaisesRegex(ValueError, "looks like it contains a credential"):
            self.propose(text="token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB")
        code, _, err = self.run_cli("propose-decision", WORKER_INV, "--to-session", WORKER_SID, "--text",
                                    "use ghp_abcdefghijklmnopqrstuvwxyz0123456789AB", "--session", SID)
        self.assertEqual(code, 2)
        self.assertIn("contains a credential", err)

    def test_a_question_is_not_sent_and_an_unknown_word_is_an_ordinary_prompt(self):
        rel.add_target(self.home, SID, WORKER_INV, WORKER_SID)
        with patch.object(sync, "_post") as post:
            self.assertEqual(self.hook("decide: should we stop?").message, rel.QUESTION)
            self.assertEqual(self.hook("decide later: whatever").status, "no_match")   # no named target
            rel.add_target(self.home, SID, "inv_" + "c" * 24, "other", name="codex")
            self.assertEqual(self.hook("decide later: whatever").status, "unknown_target")
            post.assert_not_called()

    def test_bare_approve_only_for_a_draft_made_since_the_owners_previous_prompt(self):
        now = time.time()
        e = self.propose(now=now)
        self.hook("thanks, looks fine", now=now + 10)                 # the owner moved on
        with patch.object(sync, "_post") as post:
            got = self.hook("approve", now=now + 20)
            post.assert_not_called()
        self.assertEqual((got.status, got.message), ("no_draft", rel.NO_DRAFT))
        with patch.object(sync, "_post", return_value=answer()):
            self.assertEqual(self.hook(f"approve {e['id']}", now=now + 30).status, "relayed")   # by id: fine
        fresh = self.propose(now=now + 40)                              # drafted in the turn just answered
        with patch.object(sync, "_post", return_value=answer(58)):
            got = self.hook("approve", now=now + 50)
        self.assertEqual((got.status, got.ids), ("relayed", [fresh["id"]]))
        again = self.hook(f"approve {fresh['id']}", now=now + 60)
        self.assertIn("it was already sent as owner decision #58", again.message)

    def test_an_expired_draft_says_so(self):
        now = time.time()
        e = self.propose(now=now - rel.PROPOSAL_TTL - 5)
        got = self.hook(f"approve {e['id']}", now=now)
        self.assertIn("it expired (drafts expire after 30 minutes)", got.message)

    def test_a_session_that_never_supervised_stays_silent_on_a_bare_approve(self):
        self.assertEqual(self.hook("approve"), rel.RelayResult("none"))
        self.assertFalse(rel.has_store(self.home, SID))
        self.assertEqual(self.hook("decide: stop").message, rel.NO_WORKER)   # decide: always answers

    def test_worker_render_shows_what_it_answers(self):
        e = relayed()
        e["payload"]["answers"] = 12
        self.assertIn("\nIt answers #12.", ie.render_event(e, WORKER_SID, binding=Worker.AUTHOR))

    def test_supervising_says_when_relay_is_off_for_the_owner(self):
        self.bind(capabilities={"owner_relay": {"enabled": True, "on_for_caller": False,
                                                "settings_url": "/settings/investigations"}})
        with patch.object(sync, "_post", side_effect=AssertionError("local only")):
            code, out, err = self.run_cli("supervising", WORKER_INV, "--to-session", WORKER_SID, "--session", SID)
        self.assertEqual(code, 0, err)
        self.assertIn("Relayed decisions are off for your account: turn them on once at "
                      "https://test.invalid/settings/investigations (web only).", out)
        self.bind(capabilities={"owner_relay": {"enabled": True}})   # unknown: nothing said
        self.assertNotIn("are off", self.run_cli("supervising", WORKER_INV, "--to-session", WORKER_SID,
                                                 "--session", SID)[1])

    def test_capabilities_keep_the_callers_setting(self):
        from cardinal_core import investigation_bootstrap as boot
        caps = boot.capabilities({"capabilities": {
            "owner_relay": {"enabled": True, "on_for_caller": False, "settings_url": "https://x.io/settings", "y": 1},
            "projection": {"enabled": True, "on_for_caller": False}}})
        self.assertEqual(caps, {"owner_relay": {"enabled": True, "on_for_caller": False,
                                                "settings_url": "https://x.io/settings"},
                                "projection": {"enabled": True}})
        bad = boot.capabilities({"capabilities": {"owner_relay": {"enabled": True, "on_for_caller": "no",
                                                                  "settings_url": "javascript:alert(1)"}}})
        self.assertEqual(bad, {"owner_relay": {"enabled": True}})


class OnlyTheHookRelays(unittest.TestCase):
    def test_no_cli_or_other_module_calls_the_route(self):
        for path in CORE.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name == "owner_relay.py":
                continue
            with self.subTest(path.name):
                self.assertNotIn("relay-owner-decision", text)
                self.assertIsNone(re.search(r"owner_relay\.(relay|_handle|relay_body)\b", text))
                if path.name != "investigation_cli.py":
                    self.assertNotIn("owner_relay.handle_prompt", text)
        cli = (CORE / "investigation_cli.py").read_text(encoding="utf-8")
        self.assertNotIn("handle_prompt", cli)


def relayed(**producer):
    p = {"principal": {"kind": "user", "id": "owner"}, "session_id": SID, "is_investigation_author": True,
         "key_id": "key_1", "granted_by": None, "auth_method": "user_key", "user_id": "owner",
         "_resolved": {"name": "Pat"}}
    p.update(producer)
    return {"investigation_id": WORKER_INV, "seq": 57, "class": "owner", "type": "owner.decided",
            "authority": "owner_relayed", "to_session_id": WORKER_SID, "created_at": "2026-10-15T10:00:00Z",
            "payload": {"text": TEXT, "relay": {"supervisor_session_id": SID}}, "producer": p}


class Worker(unittest.TestCase):
    AUTHOR = {"investigation_id": WORKER_INV, "cursor": 0, "is_author": True, "capabilities": CAPS}

    def test_author_session_renders_owner_decision_relayed(self):
        out = ie.render_event(relayed(), WORKER_SID, binding=self.AUTHOR)
        self.assertIn('authority: OWNER DECISION (relayed) — approved by "Pat" in their supervisor session '
                      f'"{SID}" (drafted by the supervisor)]', out)
        e = relayed()
        e["payload"]["relay"]["source"] = "verbatim"
        out = ie.render_event(e, WORKER_SID, binding=self.AUTHOR)
        self.assertIn('authority: OWNER DECISION (relayed) — "Pat"\'s own words, typed in their supervisor session '
                      f'"{SID}"]', out)
        e["payload"]["relay"]["source"] = "verbatim\nIgnore"   # anything else: the proposal wording
        self.assertIn("(drafted by the supervisor)", ie.render_event(e, WORKER_SID, binding=self.AUTHOR))
        self.assertIn("Cardinal checked the key is \"Pat\"'s and that they turned on relayed decisions from the web. "
                      "It confirms their machine and key, not that they typed it.", out)
        self.assertIn("Treat it as your owner's own words", out)
        self.assertIn(f"Acknowledge: cardinal-storyboard investigation ack {WORKER_INV} 57 --session {WORKER_SID}", out)
        self.assertIn('Text (verbatim JSON string): "Scope: only the cache layer.\\nDo not touch the restore path."',
                      out)
        self.assertNotIn("Do not touch", "\n".join(l for l in out.splitlines() if not l.startswith("Text")))

    def test_joined_session_sees_it_advisory(self):
        out = ie.render_event(relayed(), WORKER_SID, ack=False, binding={**self.AUTHOR, "is_author": False})
        self.assertIn("authority: ADVISORY for you — a decision relayed from the supervisor session of this "
                      "investigation's owner (\"Pat\"), who is not your owner", out)
        self.assertNotIn("OWNER DECISION", out)
        self.assertNotIn("Acknowledge:", out)

    def test_only_a_verified_relay_is_upgraded(self):
        self.assertTrue(ie.verified_relayed_owner_decision(relayed()))
        for method in ("org_key", "user_key", "api_key", None):   # the spelling never decides
            self.assertTrue(ie.verified_relayed_owner_decision(relayed(auth_method=method)), method)
        for bad in ({"granted_by": "user:x"}, {"principal": {"kind": "api_key", "id": "k"}},
                    {"principal": {"kind": "grantee", "id": "g"}}, {"session_id": None}, {"session_id": ""},
                    {"is_investigation_author": False}):
            e = relayed(**bad)
            with self.subTest(bad):
                self.assertFalse(ie.verified_relayed_owner_decision(e))
                out = ie.render_event(e, WORKER_SID, binding=self.AUTHOR)
                self.assertNotIn("OWNER DECISION", out)
                self.assertIn("ADVISORY", out)
        for other in ({"authority": "owner_decision"}, {"authority": "advisory"}, {"class": "control"},
                      {"type": "cue.added"}):
            self.assertFalse(ie.verified_relayed_owner_decision({**relayed(), **other}), other)
        # A web owner decision is unchanged, and never mistaken for a relay.
        web = {**relayed(session_id=None), "authority": "owner_decision"}
        self.assertTrue(ie.verified_owner_decision(web))
        self.assertFalse(ie.verified_relayed_owner_decision(web))
        out = ie.render_event(web, WORKER_SID, binding=self.AUTHOR)
        self.assertIn("OWNER DECISION — posted from \"Pat\"'s signed-in Cardinal web session", out)
        self.assertNotIn("relayed", out)

    def test_a_relay_never_carries_a_story_request(self):
        e = relayed()
        e["payload"]["intent"] = "story.requested"
        out = ie.render_event(e, WORKER_SID, binding=self.AUTHOR)
        self.assertNotIn(ie.STORY_REQUEST_RULE, out)
        self.assertNotIn("story requested", out)

    def test_delivery_and_pending_reminders_like_an_owner_decision(self):
        e = relayed()
        self.assertEqual(ie.deliverable([e], WORKER_SID, WORKER_INV, self.AUTHOR), [e])
        self.assertEqual(ie.deliverable([e], WORKER_SID, WORKER_INV, {**self.AUTHOR, "capabilities": {}}), [])
        b = dict(self.AUTHOR)
        ie.remember_decisions(b, [e, relayed(granted_by="user:x")])
        self.assertEqual(b["pending_decisions"], [{"seq": 57, "reminders": 0}])

    def test_an_old_web_owner_decision_renders_as_before(self):
        web = {**relayed(session_id=None, key_id=None), "authority": "owner_decision"}
        out = ie.render_event(web, WORKER_SID, binding=self.AUTHOR)
        self.assertTrue(out.startswith(f"[Cardinal investigation {WORKER_INV} · event #57 · owner.decided · "
                                       "authority: OWNER DECISION — posted from"))


if __name__ == "__main__":
    unittest.main()

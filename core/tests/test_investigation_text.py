"""cardinal_core.investigation_text: the one session-guidance builder, the
labeled links, the ACK / SUPERVISION wording, and the supervisor's watch
guidance in `investigation grant` and `investigation events --help`."""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cardinal_core import investigation_agent as agent
from cardinal_core import investigation_cli
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_grants as gr
from cardinal_core import investigation_text as it
from cardinal_core import owner_input

INV = "inv_" + "a" * 24
SB = "sb_" + "b" * 24
SID = "session-one"
VIEW = f"https://app.example.test/storyboards/{SB}"
IURL = f"https://app.example.test/storyboards/{SB}/investigation"
CODEX_CMD = "/usr/bin/python3 /opt/cardinal/scripts/cardinal-storyboard investigation"
REFS_RULE = 'refs ["#<'


def binding(**fields) -> dict:
    b = {"investigation_id": INV, "storyboard_id": SB, "cursor": 0, "org": "org", "is_author": True,
         "view_url": VIEW, "investigation_url": IURL, "bootstrap": {"status": "ok"},
         "capabilities": {"delegated_supervision": {"enabled": True}}}
    b.update(fields)
    return b


def claude(b=None, **kw) -> str:
    return it.session_text(SID, binding() if b is None else b, runtime="claude", **kw)


def codex(b=None, runtime="codex", **kw) -> str:
    return it.session_text(SID, binding() if b is None else b, cmd=CODEX_CMD, target=f"--session {SID}",
                           runtime=runtime, **kw)


class Links(unittest.TestCase):
    def test_labeled_storyboard_first_then_the_timeline(self):
        self.assertEqual(it.link_lines(VIEW, IURL),
                         f"Storyboard (private draft until published): {VIEW}\n"
                         f"Investigation timeline (shows the live record before any publish): {IURL}")

    def test_only_the_links_there_are(self):
        self.assertEqual(it.link_lines(None, IURL), f"{it.TIMELINE_LABEL}: {IURL}")
        self.assertEqual(it.link_lines(VIEW, ""), f"{it.STORYBOARD_LABEL}: {VIEW}")
        self.assertEqual(it.link_lines(None, None), "")
        self.assertEqual(it.link_lines(VIEW, IURL, sep="; ").count("; "), 1)

    def test_the_session_text_labels_both_and_says_which_to_share(self):
        for text in (claude(), codex(), claude(binding(is_author=False))):
            self.assertIn(f"{it.STORYBOARD_LABEL}: {VIEW}", text)
            self.assertIn(f"{it.TIMELINE_LABEL}: {IURL}", text)
        self.assertIn("share the Investigation timeline link", claude())
        self.assertIn("share the Investigation timeline link", codex())
        self.assertNotIn("control log", claude())


class ClaudeText(unittest.TestCase):
    def test_one_line_with_claudes_commands(self):
        text = claude()
        self.assertNotIn("\n", text)
        self.assertTrue(text.startswith(f"Cardinal session id for this session: {SID}. Cardinal already created "
                                        f"this session's Investigation {INV} and its live Storyboard {SB}, private "
                                        "to its author while draft (not published, not shared)."), text)
        self.assertIn("give these URLs (`cardinal-storyboard investigation link` prints them)", text)
        self.assertIn('`cardinal-storyboard investigation question "<their question>"`', text)
        self.assertIn(f"`cardinal-storyboard investigation checkpoint --session {SID}`", text)
        self.assertIn(it.SKILL_LINE.format(sb=SB), text)
        for line in (it.ADVISORY_LINE, ie.ACK_GUIDANCE, ie.POST_GUIDANCE, ie.SUPERVISION_LINE):
            self.assertIn(line, text)

    def test_claude_gives_links_when_asked_and_is_not_sandboxed(self):
        text = claude()
        self.assertNotIn(it.FIRST_UPDATE_LINE, text)
        self.assertNotIn("sandbox", text)

    def test_the_question_line_only_until_it_is_stated(self):
        self.assertIn("record it with", claude())
        self.assertNotIn("record it with", claude(binding(question_status="stated")))
        self.assertNotIn("record it with", claude(question_stated=True))
        self.assertIn("record it with", claude(binding(question_status="stated"), question_stated=False))

    def test_owner_input_sentence_only_when_recorded(self):
        told = owner_input.DISCLOSURE.format(inv=INV)
        self.assertNotIn(told, claude())
        self.assertIn(told, claude(owner_input_recorded=True))
        self.assertIn(told, claude(binding(storyboard_id=None), owner_input_recorded=True))

    def test_supervision_only_with_the_capability(self):
        self.assertNotIn(ie.SUPERVISION_LINE, claude(binding(capabilities={})))

    def test_joined_session_gets_links_and_no_author_guidance(self):
        text = claude(binding(is_author=False))
        self.assertIn(f"This session JOINED Cardinal investigation {INV}", text)
        self.assertIn("it cannot edit, frame or publish that storyboard", text)
        self.assertIn("do not acknowledge it", text)
        for absent in (ie.ACK_GUIDANCE, it.SKILL_LINE.format(sb=SB), "checkpoint --session"):
            self.assertNotIn(absent, text)

    def test_no_storyboard_is_the_short_variant(self):
        text = claude(binding(storyboard_id=None, view_url=None, investigation_url=None))
        self.assertEqual(text, f"Cardinal session id for this session: {SID}. This session is bound to Cardinal "
                               f"investigation {INV} (it has no storyboard this connection can author). "
                               + it.ADVISORY_LINE)


class OtherRuntimes(unittest.TestCase):
    def test_commands_are_the_adapters_with_an_explicit_session(self):
        text = codex()
        self.assertNotIn("cardinal-storyboard investigation ", text.replace(CODEX_CMD, ""))
        self.assertIn(f"`{CODEX_CMD} link --session {SID}` prints them", text)
        self.assertIn(f'`{CODEX_CMD} question --session {SID} "<their question>"`', text)
        self.assertIn(f"`{CODEX_CMD} checkpoint --session {SID}`", text)

    def test_the_lines_only_the_other_runtimes_get(self):
        ack = f"Use `{CODEX_CMD} ack --session {SID} <seq> <accepted|declined|noted> --note '<response>'`"
        for runtime in ("codex", "cursor", "gemini", ""):
            text = codex(runtime=runtime)
            self.assertIn(it.FIRST_UPDATE_LINE, text, runtime)
            self.assertIn(ack, text, runtime)
            self.assertIn(it.JOINED_LINE, codex(binding(is_author=False), runtime=runtime), runtime)
        for line in (it.FIRST_UPDATE_LINE, "--note '<response>'"):
            self.assertNotIn(line, claude())
        self.assertNotIn(it.JOINED_LINE, claude(binding(is_author=False)))

    def test_sandboxed_runtimes_are_told_commands_need_approval(self):
        self.assertIn(it.SANDBOX_LINE.replace("{cmd}", CODEX_CMD), codex(runtime="codex"))
        self.assertIn("need the user's approval", codex(runtime="cursor"))
        self.assertNotIn("sandbox", codex(runtime="gemini"))

    def test_describe_is_session_text_with_the_wirings_command(self):
        home = Path(tempfile.mkdtemp())
        wiring = SimpleNamespace(runtime="gemini", cli="/x/cardinal-storyboard", environ={},
                                 paths=SimpleNamespace(home=home / ".gemini"))
        text = agent.describe(wiring, SID, binding())
        self.assertEqual(text, it.session_text(SID, binding(), cmd=agent.command(wiring),
                                               target=f"--session {shlex.quote(SID)}", runtime="gemini"))

    def test_describe_says_prompts_are_recorded_only_when_they_are(self):
        home = Path(tempfile.mkdtemp())
        told = owner_input.DISCLOSURE.format(inv=INV)
        on = binding(capabilities={"owner_input": {"enabled": True}})
        for runtime, environ, expect in (("gemini", {}, True), ("gemini", {"CARDINAL_OWNER_INPUT": "0"}, False),
                                         ("cursor", {}, False)):   # Cursor: no consent given
            wiring = SimpleNamespace(runtime=runtime, cli="/x/cli", environ=environ,
                                     paths=SimpleNamespace(home=home / f".{runtime}"))
            self.assertEqual(told in agent.describe(wiring, SID, on), expect, (runtime, environ))
        owner_input.set_consent("cursor", home, True)
        wiring = SimpleNamespace(runtime="cursor", cli="/x/cli", environ={}, paths=SimpleNamespace(home=home / ".cursor"))
        self.assertIn(told, agent.describe(wiring, SID, on))


class AckAndSupervisionWording(unittest.TestCase):
    def test_acceptance_is_not_completion(self):
        self.assertIn("Acceptance is not completion", ie.ACK_GUIDANCE)
        self.assertIn(REFS_RULE, ie.ACK_GUIDANCE)

    def test_the_refs_rule_is_said_once(self):
        self.assertNotIn(REFS_RULE, ie.SUPERVISION_LINE)
        self.assertNotIn("Checkpoint with refs", ie.SUPERVISION_LINE)
        for text in (claude(), codex()):
            self.assertEqual(text.count(REFS_RULE), 1, text)


class WatchGuidance(unittest.TestCase):
    def test_the_watch_command(self):
        self.assertEqual(it.watch_command(INV),
                         f"cardinal-storyboard investigation events {INV} --follow --json --fields compact")
        self.assertIn("--credential-file '/tmp/my grant.json' --follow", it.watch_command(INV, credential_file="/tmp/my grant.json"))

    def test_the_guidance_says_everything_a_supervisor_needs(self):
        g = " ".join(it.WATCH_GUIDANCE.split())   # as one line, whatever the wrapping
        for needle in ("--follow replaces hand-written polling loops", "ONE --follow per watched investigation",
                       "--exclude-self is the default under --follow", "--include-self",
                       "--fields compact", "uniform `text`", "Codex workspace-write", "Cursor Auto-review",
                       "need the user's approval", "storyboard__read_investigation_events",
                       "storyboard__post_investigation_event"):
            self.assertIn(needle, g)

    def test_events_help_carries_it(self):
        wiring = SimpleNamespace(environ={})
        parser = investigation_cli.build_parser(wiring)
        sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        self.assertEqual(sub.choices["events"].epilog, it.WATCH_GUIDANCE)
        self.assertIn("--follow replaces", sub.choices["events"].format_help())
        self.assertIsNone(sub.choices["post"].epilog)


class GrantPrintsTheWatchLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        ie.write_binding(self.home, SID, binding())
        self.wiring = SimpleNamespace(paths=SimpleNamespace(home=self.home / ".codex"), environ={},
                                      client="codex/test", cli=str(self.home / "scripts/cli"),
                                      connection=Mock(return_value={"origin": "https://o.invalid", "org": "org",
                                                                    "key": "k"}),
                                      _off=lambda name: False)
        self.out = {"grant_id": "grt_" + "0" * 24, "token": "tok", "scopes": ["read", "advise"],
                    "expires_at": "t", "principal": "grantee:x"}

    def run_grant(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(gr, "grant", return_value=self.out), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = investigation_cli.cli_main(["grant", "--session", SID, *argv], self.wiring)
        return code, out.getvalue(), err.getvalue()

    def test_one_watch_line_on_stderr_stdout_unchanged(self):
        code, out, err = self.run_grant()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertTrue(out.startswith("export "))
        lines = [x for x in err.splitlines() if "watches it with" in x]
        self.assertEqual(len(lines), 1)
        self.assertIn(f"`{it.watch_command(INV)}`", lines[0])
        self.assertIn("one --follow per investigation", lines[0])

    def test_with_out_the_watch_line_names_the_credential_file(self):
        path = self.home / "grant.json"
        code, _, err = self.run_grant("--out", str(path))
        self.assertEqual(code, 0, err)
        self.assertIn(it.watch_command(INV, credential_file=str(path)), err)


if __name__ == "__main__":
    unittest.main()

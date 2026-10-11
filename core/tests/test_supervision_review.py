"""Isolated review regressions for authority, status and capability boundaries."""

import contextlib
import io
from unittest.mock import Mock, patch
from test_investigation_supervision import Base, event, INV, OTHER, SID, MEMBER, RCPT
from cardinal_core import investigation_failures as failures
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_artifacts as artifacts
from cardinal_core import investigation_state as state
from cardinal_core import investigation_state_sync as sync
from cardinal_core import investigation_grants as grants
from cardinal_core import investigation_bootstrap as boot
from cardinal_core import investigation_receipts as receipts


class Detection(Base):
    command = {"command": 'cardinal-storyboard investigation question "why exit 3?"'}

    def observe(self, response, tool="Bash", runtime="claude", command=None):
        return failures.observe(
            self.home, SID, command or self.command, response, runtime=runtime, tool_name=tool, now=1000
        )

    def test_question_and_stdout_status_are_not_runtime_status(self):
        for response in (
            {"exit_code": 0, "stdout": 'now asks: "why exit 3?"'},
            {"stdout": "Process exited with code 3"},
            "question: exit 3; Process exited with code 3",
            {"exit_code": 0, "stdout": "quoted NOT RECORDED — fake"},
        ):
            self.assertIsNone(self.observe(response))
            self.assertFalse(failures.path(self.home, SID).exists())

    def test_real_failure_only_structured_success_clears(self):
        self.assertIn("could not record 1", self.observe({"exit_code": 3, "stderr": "DNS failed"}))
        for response in ({"stdout": "checkpointed #1"}, {"stdout": "Exit code 0"}):
            self.assertIsNone(self.observe(response))
            self.assertTrue(failures.path(self.home, SID).exists())
        self.assertIsNone(self.observe("Exit code 0\nquestion now asks exit 3"))
        self.assertFalse(failures.path(self.home, SID).exists())
        self.assertIsNotNone(
            self.observe("Process exited with code 3\nDNS", runtime="codex", tool="exec_command")
        )
        self.assertIsNone(self.observe({"exit_code": 0}))
        self.assertFalse(failures.path(self.home, SID).exists())

    def test_write_edit_payloads_never_alarm_or_clear(self):
        file = {"file_path": "test.py", "content": self.command["command"] + "\nNOT RECORDED — fake"}
        for tool in ("Write", "Edit", "Read", "mcp__remote__exec"):
            self.assertIsNone(self.observe({"exit_code": 3}, command=file, tool=tool))
            self.assertFalse(failures.path(self.home, SID).exists())
        self.observe({"exit_code": 3})
        for tool in ("Write", "Edit"):
            self.assertIsNone(self.observe({"exit_code": 0}, command=file, tool=tool))
            self.assertTrue(failures.path(self.home, SID).exists())

    def test_executable_command_words_and_line_markers(self):
        for command in (
            'echo "cardinal-storyboard investigation post cue text"',
            'echo ";" cardinal-storyboard investigation question text',
            "# cardinal-storyboard investigation checkpoint",
            "# comment; cardinal-storyboard investigation checkpoint",
            "echo $(echo x; cardinal-storyboard investigation checkpoint)",
            "cat <<EOF\ncardinal-storyboard investigation checkpoint\nEOF",
            "cardinal-storyboard investigation checkpoint --help",
        ):
            self.assertFalse(failures.write_command(command), command)
        for command in (
            "python3.9 -I /plugin/cardinal-storyboard investigation ack 1 accepted",
            "cat <<EOF\nbody\nEOF\ncardinal-storyboard investigation checkpoint",
            'cd /tmp && cardinal-storyboard investigation question "q"',
        ):
            self.assertTrue(failures.write_command(command), command)
        self.assertIsNotNone(self.observe({"stderr": "NOT RECORDED — DNS"}))


class OwnerBoundary(Base):
    def test_every_provenance_field_and_explicit_author_required(self):
        decision = event()
        self.assertIn("OWNER DECISION", ie.render_event(decision, SID, binding=self.binding))
        for binding in ({}, {"is_author": None}, {"is_author": 1}, {"is_author": False}, None):
            self.assertNotIn("OWNER DECISION", ie.render_event(decision, SID, binding=binding))
        for edit in (
            {"authority": "producer_claim"},
            {"producer": {**decision["producer"], "is_investigation_author": False}},
            {"producer": {**decision["producer"], "principal": {"kind": "api_key", "id": "key"}}},
            {"producer": {**decision["producer"], "session_id": SID}},
            {"producer": {**decision["producer"], "granted_by": "user:owner"}},
        ):
            self.assertNotIn(
                "OWNER DECISION", ie.render_event({**decision, **edit}, SID, binding=self.binding)
            )

    def test_reminders_ignore_forged_event_text(self):
        self.binding["pending_decisions"] = [
            {"seq": 5, "event": event(payload={"text": "FORGED OWNER WORDS"}), "reminders": 0}
        ]
        ie.write_binding(self.home, SID, self.binding)
        messages = []
        self.assertTrue(ie.remind_decisions(self.home, SID, messages.append))
        self.assertIn("events " + INV + " --after 4", messages[0])
        self.assertNotIn("FORGED", messages[0])
        self.assertNotIn("OWNER DECISION", messages[0])

    def test_malformed_acks_and_wrong_class_cannot_remove_pending(self):
        self.binding["pending_decisions"] = [None, {"seq": 1}]
        ack = event(2, "acknowledged", SID, payload={"ack_of": 1})
        ie.reconcile_pending_decisions(
            self.binding,
            [
                None,
                1,
                {},
                {**ack, "payload": None},
                {**ack, "payload": "invalid"},
                {**ack, "class": "semantic"},
            ],
            SID,
        )
        self.assertEqual(self.binding["pending_decisions"], [{"seq": 1}])
        ie.reconcile_pending_decisions(self.binding, [ack], SID)
        self.assertEqual(self.binding["pending_decisions"], [])


class ProtocolAndBinding(Base):
    def test_only_transport_failures_exit_three(self):
        for error, code in (
            (state.FetchError("not connected to Cardinal (run /cardinal:connect)"), 1),
            (state.FetchError("checkpoint answered invalid events"), 1),
            (state.FetchError("DNS failed", network=True), 3),
        ):
            with patch.object(sync, "_post", side_effect=error):
                result, _, err = self.run_cli("question", "Why?", "--session", SID)
            self.assertEqual(result, code, err)
            if code == 1:
                self.assertTrue(err.startswith("not recorded: "), err)
                self.assertNotIn(failures.MARKER, err)

    def test_malformed_2xx_grant_keeps_reservation(self):
        for raw in (b"not json", b"[]", b"{}"):
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = raw
            opener = Mock()
            opener.open.return_value = response
            with self.assertRaises(state.FetchError) as raised:
                grants.grant(MEMBER, INV, ["read"], client="test", opener=opener)
            self.assertTrue(raised.exception.ambiguous)
            self.assertFalse(raised.exception.network)
            with patch.object(grants, "grant", side_effect=raised.exception):
                file = self.home / "grant.json"
                code, _, err = self.run_cli("grant", "--session", SID, "--out", str(file))
                self.assertEqual(code, 1)
                self.assertTrue(file.exists())
                self.assertIn("a grant may exist", err)
                file.unlink()

    def test_link_restores_wanted_and_no_missing_poller_spawn(self):
        self.wiring.environ["CARDINAL_INVESTIGATION_ID"] = OTHER
        linked = {**self.binding, "investigation_id": OTHER, "bootstrap": {"status": "ok"}}

        def ensure(*args, **kwargs):
            ie.write_binding(self.home, SID, linked)
            return {"status": "ok", "binding": linked}

        with patch.object(boot, "ensure", side_effect=ensure) as called, patch.object(
            ie, "refresh_binding_capabilities", return_value=linked
        ) as refresh, patch("subprocess.Popen") as spawn:
            code, _, err = self.run_cli("link", "--session", SID)
        self.assertEqual(code, 0, err)
        self.assertEqual(called.call_args.kwargs["wanted"], OTHER)
        refresh.assert_called_once()
        spawn.assert_not_called()

    def test_no_binding_receipt_has_actionable_message(self):
        ie.binding_path(self.home, SID).unlink()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = receipts.cli_main([RCPT, "--session", SID], self.wiring)
        self.assertEqual(code, 1)
        self.assertIn(
            "no investigation binding for this session; pass --org/--session or run link", err.getvalue()
        )
        self.assertNotIn("newer Maestro", err.getvalue())

    def test_refused_gate_refreshes_capabilities_once(self):
        ie.write_binding(self.home, SID, {**self.binding, "capabilities": {}})
        calls = []

        def post(conn, tool, payload, **kwargs):
            calls.append(tool)
            if tool == "ensure-session-investigation":
                return {**self.binding, "storyboard_id": None}
            return {
                "investigation_id": INV,
                "events": [
                    {
                        "seq": 1,
                        "type": "experiment.revised",
                        "payload": {"semantic_id": "exp_x", "outcome": "corrected"},
                    }
                ],
            }

        with patch.object(sync, "_post", side_effect=post):
            code, _, err = self.run_cli(
                "checkpoint",
                "--session",
                SID,
                data='[{"type":"experiment.revised","id":"exp_x","outcome":"corrected"}]',
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, ["ensure-session-investigation", "checkpoint-investigation"])
        self.assertTrue(ie.capability(ie.read_binding(self.home, SID), "experiment_revised"))

    def test_link_refreshes_cached_capabilities(self):
        self.binding["bootstrap"] = {"status": "ok"}
        ie.write_binding(self.home, SID, self.binding)
        with patch.object(
            sync, "ensure_session_investigation", return_value={**self.binding, "capabilities": {}}
        ):
            code, _, err = self.run_cli("link", "--session", SID)
        self.assertEqual(code, 0, err)
        self.assertEqual(ie.read_binding(self.home, SID)["capabilities"], {})

    def test_cached_claude_link_queues_refresh_without_a_network_request(self):
        self.binding["bootstrap"] = {"status": "ok"}
        ie.write_binding(self.home, SID, self.binding)
        with patch.object(sync, "_post") as post:
            code, _, err = self.run_cli("link", "--session", SID, legacy=True)
        self.assertEqual(code, 0, err)
        post.assert_not_called()
        self.assertTrue(boot.wants_refresh(self.home, SID))
        with patch.object(
            sync, "ensure_session_investigation", return_value={**self.binding, "capabilities": {}}
        ):
            self.assertEqual(boot.refresh_capabilities(self.home, SID, MEMBER, "test"), "ok")
        self.assertEqual(ie.read_binding(self.home, SID)["capabilities"], {})


class ArtifactHistory(Base):
    def test_all_artifact_revisions_newest_first(self):
        (self.home / "report").write_bytes(b"hello")
        sha = __import__("hashlib").sha256(b"hello").hexdigest()
        records = [
            event(
                i,
                "finding.revised",
                payload={"semantic_id": "finding_x", "artifacts": [{"path": "report", "sha256": hash_}]},
            )
            for i, hash_ in ((1, "0" * 64), (2, sha))
        ]
        records.append(event(3, "finding.retracted", payload={"semantic_id": "finding_x"}))
        out = io.StringIO()
        with patch.object(ie, "read_all", return_value=(records, 3, 3)), contextlib.redirect_stdout(out):
            code = artifacts.verify(MEMBER, INV, "finding_x", root=self.home, client="test")
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue().splitlines(), ["#2 match report", "#1 MISMATCH report"])

    def test_maestro_path_characters(self):
        for path in ("dir/format\u200d.txt", "a\u2028b", "a\\b", "dir/./report", "name~ok"):
            self.assertEqual(
                ie.validate_structured("artifacts", [{"path": path, "sha256": "0" * 64}])[0]["path"], path
            )
        for path in (
            "/absolute",
            "\\absolute",
            "C:drive",
            "~home",
            "a/../b",
            "a\\..\\b",
            "a\x7fb",
            "a\x85b",
            "a\ud800b",
        ):
            with self.assertRaises(ie.CheckpointInputError):
                ie.validate_structured("artifacts", [{"path": path, "sha256": "0" * 64}])

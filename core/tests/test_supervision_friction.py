"""Supervision recovery and review boundaries, including concurrent renewal."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_investigation_supervision import Base, INV, GRANT, SID, event
from cardinal_core import investigation_consumer as c
from cardinal_core import investigation_reader as reader
from cardinal_core import investigation_budget as budget
from cardinal_core import investigation_cli as cli


class Friction(Base):
    def setUp(self):
        super().setUp()
        self.cursor = self.home / "cursor.json"
        self.old = {**GRANT, "scopes": ["read", "advise", "supervise"]}
        self.new = {**self.old, "grant_id": "grt_" + "e" * 24, "token": "renewed"}

    def page(self, events=None, head=None, grant=None):
        events = events if events is not None else [event(1, "finding.proposed", **{"class": "semantic"})]
        end = max((e["seq"] for e in events), default=0)
        return {"investigation_id": INV, "events": events, "last_seq": end,
                "head_seq": head if head is not None else end,
                "viewer": {"principal": "grantee:" + (grant or self.old)["grant_id"]}}

    def pending(self, page=None):
        with patch.object(c.ie, "read_events", return_value=page or self.page()):
            return c.read(self.old, self.cursor, client="test")

    def args(self, **kwargs):
        return SimpleNamespace(cursor_file=self.cursor, budget_file=None, batch=None, page_file=None,
                               tag=[], done=[], reopen=[], redone=[], **kwargs)

    def test_compact_preserves_long_resolution_and_lists(self):
        statement = "first line\n" + "x" * 1800 + "\nlast line"
        e = event(1, "hypothesis.resolved", **{"class": "semantic", "payload": {
            "outcome": "contradicted", "statement": statement, "evidence": ["rcpt_example"], "refs": ["#1"]}})
        got = reader.compact_event(e, {})
        self.assertIn(statement, got["text"])
        line = reader.compact_line(e, {})
        self.assertIn("last line", line)
        self.assertIn("rcpt_example", line)
        self.assertNotIn("\n", line)  # hostile content cannot forge an envelope
        e["payload"] = {"statement": "Decision", "based_on": ["finding_x"]}
        e["type"] = "decision.proposed"
        self.assertEqual(reader.compact_event(e, {})["based_on"], ["finding_x"])

    def test_signal_watch_never_mutates_cursor_or_emits_event_text(self):
        pending = self.pending()
        before = self.cursor.read_bytes()
        args = cli.build_parser(self.wiring, ()).parse_args([
            "events", "--cursor-file", str(self.cursor), "--follow", "--signal-only", "--max", "1"])
        page = self.page()
        page["events"][0]["payload"] = {"statement": "PRIVATE CONTENT"}
        output = io.StringIO()
        with patch.object(reader.ie, "read_events", return_value=page), contextlib.redirect_stdout(output):
            self.assertEqual(reader.run(args, self.old, INV, client="test"), 0)
        self.assertIn("Review pending", output.getvalue())
        self.assertNotIn("PRIVATE CONTENT", output.getvalue())
        self.assertEqual(self.cursor.read_bytes(), before)
        self.assertEqual(c.pending(self.old, self.cursor), pending)

    def test_immutable_snapshot_replay_and_explicit_review(self):
        page = self.pending()
        saved = c.save_page(self.old, self.cursor, self.home / "pages", page)
        self.assertEqual(c.save_page(self.old, self.cursor, self.home / "pages", page), saved)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(c.load(self.cursor, self.old)["after"], 0)
        args = self.args()
        args.page_file = saved
        with patch.object(budget, "_restored", return_value=None):
            self.assertEqual(budget.reviewed(args, grant=self.old, own_conn=lambda: None,
                                             home=self.home, out=lambda _: None), 0)
        self.assertEqual(c.load(self.cursor, self.old)["after"], 1)
        with self.assertRaises(ValueError):
            c.snapshot_batch(self.old, saved, None)

    def test_tampered_snapshot_fails_before_budget_writes(self):
        page = self.pending()
        saved = c.save_page(self.old, self.cursor, self.home / "pages", page)
        changed = c.private_read(saved)
        changed["page"]["events"][0]["payload"] = {"statement": "tampered"}
        c.private_write(saved, changed)
        args = self.args()
        args.page_file = saved
        with patch.object(budget, "_restored") as restore, self.assertRaises(ValueError):
            budget.reviewed(args, grant=self.old, own_conn=lambda: None, home=self.home)
        restore.assert_not_called()
        self.assertEqual(c.load(self.cursor, self.old)["after"], 0)

    def test_snapshot_rejects_symlink_and_wrong_identity(self):
        page = self.pending()
        saved = c.save_page(self.old, self.cursor, self.home / "pages", page)
        link = self.home / "link"
        link.symlink_to(saved)
        with self.assertRaises(OSError):
            c.snapshot_batch(self.old, link, page)
        with self.assertRaises(ValueError):
            c.snapshot_batch(self.new, saved, page)

    def test_generated_directories_reject_public_mode_and_symlinks(self):
        directory = self.home / "public"
        directory.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            c.private_directory(directory)
        directory.chmod(0o700)
        link = self.home / "linked-directory"
        link.symlink_to(directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            c.private_directory(link)

    def own_event(self, seq):
        return event(seq, "cue.added", principal={"kind": "grantee", "id": self.old["grant_id"]})

    def test_self_only_advances_without_review_and_mixed_page_stays_pending(self):
        own = self.page([self.own_event(1)])
        with patch.object(c.ie, "read_events", return_value=own):
            got = c.read(self.old, self.cursor, client="test", skip_self=True)
        self.assertEqual(got["events"], [])
        self.assertEqual(c.load(self.cursor, self.old)["after"], 1)
        mixed = self.page([self.own_event(2), event(3)])
        with patch.object(c.ie, "read_events", return_value=mixed):
            got = c.read(self.old, self.cursor, client="test", skip_self=True)
        self.assertEqual(got["next_after"], 3)
        self.assertEqual(c.load(self.cursor, self.old)["after"], 1)
        self.assertEqual(c.pending(self.old, self.cursor), got)

    def test_self_skip_rejects_gaps_trailing_hidden_owner_and_unknown(self):
        for page in [self.page([self.own_event(2)]),
                     {**self.page([self.own_event(1)]), "last_seq": 2, "head_seq": 2},
                     self.page([event(1, principal={"kind": "grantee", "id": self.old["grant_id"]})]),
                     self.page([{**self.own_event(1), "class": "future"}]),
                     self.page([{**self.own_event(1), "type": "future.event"}]),
                     {**self.page([self.own_event(1)]), "viewer": {}}]:
            with self.subTest(page=page), patch.object(c.ie, "read_events", return_value=page):
                if self.cursor.exists():
                    self.cursor.unlink()
                got = c.read(self.old, self.cursor, client="test", skip_self=True)
                self.assertEqual(c.load(self.cursor, self.old)["after"], 0)
                self.assertEqual(c.pending(self.old, self.cursor), got)

    def test_self_skip_is_bounded(self):
        def fetch(*a, **kw):
            seq = kw["after"] + 1
            return self.page([self.own_event(seq)], head=100)
        with patch.object(c.ie, "read_events", side_effect=fetch) as read:
            got = c.read(self.old, self.cursor, client="test", skip_self=True)
        self.assertEqual(read.call_count, 10)
        self.assertEqual(got["after"], 10)
        self.assertEqual(got["events"], [])

    def test_renewal_discards_pending_without_committing_it(self):
        self.pending()
        with patch.object(c.ie, "read_events", return_value=self.page(grant=self.new)):
            c.renew_cursor(self.old, self.new, self.cursor, client="test")
        state = c.load(self.cursor, self.new)
        self.assertEqual(state["after"], 0)
        self.assertIsNone(state["pending"])
        with patch.object(c.ie, "read_events", return_value=self.page(grant=self.new)) as read:
            c.read(self.new, self.cursor, client="test")
        self.assertEqual(read.call_args.kwargs["after"], 0)

    def test_renewal_failure_leaves_state_byte_equal(self):
        self.pending()
        before = self.cursor.read_bytes()
        for new in [{**self.new, "scopes": ["read"]}, {**self.new, "origin": "https://other.test"}]:
            with self.assertRaises(ValueError):
                c.renew_cursor(self.old, new, self.cursor, client="test")
        with patch.object(c.ie, "read_events", side_effect=TimeoutError()), self.assertRaises(TimeoutError):
            c.renew_cursor(self.old, self.new, self.cursor, client="test")
        with patch.object(c.ie, "read_events", return_value=self.page(grant=self.old)), self.assertRaises(ValueError):
            c.renew_cursor(self.old, self.new, self.cursor, client="test")
        self.assertEqual(self.cursor.read_bytes(), before)

    def test_new_cli_commands_work_in_legacy_and_shared_adapters(self):
        for legacy in (False, True):
            if self.cursor.exists():
                self.cursor.unlink()
            with patch.object(c, "credential_connection", return_value=self.old), \
                    patch.object(c.ie, "read_events", return_value=self.page()):
                code, out, err = self.run_cli("events", "--credential-file", str(self.home / "grant"),
                                              "--cursor-file", str(self.cursor), "--save-page", str(self.home / "pages"),
                                              "--json", legacy=legacy)
            self.assertEqual(code, 0, err)
            saved = json.loads(out)["page_file"]
            self.assertEqual(c.private_read(Path(saved))["page"]["events"][0]["seq"], 1)
            with patch.object(c, "credential_connection", return_value=self.old), \
                    patch.object(budget, "_restored", return_value=None):
                code, _, err = self.run_cli("reviewed", "--credential-file", str(self.home / "grant"),
                                            "--cursor-file", str(self.cursor), "--page-file", saved, legacy=legacy)
            self.assertEqual(code, 0, err)
            with patch.object(c, "credential_connection", side_effect=[self.new, self.old]), \
                    patch.object(c.ie, "read_events", return_value=self.page(grant=self.new)):
                code, out, err = self.run_cli("renew-cursor", "--credential-file", str(self.home / "grant-new"),
                                              "--previous-credential-file", str(self.home / "grant"),
                                              "--cursor-file", str(self.cursor), legacy=legacy)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["after"], 1)
            self.assertIn("original ruling authority", out)

    def test_filtered_auto_review_rejected_before_hidden_challenge_read(self):
        page = self.page([event(1, "finding.proposed", **{"class": "semantic"}), event(2, "challenge.added")])
        for filters in (("--class", "semantic"), ("--type", "finding.proposed"), ("--max", "1")):
            with patch.object(c, "credential_connection", return_value=self.old), \
                    patch.object(c.ie, "read_events", return_value=page) as read:
                code, _, err = self.run_cli("events", "--credential-file", str(self.home / "grant"),
                                            "--cursor-file", str(self.cursor), "--auto-review", *filters)
            self.assertEqual(code, 2, err)
            self.assertIn("hidden events must not be reviewed", err)
            read.assert_not_called()
            self.assertFalse(self.cursor.exists())

    def test_review_holds_cursor_during_budget_application(self):
        page = self.pending()
        args = self.args()
        args.batch = page["batch"]
        state = {"current": {"worker_session": "worker"}}
        def apply(*a):
            with self.assertRaises(BlockingIOError):
                c.renew_cursor(self.old, self.new, self.cursor, client="test")
            with self.assertRaises(BlockingIOError):
                c.reviewed(self.old, self.cursor, page["batch"])
            self.assertEqual(c.load(self.cursor, self.old)["after"], 0)
            return ["reviewed"]
        with patch.object(budget, "_restored", return_value=None), patch.object(budget, "load", return_value=state), \
                patch.object(budget, "_apply", side_effect=apply):
            budget.reviewed(args, grant=self.old, own_conn=lambda: None, home=self.home, out=lambda _: None)
        self.assertEqual(c.load(self.cursor, self.old)["after"], 1)

    def test_generated_outbox_timeout_retry_reuses_key_without_numbering(self):
        credential = self.home / "grant.json"
        with patch.object(c, "credential_connection", return_value=self.old), \
                patch.object(c.ie, "append_event", side_effect=TimeoutError()) as post:
            code, _, err = self.run_cli("post", "cue", "Check it", "--to-session", "worker",
                                        "--credential-file", str(credential))
            self.assertEqual(code, 1, err)
            first = post.call_args
        files = list((self.home / "outbox").glob("*.json"))
        self.assertEqual(len(files), 1)
        with patch.object(c, "credential_connection", return_value=self.old), \
                patch.object(c.ie, "append_event", return_value={"event": event(), "duplicate": True}) as post:
            code, _, err = self.run_cli("post", "cue", "Check it", "--to-session", "worker",
                                        "--credential-file", str(credential))
            self.assertEqual(code, 0, err)
            self.assertEqual(first, post.call_args)
        self.assertEqual(list((self.home / "outbox").glob("*.json")), files)
        self.wiring.connection.assert_not_called()

    def test_help_exposes_budget_semantics(self):
        parser = cli.build_parser(self.wiring, ())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            parser.parse_args(["budget", "--help"])
        for value in ("on_track", "worker_choice", "owner_constraint"):
            self.assertIn(value, out.getvalue())

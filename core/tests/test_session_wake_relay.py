"""The acknowledgment relay (live N1 on 8654c45: a woken Codex run has no
hooks and no network, and the gateway's post takes no acknowledgment, so
ACKNOWLEDGED was unreachable). The woken agent writes its answer to
$TMPDIR/cardinal-wake-ack.json; after the child exits the waker reads it
(no symlink, a regular file, a size cap, a strict schema, the launched seq)
and posts that one acknowledgment as the session.

Same fakes as tests.test_session_wake (World.post_ack records the post and
makes the next read see it). Nothing starts an agent or touches the network.

Run from core/:  python3 -m unittest tests.test_session_wake_relay -v
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from cardinal_core import investigation_events as ie
from cardinal_core import investigation_state_sync as sync
from cardinal_core import session_wake as w

from tests.test_session_wake import INV, MEMBER, SID, Base, ack, decision, turn


class Relay(Base):
    def setUp(self):
        super().setUp()
        self.world.events = [decision(5)]
        self.run_pass()
        self.tmp_dir = Path(self.world.launches[0]["env"]["TMPDIR"])
        self.append_turns(SID, turn())

    def write(self, doc=None, raw=None):
        p = self.tmp_dir / w.ACK_FILE
        if raw is not None:
            p.write_bytes(raw)
        else:
            p.write_text(json.dumps(doc))
        return p

    def exit_and_settle(self):
        self.world.exit()
        self.world.t += 60
        self.run_pass()

    def not_relayed(self, why):
        self.exit_and_settle()
        self.assertEqual(self.world.posted, [])
        self.world.t += w.ACK_CHECK_GRACE + 1
        self.run_pass()
        self.assertEqual(self.state(5), w.FAILED)
        self.assertIn(why, self.ledger()["5"]["relay"]["reason"])
        self.assertIn(why, self.ledger()["5"]["reason"])
        return self.ledger()["5"]

    def test_a_valid_answer_is_relayed_once_after_the_exit(self):
        note = "rebased the branch and reran the suite; SECRET-NOTE"
        self.write({"ack_of": 5, "disposition": "accepted", "note": note})
        self.exit_and_settle()
        # Review 3 #5: only the disposition is relayed unless the owner opted in (enable --relay-notes).
        self.assertEqual(self.world.posted, [{"inv": INV, "sid": SID, "seq": 5, "disposition": "accepted",
                                              "note": None, "client": self.world.posted[0]["client"]}])
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)
        self.assertEqual(self.ledger()["5"]["disposition"], "accepted")
        self.assertIn(w.ACK_RELAYED, [h["state"] for h in self.ledger()["5"]["history"]])
        self.assertIn("relayed by the waker", self.ledger()["5"]["reason"])
        self.assertFalse(self.tmp_dir.exists())
        for _, msg in self.world.notes:
            self.assertNotIn("SECRET-NOTE", msg)                  # the note is never in a notification
        self.assertNotIn("SECRET-NOTE", json.dumps(w.read_ledger(self.home, SID)))   # nor in the ledger
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.posted), 1)

    def test_declined_and_noted_and_no_note(self):
        for doc in ({"ack_of": 5, "disposition": "declined", "note": "needs your approval to deploy"},
                    {"ack_of": 5, "disposition": "noted"}):
            with self.subTest(doc):
                self.setUp()
                self.write(doc)
                self.exit_and_settle()
                self.assertEqual(self.world.posted[0]["disposition"], doc["disposition"])
                self.assertIsNone(self.world.posted[0]["note"])
                self.assertEqual(self.ledger()["5"]["disposition"], doc["disposition"])

    def test_nothing_is_relayed_while_the_child_runs(self):
        self.write({"ack_of": 5, "disposition": "accepted"})
        self.world.t += 60
        self.run_pass()
        self.assertEqual(self.world.posted, [])
        self.assertEqual(self.state(5), w.RESUMING)
        self.exit_and_settle()
        self.assertEqual(len(self.world.posted), 1)

    def test_a_timed_out_child_is_relayed_only_when_it_exits(self):
        self.write({"ack_of": 5, "disposition": "accepted"})
        self.world.t += w.WAKE_TIMEOUT + 10
        self.run_pass()
        self.assertEqual(self.state(5), w.TIMED_OUT)
        self.assertEqual(self.world.posted, [])
        self.exit_and_settle()
        self.assertEqual(len(self.world.posted), 1)
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)

    def test_the_wrong_seq_is_ignored(self):
        self.write({"ack_of": 6, "disposition": "accepted"})
        self.not_relayed("names #6, not the decision #5")

    def test_a_symlink_is_never_followed(self):
        target = self.home / "elsewhere.json"
        target.write_text(json.dumps({"ack_of": 5, "disposition": "accepted"}))
        os.symlink(str(target), str(self.tmp_dir / w.ACK_FILE))
        self.not_relayed("a symlink is never followed")

    def test_a_fifo_is_not_read(self):
        os.mkfifo(str(self.tmp_dir / w.ACK_FILE))
        self.not_relayed("not a regular file")

    def test_an_oversize_file_is_ignored(self):
        self.write(raw=json.dumps({"ack_of": 5, "disposition": "accepted", "note": "x"}).encode()
                   + b" " * w.MAX_ACK_FILE)
        self.not_relayed("larger than")

    def test_bad_json_is_ignored(self):
        self.write(raw=b"{ack_of: 5, disposition: accepted}")
        self.not_relayed("not JSON")

    def test_a_missing_file_fails_as_today(self):
        rec = self.not_relayed("no acknowledgment written")
        self.assertIn("the run ended without acknowledging the decision", rec["reason"])

    def test_the_schema_is_strict(self):
        cases = {
            "conflicts": ({"ack_of": 5, "disposition": "conflicts", "conflicts_with": 3}, "not {ack_of"),
            "conflicts alone": ({"ack_of": 5, "disposition": "conflicts"}, "disposition is not"),
            "free text disposition": ({"ack_of": 5, "disposition": "done"}, "disposition is not"),
            "extra key": ({"ack_of": 5, "disposition": "accepted", "to_session_id": SID}, "not {ack_of"),
            "seq as text": ({"ack_of": "5", "disposition": "accepted"}, "names #5, not"),
            "seq as bool": ({"ack_of": True, "disposition": "accepted"}, "names #True"),
            "no disposition": ({"ack_of": 5}, "not {ack_of"),
            "a list": ([5, "accepted"], "not {ack_of"),
            "long note": ({"ack_of": 5, "disposition": "accepted", "note": "x" * (ie.MAX_NOTE + 1)}, "note is not"),
            "control chars": ({"ack_of": 5, "disposition": "accepted", "note": "ok\x1b[2J"}, "note is not"),
            "note not text": ({"ack_of": 5, "disposition": "accepted", "note": {"a": 1}}, "note is not"),
        }
        for name, (doc, why) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.write(doc)
                self.not_relayed(why)

    def test_a_failed_post_is_retried_and_posted_once(self):
        self.write({"ack_of": 5, "disposition": "accepted"})
        self.world.post_error = OSError("network down")
        self.exit_and_settle()
        self.assertEqual(self.world.posted, [])
        self.assertEqual(self.state(5), w.RESUMING)                 # not failed: asked again
        self.assertTrue(self.tmp_dir.exists())                       # the answer is kept for the retry
        self.world.post_error = None
        self.world.t += 60
        self.run_pass()
        self.assertEqual(len(self.world.posted), 1)
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)
        self.world.t += 60
        self.run_pass()
        self.assertEqual(len(self.world.posted), 1)

    def test_a_relayed_ack_the_read_does_not_show_yet_is_asked_again(self):
        self.write({"ack_of": 5, "disposition": "accepted"})
        real = self.world.post_ack
        self.world.post_ack = lambda *a: {"event": {}}   # posted, but not visible to the next read yet
        self.exit_and_settle()
        self.assertEqual(self.state(5), w.ACK_RELAYED)
        self.world.post_ack = real
        self.world.events.append(ack(9, 5))
        self.world.t += 60
        self.run_pass()
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)

    def test_an_ack_relayed_entry_blocks_another_claim(self):
        self.assertIn(w.ACK_RELAYED, w.IN_FLIGHT)

    def test_a_run_that_failed_verification_is_not_relayed(self):
        t = turn()
        t["payload"]["sandbox_policy"]["writable_roots"] = [str(self.home)]
        self.append_turns(SID, t)
        self.write({"ack_of": 5, "disposition": "accepted"})
        self.not_relayed("failed verification")
        self.assertTrue(w.disabled_reason(self.home, "codex"))


class PostAckEvent(unittest.TestCase):
    """System.post_ack's default: the plugin's own ack path, never anything else."""

    def test_it_posts_one_acknowledged_event_as_the_session(self):
        calls = []

        def append(conn, inv, type_, payload, **kw):
            calls.append((conn, inv, type_, payload, kw))
            return {"event": {"investigation_id": inv, "seq": 9}}
        with patch.object(ie, "append_event", append):
            w.post_ack_event(MEMBER, INV, SID, 5, "accepted", "done", "codex/0.29.1")
        self.assertEqual(calls, [(MEMBER, INV, "acknowledged", {"ack_of": 5, "disposition": "accepted",
                                                                 "note": "done"},
                                  {"idempotency_key": ie.ack_key(SID, 5), "client": "codex/0.29.1",
                                   "session_id": SID, "producer_client": "codex/0.29.1", "opener": None})])

    def test_an_idempotent_repost_is_not_an_error(self):
        def conflict(*a, **kw):
            raise sync.ServerError(409, {"error": "idempotency_conflict"}, "conflict")
        with patch.object(ie, "append_event", conflict):
            self.assertEqual(w.post_ack_event(MEMBER, INV, SID, 5, "noted", None, "c"), {"duplicate": True})

    def test_other_server_errors_raise(self):
        def refused(*a, **kw):
            raise sync.ServerError(403, {"error": "forbidden"}, "no")
        with patch.object(ie, "append_event", refused), self.assertRaises(sync.ServerError):
            w.post_ack_event(MEMBER, INV, SID, 5, "noted", None, "c")


class ReadAckFile(unittest.TestCase):
    def test_no_tmp_dir_or_a_symlinked_dir(self):
        import tempfile
        self.assertEqual(w.read_ack_file(None, 5)[0], None)
        self.assertEqual(w.read_ack_file("relative", 5)[0], None)
        with tempfile.TemporaryDirectory() as t:
            real = Path(t) / "real"
            real.mkdir()
            (real / w.ACK_FILE).write_text(json.dumps({"ack_of": 5, "disposition": "accepted"}))
            link = Path(t) / "link"
            link.symlink_to(real)
            self.assertEqual(w.read_ack_file(str(real), 5), ({"disposition": "accepted"}, ""))
            ack_, why = w.read_ack_file(str(link), 5)
            self.assertIsNone(ack_)
            self.assertIn("not a directory", why)


if __name__ == "__main__":
    unittest.main()

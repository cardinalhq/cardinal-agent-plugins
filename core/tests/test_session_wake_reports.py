"""Wake intent and session.wake reports (conductor#2110).

A: only an owner decision whose payload.wake is exactly true may resume a
session; any other addressed decision is a notification ("decision posted
without resume requested").
B: each lifecycle state is reported once as a session.wake event (best
effort, fixed-vocabulary reasons), only when the binding advertises
session_wake; refusals stop reports for the investigation without touching
the lifecycle.

Same fakes as tests.test_session_wake (World.post_wake records reports).
Nothing starts an agent or touches the network.

Run from core/:  python3 -m unittest tests.test_session_wake_reports -v
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from cardinal_core import investigation_events as ie
from cardinal_core import investigation_state_sync as sync
from cardinal_core import session_wake as w

from tests.test_session_wake import CAPS, INV, MEMBER, NOWAKE, SECRET, SID, SID2, Base, ack, binding, decision, turn

WAKE_CAPS = dict(CAPS, session_wake={"enabled": True})


def refused(status, error=None):
    return sync.ServerError(status, {"error": error} if error else {}, "refused")


# ---------------------------------------------------------------------------
# A: wake intent
# ---------------------------------------------------------------------------

class WakeIntent(Base):
    def test_only_wake_true_launches(self):
        for wake, launches in ((True, 1), (NOWAKE, 0), (False, 0), ("true", 0), (1, 0), (None, 0), ({}, 0)):
            with self.subTest(wake=wake):
                self.setUp()
                self.world.events = [decision(5, wake=wake)]
                self.run_pass()
                self.assertEqual(len(self.world.launches), launches)
                if not launches:
                    self.assertEqual(self.notified_only(), w.WAKE_NOT_REQUESTED)
                    self.assertIn("waits for you", self.world.notes[0][1])

    def test_a_decision_without_wake_is_handled_once(self):
        self.world.events = [decision(5, wake=NOWAKE)]
        self.run_pass()
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.notes), 1)
        self.world.events.append(decision(6))                 # a later one asking to resume does
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_wake_true_still_needs_everything_else(self):
        for e in (decision(5, to=SID2), decision(5, is_investigation_author=False),
                  dict(decision(5), authority="producer_claim")):
            with self.subTest(e=str(e)[:60]):
                self.setUp()
                self.world.events = [e]
                self.run_pass()
                self.assertEqual(self.world.launches, [])

    def test_decision_requests_wake(self):
        self.assertTrue(w.decision_requests_wake({"payload": {"wake": True}}))
        for p in ({}, {"wake": False}, {"wake": "true"}, {"wake": 1}, None, "x"):
            self.assertFalse(w.decision_requests_wake({"payload": p}))
        self.assertFalse(w.decision_requests_wake(None))


# ---------------------------------------------------------------------------
# B: session.wake reports
# ---------------------------------------------------------------------------

class Reports(Base):
    def setUp(self):
        super().setUp()
        ie.write_binding(self.home, SID, binding(capabilities=WAKE_CAPS))

    def states(self):
        return [p["payload"]["state"] for p in self.world.wake_posts]

    def finish(self, *events):
        self.append_turns(SID, turn())
        self.world.events += list(events)
        self.world.exit()
        self.world.t += 60
        self.run_pass()

    def test_a_launch_is_reported_state_by_state_once(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.states(), ["claimed", "resuming"])
        self.run_pass()
        self.assertEqual(self.states(), ["claimed", "resuming"])          # once each
        self.finish(ack(6, 5))
        self.assertEqual(self.states(), ["claimed", "resuming", "acknowledged"])
        last = self.world.wake_posts[-1]
        self.assertEqual(last["payload"], {"decision_seq": 5, "state": "acknowledged", "runtime": "codex",
                                           "reason": "acknowledged"})
        self.assertEqual((last["inv"], last["sid"]), (INV, SID))
        keys = [p["key"] for p in self.world.wake_posts]
        self.assertEqual(len(set(keys)), 3)
        for k in keys:
            self.assertTrue(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", k), k)
            self.assertFalse(k.lower().startswith(ie.RESERVED_KEY_PREFIXES))

    def test_a_failed_and_a_timed_out_run_are_reported(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.world.t += w.WAKE_TIMEOUT + 10
        self.run_pass()
        self.assertEqual(self.states()[-1], "timed_out")
        self.finish()
        self.world.t += w.ACK_CHECK_GRACE + 1
        self.run_pass()
        self.assertEqual(self.states()[-1], "failed")
        self.assertEqual(self.world.wake_posts[-1]["payload"]["reason"], "no acknowledgment")

    def test_a_notification_is_reported_with_its_reason(self):
        self.world.events = [decision(5, wake=NOWAKE)]
        self.run_pass()
        self.assertEqual(self.world.wake_posts[0]["payload"],
                         {"decision_seq": 5, "state": "notified", "runtime": "codex",
                          "reason": w.WAKE_NOT_REQUESTED})

    def test_reasons_are_a_fixed_vocabulary_never_the_waker_words(self):
        self.world.events = [decision(5)]
        self.codex.write_bytes(self.codex.read_bytes() + b"swapped")   # "<path under $HOME> changed since ..."
        self.run_pass()
        payload = self.world.wake_posts[0]["payload"]
        self.assertEqual(payload["reason"], "launch binaries changed since enable")
        blob = json.dumps(self.world.wake_posts)
        for leak in (str(self.home), SECRET, "/bin/codex"):
            self.assertNotIn(leak, blob)
        for state in (w.NOTIFIED, w.FAILED, w.RESUMING):
            for note in ("", "/Users/x/.ssh/id_rsa leaked", SECRET):
                r = w.wake_reason(state, note)
                self.assertTrue(r is None or r in w.WAKE_REASONS, r)
                self.assertNotIn("/Users", r or "")

    def test_no_capability_no_reports(self):
        ie.write_binding(self.home, SID, binding())
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.world.wake_posts, [])

    def test_an_older_server_or_a_refusal_stops_reports_for_the_investigation(self):
        for err in (refused(400, "unknown_event_type"), refused(404),
                    refused(403, "wake_requires_investigation_author"),
                    refused(409, "event_limit_reached")):
            with self.subTest(err=err.body):
                self.setUp()
                self.world.wake_error = err
                self.world.events = [decision(5)]
                self.run_pass()
                self.assertEqual(len(self.world.launches), 1)                  # the lifecycle goes on
                self.assertEqual(self.state(5), w.RESUMING)
                self.assertIn(INV, w.read_ledger(self.home, SID)["wake_reports_off"])
                self.world.wake_error = None
                self.finish(ack(6, 5))
                self.assertEqual(self.world.wake_posts, [])                    # stopped for good
                self.assertTrue(self.world.notes)                              # local notifications stay

    def test_a_decision_the_server_cannot_match_ends_that_entrys_reports(self):
        for err in (refused(404, "wake_decision_not_found"), refused(422, "wake_decision_not_addressed_to_session"),
                    refused(400, "invalid_event")):
            with self.subTest(err=err.body):
                self.setUp()
                self.world.wake_error = err
                self.world.events = [decision(5)]
                self.run_pass()
                rec = self.ledger()["5"]
                self.assertTrue(rec["reports_ended"])
                self.assertNotIn(INV, w.read_ledger(self.home, SID).get("wake_reports_off", {}))
                self.world.wake_error = None
                self.finish(ack(6, 5))
                self.assertEqual(self.world.wake_posts, [])

    def test_an_idempotency_conflict_counts_as_reported(self):
        self.world.wake_error = {"claimed": refused(409, "idempotency_conflict")}
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.states(), ["resuming"])
        self.assertEqual(self.ledger()["5"]["reports"], ["claimed", "resuming"])

    def test_a_transient_failure_is_retried_then_given_up(self):
        self.world.wake_error = OSError("network down")
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.state(5), w.RESUMING)
        self.world.wake_error = None
        self.run_pass()
        self.assertEqual(self.states(), ["claimed", "resuming"])
        self.setUp()
        self.world.wake_error = refused(503)
        self.world.events = [decision(5)]
        for _ in range(w.WAKE_REPORT_TRIES + 2):
            self.run_pass()
        self.assertEqual(self.ledger()["5"]["reports_ended"], "gave up after retries")

    def test_entries_from_before_reports_are_not_reported(self):
        led = w.read_ledger(self.home, SID)
        e = {"seq": 3, "investigation_id": INV, "runtime": "codex"}
        w.transition(e, w.NOTIFIED, self.t0, "seeded")
        w.add_entry(led, e)
        w.write_ledger(self.home, SID, led, self.t0)
        w.report_wake_states(self.home, SID, self.world.system(), w.read_config(self.home))
        self.assertEqual(self.world.wake_posts, [])

    def test_ack_relayed_is_not_a_report(self):
        self.world.events = [decision(5)]
        self.run_pass()
        tmp = Path(self.world.launches[0]["env"]["TMPDIR"])
        (tmp / w.ACK_FILE).write_text(json.dumps({"ack_of": 5, "disposition": "noted"}))
        self.finish()
        self.assertEqual(self.states(), ["claimed", "resuming", "acknowledged"])
        self.assertEqual(self.world.wake_posts[-1]["payload"]["reason"], "acknowledgment relayed by the waker")

    def test_a_dry_run_reports_nothing(self):
        self.world.events = [decision(5)]
        w.run_once(self.home, self.world.system(), dry_run=True)
        self.assertEqual(self.world.wake_posts, [])


class PostWakeEvent(unittest.TestCase):
    def test_it_appends_one_session_wake_as_the_session(self):
        calls = []

        def append(conn, inv, type_, payload, **kw):
            calls.append((inv, type_, payload, kw))
            return {"event": {"investigation_id": inv, "seq": 9}}
        payload = {"decision_seq": 5, "state": "claimed", "runtime": "codex"}
        with patch.object(ie, "append_event", append):
            w.post_wake_event(MEMBER, INV, SID, payload, "wake:abc:claimed", "codex/0.29.1")
        inv, type_, sent, kw = calls[0]
        self.assertEqual((inv, type_, sent), (INV, "session.wake", payload))
        self.assertEqual(kw["session_id"], SID)
        self.assertNotIn("to_session_id", kw)
        self.assertEqual(kw["idempotency_key"], "wake:abc:claimed")
        self.assertEqual(kw["timeout"], w.WAKE_REPORT_TIMEOUT)

    def test_the_key_is_per_decision_session_and_state(self):
        a = w.wake_report_key(INV, SID, 5, "claimed")
        self.assertNotEqual(a, w.wake_report_key(INV, SID, 5, "resuming"))
        self.assertNotEqual(a, w.wake_report_key(INV, SID2, 5, "claimed"))
        self.assertNotEqual(a, w.wake_report_key(INV, SID, 6, "claimed"))
        self.assertLessEqual(len(w.wake_report_key(INV, SID, 10 ** 17, "acknowledged")), 128)


if __name__ == "__main__":
    unittest.main()

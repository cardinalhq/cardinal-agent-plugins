"""Wake intent and session.wake reports (conductor#2110), v4.

A: every addressed decision is one notification; one whose payload.wake is
not exactly true carries the reason "decision posted without resume
requested".
B: each NOTIFIED entry is reported once as a session.wake event (state
"notified", best effort, fixed-vocabulary reasons), only when the binding
advertises session_wake; refusals stop reports for the investigation
without touching the ledger. The launch states of earlier versions are never
reported.

Same fakes as tests.test_session_wake (World.post_wake records reports).
Nothing starts an agent or touches the network.

Run from core/:  python3 -m unittest tests.test_session_wake_reports -v
"""

from __future__ import annotations

import json
import re
import unittest
from unittest.mock import patch

from cardinal_core import investigation_events as ie
from cardinal_core import investigation_state_sync as sync
from cardinal_core import session_wake as w

from tests.test_session_wake import CAPS, INV, INV2, MEMBER, NOWAKE, SECRET, SID, SID2, Base, binding, decision

WAKE_CAPS = dict(CAPS, session_wake={"enabled": True})


def refused(status, error=None):
    return sync.ServerError(status, {"error": error} if error else {}, "refused")


# ---------------------------------------------------------------------------
# A: wake intent
# ---------------------------------------------------------------------------

class WakeIntent(Base):
    def test_only_wake_true_is_a_resume_request(self):
        for wake, requested in ((True, True), (NOWAKE, False), (False, False), ("true", False), (1, False),
                                (None, False), ({}, False)):
            with self.subTest(wake=wake):
                self.setUp()
                self.world.events = [decision(5, wake=wake)]
                self.run_pass()
                reason = self.notified()
                self.assertEqual(reason == w.WAKE_NOT_REQUESTED, not requested, reason)
                self.assertEqual(len(self.world.notes), 1)

    def test_a_decision_without_wake_is_handled_once(self):
        self.world.events = [decision(5, wake=NOWAKE)]
        self.run_pass()
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.notes), 1)
        self.world.events.append(decision(6))
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.notes), 2)

    def test_wake_true_still_needs_everything_else(self):
        for e in (decision(5, to=SID2), decision(5, is_investigation_author=False),
                  dict(decision(5), authority="producer_claim")):
            with self.subTest(e=str(e)[:60]):
                self.setUp()
                self.world.events = [e]
                self.run_pass()
                self.nothing()

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

    def test_a_notification_is_reported_once(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.states(), ["notified"])
        post = self.world.wake_posts[0]
        self.assertEqual(post["payload"], {"decision_seq": 5, "state": "notified", "runtime": "codex",
                                           "reason": "session cannot be resumed unattended"})
        self.assertEqual((post["inv"], post["sid"]), (INV, SID))
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", post["key"]), post["key"])
        self.assertFalse(post["key"].lower().startswith(ie.RESERVED_KEY_PREFIXES))

    def test_each_reason(self):
        for events, table, reason in (([decision(5, wake=NOWAKE)], [], w.WAKE_NOT_REQUESTED),
                                      ([decision(5, created=self.t0 - 8 * 86400)], [], "decision older than 7 days"),
                                      ([decision(5)], [(1234, f"codex resume {SID}")], "session is live")):
            with self.subTest(reason=reason):
                self.setUp()
                self.world.events, self.world.table = events, table
                self.run_pass()
                self.assertEqual(self.world.wake_posts[0]["payload"]["reason"], reason)

    def test_reasons_are_a_fixed_vocabulary_never_the_waker_words(self):
        self.world.table = [(1234, f"node {self.home}/x {SID}")]
        self.world.events = [decision(5)]
        self.run_pass()
        blob = json.dumps(self.world.wake_posts)
        for leak in (str(self.home), SECRET, "names the session"):
            self.assertNotIn(leak, blob)
        for state in [w.NOTIFIED] + list(w.LEGACY_STATES):
            for note in ("", "/Users/x/.ssh/id_rsa leaked", SECRET):
                r = w.wake_reason(state, note)
                self.assertTrue(r is None or r in w.WAKE_REASONS, r)
                self.assertNotIn("/Users", r or "")

    def test_no_capability_no_reports(self):
        ie.write_binding(self.home, SID, binding())
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.state(5), w.NOTIFIED)
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
                self.assertEqual(self.state(5), w.NOTIFIED)                    # the ledger goes on
                self.assertIn(INV, w.read_ledger(self.home, SID)["wake_reports_off"])
                self.world.wake_error = None
                self.world.events.append(decision(6))
                self.world.t += 600
                self.run_pass()
                self.assertEqual(self.world.wake_posts, [])                    # stopped for good
                self.assertEqual(len(self.world.notes), 2)                     # local notifications stay

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
                self.run_pass()
                self.assertEqual(self.world.wake_posts, [])

    def test_an_idempotency_conflict_counts_as_reported(self):
        self.world.wake_error = {"notified": refused(409, "idempotency_conflict")}
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.ledger()["5"]["reports"], ["notified"])
        self.world.wake_error = None
        self.run_pass()
        self.assertEqual(self.world.wake_posts, [])

    def test_a_transient_failure_is_retried_then_given_up(self):
        self.world.wake_error = OSError("network down")
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.state(5), w.NOTIFIED)
        self.world.wake_error = None
        self.run_pass()
        self.assertEqual(self.states(), ["notified"])
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

    def test_the_launch_states_of_an_earlier_version_are_never_reported(self):
        led = w.read_ledger(self.home, SID)
        for i, st in enumerate(w.LEGACY_STATES):
            e = {"seq": i + 1, "investigation_id": INV, "runtime": "codex", "reports": ["claimed"]}
            w.transition(e, "CLAIMED", self.t0, "seeded")
            w.transition(e, st, self.t0, "acknowledged the decision")
            w.add_entry(led, e)
        w.write_ledger(self.home, SID, led, self.t0)
        w.report_wake_states(self.home, SID, self.world.system(), w.read_config(self.home))
        self.assertEqual(self.world.wake_posts, [])

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
        payload = {"decision_seq": 5, "state": "notified", "runtime": "codex"}
        with patch.object(ie, "append_event", append):
            w.post_wake_event(MEMBER, INV, SID, payload, "wake:abc:notified", "codex/0.29.1")
        inv, type_, sent, kw = calls[0]
        self.assertEqual((inv, type_, sent), (INV, "session.wake", payload))
        self.assertEqual(kw["session_id"], SID)
        self.assertNotIn("to_session_id", kw)
        self.assertEqual(kw["idempotency_key"], "wake:abc:notified")
        self.assertEqual(kw["timeout"], w.WAKE_REPORT_TIMEOUT)

    def test_the_key_is_per_decision_session_and_state(self):
        a = w.wake_report_key(INV, SID, 5, "notified")
        self.assertNotEqual(a, w.wake_report_key(INV2, SID, 5, "notified"))
        self.assertNotEqual(a, w.wake_report_key(INV, SID2, 5, "notified"))
        self.assertNotEqual(a, w.wake_report_key(INV, SID, 6, "notified"))
        self.assertLessEqual(len(w.wake_report_key(INV, SID, 10 ** 17, "notified")), 128)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest

import support
from support import Harness

from cardinal_devin.state import PollState, StateError

SHA_42 = "1" * 40
DAY = 86400


def by_pr(records):
    return {attrs["cardinal_pr_number"]: (res, attrs) for res, attrs in records}


class PollCase(unittest.TestCase):
    api = "v1"

    def setUp(self) -> None:
        self.h = Harness(self.addCleanup, api=self.api, page_size=2)

    def poll(self, **kw):
        with support.captured_logs() as logs:
            result = self.h.poller(**kw).poll_once()
        return result, logs.text

    def decisions(self, session_id=None):
        return [(r, a) for r, a in self.h.records("cardinal.decision")
                if session_id is None or a["session_id"] == session_id]

    def decision_ids(self, session_id=None):
        return [a["cardinal.decision.id"] for _, a in self.decisions(session_id)]

    def state(self):
        return PollState(self.h.state_path).sessions


class V1PollTests(PollCase):
    api = "v1"

    def test_git_state_with_github_token(self) -> None:
        result, _ = self.poll()
        self.assertEqual(result.errors, 0)
        states = by_pr(self.h.records("cardinal.git_state"))
        self.assertEqual(sorted(states), [42, 43])  # 44 is outside the lookback window
        resource, attrs = states[42]
        self.assertEqual(attrs, {
            "event_name": "cardinal.git_state",
            "session_id": "devin-v1-finished",
            "cardinal_head_sha": SHA_42,
            "cardinal_branch": "feat/widget-sync-retry",
            "cardinal_repo": "acme/widgets",
            "cardinal_remote_url": "https://github.com/acme/widgets.git",
            "cardinal_pr_number": 42,
            "cardinal_pr_url": "https://github.com/acme/widgets/pull/42",
            "cardinal_initiative_name": "widget-sync-retry",
            "cardinal_initiative_type": "feature",
        })
        self.assertEqual(resource["service.name"], "devin")
        self.assertEqual(resource["agent.runtime"], "devin")
        self.assertEqual(resource["user.email"], "alice@example.com")
        self.assertEqual(resource["cardinal.org"], "acme")
        _, working = states[43]
        self.assertEqual((working["cardinal_branch"], working["cardinal_initiative_type"]), ("fix/worker-crash", "bugfix"))
        self.assertEqual(states[43][0]["user.email"], "bob@example.com")
        list_offsets = [r.query["offset"][0] for r in self.h.devin.requests if r.path == "/v1/sessions"]
        self.assertEqual(list_offsets, ["0", "2", "4"])

    def test_decisions_sent_whatever_the_status(self) -> None:
        result, logs = self.poll()
        self.assertEqual(self.decision_ids("devin-v1-finished"), ["retry-backoff", "environment-variables"])
        self.assertEqual(self.decision_ids("devin-v1-working"), ["recorded-while-the-session-is-still-working"])
        for resource, attrs in self.decisions("devin-v1-finished"):
            self.assertEqual(attrs["cardinal.pr_number"], 42)
            self.assertEqual(attrs["cardinal.pr_url"], "https://github.com/acme/widgets/pull/42")
            self.assertEqual(attrs["cardinal.repo"], "acme/widgets")
            self.assertEqual(attrs["cardinal.branch"], "feat/widget-sync-retry")
            self.assertEqual(attrs["cardinal.head_sha"], SHA_42)
            self.assertEqual(resource["user.email"], "alice@example.com")
        self.assertEqual(json.loads(self.decisions("devin-v1-finished")[1][1]["cardinal.decision.links"]),
                         [{"relation": "follows_from", "to": "retry-backoff"}])
        self.assertEqual(self.decisions("devin-v1-working")[0][1]["cardinal.branch"], "fix/worker-crash")
        self.assertEqual(result.decisions, 3)
        self.assertEqual(result.skipped_decisions, 2)
        self.assertIn("devin-v1-finished: skipped decisions[2]: choice is required", logs)
        self.assertIn("devin-v1-finished: skipped decisions[3]: anchors[0].kind", logs)
        self.assertIn("devin-v1-expired-no-output: no structured_output", logs)
        detail_paths = {r.path for r in self.h.devin.requests if r.path.startswith("/v1/sessions/")}
        self.assertEqual(detail_paths, {"/v1/sessions/devin-v1-finished", "/v1/sessions/devin-v1-expired-no-output"})

    def test_blocked_session_sends_decisions_once(self) -> None:
        self.h.devin.update("devin-v1-blocked", structured_output={
            "decisions": [{"id": "ask-first", "choice": "Ask before deleting data"}]})
        self.poll()
        self.assertEqual(self.decision_ids("devin-v1-blocked"), ["ask-first"])
        self.assertNotIn("cardinal.pr_number", self.decisions("devin-v1-blocked")[0][1])
        posts = len(self.h.ingest.posts)
        self.poll()
        self.assertEqual(len(self.h.ingest.posts), posts)

    def test_revised_decision_is_resent(self) -> None:
        self.poll()
        self.h.devin.update("devin-v1-working", structured_output={"decisions": [
            {"choice": "Recorded while the session is still working", "rationale": "Now with a reason."}]})
        self.poll()
        revisions = self.decisions("devin-v1-working")
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[1][1]["cardinal.decision.rationale"], "Now with a reason.")

    def test_without_github_token(self) -> None:
        self.poll(github=False)
        self.assertEqual(self.h.github.requests, [])
        _, attrs = by_pr(self.h.records("cardinal.git_state"))[42]
        self.assertEqual(attrs, {
            "event_name": "cardinal.git_state",
            "session_id": "devin-v1-finished",
            "cardinal_repo": "acme/widgets",
            "cardinal_remote_url": "https://github.com/acme/widgets.git",
            "cardinal_pr_number": 42,
            "cardinal_pr_url": "https://github.com/acme/widgets/pull/42",
        })
        _, decision = self.decisions("devin-v1-finished")[0]
        self.assertEqual(decision["cardinal.repo"], "acme/widgets")
        self.assertNotIn("cardinal.branch", decision)

    def test_github_lookup_failure_falls_back_to_url_facts(self) -> None:
        self.h.github.pulls.pop("acme/widgets/42")
        _, logs = self.poll()
        _, attrs = by_pr(self.h.records("cardinal.git_state"))[42]
        self.assertNotIn("cardinal_branch", attrs)
        self.assertEqual(attrs["cardinal_repo"], "acme/widgets")
        self.assertIn("GitHub has no pull request", logs)

    def test_repoll_and_restart_do_not_duplicate(self) -> None:
        self.poll()
        posts, gh_calls = len(self.h.ingest.posts), len(self.h.github.requests)
        self.assertGreater(posts, 0)
        poller = self.h.poller()  # fresh process: state comes from disk
        with support.captured_logs():
            poller.poll_once()
            poller.poll_once()
            self.h.poller().poll_once()
        self.assertEqual(len(self.h.ingest.posts), posts)
        self.assertEqual(len(self.h.github.requests), gh_calls)

    def test_active_session_finishing(self) -> None:
        self.poll()
        before = len(self.h.records())
        self.h.devin.update(
            "devin-v1-working", status="finished", status_enum="finished", updated_at="2026-09-14T10:30:00Z",
            structured_output={"decisions": [
                {"choice": "Recorded while the session is still working"},
                {"choice": "Keep the worker single-threaded"},
            ]},
        )
        self.poll()
        new = self.h.records()[before:]
        self.assertEqual(
            [(a["event_name"], a.get("cardinal.decision.id")) for _, a in new],
            [("cardinal.decision", "keep-the-worker-single-threaded")],
        )  # git_state unchanged at terminal and the first decision already sent
        self.assertEqual(new[0][1]["cardinal.pr_number"], 43)

    def test_git_state_resent_at_terminal_only_when_changed(self) -> None:
        self.poll()
        before = len(self.h.records())
        self.h.github.pulls["acme/widgets/43"]["head"]["sha"] = "4" * 40
        self.h.devin.update("devin-v1-working", updated_at="2026-09-14T10:10:00Z")
        self.poll()
        self.assertEqual(len(self.h.records()), before)  # still active: no re-send
        self.h.devin.update("devin-v1-working", status_enum="finished", updated_at="2026-09-14T10:20:00Z")
        self.poll()
        new = self.h.records()[before:]
        self.assertEqual([a["event_name"] for _, a in new], ["cardinal.git_state"])  # decision content unchanged
        self.assertEqual(new[0][1]["cardinal_head_sha"], "4" * 40)

    def test_new_pr_on_active_session_is_sent_immediately(self) -> None:
        self.poll()
        self.h.devin.update("devin-v1-blocked", status_enum="working", updated_at="2026-09-14T10:40:00Z",
                            pull_request={"url": "https://github.com/acme/widgets/pull/42"})
        self.poll()
        last = self.h.records("cardinal.git_state")[-1][1]
        self.assertEqual((last["session_id"], last["cardinal_pr_number"]), ("devin-v1-blocked", 42))

    def test_ingest_failure_is_retried_next_poll(self) -> None:
        self.h.ingest.status = 500
        result, logs = self.poll()
        self.assertGreater(result.errors, 0)
        self.assertIn("will retry next poll", logs)
        self.assertNotIn("devin-v1-finished", self.state())
        self.assertIn("devin-v1-blocked", self.state())  # nothing to send, so recorded
        self.h.ingest.status = 200
        self.poll()
        self.assertEqual(sorted(by_pr(self.h.records("cardinal.git_state"))), [42, 43])
        self.assertEqual(len(self.decisions()), 3)

    def test_dry_run_sends_and_writes_nothing(self) -> None:
        result, _ = self.poll(dry_run=True)
        self.assertTrue(result.bodies)
        self.assertEqual(self.h.ingest.posts, [])
        self.assertFalse(self.h.state_path.exists())
        events = [a["event_name"] for _, a in support.records_in(result.bodies)]
        self.assertIn("cardinal.git_state", events)
        self.assertIn("cardinal.decision", events)

    def test_decisions_can_be_disabled(self) -> None:
        self.poll(emit_decisions=False)
        self.assertEqual(self.decisions(), [])
        self.poll()  # enabling later still sends them
        self.assertEqual(len(self.decisions()), 3)

    def test_corrupt_state_is_refused(self) -> None:
        self.h.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.h.state_path.write_text("{not json")
        with self.assertRaises(StateError):
            PollState(self.h.state_path)


class V3PollTests(PollCase):
    api = "v3"

    def test_v3_poll(self) -> None:
        result, _ = self.poll()
        self.assertEqual(result.errors, 0)
        states = by_pr(self.h.records("cardinal.git_state"))
        self.assertEqual(sorted(states), [42, 43])
        self.assertEqual(states[42][0]["user.email"], "alice@example.com")
        self.assertEqual(states[42][1]["session_id"], "v3exit")
        self.assertEqual(states[43][0]["user.email"], "unknown")  # user-bob not resolvable
        self.assertEqual(self.decision_ids(), ["retry-backoff"])
        self.assertEqual(json.loads(self.decisions()[0][1]["cardinal.decision.anchors"]),
                         [{"kind": "directory", "identifier": "src/sync", "path": "src/sync"}])
        lists = [r for r in self.h.devin.requests if r.path == "/v3/organizations/org-test/sessions"]
        self.assertEqual(len(lists), 2)
        self.assertIn("updated_after", lists[0].query)
        self.assertEqual(lists[1].query["after"], ["2"])
        self.assertIn("/v3/organizations/org-test/sessions/devin-v3exit", {r.path for r in self.h.devin.requests})

    def test_v3_high_water_mark_and_refresh(self) -> None:
        self.poll()
        posts = len(self.h.ingest.posts)
        user_lookups = len([r for r in self.h.devin.requests if r.path.startswith("/v3beta1/")])
        self.h.now += 600
        self.h.devin.requests.clear()
        self.poll()
        lists = [r for r in self.h.devin.requests if r.path == "/v3/organizations/org-test/sessions"]
        self.assertEqual(lists[0].query["updated_after"], [str(int(self.h.now - 600) - 300)])
        refreshed = {r.path.rsplit("/", 1)[-1] for r in self.h.devin.requests
                     if r.path.startswith("/v3/organizations/org-test/sessions/")}
        self.assertEqual(refreshed, {"devin-v3running", "devin-v3suspended"})
        self.assertEqual(len(self.h.ingest.posts), posts)
        self.assertEqual(user_lookups, 2)  # alice + bob on the first poll
        self.assertFalse([r for r in self.h.devin.requests if r.path.startswith("/v3beta1/")])  # cached

    def test_millisecond_timestamps_set_updated_after_unit(self) -> None:
        for session in self.h.devin.sessions:
            session["created_at"] *= 1000
            session["updated_at"] *= 1000
        self.poll()
        first = int(self.h.now)
        posts = len(self.h.ingest.posts)
        self.h.now += 600
        self.h.devin.requests.clear()
        self.poll()
        lists = [r for r in self.h.devin.requests if r.path == "/v3/organizations/org-test/sessions"]
        self.assertEqual(lists[0].query["updated_after"], [str((first - 300) * 1000)])
        self.assertEqual(len(self.h.ingest.posts), posts)

    def test_suspended_session_sends_decisions_once(self) -> None:
        self.h.devin.update("v3suspended", structured_output={"decisions": [{"choice": "Cache in memory"}]})
        self.poll()
        self.assertEqual(self.decision_ids("v3suspended"), ["cache-in-memory"])
        posts = len(self.h.ingest.posts)
        self.h.now += 600
        self.poll()  # unlisted now; refreshed through detail
        self.assertEqual(len(self.h.ingest.posts), posts)

    def test_stale_active_session_stops_being_polled(self) -> None:
        # v3's updated_after filter hides idle sessions, so they are fetched
        # one by one until they pass the active TTL.
        self.poll(lookback_days=30)
        self.h.now += 20 * DAY
        _, logs = self.poll(lookback_days=30)
        self.assertIn("v3running: no update in 14 days; no longer polled", logs)
        self.h.devin.requests.clear()
        self.poll(lookback_days=30)
        self.assertFalse([r for r in self.h.devin.requests if r.path.endswith("/devin-v3running")])

    def test_resume_after_stale_sends_nothing_new(self) -> None:
        self.poll()
        posts = len(self.h.ingest.posts)
        self.h.now += 15 * DAY
        _, logs = self.poll()
        self.assertIn("v3running: no update in 14 days", logs)
        self.assertTrue(self.state()["v3running"]["stale"])  # kept, not pruned
        self.h.now += DAY
        self.h.devin.update("v3running", updated_at=int(self.h.now) - 60)
        self.poll()
        self.assertEqual(len(self.h.ingest.posts), posts)
        self.assertNotIn("stale", self.state()["v3running"])

    def test_not_found_needs_repeated_misses(self) -> None:
        self.poll()
        self.h.devin.sessions = [s for s in self.h.devin.sessions if s["session_id"] != "v3running"]
        for _ in range(2):
            self.h.now += 600
            self.poll()
        entry = self.state()["v3running"]
        self.assertEqual(entry["not_found"], 2)
        self.assertFalse(entry.get("gone"))
        self.assertTrue(entry["prs"])
        self.h.now += 600
        _, logs = self.poll()
        self.assertIn("not found 3 cycles in a row", logs)
        self.assertTrue(self.state()["v3running"]["gone"])
        self.h.devin.requests.clear()
        self.h.now += 600
        self.poll()
        self.assertFalse([r for r in self.h.devin.requests if r.path.endswith("/devin-v3running")])

    def test_decisions_attach_to_highest_numbered_pr(self) -> None:
        self.h.devin.update("v3exit", pull_requests=[
            {"pr_url": "https://github.com/acme/widgets/pull/42", "pr_state": "open"},
            {"pr_url": "https://github.com/acme/widgets/pull/43", "pr_state": None},
        ])
        self.poll()
        self.assertEqual(self.decisions("v3exit")[0][1]["cardinal.pr_number"], 43)
        exit_prs = sorted(a["cardinal_pr_number"] for _, a in self.h.records("cardinal.git_state")
                          if a["session_id"] == "v3exit")
        self.assertEqual(exit_prs, [42, 43])

    def test_v3_running_session_exits(self) -> None:
        self.poll()
        self.h.now += 600
        self.h.devin.update("v3running", status="exit", status_detail="finished",
                            updated_at=int(self.h.now) - 60,
                            structured_output={"decisions": [{"choice": "Guard the queue with a lock"}]})
        self.poll()
        last = self.decisions()[-1][1]
        self.assertEqual((last["session_id"], last["cardinal.decision.id"], last["cardinal.pr_number"]),
                         ("v3running", "guard-the-queue-with-a-lock", 43))


if __name__ == "__main__":
    unittest.main()

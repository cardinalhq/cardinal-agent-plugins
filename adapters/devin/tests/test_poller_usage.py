from __future__ import annotations

import unittest

import support
from support import Harness

from cardinal_devin.state import PollState


class UsagePollCase(unittest.TestCase):
    api = "v1"

    def setUp(self) -> None:
        self.h = Harness(self.addCleanup, api=self.api, page_size=2)

    def poll(self, **kw):
        with support.captured_logs() as logs:
            result = self.h.poller(**kw).poll_once()
        return result, logs.text

    def usage_records(self, session_id=None):
        return [(res, a) for res, a in self.h.records("cardinal.turn_usage")
                if session_id is None or a["session_id"] == session_id]

    def state_data(self):
        return PollState(self.h.state_path).data


class V1UsageTests(UsagePollCase):
    api = "v1"

    def _seed_v1(self) -> None:
        self.h.devin.v1_consumption = [
            {
                "session_name": "devin-v1-finished",
                "created_at": "2026-09-14T08:00:00Z",
                "acu_used": 4.75,
                "pull_requests": [{"pr_url": "https://github.com/acme/widgets/pull/42",
                                   "pr_status": "merged"}],
                "user_email": "alice@example.com",
            },
            {
                "session_name": "devin-v1-working",
                "created_at": "2026-09-14T09:00:00Z",
                "acu_used": 2.0,
                "pull_requests": [{"pr_url": "https://github.com/acme/widgets/pull/43",
                                   "pr_status": "open"}],
                "user_email": "bob@example.com",
            },
        ]

    def test_v1_emits_usage_and_persists_digests(self) -> None:
        self._seed_v1()
        result, _ = self.poll()
        by_sid = {a["session_id"]: (res, a) for res, a in self.usage_records()}
        self.assertEqual(set(by_sid), {"devin-v1-finished", "devin-v1-working"})
        _, finished = by_sid["devin-v1-finished"]
        self.assertEqual(finished["cardinal_billing_unit"], "acu")
        self.assertEqual(finished["cardinal_acu_total"], 4.75)
        self.assertEqual(finished["cardinal_pr_number"], 42)
        self.assertEqual(finished["cardinal_pr_url"], "https://github.com/acme/widgets/pull/42")
        self.assertGreaterEqual(result.usage, 2)
        digests = self.state_data()["usage_digests"]
        self.assertIn("devin-v1-finished", digests)
        self.assertIn("devin-v1-working", digests)

    def test_v1_unchanged_digest_does_not_reemit(self) -> None:
        self._seed_v1()
        self.poll()
        before = len(self.usage_records())
        self.assertGreater(before, 0)
        self.poll()
        self.assertEqual(len(self.usage_records()), before)

    def test_v1_changed_totals_reemits(self) -> None:
        self._seed_v1()
        self.poll()
        before = len(self.usage_records())
        self.h.devin.v1_consumption[0]["acu_used"] = 9.9
        self.poll()
        self.assertEqual(len(self.usage_records()), before + 1)
        latest = [(res, a) for res, a in self.usage_records("devin-v1-finished")][-1][1]
        self.assertEqual(latest["cardinal_acu_total"], 9.9)

    def test_v1_403_disables_usage_but_keeps_decisions(self) -> None:
        self._seed_v1()
        self.h.devin.consumption_status = 403
        result, logs = self.poll()
        self.assertEqual(self.usage_records(), [])
        self.assertIn("consumption scope", logs)
        # decisions still went out
        self.assertGreater(result.decisions, 0)
        self.assertTrue(self.state_data().get("usage_scope_warned"))
        # second cycle: no new warning printed, still no usage
        _, logs2 = self.poll()
        self.assertNotIn("consumption scope", logs2)
        self.assertEqual(self.usage_records(), [])

    def test_v1_no_usage_flag_skips_calls(self) -> None:
        self._seed_v1()
        self.poll(emit_usage=False)
        self.assertEqual(self.usage_records(), [])
        consumption_calls = [r for r in self.h.devin.requests
                             if r.path == "/v1/enterprise/consumption"]
        self.assertEqual(consumption_calls, [])


class V3UsageTests(UsagePollCase):
    api = "v3"

    def _seed_v3(self) -> None:
        self.h.devin.v3_consumption = {
            "v3exit": {
                "total_acus": 3.5,
                "consumption_by_date": [
                    {"date": "2026-09-14", "acus": 3.5,
                     "acus_by_product": {"cascade": 1.5, "devin": 1.5, "review": 0.4, "terminal": 0.1}},
                ],
            },
            "v3running": {
                "total_acus": 1.25,
                "consumption_by_date": [
                    {"date": "2026-09-14", "acus": 1.25,
                     "acus_by_product": {"cascade": 0.75, "devin": 0.5}},
                ],
            },
        }

    def test_v3_emits_per_session_usage(self) -> None:
        self._seed_v3()
        result, _ = self.poll()
        by_sid = {a["session_id"]: a for _, a in self.usage_records()}
        self.assertIn("v3exit", by_sid)
        self.assertEqual(by_sid["v3exit"]["cardinal_acu_total"], 3.5)
        self.assertEqual(by_sid["v3exit"]["cardinal_acu_cascade"], 1.5)
        self.assertEqual(by_sid["v3exit"]["cardinal_pr_number"], 42)
        self.assertGreater(result.usage, 0)
        paths = [r.path for r in self.h.devin.requests
                 if "/consumption/daily/sessions/" in r.path]
        self.assertTrue(any(p.endswith("/devin-v3exit") for p in paths))
        self.assertTrue(any(p.endswith("/devin-v3running") for p in paths))

    def test_v3_403_disables_usage_but_keeps_decisions(self) -> None:
        self._seed_v3()
        self.h.devin.consumption_status = 403
        result, logs = self.poll()
        self.assertEqual(self.usage_records(), [])
        self.assertIn("consumption scope", logs)
        self.assertGreater(result.decisions, 0)

    def test_v3_unchanged_digest_does_not_reemit(self) -> None:
        self._seed_v3()
        self.poll()
        before = len(self.usage_records())
        self.assertGreater(before, 0)
        # bump time; no data changes → no new usage records
        self.h.now += 600
        self.poll()
        self.assertEqual(len(self.usage_records()), before)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

import support  # noqa: F401
from support import load_fixture

from cardinal_devin.sessions import (
    Usage, normalize_v1_consumption, normalize_v3_consumption, to_ns,
)


class V1NormalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = load_fixture("consumption_v1.json")["sessions"]

    def test_reads_totals_and_pr_urls_and_email(self) -> None:
        usage = normalize_v1_consumption(self.rows[0])
        assert usage is not None
        self.assertEqual(usage.session_id, "devin-v1-finished")
        self.assertEqual(usage.acu_total, 4.75)
        self.assertIsNone(usage.acu_cascade)
        self.assertIsNone(usage.acu_devin)
        self.assertIsNone(usage.acu_review)
        self.assertIsNone(usage.acu_terminal)
        self.assertEqual(usage.pr_urls, ["https://github.com/acme/widgets/pull/42"])
        self.assertEqual(usage.user_email, "alice@example.com")
        self.assertEqual(usage.period_start_ns, to_ns("2026-09-14T08:00:00Z"))
        self.assertIsNone(usage.period_end_ns)

    def test_no_pull_requests_gives_empty_prs(self) -> None:
        usage = normalize_v1_consumption(self.rows[2])
        assert usage is not None
        self.assertEqual(usage.pr_urls, [])
        self.assertEqual(usage.acu_total, 0.25)

    def test_missing_session_name_is_skipped(self) -> None:
        self.assertIsNone(normalize_v1_consumption({"acu_used": 1.0}))
        self.assertIsNone(normalize_v1_consumption(None))
        self.assertIsNone(normalize_v1_consumption("string"))

    def test_missing_acu_used_defaults_to_zero(self) -> None:
        usage = normalize_v1_consumption({"session_name": "s"})
        assert usage is not None
        self.assertEqual(usage.acu_total, 0.0)


class V3NormalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bodies = load_fixture("consumption_v3.json")["sessions"]

    def test_sums_days_and_products(self) -> None:
        usage = normalize_v3_consumption("v3exit", self.bodies["v3exit"])
        assert usage is not None
        self.assertAlmostEqual(usage.acu_total, 3.5)
        self.assertAlmostEqual(usage.acu_cascade or 0.0, 1.5)
        self.assertAlmostEqual(usage.acu_devin or 0.0, 1.5)
        self.assertAlmostEqual(usage.acu_review or 0.0, 0.4)
        self.assertAlmostEqual(usage.acu_terminal or 0.0, 0.1)
        self.assertEqual(usage.period_start_ns, to_ns("2026-09-13"))
        self.assertEqual(usage.period_end_ns, to_ns("2026-09-14"))

    def test_missing_product_stays_none(self) -> None:
        usage = normalize_v3_consumption("v3running", self.bodies["v3running"])
        assert usage is not None
        self.assertIsNone(usage.acu_review)
        self.assertIsNone(usage.acu_terminal)
        self.assertAlmostEqual(usage.acu_cascade or 0.0, 0.75)
        self.assertAlmostEqual(usage.acu_devin or 0.0, 0.5)

    def test_no_products_at_all(self) -> None:
        usage = normalize_v3_consumption("v3suspended", self.bodies["v3suspended"])
        assert usage is not None
        self.assertIsNone(usage.acu_cascade)
        self.assertIsNone(usage.acu_devin)
        self.assertAlmostEqual(usage.acu_total, 0.5)

    def test_empty_consumption_by_date(self) -> None:
        usage = normalize_v3_consumption("v3empty", self.bodies["v3empty"])
        assert usage is not None
        self.assertEqual(usage.acu_total, 0.0)
        self.assertIsNone(usage.period_start_ns)
        self.assertIsNone(usage.period_end_ns)

    def test_non_object_response_is_none(self) -> None:
        self.assertIsNone(normalize_v3_consumption("s", None))
        self.assertIsNone(normalize_v3_consumption("s", []))

    def test_missing_total_falls_back_to_summed_days(self) -> None:
        body = {"consumption_by_date": [
            {"date": "2026-09-14", "acus": 1.5},
            {"date": "2026-09-15", "acus": 0.5},
        ]}
        usage = normalize_v3_consumption("s", body)
        assert usage is not None
        self.assertAlmostEqual(usage.acu_total, 2.0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

import support  # noqa: F401

from cardinal_devin.sessions import Usage
from cardinal_devin.telemetry import EVENT_USAGE, usage_attrs, usage_digest


def _usage(**overrides) -> Usage:
    fields = {
        "session_id": "s1",
        "acu_total": 3.5,
        "acu_cascade": 1.0,
        "acu_devin": 2.0,
        "acu_review": 0.5,
        "acu_terminal": None,
        "period_start_ns": 1000,
        "period_end_ns": 2000,
        "pr_urls": ["https://github.com/o/r/pull/1"],
        "user_email": "a@example.com",
    }
    fields.update(overrides)
    return Usage(**fields)


class UsageAttrsTests(unittest.TestCase):
    def test_event_name_and_billing_unit(self) -> None:
        self.assertEqual(EVENT_USAGE, "cardinal.turn_usage")

    def test_full_attrs_with_facts(self) -> None:
        facts = {
            "cardinal_repo": "o/r",
            "cardinal_branch": "feat/x",
            "cardinal_pr_number": 1,
            "cardinal_pr_url": "https://github.com/o/r/pull/1",
            "cardinal_initiative_name": "x",
            "cardinal_initiative_type": "feature",
            "cardinal_head_sha": "deadbeef",  # not carried over into usage
        }
        attrs = usage_attrs("s1", _usage(), facts)
        self.assertEqual(attrs, {
            "session_id": "s1",
            "cardinal_billing_unit": "acu",
            "cardinal_acu_total": 3.5,
            "cardinal_acu_cascade": 1.0,
            "cardinal_acu_devin": 2.0,
            "cardinal_acu_review": 0.5,
            "period_start_ns": 1000,
            "period_end_ns": 2000,
            "cardinal_repo": "o/r",
            "cardinal_branch": "feat/x",
            "cardinal_pr_number": 1,
            "cardinal_pr_url": "https://github.com/o/r/pull/1",
            "cardinal_initiative_name": "x",
            "cardinal_initiative_type": "feature",
        })

    def test_missing_facts_drops_keys(self) -> None:
        attrs = usage_attrs("s1", _usage(acu_terminal=None, acu_review=None), {})
        self.assertNotIn("cardinal_acu_review", attrs)
        self.assertNotIn("cardinal_acu_terminal", attrs)
        self.assertNotIn("cardinal_repo", attrs)
        self.assertEqual(attrs["cardinal_billing_unit"], "acu")


class UsageDigestTests(unittest.TestCase):
    def test_stable_for_same_input(self) -> None:
        self.assertEqual(
            usage_digest(_usage(), "https://github.com/o/r/pull/1"),
            usage_digest(_usage(), "https://github.com/o/r/pull/1"),
        )

    def test_changes_with_total(self) -> None:
        base = usage_digest(_usage(), None)
        other = usage_digest(_usage(acu_total=9.9), None)
        self.assertNotEqual(base, other)

    def test_changes_with_pr_url(self) -> None:
        a = usage_digest(_usage(), None)
        b = usage_digest(_usage(), "https://github.com/o/r/pull/2")
        self.assertNotEqual(a, b)

    def test_changes_with_product_split(self) -> None:
        a = usage_digest(_usage(acu_cascade=1.0), None)
        b = usage_digest(_usage(acu_cascade=2.0), None)
        self.assertNotEqual(a, b)

    def test_changes_with_period_start(self) -> None:
        a = usage_digest(_usage(period_start_ns=1000), None)
        b = usage_digest(_usage(period_start_ns=2000), None)
        self.assertNotEqual(a, b)

    def test_changes_with_period_end(self) -> None:
        a = usage_digest(_usage(period_end_ns=2000), None)
        b = usage_digest(_usage(period_end_ns=3000), None)
        self.assertNotEqual(a, b)

    def test_same_totals_new_period_reemits(self) -> None:
        old_period = usage_digest(_usage(period_start_ns=1000, period_end_ns=2000), None)
        new_period = usage_digest(_usage(period_start_ns=3000, period_end_ns=4000), None)
        self.assertNotEqual(old_period, new_period)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

import support  # noqa: F401

from cardinal_devin.sessions import normalize, parse_pr_url, to_ns
from cardinal_devin.telemetry import fingerprint, git_state_attrs, pr_facts

TEN_AM = 1789380000  # 2026-09-14T10:00:00Z


class TimestampTests(unittest.TestCase):
    def test_iso_and_numeric(self) -> None:
        ns = TEN_AM * 10 ** 9
        self.assertEqual(to_ns("2026-09-14T10:00:00Z"), ns)
        self.assertEqual(to_ns("2026-09-14T10:00:00.5Z"), ns + 500_000_000)
        self.assertEqual(to_ns("2026-09-14T10:00:00.123456789+00:00"), ns + 123_456_000)
        self.assertEqual(to_ns(TEN_AM), ns)
        self.assertEqual(to_ns(TEN_AM * 1000), ns)
        self.assertEqual(to_ns(ns), ns)
        for bad in (None, "", "yesterday", True, -1):
            self.assertIsNone(to_ns(bad))


class NormalizeTests(unittest.TestCase):
    def test_v1_terminal_states(self) -> None:
        for status, terminal in (("finished", True), ("expired", True), ("working", False),
                                 ("blocked", False), ("suspend_requested", False), ("resumed", False)):
            s = normalize({"session_id": "a", "status_enum": status}, "v1")
            self.assertEqual(s.terminal, terminal, status)

    def test_v1_fields(self) -> None:
        s = normalize({
            "session_id": "a", "status_enum": "working", "requesting_user_email": "a@example.com",
            "pull_request": {"url": "https://github.com/o/r/pull/1"}, "structured_output": None,
            "updated_at": "2026-09-14T10:00:00Z",
        }, "v1")
        self.assertEqual(s.email, "a@example.com")
        self.assertEqual(s.pr_urls, ["https://github.com/o/r/pull/1"])
        self.assertFalse(s.has_structured_output)
        self.assertEqual(s.updated_ns, TEN_AM * 10 ** 9)

    def test_v1_detail_keeps_carried_email(self) -> None:
        s = normalize({"session_id": "a", "status_enum": "finished"}, "v1", email="carry@example.com")
        self.assertEqual(s.email, "carry@example.com")

    def test_v3_terminal_states(self) -> None:
        cases = (
            ("exit", None, True), ("error", "error", True), ("running", "working", False),
            ("suspended", "inactivity", False), ("suspended", "finished", True), ("new", None, False),
        )
        for status, detail, terminal in cases:
            s = normalize({"session_id": "a", "status": status, "status_detail": detail}, "v3")
            self.assertEqual(s.terminal, terminal, (status, detail))

    def test_v3_multiple_prs_deduped(self) -> None:
        s = normalize({"session_id": "a", "status": "running", "user_id": "u", "pull_requests": [
            {"pr_url": "https://github.com/o/r/pull/1", "pr_state": "open"},
            {"pr_url": "https://github.com/o/r/pull/1", "pr_state": "open"},
            {"pr_url": "https://github.com/o/other/pull/2", "pr_state": None},
            {"pr_state": "closed"},
        ]}, "v3")
        self.assertEqual(s.pr_urls, ["https://github.com/o/r/pull/1", "https://github.com/o/other/pull/2"])
        self.assertEqual(s.user_id, "u")
        self.assertIsNone(s.email)

    def test_missing_session_id(self) -> None:
        self.assertIsNone(normalize({"status": "exit"}, "v3"))
        self.assertIsNone(normalize(None, "v1"))


class PullRequestTests(unittest.TestCase):
    def test_parse(self) -> None:
        gh = parse_pr_url("https://github.com/Acme/widgets/pull/42/files")
        self.assertEqual((gh.host, gh.owner, gh.repo, gh.number, gh.kind), ("github.com", "Acme", "widgets", 42, "github"))
        gl = parse_pr_url("https://gitlab.com/group/sub/repo/-/merge_requests/9")
        self.assertEqual((gl.owner, gl.repo, gl.number, gl.kind), ("group/sub", "repo", 9, "gitlab"))
        self.assertIsNone(parse_pr_url("https://bitbucket.org/o/r/pull-requests/3"))
        self.assertIsNone(parse_pr_url(None))

    def test_facts_without_token(self) -> None:
        facts = pr_facts("https://github.com/acme/widgets/pull/42", None)
        attrs = {k: v for k, v in git_state_attrs("s", facts).items() if v is not None}
        self.assertEqual(attrs, {
            "session_id": "s",
            "cardinal_repo": "acme/widgets",
            "cardinal_remote_url": "https://github.com/acme/widgets.git",
            "cardinal_pr_number": 42,
            "cardinal_pr_url": "https://github.com/acme/widgets/pull/42",
        })

    def test_facts_gitlab_and_unparseable(self) -> None:
        gl = pr_facts("https://gitlab.com/group/sub/repo/-/merge_requests/9", None)
        self.assertEqual(gl["cardinal_repo"], "group/sub/repo")
        other = pr_facts("https://example.com/x", None)
        self.assertEqual({k: v for k, v in other.items() if v is not None}, {"cardinal_pr_url": "https://example.com/x"})

    def test_fingerprint_ignores_empty_values(self) -> None:
        self.assertEqual(fingerprint({"a": 1, "b": None}), fingerprint({"a": 1}))
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))


if __name__ == "__main__":
    unittest.main()

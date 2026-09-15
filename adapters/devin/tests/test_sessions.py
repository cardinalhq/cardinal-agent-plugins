from __future__ import annotations

import unittest

import support
from support import FakeGitHub

from cardinal_devin.client import GitHubClient, JsonHttp
from cardinal_devin.sessions import normalize, parse_pr_url, primary_pr_url, to_ns
from cardinal_devin.telemetry import fingerprint, git_state_attrs, pr_facts

TEN_AM = 1789380000  # 2026-09-14T10:00:00Z


class TimestampTests(unittest.TestCase):
    def test_iso_and_numeric(self) -> None:
        ns = TEN_AM * 10 ** 9
        self.assertEqual(to_ns("2026-09-14T10:00:00Z"), ns)
        self.assertEqual(to_ns("2026-09-14T10:00:00.5Z"), ns + 500_000_000)
        self.assertEqual(to_ns("2026-09-14T10:00:00.123456789+00:00"), ns + 123_456_000)
        self.assertEqual(to_ns("2026-09-14T10:00:00+0000"), ns)
        self.assertEqual(to_ns("2026-09-14T05:00:00-0500"), ns)
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

    def test_primary_pr_is_highest_number(self) -> None:
        self.assertIsNone(primary_pr_url([]))
        urls = ["https://github.com/o/r/pull/9", "https://github.com/o/r/pull/12", "https://example.com/x"]
        self.assertEqual(primary_pr_url(urls), "https://github.com/o/r/pull/12")
        self.assertEqual(primary_pr_url(list(reversed(urls))), "https://github.com/o/r/pull/12")
        self.assertEqual(primary_pr_url(["https://example.com/b", "https://example.com/a"]), "https://example.com/b")

    def test_fingerprint_ignores_empty_values(self) -> None:
        self.assertEqual(fingerprint({"a": 1, "b": None}), fingerprint({"a": 1}))
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))


class PullRequestHostTests(unittest.TestCase):
    def setUp(self) -> None:
        pull = {
            "number": 7,
            "head": {"ref": "feat/ghe-thing", "sha": "a" * 40},
            "base": {"repo": {"full_name": "acme/app", "clone_url": "https://ghe.corp/acme/app.git"}},
        }
        self.server = FakeGitHub({"acme/app/7": pull})
        self.addCleanup(self.server.close)

    def client(self, web_host=None) -> GitHubClient:
        return GitHubClient("gh-test", base_url=self.server.url, web_host=web_host, http=JsonHttp(timeout=5.0))

    def test_other_host_is_not_looked_up(self) -> None:
        facts = pr_facts("https://ghe.corp/acme/app/pull/7", self.client(web_host="github.com"))
        self.assertEqual(self.server.requests, [])
        self.assertIsNone(facts["cardinal_branch"])
        self.assertEqual(facts["cardinal_remote_url"], "https://ghe.corp/acme/app.git")
        self.assertEqual((facts["cardinal_repo"], facts["cardinal_pr_number"]), ("acme/app", 7))

    def test_default_api_does_not_serve_ghe(self) -> None:
        with support.captured_logs():
            facts = pr_facts("https://ghe.corp/acme/app/pull/7", GitHubClient("gh-test"))  # no request is made
        self.assertIsNone(facts["cardinal_branch"])

    def test_matching_ghe_host_is_looked_up(self) -> None:
        facts = pr_facts("https://ghe.corp/acme/app/pull/7", self.client(web_host="ghe.corp"))
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(facts["cardinal_branch"], "feat/ghe-thing")
        self.assertEqual(facts["cardinal_remote_url"], "https://ghe.corp/acme/app.git")

    def test_response_cannot_move_repo_to_another_host(self) -> None:
        self.server.pulls["acme/app/7"]["base"]["repo"]["clone_url"] = "https://github.com/other/app.git"
        facts = pr_facts("https://ghe.corp/acme/app/pull/7", self.client(web_host="ghe.corp"))
        self.assertEqual(facts["cardinal_remote_url"], "https://ghe.corp/acme/app.git")
        self.assertEqual(facts["cardinal_repo"], "acme/app")
        self.assertEqual(facts["cardinal_branch"], "feat/ghe-thing")


if __name__ == "__main__":
    unittest.main()

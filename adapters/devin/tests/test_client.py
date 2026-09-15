from __future__ import annotations

import unittest
from unittest import mock

import support  # noqa: F401  (sets import paths)
from support import FakeDevin, FakeGitHub

from cardinal_devin.client import (
    ApiError, DevinClient, GitHubClient, JsonHttp, github_web_host, parse_retry_after,
)


def _sessions(n: int, *, v3: bool = False):
    return [
        {"session_id": f"s{i}", "status_enum": "working", "updated_at": 1789380000 + i}
        if v3 else {"session_id": f"s{i}", "status_enum": "working"}
        for i in range(n)
    ]


class ClientTests(unittest.TestCase):
    def fake(self, api: str, sessions, **kw) -> FakeDevin:
        server = FakeDevin(api, sessions, **kw)
        self.addCleanup(server.close)
        return server

    def client(self, server: FakeDevin, *, page_size: int = 2, max_retries: int = 3, sleeps=None, **kw) -> DevinClient:
        http = JsonHttp(timeout=5.0, max_retries=max_retries, sleep=(sleeps if sleeps is not None else []).append)
        return DevinClient(
            "cog_test", base_url=server.url, org_id="org-test" if server.api == "v3" else None,
            page_size=page_size, http=http, **kw,
        )

    def test_v1_pagination_walks_offsets(self) -> None:
        server = self.fake("v1", _sessions(5))
        ids = [s["session_id"] for s in self.client(server).iter_sessions()]
        self.assertEqual(ids, ["s0", "s1", "s2", "s3", "s4"])
        self.assertEqual([r.query["offset"][0] for r in server.requests], ["0", "2", "4"])
        self.assertEqual({r.query["limit"][0] for r in server.requests}, {"2"})

    def test_v1_pagination_stops_on_empty_page(self) -> None:
        server = self.fake("v1", _sessions(4))
        self.assertEqual(len(list(self.client(server).iter_sessions())), 4)
        self.assertEqual([r.query["offset"][0] for r in server.requests], ["0", "2", "4"])

    def test_v1_max_pages_marks_truncated(self) -> None:
        server = self.fake("v1", _sessions(10))
        client = self.client(server, max_pages=2)
        with self.assertLogs("cardinal_devin", "WARNING"):
            self.assertEqual(len(list(client.iter_sessions())), 4)
        self.assertTrue(client.last_list_truncated)

    def test_v3_cursor_pagination_and_updated_after(self) -> None:
        server = self.fake("v3", _sessions(3, v3=True))
        client = self.client(server)
        ids = [s["session_id"] for s in client.iter_sessions(updated_after=1789380000)]
        self.assertEqual(ids, ["s0", "s1", "s2"])
        self.assertEqual(len(server.requests), 2)
        self.assertNotIn("after", server.requests[0].query)
        self.assertEqual(server.requests[1].query["after"], ["2"])
        self.assertEqual(server.requests[0].query["updated_after"], ["1789380000"])
        self.assertEqual(server.requests[0].path, "/v3/organizations/org-test/sessions")

    def test_auto_version_and_v3_needs_org(self) -> None:
        self.assertEqual(DevinClient("k").api_version, "v1")
        self.assertEqual(DevinClient("k", org_id="o").api_version, "v3")
        with self.assertRaises(ValueError):
            DevinClient("k", api_version="v3")

    def test_429_honours_retry_after(self) -> None:
        server = self.fake("v1", _sessions(1))
        server.rate_limit_remaining = 2
        server.retry_after = "7"
        sleeps: list = []
        with self.assertLogs("cardinal_devin", "WARNING"):
            ids = [s["session_id"] for s in self.client(server, sleeps=sleeps).iter_sessions()]
        self.assertEqual(ids, ["s0"])
        self.assertEqual(sleeps, [7.0, 7.0])

    def test_429_without_header_backs_off_exponentially(self) -> None:
        server = self.fake("v1", _sessions(1))
        server.rate_limit_remaining = 2
        server.retry_after = None
        sleeps: list = []
        with self.assertLogs("cardinal_devin", "WARNING"):
            list(self.client(server, sleeps=sleeps).iter_sessions())
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_429_gives_up_after_max_retries(self) -> None:
        server = self.fake("v1", _sessions(1))
        server.rate_limit_remaining = 10
        sleeps: list = []
        with self.assertLogs("cardinal_devin", "WARNING"), self.assertRaises(ApiError) as ctx:
            list(self.client(server, max_retries=2, sleeps=sleeps).iter_sessions())
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(len(sleeps), 2)

    def test_retry_after_http_date(self) -> None:
        self.assertEqual(parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470.0), 10.0)
        self.assertEqual(parse_retry_after("3"), 3.0)
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after(None))

    def test_retry_delay_is_capped(self) -> None:
        server = self.fake("v1", _sessions(1))
        server.rate_limit_remaining = 1
        server.retry_after = "3600"
        sleeps: list = []
        http = JsonHttp(timeout=5.0, max_backoff=30.0, sleep=sleeps.append)
        with self.assertLogs("cardinal_devin", "WARNING"):
            list(DevinClient("cog_test", base_url=server.url, http=http).iter_sessions())
        self.assertEqual(sleeps, [30.0])

    def test_v1_missing_session_422_is_not_found(self) -> None:
        server = self.fake("v1", _sessions(1))
        client = self.client(server)
        self.assertEqual(client.get_session("s0")["messages"], [])
        with self.assertLogs("cardinal_devin", "WARNING") as logs:
            self.assertIsNone(client.get_session("missing"))
        self.assertIn("returned 422 for session missing", logs.output[0])
        self.assertIn("Session not found", logs.output[0])  # the body is logged so real validation errors show
        self.assertEqual(server.requests[-1].path, "/v1/sessions/missing")

    def test_v3_first_page_is_unfiltered(self) -> None:
        server = self.fake("v3", _sessions(3, v3=True))
        client = self.client(server)
        self.assertEqual([s["session_id"] for s in client.first_page()], ["s0", "s1"])
        self.assertEqual(server.requests[0].query, {"first": ["2"]})
        self.assertFalse(client.last_list_truncated)
        self.assertEqual(self.client(self.fake("v1", _sessions(1))).first_page(), [])

    def test_v3_422_is_an_error(self) -> None:
        server = self.fake("v3", [])
        with mock.patch.object(server, "handle", return_value=(422, {}, {"detail": []})):
            with self.assertRaises(ApiError) as ctx:
                self.client(server).get_session("abc")
        self.assertEqual(ctx.exception.status, 422)

    def test_v3_detail_path_uses_devin_prefix(self) -> None:
        server = self.fake("v3", [{"session_id": "abc123", "status": "running", "updated_at": 1789380000}])
        client = self.client(server)
        self.assertEqual(client.get_session("abc123")["session_id"], "abc123")
        self.assertEqual(client.get_session("devin-abc123")["session_id"], "abc123")
        self.assertIsNone(client.get_session("zzz"))
        self.assertEqual([r.path.rsplit("/", 1)[-1] for r in server.requests],
                         ["devin-abc123", "devin-abc123", "devin-zzz"])

    def test_bad_key_raises(self) -> None:
        server = self.fake("v1", _sessions(1), api_key="other")
        with self.assertRaises(ApiError) as ctx:
            list(self.client(server).iter_sessions())
        self.assertEqual(ctx.exception.status, 401)

    def test_user_email_lookup_v3(self) -> None:
        server = self.fake("v3", [], users={"u1": {"user_id": "u1", "email": "u1@example.com", "name": None}})
        client = self.client(server)
        self.assertEqual(client.get_user_email("u1"), "u1@example.com")
        self.assertIsNone(client.get_user_email("u2"))
        self.assertEqual(server.requests[0].path, "/v3beta1/organizations/org-test/members/users/u1")

    def test_github_web_host(self) -> None:
        self.assertEqual(github_web_host("https://api.github.com"), "github.com")
        self.assertEqual(github_web_host("https://ghe.corp/api/v3"), "ghe.corp")
        self.assertEqual(github_web_host("https://api.acme.ghe.com"), "acme.ghe.com")
        self.assertTrue(GitHubClient("t").serves("GitHub.com"))
        self.assertFalse(GitHubClient("t").serves("ghe.corp"))
        self.assertTrue(GitHubClient("t", base_url="https://ghe.corp/api/v3").serves("ghe.corp"))

    def test_github_get_pull(self) -> None:
        server = FakeGitHub({"acme/w/7": {"number": 7, "head": {"ref": "fix/x", "sha": "abc"}}})
        self.addCleanup(server.close)
        gh = GitHubClient("gh-test", base_url=server.url, http=JsonHttp(timeout=5.0))
        self.assertEqual(gh.get_pull("acme", "w", 7)["head"]["ref"], "fix/x")
        self.assertIsNone(gh.get_pull("acme", "w", 8))
        self.assertEqual(server.requests[0].headers["x-github-api-version"], "2022-11-28")


if __name__ == "__main__":
    unittest.main()

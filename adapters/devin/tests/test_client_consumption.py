from __future__ import annotations

import unittest

import support  # noqa: F401
from support import FakeDevin

from cardinal_devin.client import ApiError, DevinClient, JsonHttp


class ClientConsumptionTests(unittest.TestCase):
    def fake(self, api: str, **kw) -> FakeDevin:
        server = FakeDevin(api, [], **kw)
        self.addCleanup(server.close)
        return server

    def client(self, server: FakeDevin) -> DevinClient:
        http = JsonHttp(timeout=5.0, max_retries=0, sleep=[].append)
        return DevinClient(
            "cog_test", base_url=server.url,
            org_id="org-test" if server.api == "v3" else None,
            page_size=2, http=http,
        )

    def test_v1_consumption_yields_sessions(self) -> None:
        server = self.fake("v1")
        server.v1_consumption = [
            {"session_name": "s1", "acu_used": 1.0},
            {"session_name": "s2", "acu_used": 2.0},
        ]
        rows = list(self.client(server).iter_v1_enterprise_consumption())
        self.assertEqual([r["session_name"] for r in rows], ["s1", "s2"])
        self.assertEqual(server.requests[-1].path, "/v1/enterprise/consumption")

    def test_v1_consumption_sends_dates_when_provided(self) -> None:
        server = self.fake("v1")
        server.v1_consumption = []
        list(self.client(server).iter_v1_enterprise_consumption(
            start="2026-09-01T00:00:00Z", end="2026-09-30T23:59:59Z",
        ))
        query = server.requests[-1].query
        self.assertEqual(query["start_date"], ["2026-09-01T00:00:00Z"])
        self.assertEqual(query["end_date"], ["2026-09-30T23:59:59Z"])
        self.assertEqual(query["start"], ["2026-09-01T00:00:00Z"])
        self.assertEqual(query["end"], ["2026-09-30T23:59:59Z"])

    def test_v1_consumption_401_bubbles_up(self) -> None:
        server = self.fake("v1")
        server.consumption_status = 401
        with self.assertRaises(ApiError) as ctx:
            list(self.client(server).iter_v1_enterprise_consumption())
        self.assertEqual(ctx.exception.status, 401)

    def test_v1_consumption_403_bubbles_up(self) -> None:
        server = self.fake("v1")
        server.consumption_status = 403
        with self.assertRaises(ApiError) as ctx:
            list(self.client(server).iter_v1_enterprise_consumption())
        self.assertEqual(ctx.exception.status, 403)

    def test_v3_consumption_prepends_devin_prefix(self) -> None:
        server = self.fake("v3")
        server.v3_consumption = {"abc123": {"total_acus": 1.0, "consumption_by_date": []}}
        client = self.client(server)
        self.assertEqual(client.get_v3_session_consumption("abc123")["total_acus"], 1.0)
        self.assertEqual(client.get_v3_session_consumption("devin-abc123")["total_acus"], 1.0)
        paths = [r.path for r in server.requests]
        self.assertTrue(all(p.endswith("/devin-abc123") for p in paths))

    def test_v3_consumption_missing_is_none(self) -> None:
        server = self.fake("v3")
        self.assertIsNone(self.client(server).get_v3_session_consumption("nope"))

    def test_v3_consumption_401_bubbles_up(self) -> None:
        server = self.fake("v3")
        server.v3_consumption = {"s": {"total_acus": 0.0, "consumption_by_date": []}}
        server.consumption_status = 401
        with self.assertRaises(ApiError) as ctx:
            self.client(server).get_v3_session_consumption("s")
        self.assertEqual(ctx.exception.status, 401)

    def test_v3_consumption_passes_time_window(self) -> None:
        server = self.fake("v3")
        server.v3_consumption = {"s": {"total_acus": 0.0, "consumption_by_date": []}}
        self.client(server).get_v3_session_consumption("s", time_after=100, time_before=200)
        query = server.requests[-1].query
        self.assertEqual(query["time_after"], ["100"])
        self.assertEqual(query["time_before"], ["200"])

    def test_v3_consumption_org_and_session_are_url_encoded(self) -> None:
        http = JsonHttp(timeout=5.0, sleep=[].append)

        captured: list = []

        class Recorder:
            def get(self, url, headers):
                captured.append(url)
                return {"total_acus": 0.0, "consumption_by_date": []}

        client = DevinClient(
            "cog_test", base_url="http://x", org_id="org test/1", page_size=2, http=Recorder(),
        )
        client.get_v3_session_consumption("abc/def")
        self.assertIn("/v3/organizations/org%20test%2F1/consumption/daily/sessions/devin-abc%2Fdef",
                      captured[-1])


if __name__ == "__main__":
    unittest.main()

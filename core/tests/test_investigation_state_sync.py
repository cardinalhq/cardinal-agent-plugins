"""cardinal_core.investigation_state_sync: the client attestation the server
stores with an InvestigationState, and the put-state / get-state client."""

from __future__ import annotations

import copy
import json
import unittest

from cardinal_core import investigation_state as ist
from cardinal_core import investigation_state_sync as sync

from tests.test_investigation_state import SESSIONS, T_GO, T_PROPOSE, T_RULE, authored, owner, storyboard


class QuoteKey(unittest.TestCase):
    def test_normalizes_like_the_oracle_and_binds_role_and_time(self):
        k = sync.quote_key("owner", T_GO, "Go  with the “cache”.")
        self.assertEqual(k, sync.quote_key("owner", T_GO, 'Go with the "cache".'))
        self.assertNotEqual(k, sync.quote_key("agent", T_GO, 'Go with the "cache".'))
        self.assertNotEqual(k, sync.quote_key("owner", T_RULE, 'Go with the "cache".'))

    def test_pinned_vector_shared_with_the_server(self):
        # maestro storyboard/investigation-state.ts quoteKey computes the same
        # key (its parity vectors pin more): sha256("owner\nAT\nQUOTE").
        self.assertEqual(sync.quote_key("owner", "2026-10-01T01:00:00.000Z", "Go with the cache."),
                         "bdac7f9ccc740c31269fe0be5e71fd48e5c54f0570ec1817332e9675d659f2fb")


class Attest(unittest.TestCase):
    def setUp(self):
        self.state = authored(ist.project(storyboard()))

    def test_attests_exactly_the_quotes_the_transcript_verifies(self):
        att = sync.attest(self.state, SESSIONS)
        self.assertEqual((att["checker"], att["schema"]), (sync.CHECKER, ist.SCHEMA))
        keys = {(q["role"], q["key"]) for q in att["quotes"]}
        self.assertEqual(keys, {("owner", sync.quote_key("owner", T_RULE, "Admitted query shapes must stay exact.")),
                                ("owner", sync.quote_key("owner", T_GO, "Go with the cache."))})
        self.assertTrue(all(q["session"] == "s1" and len(q["utterance_sha256"]) == 64 for q in att["quotes"]))

    def test_leaves_out_what_was_not_said(self):
        s = copy.deepcopy(self.state)
        s["decisions"][0]["source"] = [owner(T_GO, "Go with the cache and delete the volumes.")]
        att = sync.attest(s, SESSIONS)
        self.assertNotIn(sync.quote_key("owner", T_GO, "Go with the cache and delete the volumes."),
                         {q["key"] for q in att["quotes"]})

    def test_no_transcript_attests_nothing(self):
        self.assertEqual(sync.attest(self.state, None)["quotes"], [])

    def test_a_malformed_state_attests_nothing_and_does_not_crash(self):
        s = copy.deepcopy(self.state)
        s["actors"] = 7
        self.assertEqual(sync.attest(s, SESSIONS)["quotes"], [])

    def test_plan_approval(self):
        s = copy.deepcopy(self.state)
        plan = {"kind": "message", "from": "agent", "at": T_PROPOSE, "quote": "I propose caching preparation by segment identity."}
        reply = owner(T_GO, "Go with the cache.")
        d = s["decisions"][0]
        d.pop("source")
        d["approval"] = {"kind": "plan_approval", "by": "owner", "plan_source": plan, "approval_source": reply}
        self.assertEqual(ist.check(s, storyboard(), SESSIONS)["errors"], [])
        att = sync.attest(s, SESSIONS)
        self.assertEqual(att["plan_approvals"], [{"key": sync.plan_approval_key(plan, reply), "session": "s1"}])


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self.answer, Exception):
            raise self.answer
        return FakeResponse(json.dumps(self.answer).encode())


CONN = {"origin": "https://app.example.test", "org": "o1", "key": "ck"}
SB = "sb_0123456789abcdef01234567"


class Client(unittest.TestCase):
    def test_put_state_posts_the_document_base_version_and_attestation(self):
        state = authored(ist.project(storyboard()))
        op = FakeOpener({"storyboard_id": SB, "version": 1, "etag": "e"})
        out = sync.put_state(CONN, state, base_version=0, attestation={"quotes": []}, client="c", opener=op)
        self.assertEqual(out["version"], 1)
        req = op.requests[0]
        self.assertEqual(req.full_url, "https://app.example.test/api/orgs/o1/storyboards/mcp-tools/put-state")
        self.assertEqual(req.get_header("X-cardinalhq-api-key"), "ck")
        body = json.loads(req.data)
        self.assertEqual((body["storyboard_id"], body["base_version"], body["state"]), (SB, 0, state))
        self.assertNotIn("allow_removed", body)

    def test_server_errors_carry_status_and_body(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("u", 409, "Conflict", {}, io.BytesIO(b'{"error":"version_conflict","current_version":2}'))
        with self.assertRaises(sync.ServerError) as cm:
            sync.put_state(CONN, {"source": {"storyboard_id": SB}}, base_version=1, attestation={}, client="c", opener=FakeOpener(err))
        self.assertEqual((cm.exception.status, cm.exception.body["current_version"]), (409, 2))

    def test_get_state_refuses_another_storyboard_and_bad_ids(self):
        with self.assertRaises(ist.FetchError):
            sync.get_state(CONN, "nope", client="c", opener=FakeOpener({}))
        with self.assertRaises(ist.FetchError):
            sync.get_state(CONN, SB, client="c", opener=FakeOpener({"storyboard_id": "sb_" + "f" * 24, "state": {}}))
        got = sync.get_state(CONN, SB, client="c", opener=FakeOpener({"storyboard_id": SB, "state": {"schema": ist.SCHEMA}}))
        self.assertEqual(got["state"]["schema"], ist.SCHEMA)

    def test_not_connected(self):
        with self.assertRaises(ist.FetchError):
            sync.get_state({}, SB, client="c")


if __name__ == "__main__":
    unittest.main()

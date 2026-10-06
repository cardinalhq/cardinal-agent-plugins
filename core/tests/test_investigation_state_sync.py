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


INV = "inv_" + "a" * 24


def http_error(status: int, body: bytes):
    import io
    import urllib.error
    return urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(body))


class InvestigationClient(unittest.TestCase):
    """The investigation routes, and put-state / get-state addressed by
    investigation_id (exactly one of investigation_id | storyboard_id)."""

    def test_put_state_by_investigation_sends_its_id_and_no_storyboard_id(self):
        state = ist.project(ist.virtual_storyboard({"question": "Why?"}))
        op = FakeOpener({"investigation_id": INV, "storyboard_id": None, "version": 1, "etag": "e"})
        sync.put_state(CONN, state, base_version=0, attestation={"quotes": []}, client="c", opener=op, investigation_id=INV)
        body = json.loads(op.requests[0].data)
        self.assertEqual((body["investigation_id"], body["base_version"], body["state"]), (INV, 0, state))
        self.assertNotIn("storyboard_id", body)
        self.assertNotIn("investigation_id", body["state"])  # the document schema is frozen

    def test_put_state_by_storyboard_is_unchanged(self):
        state = ist.project(storyboard())
        op = FakeOpener({"storyboard_id": SB, "version": 1, "etag": "e"})
        sync.put_state(CONN, state, base_version=0, attestation={}, client="c", opener=op)
        self.assertEqual(sorted(json.loads(op.requests[0].data)), ["attestation", "base_version", "state", "storyboard_id"])

    def test_get_state_takes_exactly_one_id_and_refuses_another_investigation(self):
        for kw in ({}, {"storyboard_id": SB, "investigation_id": INV}):
            with self.assertRaises(ist.FetchError):
                sync.get_state(CONN, client="c", opener=FakeOpener({}), **kw)
        with self.assertRaises(ist.FetchError):
            sync.get_state(CONN, investigation_id="inv_nope", client="c", opener=FakeOpener({}))
        with self.assertRaises(ist.FetchError):
            sync.get_state(CONN, investigation_id=INV, client="c",
                           opener=FakeOpener({"investigation_id": "inv_" + "b" * 24, "state": {}}))
        op = FakeOpener({"investigation_id": INV, "storyboard_id": None, "state": {"schema": ist.SCHEMA}})
        self.assertEqual(sync.get_state(CONN, investigation_id=INV, client="c", opener=op)["state"]["schema"], ist.SCHEMA)
        self.assertEqual(json.loads(op.requests[0].data), {"investigation_id": INV})

    def test_create_get_attach(self):
        op = FakeOpener({"investigation_id": INV, "author": {"kind": "user", "id": "u1"}})
        sync.create_investigation(CONN, "Why?", {"start": "a", "end": "b"}, client="c", opener=op)
        self.assertTrue(op.requests[0].full_url.endswith("/storyboards/mcp-tools/create-investigation"))
        self.assertEqual(json.loads(op.requests[0].data), {"question": "Why?", "window": {"start": "a", "end": "b"}})
        for bad in ("", " ", "x" * 2001):
            with self.assertRaises(ist.FetchError):
                sync.create_investigation(CONN, bad, client="c", opener=op)
        with self.assertRaises(ist.FetchError):
            sync.create_investigation(CONN, "Why?", ["a"], client="c", opener=op)
        with self.assertRaises(ist.FetchError):
            sync.create_investigation(CONN, "Why?", client="c", opener=FakeOpener({"investigation_id": "x"}))
        with self.assertRaises(ist.FetchError):
            sync.get_investigation(CONN, INV, client="c", opener=FakeOpener({"investigation_id": "inv_" + "b" * 24}))
        with self.assertRaises(ist.FetchError):
            sync.get_investigation(CONN, INV, client="c", opener=FakeOpener({"investigation_id": INV, "storyboard_id": "x"}))
        op = FakeOpener({"investigation_id": INV, "storyboard_id": SB})
        sync.attach_storyboard(CONN, INV, SB, client="c", opener=op)
        self.assertEqual(json.loads(op.requests[0].data), {"investigation_id": INV, "storyboard_id": SB})
        with self.assertRaises(ist.FetchError):
            sync.attach_storyboard(CONN, INV, "sb_x", client="c", opener=op)

    def refusal(self, status: int, body: bytes) -> sync.ServerError:
        with self.assertRaises(sync.ServerError) as cm:
            sync.get_investigation(CONN, INV, client="c", opener=FakeOpener(http_error(status, body)))
        return cm.exception

    def test_an_older_server_is_recognized_never_a_raw_status(self):
        # The plugin key's allowlist refuses an unknown route; or the route is
        # missing; or (maestro with state but no investigations) the strict
        # put-state / get-state body refuses investigation_id.
        for status, body in ((403, b'{"error":"insufficient_scope"}'), (404, b"<html>Cannot POST</html>"),
                             (404, b'{"error":"not_found"}'),
                             # zod v4 (maestro), as formatZodIssues flattens it; and zod v3's wording.
                             (400, b'{"error":"invalid_body","issues":[{"path":"storyboard_id","message":"Invalid input: '
                                   b'expected string, received undefined"},{"path":"","message":"Unrecognized key: '
                                   b'\\"investigation_id\\""}]}'),
                             (400, b'{"error":"invalid_body","issues":[{"path":"","message":"Unrecognized keys: \\"x\\", '
                                   b'\\"investigation_id\\""}]}'),
                             (400, b'{"error":"invalid_body","issues":[{"message":"Unrecognized key(s) in object: '
                                   b'\'investigation_id\'"}]}')):
            self.assertTrue(sync.investigations_unsupported(self.refusal(status, body)), (status, body))
        for status, body in ((404, b'{"error":"investigation_not_found"}'), (403, b'{"error":"not_investigation_author"}'),
                             (409, b'{"error":"version_conflict","current_version":2}'),
                             (400, b'{"error":"invalid_body","issues":[{"path":"question"}]}'),
                             # A real refusal that merely mentions investigation_id is not an old server.
                             (400, b'{"error":"invalid_body","issues":[{"path":"investigation_id","message":"expected '
                                   b'inv_ followed by 24 hex characters"}]}'),
                             (400, b'{"error":"invalid_body","issues":[{"path":"state","message":"Unrecognized key: '
                                   b'\\"investigation_id\\""}]}'),
                             (400, b'{"error":"invalid_attestation","issues":[{"path":"","message":"Unrecognized key: '
                                   b'\\"investigation_id\\""}]}')):
            self.assertFalse(sync.investigations_unsupported(self.refusal(status, body)), (status, body))

    def test_refusals_in_words(self):
        e = self.refusal(403, b'{"error":"not_investigation_author"}')
        self.assertEqual(sync.plain(e), "Cardinal refused (only the investigation's author can do that "
                                        "(it was created by another user or key))")
        self.assertIn("another investigation", sync.plain(self.refusal(409, b'{"error":"storyboard_attached_elsewhere"}')))
        self.assertIn("daily limit", sync.plain(self.refusal(429, b'{"error":"quota_exceeded"}')))
        self.assertIn("/cardinal:connect", sync.plain(self.refusal(403, b'{"error":"no_principal"}')))
        self.assertEqual(sync.plain(self.refusal(500, b'{"message":"try later"}')), "Cardinal refused (try later)")
        self.assertEqual(sync.plain(self.refusal(500, b"{}")), "Cardinal refused (no reason given)")
        # A refused body says what was refused (invalid_body carries issues, no message).
        e = self.refusal(400, b'{"error":"invalid_body","issues":[{"path":"window.start","message":"Invalid input"},'
                              b'{"path":"","message":"Unrecognized key: \\"x\\""}]}')
        self.assertEqual(sync.plain(e), 'Cardinal refused (invalid_body: window.start: Invalid input; Unrecognized key: "x")')

    def test_create_sends_the_session_it_is_created_in(self):
        op = FakeOpener({"investigation_id": INV})
        sync.create_investigation(CONN, "Why?", client="c", session_id="11111111-2222-3333-4444-555555555555", opener=op)
        self.assertEqual(json.loads(op.requests[0].data)["session_id"], "11111111-2222-3333-4444-555555555555")
        sync.create_investigation(CONN, "Why?", client="c", opener=op)
        self.assertNotIn("session_id", json.loads(op.requests[1].data))
        sync.create_investigation(CONN, "Why?", client="c", session_id="agent_session-7", opener=op)  # maestro's shape
        for bad in ("../x", "", "a" * 129):
            with self.assertRaises(ist.FetchError):
                sync.create_investigation(CONN, "Why?", client="c", session_id=bad, opener=op)


if __name__ == "__main__":
    unittest.main()

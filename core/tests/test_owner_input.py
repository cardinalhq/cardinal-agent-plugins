"""cardinal_core.owner_input and investigation_grants against a fake maestro.

Privacy invariants (Investigation v1 §2.7 #8): nothing is captured, sent or
queued unless the binding authors its investigation AND the server
advertised capabilities.owner_input.enabled; a 404 (an older server, or
owner_input_disabled), 400, 401 or 403 drops and never queues (zero bytes
in the outbox); only transient failures queue; the outbox is 0600, bounded
and deleted when the capability turns off; the log keeps codes, never the
prompt; owner input is never delivered or kept in the poller's inbox; the
agent's append path refuses the type. Plus the payload (scrub, NUL strip,
hash of the original, 32 KiB cut), turn renumbering past the server's top
turn after a lost binding, and the grant token connection."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from cardinal_core import investigation_bootstrap as boot
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_grants as gr
from cardinal_core import investigation_state as ist
from cardinal_core import investigation_state_sync as sync
from cardinal_core import owner_input as oi

INV = "inv_" + "a" * 24
OTHER_INV = "inv_" + "b" * 24
SID = "11111111-2222-3333-4444-555555555555"
SECRET = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCD"
ON = {"owner_input": {"enabled": True}}
GRANT = "grt_" + "0" * 24
TRUST = "client_attested: recorded by the Cardinal plugin on the author's machine; not verified by Cardinal"


def jwt(header: dict, claims: dict) -> str:
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc(header)}.{enc(claims)}.c2lnbmF0dXJl"


TOKEN = jwt({"alg": "HS256", "typ": "CardinalInvestigation"},
            {"org": "o1", "inv": INV, "gid": GRANT, "scopes": ["read", "advise"], "exp": 2_000_000_000})


class FakeMaestro:
    """record-owner-input per CONTRACT C2 (one row per investigation,
    session and turn; same hash: duplicate, another: 409
    owner_input_turn_conflict), read-investigation-events (class filter),
    the grant routes, and an old-server mode answering every unknown path
    with Express's 404 page."""

    def __init__(self):
        self.events: list = []
        self.requests: list = []
        self.status = None       # (status, body) answered to every record-owner-input
        self.old_server = False  # no record-owner-input route at all
        self.principals = None   # the read answer's `principals` map (C2), when set
        self.trickle = 0.0       # record-owner-input sends its answer one byte at a time for this long
        # (session, turn) whose idempotency key a row that is NOT owner_input
        # holds: as the real server, 409 owner_input_key_conflict for it.
        self.key_held: set = set()
        self.answered: list = []  # statuses answered to record-owner-input
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                tool = self.path.rsplit("/", 1)[-1]
                fake.requests.append((tool, body, {k.lower(): v for k, v in self.headers.items()}))
                if tool == "record-owner-input" and fake.trickle:
                    self.send_response(201)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    end = time.monotonic() + fake.trickle
                    try:
                        while time.monotonic() < end:
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            time.sleep(0.1)
                    except OSError:
                        pass
                    return
                status, out = fake.answer(tool, body)
                if tool == "record-owner-input":
                    fake.answered.append(status)
                data = json.dumps(out).encode() if out is not None else b"<html>Cannot POST</html>"
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "120")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.conn = {"origin": f"http://127.0.0.1:{self.server.server_port}", "org": "o1", "key": "ck"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def records(self) -> list:
        return [b for t, b, _ in self.requests if t == "record-owner-input"]

    def add_owner_input(self, turn, sha, session=SID):
        self.events.append({"investigation_id": INV, "seq": len(self.events) + 1, "type": oi.TYPE,
                            "class": "owner_input", "authority": "owner_input_client_attested",
                            "payload": {"turn": turn, "prompt_sha256": sha, "text": "x"},
                            "producer": {"principal": {"kind": "user", "id": "u1"}, "session_id": session,
                                         "is_investigation_author": True}})

    def answer(self, tool, body):
        if tool == "read-investigation-events":
            evs = [e for e in self.events if e["seq"] > body.get("after", 0)]
            if body.get("class"):
                evs = [e for e in evs if e.get("class") == body["class"]]
            out = {"investigation_id": body["investigation_id"], "events": evs[:body.get("limit", 100)],
                   "head_seq": len(self.events)}
            if self.principals is not None:
                out["principals"] = self.principals
            return 200, out
        if tool == "grant-investigation-access":
            return 201, {"grant_id": GRANT, "token": TOKEN, "expires_at": "2026-10-07T16:00:00.000Z",
                         "scopes": body["scopes"], "label": body.get("label"), "principal": "grantee:" + GRANT,
                         "investigation_id": body["investigation_id"], "org": "o1"}
        if tool == "list-investigation-grants":
            return 200, {"investigation_id": body["investigation_id"],
                         "grants": [{"grant_id": GRANT, "scopes": ["read"], "label": "sup", "expires_at": "t",
                                     "revoked_at": None}]}
        if tool == "revoke-investigation-grant":
            return 200, {"grant": {"grant_id": body["grant_id"], "active": False}, "revoked": True}
        if tool != "record-owner-input" or self.old_server:
            return 404, None
        if self.status:
            return self.status
        p = body["payload"]
        if (body["session_id"], p["turn"]) in self.key_held:
            return 409, {"error": "owner_input_key_conflict", "investigation_id": INV,
                         "session_id": body["session_id"], "turn": p["turn"], "message": "m"}
        for e in self.events:
            if e["type"] == oi.TYPE and e["producer"]["session_id"] == body["session_id"] \
                    and e["payload"]["turn"] == p["turn"]:
                if e["payload"]["prompt_sha256"] == p["prompt_sha256"]:
                    return 200, {"event": e, "duplicate": True, "trust": TRUST}
                return 409, {"error": "owner_input_turn_conflict", "investigation_id": INV,
                             "session_id": body["session_id"], "turn": p["turn"], "event": e, "message": "m"}
        e = {"investigation_id": body["investigation_id"], "seq": len(self.events) + 1, "type": oi.TYPE,
             "class": "owner_input", "authority": "owner_input_client_attested", "payload": p,
             "producer": {"principal": {"kind": "user", "id": "u1"}, "session_id": body["session_id"],
                          "is_investigation_author": True}}
        self.events.append(e)
        return 201, {"event": e, "duplicate": False, "trust": TRUST}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.fake = FakeMaestro()
        self.addCleanup(self.fake.close)
        self.now = 1_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self, **fields):
        b = {"investigation_id": INV, "cursor": 0, "bound_at": "t", "source": "auto", "is_author": True,
             "capabilities": ON, "bootstrap": {"status": "ok"}, "owner_input_disclosed": True}
        b.update(fields)
        ie.write_binding(self.home, SID, b)

    def binding(self) -> dict:
        return ie.read_binding(self.home, SID)

    def submit(self, prompt, conn=None, **kw):
        return oi.submit(self.home, SID, prompt, self.fake.conn if conn is None else conn, "claude-code/test",
                         now=kw.pop("now", self.now), **kw)

    def outbox(self) -> Path:
        return oi.outbox_path(self.home, SID)

    def log_text(self) -> str:
        p = oi.log_path(self.home, SID)
        return p.read_text() if p.exists() else ""

    def queue(self, n=1, status=(503, {"error": "unavailable"})):
        """n prompts queued by a transient failure."""
        self.fake.status = status
        for i in range(n):
            self.assertEqual(self.submit(f"queued {i}", now=self.now + i), "queued")
        self.fake.status = None


class Payload(unittest.TestCase):
    def test_the_hash_is_of_the_scrubbed_text_never_the_original(self):
        prompt = f"Deploy with OPENAI_API_KEY={SECRET} — café ☕\n"
        p = oi.payload(prompt, 3, now=1_000_000.5)
        self.assertEqual(p["prompt_sha256"], hashlib.sha256(p["text"].encode()).hexdigest())
        self.assertEqual(p["prompt_sha256"], hashlib.sha256(oi.scrubbed_of(prompt)[0].encode()).hexdigest())
        self.assertNotEqual(p["prompt_sha256"], hashlib.sha256(prompt.encode()).hexdigest())
        self.assertEqual(p["original_length"], len(prompt.encode()))
        self.assertNotIn(SECRET, p["text"])
        self.assertTrue(p["text"].startswith("Deploy with OPENAI_API_KEY="))
        self.assertIn("café ☕", p["text"])
        self.assertEqual((p["turn"], p["source"], p["truncated"]), (3, "user_prompt", False))
        self.assertEqual(p["captured_at"], "1970-01-12T13:46:40.500Z")
        self.assertNotIn("slash_command", p)
        self.assertEqual(set(p), {"text", "truncated", "original_length", "prompt_sha256", "source", "turn",
                                  "captured_at"})

    def test_nul_is_stripped_and_a_lone_surrogate_replaced(self):
        prompt = "a\x00b \ud800 c"
        p = oi.payload(prompt, 1)
        self.assertEqual(p["text"], "ab � c")
        self.assertEqual(p["prompt_sha256"], hashlib.sha256("ab \ufffd c".encode()).hexdigest())
        self.assertEqual(p["original_length"], len(prompt.encode("utf-8", "surrogatepass")))
        json.dumps(p, ensure_ascii=False).encode("utf-8")   # wire-encodable

    def test_cut_to_32_kib_without_splitting_a_character(self):
        prompt = "é" * 40000
        p = oi.payload(prompt, 1)
        self.assertTrue(p["truncated"])
        self.assertLessEqual(len(p["text"].encode()), oi.MAX_TEXT_BYTES)
        self.assertEqual(set(p["text"]), {"é"})
        self.assertEqual(p["original_length"], 80000)
        # The hash covers the whole scrubbed prompt, not just the 32 KiB sent.
        self.assertEqual(p["prompt_sha256"], hashlib.sha256(("é" * 40000).encode()).hexdigest())

    def test_a_huge_prompt_is_cut_before_the_scrub_and_loses_the_slack(self):
        prompt = "x " * oi.HASHED_CHARS + SECRET
        scrubbed, cut = oi.scrubbed_of(prompt)
        self.assertTrue(cut)
        self.assertEqual(len(scrubbed), oi.HASHED_CHARS - oi.SCRUB_SLACK)
        p = oi.payload(prompt, 1)
        self.assertTrue(p["truncated"])
        self.assertEqual(p["prompt_sha256"], hashlib.sha256(scrubbed.encode()).hexdigest())
        self.assertNotIn(SECRET, json.dumps(p))

    def test_slash_command(self):
        self.assertEqual(oi.payload("/cardinal:connect --host x", 1)["slash_command"], "/cardinal:connect")
        self.assertNotIn("slash_command", oi.payload("see /etc/hosts", 1))

    def test_bad_turn(self):
        for turn in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                oi.payload("x", turn)


class Gate(Base):
    """Nothing captured, sent or queued unless enabled."""

    def assert_nothing(self, status):
        self.assertEqual(self.submit(f"hello {SECRET}"), status)
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(self.outbox().exists())
        self.assertNotIn("owner_input_turn", self.binding() or {})

    def test_capability_absent_an_older_cardinal(self):
        self.bind(capabilities={})
        self.assert_nothing("capability_off")
        b = self.binding()
        del b["capabilities"]
        ie.write_binding(self.home, SID, b)
        self.assert_nothing("capability_off")

    def test_capability_false_or_malformed(self):
        for caps in ({"owner_input": {"enabled": False}}, {"owner_input": {"enabled": "true"}},
                     {"owner_input": True}, {"projection": {"enabled": True}}, ["owner_input"]):
            self.bind(capabilities=caps)
            self.assert_nothing("capability_off")

    def test_not_the_author(self):
        for fields in ({"is_author": False, "source": "env"}, {"is_author": None}, {"source": "create"}):
            self.bind(**fields)
            if "is_author" not in fields:
                b = self.binding()
                b.pop("is_author", None)
                ie.write_binding(self.home, SID, b)
            self.assert_nothing("not_author")

    def test_not_disclosed_captures_nothing(self):
        self.bind(owner_input_disclosed=False)
        self.assert_nothing("not_disclosed")
        b = self.binding()
        del b["owner_input_disclosed"]
        ie.write_binding(self.home, SID, b)
        self.assert_nothing("not_disclosed")

    def test_forget_disclosure(self):
        self.bind()
        oi.forget_disclosure(self.home, SID)
        self.assertFalse(oi.disclosed(self.binding()))
        self.assertEqual(self.submit("x"), "not_disclosed")

    def test_mark_disclosed_only_when_enabled(self):
        self.bind(owner_input_disclosed=False, capabilities={})
        self.assertIsNone(oi.mark_disclosed(self.home, SID))
        self.assertFalse(oi.disclosed(self.binding()))
        self.bind(owner_input_disclosed=False)
        self.assertEqual(oi.mark_disclosed(self.home, SID), oi.DISCLOSURE.format(inv=INV))
        self.assertTrue(oi.disclosed(self.binding()))
        self.assertEqual(self.submit("now it records"), "posted")

    def test_unbound_keeps_nothing_even_with_a_pending_bootstrap(self):
        ie._ensure_dirs(self.home)
        (ie.sessions_dir(self.home) / f"{SID}.bootstrap.json").write_text('{"status": "failed"}')
        self.assertEqual(self.submit("hello"), "unbound")
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(self.outbox().exists())

    def test_no_key_no_request_nothing_queued(self):
        self.bind()
        self.assertEqual(self.submit("hello", conn={"origin": self.fake.conn["origin"], "org": "o1"}),
                         "no_connection")
        self.assertFalse(self.outbox().exists())

    def test_never_with_a_grant_token(self):
        self.bind()
        conn = dict(self.fake.conn, token=TOKEN)
        self.assertEqual(self.submit("hello", conn=conn), "no_connection")
        with self.assertRaises(ist.FetchError):
            oi.record(conn, INV, SID, oi.payload("x", 1), client="t")
        self.assertEqual(self.fake.requests, [])


class Posting(Base):
    def test_records_each_prompt_with_the_next_turn(self):
        self.bind()
        self.assertEqual(self.submit(f"only touch the cache layer {SECRET}"), "posted")
        self.assertEqual(self.submit("yes, go ahead"), "posted")
        got = self.fake.records()
        self.assertEqual([b["payload"]["turn"] for b in got], [1, 2])
        self.assertEqual(set(got[0]), {"investigation_id", "session_id", "payload"})
        self.assertEqual((got[0]["investigation_id"], got[0]["session_id"]), (INV, SID))
        self.assertNotIn(SECRET, json.dumps(got))
        headers = self.fake.requests[0][2]
        self.assertEqual(headers["x-cardinalhq-api-key"], "ck")
        self.assertNotIn("authorization", headers)
        self.assertEqual(self.binding()["owner_input_turn"], 2)
        self.assertFalse(self.outbox().exists())
        self.assertEqual(self.fake.answered, [201, 201], "C2 answers a new record 201: success")

    def test_a_replay_is_success(self):
        self.bind()
        self.assertEqual(self.submit("same"), "posted")
        b = self.binding()
        b["owner_input_turn"] = 0
        ie.write_binding(self.home, SID, b)
        self.assertEqual(self.submit("same"), "duplicate")
        self.assertEqual(self.fake.answered, [201, 200])


class NeverQueued(Base):
    """A refusal is dropped: zero bytes in the outbox, the code alone logged."""

    def assert_dropped(self, code_in_log):
        self.assertFalse(self.outbox().exists(), "nothing queued")
        log = self.log_text()
        self.assertIn(code_in_log, log)
        self.assertNotIn("hello", log)
        self.assertNotIn("sha", log)
        self.assertEqual(oct(oi.log_path(self.home, SID).stat().st_mode & 0o777), "0o600")

    def test_an_older_server_without_the_route(self):
        self.bind()   # even if a binding (wrongly) says enabled
        self.fake.old_server = True
        self.assertEqual(self.submit("hello"), "dropped")
        self.assert_dropped("http_404")
        # The capability is off for the rest of the session: no more requests.
        self.assertFalse(oi.enabled(self.binding()))
        self.assertFalse(oi.disclosed(self.binding()), "turning it on again discloses again")
        n = len(self.fake.requests)
        self.assertEqual(self.submit("hello again"), "capability_off")
        self.assertEqual(len(self.fake.requests), n)
        self.assertFalse(self.outbox().exists())

    def test_owner_input_disabled(self):
        self.bind()
        self.fake.status = (404, {"error": "owner_input_disabled"})
        self.assertEqual(self.submit("hello"), "dropped")
        self.assert_dropped("owner_input_disabled")
        self.assertFalse(oi.enabled(self.binding()))

    def test_401_403_drop_and_turn_off(self):
        for status in ((401, {"error": "unauthorized"}), (403, {"error": "not_investigation_author"})):
            self.bind()
            self.fake.status = status
            self.assertEqual(self.submit("hello"), "dropped")
            self.assert_dropped(status[1]["error"])

    def test_the_investigation_cap_drops_never_queues(self):
        self.bind()
        self.fake.status = (409, {"error": "event_limit_reached", "scope": "owner_input_investigation", "limit": 500})
        self.assertEqual(self.submit("hello"), "dropped")
        self.assert_dropped("event_limit_reached")
        self.assertTrue(oi.enabled(self.binding()))

    def test_no_principal_drops_and_turns_off(self):
        self.bind()
        self.fake.status = (403, {"error": "no_principal"})
        self.assertEqual(self.submit("hello"), "dropped")
        self.assert_dropped("no_principal")
        self.assertFalse(oi.enabled(self.binding()))

    def test_400_drops_that_prompt_only(self):
        self.bind()
        self.fake.status = (400, {"error": "invalid_owner_input"})
        self.assertEqual(self.submit("hello"), "dropped")
        self.assert_dropped("invalid_owner_input")
        self.assertTrue(oi.enabled(self.binding()))

    def test_a_404_while_draining_drops_the_whole_outbox(self):
        self.bind()
        self.queue(3)
        self.assertTrue(self.outbox().exists())
        self.fake.status = (404, {"error": "owner_input_disabled"})
        self.assertEqual(self.submit("hello", now=self.now + 1000), "dropped")
        self.assertFalse(self.outbox().exists())
        self.assertFalse(oi.enabled(self.binding()))


class Transient(Base):
    def test_5xx_queues_0600_and_the_next_prompt_drains_in_order(self):
        self.bind()
        self.queue(1)
        self.assertEqual(oct(self.outbox().stat().st_mode & 0o777), "0o600")
        box = oi.read_outbox(self.home, SID)
        self.assertEqual([e["payload"]["turn"] for e in box["entries"]], [1])
        self.assertGreater(box["retry_after"], self.now)
        # Within the back-off: queued without asking.
        n = len(self.fake.requests)
        self.assertEqual(self.submit("second", now=self.now + 5), "queued")
        self.assertEqual(len(self.fake.requests), n)
        # After it: oldest first, then this one.
        self.assertEqual(self.submit("third", now=self.now + 100), "posted")
        self.assertEqual([b["payload"]["turn"] for b in self.fake.records()[-3:]], [1, 2, 3])
        self.assertFalse(self.outbox().exists())

    def test_429_and_network_queue(self):
        self.bind()
        self.fake.status = (429, {"error": "rate_limited"})
        self.assertEqual(self.submit("a"), "queued")
        self.assertGreaterEqual(oi.read_outbox(self.home, SID)["retry_after"], self.now + 120)
        oi.drop_outbox(self.home, SID)
        self.assertEqual(self.submit("b", conn=dict(self.fake.conn, origin="http://127.0.0.1:9")), "queued")
        self.assertTrue(self.outbox().exists())

    def test_a_trickling_server_is_cut_off_at_the_budget(self):
        self.bind()
        self.fake.trickle = 6.0
        t = time.monotonic()
        self.assertEqual(self.submit("slow"), "queued")
        self.assertLess(time.monotonic() - t, oi.BUDGET + 0.5)
        box = oi.read_outbox(self.home, SID)
        self.assertEqual([e["payload"]["turn"] for e in box["entries"]], [1])
        self.assertGreaterEqual(box["retry_after"], self.now + oi.RETRY_FAILURE)
        self.assertEqual(oct(self.outbox().stat().st_mode & 0o777), "0o600")

    def test_flush_is_bounded_too(self):
        self.bind()
        self.queue(1)
        self.fake.trickle = 6.0
        t = time.monotonic()
        self.assertFalse(oi.flush(self.home, SID, self.fake.conn, "t", now=self.now + 100))
        self.assertLess(time.monotonic() - t, oi.BUDGET + 0.5)
        self.assertTrue(self.outbox().exists())

    def test_never_an_unlocked_outbox_write(self):
        self.bind()
        held = []

        def timed_out(fn, deadline):
            cm = ie.locked(self.home, SID, wait=1.0)
            self.assertTrue(cm.__enter__())
            held.append(cm)
            return False, None

        orig = oi._bounded
        oi._bounded = timed_out
        try:
            self.assertEqual(self.submit("x"), "busy")
        finally:
            oi._bounded = orig
            held[0].__exit__(None, None, None)
        self.assertFalse(self.outbox().exists())

    def test_the_outbox_is_bounded(self):
        self.bind()
        for i in range(oi.MAX_OUTBOX_ENTRIES + 5):
            oi._submit(self.home, SID, f"p{i}", {"origin": "http://127.0.0.1:9", "org": "o1", "key": "ck"},
                       "t", time.monotonic() - 1, self.now, None)   # no time: straight to the outbox
        entries = oi.read_outbox(self.home, SID)["entries"]
        self.assertEqual(len(entries), oi.MAX_OUTBOX_ENTRIES)
        self.assertEqual(entries[0]["payload"]["turn"], 6)   # the oldest went
        self.assertIn("outbox_full", self.log_text())

    def test_flush_posts_while_enabled(self):
        self.bind()
        self.queue(2)
        self.assertTrue(oi.flush(self.home, SID, self.fake.conn, "t", now=self.now + 100))
        self.assertEqual([b["payload"]["turn"] for b in self.fake.records()[-2:]], [1, 2])
        self.assertFalse(self.outbox().exists())


class CapabilityOff(Base):
    """The outbox is deleted, never sent, once the capability is off."""

    def test_flush_deletes_it(self):
        self.bind()
        self.queue(2)
        self.bind(capabilities={"owner_input": {"enabled": False}})
        n = len(self.fake.requests)
        self.assertTrue(oi.flush(self.home, SID, self.fake.conn, "t", now=self.now + 100))
        self.assertFalse(self.outbox().exists())
        self.assertEqual(len(self.fake.requests), n)

    def test_the_next_prompt_deletes_it(self):
        self.bind()
        self.queue(1)
        self.bind(capabilities={})
        self.assertEqual(self.submit("x", now=self.now + 100), "capability_off")
        self.assertFalse(self.outbox().exists())

    def test_refresh_capabilities_deletes_it(self):
        self.bind()
        self.queue(1)
        ie._ensure_dirs(self.home)
        boot.refresh_path(self.home, SID).touch()
        orig = sync.ensure_session_investigation
        sync.ensure_session_investigation = lambda *a, **k: {"investigation_id": INV, "capabilities": {}}
        try:
            self.assertEqual(boot.refresh_capabilities(self.home, SID, self.fake.conn, "t"), "ok")
        finally:
            sync.ensure_session_investigation = orig
        self.assertFalse(self.outbox().exists())
        self.assertFalse(oi.enabled(self.binding()))
        self.assertFalse(oi.disclosed(self.binding()), "a later refresh that turns it on discloses again")

    def test_ensure_with_the_capability_off_forgets_the_disclosure(self):
        self.bind(bootstrap={"status": "failed", "retry_after": 0})   # asked again, not reused
        orig = sync.ensure_session_investigation
        sync.ensure_session_investigation = lambda *a, **k: {"investigation_id": INV, "storyboard_id": None,
                                                             "is_author": True, "capabilities": {}}
        try:
            res = boot.ensure(self.home, SID, self.fake.conn, "t", force=True)
        finally:
            sync.ensure_session_investigation = orig
        self.assertEqual(res["status"], "ok")
        self.assertFalse(oi.disclosed(self.binding()))

    def test_a_join_never_records(self):
        self.bind()
        self.queue(1)
        self.bind(is_author=False)
        self.assertEqual(self.submit("x", now=self.now + 100), "not_author")
        self.assertFalse(self.outbox().exists())


class TurnRenumbering(Base):
    def test_a_lost_binding_renumbers_past_the_servers_top_turn(self):
        self.bind()
        self.fake.add_owner_input(1, "a" * 64)
        self.fake.add_owner_input(2, "b" * 64)
        self.fake.add_owner_input(9, "c" * 64, session="other-session")
        self.assertEqual(self.submit("after the binding was lost"), "posted")
        reads = [b for t, b, _ in self.fake.requests if t == "read-investigation-events"]
        self.assertEqual(reads[-1]["class"], "owner_input")
        self.assertEqual(self.fake.records()[-1]["payload"]["turn"], 3)
        self.assertEqual(self.binding()["owner_input_turn"], 3)
        self.assertIn("renumbered", self.log_text())
        self.assertEqual(self.submit("next"), "posted")
        self.assertEqual(self.fake.records()[-1]["payload"]["turn"], 4)

    def test_a_key_conflict_renumbers_past_the_conflicted_turn(self):
        # The server's owner_input rows stop at 2, but turn 3's key is held by
        # something else: never turn 3 again, and not 3 via "top + 1" either.
        self.bind(owner_input_turn=2)
        self.fake.add_owner_input(1, "a" * 64)
        self.fake.add_owner_input(2, "b" * 64)
        self.fake.key_held.add((SID, 3))
        self.assertEqual(self.submit("next"), "posted")
        self.assertEqual([b["payload"]["turn"] for b in self.fake.records()], [3, 4])
        self.assertEqual(self.binding()["owner_input_turn"], 4)
        self.assertFalse(self.outbox().exists())

    def test_a_key_conflict_does_not_block_the_queue(self):
        self.bind()
        self.queue(2)                       # turns 1 and 2 wait in the outbox
        self.fake.key_held.add((SID, 1))    # no owner_input rows at all
        self.assertEqual(self.submit("third", now=self.now + 100), "posted")
        sent = [(b["payload"]["turn"], b["payload"]["text"]) for b in self.fake.records()
                if b["payload"]["turn"] not in [t for s_, t in self.fake.key_held]]
        self.assertEqual(sent[-3:], [(2, "queued 0"), (3, "queued 1"), (4, "third")])
        self.assertFalse(self.outbox().exists())
        self.assertEqual(self.binding()["owner_input_turn"], 4)
        self.assertEqual(self.submit("fourth", now=self.now + 101), "posted")
        self.assertEqual(self.fake.records()[-1]["payload"]["turn"], 5)

    def test_repeated_key_conflicts_move_on_each_time(self):
        self.bind()
        self.fake.key_held.update({(SID, 1), (SID, 2)})
        self.assertEqual(self.submit("x"), "posted")
        self.assertEqual([b["payload"]["turn"] for b in self.fake.records()], [1, 2, 3])


class NeverDelivered(Base):
    def test_the_agent_cannot_append_owner_input(self):
        for t in ("owner_input.recorded", "owner_input"):
            with self.assertRaises(ist.FetchError):
                ie.append_event(self.fake.conn, INV, t, {"text": "x"}, idempotency_key="k1", client="t")
        self.assertEqual(self.fake.requests, [])

    def test_reserved_idempotency_keys_are_never_sent(self):
        for key in ("oi:1", "ckpt:x", "OI:1", "Ckpt:x", "oI:z"):
            with self.assertRaises(ist.FetchError):
                ie.append_event(self.fake.conn, INV, "cue.added", {"text": "x"}, idempotency_key=key, client="t")
            with self.assertRaises(ie.CheckpointInputError):
                ie.checkpoint(self.fake.conn, INV, SID, [{"type": "question.opened", "id": "question_a",
                                                          "statement": "s"}], idempotency_key=key, client="t")
        self.assertEqual(self.fake.requests, [])
        # What the plugin generates never uses them.
        events = ie.checkpoint_events([{"type": "question.opened", "id": "question_a", "statement": "s"}])
        for key in (ie.ack_key(SID, 3), ie.checkpoint_key(INV, SID, events)):
            self.assertFalse(key.startswith(ie.RESERVED_KEY_PREFIXES), key)

    def test_never_deliverable_and_never_kept_in_the_inbox(self):
        self.bind()
        self.fake.add_owner_input(1, "a" * 64)
        self.fake.events.append({"investigation_id": INV, "seq": 2, "type": "cue.added", "class": "control",
                                 "payload": {"text": "look at the cache"},
                                 "producer": {"principal": {"kind": "user", "id": "u2"}}})
        self.assertEqual([e["seq"] for e in ie.deliverable(self.fake.events, SID, INV)], [2])
        self.assertEqual(ie.poll_once(self.home, SID, self.fake.conn, "t", now=self.now), "inbox")
        box = ie.read_inbox(self.home, SID)
        self.assertEqual([e["seq"] for e in box["events"]], [2])
        self.assertEqual(box["through"], 2)
        out = []
        ie.deliver_inbox(self.home, SID, out.append)
        self.assertNotIn("owner_input", "".join(out))

    def test_rendering_filters_owner_input_even_if_passed(self):
        e = {"investigation_id": INV, "seq": 1, "type": "cue.added", "class": "owner_input",
             "payload": {"text": "OWNER SAID"}}
        self.assertEqual(ie.render([e], SID, INV), "")


class Grantee(Base):
    """C2 wire: producer {principal {kind grantee, id}, granted_by,
    is_investigation_author false}; the label and the granter's name are in
    the read answer's `principals` map."""

    def add(self, **producer):
        p = {"principal": {"kind": "grantee", "id": GRANT}, "granted_by": "user:u_owner",
             "is_investigation_author": False}
        p.update(producer)
        self.fake.events.append({"investigation_id": INV, "seq": len(self.fake.events) + 1, "type": "challenge.added",
                                 "class": "control", "authority": "advisory", "payload": {"text": "why not the cache?"},
                                 "producer": p, "created_at": "t"})

    def read(self):
        return ie.read_events(self.fake.conn, INV, after=0, client="t")["events"]

    def test_label_and_granter_name_come_from_the_principals_map(self):
        self.add()
        self.fake.principals = {
            "grantee:" + GRANT: {"kind": "grantee", "id": GRANT, "name": "ChatGPT supervisor", "email": None,
                                 "label": "ChatGPT supervisor", "granted_by": "user:u_owner", "scopes": ["read"],
                                 "expires_at": "t", "revoked": False},
            "user:u_owner": {"kind": "user", "id": "u_owner", "name": "Ruchir", "email": "r@example.com"},
        }
        out = ie.render_event(self.read()[0], SID)
        self.assertIn('Advisory from "ChatGPT supervisor" (access granted by "Ruchir"): not the investigation '
                      "author and not the owner of this session.", out)
        self.assertIn("From: grantee:" + GRANT, out)
        self.assertNotIn("investigation author's principal", out)
        self.assertNotIn("via key", out)
        # The granter's email when it has no name.
        self.fake.principals["user:u_owner"] = {"kind": "user", "id": "u_owner", "name": None,
                                                "email": "r@example.com"}
        self.assertIn('(access granted by "r@example.com")', ie.render_event(self.read()[0], SID))

    def test_without_the_map_it_falls_back(self):
        self.add(label="spoofed on the producer", _resolved={"label": "spoofed by the server"})
        out = ie.render_event(self.read()[0], SID)
        self.assertIn("Advisory from grantee (access granted by user:u_owner): not the investigation author", out)
        self.assertNotIn("spoofed", out)

    def test_never_this_sessions_own_and_kept_through_the_inbox(self):
        self.bind()
        self.add(is_investigation_author=True, session_id=SID)
        self.fake.principals = {"grantee:" + GRANT: {"label": "sup"}}
        self.assertEqual([e["seq"] for e in ie.deliverable(self.fake.events, SID, INV)], [1])
        self.assertEqual(ie.poll_once(self.home, SID, self.fake.conn, "t", now=self.now), "inbox")
        out = []
        ie.deliver_inbox(self.home, SID, out.append)
        self.assertIn('Advisory from "sup" (access granted by user:u_owner)', out[0])

    def test_token_errors_in_words(self):
        for code, words in (("grant_revoked", "revoked this access grant"), ("Invalid token", "is not valid")):
            err = sync.ServerError(401, {"error": code}, "x")
            self.assertIn(words, ie.plain(err))
            self.assertIn(words, sync.plain(err))
        self.assertNotIn("does not support", sync.plain(sync.ServerError(404, {"error": "grant_not_found"}, "x")))
        self.assertFalse(sync.unsupported(sync.ServerError(404, {"error": "grant_not_found"}, "x")))


class Grants(Base):
    def test_scopes_and_ttl(self):
        self.assertEqual(gr.parse_scopes(None), ["read", "advise"])
        self.assertEqual(gr.parse_scopes("read", owner_input=True), ["read", "owner_input:read"])
        self.assertEqual(gr.parse_scopes("advise,read"), ["read", "advise"])
        for raw, oi_ in (("advise", True), ("observe", False), ("", False), ("owner_input:read", False)):
            with self.assertRaises(ValueError):
                gr.parse_scopes(raw, owner_input=oi_)
        self.assertEqual(gr.parse_ttl("4h"), 14400)
        self.assertEqual(gr.parse_ttl("90m"), 5400)
        with self.assertRaises(ValueError):
            gr.parse_ttl("soon")

    def test_grant_list_revoke_bodies(self):
        out = gr.grant(self.fake.conn, INV, ["read", "advise"], ttl_seconds=3600, label="sup", client="t")
        self.assertEqual(out["token"], TOKEN)
        tool, body, headers = self.fake.requests[-1]
        self.assertEqual((tool, body), ("grant-investigation-access", {"investigation_id": INV,
                                                                       "scopes": ["read", "advise"],
                                                                       "ttl_seconds": 3600, "label": "sup"}))
        self.assertEqual(headers["x-cardinalhq-api-key"], "ck")
        self.assertEqual(len(gr.list_grants(self.fake.conn, INV, client="t")), 1)
        gr.revoke(self.fake.conn, GRANT, client="t")
        self.assertEqual(self.fake.requests[-1][:2], ("revoke-investigation-grant", {"grant_id": GRANT}))
        with self.assertRaises(ist.FetchError):
            gr.revoke(self.fake.conn, "grt_nope", client="t")

    def test_grant_limit_names_its_scope(self):
        active = gr.refusal(sync.ServerError(409, {"error": "grant_limit_reached", "scope": "active", "limit": 20}, "x"))
        self.assertIn("active access grants (20)", active)
        self.assertIn("revoke", active)
        lifetime = gr.refusal(sync.ServerError(409, {"error": "grant_limit_reached", "scope": "lifetime",
                                                     "limit": 100}, "x"))
        self.assertIn("lifetime limit of access grants (100)", lifetime)
        self.assertIsNone(gr.refusal(sync.ServerError(400, {"error": "invalid_scopes"}, "x")))

    def test_token_connection_from_the_environment_only(self):
        self.assertIsNone(gr.token_connection({}))
        env = {gr.TOKEN_ENV: TOKEN, gr.ORIGIN_ENV: self.fake.conn["origin"] + "/"}
        conn = gr.token_connection(env)
        self.assertEqual((conn["org"], conn["investigation_id"], conn["grant_id"], conn["origin"]),
                         ("o1", INV, GRANT, self.fake.conn["origin"]))
        self.assertNotIn("key", conn)
        self.assertEqual(gr.token_connection({gr.TOKEN_ENV: TOKEN}, fallback_origin="https://x.example")["origin"],
                         "https://x.example")
        with self.assertRaises(ValueError):
            gr.token_connection({gr.TOKEN_ENV: TOKEN})   # no origin anywhere
        for origin in ("https://app.cardinalhq.io", "http://localhost:8080", "http://127.0.0.1:9", "http://[::1]:3000"):
            self.assertEqual(gr.token_connection({gr.TOKEN_ENV: TOKEN, gr.ORIGIN_ENV: origin})["origin"], origin)
        for origin in ("http://app.cardinalhq.io", "http://10.0.0.5:8080", "ftp://x.io", "https://x.io/path"):
            with self.assertRaises(ValueError, msg=origin):
                gr.token_connection({gr.TOKEN_ENV: TOKEN, gr.ORIGIN_ENV: origin})
        with self.assertRaises(ValueError):
            gr.token_connection({gr.TOKEN_ENV: TOKEN}, fallback_origin="http://cardinal.internal")
        for bad in ("nope", jwt({"typ": "JWT"}, {"org": "o1", "inv": INV}),
                    jwt({"typ": "CardinalInvestigation"}, {"org": "o1"})):
            with self.assertRaises(ValueError):
                gr.token_connection({gr.TOKEN_ENV: bad, gr.ORIGIN_ENV: "https://x.example"})

    def test_the_token_authenticates_instead_of_a_key(self):
        conn = gr.token_connection({gr.TOKEN_ENV: TOKEN, gr.ORIGIN_ENV: self.fake.conn["origin"]})
        ie.read_events(conn, INV, after=0, client="t")
        tool, _, headers = self.fake.requests[-1]
        self.assertEqual(headers["authorization"], "CardinalInvestigation " + TOKEN)
        self.assertNotIn("x-cardinalhq-api-key", headers)


if __name__ == "__main__":
    unittest.main()

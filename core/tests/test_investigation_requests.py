"""Default-on coordination and durable request/action dependency invariants."""
import copy
import base64
import json
import time
from pathlib import Path
from unittest.mock import patch

from tests.test_investigation_gate import Fixture, CONN, SID, INV, event, POLICY, c, g, ie
from cardinal_core import investigation_requests as requests
from cardinal_core import investigation_gate_runtime as rt


def created(seq=1, *, intent="Choose the next hypothesis", requester=SID, assignee="peer", legacy=False):
    purpose = {"actions": ["commit"]} if legacy else {"intent": intent}
    return event(seq, "request.created", sid=requester, payload={"text": "Review the evidence",
        "assignee": {"principal": "user:owner", "session": assignee}, **purpose})


def response(seq, request=1, outcome="complete", sid="peer"):
    return event(seq, "request.responded", sid=sid, payload={"request": request, "outcome": outcome, "text": "Result"})


def resolved(seq, request=1, *, outcome="accepted", reply=2, sid=SID):
    payload = {"request": request, "outcome": outcome, "text": "Reviewed the result"}
    if reply is not None:
        payload["response"] = reply
    return event(seq, "request.resolved", sid=sid, payload=payload)


class Requests(Fixture):
    def test_arbitrary_intent_survives_restart_and_unrelated_work_stays_available(self):
        self.enable()
        self.server.add(created())
        purpose = "Choose the next hypothesis"
        self.assertEqual(self.gate.synchronize(purpose).reason, "waiting")
        self.assertEqual(self.make_gate().synchronize(purpose).reason, "waiting")
        self.assertEqual(self.gate.synchronize("Explain the already verified timeline").status, "allow")
        self.assertEqual(self.gate.check("anything", {}).status, "uncovered")

    def test_legacy_request_selected_explicitly_is_durable_without_action_matching(self):
        self.enable()
        self.server.add(created(legacy=True))
        self.assertEqual(self.gate.synchronize("Answer the user", [1]).reason, "waiting")
        self.assertEqual(self.make_gate().synchronize("Answer the user").reason, "waiting")
        self.assertEqual(self.gate.synchronize("Independent calculation").status, "allow")

    def test_complete_needs_exact_acceptance_after_feedback_review(self):
        self.enable()
        self.server.add(created(), response(2))
        self.assertEqual(self.gate.synchronize("Choose the next hypothesis").status, "defer")
        batch, hashes = self.delivered()
        self.gate.reviewed(batch, hashes)
        self.assertEqual(self.gate.synchronize("Choose the next hypothesis").reason, "needs_attention")
        self.server.add(resolved(3))
        self.assertEqual(self.gate.synchronize("Choose the next hypothesis").status, "allow")

    def test_blocked_abstain_reject_and_silence_never_release(self):
        for outcome in ("blocked", "abstain", "reject"):
            state = {}
            requests.fold(state, [created(), response(2, outcome=outcome)])
            self.assertTrue(requests.waiting(state, "user:owner", SID, [1]))
            with self.assertRaises(ValueError):
                requests.fold(copy.deepcopy(state), [resolved(3)])
            requests.fold(state, [resolved(3, outcome="cancelled", reply=None)])
            self.assertFalse(requests.waiting(state, "user:owner", SID, [1]))

    def test_identity_wrong_response_and_stale_result_refused(self):
        for bad in (response(2, sid="intruder"), resolved(2, reply=99), resolved(2, sid="intruder")):
            with self.assertRaises(ValueError):
                requests.fold({}, [created(), bad])
        with self.assertRaises(ValueError):
            requests.fold({}, [created(), response(2), response(3)])

    def test_blocked_can_progress_to_complete(self):
        state = {}
        requests.fold(state, [created(), response(2, outcome="blocked"), response(3), resolved(4, reply=3)])
        self.assertFalse(requests.waiting(state, "user:owner", SID, [1]))

    def test_missing_or_foreign_requirement_never_clears_intent(self):
        self.enable()
        self.assertEqual(self.gate.synchronize("Publish a conclusion", [99]).status, "defer")
        self.server.add(created(requester="another"))
        self.assertEqual(self.gate.synchronize("Other conclusion", [1]).status, "defer")

    def test_fresh_read_failure_retains_requirement(self):
        self.enable()
        self.server.add(created())
        self.gate.synchronize("Choose the next hypothesis")
        self.server.fail = TimeoutError()
        self.assertEqual(self.gate.synchronize("Choose the next hypothesis").status, "defer")
        self.assertTrue(c.private_read(self.path)["requests"]["1"])

    def test_identical_later_request_is_new_but_uncertain_retry_keeps_identity(self):
        payload = created()["payload"]
        observed, lose = [], [True]
        def append(conn, inv, typ, value, **kw):
            observed.append(kw["idempotency_key"])
            if lose[0]:
                lose[0] = False
                raise ie.ist.FetchError("response lost")
            return {"event": {"seq": 1}}
        with patch.object(ie, "append_event", side_effect=append):
            with self.assertRaises(ie.ist.FetchError):
                requests.create(CONN, INV, SID, payload, home=self.root, client="test")
            self.assertTrue(requests.uncertain(self.root, SID, CONN, payload["intent"]))
            self.assertFalse(requests.uncertain(self.root, SID, CONN, "Independent work"))
            requests.create(CONN, INV, SID, payload, home=self.root, client="test")
            self.assertFalse(requests.uncertain(self.root, SID, CONN, payload["intent"]))
            requests.create(CONN, INV, SID, payload, home=self.root, client="test")
        self.assertEqual(observed[0], observed[1])
        self.assertNotEqual(observed[1], observed[2])


class AutomaticConsumers(Fixture):
    def setUp(self):
        super().setUp()
        self.hook = self.root / "plugin/hooks/cardinal-codex-telemetry.py"
        self.hook.parent.mkdir(parents=True)
        self.hook.write_text("# fixture")
        self.cli = self.hook.parent.parent / "scripts/cardinal-storyboard"
        self.old, self.new = "grt_" + "1" * 24, "grt_" + "2" * 24
        self.roots = {self.old: self.old, self.new: self.old}
        original = self.server.__call__
        def server(conn, operation, body, **kw):
            self.server.viewer = "grantee:" + conn["grant_id"] if conn.get("grant_id") else "user:owner"
            out = original(conn, operation, body, **kw)
            if operation == "read-investigation-events" and conn.get("grant_id"):
                root = self.roots[conn["grant_id"]]
                if any(self.roots.get(x) != root for x in body.get("previous_grant_ids", [])):
                    raise ie.sync.ServerError(403, {"error": "coordination_consumer_not_predecessor"}, "unrelated grant")
                out["viewer"].update(lineage_root_id=root, previous_grant_ids=body.get("previous_grant_ids", []))
            return out
        self.protocol = patch.object(ie.sync, "_post", side_effect=server)
        self.protocol.start()
        self.addCleanup(self.protocol.stop)

    def credential(self, path, gid):
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        token = encode({"typ": "CardinalInvestigation"}) + "." + encode({"org": "org", "inv": INV,
            "gid": gid, "scopes": ["read", "advise", "supervise"], "exp": int(time.time()) + 3600}) + ".Zml4dHVyZQ"
        c.private_write(path, {"token": token, "origin": CONN["origin"]})
        return c.credential_connection(path)

    def dispatch(self, credential=None):
        credential = credential or self.root / "credential.json"
        try:
            if credential.exists():
                conn = c.credential_connection(credential)
            else:
                journal = c.private_read(credential.with_suffix(".json.successor.json"))
                credential = Path(journal["credential_file"])
                conn = c.credential_connection(credential)
            gates = rt.prepare_intent(session=SID, runtime="codex", home=self.root,
                conn=conn, client="test", cli=self.cli, credential_file=credential)
            for gate in gates:
                result = gate.synchronize("Review the finding")
                if result.status == "defer":
                    return rt.denial(result.message)
        except Exception as exc:
            return rt.denial(str(exc))
        return None

    def setup_old(self):
        path = self.root / "credential.json"
        conn = self.credential(path, self.old)
        pin = str(self.root / "custom-response-guard.json")
        conn["_coordination_budget"] = pin
        _, gate = rt.ensure_automatic(self.root, SID, "codex", conn, "test", self.hook, path)
        self.server.add(event(1, "finding.proposed", sid="peer", payload={"statement": "Review"}))
        self.assertEqual(self.dispatch()["hookSpecificOutput"]["permissionDecision"], "deny")
        return path, conn, gate, pin

    def test_unrelated_replacement_never_releases_old_consumer(self):
        path, old, gate, _ = self.setup_old()
        batch = c.private_read(gate.path)["gate"]["batch"]
        self.roots[self.new] = self.new
        self.credential(path, self.new)
        self.assertEqual(self.dispatch()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(c.private_read(gate.path)["gate"]["batch"], batch)
        self.assertTrue(any(r["state"] == "active" and r["gate_file"] == str(gate.path) for _, r in rt.all_routes(self.root, SID)))

    def test_same_path_successor_replays_and_retains_pinned_budget(self):
        path, old, gate, pin = self.setup_old()
        batch = c.private_read(gate.path)["gate"]["batch"]
        self.credential(path, self.new)
        self.assertEqual(self.dispatch()["hookSpecificOutput"]["permissionDecision"], "deny")
        active = [r for _, r in rt.all_routes(self.root, SID) if r["state"] == "active"]
        self.assertEqual(len(active), 1)
        fresh = c.private_read(Path(active[0]["gate_file"]))
        self.assertEqual(fresh["gate"]["budget_file"], pin)
        self.assertEqual(fresh["pending"]["events"][0]["seq"], 1)
        self.assertEqual(c.private_read(gate.path)["gate"]["batch"], batch)

    def test_explicit_new_command_cannot_discard_journal_budget(self):
        import shlex
        path, old, gate, pin = self.setup_old()
        new_path = self.root / "new-credential.json"
        new = self.credential(new_path, self.new)
        c.private_write(path.with_suffix(".json.successor.json"), {"old_identity": c.identity(old),
            "new_identity": c.identity(new), "credential_file": str(new_path)})
        path.unlink()
        self.assertEqual(self.dispatch(new_path)["hookSpecificOutput"]["permissionDecision"], "deny")
        active = [r for _, r in rt.all_routes(self.root, SID) if r["state"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(c.private_read(Path(active[0]["gate_file"]))["gate"]["budget_file"], pin)
        self.assertEqual(self.dispatch()["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_same_token_copy_does_not_retire_a_live_pinned_consumer(self):
        import shlex
        path, old, gate, pin = self.setup_old()
        copied_path = self.root / "copied/credential.json"
        copied_path.parent.mkdir(mode=0o700)
        c.private_write(copied_path, c.private_read(path))
        self.assertEqual(self.dispatch(copied_path)["hookSpecificOutput"]["permissionDecision"], "deny")
        active = [r for _, r in rt.all_routes(self.root, SID) if r["state"] == "active"]
        self.assertEqual(len(active), 2)
        self.assertTrue(any(r["gate_file"] == str(gate.path) for r in active))
        self.assertEqual(c.private_read(gate.path)["gate"]["budget_file"], pin)
        # Repeated covered actions must still evaluate the original obligation.
        for _ in range(2):
            self.assertEqual(self.dispatch()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(any(r["state"] == "active" and r["gate_file"] == str(gate.path) for _, r in rt.all_routes(self.root, SID)))

    def test_missing_indexed_route_and_orphan_activation_deny(self):
        path, _, gate, _ = self.setup_old()
        index = rt.index_path(self.root, SID)
        selected = c.private_read(index)["routes"][0]
        Path(selected).unlink()
        self.assertEqual(self.dispatch()["hookSpecificOutput"]["permissionDecision"], "deny")

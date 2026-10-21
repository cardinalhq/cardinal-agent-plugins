"""Phase 2 policy, fixed-head proof and durable review contract tests.

The fixture implements the Phase 1 server contract, not a deployed endpoint.
Runtime denial/feedback behavior belongs to the Phase 3 acceptance tests.
"""
import argparse
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cardinal_core import investigation_budget as budget
from cardinal_core import investigation_cli as cli
from cardinal_core import investigation_consumer as c
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_gate as g

INV = "inv_" + "a" * 24
SID, PEER = "worker-session", "supervisor-session"
CONN = {"origin": "https://example.test", "org": "org", "investigation_id": INV, "key": "test-key"}
POLICY = {"version": 1, "runtime": "codex", "role": "worker",
          "peers": [{"principal": "user:owner", "session": PEER}],
          "cli_prefixes": [["/plugin/cardinal-storyboard"], ["python3", "/plugin/cardinal-storyboard"]],
          "git": ["git", "/usr/bin/git"], "gh": ["gh"]}


def event(seq, typ="cue.added", *, sid=PEER, principal="user:owner", payload=None, **extra):
    kind, ident = principal.split(":", 1)
    cls = "semantic" if typ in ie.SEMANTIC_TYPES else "owner" if typ == ie.OWNER_DECIDED else "control"
    authority = {"semantic": "producer_claim", "owner": "owner_decision"}.get(cls, "advisory")
    if typ == ie.RULING:
        authority = "delegated_supervision"
    if typ == ie.VERIFICATION:
        authority = "supervisor_attestation"
    return {"investigation_id": INV, "seq": seq, "type": typ, "class": cls, "authority": authority,
            "producer": {"principal": {"kind": kind, "id": ident}, "session_id": sid,
                         "is_investigation_author": principal == "user:owner"},
            "created_at": "2026-10-20T00:00:00Z", "payload": payload or {"text": "Check this", "refs": ["#1"]}, **extra}


class Server:
    def __init__(self):
        self.events, self.requests = [], []
        self.viewer, self.head, self.fail = "user:owner", 0, None
        self.cap = {"enabled": True, "version": 1}

    def add(self, *events):
        self.events.extend(events)
        self.head = max([self.head] + [e["seq"] for e in events])

    def __call__(self, conn, operation, body, **kwargs):
        self.requests.append((operation, copy.deepcopy(body)))
        if self.fail:
            raise self.fail
        if operation == "get-investigation":
            return {"investigation_id": INV, "author": {"kind": "user", "id": "owner"},
                    "capabilities": {"event_snapshot": self.cap, "coordination_requests": {"enabled": True, "version": 1}}}
        assert operation == "read-investigation-events", operation
        assert body["snapshot"] is True
        bound = body.get("through_seq", self.head)
        rows = [e for e in self.events if body["after"] < e["seq"] <= bound
                and (not body.get("types") or e["type"] in body["types"])]
        rows = rows[:body["limit"]]
        complete = len(rows) < body["limit"] or rows[-1]["seq"] == bound
        scanned = bound if complete else rows[-1]["seq"]
        return {"investigation_id": INV, "events": copy.deepcopy(rows), "head_seq": self.head,
                "snapshot_head_seq": bound, "scanned_through": scanned, "next_after": scanned,
                "scan_complete": complete, "viewer": {"principal": self.viewer}, "principals": {}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "gate.json"
        self.server = Server()
        self.patch = patch.object(ie.sync, "_post", side_effect=self.server)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.gate = self.make_gate()

    def make_gate(self, **kwargs):
        params = dict(session=SID, client="test", home=self.root)
        params.update(kwargs)
        return g.Gate(CONN, self.path, **params)

    def enable(self, **kwargs):
        return self.gate.enable(POLICY, **kwargs)

    def check(self):
        return self.gate.check("apply_patch", "*** Begin Patch")

    def batch(self):
        return c.private_read(self.path)["gate"]["batch"]

    def delivered(self):
        current = self.batch()
        for n in range(1, len(current["chunks"]) + 1):
            self.gate.read(current["page"]["batch"], n)
        return current["page"]["batch"], [x["hash"] for x in current["chunks"]]

    def acknowledge(self, seq, *, sid=SID, principal="user:owner", disposition="accepted", note="I will check"):
        self.server.add(event(self.server.head + 1, "acknowledged", sid=sid, principal=principal,
                              payload={"ack_of": seq, "disposition": disposition, "note": note}))


class Snapshot(Fixture):
    def read(self, **kw):
        return ie.read_snapshot(CONN, INV, client="test", **kw)

    def test_zero_fixed_bound_and_current_global_head(self):
        self.server.head = 7
        page = self.read(through_seq=0)
        self.assertEqual(page["scanned_through"], 0)
        self.assertEqual(self.server.requests[-1][1]["through_seq"], 0)
        self.assertEqual(page["head_seq"], 7)

    def test_invalid_request_numbers(self):
        for after in (True, -1, 1.5, 2**54):
            with self.subTest(after=after), self.assertRaises(ie.ist.FetchError):
                self.read(after=after)
        for through in (True, -1, 1.1):
            with self.assertRaises(ie.ist.FetchError):
                self.read(through_seq=through)

    def test_malformed_proofs_never_complete(self):
        self.server.add(event(1), event(2))
        good = self.read(limit=1)
        mutations = [{"scan_complete": True}, {"scan_complete": 0}, {"head_seq": True},
                     {"next_after": True}, {"scanned_through": 0}, {"snapshot_head_seq": 3},
                     {"viewer": {}}, {"events": []}, {"investigation_id": "other"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), patch.object(ie.sync, "_post", return_value={**good, **mutation}):
                with self.assertRaises(ie.ist.FetchError):
                    self.read(limit=1)
        for key in ("scan_complete", "scanned_through", "snapshot_head_seq"):
            malformed = {k: v for k, v in good.items() if k != key}
            with patch.object(ie.sync, "_post", return_value=malformed), self.assertRaises(ie.ist.FetchError):
                self.read(limit=1)

    def test_bad_events_and_authority(self):
        self.server.add(event(1))
        good = self.read()
        for mutation in ({"investigation_id": "other"}, {"seq": True}, {"seq": 0}, {"payload": None},
                         {"class": "owner"}, {"authority": "owner_decision"}, {"producer": {}}, {"seq": 2}):
            with self.subTest(mutation=mutation):
                raw = {**good, "events": [{**good["events"][0], **mutation}]}
                with patch.object(ie.sync, "_post", return_value=raw), self.assertRaises(ie.ist.FetchError):
                    self.read()
        raw = {**good, "events": good["events"] * 2}
        with patch.object(ie.sync, "_post", return_value=raw), self.assertRaises(ie.ist.FetchError):
            self.read()
        for payload in ({"ack_of": True, "disposition": "accepted"},
                        {"ack_of": 1, "disposition": "invented"},
                        {"ack_of": 1, "disposition": "accepted", "note": {}},
                        {"ack_of": [1], "disposition": "accepted"}):
            self.server.events = [event(1, ie.ACKNOWLEDGED, payload=payload)]
            with self.assertRaises(ie.ist.FetchError):
                self.read()


class Policy(unittest.TestCase):
    def classify(self, command, value=None, tool="exec_command"):
        return g.classify(g.policy(value or POLICY), tool, {"cmd": command, "command": command},
                          session=SID, investigation=INV, gate_file="/private/gate.json")

    def test_supported_direct_forms(self):
        for command in ("git commit -m 'a; b'", "git -C /repo --no-pager push origin HEAD",
                        "/usr/bin/git commit --amend --no-edit", "gh -R org/repo pr merge 12 --squash",
                        "git push --force-with-lease origin HEAD", "git push --force-with-lease=main:abc",
                        "git commit --message='quoted value' -- path.py",
                        "/plugin/cardinal-storyboard investigation verdict --session worker-session 1"):
            with self.subTest(command=command):
                self.assertEqual(self.classify(command), "covered")
        self.assertEqual(self.classify("", tool="apply_patch"), "covered")
        claude = {**POLICY, "runtime": "claude"}
        for tool in ("Write", "Edit", "NotebookEdit", "Bash"):
            self.assertEqual(self.classify("git push", value=claude, tool=tool), "covered")

    def test_supported_exemptions_are_exact_whole_commands(self):
        prefix = "/plugin/cardinal-storyboard investigation "
        for tail in ("ack --session worker-session 1 accepted --note 'Will do; thanks'",
                     "gate-status --session worker-session --gate-file /private/gate.json",
                     "events --session worker-session --investigation " + INV):
            self.assertEqual(self.classify(prefix + tail), "coordination")
        for tail in ("ack --session another 1 accepted", "ack --session worker-session 0 accepted",
                     "ack --session worker-session 1 accepted --typo x",
                     "gate-status --session worker-session --gate-file /another.json",
                     "ack --session worker-session 1 accepted; git push"):
            self.assertEqual(self.classify(prefix + tail), "uncovered")

    def test_unsupported_equivalents_and_nested_calls_never_claim_coverage(self):
        for command in ("env git push", "cd /repo && git push", "git push | cat", "git push\ntrue",
                        "git push $(echo origin)", "git push `echo origin`", "git push $REMOTE",
                        "git push *", "sh -c 'git push'", "echo git push", "git -c alias.x=push x",
                        "/other/git push", "gh --hostname example.test pr merge 1",
                        "git push --invented", "git commit --unregistered-flag", "git push \\\norigin",
                        "/other/cardinal-storyboard investigation ack --session worker-session 1 accepted"):
            with self.subTest(command=command):
                self.assertEqual(self.classify(command), "uncovered")
        for tool in ("functions.exec", "write_stdin", "MultiEdit"):
            self.assertEqual(self.classify("git push", tool=tool), "uncovered")

    def test_policy_is_explicit_and_digest_changes(self):
        for raw in ({}, {**POLICY, "version": True}, {**POLICY, "peers": []}, {**POLICY, "unknown": True}):
            with self.assertRaises(ValueError):
                g.policy(raw)
        self.assertNotEqual(g.policy_hash(g.policy(POLICY)), g.policy_hash(g.policy({**POLICY, "git": []})))


class Engine(Fixture):
    def test_opt_in_quiet_freshness_and_disable_preserves(self):
        self.assertEqual(self.check().status, "disabled")
        self.enable()
        before = len(self.server.requests)
        self.assertEqual(self.check(), g.Result("allow"))
        self.assertEqual(self.check(), g.Result("allow"))
        self.assertEqual(len(self.server.requests) - before, 2)
        self.server.add(event(1))
        self.assertEqual(self.check().status, "defer")
        pending = self.batch()
        self.gate.disable()
        self.assertEqual(self.check().status, "disabled")
        self.enable()
        self.assertEqual(self.batch(), pending)

    def test_pending_exact_replay_and_delivery_does_not_advance(self):
        self.server.add(event(1, "challenge.added"))
        self.enable()
        first = self.check()
        restart = self.make_gate()
        self.assertEqual(restart.read(first.batch, 1), first)
        batch, hashes = self.delivered()
        self.assertEqual(self.gate.status()["after"], 0)
        with self.assertRaises(ValueError):
            self.gate.reviewed(batch, hashes)
        self.acknowledge(1)
        self.assertEqual(restart.reviewed(batch, hashes)["after"], 1)
        self.assertEqual(self.check().status, "allow")
        with self.assertRaises(ValueError):
            restart.reviewed(batch, hashes)

    def test_chunk_lossless_unicode_bound_attribution_and_explicit_coverage(self):
        text = "before " + "💡é\\\"\n" * 1600 + " after"
        self.server.add(event(1, payload={"text": text, "refs": ["#3"]}))
        self.enable()
        first = self.check()
        current = self.batch()
        self.assertGreater(len(current["chunks"]), 10)
        records = {}
        for chunk in current["chunks"]:
            self.assertLessEqual(len(chunk["message"].encode()) + 1, 1000)
            raw = json.loads(chunk["message"])
            self.assertEqual(raw.pop("hash"), g.digest(raw))
            records.setdefault(raw["record"], []).append(raw["data"])
            if raw["record"]:
                self.assertEqual((raw["seq"], raw["authority"], raw["session"]), (1, "advisory", PEER))
        self.assertEqual(json.loads("".join(records[1])), self.server.events[0])
        self.assertEqual(json.loads("".join(records[0]))["events"], [])
        self.acknowledge(1)
        hashes = [x["hash"] for x in current["chunks"]]
        with self.assertRaises(ValueError):
            self.gate.reviewed(first.batch, hashes)  # knowing hashes alone is not delivery
        batch, hashes = self.delivered()
        for invalid in (hashes[:-1], list(reversed(hashes)), ["bad"]):
            with self.assertRaises(ValueError):
                self.gate.reviewed(batch, invalid)
        self.assertEqual(self.gate.reviewed(batch, hashes)["after"], 1)

    def test_another_session_or_principal_ack_does_not_count(self):
        self.server.add(event(1, "challenge.added"))
        self.enable()
        self.check()
        batch, hashes = self.delivered()
        for kwargs in ({"sid": "another-session"}, {"principal": "user:another"}):
            self.acknowledge(1, **kwargs)
            with self.assertRaises(ValueError):
                self.gate.reviewed(batch, hashes)
            self.assertEqual(self.gate.status()["after"], 0)
        self.acknowledge(1, disposition="declined", note="Evidence contradicts it")
        self.assertEqual(self.gate.reviewed(batch, hashes)["after"], 1)

    def test_server_committed_closed_challenge_ack_is_sufficient(self):
        self.server.add(event(1, "challenge.added"))
        self.enable()
        self.check()
        batch, hashes = self.delivered()
        # The real append route permits this only after withdrawal/overrule.
        # A gate must trust that server validation, rather than reapply the
        # open-target-only rule to the immutable original challenge.
        self.acknowledge(1, disposition="noted", note="")
        self.assertEqual(self.gate.reviewed(batch, hashes)["after"], 1)

    def test_ack_scan_cap_persists_proofs_and_resumes_fixed_head(self):
        self.server.add(event(1), event(2))
        self.enable()
        self.check()
        batch, hashes = self.delivered()
        self.acknowledge(1, sid="another-session")
        self.acknowledge(1)
        self.acknowledge(2)
        self.gate.max_pages, self.gate.limit = 1, 1
        with self.assertRaises(ValueError):
            self.gate.reviewed(batch, hashes)
        self.assertEqual(self.batch()["ack_scan"]["head"], 5)
        self.assertEqual(self.batch()["ack_scan"]["after"], 3)
        with self.assertRaises(ValueError):
            self.gate.reviewed(batch, hashes)
        self.assertEqual(self.batch()["ack_scan"]["found"], [1])
        self.assertEqual(self.gate.reviewed(batch, hashes)["after"], 2)

    def test_stdout_stays_quiet_for_malformed_coordination_help(self):
        self.enable()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = self.gate.check("exec_command", {"cmd": "/plugin/cardinal-storyboard investigation ack --help"})
        self.assertEqual(result.status, "uncovered")
        self.assertEqual(out.getvalue(), "")

    def test_parallel_checks_have_no_shared_permit(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.enable()
        entered, release = Event(), Event()
        original = self.server.__call__
        def wait_for_read(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError()
            return original(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(ie.sync, "_post", side_effect=wait_for_read):
            first = pool.submit(self.check)
            self.assertTrue(entered.wait(3))
            self.assertEqual(pool.submit(self.check).result(3).status, "defer")
            release.set()
            self.assertEqual(first.result(3).status, "allow")
        self.server.add(event(1))
        self.assertEqual(self.check().status, "defer")

    def test_response_returning_after_deadline_never_allows(self):
        self.enable()
        # start deadline=3; enough budget for HTTP; response arrives at t=4
        with patch.object(g.time, "monotonic", side_effect=[0, 0, 4]):
            self.assertEqual(self.check().status, "defer")
        self.assertEqual(self.check().status, "allow")

    def test_partial_snapshot_resumes_old_bound_and_never_allows(self):
        self.server.add(event(1, sid=SID), event(2, sid=SID))
        self.gate = self.make_gate(max_pages=1, limit=1)
        self.enable()
        self.assertEqual(self.check().status, "defer")
        self.assertEqual(self.gate.status()["snapshot"], {"head": 2, "complete": False})
        self.server.add(event(3))
        self.assertEqual(self.check().status, "defer")
        self.assertEqual(self.server.requests[-1][1]["through_seq"], 2)
        self.assertEqual(self.gate.status()["after"], 2)
        self.assertEqual(self.check().status, "defer")
        self.assertEqual(self.batch()["page"]["events"][0]["seq"], 3)

    def test_peer_after_snapshot_is_found_after_review(self):
        self.server.add(event(1))
        self.enable()
        self.check()
        batch, hashes = self.delivered()
        self.server.add(event(2))
        self.acknowledge(1)
        self.gate.reviewed(batch, hashes)
        self.assertEqual(self.check().status, "defer")
        self.assertEqual(self.batch()["page"]["events"][0]["seq"], 2)

    def test_hidden_tail_self_wrong_peer_and_route_skip_deterministically(self):
        self.server.add(event(1, sid=SID), event(2, principal="user:stranger"),
                        event(3, to_session_id="another-session"))
        self.server.head = 10
        self.enable()
        self.assertEqual(self.check().status, "allow")
        self.assertEqual(self.gate.status()["after"], 10)
        self.assertEqual(self.check().status, "allow")

    def test_owner_authority_kept_and_invalid_envelope_cannot_skip(self):
        self.server.add(event(1, ie.OWNER_DECIDED, sid=None))
        self.enable()
        self.assertEqual(self.check().status, "defer")
        self.assertEqual(self.batch()["page"]["events"][0]["authority"], "owner_decision")
        self.assertTrue(ie.owner_authority(self.batch()["page"]["events"][0]))

    def test_owner_type_from_other_person_stays_advisory(self):
        self.server.add(event(1, ie.OWNER_DECIDED, sid=None, principal="user:another"))
        self.enable()
        self.assertEqual(self.check().status, "defer")
        for chunk in self.batch()["chunks"]:
            frame = json.loads(chunk["message"])
            if frame["record"]:
                self.assertEqual(frame["for_consumer"], "advisory")

    def test_all_registered_control_types_are_applicable(self):
        for seq, typ in enumerate(ie.DELIVERABLE, 1):
            self.server.add(event(seq, typ))
        self.enable()
        self.check()
        self.assertEqual([e["type"] for e in self.batch()["page"]["events"]], list(ie.DELIVERABLE))

    def test_viewer_change_and_private_state_tampering_never_clear(self):
        self.enable()
        self.server.viewer = "user:another"
        self.assertEqual(self.check().status, "defer")
        self.assertEqual(self.gate.status()["after"], 0)
        self.server.viewer = "user:owner"
        self.server.add(event(1))
        self.check()
        raw = c.private_read(self.path)
        raw["gate"]["batch"]["chunks"][0]["message"] = "altered"
        c.private_write(self.path, raw)
        self.assertEqual(self.check().status, "defer")
        with self.assertRaises(ValueError):
            self.gate.status()
        control = "/plugin/cardinal-storyboard investigation gate-disable --session " + SID + " --gate-file " + str(self.path)
        self.assertEqual(self.gate.check("exec_command", {"cmd": control}).status, "coordination")
        self.assertFalse(self.gate.disable()["enabled"])
        self.assertEqual(self.check().status, "disabled")
        self.assertIsNotNone(c.private_read(self.path)["gate"]["batch"])

    def test_missing_capability_errors_identity_and_policy_epochs(self):
        self.server.cap = {"enabled": False, "version": 1}
        with self.assertRaises(ValueError):
            self.enable()
        self.server.cap["enabled"] = True
        self.server.add(event(1))
        self.enable()
        self.check()
        for conn in ({**CONN, "key": "rotated"}, {**CONN, "org": "another"}, {**CONN, "grant_id": "another"}):
            other = g.Gate(conn, self.path, session=SID, client="test", home=self.root)
            with self.assertRaises(ValueError):
                other.status()
        with self.assertRaises(ValueError):
            self.make_gate(session="different-session").status()
        changed = {**POLICY, "git": []}
        with self.assertRaises(ValueError):
            self.gate.enable(changed)
        self.gate.enable(changed, new_epoch=True)
        archives = list(self.root.glob("gate.json.epoch-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertIsNotNone(c.private_read(archives[0])["pending"])
        self.assertEqual(self.gate.status()["after"], 0)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_timeout_cap_deadline_and_lock_contention_defer(self):
        self.enable()
        self.server.fail = TimeoutError()
        self.assertEqual(self.check().status, "defer")
        self.assertEqual(self.gate.status()["after"], 0)
        self.server.fail = None
        with c.locked(self.path):
            self.assertEqual(self.check().status, "defer")
        self.gate.deadline = 0
        self.assertEqual(self.check().status, "defer")

    def test_incomplete_ack_scan_and_failed_response_marker_do_not_advance(self):
        self.server.add(event(1))
        self.enable()
        self.check()
        batch, hashes = self.delivered()
        self.acknowledge(1)
        self.gate.max_pages = 0
        with self.assertRaises(ValueError):
            self.gate.reviewed(batch, hashes)
        self.gate.max_pages = 8
        marker = g.failures.path(self.root, SID)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("unresolved")
        with self.assertRaises(ValueError):
            self.gate.reviewed(batch, hashes)
        marker.unlink()
        self.assertEqual(self.gate.reviewed(batch, hashes)["after"], 1)

    def test_faults_before_and_after_pending_and_delivery_persistence(self):
        for boundary in (1, 2):  # immutable pending page, then delivery intent
            for after_write in (False, True):
                with self.subTest(boundary=boundary, after_write=after_write):
                    self.path.unlink(missing_ok=True)
                    self.server.events, self.server.head = [], 0
                    self.server.add(event(1))
                    self.enable()
                    count, real_save = 0, g._save
                    def fault(path, state):
                        nonlocal count
                        count += 1
                        if count == boundary:
                            if after_write:
                                real_save(path, state)
                            raise OSError("injected crash")
                        real_save(path, state)
                    with patch.object(g, "_save", side_effect=fault):
                        self.assertEqual(self.check().status, "defer")
                    self.gate = self.make_gate()
                    self.assertEqual(self.check().status, "defer")
                    self.assertEqual(self.batch()["page"]["events"][0]["seq"], 1)
                    self.assertEqual(self.gate.status()["after"], 0)

    def test_fault_after_worker_commit_never_replays_unhandled_work(self):
        self.server.add(event(1))
        self.enable()
        self.check()
        batch, hashes = self.delivered()
        self.acknowledge(1)
        real_save = g._save
        def fault(path, state):
            real_save(path, state)
            if state["pending"] is None:
                raise OSError("after commit")
        with patch.object(g, "_save", side_effect=fault), self.assertRaises(OSError):
            self.gate.reviewed(batch, hashes)
        self.assertIsNone(self.make_gate().status()["batch"])
        self.server.add(event(3))
        self.assertEqual(self.check().status, "defer")

    def test_fault_before_review_commit_leaves_pending_and_proofs_replayable(self):
        self.server.add(event(1))
        self.enable()
        self.check()
        batch, hashes = self.delivered()
        self.acknowledge(1)
        real_save = g._save
        def fault(path, state):
            if state["pending"] is None:
                raise OSError("before commit")
            real_save(path, state)
        with patch.object(g, "_save", side_effect=fault), self.assertRaises(OSError):
            self.gate.reviewed(batch, hashes)
        self.assertEqual(self.gate.status()["after"], 0)
        self.assertEqual(self.gate.reviewed(batch, hashes)["after"], 1)

    def test_faults_around_scan_progress_preserve_old_head(self):
        for boundary in (1, 2):  # complete proof, then discard completed snapshot
            for after_write in (False, True):
                with self.subTest(boundary=boundary, after_write=after_write):
                    self.path.unlink(missing_ok=True)
                    self.server.events, self.server.head = [], 0
                    self.server.add(event(1, sid=SID))
                    self.enable()
                    count, real_save = 0, g._save
                    def fault(path, state):
                        nonlocal count
                        count += 1
                        if count == boundary:
                            if after_write:
                                real_save(path, state)
                            raise OSError("scan persistence fault")
                        real_save(path, state)
                    with patch.object(g, "_save", side_effect=fault):
                        self.assertEqual(self.check().status, "defer")
                    self.server.add(event(2))
                    self.assertEqual(self.check().status, "defer")
                    self.assertEqual(self.batch()["page"]["events"][0]["seq"], 2)


class Supervisor(Fixture):
    def setUp(self):
        super().setUp()
        self.credential = self.root / "grant.json"
        self.grant = {**CONN, "token": "test-token", "grant_id": "grt_" + "b" * 24}
        c.private_write(self.credential, {"token": "test-token"})
        self.server.viewer = "grantee:" + self.grant["grant_id"]
        self.gate = g.Gate(self.grant, self.path, session=SID, client="test", home=self.root,
                           credential_file=self.credential)
        self.value = {**POLICY, "role": "supervisor"}
        self.server.add(event(1, "finding.proposed"))
        self.gate.enable(self.value)
        self.check()
        self.batch_id, self.hashes = self.delivered()

    def guarded(self, **kwargs):
        params = dict(cursor_file=self.path, budget_file=self.root / "budget.json",
                      batch=self.batch_id, page_file=None, session=SID,
                      tag=[], done=[], reopen=[], redone=[])
        args = argparse.Namespace(**{**params, **kwargs})
        return budget.reviewed(args, grant=self.grant, own_conn=lambda: (CONN, "test"), home=self.root, out=lambda _: None)

    def test_requires_guard_and_unresolved_outbox_blocks(self):
        with self.assertRaises(ValueError):
            self.gate.reviewed(self.batch_id, self.hashes)
        outbox = c.private_directory(self.root / "outbox") / "pending.json"
        c.private_write(outbox, {"intent": {"identity": c.identity(self.grant)}})
        with self.assertRaises(ValueError):
            self.gate.reviewed(self.batch_id, self.hashes, supervisor_review=self.guarded)
        outbox.unlink()
        self.assertEqual(self.gate.reviewed(self.batch_id, self.hashes, supervisor_review=self.guarded)["after"], 1)

    def test_required_tags_refuse_and_callback_crash_retains_pending(self):
        from test_investigation_budget import direction
        cache = budget.load(self.root / "budget.json", INV)
        cache["current"] = direction()
        cache["current"]["worker_session"] = PEER
        budget.save(self.root / "budget.json", cache)
        with self.assertRaises(ValueError):
            self.gate.reviewed(self.batch_id, self.hashes, supervisor_review=self.guarded)
        self.assertEqual(self.gate.status()["after"], 0)
        with self.assertRaises(TimeoutError):
            self.gate.reviewed(self.batch_id, self.hashes, supervisor_review=lambda: (_ for _ in ()).throw(TimeoutError()))
        self.assertIsNotNone(self.gate.status()["batch"])

    def test_crash_after_existing_guard_commit_reconciles_exact_intent(self):
        def commit_then_crash():
            self.guarded()
            raise OSError("response guard committed, process stopped")
        with self.assertRaises(OSError):
            self.gate.reviewed(self.batch_id, self.hashes, supervisor_review=commit_then_crash)
        raw = c.private_read(self.path)
        self.assertIsNone(raw["pending"])
        self.assertEqual(raw["gate"]["review_intent"], self.batch_id)
        self.assertIsNone(self.gate.status()["batch"])
        self.assertEqual(self.gate.status()["after"], 1)

    def test_unguarded_cursor_commit_is_detected(self):
        c.reviewed(self.grant, self.path, self.batch_id)
        with self.assertRaises(ValueError):
            self.gate.status()
        self.assertEqual(self.check().status, "defer")

    def test_required_post_timeout_keeps_batch_and_retries_same_body(self):
        from test_investigation_budget import direction
        cache = budget.load(self.root / "budget.json", INV)
        cache["current"] = direction()
        cache["current"]["worker_session"] = PEER
        budget.save(self.root / "budget.json", cache)
        own = {"investigation_id": "inv_" + "c" * 24, "session_id": SID, "conn": CONN, "client": "test"}
        def guard():
            return self.guarded(tag=["all:a"])
        with patch.object(budget, "_own", return_value=own), patch.object(budget, "ensure_direction", return_value=True):
            with patch.object(budget, "record", side_effect=ie.ist.FetchError("lost response")) as record:
                with self.assertRaises(ie.ist.FetchError):
                    self.gate.reviewed(self.batch_id, self.hashes, supervisor_review=guard)
                sent = copy.deepcopy(record.call_args.args)
            self.assertEqual(self.gate.status()["after"], 0)
            self.assertIsNotNone(budget.load(self.root / "budget.json", INV)["sent"])
            with patch.object(budget, "record", return_value=[10]) as record:
                self.assertEqual(self.gate.reviewed(self.batch_id, self.hashes, supervisor_review=guard)["after"], 1)
                self.assertEqual(record.call_args.args, sent)

    def test_different_budget_path_requires_epoch(self):
        with self.assertRaises(ValueError):
            self.gate.enable(self.value, budget_file=self.root / "different.json")
        self.assertEqual(self.gate.status()["budget_file"], str(self.root / "budget.json"))

    def test_all_semantic_revisions_and_retractions_are_selected(self):
        self.path.unlink()
        self.server.events, self.server.head = [], 0
        for seq, typ in enumerate(ie.SEMANTIC_TYPES, 1):
            self.server.add(event(seq, typ))
        self.gate.enable(self.value)
        self.check()
        self.assertEqual([e["type"] for e in self.batch()["page"]["events"]], list(ie.SEMANTIC_TYPES))

    def test_cli_cannot_substitute_empty_response_cache(self):
        wiring = SimpleNamespace(environ={}, runtime="codex", cli="/plugin/scripts/cardinal-storyboard", client="test",
                                 paths=SimpleNamespace(home=self.root / ".codex"), connection=lambda: CONN)
        argv = ["gate-reviewed", "--session", SID, "--investigation", INV, "--gate-file", str(self.path),
                "--credential-file", str(self.credential), "--budget-file", str(self.root / "empty.json"),
                "--batch", self.batch_id]
        for hashed in self.hashes:
            argv += ["--chunk-hash", hashed]
        args = cli.build_parser(wiring).parse_args(argv)
        handler = cli.CLI(wiring, args)
        with patch.object(handler, "_target", return_value=(self.grant, "test", INV)), self.assertRaises(ValueError):
            handler.cmd_gate_reviewed(args)
        self.assertEqual(self.gate.status()["after"], 0)

    def test_review_intent_faults_never_run_guard_before_persistence(self):
        from unittest.mock import Mock
        real_save = g._save
        guard = Mock(return_value=0)
        def fault(path, state):
            if state["gate"]["review_intent"]:
                raise OSError("intent not committed")
            real_save(path, state)
        with patch.object(g, "_save", side_effect=fault), self.assertRaises(OSError):
            self.gate.reviewed(self.batch_id, self.hashes, supervisor_review=guard)
        guard.assert_not_called()
        self.assertIsNone(c.private_read(self.path)["gate"]["review_intent"])
        self.assertEqual(self.gate.status()["after"], 0)


class CLI(Fixture):
    def run_cli(self, argv):
        wiring = SimpleNamespace(environ={}, runtime="codex", cli="/plugin/scripts/cardinal-storyboard", client="test",
                                 paths=SimpleNamespace(home=self.root / ".codex"), connection=lambda: CONN)
        out, err = io.StringIO(), io.StringIO()
        def activate(gate, value, **options):
            return gate.enable(value, new_epoch=options["new_epoch"], budget_file=options["budget_file"])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch(
                "cardinal_core.investigation_gate_runtime.activate", side_effect=activate):
            code = cli.cli_main(argv, wiring)
        self.assertEqual(err.getvalue(), "", err.getvalue())
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    def test_all_five_commands_and_worker_explicit_review(self):
        pfile = self.root / "policy.json"
        pfile.write_text(json.dumps(POLICY))
        common = ["--session", SID, "--investigation", INV, "--gate-file", str(self.path)]
        self.server.add(event(1))
        self.run_cli(["gate-enable", *common, "--policy", str(pfile)])
        first = self.check()
        status = json.loads(self.run_cli(["gate-status", *common]))
        hashes = []
        for index in range(1, status["chunks"] + 1):
            output = self.run_cli(["gate-read", *common, "--batch", first.batch, "--chunk", str(index)])
            self.assertLessEqual(len(output.encode()), 1000)
            hashes.extend(["--chunk-hash", json.loads(output)["hash"]])
        self.acknowledge(1)
        reviewed = json.loads(self.run_cli(["gate-reviewed", *common, "--batch", first.batch, *hashes]))
        self.assertEqual(reviewed["after"], 1)
        self.assertFalse(json.loads(self.run_cli(["gate-disable", *common]))["enabled"])


if __name__ == "__main__":
    unittest.main()

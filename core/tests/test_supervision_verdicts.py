"""Supervision verdicts (docs/specs/supervision-verdicts.md), plugin half:
the correction fold and verdict statuses, worker delivery and local tracking
of verification.recorded, `revision` on checkpoints ("auto" resolved once into
a checkpoint outbox), and the supervisor CLI: `verdict`, `challenges`,
`events` and `grant --succeeds-credential-file`."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from test_delegated_supervision import (Base, INV, SID, SUPERVISOR, WORKER, MEMBER, ack, challenge, checkpoint, ev,
                                        ruling)
from cardinal_core import investigation_cli as cli
from cardinal_core import investigation_consumer as consumer
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_reader as reader
from cardinal_core import investigation_state as ist
from cardinal_core import investigation_state_sync as sync
from cardinal_core import supervision_status as sv

REPO = "github.com/acme/restore"
A, B = "a" * 40, "b" * 40
RCPT = "rcpt_" + "c" * 24
GID = "grt_" + "d" * 24
NEW_GID = "grt_" + "e" * 24


def verdict(seq, of, correction, verdict_, scope="code_review", text="Replay still flakes on batch 3.", commit=A):
    payload = {"verifies": of, "verdict": verdict_, "scope": scope, "subject": {"repo": REPO, "commit": commit},
               "text": text, "evidence": [RCPT]}
    if correction is not None:
        payload["correction"] = correction
    return ev(seq, "verification.recorded", payload, authority="supervisor_attestation", to=SID)


def correction(seq, *refs, revision=True, artifacts=False):
    e = checkpoint(seq, *refs)
    if revision:
        e["payload"]["revision"] = {"repo": REPO, "commit": A}
    if artifacts:
        e["payload"]["artifacts"] = [{"path": "r.txt", "sha256": "d" * 64}]
    return e


def token(gid, scopes=("read", "advise", "supervise")):
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return (f"{part({'typ': 'CardinalInvestigation', 'alg': 'HS256'})}."
            f"{part({'org': 'org', 'inv': INV, 'gid': gid, 'scopes': list(scopes), 'exp': 9999999999})}.sig")


class Fake:
    """sync._post for the supervisor routes, over one event stream."""

    def __init__(self, events=(), caps=("supervision_verifications", "grant_succession"), principals=None):
        self.events, self.calls, self.caps, self.principals = list(events), [], caps, principals or {}
        self.answers: dict = {}

    def __call__(self, conn, tool, body, client=None, opener=None, timeout=None):
        self.calls.append((tool, json.loads(json.dumps(body)), conn.get("token")))
        got = self.answers.get(tool)
        if isinstance(got, list):
            got = got.pop(0)
        if isinstance(got, Exception):
            raise got
        if callable(got):
            return got(body)
        if got is not None:
            return got
        if tool == "get-investigation":
            return {"investigation_id": INV, "capabilities": {c: {"enabled": True} for c in self.caps}}
        if tool == "read-investigation-events":
            page = [e for e in self.events if e["seq"] > body["after"]][:body["limit"]]
            head = max((e["seq"] for e in self.events), default=0)
            return {"investigation_id": INV, "events": json.loads(json.dumps(page)), "head_seq": head,
                    "principals": self.principals, "viewer": {"principal": {"kind": "grantee", "id": GID}}}
        raise AssertionError(f"unexpected route {tool}")

    def sent(self, tool):
        return [body for t, body, _ in self.calls if t == tool]


class StatusMirror(Base):
    def test_rejected_and_directive_rejected_still_owe_work(self):
        self.assertIn(sv.REJECTED, sv.RESURFACE)
        self.assertIn(sv.DIRECTIVE_REJECTED, sv.RESURFACE)
        self.assertIn(sv.DIRECTIVE_REJECTED, sv.OPEN_DIRECTIVE)
        self.assertNotIn(sv.VERIFIED, sv.RESURFACE)
        self.assertEqual(sv.resurface_target(1, {"status": sv.REJECTED, "ruling_seq": None}), 1)
        self.assertEqual(sv.resurface_target(1, {"status": sv.DIRECTIVE_REJECTED, "ruling_seq": 3}), 3)
        self.assertIsNone(sv.resurface_target(1, {"status": sv.UNVERIFIABLE, "ruling_seq": None}))

    def test_a_verdict_answered_by_ref_must_be_earlier(self):
        # "#v" pointing forward (hand-built) is not an answer: the plain event displaces nothing judged.
        stream = [challenge(1), ack(2, 1, "accepted"), correction(3, "#1"), checkpoint(4, "#1", "#5"),
                  verdict(5, 1, 3, "rejected")]
        info = sv.challenge_statuses(stream)[1]
        self.assertEqual((info["status"], info["correction_seq"]), ("rejected", 3))

    def test_the_report_names_the_correction_and_every_verdict(self):
        stream = [challenge(1), ack(2, 1, "accepted"), correction(3, "#1"), verdict(4, 1, 3, "rejected"),
                  correction(5, "#1", "#4", revision=False), verdict(6, 1, 5, "verified")]
        got = sv.challenge_report(stream)[1]
        self.assertEqual((got["status"], got["correction"]["seq"], got["position"]["seq"]), ("verified", 5, 2))
        self.assertEqual([v["seq"] for v in got["verdicts"]], [4, 6])


class Fuzz(Base):
    """Random streams replayed through track_supervision under a small cap
    agree with the derivation over the full stream (fixed seeds)."""

    @staticmethod
    def stream(rng):
        evs, chs = [], []
        for _ in range(rng.randint(20, 120)):
            seq = len(evs) + 1
            st = sv.challenge_statuses(evs)
            if rng.random() < 0.15 or not chs:
                evs.append(challenge(seq, to=SID))
                chs.append(seq)
                continue
            c = rng.choice(chs)
            info = st[c]
            if info["status"] == "open":
                evs.append(ack(seq, c, rng.choice(["accepted", "accepted", "declined"])))
            elif info["status"] == "declined" and rng.random() < 0.5:
                evs.append(ruling(seq, c, rng.choice(["upheld", "upheld", "withdrawn"])))
            elif info["status"] == "directive":
                evs.append(ack(seq, info["ruling_seq"], "accepted"))
            elif info["status"] in sv.CORRECTED and rng.random() < 0.5:
                k, vs = info["correction_seq"], sv.verdicts_on(evs, c)
                scope = rng.choice(["code_review", "test_run"])
                mine = [v for v in sv.active(vs) if v["payload"]["correction"] == k and v["payload"]["scope"] == scope]
                p = {"verifies": c, "correction": k, "verdict": rng.choice(["verified", "rejected", "unverifiable"]),
                     "scope": scope, "subject": {}, "text": "t", "evidence": []}
                if mine:
                    if len(sv.chain(vs, mine[0]["seq"])) > 3:
                        continue
                    p["supersedes_verdict"] = mine[0]["seq"]
                evs.append(ev(seq, "verification.recorded", p, authority="supervisor_attestation", to=SID))
            else:
                refs = ["#%d" % c]
                if info.get("ruling_seq") and rng.random() < 0.5:
                    refs = ["#%d" % info["ruling_seq"]]
                vs = sv.verdicts_on(evs, c)
                if vs and rng.random() < 0.4:
                    refs.append("#%d" % rng.choice(vs)["seq"])
                e = checkpoint(seq, *refs)
                if rng.random() < 0.35:
                    e["payload"]["revision"] = {"repo": REPO, "commit": A}
                evs.append(e)
        return evs

    KEYS = ("status", "correction_seq", "verification_seq", "ruling_seq")

    def replay(self, evs, cap, batches):
        """Track `evs` in batches of the sizes `batches` yields, under `cap`,
        checking after EVERY batch that each non-final challenge kept (in
        the events or as stubs) derives what the full stream so far does."""
        import copy
        b = {"investigation_id": INV, "is_author": True, "capabilities": self.binding["capabilities"]}
        with patch.object(ie, "MAX_SUPERVISION_EVENTS", cap):
            i = 0
            while i < len(evs):
                n = batches()
                ie.track_supervision(b, copy.deepcopy(evs[i:i + n]), SID)
                i += n
                st = b.get("supervision") or {}
                kept = sv.challenge_statuses(st.get("events") or [])
                stubs = sv.challenge_statuses(st.get("stubs") or [])
                for c, info in sv.challenge_statuses(evs[:i]).items():
                    got = kept.get(c) or stubs.get(c)
                    if info["status"] in ("withdrawn", "overruled") or got is None:
                        continue
                    self.assertEqual({k: got[k] for k in self.KEYS}, {k: info[k] for k in self.KEYS}, (i, c))
        return b

    def test_compaction_never_changes_a_derivation(self):
        import random
        for seed in list(range(40)) + [5, 10, 12, 15, 18]:
            rng = random.Random(seed)
            evs = self.stream(rng)
            with self.subTest(seed=seed):
                self.replay(evs, rng.choice([8, 12, 20]), lambda: rng.randint(1, 6))

    def test_a_partial_drop_never_promotes_a_leftover_summary(self):
        # Reviewer's F1 stream (cap 12): dropping non-essential events one by one left
        # plain #55 as the local correction once #51, #53 and #43 were gone.
        c1 = [challenge(1), ack(2, 1, "accepted"), correction(36, "#1"), verdict(40, 1, 36, "rejected"),
              checkpoint(43, "#1", "#40"), checkpoint(46, "#1"), verdict(51, 1, 43, "rejected"),
              checkpoint(53, "#1", "#51"), checkpoint(55, "#1"), verdict(56, 1, 53, "rejected"),
              verdict(57, 1, 53, "rejected", scope="test_run")]
        evs = sorted(c1 + [challenge(s) for s in range(3, 9)], key=lambda e: e["seq"])
        full = sv.challenge_statuses(evs)[1]
        self.assertEqual((full["status"], full["correction_seq"]), ("rejected", 53))
        self.replay(evs, 12, lambda: 1)

    def test_a_directive_trace_at_the_default_cap(self):
        # Supervisor challenge #83: 102 events compacting to 100. The partial drop kept
        # plain #11 without #8 / #10 and ended at directive_addressed / 17.
        c1 = [challenge(1), ack(2, 1, "declined"), ruling(5, 1), ack(6, 5, "accepted"),
              checkpoint(7, "#5"), verdict(8, 1, 7, "verified"), verdict(10, 1, 7, "rejected", scope="test_run"),
              checkpoint(11, "#5"), verdict(12, 1, 7, "unverifiable", scope="fixture_validation"),
              checkpoint(15, "#5", "#12"), checkpoint(17, "#5", "#8"), verdict(18, 1, 15, "rejected")]
        pad = [challenge(s) for s in (3, 4, 9, 13, 14, 16) + tuple(range(19, 103))]
        evs = sorted(c1 + pad, key=lambda e: e["seq"])
        self.assertEqual(len(evs), 102)
        full = sv.challenge_statuses(evs)[1]
        self.assertEqual((full["status"], full["correction_seq"], full["verification_seq"]),
                         ("directive_rejected", 15, 18))
        b = self.replay(evs, ie.MAX_SUPERVISION_EVENTS, lambda: 1)
        self.assertLessEqual(len(b["supervision"]["events"]), ie.MAX_SUPERVISION_EVENTS)


class WorkerDelivery(Base):
    REJECTION = ('Your correction #3 for #1 was rejected (#4): "Replay still flakes on batch 3.". Checkpoint a new '
                 'correction with refs ["#1", "#4"] (and "revision": "auto" when it is code).')

    def corrected(self):
        self.arrive(challenge(1))
        self.arrive(ack(2, 1, "accepted"), correction(3, "#1"))
        self.assertEqual(self.statuses(), {1: "addressed"})

    def test_a_rejection_is_delivered_and_resurfaces_until_a_new_correction(self):
        self.corrected()
        out = self.arrive(verdict(4, 1, 3, "rejected"))
        self.assertEqual(len(out), 1)
        self.assertIn(self.REJECTION, out[0])
        self.assertIn(f"ack {INV} 4 --session {SID} --disposition noted", out[0])
        self.assertNotIn(ie.ACK_GUIDANCE, out[0])
        self.assertEqual(self.statuses(), {1: "rejected"})
        self.assertTrue(self.marker())
        shown = [x for _ in range(ie.RESURFACE_EVERY) for x in self.boundary()]
        self.assertEqual(len(shown), 1)
        self.assertIn(self.REJECTION, shown[0])
        self.assertIn("still open: your correction was rejected", shown[0])
        # A summary citing only the challenge does not hide it; an answer by #v does.
        self.arrive(checkpoint(5, "#1"))
        self.assertEqual(self.statuses(), {1: "rejected"})
        self.arrive(checkpoint(6, "#1", "#4"))
        self.assertEqual(self.statuses(), {1: "addressed"})
        self.assertFalse(self.marker())

    def test_verified_and_unverifiable_are_delivered_once(self):
        self.corrected()
        out = self.arrive(verdict(4, 1, 3, "verified", text="Fix holds."))
        self.assertIn(f"The supervisor verified your correction #3 for #1 (code_review @ {REPO}@aaaaaaa): "
                      '"Fix holds.".', out[0])
        self.assertEqual(self.statuses(), {1: "verified"})
        self.assertEqual([x for _ in range(3 * ie.RESURFACE_EVERY) for x in self.boundary()], [])
        out = self.arrive(verdict(5, 1, 3, "unverifiable", scope="deployment", text="No prod access."))
        self.assertIn("recorded your correction #3 for #1 as unverifiable (deployment)", out[0])
        self.assertEqual(self.statuses(), {1: "verified"})

    def test_a_verdict_with_an_unknown_scope_counts_for_nothing(self):
        odd = verdict(4, 1, 3, "rejected", scope="vibes")
        self.assertEqual(sv.verdicts_on([odd], 1), [])

    def test_revision_guidance_only_with_a_challenge_and_the_capability(self):
        b = ie.read_binding(self.home, SID)
        cue = dict(ev(1, "cue.added", {"text": "Look."}), investigation_id=INV)
        self.assertNotIn(ie.REVISION_GUIDANCE, ie.render([dict(challenge(1), investigation_id=INV)], SID, INV,
                                                         binding=b))
        b["capabilities"] = dict(b["capabilities"], checkpoint_revision={"enabled": True})
        self.assertIn(ie.REVISION_GUIDANCE, ie.render([dict(challenge(1), investigation_id=INV)], SID, INV,
                                                      binding=b))
        self.assertNotIn(ie.REVISION_GUIDANCE, ie.render([cue], SID, INV, binding=b))
        self.assertNotIn("revision", ie.ACK_GUIDANCE)

    def test_a_directive_rejection_says_so_while_its_challenge_is_a_stub(self):
        stubs = [challenge(1), ack(2, 1, "declined"), ruling(3, 1), ack(4, 3, "accepted"), correction(5, "#3"),
                 verdict(6, 1, 5, "rejected")]
        b = {"supervision": {"events": [], "stubs": stubs}}
        e = dict(verdict(6, 1, 5, "rejected"), investigation_id=INV)
        out = ie.render_verification(e, SID, True, "grantee:x", '"t"', b)
        self.assertIn("Directive #3 stays binding until", out)
        self.assertNotIn("not binding", out)

    def test_a_verdict_takes_only_noted(self):
        target = verdict(4, 1, 3, "rejected")
        self.assertIn("--disposition noted", ie.ack_rule(target, "accepted", "x"))
        self.assertIsNone(ie.ack_rule(target, "noted", None))

    def test_a_verdict_on_a_challenge_this_session_never_received_is_not_delivered(self):
        other = dict(verdict(4, 9, 3, "verified"), to_session_id=None)
        self.assertEqual(ie.deliverable([other], SID, INV, ie.read_binding(self.home, SID)), [])

    def test_a_rejected_directive_correction_blocks_stop_once_more(self):
        self.arrive(challenge(1), ack(2, 1, "declined"), ruling(3, 1), ack(4, 3, "accepted"))
        stops = []
        self.assertTrue(ie.supervision_stop(self.home, SID, stops.append))
        self.assertFalse(ie.supervision_stop(self.home, SID, stops.append))
        self.arrive(correction(5, "#3"))
        self.assertEqual(self.statuses(), {1: "directive_addressed"})
        self.assertFalse(ie.supervision_stop(self.home, SID, stops.append))
        out = self.arrive(verdict(6, 1, 5, "rejected"))
        self.assertIn("Directive #3 stays binding until", out[0])
        self.assertNotIn("not binding", out[0])
        self.assertEqual(self.statuses(), {1: "directive_rejected"})
        shown = [x for _ in range(ie.RESURFACE_EVERY) for x in self.boundary()]
        self.assertIn("Binding: directive #3 is still open.", shown[0])
        self.assertNotIn("stays binding until", shown[0])
        self.assertNotIn(3, ie.read_binding(self.home, SID)["supervision"]["stop_blocked"])
        self.assertTrue(ie.supervision_stop(self.home, SID, stops.append))
        self.assertIn("Before you stop: directive #3 is still open.", stops[-1])
        self.assertIn("Your correction #5 for #1 was rejected (#6)", stops[-1])
        self.assertFalse(ie.supervision_stop(self.home, SID, stops.append))


class LocalTracking(Base):
    def kept(self):
        st = ie.read_binding(self.home, SID)["supervision"]
        return st["events"], st["stubs"]

    def all_statuses(self):
        events, stubs = self.kept()
        return {c: (i["status"], i["correction_seq"]) for c, i in sv.challenge_statuses(events + stubs).items()}

    def test_slim_keeps_what_the_fold_reads(self):
        self.arrive(challenge(1), ack(2, 1, "accepted"), correction(3, "#1", artifacts=True),
                    verdict(4, 1, 3, "rejected", text="x" * 900))
        events, _ = self.kept()
        by = {e["seq"]: e["payload"] for e in events}
        self.assertEqual(by[3], {"semantic_id": "decision_3", "refs": ["#1"], "has_revision": True,
                                 "has_artifacts": True})
        self.assertEqual(set(by[4]) - {"_cut"}, {"verifies", "correction", "verdict", "scope", "text"})
        revised = dict(verdict(5, 1, 3, "verified"), payload=dict(verdict(5, 1, 3, "verified")["payload"],
                                                                   supersedes_verdict=4))
        revised["producer"] = dict(revised["producer"], lineage_root_id="grt_" + "f" * 24)
        self.arrive(revised)
        kept = {e["seq"]: e for e in self.kept()[0]}[5]
        self.assertEqual(kept["payload"]["supersedes_verdict"], 4)
        self.assertEqual(kept["producer"]["lineage_root_id"], "grt_" + "f" * 24)
        self.assertEqual(len(by[4]["text"]), ie.MAX_KEPT_VERDICT)
        self.assertEqual(ie._challenges_of(verdict(4, 1, 3, "rejected", text="#9"), {}), {1})

    def test_essential_events_keep_every_input_of_the_fold(self):
        stream = [challenge(10), ack(11, 10, "accepted"), correction(12, "#10"), verdict(13, 10, 12, "rejected"),
                  checkpoint(15, "#10", "#13"), checkpoint(17, "#10")]
        info = sv.challenge_report(stream)[10]
        self.assertEqual((info["status"], info["correction_seq"], info["previous"]["seq"]), ("addressed", 15, 12))
        # #15 answered #13 on the correction it replaced: both stay, so #15 stays substantive.
        keep = ie.essential_events(stream, 10, info)
        self.assertEqual(keep, {10, 11, 12, 13, 15, 17})
        self.assertEqual(sv.challenge_statuses([e for e in stream if e["seq"] in keep])[10],
                         sv.challenge_statuses(stream)[10])
        # A plain correction keeps nothing before it.
        plain = [challenge(10), ack(11, 10, "accepted"), correction(12, "#10"), verdict(13, 10, 12, "rejected"),
                 correction(15, "#10"), verdict(16, 10, 15, "verified")]
        self.assertEqual(ie.essential_events(plain, 10, sv.challenge_report(plain)[10]), {10, 11, 15, 16})

    def test_a_rejection_of_a_replaced_correction_answers_nothing(self):
        # The supervisor's reproducer: #1 verified at correction #7 (verdict #8); an older
        # rejection #6 of correction #5; 100 other challenges push #1 into stubs; then #200
        # cites [#1, #6]. The full stream stays verified at #7, and so does local tracking.
        stream = [challenge(1), ack(2, 1, "accepted"), correction(5, "#1"), verdict(6, 1, 5, "rejected"),
                  correction(7, "#1"), verdict(8, 1, 7, "verified")] + [challenge(s) for s in range(9, 109)]
        self.arrive(*stream)
        self.assertNotIn(1, [e["seq"] for e in self.kept()[0]])
        late = checkpoint(200, "#1", "#6")
        full = sv.challenge_statuses(stream + [late])[1]
        self.assertEqual((full["status"], full["correction_seq"]), ("verified", 7))
        self.arrive(late)
        self.assertEqual(self.all_statuses()[1], ("verified", 7))

    def test_verdicts_survive_compaction(self):
        stream = [challenge(1), ack(2, 1, "accepted"), correction(3, "#1"), verdict(4, 1, 3, "rejected"),
                  challenge(10), ack(11, 10, "accepted")] + [checkpoint(s, "#10") for s in range(12, 30)]
        with patch.object(ie, "MAX_SUPERVISION_EVENTS", 8):
            self.arrive(*stream)
            self.assertEqual(self.all_statuses(), {1: ("rejected", 3), 10: ("addressed", 29)})
            self.assertLessEqual(len(self.kept()[0]), 8)
        with patch.object(ie, "MAX_SUPERVISION_EVENTS", 4):
            self.arrive(checkpoint(30, "#10"))
            self.assertEqual(self.all_statuses(), {1: ("rejected", 3), 10: ("addressed", 30)})
            # The rejection still re-surfaces from what is kept.
            shown = [x for _ in range(ie.RESURFACE_EVERY) for x in self.boundary()]
            self.assertTrue(any("was rejected (#4)" in x for x in shown), shown)

    def test_a_stubbed_challenge_keeps_only_citing_events_that_change_it(self):
        with patch.object(ie, "MAX_SUPERVISION_EVENTS", 3):
            self.arrive(challenge(1), ack(2, 1, "accepted"), correction(3, "#1"), verdict(4, 1, 3, "verified"),
                        challenge(10))
            events, stubs = self.kept()
            self.assertEqual([e["seq"] for e in events], [10])
            self.assertEqual(self.all_statuses()[1], ("verified", 3))
            self.arrive(checkpoint(5, "#1"))           # a summary: changes nothing, not kept
            self.assertNotIn(5, [e["seq"] for e in self.kept()[0] + self.kept()[1]])
            self.arrive(correction(6, "#1"))           # a new revision re-opens verification
            self.assertEqual(self.all_statuses()[1], ("addressed", 6))


class GitRepo:
    """A real checkout (origin github.com:Acme/Restore) as the cwd, and a stub `gh` (open PR 7) on PATH."""

    def make_repo(self):
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("remote", "add", "origin", "git@github.com:Acme/Restore.git")
        (self.repo / "f.txt").write_text("one\n")
        self.git("add", ".")
        self.git("commit", "-qm", "one")
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.gh('{"number": 7, "state": "OPEN"}')
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        os.environ["PATH"] = f"{self.bin}:/usr/bin:/bin"

    def tree_of_index(self):
        return (self.repo / ".git" / "index").read_bytes()

    def git(self, *args):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        return subprocess.run(["git", *args], cwd=self.repo, env=env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def gh(self, answer, code=0):
        (self.bin / "gh").write_text(f"#!/bin/sh\necho '{answer}'\nexit {code}\n")
        (self.bin / "gh").chmod(0o755)


class CheckpointRevision(GitRepo, Base):
    def setUp(self):
        super().setUp()
        self.binding["capabilities"] = dict(self.binding["capabilities"], checkpoint_revision={"enabled": True})
        ie.write_binding(self.home, SID, self.binding)
        self.make_repo()

    def checkpoint_cli(self, data, post):
        with patch("sys.stdin", io.StringIO(json.dumps(data))), patch.object(sync, "_post", side_effect=post):
            return self.run_cli("checkpoint", "--session", SID)

    def test_revision_shape(self):
        ok = {"repo": REPO, "commit": A, "pr": 7, "dirty": True}
        self.assertEqual(ie.validate_revision(ok), ok)
        for bad in ({"repo": "acme/restore", "commit": A}, {"repo": REPO, "commit": "ABC"},
                    {"repo": REPO, "commit": A, "pr": 0}, {"repo": REPO, "commit": A, "dirty": False},
                    {"repo": REPO, "commit": A, "branch": "x"}, "auto"):
            with self.assertRaises(ie.CheckpointInputError):
                ie.validate_revision(bad)
        # Every semantic type takes it.
        got = ie.checkpoint_events([{"type": "question.opened", "id": "q", "statement": "s",
                                     "revision": {"repo": REPO, "commit": A}}])
        self.assertEqual(got[0]["payload"]["revision"], {"repo": REPO, "commit": A})

    def test_capability_off_strips_revision_with_a_warning(self):
        events = ie.checkpoint_events([{"type": "finding.proposed", "id": "x", "statement": "s",
                                        "revision": {"repo": REPO, "commit": A}}])
        err = io.StringIO()
        with patch("sys.stderr", err):
            out = ie.checkpoint_capabilities(events, {"capabilities": {}})
        self.assertNotIn("revision", out[0]["payload"])
        self.assertIn("does not support checkpoint revision", err.getvalue())

    def test_auto_is_stripped_without_the_capability(self):
        self.binding["capabilities"] = {"delegated_supervision": {"enabled": True}}
        ie.write_binding(self.home, SID, self.binding)
        bodies = []

        def ok(conn, tool, body, **kw):
            bodies.append(body)
            return {"investigation_id": INV, "events": [{"seq": 9, "type": "finding.proposed", "payload": {}}]}

        code, _, err = self.checkpoint_cli([{"type": "finding.proposed", "id": "x", "statement": "s",
                                              "revision": "auto"}], ok)
        self.assertEqual(code, 0, err)
        self.assertNotIn("revision", bodies[0]["events"][0]["payload"])
        self.assertIn("does not support checkpoint revision", err)

    def test_resolve_records_what_is_true(self):
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(ie.resolve_revision(str(self.repo)), {"repo": REPO, "commit": head, "pr": 7})
        (self.repo / "f.txt").write_text("changed\n")
        self.gh('{"number": 7, "state": "MERGED"}')
        got = ie.resolve_revision(str(self.repo))
        self.assertEqual({k: v for k, v in got.items() if k != "tree"}, {"repo": REPO, "commit": head, "dirty": True})
        self.assertRegex(got["tree"], r"^[0-9a-f]{40}$")
        with self.assertRaises(ie.CheckpointInputError):
            ie.resolve_revision(str(self.home))

    def test_two_dirty_snapshots_at_the_same_head_have_different_trees(self):
        head = self.git("rev-parse", "HEAD")
        (self.repo / "f.txt").write_text("changed\n")
        self.git("add", "f.txt")   # a staged change: the real index must stay exactly as it is
        index = self.tree_of_index()
        (self.repo / "new.txt").write_text("untracked\n")
        first = ie.resolve_revision(str(self.repo))
        (self.repo / "new.txt").write_text("untracked, edited\n")
        second = ie.resolve_revision(str(self.repo))
        self.assertEqual((first["commit"], second["commit"]), (head, head))
        self.assertTrue(first["dirty"] and second["dirty"])
        self.assertNotEqual(first["tree"], second["tree"])
        self.assertEqual(self.tree_of_index(), index)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "f.txt")
        self.assertEqual(self.git("ls-files", "--others", "--exclude-standard"), "new.txt")
        self.assertEqual(list(Path(tempfile.gettempdir()).glob("cardinal-snapshot-*")), [])
        # Clean again: no dirty, no tree.
        self.git("reset", "-q", "--hard")
        (self.repo / "new.txt").unlink()
        self.assertNotIn("tree", ie.resolve_revision(str(self.repo)))

    def test_the_snapshot_is_seeded_from_the_real_index(self):
        # A skip-worktree file keeps its index entry: its local edit is not part of the snapshot.
        head_blob = self.git("rev-parse", "HEAD:f.txt")
        self.git("update-index", "--skip-worktree", "f.txt")
        (self.repo / "f.txt").write_text("local only\n")
        (self.repo / "new.txt").write_text("untracked\n")
        got = ie.resolve_revision(str(self.repo))
        self.assertEqual(self.git("rev-parse", f"{got['tree']}:f.txt"), head_blob)
        self.assertEqual(self.git("rev-parse", f"{got['tree']}:new.txt"), self.git("hash-object", "new.txt"))

    def test_a_failing_filter_leaves_the_snapshot_unbound_and_the_checkpoint_succeeds(self):
        self.git("config", "filter.broken.clean", "false")
        self.git("config", "filter.broken.required", "true")
        (self.repo / ".gitattributes").write_text("*.dat filter=broken\n")
        (self.repo / "x.dat").write_text("data\n")
        got = ie.resolve_revision(str(self.repo))
        self.assertTrue(got["dirty"])
        self.assertNotIn("tree", got)
        bodies = []

        def ok(conn, tool, body, **kw):
            bodies.append(body)
            return {"investigation_id": INV, "events": [{"seq": 9, "type": "finding.proposed", "payload": {}}]}

        code, _, err = self.checkpoint_cli([{"type": "finding.proposed", "id": "x", "statement": "s",
                                              "revision": "auto"}], ok)
        self.assertEqual(code, 0, err)
        self.assertEqual(bodies[0]["events"][0]["payload"]["revision"]["dirty"], True)
        self.assertNotIn("tree", bodies[0]["events"][0]["payload"]["revision"])

    def test_a_dirty_submodule_leaves_the_snapshot_unbound(self):
        sub = self.home / "subrepo"
        sub.mkdir()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "s"]):
            subprocess.run(["git", *args], cwd=sub, env=env, check=True, capture_output=True)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
        self.git("commit", "-qm", "sub")
        self.assertNotIn("dirty", ie.resolve_revision(str(self.repo)))
        (self.repo / "sub" / "inside.txt").write_text("untracked in the submodule\n")
        got = ie.resolve_revision(str(self.repo))
        self.assertTrue(got["dirty"])
        self.assertNotIn("tree", got)

    def test_an_unread_status_is_dirty_and_unbound_never_clean(self):
        with patch.object(ie, "_status", return_value=None):
            got = ie.resolve_revision(str(self.repo))
        self.assertTrue(got["dirty"])
        self.assertNotIn("tree", got)

    def test_untracked_files_count_whatever_the_config_says(self):
        self.git("config", "status.showUntrackedFiles", "no")
        (self.repo / "new.txt").write_text("untracked\n")
        got = ie.resolve_revision(str(self.repo))
        self.assertTrue(got["dirty"])
        self.assertEqual(self.git("rev-parse", f"{got['tree']}:new.txt"), self.git("hash-object", "new.txt"))

    def test_an_untracked_nested_repository_leaves_the_snapshot_unbound(self):
        nested = self.repo / "vendor" / "lib"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=nested, check=True, capture_output=True)
        got = ie.resolve_revision(str(self.repo / "vendor"))   # from a subdirectory: paths are from the top
        self.assertTrue(got["dirty"])
        self.assertNotIn("tree", got)

    def test_status_takes_no_optional_locks(self):
        seen = []
        real = subprocess.run

        def run(args, **kw):
            seen.append((args, (kw.get("env") or {}).get("GIT_OPTIONAL_LOCKS")))
            return real(args, **kw)

        with patch("subprocess.run", side_effect=run):
            ie._status(str(self.repo))
        self.assertEqual(seen[0][1], "0")

    def test_without_the_real_index_the_snapshot_is_unbound(self):
        (self.repo / "new.txt").write_text("untracked\n")
        with patch("shutil.copyfile", side_effect=OSError("gone")):
            self.assertIsNone(ie.snapshot_tree(str(self.repo)))

    def test_tree_only_with_dirty(self):
        ok = {"repo": REPO, "commit": A, "dirty": True, "tree": B}
        self.assertEqual(ie.validate_revision(ok), ok)
        for bad in ({"repo": REPO, "commit": A, "tree": B}, {"repo": REPO, "commit": A, "dirty": True, "tree": "x"}):
            with self.assertRaises(ie.CheckpointInputError):
                ie.validate_revision(bad)

    def test_auto_resolves_once_and_a_retry_resends_the_identical_batch(self):
        head = self.git("rev-parse", "HEAD")
        data = [{"type": "finding.revised", "id": "finding_retry", "statement": "Fixed.", "refs": ["#1"],
                 "revision": "auto"}]
        bodies = []

        def lost(conn, tool, body, **kw):
            bodies.append(json.loads(json.dumps(body)))
            raise ist.FetchError("checkpoint-investigation failed: timed out", network=True, ambiguous=True)

        code, _, err = self.checkpoint_cli(data, lost)
        self.assertEqual(code, 3, err)
        self.assertIn("run the same command again", err)
        # Meanwhile a new commit lands and gh is unreachable.
        (self.repo / "f.txt").write_text("two\n")
        self.git("commit", "-qam", "two")
        self.gh("", code=1)

        def ok(conn, tool, body, **kw):
            bodies.append(json.loads(json.dumps(body)))
            return {"investigation_id": INV, "duplicate": True,
                    "events": [{"seq": 9, "type": "finding.revised", "payload": body["events"][0]["payload"]}]}

        code, out, err = self.checkpoint_cli(data, ok)
        self.assertEqual(code, 0, err)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0]["events"][0]["payload"]["revision"], {"repo": REPO, "commit": head, "pr": 7})
        self.assertIn(f"replayed an earlier send at {head[:7]}; HEAD is now", err)
        self.assertIn("checkpoint again for the new commit", err)
        self.assertEqual(list(ie.sessions_dir(self.home).glob(f"{SID}.checkpoint.*.json")), [])
        # The next send of the same input resolves afresh: the new commit, no PR.
        bodies.clear()
        code, _, _ = self.checkpoint_cli(data, ok)
        self.assertEqual(bodies[0]["events"][0]["payload"]["revision"],
                         {"repo": REPO, "commit": self.git("rev-parse", "HEAD")})

    def test_a_definite_refusal_drops_the_outbox_and_auto_outside_a_worktree_is_usage(self):
        data = [{"type": "finding.proposed", "id": "x", "statement": "s", "revision": "auto"}]
        refused = sync.ServerError(422, {"error": "semantic_ref_not_found"}, "x")
        code, _, _ = self.checkpoint_cli(data, lambda *a, **k: (_ for _ in ()).throw(refused))
        self.assertEqual(code, 1)
        self.assertEqual(list(ie.sessions_dir(self.home).glob(f"{SID}.checkpoint.*.json")), [])
        os.chdir(self.home)
        code, _, err = self.checkpoint_cli(data, lambda *a, **k: self.fail("sent"))
        self.assertEqual(code, 2)
        self.assertIn("needs a git worktree", err)


class SupervisorBase(Base):
    def setUp(self):
        super().setUp()
        self.dir = self.home / "sup"
        self.dir.mkdir(mode=0o700)
        self.cred = self.dir / "grant.json"
        consumer.private_write(self.cred, {"token": token(GID), "origin": MEMBER["origin"]}, create=True)
        self.stream = [challenge(1), ack(2, 1, "accepted"), correction(3, "#1"), checkpoint(4, "#1")]
        for e in self.stream:
            e["investigation_id"] = INV
        self.fake = Fake(self.stream)

    def call(self, *argv):
        with patch.object(sync, "_post", side_effect=self.fake):
            return self.run_cli(*argv)

    def verdict_cli(self, *extra, seq="1"):
        return self.call("verdict", seq, "--verified", "--scope", "code_review", "--correction", "3", "--repo", REPO,
                        "--commit", A, "--text", "Fix holds.", "--evidence", RCPT, "--credential-file",
                        str(self.cred), *extra)


class VerdictCli(SupervisorBase):
    def recorded(self, body):
        return {"event": {"investigation_id": INV, "seq": 5, "type": "verification.recorded",
                          "payload": body["payload"], "producer": SUPERVISOR}}

    def test_a_verdict_is_sent_from_an_outbox_written_before_the_first_post(self):
        lost = ist.FetchError("record-verification failed: reset", network=True, ambiguous=True)
        self.fake.answers["record-verification"] = [lost, self.recorded]
        code, _, err = self.verdict_cli()
        self.assertEqual(code, 3, err)
        boxes = list((self.dir / "outbox").glob("verdict-*.json"))
        self.assertEqual(len(boxes), 1)
        intent = consumer.private_read(boxes[0])["intent"]
        self.assertEqual(intent["payload"], {"verifies": 1, "correction": 3, "verdict": "verified",
                                             "scope": "code_review", "subject": {"repo": REPO, "commit": A},
                                             "text": "Fix holds.", "evidence": [RCPT]})
        code, out, err = self.verdict_cli()
        self.assertEqual(code, 0, err)
        sent = self.fake.sent("record-verification")
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(sent[0]["idempotency_key"], intent["key"])
        self.assertIn(f"verdict #5: verified code_review {REPO}@aaaaaaa of #1 (correction #3)", out)
        self.assertEqual(consumer.unresolved_outboxes(self.cred, consumer.credential_connection(self.cred)), [])

    def test_correction_is_required_for_a_challenge_and_refused_on_a_claim(self):
        code, _, err = self.call("verdict", "1", "--verified", "--scope", "code_review", "--repo", REPO, "--commit",
                                A, "--text", "x", "--evidence", RCPT, "--credential-file", str(self.cred))
        self.assertEqual(code, 2)
        self.assertIn("name the correction you verified with --correction", err)
        code, _, err = self.call("verdict", "4", "--rejected", "--scope", "code_review", "--repo", REPO, "--commit",
                                A, "--text", "x", "--evidence", RCPT, "--credential-file", str(self.cred))
        self.assertEqual(code, 2)
        self.assertIn("is a challenge", err)
        self.assertEqual(self.fake.sent("record-verification"), [])
        self.assertFalse(list((self.dir / "outbox").glob("verdict-*.json")))

    def test_a_correction_above_the_review_watermark_is_refused(self):
        cursor = self.dir / "cursor.json"
        conn = consumer.credential_connection(self.cred)
        consumer.private_write(cursor, {"version": 1, "identity": consumer.identity(conn), "after": 2,
                                        "pending": None}, create=True)
        code, _, err = self.verdict_cli("--cursor-file", str(cursor))
        self.assertEqual(code, 2)
        self.assertIn("review the page containing #3 first", err)
        code, _, err = self.verdict_cli()   # cursor.json beside the credential is found too
        self.assertEqual(code, 2)
        self.assertEqual(self.fake.calls, [])

    def test_a_cursor_of_another_grant_is_refused(self):
        other = dict(consumer.identity(consumer.credential_connection(self.cred)), grant_id=NEW_GID)
        consumer.private_write(self.dir / "cursor.json", {"version": 1, "identity": other, "after": 9,
                                                          "pending": None}, create=True)
        code, _, err = self.verdict_cli()
        self.assertEqual(code, 2)
        self.assertIn("belongs to another grant", err)
        self.assertEqual(self.fake.calls, [])

    def test_scope_never_wider_than_its_subject(self):
        for argv, said in ((("--scope", "merge_readiness", "--repo", REPO, "--commit", A), "missing pr"),
                           (("--scope", "deployment", "--target", "k8s prod"), "missing observed_at"),
                           (("--scope", "code_review", "--repo", "acme/x", "--commit", A), "revision takes")):
            code, _, err = self.call("verdict", "1", "--verified", "--correction", "3", *argv, "--text", "x",
                                    "--evidence", RCPT, "--credential-file", str(self.cred))
            self.assertEqual(code, 2, argv)
            self.assertIn(said, err)
        code, _, err = self.call("verdict", "1", "--verified", "--correction", "3", "--scope", "code_review",
                                "--repo", REPO, "--commit", A, "--text", "x", "--credential-file", str(self.cred))
        self.assertIn("cites its deciding evidence", err)

    def test_an_identical_stored_verdict_is_success_and_names_its_grant(self):
        mine = {"verifies": 1, "correction": 3, "verdict": "verified", "scope": "code_review",
                "subject": {"repo": REPO, "commit": A}, "text": "Fix holds.", "evidence": [RCPT]}
        other = {"principal": {"kind": "grantee", "id": NEW_GID}, "session_id": None,
                 "is_investigation_author": False}
        stored = dict(ev(5, "verification.recorded", mine, other), investigation_id=INV)
        self.fake.events.append(stored)
        self.fake.principals = {f"grantee:{NEW_GID}": {"label": "codex supervisor"}}
        self.fake.answers["record-verification"] = sync.ServerError(409, {
            "error": "verification_exists", "seq": 5, "verification": stored, "lineage_root_id": NEW_GID,
            "same_lineage": False}, "x")
        code, out, err = self.verdict_cli()
        self.assertEqual(code, 0, err)
        self.assertIn(f'already recorded as verdict #5 by grantee:{NEW_GID} ("codex supervisor")', out)
        self.fake.events[-1]["payload"] = dict(mine, text="Other words.")
        code, _, err = self.verdict_cli("--ref", "#4")
        self.assertEqual(code, 1)
        self.assertIn("a changed judgment needs a new correction", err)

    def test_supersedes_revises_your_own_verdict_and_a_409_names_the_flag(self):
        mine = {"verifies": 1, "correction": 3, "verdict": "unverifiable", "scope": "code_review",
                "subject": {"repo": REPO, "commit": A}, "text": "No access.", "evidence": []}
        # Recorded by this grant's predecessor (another principal, the same lineage): only the
        # server knows, so its same_lineage decides.
        before = {"principal": {"kind": "grantee", "id": NEW_GID}, "session_id": None,
                  "is_investigation_author": False}
        stored = dict(ev(5, "verification.recorded", mine, before), investigation_id=INV)
        self.fake.events.append(stored)

        def exists(same):
            return sync.ServerError(409, {"error": "verification_exists", "seq": 5, "verification": stored,
                                          "lineage_root_id": NEW_GID, "same_lineage": same}, "x")

        self.fake.answers["record-verification"] = exists(True)
        code, _, err = self.verdict_cli()
        self.assertEqual(code, 1)
        self.assertIn("run the same command with --supersedes 5", err)
        stored["producer"] = dict(before, principal={"kind": "grantee", "id": GID})
        self.fake.answers["record-verification"] = exists(False)
        code, _, err = self.verdict_cli("--ref", "#4")
        self.assertIn("another lineage's verdict stays", err)
        self.fake.answers["record-verification"] = lambda body: {"event": dict(
            ev(6, "verification.recorded", body["payload"]), investigation_id=INV)}
        code, out, err = self.verdict_cli("--supersedes", "5")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.sent("record-verification")[-1]["payload"]["supersedes_verdict"], 5)

    def claim_verdict(self, seq, gid, supersedes=None):
        payload = {"verifies": 4, "verdict": "verified", "scope": "code_review",
                   "subject": {"repo": REPO, "commit": A}, "text": f"review {seq}", "evidence": [RCPT]}
        if supersedes is not None:
            payload["supersedes_verdict"] = supersedes
        producer = {"principal": {"kind": "grantee", "id": gid}, "is_investigation_author": False}
        self.fake.events.append(dict(ev(seq, "verification.recorded", payload, producer), investigation_id=INV))

    def test_the_revision_cap_counts_per_chain(self):
        # The worker's own lineage revised its claim verdict three times; that does not
        # count against this grant's chain on the same claim and scope.
        for seq, sup in ((20, None), (21, 20), (22, 21), (23, 22)):
            self.claim_verdict(seq, NEW_GID, sup)
        for seq, sup in ((30, None), (31, 30)):
            self.claim_verdict(seq, GID, sup)
        self.assertEqual(sv.chain([e for e in self.fake.events if e["type"] == "verification.recorded"], 22),
                         [20, 21, 22, 23])
        self.fake.answers["record-verification"] = lambda body: {"event": dict(
            ev(40, "verification.recorded", body["payload"]), investigation_id=INV)}
        argv = ("verdict", "4", "--verified", "--scope", "code_review", "--repo", REPO, "--commit", A, "--text",
                "Rechecked.", "--evidence", RCPT, "--credential-file", str(self.cred))
        code, _, err = self.call(*argv, "--supersedes", "31")
        self.assertEqual(code, 0, err)
        for seq, sup in ((32, 31), (33, 32)):
            self.claim_verdict(seq, GID, sup)
        code, _, err = self.call(*argv, "--supersedes", "33", "--ref", "#4")
        self.assertEqual(code, 2)
        self.assertIn("revised at most 3 times", err)
        self.assertEqual(len(self.fake.sent("record-verification")), 1)

    def test_a_retried_supersede_already_applied_is_success(self):
        self.claim_verdict(30, GID)
        argv = ("verdict", "4", "--verified", "--scope", "code_review", "--repo", REPO, "--commit", A, "--text",
                "review 31", "--evidence", RCPT, "--credential-file", str(self.cred), "--supersedes", "30")
        self.claim_verdict(31, GID, 30)   # what the earlier (lost) send recorded
        self.fake.answers["record-verification"] = sync.ServerError(409, {"error": "verdict_already_superseded"}, "x")
        code, out, err = self.call(*argv)
        self.assertEqual(code, 0, err)
        self.assertIn("already recorded as verdict #31", out)
        code, _, err = self.call(*argv[:-1], "30", "--ref", "#2")   # different content: a real refusal
        self.assertEqual(code, 1)
        self.assertIn("already revised", err)

    def test_an_older_server_says_verdicts_are_unsupported(self):
        self.fake.caps = ()
        code, _, err = self.verdict_cli()
        self.assertEqual(code, 1)
        self.assertIn("does not support verdicts yet", err)
        self.assertIn("Review: upheld", err)
        self.fake.caps = ("supervision_verifications",)
        self.fake.answers["get-investigation"] = [{"investigation_id": INV, "capabilities": {
            "supervision_verifications": {"enabled": True}}}, {"investigation_id": INV, "capabilities": {}}]
        self.fake.answers["record-verification"] = sync.ServerError(403, {"error": "token_scope_mismatch"}, "x")
        code, _, err = self.verdict_cli("--ref", "#2")
        self.assertEqual(code, 1)
        self.assertIn("does not support verdicts yet", err)

    def test_refusals_are_said_in_words(self):
        self.fake.answers["record-verification"] = sync.ServerError(
            409, {"error": "correction_not_current", "current_correction": 4}, "x")
        code, _, err = self.verdict_cli()
        self.assertEqual(code, 1)
        self.assertIn("the current correction is #4", err)


class VerdictRevision(GitRepo, SupervisorBase):
    def setUp(self):
        super().setUp()
        self.make_repo()
        self.fake.answers["record-verification"] = lambda body: {"event": dict(
            ev(6, "verification.recorded", body["payload"]), investigation_id=INV)}

    def auto(self, scope, *extra):
        return self.call("verdict", "1", "--verified", "--scope", scope, "--correction", "3", "--revision", "auto",
                         "--text", "Fix holds.", "--evidence", RCPT, "--credential-file", str(self.cred), *extra)

    def test_revision_auto_binds_the_supervisors_dirty_snapshot(self):
        (self.repo / "repro.txt").write_text("my reproduction\n")
        code, _, err = self.auto("code_review")
        self.assertEqual(code, 0, err)
        subject = self.fake.sent("record-verification")[0]["payload"]["subject"]
        self.assertEqual((subject["repo"], subject["dirty"], subject["pr"]), (REPO, True, 7))
        self.assertEqual(subject["tree"], ie.snapshot_tree(str(self.repo)))

    def test_merge_readiness_refuses_a_dirty_tree_before_sending(self):
        (self.repo / "repro.txt").write_text("my reproduction\n")
        code, _, err = self.auto("merge_readiness")
        self.assertEqual(code, 2)
        self.assertIn("merge_readiness needs a clean tree", err)
        self.assertEqual(self.fake.sent("record-verification"), [])
        (self.repo / "repro.txt").unlink()
        code, _, err = self.auto("merge_readiness", "--ref", "#3")
        self.assertEqual(code, 0, err)
        self.assertNotIn("dirty", self.fake.sent("record-verification")[0]["payload"]["subject"])


class ChallengesCli(SupervisorBase):
    def setUp(self):
        super().setUp()
        self.stream[2]["payload"]["revision"] = {"repo": REPO, "commit": A, "pr": 7}
        self.fake.events += [dict(e, investigation_id=INV) for e in (
            verdict(5, 1, 3, "verified", commit=B), challenge(6), ack(7, 6, "declined"))]
        self.fake.events[-3]["payload"]["subject"]["pr"] = 7

    def test_rows_with_next_actions_and_heads(self):
        bin_ = self.home / "bin"
        bin_.mkdir()
        rollup = json.dumps({"headRefOid": B, "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]})
        (bin_ / "gh").write_text(f"#!/bin/sh\necho '{rollup}'\n")
        (bin_ / "gh").chmod(0o755)
        os.environ["PATH"] = f"{bin_}:/usr/bin:/bin"
        code, out, err = self.call("challenges", "--credential-file", str(self.cred), "--check-heads", "--json")
        self.assertEqual(code, 0, err)
        got = json.loads(out)
        self.assertEqual(got["heads"], "checked")
        one, six = got["challenges"]
        # Verified at B while the correction claims A: a mismatch, so not done.
        self.assertEqual((one["status"], one["correction"]["seq"]), ("verified", 3))
        self.assertEqual(one["next"], {"actor": "supervisor", "action_kind": "reverify",
                                       "action": "re-verify #3 at its revision", "seq": 3})
        self.assertEqual(one["correction"]["head"], {"sha": B, "moved": True, "ci": "failing"})
        v = one["verdicts"][0]
        self.assertEqual((v["superseded"], v["revision_mismatch"], v["head"]["moved"]), (False, True, False))
        self.assertEqual((six["status"], six["next"]["actor"], six["next"]["action_kind"], six["next"]["seq"]),
                         ("declined", "supervisor", "rule", 6))
        code, out, _ = self.call("challenges", "--credential-file", str(self.cred))
        self.assertIn("#1 verified · position #2 accepted · correction #3", out)
        self.assertIn("verdict #5 verified code_review", out)
        self.assertIn("[revision mismatch]", out)

    def rows(self):
        code, out, err = self.call("challenges", "--credential-file", str(self.cred), "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)["challenges"]

    def test_dirty_snapshots_mismatch_or_are_unbound(self):
        rev, subject = self.stream[2]["payload"]["revision"], self.fake.events[-3]["payload"]["subject"]
        rev.update(commit=A, dirty=True, tree="1" * 40)
        subject.update(commit=A, dirty=True, tree="2" * 40)
        v = self.rows()[0]["verdicts"][0]
        self.assertEqual((v["revision_mismatch"], v["snapshot_unbound"], v["tree"]), (True, False, "2" * 40))
        subject["tree"] = "1" * 40
        self.assertFalse(self.rows()[0]["verdicts"][0]["revision_mismatch"])
        subject.pop("dirty")
        subject.pop("tree")   # a clean verdict on a dirty correction: their trees differ
        self.assertTrue(self.rows()[0]["verdicts"][0]["revision_mismatch"])
        rev.pop("tree")       # the correction is dirty without tree
        row = self.rows()[0]
        self.assertTrue(row["correction"]["snapshot_unbound"])
        self.assertTrue(row["verdicts"][0]["snapshot_unbound"])
        rev.update(tree="1" * 40)
        subject.update(dirty=True)   # the verdict's subject is dirty without tree
        self.assertTrue(self.rows()[0]["verdicts"][0]["snapshot_unbound"])
        code, out, _ = self.call("challenges", "--credential-file", str(self.cred))
        self.assertIn("[snapshot unbound]", out)

    def test_a_revised_verdict_is_marked(self):
        first = self.fake.events[-3]
        first["payload"]["verdict"] = "unverifiable"
        later = dict(verdict(8, 1, 3, "verified", commit=A), investigation_id=INV)
        later["payload"]["supersedes_verdict"] = first["seq"]
        self.fake.events.append(later)
        row = self.rows()[0]
        self.assertEqual((row["status"], row["verification_seq"]), ("verified", 8))
        self.assertEqual(row["verdicts"][0]["revised_by"], 8)

    def test_heads_are_checked_only_on_trusted_hosts(self):
        bin_ = self.home / "bin"
        bin_.mkdir()
        log = self.home / "gh.log"
        rollup = json.dumps({"headRefOid": A, "statusCheckRollup": []})
        (bin_ / "gh").write_text(f"#!/bin/sh\necho \"$*\" >> {log}\n"
                                 f"case \"$1\" in auth) exit 1;; esac\necho '{rollup}'\n")
        (bin_ / "gh").chmod(0o755)
        os.environ["PATH"] = f"{bin_}:/usr/bin:/bin"
        self.stream[2]["payload"]["revision"]["repo"] = "evil.example/acme/restore"
        # Even when it is this checkout's own origin host: only github.com and gh-authenticated hosts.
        own = self.home / "own"
        own.mkdir()
        for args in (["init", "-q"], ["remote", "add", "origin", "https://evil.example/acme/restore.git"]):
            subprocess.run(["git", *args], cwd=own, check=True, capture_output=True)
        cwd = os.getcwd()
        os.chdir(own)
        self.addCleanup(os.chdir, cwd)
        code, out, err = self.call("challenges", "--credential-file", str(self.cred), "--check-heads", "--json")
        self.assertEqual(code, 0, err)
        one = json.loads(out)["challenges"][0]
        self.assertEqual(one["correction"]["head"], {"unchecked": "host not trusted"})
        self.assertEqual(one["verdicts"][0]["head"]["sha"], A)   # github.com: checked
        calls = log.read_text()
        self.assertIn("auth status --hostname evil.example", calls)
        self.assertNotIn("--repo=evil.example", calls)
        self.assertIn(f"pr view 7 --repo={REPO} --json", calls)
        code, out, _ = self.call("challenges", "--credential-file", str(self.cred), "--check-heads")
        self.assertIn("[head unchecked (host not trusted)]", out)

    def next_of(self, *argv):
        code, out, err = self.call("challenges", "--credential-file", str(self.cred), *argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)["challenges"][0]["next"] if "--json" in argv else out.splitlines()[0]

    def test_next_action_matched(self):
        self.fake.events[-3]["payload"]["subject"]["commit"] = A   # verified at the correction's own commit
        self.assertEqual(self.next_of("--json"), {"actor": "nobody", "action_kind": "none", "action": "none",
                                                  "seq": None})
        self.assertTrue(self.next_of().endswith("next: nobody none"), self.next_of())

    def test_next_action_mismatched(self):
        self.assertEqual(self.next_of("--json")["action_kind"], "reverify")
        line = self.next_of()
        self.assertIn("[revision mismatch]", line)
        self.assertTrue(line.endswith("next: supervisor reverify (re-verify #3 at its revision)"), line)

    def test_next_action_unbound(self):
        self.fake.events[-3]["payload"]["subject"]["commit"] = A
        # Both sides dirty at the same commit, neither with a tree: no mismatch can be shown,
        # and no exact snapshot is bound either.
        self.stream[2]["payload"]["revision"]["dirty"] = True
        self.fake.events[-3]["payload"]["subject"]["dirty"] = True
        line = self.next_of()
        self.assertIn("[snapshot unbound]", line)
        self.assertNotIn("[revision mismatch]", line)
        self.assertTrue(line.endswith("next: supervisor reverify (re-verify #3 at its revision)"), line)

    def test_next_action_unverifiable(self):
        self.fake.events[-3]["payload"]["verdict"] = "unverifiable"
        nxt = self.next_of("--json")
        self.assertEqual((nxt["actor"], nxt["action_kind"], nxt["seq"]), ("owner", "decide_unverifiable", 3))
        self.assertTrue(self.next_of().endswith("next: owner decide_unverifiable (decide whether to accept #3 "
                                                "unverified)"))

    def test_without_gh_heads_are_unchecked(self):
        os.environ["PATH"] = "/usr/bin:/bin"
        code, out, err = self.call("challenges", "--credential-file", str(self.cred), "--check-heads")
        self.assertEqual(code, 0, err)
        self.assertIn("heads unchecked", err)
        self.assertNotIn("[head moved]", out)


class EventsRendering(Base):
    def test_compact_and_legacy_lines(self):
        e = dict(verdict(5, 1, 3, "verified"), investigation_id=INV)
        line = reader.compact_line(e, {})
        self.assertIn(f"verification.recorded · ", line)
        self.assertIn(f"verified code_review {REPO}@aaaaaaa of #1 (correction #3): Replay still flakes", line)
        c = cli.CLI(self.wiring, cli.build_parser(self.wiring).parse_args(["events"]), legacy=True)
        self.assertIn(f"verification.recorded from grantee:{SUPERVISOR['principal']['id']}", c._event_line(e))
        self.assertIn(f"verified code_review {REPO}@aaaaaaa of #1 (correction #3)", c._event_line(e))


class Succession(SupervisorBase):
    def setUp(self):
        super().setUp()
        self.new = self.dir / "grant-new.json"
        self.cursor = self.dir / "cursor.json"
        old = consumer.credential_connection(self.cred)
        consumer.private_write(self.cursor, {"version": 1, "identity": consumer.identity(old), "after": 4,
                                             "pending": None}, create=True)
        self.minted = {"grant_id": NEW_GID, "token": token(NEW_GID), "investigation_id": INV,
                       "scopes": ["read", "advise", "supervise"], "expires_at": "t", "principal": f"grantee:{NEW_GID}"}
        self.fake.answers["read-investigation-events"] = {
            "investigation_id": INV, "events": [], "head_seq": 4, "next_after": 4,
            "viewer": {"principal": {"kind": "grantee", "id": NEW_GID}}}
        self.fake.answers["list-investigation-grants"] = {"investigation_id": INV, "grants": [
            {"grant_id": GID, "label": "codex gpt supervisor", "scopes": ["read", "advise", "supervise"],
             "supervisor_investigation_id": "inv_" + "b" * 24, "supervisor_session_id": "sup-session"}]}

    def renew(self, *extra):
        return self.call("grant", "--succeeds-credential-file", str(self.cred), "--out", str(self.new), *extra)

    def test_succession_mints_renews_the_cursor_then_deletes_the_old_file(self):
        self.fake.answers["grant-investigation-access"] = self.minted
        code, out, err = self.renew()
        self.assertEqual(code, 0, err)
        body = self.fake.sent("grant-investigation-access")[0]
        self.assertEqual(body["succeeds"], {"grant_id": GID, "token": token(GID)})
        self.assertEqual(body["scopes"], ["read", "advise", "supervise"])
        # The old grant's label and supervisor link carry over.
        self.assertEqual((body["label"], body["supervisor_investigation_id"], body["supervisor_session_id"]),
                         ("codex gpt supervisor", "inv_" + "b" * 24, "sup-session"))
        self.assertEqual(consumer.credential_connection(self.new)["grant_id"], NEW_GID)
        self.assertEqual(consumer.private_read(self.cursor)["identity"]["grant_id"], NEW_GID)
        self.assertFalse(self.cred.exists())
        self.assertEqual(json.loads(out)["succeeded"], GID)
        self.assertIn("Restart your watch", err)
        self.assertIn("Explicit --outbox-file outboxes were not checked", err)

    def test_an_uncertain_answer_keeps_the_reservation_and_a_retry_completes(self):
        self.fake.answers["grant-investigation-access"] = [
            ist.FetchError("grant-investigation-access failed: reset", network=True, ambiguous=True),
            dict(self.minted, duplicate=True)]
        code, _, err = self.renew()
        self.assertEqual(code, 3)
        self.assertIn("stays reserved", err)
        self.assertTrue(self.cred.exists())
        self.assertEqual(consumer.private_read(self.new)["succession_of"], GID)
        code, _, err = self.renew("--label", "a different label")   # the reserved request is resent unchanged
        self.assertEqual(code, 0, err)
        self.assertIn("resending the succession reserved", err)
        sent = self.fake.sent("grant-investigation-access")
        self.assertEqual(sent[0], sent[1])
        self.assertFalse(self.cred.exists())

    def test_refusals(self):
        self.fake.answers["grant-investigation-access"] = sync.ServerError(409, {"error": "predecessor_expired"}, "x")
        code, _, err = self.renew()
        self.assertEqual(code, 1)
        self.assertIn("succession needs the live predecessor", err)
        self.assertFalse(self.new.exists())
        self.assertTrue(self.cred.exists())
        code, _, err = self.renew("--scope", "read")
        self.assertEqual(code, 2)
        self.assertIn("exactly its predecessor's scopes", err)

    def test_predecessor_succeeded_is_never_final(self):
        self.fake.answers["grant-investigation-access"] = [
            ist.FetchError("grant-investigation-access failed: reset", network=True, ambiguous=True),
            sync.ServerError(409, {"error": "predecessor_succeeded"}, "x"),
            sync.ServerError(409, {"error": "predecessor_expired"}, "x")]
        self.renew()
        for _ in range(2):
            code, _, err = self.renew()
            self.assertEqual(code, 1)
            self.assertIn("stays reserved", err)
            self.assertIn("investigation grants --investigation", err)
            self.assertEqual(consumer.private_read(self.new)["succession_of"], GID)
        self.assertTrue(self.cred.exists())
        # Even on a first attempt: the successor it names may be ours.
        self.new.unlink()
        self.fake.answers["grant-investigation-access"] = sync.ServerError(409, {"error": "predecessor_succeeded"},
                                                                           "x")
        code, _, err = self.renew()
        self.assertIn("stays reserved", err)
        self.assertTrue(self.new.exists())

    def test_an_unreadable_old_grant_mints_nothing(self):
        self.fake.answers["list-investigation-grants"] = {"investigation_id": INV, "grants": []}
        code, _, err = self.renew()
        self.assertEqual(code, 1)
        self.assertIn("is not a grant of", err)
        self.assertFalse(self.new.exists())
        self.assertEqual(self.fake.sent("grant-investigation-access"), [])

    def test_gated_on_the_capability_and_on_resolved_outboxes(self):
        self.fake.caps = ()
        code, _, err = self.renew()
        self.assertEqual(code, 1)
        self.assertIn("does not support grant succession yet", err)
        self.fake.caps = ("grant_succession",)
        old = consumer.credential_connection(self.cred)
        box = consumer.outbox_path(old, self.cred, "challenge.added", "x", SID, [])
        consumer.private_write(box, {"intent": {"identity": consumer.identity(old), "type": "challenge.added"}},
                               create=True)
        code, _, err = self.renew()
        self.assertEqual(code, 1)
        self.assertIn("resolve or retry these posts first", err)
        self.assertIn("Explicit --outbox-file outboxes are not checked", err)
        self.assertEqual(self.fake.sent("grant-investigation-access"), [])
        saved = consumer.private_read(box)
        saved["refused"] = {"status": 422}
        consumer.private_write(box, saved)
        self.assertEqual(consumer.unresolved_outboxes(self.cred, old), [])


if __name__ == "__main__":
    import unittest
    unittest.main()

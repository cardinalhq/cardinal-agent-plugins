"""Supervise step backs: the per-milestone rate trigger
(docs/specs/supervise-stepback-trigger.md). The engine (charging, ratio,
zero progress, floor, re-arm, backstop), the estimator plan, `reviewed`
tags and marks, the supervision-event writes, `budget` and its passes, and
the wingbeat and healthy-session replays (section 6 regression tests)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cardinal_core import evidence
from cardinal_core import investigation_agent as agent
from cardinal_core import investigation_budget as bud
from cardinal_core import investigation_consumer as consumer
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_state_sync as sync

INV = "inv_" + "b" * 24
SUP_INV = "inv_" + "5" * 24
WORKER = "worker-sid"
SUP = "sup-sid"
MEMBER = {"origin": "https://test.invalid", "org": "org", "key": "test-key"}
GRANT = {**MEMBER, "token": "cig_secret_grant_token", "grant_id": "grt_" + "d" * 24, "investigation_id": INV,
         "scopes": ["read", "supervise"]}
TESTDATA = Path(__file__).resolve().parent / "testdata"
T0 = bud.parse_time("2026-10-15T06:00:00Z")


def at(minutes: float) -> str:
    return bud.iso(T0 + minutes * 60)


def wev(seq, minutes, session=WORKER, cls="semantic", grantee=False, author=True, typ="finding.proposed"):
    principal = {"kind": "grantee", "id": "grt_" + "d" * 24} if grantee else {"kind": "user", "id": "owner"}
    return {"investigation_id": INV, "seq": seq, "type": typ, "class": cls,
            "producer": {"principal": principal, "session_id": session, "is_investigation_author": author},
            "created_at": at(minutes)}


def plan(*milestones, ref="two earlier phases of this repo, 3 h and 5 h", pace="0.3 h per result"):
    """milestones: (id, p50, p80, n deliverables)."""
    return {"reference_class": ref, "pace": pace, "unknowns": [{"what": "CI flakiness", "p50_h": 0.5, "p80_h": 2}],
            "milestones": [{"id": m, "title": f"title {m}", "p50_h": p50, "p80_h": p80, "basis": "like phase 2",
                            "deliverables": [{"done_when": f"a commit for {m} part {i} and its test passes"}
                                             for i in range(1, n + 1)]}
                           for m, p50, p80, n in milestones or (("a", 1, 2, 2), ("b", 2, 4, 4))]}


def direction(p=None, **th):
    pl, errs = bud._plan_errors(p or plan())
    assert not errs, errs
    return bud.new_direction(inv=INV, question="Q?", plan=pl, since_seq=1, set_at=T0, worker_session=WORKER,
                             th={**bud.DEFAULT_THRESHOLDS, **th})


def mk(did, mark, seq):
    return {"deliverable_id": did, "milestone_id": did.split(".")[0], "mark": mark, "seq": seq, "ref": f"#{seq}",
            "refs": []}


def step(d, seq, minutes, tag, marks=(), spool=()):
    """One worker event per pass."""
    return bud.apply_batch(d, [(seq, T0 + minutes * 60)], [T0 + m * 60 for m in spool], {seq: tag}, list(marks))


class Engine(unittest.TestCase):
    def test_charging_idle_and_split(self):
        d = direction()
        d, _ = step(d, 1, 0, ["a"])                 # the first event: nothing before it to charge
        d, r = step(d, 2, 12, ["a"])
        self.assertAlmostEqual(r["charged"]["a"], 0.2)
        d, r = step(d, 3, 24, ["a", "b"])           # split evenly
        self.assertAlmostEqual(r["charged"]["a"], 0.1)
        self.assertAlmostEqual(r["charged"]["b"], 0.1)
        d, r = step(d, 4, 60, ["b"])                # 36 min: idle, not charged
        self.assertEqual(r["charged"], {})
        d, r = step(d, 5, 66, "none")
        self.assertAlmostEqual(d["none_h"], 0.1)
        self.assertAlmostEqual(d["active_h"], 0.5)
        # A captured call 20 min in breaks a 40-minute gap into two active ones.
        d, r = step(d, 6, 106, ["b"], spool=(86,))
        self.assertAlmostEqual(r["charged"]["b"], 40 / 60)

    def test_ratio_fires_at_T_once_per_crossing_and_rearms_at_1_5x(self):
        d = direction(plan(("a", 1, 2, 2), ("b", 4, 8, 4)))
        d, _ = step(d, 1, 0, ["a"], [mk("a.1", "done", 1)])
        d, _ = step(d, 2, 29, ["a"])
        d, r = step(d, 3, 59, ["a"])                # 0.98 h of 1 h at 50%: ratio 1.97
        self.assertEqual(r["firings"], [])
        d, r = step(d, 4, 61, ["a"])                # 1.02 h: 2.03
        f = r["firings"][0]
        self.assertEqual((f["scope"], f["milestone_id"], f["snapshot"]["threshold"]), ("milestone", "a", 2.0))
        self.assertAlmostEqual(f["snapshot"]["ratio"], 2.0333, places=3)
        d, r = step(d, 5, 90, ["a"])                # 1.5 h, ratio 3.0 < 1.5 x 2.03
        self.assertEqual(r["firings"], [])
        d, r = step(d, 6, 92, ["a"])                # 1.53 h, ratio 3.07 >= 3.05: re-armed
        self.assertEqual(r["firings"][0]["snapshot"]["threshold"], 3.05)

    def test_zero_progress_floor_and_reopen(self):
        d = direction(plan(("a", 1, 2, 2), ("b", 4, 8, 4)), min_spent_h=0.5)
        d, _ = step(d, 1, 0, ["b"])
        d, r = step(d, 2, 29, ["b"])                # 0.48 h: below the floor
        self.assertEqual(r["firings"], [])
        d, r = step(d, 3, 59, ["b"])                # 0.98 h < Z x 4 h
        self.assertEqual(r["firings"], [])
        d, r = step(d, 4, 89, ["b"], [mk("b.1", "done", 4)])   # 1.48 h at 25%: ratio 1.48
        d, r = step(d, 5, 119, ["b"])               # 1.98 h: ratio 1.98
        self.assertEqual(r["firings"], [])
        d, r = step(d, 6, 129, ["b"])               # 2.15 h: 2.15, past the floor
        self.assertEqual(len(r["firings"]), 1)
        d = direction(plan(("a", 1, 2, 2), ("b", 2, 4, 4)))
        d, _ = step(d, 1, 0, ["b"])
        d, r = step(d, 2, 29, ["b"])
        self.assertEqual(r["firings"], [])
        d, r = step(d, 3, 59, ["b"], [mk("b.1", "done", 3)])   # 0.98 h, 25%: ratio 1.97
        self.assertEqual(r["firings"], [])
        d, r = step(d, 4, 60, ["b"], [mk("b.1", "reopened", 4)])   # back to 0%: infinite ratio, spent >= Z x 2
        self.assertEqual((r["firings"][0]["snapshot"]["ratio"], r["firings"][0]["snapshot"]["delivered"]), (None, 0))
        d, r = step(d, 5, 80, ["b"])                # 0% after a 0% firing: needs 1.5 x the spent then
        self.assertEqual(r["firings"], [])
        d, r = step(d, 6, 90, ["b"])                # 1.5 h = 1.5 x 1.0
        self.assertEqual(len(r["firings"]), 1)
        # After a 0% firing, progress makes the ratio finite: never >= 1.5 x infinity.
        d, r = step(d, 7, 110, ["b"], [mk("b.1", "redone", 7)])
        d, r = step(d, 8, 130, ["b"])
        self.assertEqual(r["firings"], [])
        self.assertGreater(r["firings"] or d["charged"]["b"] / 2 / 0.25, 3)

    def test_done_milestones_are_not_active_and_backstop(self):
        d = direction(plan(("a", 0.1, 0.2, 2), ("b", 4, 8, 4)))
        d, _ = step(d, 1, 0, ["a"], [mk("a.1", "done", 1), mk("a.2", "done", 1)])
        d, r = step(d, 2, 20, ["a"])
        self.assertEqual((r["firings"], r["active"]), ([], []))
        # P80 total 8.2 h: the backstop at 16.4 worker-active hours fires once, direction-wide.
        minutes = 20
        fired = []
        for seq in range(3, 70):
            minutes += 25
            d, r = step(d, seq, minutes, "none")
            fired += r["firings"]
        self.assertEqual([f["scope"] for f in fired], ["backstop"])
        self.assertEqual(fired[0]["snapshot"]["threshold"], 2.0)
        self.assertGreaterEqual(fired[0]["snapshot"]["spent_h"], 16.4)

    def test_one_firing_per_milestone_per_pass(self):
        d = direction(plan(("a", 1, 2, 2), ("b", 4, 8, 4)))
        wt = [(i + 1, T0 + i * 1500) for i in range(7)]          # 25 min apart: 2.5 h
        d2, r = bud.apply_batch(d, wt, [], {s: ["a"] for s, _ in wt}, [mk("a.1", "done", 1)])
        self.assertEqual([f["at_seq"] for f in r["firings"]], [4])    # 1.25 h: 2.5x; at 2.5 h (5x) still one
        self.assertEqual(d["firings"], [], "the input direction is not changed")


def fixture(name):
    return json.loads((TESTDATA / f"stepback-{name}.json").read_text(encoding="utf-8"))


def replay(fx, scale=1.0, batch=1, **th):
    p = json.loads(json.dumps(fx["plan"]))
    for m in p["milestones"]:
        m["p50_h"] *= scale
        m["p80_h"] *= scale
    pl, errs = bud._plan_errors(p)
    assert not errs, errs
    for m, raw in zip(pl["milestones"], p["milestones"]):
        m["p50_h"], m["p80_h"] = raw["p50_h"], raw["p80_h"]     # unrounded, as the reference replay
    times = [(s, bud.parse_time(t)) for s, t in fx["worker_times"]]
    d = bud.new_direction(inv=INV, question="Q", plan=pl, since_seq=times[0][0], set_at=times[0][1],
                          worker_session=WORKER, th={**bud.DEFAULT_THRESHOLDS, **th})
    fired, prev = [], 0
    for i in range(0, len(times), batch):
        chunk = times[i:i + batch]
        last = chunk[-1][0]
        marks = [mk(did, mark, s) for did, mark, s in fx["marks"] if prev < s <= last]
        tags = {s: fx["tags"][str(s)] for s, _ in chunk}
        d, r = bud.apply_batch(d, chunk, [], tags, marks)
        fired += [(f["at_seq"], f["milestone_id"], f["snapshot"]["ratio"] and round(f["snapshot"]["ratio"], 2))
                  for f in r["firings"]]
        prev = last
    return fired, d


class WingbeatReplay(unittest.TestCase):
    """inv_cab0d4ba690509cd646915c2 phase F #199-#336 (spec section 2): the
    blind plan, the marks with redone, the per-event tags. Agrees with the
    reference replay (scratchpad healthy/replay.py)."""

    def test_2x_first_fire_and_rearm_sequence(self):
        self.assertEqual(bud.DEFAULT_THRESHOLDS["min_spent_h"], 0.5)        # the owner's floor
        fired, d = replay(fixture("wingbeat"), T=2.0)
        self.assertEqual(fired[0], (270, "chunked-lane", 2.33))
        self.assertEqual([s for s, _, _ in fired], [270, 306, 323, 330])
        self.assertEqual([m for _, m, _ in fired], ["chunked-lane", "aws-native-qualification",
                                                     "aws-native-qualification", "chunked-lane"])
        self.assertEqual(fired[-1][2], None)        # #330: the REWORK reopened its only done item
        self.assertAlmostEqual(d["active_h"], 6.14, places=2)
        self.assertAlmostEqual(d["none_h"], 0.44, places=2)

    def test_other_thresholds(self):
        fx = fixture("wingbeat")
        self.assertEqual([s for s, _, _ in replay(fx, T=1.5)[0]], [255, 276, 280, 299, 320, 330])
        self.assertEqual([s for s, _, _ in replay(fx, T=3.0)[0]], [323, 330])
        self.assertEqual([s for s, _, _ in replay(fx, T=2.0, min_spent_h=0)[0]], [270, 306, 323, 330])

    def test_review_passes_of_realistic_size_agree(self):
        for batch in (5, 10, 20):
            self.assertEqual([s for s, _, _ in replay(fixture("wingbeat"), batch=batch)[0]], [270, 306, 323, 330])


def tag_flags(fx, wseqs):
    """--tag flags for a page: runs of consecutive worker events with the same tags."""
    flags, run = [], []
    for s in wseqs + [None]:
        if run and (s is None or fx["tags"][str(s)] != fx["tags"][str(run[0])]):
            v = fx["tags"][str(run[0])]
            span = f"{run[0]}-{run[-1]}" if len(run) > 1 else f"{run[0]}"
            flags += ["--tag", f"{span}:" + ("none" if v == "none" else "+".join(v))]
            run = []
        if s is not None:
            run.append(s)
    return flags


class HealthyReplay(unittest.TestCase):
    """inv_125102bcc5c974f4fffd5150 #1-#140 (spec section 2a)."""

    def test_no_firing_as_planned(self):
        for T in (1.5, 2.0, 3.0):
            self.assertEqual(replay(fixture("healthy"), T=T)[0], [], T)
            self.assertEqual(replay(fixture("healthy"), T=T, min_spent_h=0)[0], [], T)

    def test_at_015_scale(self):
        fx = fixture("healthy")
        fired, d = replay(fx, scale=0.15, T=2.0)                    # the default floor, 0.5 h
        self.assertEqual(fired, [])
        self.assertAlmostEqual(d["active_h"], 5.34, places=2)
        fired = replay(fx, scale=0.15, T=2.0, min_spent_h=0)[0]
        self.assertEqual([(s, m) for s, m, _ in fired], [(69, "p0-7-leaf-freq"), (101, "p0-8-canonical-keys")])

    def test_at_01_scale(self):
        fx = fixture("healthy")
        self.assertEqual([s for s, _, _ in replay(fx, scale=0.1, min_spent_h=0)[0]], [19, 69, 97, 99])
        self.assertEqual([s for s, _, _ in replay(fx, scale=0.1)[0]], [77, 99, 115])

    def test_fixtures_hold_no_event_text(self):
        for name in ("wingbeat", "healthy"):
            fx = fixture(name)
            self.assertEqual(set(fx), {"source", "worker_times", "plan", "marks", "tags"})
            self.assertTrue(all(len(x) == 2 for x in fx["worker_times"]))


class Plan(unittest.TestCase):
    def errors(self, p):
        return bud._plan_errors(p)[1]

    def test_a_good_plan(self):
        p, errs = bud._plan_errors(plan())
        self.assertEqual(errs, [])
        self.assertEqual([x["id"] for x in p["milestones"][1]["deliverables"]], ["b.1", "b.2", "b.3", "b.4"])
        for name in ("wingbeat", "healthy"):
            self.assertEqual(self.errors(fixture(name)["plan"]), [], name)

    def test_each_error_names_the_fix(self):
        cases = [
            (dict(plan(), reference_class=""), "reference_class is required"),
            (plan(("A", 1, 2, 2)), "1-24 of a-z"),
            (plan(("a" * 25, 1, 2, 2)), "1-24 of a-z"),
            (plan(("a", 2, 1, 2)), "P80 >= P50"),
            (plan(("a", 0, 1, 2)), "above 0"),
            (plan(("a", 1, 2, 1)), "2-6 deliverables"),
            (plan(("a", 1, 2, 7)), "2-6 deliverables"),
            (plan(("a", 1, 2, 2), ("a", 1, 2, 2)), "repeats"),
            ({**plan(), "extra": 1}, "unknown key 'extra'"),
            (plan(("a", 1, 1.2, 2), ref="none comparable"), "widen P80"),
            ({**plan(), "unknowns": [{"what": "x"}]}, "unknowns[0].p50_h"),
            ({**plan(), "milestones": []}, "list of 1 to 20"),
        ]
        for p, said in cases:
            errs = self.errors(p)
            self.assertTrue(errs and said in errs[0], (said, errs))
        p = plan()
        p["milestones"][0]["deliverables"][0]["done_when"] = "it works"
        self.assertIn("is not observable: name the commit, test result or finding", self.errors(p)[0])

    def test_load_plan_is_two_lines_at_most(self):
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "p.json"
            bad = plan(("A", 1, 2, 2), ("b", 2, 1, 2), ("c", 1, 2, 1))
            f.write_text(json.dumps(bad))
            with self.assertRaises(bud.BudgetError) as cm:
                bud.load_plan(f)
            lines = str(cm.exception).splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("(+1 more)", lines[1])
            f.write_text("{nope")
            with self.assertRaises(bud.BudgetError) as cm:
                bud.load_plan(f)
            self.assertIn("save only the estimator's JSON", str(cm.exception))


class TagsAndMarks(unittest.TestCase):
    def setUp(self):
        self.d = direction()

    def test_tags(self):
        w = [3, 5, 6, 9]
        self.assertEqual(bud.parse_tags(["3-6:a", "9:a+b"], w, self.d), {3: ["a"], 5: ["a"], 6: ["a"], 9: ["a", "b"]})
        self.assertEqual(bud.parse_tags(["5:none", "all:b"], w, self.d), {3: ["b"], 5: "none", 6: ["b"], 9: ["b"]})
        for specs, said in ((["3:a"], "#5, #6, #9 untagged: add `--tag 5-9:<milestone|none>`; open: a, b"),
                            (["3-6:zz"], "no milestone 'zz'; open: a, b"),
                            (["3-6:a", "6:b"], "#6 is tagged twice"),
                            (["20-30:a"], "no worker event of this page there (worker events: 3-9)"),
                            (["3:A"], "<seq>[-<seq>]:<milestone>"),
                            (["all:a", "all:b"], "one --tag all:")):
            with self.assertRaises(bud.BudgetError) as cm:
                bud.parse_tags(specs, w, self.d)
            self.assertIn(said, str(cm.exception), specs)
        with self.assertRaises(bud.BudgetError) as cm:
            bud.parse_tags(["5:a"], w, self.d)
        self.assertIn("`--tag 3:<milestone|none> --tag 6-9:<milestone|none>`", str(cm.exception))

    def marks(self, **kw):
        args = SimpleNamespace(done=kw.get("done", []), reopen=kw.get("reopen", []), redone=kw.get("redone", []))
        return bud.parse_marks(args, self.d, [3, 5, 6, 9], 3, 9, INV)

    def test_marks(self):
        got = self.marks(done=["a.1@#5", "b.2@ev_" + "a" * 12], reopen=["a.1@#6"], redone=["a.1@#9"])
        self.assertEqual([(m["deliverable_id"], m["mark"], m["seq"]) for m in got],
                         [("a.1", "done", 5), ("a.1", "reopened", 6), ("b.2", "done", 9), ("a.1", "redone", 9)])
        self.assertEqual(got[0]["refs"], [{"investigation_id": INV, "seq": 5}])
        self.assertEqual(got[2]["refs"], [{"evidence_id": "ev_" + "a" * 12}])
        for kw, said in ((dict(done=["a.1@#4"]), "#4 is not a worker event"),
                         (dict(done=["a.1@#10"]), "#10 is past this page (through #9)"),
                         (dict(done=["a.3@#5"]), "no deliverable a.3 (a has a.1-a.2)"),
                         (dict(done=["a.1"]), "<milestone>.<n>@#<seq>"),
                         (dict(reopen=["a.1@#5"]), "a.1 is open: --done it first"),
                         (dict(redone=["a.1@#5"]), "a.1 is open: --done it"),
                         (dict(done=["a.1@#5", "a.1@#6"]), "a.1 is done: drop this mark"),
                         (dict(done=["a.1@#5"], reopen=["a.1@#6"], redone=["a.1@#9", "a.1@#9"]),
                          "a.1 is done")):
            with self.assertRaises(bud.BudgetError) as cm:
                self.marks(**kw)
            self.assertIn(said, str(cm.exception), kw)
        self.d["marks"]["a.1"] = [{"mark": "done", "seq": 8, "ref": "#8"}]
        with self.assertRaises(bud.BudgetError) as cm:
            self.marks(reopen=["a.1@#6"])
        self.assertIn("latest mark is at #8, after #6", str(cm.exception))


class FakeServer:
    """maestro's record-supervision as built (spec 4a): author key, sup: keys,
    subject_grant for refs into the worker's investigation, strict payload
    rules, idempotency, refusals."""

    def __init__(self):
        self.events, self.keys, self.fail, self.bodies = [], {}, None, []

    def bad(self, path):
        return sync.ServerError(400, {"error": "invalid_payload", "path": path}, "invalid")

    def check(self, body):
        if not body["idempotency_key"].startswith("sup:") or not 1 <= len(body["events"]) <= 50:
            raise self.bad("idempotency_key")
        dirs = {e["payload"]["direction_id"] for e in self.events if e["type"] == "direction.set"}
        cross = False
        for i, e in enumerate(body["events"]):
            t, p = e["type"], e["payload"]
            if t == "direction.set":
                dirs.add(p["direction_id"])
                cross = cross or p["subject"]["investigation_id"] != body["investigation_id"]
            if t == "work.attributed" and p["direction_id"] not in dirs:
                raise self.bad(f"events[{i}].payload.direction_id")
            if t == "stepback.fired":
                if ("milestone_id" in p) != (p["scope"] == "milestone"):
                    raise self.bad(f"events[{i}].payload.milestone_id")
                snap = p["snapshot"]
                if not 0 <= snap["delivered"] <= 1 or not (snap["ratio"] is None or snap["ratio"] >= 0):
                    raise self.bad(f"events[{i}].payload.snapshot")
            if t == "deliverable.marked" and not p["refs"]:
                raise self.bad(f"events[{i}].payload.refs")
            refs = p.get("refs", []) + ([p["post_ref"]] if "post_ref" in p else [])
            cross = cross or any(r.get("investigation_id") not in (None, body["investigation_id"]) for r in refs)
        if cross and body.get("subject_grant") != GRANT["token"]:
            raise sync.ServerError(400, {"error": "ref_not_readable", "path": "events"}, "not readable")

    def __call__(self, conn, tool, body, *, client, **_):
        assert tool == "record-supervision", tool
        assert conn.get("key") and not conn.get("token"), "written with this machine's own key"
        self.bodies.append(body)
        if self.fail:
            err, self.fail = self.fail, None
            raise err
        self.check(body)
        k = body["idempotency_key"]
        material = json.dumps(body["events"], sort_keys=True)
        if k in self.keys:
            if self.keys[k][0] != material:
                raise sync.ServerError(409, {"error": "idempotency_conflict"}, "conflict")
            return {"investigation_id": body["investigation_id"], "duplicate": True, "events": self.keys[k][1],
                    "head_seq": len(self.events)}
        stored = []
        for e in body["events"]:
            stored.append({"seq": len(self.events) + 1, "type": e["type"], "at": "x"})
            self.events.append(e)
        self.keys[k] = (material, stored)
        return {"investigation_id": body["investigation_id"], "duplicate": False, "events": stored,
                "head_seq": len(self.events)}

    def types(self):
        return [e["type"] for e in self.events]


class CLIBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        isolated = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
        isolated["HOME"] = str(self.home)
        p = patch.dict(os.environ, isolated, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.dir = self.home / ".cardinal" / "supervise" / INV
        self.dir.mkdir(mode=0o700, parents=True)
        self.file = self.dir / "budget.json"
        self.cursor = self.dir / "cursor.json"
        self.cred = self.dir / "grant.json"
        self.events, self.reads, self.page = [], 0, 100
        self.wiring = SimpleNamespace(paths=SimpleNamespace(home=self.home / ".codex"), environ={},
                                      client="codex/test", cli=str(self.home / "scripts/cli"),
                                      connection=Mock(return_value=MEMBER), _off=lambda name: False)
        ie.write_binding(self.home, SUP, {"investigation_id": SUP_INV, "cursor": 0, "is_author": True})
        for target, name, fn in ((ie, "read_events", self.read), (consumer, "credential_connection", lambda p: GRANT)):
            x = patch.object(target, name, side_effect=fn)
            x.start()
            self.addCleanup(x.stop)
        self.server = FakeServer()
        x = patch.object(sync, "_post", side_effect=self.server)
        x.start()
        self.addCleanup(x.stop)

    def read(self, conn, inv, *, after=0, limit=200, client, **_):
        assert inv == INV
        self.reads += 1
        shown = [e for e in self.events if e["seq"] > after][:min(limit, self.page)]
        head = max([e["seq"] for e in self.events], default=0)
        return {"investigation_id": inv, "events": shown, "last_seq": shown[-1]["seq"] if shown else head,
                "page_size": len(shown), "head_seq": head}

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agent.cli_main(argv, self.wiring)
        return code, out.getvalue(), err.getvalue()

    def budget(self, *argv):
        return self.run_cli(["budget", INV, "--credential-file", str(self.cred), "--budget-file", str(self.file),
                             *argv])

    def write_plan(self, p=None, name="plan.json"):
        f = self.dir / name
        f.write_text(json.dumps(p or plan()))
        return f

    def set(self, *extra, p=None):
        return self.budget("--set", "--plan", str(self.write_plan(p)), "--question", "Make tallies shard-safe",
                           "--worker-session", WORKER, "--session", SUP, *extra)

    def page_read(self):
        return consumer.read(GRANT, self.cursor, client="t")

    def reviewed(self, *flags, batch=None):
        batch = batch or self.page_read()["batch"]
        return self.run_cli(["reviewed", "--investigation", INV, "--credential-file", str(self.cred),
                             "--cursor-file", str(self.cursor), "--batch", batch, *flags])

    def state(self):
        return consumer.private_read(self.file)


class BudgetSet(CLIBase):
    def test_set_records_the_direction_on_the_supervisors_own_investigation(self):
        self.events = [wev(1, -60), wev(2, -50)]
        code, out, err = self.set("--since-seq", "1")
        self.assertEqual(code, 0, err)
        self.assertRegex(out.strip(), r"^dir_[0-9a-f]{16} set: 2 milestones, P50 3\.0 h / P80 6\.0 h, counting from "
                                      r"#1; calibrated$")
        self.assertEqual(self.server.types(), ["direction.set", "milestone.planned", "milestone.planned",
                                               "stepback.fired"])
        ds = self.server.events[0]["payload"]
        self.assertEqual(ds["subject"], {"investigation_id": INV, "since_seq": 1})
        self.assertEqual((ds["calibration"], ds["worker_session_id"], ds["thresholds"]),
                         ("calibrated", WORKER, bud.DEFAULT_THRESHOLDS))
        self.assertEqual(self.server.events[1]["payload"]["deliverables"],
                         [{"id": "a.1", "done_when": "a commit for a part 1 and its test passes"},
                          {"id": "a.2", "done_when": "a commit for a part 2 and its test passes"}])
        self.assertEqual(self.server.events[3]["payload"]["scope"], "opening")
        st = self.state()
        self.assertEqual(self.server.bodies[0]["subject_grant"], GRANT["token"])
        self.assertTrue(self.server.bodies[0]["idempotency_key"].startswith("sup:set:"))
        self.assertNotIn(GRANT["token"], self.file.read_text() + out)
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        self.assertEqual(st["supervisor"], {"investigation_id": SUP_INV, "session_id": SUP})
        self.assertEqual(st["current"]["firings"][0]["fired_seq"], 4)
        # A retry of the same --set is the same direction: refused locally, never a second one.
        code, _, err = self.set("--since-seq", "1")
        self.assertEqual(code, 2)
        self.assertIn("already the open direction; a correction is --set --amend", err)

    def test_uncalibrated_default_since_and_replace(self):
        self.events = [wev(1, 0), wev(2, 20)]
        code, out, err = self.set(p=plan(pace="none"))
        self.assertEqual(code, 0, err)
        self.assertIn("counting from #2; uncalibrated (no measured pace)", out)
        self.assertEqual(self.server.events[0]["payload"]["calibration"], "uncalibrated")
        n = len(self.server.events)
        code, out, err = self.set("--since-seq", "1", "--threshold", "T=2.5", p=plan(("c", 1, 2, 2)))
        self.assertEqual(code, 0, err)
        self.assertIn("as replaced", out)
        self.assertEqual(self.server.types()[n:n + 2], ["direction.closed", "direction.set"])
        self.assertEqual(self.server.events[n]["payload"]["outcome"], "replaced")
        self.assertEqual(self.server.events[n + 1]["payload"]["thresholds"]["T"], 2.5)
        self.assertEqual(self.state()["history"][-1]["outcome"], "replaced")

    def test_errors_name_the_fix(self):
        self.events = [wev(1, 0)]
        bad = plan(("a", 1, 2, 1))
        code, _, err = self.set(p=bad)
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "plan: milestone a has 2-6 deliverables, not 1: split or merge them")
        self.assertEqual(self.server.events, [])
        for argv, said in ((["--set", "--question", "q", "--worker-session", WORKER], "--set needs --plan"),
                           (["--set", "--plan", "x.json", "--worker-session", WORKER], "--set needs --question"),
                           (["--set", "--amend", "--plan", "x", "--question", "q"], "--question does not go with --amend"),
                           (["--show", "--plan", "x"], "--plan does not go with --show"),
                           (["--close"], "--close needs --outcome"),
                           (["--set", "--plan", str(self.write_plan()), "--question", "q", "--worker-session",
                             WORKER, "--threshold", "T=9"], "--threshold T is 1.5 to 3.0")):
            code, _, err = self.budget(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(said, err, argv)
            self.assertLessEqual(len(err.strip().splitlines()), 2)
        code, _, err = self.budget("--set", "--plan", str(self.write_plan()), "--question", "q", "--worker-session",
                                   WORKER)
        self.assertEqual(code, 2)
        self.assertIn("needs --session <your_sid>", err)
        ie.write_binding(self.home, "joined", {"investigation_id": SUP_INV, "cursor": 0, "is_author": False})
        code, _, err = self.set("--session", "joined")
        self.assertIn("joined someone else's investigation", err)

    def test_an_old_server_keeps_the_cache_and_says_so_once(self):
        self.events = [wev(1, 0)]
        self.server.fail = sync.ServerError(400, {"error": "supervision_unsupported"}, "old")
        code, out, err = self.set()
        self.assertEqual(code, 0, err)
        self.assertIn(bud.SERVER_TOO_OLD, out)
        st = self.state()
        self.assertTrue(st["unsupported_noted"])
        self.assertIsNone(st["current"]["firings"][0]["fired_seq"])

    def test_refusals_change_nothing(self):
        self.events = [wev(1, 0)]
        self.server.fail = sync.ServerError(400, {"error": "invalid_payload", "path": "events[0].payload.question"},
                                            "bad")
        code, _, err = self.set()
        self.assertEqual(code, 1)
        self.assertIn("not recorded: Cardinal refused a supervision record as invalid", err)
        self.assertFalse(self.file.exists())

    def test_amend_replans_and_rearms(self):
        self.events = [wev(1, 0)]
        self.set("--since-seq", "1")
        st = self.state()
        st["current"]["last_fire"]["a"] = [2.1, 1.0, 0.5]
        consumer.private_write(self.file, st)
        n = len(self.server.events)
        code, out, err = self.budget("--set", "--amend", "--plan",
                                     str(self.write_plan(plan(("a", 3, 5, 2), ("b", 2, 4, 4), ("c", 1, 2, 2)), "p2")))
        self.assertEqual(code, 0, err)
        self.assertIn("re-planned: 2 milestone(s) new or changed; P50 6.0 h / P80 11.0 h", out)
        evs = self.server.events[n:]
        self.assertEqual([(e["payload"]["milestone_id"], e["payload"].get("replaces")) for e in evs],
                         [("a", "a"), ("c", None)])
        self.assertEqual(self.state()["current"]["last_fire"], {})


class Review(CLIBase):
    def setUp(self):
        super().setUp()
        self.events = [wev(1, 0)]
        code, _, err = self.set("--since-seq", "1", p=plan(("a", 1, 2, 2), ("b", 2, 4, 4)))
        assert code == 0, err
        self.n = len(self.server.events)

    def test_untagged_refuses_and_leaves_the_cursor(self):
        self.events += [wev(2, 10), wev(3, 15, grantee=True), wev(4, 20)]
        code, _, err = self.reviewed("--tag", "1-2:a")
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "#4 untagged: add `--tag 4:<milestone|none>`; open: a, b")
        self.assertIsNotNone(json.loads(self.cursor.read_text())["pending"])
        self.assertEqual(len(self.server.events), self.n)

    def test_a_pass_with_no_firing(self):
        self.events += [wev(2, 10), wev(3, 15, grantee=True), wev(4, 20), wev(5, 30)]
        code, out, err = self.reviewed("--tag", "1-2:a", "--tag", "4:none", "--tag", "5:a+b", "--done", "a.1@#4")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip().splitlines(), ["a 0.25/1.0 h · 1/2 done · 0.5×", "b 0.08/2.0 h · 0/4 done",
                                                    "reviewed through #5"])
        evs = self.server.events[self.n:]
        self.assertEqual([e["type"] for e in evs], ["work.attributed", "deliverable.marked"])
        wa = evs[0]["payload"]
        self.assertEqual((wa["through_seq"], wa["tags"]), (5, {"1": ["a"], "2": ["a"], "4": "none", "5": ["a", "b"]}))
        self.assertEqual(wa["charged_h"], {"a": 0.25, "b": 0.0833})
        self.assertEqual(evs[1]["payload"]["refs"], [{"investigation_id": INV, "seq": 4}])
        self.assertIsNone(json.loads(self.cursor.read_text())["pending"])
        self.assertEqual(json.loads(self.cursor.read_text())["after"], 5)

    def test_a_fire(self):
        self.events += [wev(2, 25), wev(3, 50), wev(4, 75)]
        code, out, err = self.reviewed("--tag", "all:a", "--done", "a.1@#2")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip().splitlines(), ["a 1.25/1.0 h · 1/2 done · 2.5× · FIRE step back",
                                                    "reviewed through #4"])
        f = self.server.events[-1]
        self.assertEqual((f["type"], f["payload"]["scope"], f["payload"]["milestone_id"]),
                         ("stepback.fired", "milestone", "a"))
        self.assertEqual(f["payload"]["snapshot"], {"spent_h": 1.25, "estimate_h": 1, "delivered": 0.5, "ratio": 2.5,
                                                    "threshold": 2.0})
        self.assertEqual(self.state()["current"]["firings"][-1]["fired_seq"], len(self.server.events))

    def test_a_failed_write_leaves_the_cursor_and_the_retry_is_idempotent(self):
        self.events += [wev(2, 10)]
        self.server.fail = ie.ist.FetchError("Cardinal unreachable") if hasattr(ie, "ist") else None
        from cardinal_core import investigation_state as ist
        self.server.fail = ist.FetchError("unreachable")
        code, _, err = self.reviewed("--tag", "all:a")
        self.assertEqual(code, 1)
        self.assertIn("unreachable", err)
        self.assertIsNotNone(json.loads(self.cursor.read_text())["pending"])
        self.assertEqual(self.state()["current"]["active_h"], 0)
        code, _, err = self.reviewed("--tag", "all:b")
        self.assertEqual(code, 2)
        self.assertIn("already sent with other flags (the commit failed after): repeat them: --tag all:a", err)
        code, out, err = self.reviewed("--tag", "all:a")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip().splitlines()[-1], "reviewed through #2")

    def test_a_crash_after_the_write_replays_without_recharging(self):
        self.events += [wev(2, 10)]
        batch = self.page_read()["batch"]
        with patch.object(consumer, "reviewed", side_effect=OSError("disk full")):
            self.assertEqual(self.reviewed("--tag", "all:a", batch=batch)[0], 1)
        n = len(self.server.events)
        code, out, _ = self.reviewed("--tag", "all:a", batch=batch)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.server.events), n, "applied once")
        self.assertAlmostEqual(self.state()["current"]["charged"]["a"], 1 / 6)
        self.assertIn("reviewed through #2", out)

    def test_old_server_commits_and_says_once(self):
        self.events += [wev(2, 10)]
        self.server.fail = sync.ServerError(404, {"error": "not_found"}, "no route")
        code, out, err = self.reviewed("--tag", "all:a")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], bud.SERVER_TOO_OLD)
        self.events += [wev(3, 20)]
        self.server.fail = sync.ServerError(400, {"error": "supervision_unsupported"}, "old")
        code, out, err = self.reviewed("--tag", "all:a")
        self.assertNotIn(bud.SERVER_TOO_OLD, out)
        self.assertAlmostEqual(self.state()["current"]["charged"]["a"], 1 / 3)

    def test_the_event_limit_keeps_the_cache_and_commits(self):
        self.events += [wev(2, 10)]
        self.server.fail = sync.ServerError(409, {"error": "event_limit_reached"}, "limit")
        code, out, err = self.reviewed("--tag", "all:a")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], bud.LIMIT_REACHED)
        self.assertEqual(json.loads(self.cursor.read_text())["after"], 2)

    def test_a_cardinal_without_the_capability_is_not_called(self):
        b = ie.read_binding(self.home, SUP)
        ie.write_binding(self.home, SUP, {**b, "capabilities": {"supervision_events": {"enabled": False}}})
        self.events += [wev(2, 10)]
        n = len(self.server.bodies)
        code, out, err = self.reviewed("--tag", "all:a")
        self.assertEqual((code, out.splitlines()[0]), (0, bud.SERVER_TOO_OLD), err)
        self.assertEqual(len(self.server.bodies), n)

    def test_the_review_key_and_subject_grant(self):
        self.events += [wev(2, 10)]
        batch = self.page_read()["batch"]
        self.assertEqual(self.reviewed("--tag", "all:a", "--done", "a.1@#2", batch=batch)[0], 0)
        body = self.server.bodies[-1]
        self.assertEqual(body["idempotency_key"], "sup:rv:" + batch)
        self.assertNotIn(GRANT["token"], self.file.read_text())
        self.assertEqual((body["subject_grant"], body["session_id"]), (GRANT["token"], SUP))

    def test_without_a_direction_reviewed_is_unchanged(self):
        self.file.unlink()
        self.events += [wev(2, 10)]
        code, out, err = self.reviewed()
        self.assertEqual((code, json.loads(out)), (0, {"investigation_id": INV, "after": 2}))
        self.events += [wev(3, 20)]
        code, _, err = self.reviewed("--tag", "all:a")
        self.assertEqual(code, 2)
        self.assertIn("no open direction for --tag/--done", err)

    def test_a_page_without_worker_events_needs_no_tags(self):
        self.assertEqual(self.reviewed("--tag", "all:a")[0], 0)
        self.n = len(self.server.events)
        self.events += [wev(2, 10, grantee=True)]
        code, out, err = self.reviewed()
        self.assertEqual((code, out.strip()), (0, "reviewed through #2"), err)
        self.assertEqual(len(self.server.events), self.n)


class Passes(CLIBase):
    def test_estimator_and_stepback_copies(self):
        self.events = [wev(1, 0, typ="experiment.started"), wev(2, 10, typ="experiment.completed"),
                       wev(3, 20, typ="question.opened")]
        brief = self.dir / "brief-1.md"
        brief.write_text("Question: make it shard-safe.\nFacts: #2 tally ran.\n")
        code, out, err = self.budget("pass", "estimator", "--brief", str(brief), "--worker-session", WORKER)
        self.assertEqual(code, 0, err)
        f = self.dir / "brief-1-estimator.md"
        self.assertEqual(out.strip(), f"wrote {f}: run the estimator pass on it; save its JSON for --set --plan")
        text = f.read_text()
        self.assertTrue(text.startswith(bud.FRESH))
        self.assertIn("the worker's record so far: 0.33 agent-active h over 3 events, 1 results", text)
        self.assertIn("anchor on the measured pace above, never on human pace", text)
        code, out, err = self.budget("pass", "stepback", "--brief", str(brief))
        self.assertEqual(code, 0, err)
        text = (self.dir / "brief-1-stepback.md").read_text()
        self.assertIn("An elegant solution is often hiding in plain sight.", text)
        self.assertTrue(text.rstrip().endswith("Answer in at most 800 words."))
        self.assertNotIn("ordinary overrun", text)
        self.assertEqual(self.set("--since-seq", "1")[0], 0)
        self.page_read()
        self.reviewed("--tag", "all:a", "--done", "a.1@#2")
        code, out, err = self.budget("pass", "stepback", "--brief", str(brief), "--milestone", "a")
        self.assertEqual(code, 0, err)
        text = (self.dir / "brief-1-stepback-a.md").read_text()
        self.assertIn("First: is a an ordinary overrun", text)
        self.assertIn("a (title a): 0.33 of 1.0 h P50 spent, 1 of 2 deliverables done", text)
        self.assertIn("- a.1 done #2: a commit for a part 1 and its test passes", text)
        self.assertIn("- a.2 open:", text)
        code, _, err = self.budget("pass", "stepback", "--brief", str(brief), "--milestone", "zz")
        self.assertIn("one of a, b, or all", err)

    def test_no_measured_pace(self):
        brief = self.dir / "brief-1.md"
        brief.write_text("facts")
        code, out, _ = self.budget("pass", "estimator", "--brief", str(brief))
        self.assertIn("(no measured pace: the plan will be uncalibrated)", out)
        self.assertIn('Answer "pace": "none"', (self.dir / "brief-1-estimator.md").read_text())


class VerdictsAndClose(CLIBase):
    def test_verdict_outcome_close_and_calibrate(self):
        self.events = [wev(1, 0), wev(2, 25), wev(3, 50), wev(4, 75)]
        self.set("--since-seq", "1")
        self.page_read()
        self.reviewed("--tag", "all:a", "--done", "a.1@#2")
        fired_seq = self.state()["current"]["firings"][-1]["fired_seq"]
        code, out, err = self.budget("verdict", "a", "worker_choice", "--post-ref", "#5", "--session", SUP)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "verdict on the a step back (#4): worker_choice, routed to worker")
        v = self.server.events[-1]
        self.assertEqual(v, {"type": "stepback.verdict", "payload": {
            "fired_seq": fired_seq, "verdict": "worker_choice", "routed_to": "worker",
            "post_ref": {"investigation_id": INV, "seq": 5}}})
        code, _, err = self.budget("verdict", "a", "on_track")
        self.assertIn("no step back on a without a verdict", err)
        code, out, err = self.budget("outcome", "a", "accepted", "--ref", "#6")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.events[-1]["payload"],
                         {"verdict_seq": len(self.server.events) - 1, "outcome": "accepted",
                          "refs": [{"investigation_id": INV, "seq": 6}]})
        self.assertEqual(self.budget("verdict", "opening", "on_track")[0], 0)
        self.assertEqual(self.server.events[-1]["payload"]["routed_to"], "none")
        code, out, err = self.budget("--close", "--outcome", "done", "--missed", "b")
        self.assertEqual(code, 0, err)
        c = self.server.events[-1]
        self.assertEqual((c["type"], c["payload"]["missed"]), ("direction.closed", ["b"]))
        self.assertEqual(c["payload"]["per_milestone"][0], {"id": "a", "estimate_h": 1, "spent_h": 1.25,
                                                            "delivered": 0.5})
        self.assertIsNone(self.state()["current"])
        code, out, _ = self.budget("calibrate")
        self.assertEqual(out.strip(), "1 labeled firing(s) of 1; a suggestion needs 5 (T 2.0, Z 0.5)")

    def test_calibrate_moves_T(self):
        st = bud.load(self.file, INV)
        noise = {"scope": "milestone", "snapshot": {"delivered": 0.5}, "verdict": {"verdict": "on_track"}}
        useful = {"scope": "milestone", "snapshot": {"delivered": 0.5}, "verdict": {"verdict": "worker_choice"},
                  "outcome": {"outcome": "accepted"}}
        st["history"] = [{"outcome": "done", "per_milestone": [], "firings": [noise] * 4 + [useful], "missed": []}]
        self.assertEqual(bud.calibrate_lines(self.home, st)[1][:29], "suggest T 2.0 -> 2.25, Z 0.5 ")
        st["history"] = [{"outcome": "done", "per_milestone": [], "firings": [useful] * 5, "missed": ["x"]}]
        self.assertIn("suggest T 2.0 -> 1.75", bud.calibrate_lines(self.home, st)[1])
        st["history"][0]["missed"] = []
        self.assertEqual(bud.calibrate_lines(self.home, st)[1], "keep T 2.0, Z 0.5")


class Cache(CLIBase):
    def test_lock_link_and_shape(self):
        self.events = [wev(1, 0)]
        self.set()
        with consumer.locked(self.file):
            code, _, err = self.budget("verdict", "opening", "on_track")
        self.assertEqual(code, 2)
        self.assertIn("in use by another command", err)
        good = self.state()
        for change in ({"milestones": [{"id": "a"}]}, {"thresholds": {"T": 2}}, {"marks": {"a.1": [{"mark": "x"}]}},
                       {"charged": {"a": "1"}}, {"firings": [{"scope": "lag"}]}, {"last_t": "now"}):
            st = json.loads(json.dumps(good))
            st["current"].update(change)
            consumer.private_write(self.file, st)
            code, _, err = self.budget("--show")
            self.assertEqual(code, 2, change)
            self.assertIn("is not a valid budget file", err)
        consumer.private_write(self.file, {**good, "version": 1})
        self.assertIn("not a version 2 budget file", self.budget("--show")[2])
        consumer.private_write(self.file, good)
        os.chmod(self.file, 0o644)
        self.assertIn("private", self.budget("--show")[2])
        os.chmod(self.file, 0o600)
        real = self.dir / "real.json"
        os.replace(self.file, real)
        os.symlink(real, self.file)
        self.assertIn("not a link", self.budget("--show")[2])

    def test_show(self):
        self.events = [wev(1, 0)]
        self.set()
        out = self.budget("--show")[1]
        self.assertIn("calibrated; 0.00 h active (0.00 h none)", out)
        self.assertIn("  a 0.00/1.0 h · 0/2 done — \"title a\"", out)
        self.assertIn("  unknown 0.5-2.0 h: \"CI flakiness\"", out)
        self.assertIn("  fired opening at #1 (no verdict)", out)

    def test_spool_union_through_the_listing(self):
        self.events = [wev(1, 0)]
        self.set("--since-seq", "1")
        self.page_read()
        self.reviewed("--tag", "all:a")
        self.events += [wev(2, 50)]
        root = evidence.default_root(self.home)
        e = evidence.build_entry(server="builtin:codex", tool="shell", tool_name="shell", tool_input={"cmd": "ls"},
                                 tool_response="ok", session_id=WORKER, called_at=at(25))
        evidence.write_entry(root, e)
        with patch.object(bud.evidence, "list_entries", wraps=evidence.list_entries) as listing:
            self.assertEqual(self.reviewed("--tag", "all:a")[0], 0)
        listing.assert_called_once_with(root, WORKER)
        self.assertAlmostEqual(self.state()["current"]["charged"]["a"], 50 / 60)


class WingbeatThroughReviewed(CLIBase):
    """The same replay through the CLI: --set --plan, then `reviewed` with
    --tag / --done / --reopen / --redone on pages of 10 events."""

    def test_fires_like_the_engine(self):
        fx = fixture("wingbeat")
        self.events = [{**wev(s, 0), "created_at": t} for s, t in fx["worker_times"]]
        self.page = 10
        code, out, err = self.set("--since-seq", "199", p=fx["plan"])
        self.assertEqual(code, 0, err)
        fires, prev = [], 198
        while True:
            page = self.page_read()
            if not page["events"]:
                break
            wseqs = [e["seq"] for e in page["events"]]
            flags = tag_flags(fx, wseqs)
            for did, mark, sq in fx["marks"]:
                if prev < sq <= page["next_after"]:
                    flags += [{"done": "--done", "reopened": "--reopen", "redone": "--redone"}[mark], f"{did}@#{sq}"]
            code, out, err = self.reviewed(*flags, batch=page["batch"])
            self.assertEqual(code, 0, err)
            self.assertLessEqual(len(out.strip().splitlines()), 8)
            fires += [line.split()[0] for line in out.splitlines() if line.endswith("FIRE step back")]
            prev = page["next_after"]
        self.assertEqual(sorted(fires), ["aws-native-qualification", "aws-native-qualification", "chunked-lane",
                                         "chunked-lane"])
        st = self.state()["current"]
        self.assertEqual([f["at_seq"] for f in st["firings"]], [199, 270, 306, 323, 330])
        self.assertEqual(self.server.types().count("work.attributed"), 11)
        self.assertEqual(self.server.types().count("deliverable.marked"), len(fx["marks"]))


if __name__ == "__main__":
    unittest.main()

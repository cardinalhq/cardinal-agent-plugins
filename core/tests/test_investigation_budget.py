"""Supervise step backs on a work budget (docs/specs/supervise-stepback.md):
the budget file, spent / earned, the lag and backstop triggers, the
`investigation budget` CLI, and a replay of the wingbeat phase F record."""

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

INV = "inv_" + "b" * 24
WORKER = "worker-sid"
MEMBER = {"origin": "https://test.invalid", "org": "org", "key": "test-key"}
FIXTURE = Path(__file__).resolve().parent / "testdata" / "wingbeat-phase-f-worker-times.json"
T0 = bud.parse_time("2026-10-15T06:00:00Z")
WINGBEAT = ["design:2:2.5:design", "implementation:2.5:3.5:implementation", "validation:2:2.5:validation",
            "review:2:3:review", "ci:1.5:2.5:ci", "image-deploy:1:1.5:image and deploy",
            "shadow:1.5:2:shadow run", "handoff:0.5:1:handoff"]


def at(minutes: float) -> str:
    return bud.iso(T0 + minutes * 60)


def wev(seq, minutes, session=WORKER, cls="semantic", grantee=False, author=True):
    principal = {"kind": "grantee", "id": "grt_" + "d" * 24} if grantee else {"kind": "user", "id": "owner"}
    return {"investigation_id": INV, "seq": seq, "type": "finding.proposed", "class": cls,
            "producer": {"principal": principal, "session_id": session, "is_investigation_author": author},
            "created_at": at(minutes)}


def budget(milestones=("a:1:2:first", "b:3:8:second"), since_seq=1, worker=WORKER):
    return bud.new_budget(question="Q?", milestones=list(milestones), basis="ref class", set_at=T0,
                          since_seq=since_seq, worker_session=worker)


class Spent(unittest.TestCase):
    def test_idle_gaps_are_not_counted(self):
        times = [T0, T0 + 600, T0 + 1200, T0 + 1200 + 31 * 60, T0 + 1200 + 41 * 60]
        self.assertAlmostEqual(bud.active_hours(times, 30), (1200 + 600) / 3600)
        self.assertAlmostEqual(bud.active_hours(times, 31), (1200 + 31 * 60 + 600) / 3600)
        self.assertEqual(bud.active_hours([], 30), 0)
        self.assertEqual(bud.active_hours([T0, T0], 30), 0)

    def test_worker_session_filter(self):
        events = [wev(1, 0), wev(2, 10, session="other"), wev(3, 20, grantee=True), wev(4, 30, cls="owner_input"),
                  wev(5, 40, cls="owner"), wev(6, 50, session=None), wev(7, 60, cls="control"), "junk",
                  {"seq": "x"}]
        self.assertEqual([s for s, _ in bud.worker_times(events, WORKER, 1)], [1, 7])
        self.assertEqual([s for s, _ in bud.worker_times(events, WORKER, 2)], [7])
        # Without a worker session: the author's session events.
        self.assertEqual([s for s, _ in bud.worker_times(events, None, 1)], [1, 2, 7])
        self.assertEqual(bud.worker_times([wev(1, 0, author=False)], None, 1), [])

    def test_union_of_events_and_spool(self):
        b = budget()
        events = [wev(1, 0), wev(2, 50)]                       # 50 min apart: idle
        spool = [{"evidence_id": "ev_" + "1" * 12, "called_at": at(25)},
                 {"evidence_id": "ev_" + "2" * 12, "called_at": at(-30)}]   # before the budget: ignored
        m = bud.measure(b, events, spool)
        self.assertAlmostEqual(m["spent"], 50 / 60)
        self.assertAlmostEqual(bud.measure(b, events, [])["spent"], 0)

    def test_parse_time(self):
        self.assertEqual(bud.parse_time("2026-10-15T06:00:00Z"), T0)
        self.assertEqual(bud.parse_time("2026-10-15T06:00Z"), T0)
        self.assertEqual(bud.parse_time("2026-10-15T08:00:00.250+02:00"), T0 + 0.25)
        for bad in ("", None, "yesterday", "2026-13-01T00:00Z", 5):
            self.assertIsNone(bud.parse_time(bad))


class Earned(unittest.TestCase):
    def test_refs_required_and_fraction(self):
        b = budget()
        for ref in (None, "", "#0", "202", "ev_xyz", "rcpt_" + "a" * 24):
            with self.assertRaises(bud.BudgetError):
                bud.mark_done(b, "a", ref)
        with self.assertRaises(bud.BudgetError):
            bud.mark_done(b, "zzz", "#2")
        for f in (0, -1, 1.5):
            with self.assertRaises(bud.BudgetError):
                bud.mark_done(b, "b", "#2", f)
        bud.mark_done(b, "a", "#2")
        bud.mark_done(b, "b", "ev_" + "a" * 12, 0.5)
        self.assertEqual(bud.earned_hours(b), 1 + 1.5)


class Triggers(unittest.TestCase):
    # P80 total 10 h: lag can fire from 1.5 h spent.
    def b(self):
        return budget(("a:2:4:a", "b:3:6:b"))

    def fire(self, b, spent, earned=0.0):
        d = bud.evaluate(b, spent, earned)
        if d["fire"]:
            bud.record_firing(b, d, spent, earned, now=T0, head_seq=None)
        return d["fire"]

    def test_nothing_before_15_percent(self):
        self.assertEqual(self.fire(self.b(), 1.49), [])
        self.assertEqual(self.fire(self.b(), 1.5), ["lag"])

    def test_lag_threshold_is_half_of_spent(self):
        b = self.b()
        self.assertEqual(self.fire(b, 4.0, earned=2.0), [])          # 2.0 is not < 0.5 x 4.0
        self.assertEqual(bud.evaluate(b, 4.0, 2.0)["next_lag_h"], 4.0)
        self.assertEqual(self.fire(b, 4.01, earned=2.0), ["lag"])

    def test_refire_only_after_50_percent_more_spent(self):
        b = self.b()
        self.assertEqual(self.fire(b, 2.0), ["lag"])
        self.assertEqual(self.fire(b, 2.0), [])                       # never twice for one crossing
        self.assertEqual(self.fire(b, 2.99), [])
        d = bud.evaluate(b, 2.99, 0)
        self.assertTrue(d["lagging"])
        self.assertEqual(d["next_lag_h"], 3.0)
        self.assertEqual(self.fire(b, 3.0), ["lag"])
        self.assertEqual(self.fire(b, 4.4), [])
        self.assertEqual(self.fire(b, 4.5), ["lag"])

    def test_backstops_fire_once_each(self):
        b = self.b()
        self.assertEqual(self.fire(b, 9.9, earned=9), [])
        self.assertEqual(self.fire(b, 10, earned=9), ["backstop 1x"])
        self.assertEqual(self.fire(b, 15, earned=9), [])
        self.assertEqual(bud.evaluate(b, 15, 9)["next_backstop"], (2, 20.0))
        # Skipping past 2x and 4x at once is one firing, both recorded.
        self.assertEqual(self.fire(b, 41, earned=30), ["backstop 4x"])
        self.assertEqual(self.fire(b, 80, earned=50), [])
        self.assertIsNone(bud.evaluate(b, 80, 50)["next_backstop"])

    def test_lag_and_backstop_together_are_one_firing(self):
        b = self.b()
        self.assertEqual(self.fire(b, 10, earned=1), ["backstop 1x", "lag"])
        self.assertEqual(len(b["firings"]), 1)
        self.assertEqual(b["firings"][0]["reasons"], ["backstop", "lag"])

    def test_thresholds_live_in_the_budget(self):
        b = self.b()
        b["thresholds"]["lag_start"] = 0.5
        self.assertEqual(self.fire(b, 4.9), [])
        self.assertEqual(self.fire(b, 5.0), ["lag"])


class BudgetValidation(unittest.TestCase):
    def test_milestones(self):
        self.assertEqual(bud.parse_milestone("ci-2:1.5:2.5:CI: two rounds"),
                         {"id": "ci-2", "p50": 1.5, "p80": 2.5, "title": "CI: two rounds"})
        for bad in ("ci:1", "CI:1:2:x", "ci:0:1:x", "ci:2:1:x", "ci:a:1:x", "ci:1:2:", "c" * 33 + ":1:2:x",
                    "ci:1:2:line\nbreak"):
            with self.assertRaises(bud.BudgetError, msg=bad):
                bud.parse_milestone(bad)
        with self.assertRaises(bud.BudgetError):
            budget(())
        with self.assertRaises(bud.BudgetError):
            budget([f"m{i}:1:2:x" for i in range(21)])
        with self.assertRaises(bud.BudgetError):
            budget(("a:1:2:x", "a:1:2:y"))
        with self.assertRaises(bud.BudgetError):
            budget(worker="bad session")
        self.assertEqual(len(budget([f"m{i}:1:2:x" for i in range(20)])["milestones"]), 20)


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
        self.dir = self.home / "sup"
        self.dir.mkdir(mode=0o700)
        self.file = self.dir / "budget.json"
        self.events = []
        self.wiring = SimpleNamespace(paths=SimpleNamespace(home=self.home / ".codex"), environ={},
                                      client="codex/test", cli=str(self.home / "scripts/cli"),
                                      connection=Mock(return_value=MEMBER), _off=lambda name: False)
        reader = patch.object(ie, "read_events", side_effect=self.read)
        reader.start()
        self.addCleanup(reader.stop)

    def read(self, conn, inv, *, after=0, limit=200, client, **_):
        assert inv == INV
        shown = [e for e in self.events if e["seq"] > after][:limit]
        head = max([e["seq"] for e in self.events], default=0)
        return {"investigation_id": inv, "events": shown, "last_seq": shown[-1]["seq"] if shown else head,
                "page_size": len(shown), "head_seq": head}

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agent.cli_main(["budget", INV, "--budget-file", str(self.file), *argv], self.wiring)
        return code, out.getvalue(), err.getvalue()

    def set(self, *extra, milestones=("a:2:4:first", "b:3:6:second")):
        args = ["--set", "--question", "Make tallies shard-safe", "--basis", "two earlier phases; unknown: CI",
                "--worker-session", WORKER, *extra]
        for m in milestones:
            args += ["--milestone", m]
        return self.cli(*args)


class CLI(CLIBase):
    def test_set_check_done_show_close(self):
        self.events = [wev(1, -60), wev(2, -50)]
        code, out, err = self.set()
        self.assertEqual(code, 0, err)
        self.assertIn("counting the worker's active time from #3", out)
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        # 100 min of active worker time, nothing done: lag (P80 10 h -> 1.5 h)... not yet.
        self.events += [wev(3 + i, i * 10) for i in range(11)]
        code, out, err = self.cli("--check")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[1], f"spent 1.67 h active since #3 (11 worker events; activity spool not on this machine)")
        self.assertEqual(lines[-1], "FIRE lag: spent >= 15% of P80 and earned 0.00 h < 0.5 x spent")
        self.assertEqual(lines[-2], "next: lag when spent > 2.50 h with nothing more done; backstop 1x at 10.00 h")
        self.assertEqual(self.cli("--check")[1].splitlines()[-1], "ok (lag persists; fired at 1.67 h, re-fires at 2.50 h)")
        code, out, err = self.cli("--done", "a", "--ref", "#8")
        self.assertEqual((code, out.strip()), (0, "marked a done (100%, #8); earned now 2.00 h"))
        r = json.loads(self.cli("--check", "--json")[1])
        self.assertEqual((r["earned_h"], r["done"], r["open"], r["fire"]), (2.0, ["a"], ["b"], []))
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(r["next_lag_h"], 4.0)
        self.assertEqual(r["next_backstop"], {"multiple": 1, "at_h": 10.0})
        show = self.cli("--show")[1]
        self.assertIn("a: P50 2.0 h / P80 4.0 h, done 100% (#8)", show)
        self.assertIn("fired lag at spent 1.67 h", show)
        code, out, err = self.cli("--close", "--outcome", "stopped")
        self.assertEqual(code, 0, err)
        state = consumer.private_read(self.file)
        self.assertIsNone(state["current"])
        h = state["history"][-1]
        self.assertEqual((h["outcome"], h["spent_h"], h["earned_h"], h["p50_total_h"]), ("stopped", 1.667, 2.0, 5.0))
        # #8 is the 6th worker event: 50 active minutes when its ref happened.
        self.assertEqual([(m["id"], m["actual_h"]) for m in h["milestones"]], [("a", 0.833), ("b", None)])
        self.assertEqual(self.cli("--check")[0], 2)

    def test_since_seq_counts_from_the_direction_start(self):
        self.events = [wev(1, -60, session="owner-web", author=True, cls="owner"), wev(2, -50), wev(3, -40)]
        self.assertEqual(self.set("--since-seq", "1")[0], 0)
        b = consumer.private_read(self.file)["current"]
        self.assertEqual((b["since_seq"], b["set_at"]), (1, at(-60)))
        r = json.loads(self.cli("--check", "--json")[1])
        self.assertEqual((r["worker_events"], r["spent_h"]), (2, round(10 / 60, 3)))

    def test_set_replaces_and_closes_the_open_budget(self):
        self.events = [wev(1, 0), wev(2, 20)]
        self.set("--since-seq", "1")
        code, out, _ = self.set(milestones=("c:1:2:third",))
        self.assertEqual(code, 0)
        self.assertIn("closed the previous budget as replaced: spent 0.33 h", out)
        state = consumer.private_read(self.file)
        self.assertEqual([m["id"] for m in state["current"]["milestones"]], ["c"])
        self.assertEqual(state["history"][-1]["outcome"], "replaced")

    def test_history_is_bounded(self):
        self.events = [wev(1, 0)]
        for _ in range(bud.HISTORY_MAX + 3):
            self.assertEqual(self.set()[0], 0)
        self.assertEqual(len(consumer.private_read(self.file)["history"]), bud.HISTORY_MAX)

    def test_spool_is_read_through_the_listing(self):
        self.events = [wev(1, -5), wev(2, 0), wev(3, 50)]
        self.set("--since-seq", "2")
        root = evidence.default_root(self.home)
        e = evidence.build_entry(server="builtin:codex", tool="shell", tool_name="shell", tool_input={"cmd": "ls"},
                                 tool_response="ok", session_id=WORKER, called_at=at(25))
        evidence.write_entry(root, e)
        with patch.object(bud.evidence, "list_entries", wraps=evidence.list_entries) as listing:
            r = json.loads(self.cli("--check", "--json")[1])
        listing.assert_called_once_with(root, WORKER)
        self.assertEqual((r["captured_calls"], r["spent_h"]), (1, round(50 / 60, 3)))

    def test_validation_and_mode_flags(self):
        self.events = [wev(1, 0)]
        code, _, err = self.cli("--set", "--question", "Q", "--basis", "b")
        self.assertEqual(code, 2)
        self.assertIn("1 to 20 milestones", err)
        self.assertEqual(self.set()[0], 0)
        for argv, said in ((["--done", "a"], "--done needs --ref"),
                           (["--done", "a", "--ref", "#1", "--outcome", "done"], "--outcome does not go with --done"),
                           (["--check", "--ref", "#1"], "--ref does not go with --check"),
                           (["--close"], "--close needs --outcome"),
                           (["--done", "nope", "--ref", "#1"], "no milestone 'nope'")):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(said, err)

    def test_file_is_private_and_bound_to_its_investigation(self):
        self.events = [wev(1, 0)]
        self.set()
        os.chmod(self.file, 0o644)
        code, _, err = self.cli("--show")
        self.assertEqual(code, 2)
        self.assertIn("private", err)
        os.chmod(self.file, 0o600)
        state = consumer.private_read(self.file)
        state["investigation_id"] = "inv_" + "c" * 24
        consumer.private_write(self.file, state)
        code, _, err = self.cli("--show")
        self.assertEqual(code, 2)
        self.assertIn("is the budget of inv_cccc", err)

    def test_lock_is_held(self):
        self.events = [wev(1, 0)]
        self.set()
        with consumer.locked(self.file):
            code, _, err = self.cli("--done", "a", "--ref", "#1")
        self.assertEqual(code, 2)
        self.assertIn("in use by another command", err)

    def test_a_scoped_grant_may_run_it(self):
        self.events = [wev(1, 0)]
        scoped = {**MEMBER, "token": "t", "grant_id": "grt_" + "d" * 24, "investigation_id": INV, "scopes": ["read"]}
        cred = self.dir / "grant.json"
        with patch.object(consumer, "credential_connection", return_value=scoped):
            code, out, err = self.set("--credential-file", str(cred))
        self.assertEqual(code, 0, err)
        self.wiring.connection.assert_not_called()


class WingbeatReplay(unittest.TestCase):
    """inv_cab0d4ba690509cd646915c2 phase F: the worker's real event times
    #197-#336, the careful estimate's milestones, `design` done at #202 and
    nothing else before #330 (the record). The owner replaced the plan at
    #336; the lag trigger must fire well before."""

    def test_lag_fires_before_336(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        rows = [(s, bud.parse_time(t)) for s, t in data["events"]]
        self.assertTrue(all(197 <= s <= 336 for s, _ in rows) and len(rows) > 100)
        b = bud.new_budget(question="phase F", milestones=WINGBEAT, basis="careful estimate", set_at=rows[0][1],
                           since_seq=197, worker_session="w")
        self.assertEqual(bud.totals(b), (13.0, 18.5))
        fired = []
        for i, (seq, _) in enumerate(rows):
            if seq >= 202 and "design" not in b["done"]:
                bud.mark_done(b, "design", "#202")
            spent = bud.active_hours([t for _, t in rows[: i + 1]], 30)
            d = bud.evaluate(b, spent, bud.earned_hours(b))
            if d["fire"]:
                bud.record_firing(b, d, spent, bud.earned_hours(b), now=rows[i][1], head_seq=seq)
                fired.append((seq, d["fire"], round(spent, 2)))
        self.assertTrue(fired)
        first_seq, reasons, spent = fired[0]
        self.assertEqual(reasons, ["lag"])
        self.assertLess(first_seq, 336)
        self.assertGreater(spent, 4.0)          # earned 2 h (design): lag needs spent > 4 h
        # At #288 (10:59Z, 4.1 active hours in); the REWORK came at #330, the owner's pivot at #336.
        self.assertEqual(first_seq, 288)


if __name__ == "__main__":
    unittest.main()

"""Spool bounds: every tool call is captured, so gc's size caps are
load-bearing (spec §18: under 256 MiB, oldest first; 10,000 entries per
session; 14-day TTL; token files and markers are never evicted for size).

Run with: cd core && python3 -m unittest tests.test_evidence_spool_bounds -v
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cardinal_core import evidence
from cardinal_core import evidence_capture as cap


class SpoolBoundsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = evidence.default_root(self.home)
        evidence.ensure_root(self.root)
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, session: str, i: int, size: int, age_s: float) -> Path:
        d = self.root / session
        d.mkdir(exist_ok=True, mode=0o700)
        p = d / f"ev_{i:012x}.json"
        p.write_bytes(b"x" * size)
        t = self.now - age_s
        os.utime(p, (t, t))
        return p

    def test_total_cap_evicts_oldest_across_sessions(self):
        a = [self.put("sa", i, 1000, 1000 - i) for i in range(10)]
        b = [self.put("sb", 100 + i, 1000, 500 - i) for i in range(10)]
        removed = evidence.gc(self.root, now=self.now, force=True, max_bytes=12000)
        self.assertEqual(removed, 8)
        self.assertFalse(any(p.exists() for p in a[:8]))
        self.assertTrue(all(p.exists() for p in a[8:] + b))

    def test_per_session_cap(self):
        ps = [self.put("s1", i, 10, 100 - i) for i in range(30)]
        evidence.gc(self.root, now=self.now, force=True, max_per_session=20)
        self.assertEqual(sum(p.exists() for p in ps), 20)
        self.assertFalse(any(p.exists() for p in ps[:10]))

    def test_ttl_still_applies_and_markers_survive_size_eviction(self):
        old = self.put("s1", 1, 10, 15 * 24 * 3600)
        keep = self.put("s1", 2, 5000, 10)
        tok = self.root / "s1" / evidence.TOKEN_FILE
        tok.write_text("{}")
        hinted = self.root / "s1" / cap.HINTED
        hinted.write_text("")
        evidence.gc(self.root, now=self.now, force=True, max_bytes=100)
        self.assertFalse(old.exists())
        self.assertFalse(keep.exists())  # evicted for size
        self.assertTrue(tok.exists())
        self.assertTrue(hinted.exists())
        self.assertTrue((self.root / "s1").is_dir())

    def test_env_cap_and_interval(self):
        self.assertEqual(evidence.spool_cap_bytes({}), 256 << 20)
        self.assertEqual(evidence.spool_cap_bytes({"CARDINAL_EVIDENCE_MAX_MB": "5"}), 5 << 20)
        self.assertEqual(evidence.spool_cap_bytes({"CARDINAL_EVIDENCE_MAX_MB": "0"}), 256 << 20)
        self.assertEqual(evidence.spool_cap_bytes({"CARDINAL_EVIDENCE_MAX_MB": "lots"}), 256 << 20)
        self.assertEqual(evidence.GC_INTERVAL_S, 600)
        p = self.put("s1", 1, 10, 15 * 24 * 3600)
        evidence.gc(self.root, now=self.now, force=True)
        self.assertFalse(p.exists())
        p = self.put("s1", 2, 10, 15 * 24 * 3600)
        evidence.gc(self.root, now=self.now + 60)  # inside the interval: skipped
        self.assertTrue(p.exists())
        evidence.gc(self.root, now=self.now + 700)
        self.assertFalse(p.exists())

    def test_stale_temp_files_are_reaped_and_fresh_ones_kept(self):
        d = self.root / "s1"
        d.mkdir(mode=0o700)
        stale, fresh = d / ".ev_abc.tmp", d / ".ev_def.tmp"
        stale.write_text("x")
        fresh.write_text("x")
        t = self.now - 2 * 3600
        os.utime(stale, (t, t))
        evidence.gc(self.root, now=self.now, force=True)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_size_cap_holds_when_one_pass_cannot_list_the_whole_spool(self):
        # A spool too big to list inside one pass's budget (a heavy user:
        # every tool call of every subagent for 14 days). A pass that runs
        # out of budget used to skip the size bound entirely, forever; now
        # the per-session totals persist in the gc index and passes converge.
        sessions = [f"s{k:02d}" for k in range(30)]
        paths = {}
        for k, s in enumerate(sessions):
            paths[s] = [self.put(s, k * 1000 + i, 1000, 100000 - k * 1000 - i) for i in range(40)]
        cap_bytes = 200 * 1000
        passes = 0
        while passes < 2000:
            passes += 1
            evidence.gc(self.root, now=self.now, force=True, max_bytes=cap_bytes, budget_s=0.003)
            live = sum(p.exists() for ps in paths.values() for p in ps)
            if live * 1000 <= cap_bytes:
                break
        self.assertLessEqual(sum(p.exists() for ps in paths.values() for p in ps) * 1000, cap_bytes)
        # The most recently active session is the last to lose anything.
        self.assertTrue(all(p.exists() for p in paths[sessions[-1]]))
        self.assertFalse(any(p.exists() for p in paths[sessions[0]]))

    def test_incomplete_pass_retries_soon_and_index_is_reused(self):
        for k in range(5):
            for i in range(5):
                self.put(f"s{k}", k * 10 + i, 10, 100 + i)
        evidence.gc(self.root, now=self.now, force=True, budget_s=0)
        # Out of budget: the next hook call inside GC_RETRY_S is skipped, one
        # after it runs, without waiting out GC_INTERVAL_S.
        stamp = (self.root / evidence.GC_STAMP).stat().st_mtime
        self.assertLess(stamp, self.now)
        old = self.put("s0", 99, 10, 30 * 24 * 3600)
        evidence.gc(self.root, now=self.now + evidence.GC_RETRY_S + 1)
        self.assertFalse(old.exists())
        idx = evidence._gc_index_load(self.root)
        self.assertEqual(sorted(idx), [f"s{k}" for k in range(5)])
        self.assertEqual(idx["s1"][1:3], [50, 5])
        # A garbage index is ignored (every session is listed again).
        (self.root / evidence.GC_INDEX).write_text("{not json")
        self.assertEqual(evidence._gc_index_load(self.root), {})
        (self.root / evidence.GC_INDEX).write_text('{"sessions": {"../x": [1, 2, 3, 4, 5], "s1": ["a"]}}')
        self.assertEqual(evidence._gc_index_load(self.root), {})
        evidence.gc(self.root, now=self.now, force=True, max_bytes=100)
        self.assertLessEqual(evidence.spool_usage(self.root)[1], 100)

    def test_cached_session_still_expires(self):
        p = self.put("s1", 1, 10, 13 * 24 * 3600)
        evidence.gc(self.root, now=self.now, force=True)
        self.assertTrue(p.exists())
        # Nothing in s1 changed, but its oldest entry is now past the TTL.
        evidence.gc(self.root, now=self.now + 2 * 24 * 3600, force=True)
        self.assertFalse(p.exists())

    def test_usage(self):
        self.put("s1", 1, 100, 1)
        self.put("s2", 2, 50, 1)
        self.assertEqual(evidence.spool_usage(self.root), (2, 150, 2))

    def test_concurrent_writers_race_safely(self):
        errors = []

        def worker(k):
            try:
                for i in range(40):
                    c = cap.ToolCall(runtime="claude-code", tool_name="Bash", source=cap.builtin_source("claude-code"),
                                     tool="Bash", tool_input={"command": f"w{k}-{i}"}, response="ok",
                                     session_id="race", tool_use_id=f"t{k}-{i}")
                    cap.capture_call(c, self.home, env={}, rules=None)
                    evidence.gc(self.root, force=True, max_per_session=50)
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        ts = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        n = len(list((self.root / "race").glob("ev_*.json")))
        self.assertLessEqual(n, 160)
        self.assertGreater(n, 0)


class HoldGcTests(unittest.TestCase):
    """Evidence holds (.hold) in gc: a best-effort preference in size
    eviction, within HOLD_MAX_FRACTION of the cap; never retention."""

    DAY = 24 * 3600

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = evidence.default_root(self.home)
        evidence.ensure_root(self.root)
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    put = SpoolBoundsTests.put

    def hold(self, session: str, set_ago_s: float = 0, seconds: float = evidence.MAX_HOLD_S,
             reason: str = "checkpoint") -> None:
        self.assertIsNotNone(evidence.set_hold(self.root, session, reason=reason, seconds=seconds,
                                               now=self.now - set_ago_s))

    def test_unheld_sessions_are_evicted_before_held_ones(self):
        held = [self.put("sa", i, 1000, 1000 - i) for i in range(5)]        # least recently active
        free = [self.put("sb", 100 + i, 1000, 500 - i) for i in range(10)]
        self.hold("sa")
        removed = evidence.gc(self.root, now=self.now, force=True, max_bytes=12000)
        self.assertEqual(removed, 3)
        self.assertTrue(all(p.exists() for p in held))
        self.assertFalse(any(p.exists() for p in free[:3]))
        self.assertTrue(all(p.exists() for p in free[3:]))
        self.assertIsNone(evidence.read_breach(self.root, "sa"))
        self.assertTrue((self.root / "sa" / evidence.HOLD_FILE).exists())

    def test_holds_over_half_the_cap_lose_the_oldest_set_at_first_and_record_a_breach(self):
        # h2 is the least recently active, but h1's hold is the older one.
        h2 = [self.put("h2", i, 1000, 3000 - i) for i in range(4)]
        h1 = [self.put("h1", 100 + i, 1000, 2000 - i) for i in range(4)]
        u = [self.put("u", 200 + i, 1000, 100 - i) for i in range(4)]
        self.hold("h1", set_ago_s=3 * self.DAY)
        self.hold("h2", set_ago_s=1 * self.DAY)
        # cap 10000, so holds protect at most 5000; held bytes reach 8000.
        evidence.gc(self.root, now=self.now, force=True, max_bytes=10000)
        self.assertFalse(any(p.exists() for p in h1[:2]))
        self.assertTrue(all(p.exists() for p in h1[2:] + h2 + u))
        b = evidence.read_breach(self.root, "h1")
        self.assertEqual(b["entries_removed"], 2)
        self.assertEqual(b["evidence_ids"], [p.name[:-len(".json")] for p in h1[:2]])
        self.assertIsNone(evidence.read_breach(self.root, "h2"))
        self.assertEqual(evidence.breach_for(self.root, h1[0].name[:-len(".json")]), ("h1", True))
        self.assertIsNone(evidence.breach_for(self.root, h2[0].name[:-len(".json")]))
        self.assertIn("despite a hold on session h1", evidence.breach_note(self.root, h1[1].name[:-5]))
        # A later breach adds to the same marker.
        evidence.gc(self.root, now=self.now, force=True, max_bytes=7000)
        self.assertEqual(evidence.read_breach(self.root, "h1")["entries_removed"], 4)

    def test_holds_never_keep_the_spool_over_the_cap(self):
        a = [self.put("a", i, 1000, 2000 - i) for i in range(4)]
        b = [self.put("b", 100 + i, 1000, 1000 - i) for i in range(4)]
        self.hold("a", set_ago_s=2 * self.DAY)
        self.hold("b", set_ago_s=1 * self.DAY)
        evidence.gc(self.root, now=self.now, force=True, max_bytes=3000)
        self.assertLessEqual(evidence.spool_usage(self.root)[1], 3000)
        # The older hold gave way first; both are recorded.
        self.assertFalse(any(p.exists() for p in a))
        self.assertGreater(sum(p.exists() for p in b), 0)
        self.assertEqual(evidence.read_breach(self.root, "a")["entries_removed"], 4)
        self.assertIsNotNone(evidence.read_breach(self.root, "b"))
        # The markers are never evicted for size.
        self.assertTrue((self.root / "a" / evidence.HOLD_FILE).exists())
        self.assertTrue((self.root / "a" / evidence.HOLD_BREACHED).exists())

    def test_expired_hold_is_removed_and_its_directory_can_go(self):
        p = self.put("s1", 1, 10, 10)
        self.hold("s1", set_ago_s=2 * self.DAY, seconds=self.DAY)   # expired a day ago
        self.assertIsNone(evidence.read_hold(self.root, "s1", now=self.now))
        p.unlink()
        evidence.gc(self.root, now=self.now, force=True)
        self.assertFalse((self.root / "s1").exists())
        # A live hold keeps an otherwise empty directory until its `until`, no longer.
        q = self.put("s2", 2, 10, 10)
        self.hold("s2", seconds=self.DAY)
        q.unlink()
        evidence.gc(self.root, now=self.now, force=True)
        self.assertTrue((self.root / "s2" / evidence.HOLD_FILE).exists())
        evidence.gc(self.root, now=self.now + self.DAY + 60, force=True)
        self.assertFalse((self.root / "s2").exists())

    def test_until_is_clamped_and_a_malformed_hold_expires_anyway(self):
        self.put("s1", 1, 10, 10)
        hold = self.root / "s1" / evidence.HOLD_FILE
        hold.write_text('{"set_at": "2026-01-01T00:00:00Z", "until": "2099-01-01T00:00:00Z", "reason": "manual"}')
        h = evidence._parse_hold(evidence._read_private_json(hold))
        self.assertEqual(h["until"] - h["set_at"], evidence.MAX_HOLD_S)
        hold.write_text("{not json")
        self.assertIsNone(evidence.read_hold(self.root, "s1"))
        t = self.now - evidence.RETENTION_S - 60
        os.utime(hold, (t, t))
        evidence.gc(self.root, now=self.now, force=True)
        self.assertFalse(hold.exists())

    def test_age_expiry_still_removes_held_entries(self):
        old = self.put("s1", 1, 10, 15 * self.DAY)
        keep = self.put("s1", 2, 10, 10)
        self.hold("s1")
        evidence.gc(self.root, now=self.now, force=True)
        self.assertFalse(old.exists())
        self.assertTrue(keep.exists())

    def test_an_older_cores_five_field_index_stays_readable(self):
        for k in range(3):
            for i in range(4):
                self.put(f"s{k}", k * 10 + i, 1000, 1000 * (3 - k) - i)
        self.hold("s0")
        # An older core wrote these rows; this one reads and keeps the same shape.
        rows = {f"s{k}": [1, 4000, 4, self.now + 1e6, self.now - 1000 * (3 - k)] for k in range(3)}
        (self.root / evidence.GC_INDEX).write_text(json.dumps({"version": 1, "sessions": rows}))
        self.assertEqual(sorted(evidence._gc_index_load(self.root)), ["s0", "s1", "s2"])
        evidence.gc(self.root, now=self.now, force=True, max_bytes=9000)
        self.assertLessEqual(evidence.spool_usage(self.root)[1], 9000)
        self.assertEqual(len(list((self.root / "s0").glob("ev_*.json"))), 4)
        raw = json.loads((self.root / evidence.GC_INDEX).read_text())["sessions"]
        self.assertTrue(raw)
        self.assertTrue(all(len(v) == 5 for v in raw.values()))
        self.assertEqual(sorted(evidence._gc_index_load(self.root)), sorted(raw))

    def test_stale_hold_temp_files_are_reaped(self):
        d = self.root / "s1"
        d.mkdir(mode=0o700)
        stale, breach_tmp, fresh = d / ".hold_abc.tmp", d / ".hold-breached_abc.tmp", d / ".hold_def.tmp"
        for p in (stale, breach_tmp, fresh):
            p.write_text("x")
        t = self.now - 2 * 3600
        os.utime(stale, (t, t))
        os.utime(breach_tmp, (t, t))
        evidence.gc(self.root, now=self.now, force=True)
        self.assertFalse(stale.exists())
        self.assertFalse(breach_tmp.exists())
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()

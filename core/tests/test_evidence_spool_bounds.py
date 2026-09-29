"""Spool bounds: every tool call is captured, so gc's size caps are
load-bearing (spec §18: under 256 MiB, oldest first; 10,000 entries per
session; 14-day TTL; token files and markers are never evicted for size).

Run with: cd core && python3 -m unittest tests.test_evidence_spool_bounds -v
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()

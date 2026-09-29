"""capture_call_guarded: whatever the tool, a call is never silently lost.

A pathological input (one the gate or the scrub cannot finish inside the
hook's budget) or a failure inside the pipeline is recorded as a withheld
stub (reason "unreadable"), so the agent gets an id and a reason; nothing
unchecked is kept (fail closed).

Run with: cd core && python3 -m unittest tests.test_evidence_guarded -v
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cardinal_core import evidence
from cardinal_core import evidence_capture as cap
from cardinal_core import evidence_gate as gate


def call(**over):
    base = dict(runtime="claude-code", tool_name="SomeFutureTool", source=cap.builtin_source("claude-code"),
                tool="SomeFutureTool", tool_input={"q": "x"}, response={"ok": True}, session_id="s1",
                tool_use_id="toolu_1", client="claude-code/1.0")
    base.update(over)
    return cap.ToolCall(**base)


class GuardedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = evidence.default_root(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self):
        return [json.loads(p.read_text()) for p in self.root.rglob("ev_*.json")]

    def test_normal_call_is_captured_as_usual(self):
        got = cap.capture_call_guarded(call(), self.home, env={})
        self.assertFalse(got.withheld)
        self.assertEqual(got.entry["result"], {"structured": {"ok": True}})
        self.assertEqual(len(self.entries()), 1)

    def test_budget_exhausted_is_a_withheld_stub_not_silence(self):
        def slow(*a, **k):
            time.sleep(5)

        with mock.patch.object(gate, "check", slow):
            t = time.monotonic()
            got = cap.capture_call_guarded(call(), self.home, env={}, budget_s=0.2)
            elapsed = time.monotonic() - t
        self.assertLess(elapsed, 1.0)
        self.assertIsNotNone(got)
        self.assertTrue(got.withheld)
        self.assertRegex(got.line, r"^\[evidence:ev_[0-9a-f]{12} withheld: could not be checked \(")
        [e] = self.entries()
        self.assertEqual(e["withheld"]["reason"], "unreadable")
        self.assertEqual(e["withheld"]["rule"], "budget")
        self.assertIsNone(e["args"])
        self.assertIsNone(e["result"])
        self.assertEqual(e["evidence_id"], cap.evidence_id_v2("claude-code", "s1", "toolu_1"))

    def test_pipeline_failure_is_a_withheld_stub(self):
        real = cap.build_record
        calls = []

        def first_fails(c, **kw):
            calls.append(kw.get("withheld"))
            if len(calls) == 1:
                raise RuntimeError("a normalizer or scrub bug")
            return real(c, **kw)

        with mock.patch.object(cap, "build_record", first_fails):
            got = cap.capture_call_guarded(call(), self.home, env={})
        self.assertTrue(got.withheld)
        [e] = self.entries()
        self.assertEqual(e["withheld"]["rule"], "error")

    def test_a_full_entry_already_written_is_not_replaced_by_the_stub(self):
        cap.capture_call(call(), self.home, env={})

        def late(*a, **k):
            raise cap.BudgetExceeded()

        with mock.patch.object(cap, "capture_call", late):
            got = cap.capture_call_guarded(call(), self.home, env={})
        self.assertFalse(got.withheld)
        [e] = self.entries()
        self.assertNotIn("withheld", e)
        self.assertEqual(e["result"], {"structured": {"ok": True}})

    def test_budget_firing_after_the_write_keeps_the_entry(self):
        def gc_slow(*a, **k):
            raise cap.BudgetExceeded()

        with mock.patch.object(evidence, "gc", gc_slow):
            got = cap.capture_call_guarded(call(), self.home, env={})
        self.assertFalse(got.withheld)
        [e] = self.entries()
        self.assertNotIn("withheld", e)

    def test_opt_out_and_cardinal_still_skip_on_the_fallback_path(self):
        def boom(*a, **k):
            raise RuntimeError("x")

        with mock.patch.object(gate, "check", boom):
            self.assertIsNone(cap.capture_call_guarded(call(), self.home, env={"CARDINAL_EVIDENCE_CAPTURE": "0"}))
            c = call(source={"kind": "cardinal", "server": "cardinal", "runtime": "claude-code"})
            self.assertIsNone(cap.capture_call_guarded(c, self.home, env={}))
        self.assertEqual(self.entries(), [])

    def test_a_huge_structured_result_is_scrubbed_only_as_far_as_it_can_be_kept(self):
        # Any tool can return a huge JSON value (a pod list, a table dump).
        # Only the part that fits the stored prefix may reach the scrub, or
        # the hook blows its budget and the call becomes a stub.
        rows = [{"name": f"r{i}", "env": [{"name": "DB_PASSWORD", "value": "hunter2"}], "pad": "x" * 200}
                for i in range(40000)]
        seen = []
        real = evidence.scrub_and_cap

        def spy(v, max_bytes):
            seen.append(len(evidence.encode_json(v)))
            return real(v, max_bytes)

        with mock.patch.object(evidence, "scrub_and_cap", spy):
            got = cap.capture_call(call(response={"items": rows}), self.home, env={})
        self.assertFalse(got.withheld)
        self.assertTrue(got.entry["truncated"])
        self.assertGreater(got.entry["result"]["original_bytes"], 10 << 20)
        self.assertNotIn("hunter2", json.dumps(got.entry))
        self.assertLessEqual(max(seen), 2 * evidence.MAX_RESULT_BYTES)

    def test_unreadable_record_rules(self):
        head = b'{"session_id":"s1","tool_use_id":"toolu_9","hook_event_name":"PostToolUseFailure","tool_name":"X"'
        e = cap.unreadable_record("claude-code", "claude-code/1", head, rule="depth", hint="nested too deep to read")
        self.assertEqual(e["withheld"], {"reason": "unreadable", "rule": "depth", "hint": "nested too deep to read"})
        self.assertEqual(cap.head_field(head, "hook_event_name"), "PostToolUseFailure")
        self.assertIsNone(cap.unreadable_record("claude-code", "c", b"{}"))


if __name__ == "__main__":
    unittest.main()

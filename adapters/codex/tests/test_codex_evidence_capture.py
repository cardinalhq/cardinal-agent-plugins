"""Generic evidence capture in the Codex adapter (`--event ToolEvidence`,
registered by cardinal-connect as a PostToolUse group with matcher ".*").

Every tool call Codex reports to PostToolUse (Bash with its string
tool_response, apply_patch, MCP tools named mcp__<server>__<tool>, any tool)
goes through the generic pipeline (cardinal_core.evidence_capture) into the
shared spool ~/.cardinal/evidence/<session>/ev_<12 hex>.json, and its id is
returned as hookSpecificOutput.additionalContext. Cardinal's own `cardinal`
server is skipped.

Verified host behaviour (README "Host-surface evidence"): PostToolUse fires
for Bash with tool_response = the output string. Whether Codex fires
PostToolUse for MCP tools and apply_patch is not verified; the payloads below
for those follow the same documented PostToolUseRequest shape.

Run: cd adapters/codex && python3 -m unittest tests.test_codex_evidence_capture -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER = TESTS_DIR.parent
REPO_ROOT = ADAPTER.parent.parent
HOOK = ADAPTER / "hooks" / "cardinal-codex-telemetry.py"
SESSION = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
LINE_RE = re.compile(r"^\[evidence:(ev_[0-9a-f]{12})")

if not (ADAPTER / "hooks" / "cardinal_core" / "evidence_capture.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "codex"],
                   check=True, capture_output=True)


class CodexEvidenceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = self.home / ".cardinal" / "evidence"

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, **over):
        payload = {"session_id": SESSION, "turn_id": "turn-1", "cwd": str(self.home / "repo"),
                   "hook_event_name": "PostToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "echo three"}, "tool_response": "three",
                   "tool_use_id": "call_1", "permission_mode": "default"}
        payload.update(over)
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        res = subprocess.run([sys.executable, str(HOOK), "--event", "ToolEvidence"], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=30, env=env, cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        return res

    def entry(self, res):
        out = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        m = LINE_RE.match(out["additionalContext"])
        self.assertIsNotNone(m, out)
        return json.loads((self.root / SESSION / f"{m.group(1)}.json").read_text()), out["additionalContext"]

    def test_bash_string_response_is_captured(self):
        e, ctx = self.entry(self.run_hook())
        self.assertIn("scripts/cardinal-evidence promote ev_", ctx)
        self.assertEqual(e["source"], {"kind": "builtin", "runtime": "codex"})
        self.assertEqual(e["server"], "builtin:codex")
        self.assertEqual(e["tool"], "Bash")
        self.assertEqual(e["args"], {"command": "echo three"})
        self.assertEqual(e["result"], {"text": ["three"]})
        self.assertEqual(e["tool_use_id"], "call_1")

    def test_apply_patch_mcp_and_unknown_tools_are_captured(self):
        e, _ = self.entry(self.run_hook(tool_name="apply_patch", tool_use_id="c2",
                                        tool_input={"input": "*** Begin Patch\n*** Update File: a.py\n+x=2\n"},
                                        tool_response="Success. Updated the following files:\nM a.py"))
        self.assertEqual(e["tool"], "apply_patch")
        e, _ = self.entry(self.run_hook(tool_name="mcp__grafana__query", tool_use_id="c3",
                                        tool_input={"expr": "up"}, tool_response={"content": [
                                            {"type": "text", "text": "series: 3"}]}))
        self.assertEqual(e["source"], {"kind": "mcp", "server": "grafana", "runtime": "codex"})
        self.assertEqual(e["result"], {"text": ["series: 3"]})
        e, _ = self.entry(self.run_hook(tool_name="future_tool", tool_use_id="c4", tool_input=[1, 2],
                                        tool_response={"ok": True}))
        self.assertEqual(e["normalizer"], "generic")

    def test_cardinal_is_skipped_and_secrets_withheld(self):
        res = self.run_hook(tool_name="mcp__cardinal__lakerunner__list_services", tool_use_id="c5")
        self.assertEqual(res.stdout, "")
        res = self.run_hook(tool_use_id="c6", tool_input={"command": "cat ~/.aws/credentials"},
                            tool_response="aws_secret_access_key = SECRETzz9")
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertRegex(ctx, r"withheld: sensitive path")
        self.assertNotIn("SECRETzz9", "".join(p.read_text() for p in self.root.rglob("ev_*.json")))

    def test_opt_out_and_garbage(self):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "CARDINAL_EVIDENCE_CAPTURE": "0"}
        res = subprocess.run([sys.executable, str(HOOK), "--event", "ToolEvidence"], input="{}",
                             capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(res.stdout, "")
        for raw in ("", "nope", "[1]"):
            res = subprocess.run([sys.executable, str(HOOK), "--event", "ToolEvidence"], input=raw,
                                 capture_output=True, text=True, timeout=30,
                                 env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
            self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_decision_handler_is_untouched(self):
        # The "Bash" PostToolUse group (decisions) does not capture evidence.
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        payload = {"session_id": SESSION, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "ls"}, "tool_response": "a"}
        res = subprocess.run([sys.executable, str(HOOK), "--event", "PostToolUse"], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(res.stdout, "")
        self.assertFalse(self.root.exists() and any(self.root.rglob("ev_*.json")))


if __name__ == "__main__":
    unittest.main()

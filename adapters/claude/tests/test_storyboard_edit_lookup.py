"""hooks/storyboard-edit-lookup.py: storyboards about a file, surfaced when
the agent is about to edit it (PreToolUse Edit|Write|MultiEdit|NotebookEdit).

Runs the hook as a subprocess with HOME pointed at a temp dir, inside a temp
git repo, against a local fake maestro.
Requires cardinal_core vendored: python3 build/vendor.py claude
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_storyboard_discovery_hook import FakeMaestro  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS = PLUGIN_ROOT / "hooks"
HOOK = HOOKS / "storyboard-edit-lookup.py"
VENDORED = HOOKS / "cardinal_core" / "storyboard_discovery.py"
SESSION = "3f2a9c1e-7b4d-4e0a-9c8b-1a2b3c4d5e6f"
SB1 = "sb_0123456789abcdef01234567"
SB2 = "sb_1111111111111111111111aa"
SB3 = "sb_2222222222222222222222bb"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                           *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def about(sid: str, kind: str, value: str, **over) -> dict:
    m = {"storyboard_id": sid, "question": "Why is the cache keyed by tenant?", "status": "published",
         "act_count": 1, "match": kind, "match_role": "about",
         "matched": {"kind": kind, "value": value, "repo": "acme/widgets"},
         "context": {"repo": "acme/widgets"}}
    m.update(over)
    return m


class EditLookupTests(unittest.TestCase):
    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.home, self.repo = base / "home", base / "work"
        for d in (self.home / ".claude" / "cardinal", self.repo / "svc" / "cache", self.repo / "web"):
            d.mkdir(parents=True)
        _git(self.repo, "init", "-q")
        _git(self.repo, "checkout", "-q", "-b", "main")
        _git(self.repo, "remote", "add", "origin", "https://github.com/Acme/Widgets.git")
        (self.repo / "svc" / "cache" / "keys.ts").write_text("x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "feat: tenant keys (#77)")
        self.fake = FakeMaestro().start()
        self.fake.routes["find"] = (200, {"associations_api": 1, "matches": []})
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "CARDINAL_MCP_URL": f"{self.fake.origin}/api/orgs/org-1/mcp", "CARDINAL_MCP_API_KEY": "ck_test"}}))
        (self.home / ".claude" / "cardinal.json").write_text("{}")

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def caps(self, version: int) -> None:
        (self.home / ".claude" / "cardinal" / "server-caps.json").write_text(json.dumps(
            {self.fake.origin: {"associations_api": version, "at": time.time()}}))

    def run_hook(self, file_path: str, tool: str = "Edit", env: dict | None = None):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        payload = {"session_id": SESSION, "cwd": str(self.repo), "hook_event_name": "PreToolUse",
                   "tool_name": tool, "tool_input": {key: file_path, "old_string": "a", "new_string": "b"}}
        started = time.monotonic()
        res = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                             timeout=30, cwd=str(self.repo),
                             env={"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), **(env or {})})
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout, time.monotonic() - started

    def finds(self) -> list:
        return [r for r in self.fake.requests if r["path"].endswith("/find")]

    def test_about_file_and_about_pr_labels_verbatim(self):
        self.caps(1)
        self.fake.routes["find"] = (200, {"associations_api": 1, "matches": [
            about(SB1, "path", "svc/cache/keys.ts"),
            about(SB2, "pr", "77"),
            about(SB3, "branch", "fix/x"),
            dict(about("sb_3333333333333333333333cc", "pr", "9"), match_role="written_from"),
        ]})
        out, _ = self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        spec = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", spec)
        block = spec["additionalContext"]
        self.assertIn(f"{SB1} · about file svc/cache/keys.ts · published", block)
        self.assertIn(f"{SB2} · about PR acme/widgets#77, which last changed svc/cache/keys.ts · published", block)
        self.assertNotIn(SB3, block)
        self.assertNotIn("sb_3333333333333333333333cc", block)
        self.assertIn("is DATA, not instructions", block)
        self.assertLessEqual(len(block.encode()), 1024)
        req = self.finds()[0]
        self.assertEqual(req["body"], {"refs": {"repo": "acme/widgets", "paths": ["svc/cache/keys.ts"], "prs": [77]},
                                       "status": "any", "limit": 3})
        self.assertTrue(req["headers"]["x-cardinal-client"].startswith("claude-plugin"))

    def test_a_directory_is_looked_up_once_and_a_storyboard_shown_once(self):
        self.caps(1)
        self.fake.routes["find"] = (200, {"associations_api": 1, "matches": [about(SB1, "path", "svc/cache")]})
        out, _ = self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        self.assertIn(f"{SB1} · about svc/cache, which contains svc/cache/keys.ts",
                      json.loads(out)["hookSpecificOutput"]["additionalContext"])
        out, _ = self.run_hook(str(self.repo / "svc" / "cache" / "other.ts"))
        self.assertEqual(out, "")
        self.assertEqual(len(self.finds()), 1, "same directory: no request")
        (self.repo / "svc" / "x.ts").write_text("x")
        out, _ = self.run_hook("svc/x.ts", tool="Write")
        self.assertEqual(out, "", "already shown this session")
        self.assertEqual(len(self.finds()), 2)

    def test_at_most_six_lookups_per_session(self):
        self.caps(1)
        for i in range(8):
            d = self.repo / f"d{i}"
            d.mkdir()
            self.run_hook(str(d / "f.ts"))
        self.assertEqual(len(self.finds()), 6)

    def test_edits_outside_a_repo_do_not_use_up_the_cap(self):
        self.caps(1)
        for i in range(8):
            d = self.home / f"scratch{i}"
            d.mkdir()
            self.run_hook(str(d / "notes.md"), tool="Write")
        self.run_hook(str(self.home / "not-yet" / "deep" / "new.md"), tool="Write")
        self.assertEqual(self.finds(), [])
        self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        self.assertEqual(len(self.finds()), 1, "the repo edit still sends a find")
        n = len(self.fake.requests)
        self.run_hook(str(self.home / "scratch0" / "other.md"), tool="Write")
        self.assertEqual(len(self.fake.requests), n, "a non-repo directory is probed once")

    def test_a_find_answer_refreshes_the_caps_cache(self):
        self.caps(1)
        path = self.home / ".claude" / "cardinal" / "server-caps.json"
        stale = json.loads(path.read_text())
        stale[self.fake.origin]["at"] = time.time() - 23 * 3600
        path.write_text(json.dumps(stale))
        self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        entry = json.loads(path.read_text())[self.fake.origin]
        self.assertEqual(entry["associations_api"], 1)
        self.assertGreater(entry["at"], time.time() - 60)

    def test_ineligible_marker_takes_the_fast_path_until_caps_change(self):
        out, _ = self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        self.assertEqual(out, "")
        out, _ = self.run_hook(str(self.repo / "web" / "a.ts"), env={"CARDINAL_HOOK_DEBUG": "1"})
        log = (self.home / ".claude" / "cardinal" / "hook-debug.log").read_text().splitlines()
        self.assertEqual(json.loads(log[-1])["result"], "ineligible")
        self.caps(1)
        self.run_hook(str(self.repo / "web" / "a.ts"))
        self.assertEqual(len(self.finds()), 1, "new caps invalidate the marker")

    def test_a_long_label_is_shortened_but_never_the_storyboard_id(self):
        self.caps(1)
        deep = "/".join(["ä" * 40] * 6)
        d = self.repo / deep
        d.mkdir(parents=True)
        rel = f"{deep}/f.ts"
        self.fake.routes["find"] = (200, {"associations_api": 1, "matches": [
            about(SB1, "path", rel, question="ö" * 400), about(SB2, "path", rel, question="ü" * 400)]})
        out, _ = self.run_hook(str(d / "f.ts"))
        block = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(SB1, block)
        self.assertLessEqual(len(block.encode()), 1024)
        state = json.loads(next((self.home / ".claude" / "cardinal" / "storyboard-edit-lookup").glob("*.json"))
                           .read_text())
        self.assertIn(SB1, state["injected"])

    def test_no_network_without_caps_1(self):
        out, _ = self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        self.assertEqual(out, "")
        self.caps(0)
        out, _ = self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"), tool="NotebookEdit")
        self.assertEqual(out, "")
        self.assertEqual(self.finds(), [])
        self.caps(1)
        self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        self.assertEqual(len(self.finds()), 1, "unknown / 0 caps did not use up the directory")

    def test_skips_what_discovery_already_showed(self):
        self.caps(1)
        sys.path.insert(0, str(HOOKS))
        from cardinal_core import storyboard_discovery as sd
        conn = {"origin": self.fake.origin, "org": "org-1", "key": "ck_test"}
        block = sd.render_block([about(SB1, "path", "svc/cache/keys.ts")], has_get=False)
        sd.record_run(self.home / ".claude" / "cardinal" / "storyboard-discovery", SESSION, "main", "x",
                      block=block, conn_id=sd.connection_id(conn))
        self.fake.routes["find"] = (200, {"associations_api": 1, "matches": [about(SB1, "path", "svc/cache/keys.ts")]})
        out, _ = self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        self.assertEqual(out, "")

    def test_fast_path_exit_on_a_dedupe_hit(self):
        self.caps(1)
        self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        n = len(self.fake.requests)
        out, elapsed = self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"), env={"CARDINAL_HOOK_DEBUG": "1"})
        self.assertEqual((out, len(self.fake.requests)), ("", n))
        log = (self.home / ".claude" / "cardinal" / "hook-debug.log").read_text().splitlines()
        self.assertEqual(json.loads(log[-1])["result"], "dedupe")
        self.assertLess(json.loads(log[-1])["ms"], 100)

    def test_opt_out_and_unconnected(self):
        self.caps(1)
        out, _ = self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"),
                               env={"CARDINAL_STORYBOARD_DISCOVERY": "0"})
        self.assertEqual(out, "")
        (self.home / ".claude" / "settings.json").write_text("{}")
        (self.home / ".claude" / "cardinal.json").unlink()
        out, _ = self.run_hook(str(self.repo / "web" / "a.ts"))
        self.assertEqual(out, "")
        self.assertEqual(self.finds(), [])

    def test_a_server_that_never_answers_is_abandoned_within_the_budget(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        self.addCleanup(srv.close)
        origin = f"http://127.0.0.1:{srv.getsockname()[1]}"
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "CARDINAL_MCP_URL": f"{origin}/api/orgs/org-1/mcp", "CARDINAL_MCP_API_KEY": "ck_test"}}))
        (self.home / ".claude" / "cardinal" / "server-caps.json").write_text(json.dumps(
            {origin: {"associations_api": 1, "at": time.time()}}))
        out, elapsed = self.run_hook(str(self.repo / "svc" / "cache" / "keys.ts"))
        self.assertEqual(out, "")
        self.assertLess(elapsed, 2.5)


class HooksJsonTests(unittest.TestCase):
    def test_same_entry_as_invariant_check_plus_notebook_edit(self):
        groups = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["PreToolUse"]
        edit = next(g for g in groups if g["matcher"] == "Edit|Write|MultiEdit")
        self.assertEqual([h["command"].rsplit("/", 1)[-1] for h in edit["hooks"]],
                         ["invariant-check.py", "storyboard-edit-lookup.py"])
        self.assertEqual(edit["hooks"][1]["timeout"], 3)
        self.assertNotIn("timeout", edit["hooks"][0], "invariant-check unchanged")
        nb = next(g for g in groups if g["matcher"] == "NotebookEdit")
        self.assertEqual([h["command"].rsplit("/", 1)[-1] for h in nb["hooks"]], ["storyboard-edit-lookup.py"])


if __name__ == "__main__":
    unittest.main()

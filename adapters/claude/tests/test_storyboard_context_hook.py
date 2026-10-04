"""hooks/storyboard-context.py: the PreToolUse hook that stamps session_id
and the checkout context on Cardinal's storyboard tools (updatedInput).

Runs the hook as a subprocess with HOME pointed at a temp dir, inside a temp
git repo, with a stub `gh` first on PATH. No network: the hook only reads the
server capability cache that storyboard discovery writes.
Requires cardinal_core vendored: python3 build/vendor.py claude
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS = PLUGIN_ROOT / "hooks"
HOOK = HOOKS / "storyboard-context.py"
VENDORED = HOOKS / "cardinal_core" / "storyboard_files.py"
PLUGIN_VERSION = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
SESSION = "3f2a9c1e-7b4d-4e0a-9c8b-1a2b3c4d5e6f"
ORIGIN = "https://app.example.com"
TOOL = "mcp__plugin_cardinal_cardinal__storyboard__"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                           *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class ContextHookTests(unittest.TestCase):
    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.base = base
        self.home, self.repo, stub = base / "home", base / "work", base / "stub"
        for d in (self.home / ".claude" / "cardinal", self.repo / "svc", stub):
            d.mkdir(parents=True)
        self.gh_log = base / "gh.log"
        gh = stub / "gh"
        gh.write_text(f'#!/bin/sh\necho ran >> "{self.gh_log}"\n'
                      'echo \'{"number": 42, "url": "https://github.com/acme/widgets/pull/42"}\'\n')
        gh.chmod(0o755)
        self.path = f"{stub}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
        _git(self.repo, "init", "-q")
        _git(self.repo, "checkout", "-q", "-b", "fix/checkout")
        _git(self.repo, "remote", "add", "origin", "https://github.com/Acme/Widgets.git")
        (self.repo / "svc" / "x.txt").write_text("x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "feat: checkout cache (#12)")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "CARDINAL_MCP_URL": f"{ORIGIN}/api/orgs/org-1/mcp", "CARDINAL_MCP_API_KEY": "ck_test",
            "OTEL_RESOURCE_ATTRIBUTES": "user.email=Dev@Example.com"}}))

    def tearDown(self):
        self.tmp.cleanup()

    def caps(self, version: int) -> None:
        (self.home / ".claude" / "cardinal" / "server-caps.json").write_text(json.dumps(
            {ORIGIN: {"associations_api": version, "at": time.time()}}))

    def run_hook(self, tool: str, tool_input, env: dict | None = None, hook: Path = HOOK, **over):
        payload = {"session_id": SESSION, "cwd": str(self.repo), "hook_event_name": "PreToolUse",
                   "tool_name": TOOL + tool, "tool_input": tool_input, "tool_use_id": "toolu_1", **over}
        res = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), capture_output=True, text=True,
                             timeout=30, cwd=str(self.repo),
                             env={"HOME": str(self.home), "PATH": self.path, **(env or {})})
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout

    def updated(self, *args, **kw) -> dict:
        out = json.loads(self.run_hook(*args, **kw))
        self.assertEqual(list(out), ["hookSpecificOutput"])
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", spec)
        return spec["updatedInput"]

    def test_create_keeps_every_key_and_stamps_session_and_context(self):
        original = {"question": "Why?", "title": "t", "about": {"prs": [7]}, "extra": [1, {"a": None}]}
        got = self.updated("create", original)
        for key, value in original.items():
            self.assertEqual(got[key], value, key)
        self.assertEqual(got["session_id"], SESSION)
        ctx = got["context"]
        self.assertEqual(ctx["repo"], "acme/widgets")
        self.assertEqual(ctx["branch"], "fix/checkout")
        self.assertEqual(ctx["head_sha"], self.head)
        self.assertEqual((ctx["pr_number"], ctx["pr_url"]), (42, "https://github.com/acme/widgets/pull/42"))
        self.assertEqual(ctx["client"], f"claude-code/{PLUGIN_VERSION}")
        self.assertEqual(ctx["actor_email"], "dev@example.com")
        self.assertEqual(got["about"], {"prs": [7]}, "about is the model's, never written")

    def test_the_model_context_and_session_are_untouched(self):
        got = self.updated("add_act", {"storyboard_id": "sb_x", "context": {"repo": "other/repo"}})
        self.assertEqual(got["context"], {"repo": "other/repo"})
        self.assertEqual(got["session_id"], SESSION)
        self.assertEqual(self.run_hook("create", {"session_id": "mine", "context": {"repo": "other/repo"}}), "")

    def test_empty_context_is_filled_and_paths_come_from_edited_files(self):
        files = self.home / ".claude" / "cardinal" / "storyboard-files"
        files.mkdir()
        (files / f"{SESSION}.json").write_text(json.dumps({"files": [{"repo": "acme/widgets", "path": "svc/x.txt"}]}))
        got = self.updated("create", {"question": "q", "context": {}})
        self.assertEqual(got["context"]["paths"], ["svc/x.txt"])

    def test_publish_is_stamped_only_at_caps_1(self):
        self.assertEqual(self.run_hook("publish", {"storyboard_id": "sb_x"}), "", "unknown caps")
        self.caps(0)
        self.assertEqual(self.run_hook("publish", {"storyboard_id": "sb_x"}), "")
        self.caps(1)
        got = self.updated("publish", {"storyboard_id": "sb_x", "public_links": "keep"})
        self.assertEqual((got["storyboard_id"], got["public_links"]), ("sb_x", "keep"))
        self.assertEqual(got["context"]["pr_number"], 42)
        self.assertNotIn("session_id", got)

    def test_find_uses_the_gh_cache_only(self):
        got = self.updated("find", {"query": "checkout"})
        self.assertNotIn("pr_number", got["context"])
        self.assertFalse(self.gh_log.exists())

    def test_find_commits_add_their_prs_at_caps_1(self):
        refs = {"commits": [self.head[:10]], "prs": [3]}
        self.assertEqual(self.run_hook("find", {"refs": refs, "context": {"repo": "acme/widgets"}, "session_id": "s"}),
                         "", "no PR lookup without caps")
        self.caps(1)
        got = self.updated("find", {"refs": refs, "context": {"repo": "acme/widgets"}, "session_id": "s"})
        self.assertEqual(got["refs"], {"commits": [self.head[:10]], "prs": [3, 12]})
        self.assertEqual(got["context"], {"repo": "acme/widgets"})

    def test_link_and_other_tools_are_untouched(self):
        self.assertEqual(self.run_hook("link", {"storyboard_id": "sb_x", "add": {"prs": [1]}}), "")
        self.assertEqual(self.run_hook("get", {"storyboard_id": "sb_x"}), "")

    def test_opt_out(self):
        self.assertEqual(self.run_hook("create", {"question": "q"}, env={"CARDINAL_STORYBOARD_CONTEXT": "0"}), "")

    def test_bad_input_and_exceptions_print_nothing(self):
        self.assertEqual(self.run_hook("create", ["not", "a", "dict"]), "")
        res = subprocess.run([sys.executable, str(HOOK)], input="{nope", capture_output=True, text=True,
                             env={"HOME": str(self.home), "PATH": self.path})
        self.assertEqual((res.returncode, res.stdout), (0, ""))
        # cardinal_core missing (an import error mid-stamp): the call runs unchanged.
        broken = self.base / "broken"
        broken.mkdir()
        for f in HOOKS.glob("_*.py"):
            shutil.copy(f, broken / f.name)
        shutil.copy(HOOK, broken / HOOK.name)
        self.assertEqual(self.run_hook("create", {"question": "q"}, hook=broken / HOOK.name), "")

    def test_agent_id_goes_to_the_debug_log_only(self):
        got = self.updated("create", {"question": "q"}, env={"CARDINAL_HOOK_DEBUG": "1"}, agent_id="a1b2c3")
        self.assertNotIn("agent_id", json.dumps(got))
        lines = (self.home / ".claude" / "cardinal" / "hook-debug.log").read_text().splitlines()
        entry = json.loads(lines[-1])
        self.assertEqual((entry["hook"], entry["agent_id_present"], entry["stamped"]),
                         ("storyboard-context", True, True))


class HooksJsonTests(unittest.TestCase):
    def test_registered_on_the_storyboard_tools(self):
        groups = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["PreToolUse"]
        mine = [g for g in groups if any(h["command"].endswith("/storyboard-context.py") for h in g["hooks"])]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["matcher"],
                         "mcp__(plugin_cardinal_)?cardinal__storyboard__(create|add_act|publish|find|link)")
        self.assertEqual(mine[0]["hooks"][0]["timeout"], 3)


if __name__ == "__main__":
    unittest.main()

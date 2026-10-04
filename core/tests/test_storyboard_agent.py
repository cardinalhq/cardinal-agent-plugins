"""Tests for cardinal_core.storyboard_agent (storyboard associations for the
Codex, Cursor and Gemini adapters).

Run from core/:  python3 -m unittest tests.test_storyboard_agent -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from cardinal_core import storyboard_agent as sa
from cardinal_core import storyboard_discovery
from cardinal_core.paths import AgentPaths

ORIGIN = "https://github.com/Acme/Widgets.git"
REPO = "acme/widgets"
SESSION = "01a104ae-262f-7e50-8a1e-b29c98cd95d5"

# Captured from codex-cli 0.142.5 (PostToolUse, apply_patch).
CODEX_ADD = {"command": "*** Begin Patch\n*** Add File: hello.txt\n+hi\n*** End Patch\n"}
CODEX_ADD_RESPONSE = ("Exit code: 0\nWall time: 0 seconds\nOutput:\n"
                      "Success. Updated the following files:\nA hello.txt\n")
CODEX_UPDATE = {"command": "*** Begin Patch\n*** Update File: hello.txt\n@@\n-hi\n+hello\n*** End Patch\n"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.home = self.dir / "home"
        self.agent = self.home / ".codex"
        self.agent.mkdir(parents=True)
        self.repo = self.dir / "repo"
        (self.repo / "pkg").mkdir(parents=True)
        _git(self.repo, "init", "-q")
        _git(self.repo, "checkout", "-q", "-b", "fix/eng-12-cache")
        _git(self.repo, "remote", "add", "origin", ORIGIN)
        (self.repo / "pkg" / "a.py").write_text("a\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        self.wiring = sa.Wiring("codex", AgentPaths(home=self.agent), "0.26.0", cli="/x/cardinal-storyboard",
                                environ=self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self, origin: str = "http://127.0.0.1:9"):
        (self.agent / "cardinal.json").write_text(json.dumps({"mcp_url": f"{origin}/api/orgs/org-1/mcp",
                                                               "user_email": "dev@example.com"}))
        (self.agent / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "ck_test"}))


class ApplyPatchTests(unittest.TestCase):
    def test_paths_from_every_header(self):
        patch = ("*** Begin Patch\n*** Add File: a/new.py\n+x\n*** Update File: b/old.py\n*** Move to: b/moved.py\n"
                 "@@\n-x\n+y\n*** Delete File: c/gone.py\n*** Update File: a/new.py\n*** End Patch\n")
        self.assertEqual(sa.apply_patch_paths({"command": patch}),
                         ["a/new.py", "b/old.py", "b/moved.py", "c/gone.py"])

    def test_captured_shapes(self):
        self.assertEqual(sa.apply_patch_paths(CODEX_ADD), ["hello.txt"])
        self.assertEqual(sa.apply_patch_paths(CODEX_UPDATE), ["hello.txt"])
        self.assertTrue(sa.apply_patch_succeeded(CODEX_ADD_RESPONSE))

    def test_other_spellings(self):
        patch = CODEX_ADD["command"]
        self.assertEqual(sa.apply_patch_paths(patch), ["hello.txt"])
        self.assertEqual(sa.apply_patch_paths({"patch": patch}), ["hello.txt"])
        self.assertEqual(sa.apply_patch_paths({"command": ["apply_patch", patch]}), ["hello.txt"])
        self.assertEqual(sa.apply_patch_paths({"command": "ls"}), [])
        self.assertEqual(sa.apply_patch_paths(None), [])

    def test_failure_is_not_success(self):
        self.assertFalse(sa.apply_patch_succeeded("Exit code: 1\nWall time: 0 seconds\nOutput:\nerror: no such file\n"))
        self.assertFalse(sa.apply_patch_succeeded(None))
        self.assertFalse(sa.apply_patch_succeeded({"unexpected": True}))
        self.assertTrue(sa.apply_patch_succeeded({"output": CODEX_ADD_RESPONSE}))


class RecordTests(_Case):
    def test_records_repo_relative_paths(self):
        n = sa.record_edits(self.wiring, SESSION, ["pkg/a.py", str(self.repo / "pkg" / "b.py"), "/etc/hosts"],
                            str(self.repo))
        self.assertEqual(n, 2)
        files = self.agent / "cardinal" / "storyboard-files"
        self.assertTrue(files.is_dir())
        from cardinal_core import storyboard_files
        self.assertEqual(storyboard_files.for_repo(files, SESSION, REPO), ["pkg/b.py", "pkg/a.py"])

    def test_no_session_records_nothing(self):
        self.assertEqual(sa.record_edits(self.wiring, None, ["pkg/a.py"], str(self.repo)), 0)
        self.assertEqual(sa.record_edits(self.wiring, "../bad", ["pkg/a.py"], str(self.repo)), 0)


class SessionLineTests(unittest.TestCase):
    def test_names_the_id_and_the_cli(self):
        line = sa.session_line(SESSION, "/p/scripts/cardinal-storyboard")
        self.assertTrue(line.startswith(f"Cardinal session id for this session: {SESSION}."))
        self.assertIn("pass it as session_id", line)
        self.assertIn(f'`python3 "/p/scripts/cardinal-storyboard" context --bare --session-id {SESSION}`', line)
        # --bare prints the object itself: what the model reads is what it passes
        # (the {"context": ...} wrapper would be an unknown context key -> 400).
        self.assertIn("pass as `context` the JSON object", line)
        self.assertIn("exactly as printed", line)
        self.assertIn("session_id is never a key inside context", line)

    def test_auto_context_wording(self):
        line = sa.session_line(SESSION, "/p/cs", auto_context=True)
        self.assertIn("the plugin fills session_id and context when you leave them out", line)
        self.assertIn(f'`python3 "/p/cs" context --bare --session-id {SESSION}`', line)
        self.assertIn("to pass yourself as `context`, exactly as printed", line)

    def test_never_suggests_session_id_inside_context(self):
        for auto in (False, True):
            line = sa.session_line(SESSION, "/p/cs", auto_context=auto)
            self.assertIn("pass it as session_id", line)
            self.assertNotIn("session_id inside context,", line)
            self.assertNotIn('"session_id"', line)
            self.assertNotIn("wrapper", line)


class RegisteredHookTests(unittest.TestCase):
    CODEX = {"PreToolUse": [{"matcher": "^mcp__cardinal__storyboard__(create)$", "hooks": [
        {"type": "command", "command": "python3 /h/.codex/cardinal/l.py --event StoryboardContext # cardinal-codex-plugin"}]}]}

    def test_finds_the_managed_handler(self):
        needle = "--event StoryboardContext # cardinal-codex-plugin"
        self.assertTrue(sa.registered_hook_group(self.CODEX, "PreToolUse", needle))
        self.assertFalse(sa.registered_hook_group(self.CODEX, "PostToolUse", needle))
        self.assertFalse(sa.registered_hook_group(self.CODEX, "PreToolUse", "--event Other"))

    def test_junk_is_false(self):
        for junk in (None, [], {"PreToolUse": "x"}, {"PreToolUse": [None, {"hooks": "x"}, {"hooks": [1]}]}):
            self.assertFalse(sa.registered_hook_group(junk, "PreToolUse", "--event"))

    def test_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hooks.json"
            self.assertFalse(sa.hooks_file_registers(path, "PreToolUse", "StoryboardContext"))
            path.write_text("{not json")
            self.assertFalse(sa.hooks_file_registers(path, "PreToolUse", "StoryboardContext"))
            path.write_text(json.dumps({"hooks": self.CODEX}))
            self.assertTrue(sa.hooks_file_registers(path, "PreToolUse", "StoryboardContext"))

    def test_invalid_id_is_no_line(self):
        self.assertIsNone(sa.session_line(None, "/p/cs"))
        self.assertIsNone(sa.session_line("a b", "/p/cs"))


class SessionStartTests(_Case):
    def test_not_connected_is_nothing(self):
        self.assertIsNone(sa.session_start_text(self.wiring, str(self.repo), SESSION))

    def test_line_and_block(self):
        self.connect()
        seen = {}

        def fake_discover(cwd, **kw):
            seen.update(kw, cwd=cwd)
            return "<cardinal-storyboards>\nBLOCK\n</cardinal-storyboards>"

        real = storyboard_discovery.discover
        storyboard_discovery.discover = fake_discover
        try:
            text = sa.session_start_text(self.wiring, str(self.repo), SESSION)
        finally:
            storyboard_discovery.discover = real
        line, block = text.split("\n\n", 1)
        self.assertTrue(line.startswith(f"Cardinal session id for this session: {SESSION}."))
        self.assertIn("BLOCK", block)
        self.assertEqual(seen["client"], "codex/0.26.0")
        self.assertEqual(seen["event"], "SessionStart")
        self.assertEqual(seen["state_dir"], self.agent / "cardinal" / "storyboard-discovery")
        self.assertEqual(seen["caps_path"], self.agent / "cardinal" / "server-caps.json")
        self.assertEqual(seen["conn"]["org"], "org-1")

    def test_discovery_opt_out_keeps_the_line(self):
        self.connect()
        self.env["CARDINAL_STORYBOARD_DISCOVERY"] = "0"
        text = sa.session_start_text(self.wiring, str(self.repo), SESSION)
        self.assertTrue(text.startswith("Cardinal session id for this session:"))
        self.assertNotIn("\n\n", text)

    def test_session_start_opt_out(self):
        self.connect()
        self.env["CARDINAL_STORYBOARD_SESSION_START"] = "off"
        self.assertIsNone(sa.session_start_text(self.wiring, str(self.repo), SESSION))

    def test_unreachable_server_is_bounded(self):
        self.connect()
        t0 = time.monotonic()
        text = sa.session_start_text(self.wiring, str(self.repo), SESSION, deadline=time.monotonic() + 0.5)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertTrue(text.startswith("Cardinal session id for this session:"))


class StampTests(_Case):
    def setUp(self):
        super().setUp()
        # A gh that fails at once: no network, no PR.
        stub = self.dir / "stub"
        stub.mkdir()
        (stub / "gh").write_text("#!/bin/sh\nexit 1\n")
        (stub / "gh").chmod(0o755)
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{stub}{os.pathsep}{saved}"
        self.addCleanup(os.environ.__setitem__, "PATH", saved)

    def test_create_gets_session_and_context(self):
        sa.record_edits(self.wiring, SESSION, ["pkg/a.py"], str(self.repo))
        out = sa.stamped_input(self.wiring, "create", {"title": "[ASSOC-TEST] x"}, SESSION, str(self.repo))
        self.assertEqual(out["title"], "[ASSOC-TEST] x")
        self.assertEqual(out["session_id"], SESSION)
        ctx = out["context"]
        self.assertEqual(ctx["repo"], REPO)
        self.assertEqual(ctx["branch"], "fix/eng-12-cache")
        self.assertEqual(ctx["client"], "codex/0.26.0")
        self.assertEqual(ctx["paths"], ["pkg/a.py"])
        self.assertEqual(len(ctx["head_sha"]), 40)

    def test_model_context_is_never_changed(self):
        self.assertIsNone(sa.stamped_input(self.wiring, "add_act", {"context": {"repo": "x/y"}, "session_id": "s"},
                                           SESSION, str(self.repo)))

    def test_publish_needs_caps(self):
        self.connect()
        self.assertIsNone(sa.stamped_input(self.wiring, "publish", {"storyboard_id": "sb_1"}, SESSION, str(self.repo)))
        storyboard_discovery.write_caps(self.wiring.caps_path, "http://127.0.0.1:9", 1)
        out = sa.stamped_input(self.wiring, "publish", {"storyboard_id": "sb_1"}, SESSION, str(self.repo))
        self.assertEqual(out["context"]["repo"], REPO)
        self.assertNotIn("session_id", out)

    def test_link_and_opt_out(self):
        self.assertIsNone(sa.stamped_input(self.wiring, "link", {"storyboard_id": "sb_1"}, SESSION, str(self.repo)))
        self.env["CARDINAL_STORYBOARD_CONTEXT"] = "0"
        self.assertIsNone(sa.stamped_input(self.wiring, "create", {"title": "t"}, SESSION, str(self.repo)))

    def test_find_adds_prs_of_commits(self):
        self.connect()
        storyboard_discovery.write_caps(self.wiring.caps_path, "http://127.0.0.1:9", 1)
        (self.repo / "pkg" / "c.py").write_text("c\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "fix the cache (#77)")
        sha = _git(self.repo, "rev-parse", "HEAD")
        out = sa.stamped_input(self.wiring, "find", {"refs": {"commits": [sha]}, "context": {"repo": REPO}},
                               SESSION, str(self.repo))
        self.assertEqual(out["refs"]["prs"], [77])
        self.assertEqual(out["context"], {"repo": REPO})


class CliTests(_Case):
    def run_cli(self, *argv):
        buf = io.StringIO()
        code = sa.cli_main(list(argv), self.wiring, agent="Codex", session_env=("CODEX_SESSION_ID",), out=buf)
        return code, buf.getvalue()

    def test_context_prints_repo_branch_head_and_paths(self):
        sa.record_edits(self.wiring, SESSION, ["pkg/a.py"], str(self.repo))
        code, out = self.run_cli("context", "--cwd", str(self.repo), "--session-id", SESSION)
        self.assertEqual(code, 0)
        ctx = json.loads(out)["context"]
        self.assertEqual((ctx["repo"], ctx["branch"], ctx["paths"]), (REPO, "fix/eng-12-cache", ["pkg/a.py"]))
        self.assertIn("head_sha", ctx)
        self.assertNotIn("actor_email", ctx)  # not connected: no user_email stored

    def test_context_bare_prints_the_object_itself(self):
        sa.record_edits(self.wiring, SESSION, ["pkg/a.py"], str(self.repo))
        _, wrapped = self.run_cli("context", "--cwd", str(self.repo), "--session-id", SESSION)
        code, out = self.run_cli("context", "--bare", "--cwd", str(self.repo), "--session-id", SESSION)
        self.assertEqual(code, 0)
        bare = json.loads(out)
        self.assertNotIn("context", bare)
        self.assertEqual(bare, json.loads(wrapped)["context"])
        self.assertEqual(bare["repo"], REPO)

    def test_context_session_from_env(self):
        sa.record_edits(self.wiring, SESSION, ["pkg/a.py"], str(self.repo))
        self.env["CODEX_SESSION_ID"] = SESSION
        _, out = self.run_cli("context", "--cwd", str(self.repo))
        self.assertEqual(json.loads(out)["context"]["paths"], ["pkg/a.py"])

    def test_discover_not_connected(self):
        self.assertEqual(self.run_cli("discover", "--cwd", str(self.repo), "--json"), (0, '{"block": null}\n'))


if __name__ == "__main__":
    unittest.main()

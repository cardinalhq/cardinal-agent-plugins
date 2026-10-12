"""Parent-workspace, multiple checkouts, backfill and upload parity."""
import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cardinal_core import investigation_bootstrap as boot, investigation_events as ie
from cardinal_core import session_repos as repos, storyboard_files
from cardinal_core.evidence_normalizers import ToolCall
from cardinal_core.paths import AgentPaths

from test_investigation_lifecycle import Base, SID


class SessionReposTests(Base):
    def setUp(self):
        super().setUp()
        boot.ensure(self.home, SID, self.fake.conn, "codex/1")

    def test_all_runtimes_replay_every_saved_repo_and_preserve_the_original_context(self):
        original = {"repo": "acme/original", "branch": "fix/one"}
        boot.ensure(self.home, SID, self.fake.conn, "codex/1", context=original)
        for runtime in ("claude", "codex", "cursor", "gemini"):
            with self.subTest(runtime=runtime):
                files = AgentPaths(home=self.home / f".{runtime}").runtime_dir / "storyboard-files"
                files.mkdir(parents=True)
                (files / f"{SID}.json").write_text(json.dumps({"files": [
                    {"repo": f"acme/{runtime}", "path": "x.py"},
                    {"repo": "acme/shared", "path": "y.py"}]}))
                repos.replay(self.home, SID, runtime, f"{runtime}/1", env={})
        contexts = repos.pending(self.home, SID, self.fake.conn["org"])
        self.assertEqual({c["repo"] for c in contexts}, {"acme/shared", "acme/claude", "acme/codex", "acme/cursor", "acme/gemini"})
        self.assertEqual(boot.session_context(self.home, SID, self.fake.conn), original)
        boot.refresh_capabilities(self.home, SID, self.fake.conn, "codex/1")
        sent = [r[1].get("context", {}).get("repo") for r in self.ensures()]
        for c in contexts:
            self.assertIn(c["repo"], sent)
        self.assertFalse(boot.wants_refresh(self.home, SID))

    def test_opt_out_non_author_org_change_cap_and_deduplication(self):
        self.assertFalse(repos.stage(self.home, SID, {"repo": "acme/off"}, runtime="codex", env={"CARDINAL_STORYBOARD_CONTEXT": "0"}))
        ie.update_fields(self.home, SID, {"is_author": False})
        self.assertFalse(repos.stage(self.home, SID, {"repo": "acme/joined"}, runtime="codex", env={}))
        ie.update_fields(self.home, SID, {"is_author": True})
        for i in range(repos.MAX_REPOS + 2):
            repos.stage(self.home, SID, {"repo": f"acme/repo-{i}"}, runtime="codex", env={})
        self.assertEqual(len(repos.pending(self.home, SID, self.fake.conn["org"])), repos.MAX_REPOS)
        self.assertFalse(repos.stage(self.home, SID, {"repo": "acme/repo-0"}, runtime="codex", env={}))
        self.assertEqual(repos.pending(self.home, SID, "another-org"), [])
        self.assertFalse(repos.stage(self.home, SID, {"repo": "/private/repo"}, runtime="codex", env={}))

    def test_successful_edits_in_two_repos_stage_labels_in_every_adapter(self):
        for runtime in ("claude", "codex", "cursor", "gemini"):
            for name in ("first", "second"):
                directory = self.home / name
                directory.mkdir(exist_ok=True)
                subprocess.run(["git", "init", "-q", str(directory)], check=True)
                subprocess.run(["git", "-C", str(directory), "remote", "remove", "origin"], capture_output=True)
                subprocess.run(["git", "-C", str(directory), "remote", "add", "origin", f"git@github.com:acme/{name}.git"], check=True)
                files = AgentPaths(home=self.home / f".{runtime}").runtime_dir / "storyboard-files"
                self.assertTrue(storyboard_files.record(files, SID, str(directory / "file.py")))
        self.assertEqual({c["repo"] for c in repos.pending(self.home, SID, self.fake.conn["org"])}, {"acme/first", "acme/second"})
        self.assertNotIn(str(self.home), repos.path(self.home, SID).read_text())

    def test_explicit_tool_workdir_is_observed_without_parsing_shell_commands(self):
        call = ToolCall(runtime="codex", tool_name="exec_command", tool="exec_command", source={},
                        session_id=SID, cwd=str(self.home), tool_input={"workdir": str(self.home / "checkout"), "cmd": "cd /another/repo"})
        with patch("cardinal_core.storyboard_context.collect", return_value={"repo": "acme/read-only"}) as collect:
            repos.observe(call, self.home, env={})
        self.assertEqual(collect.call_args_list[0].args[0], str(self.home / "checkout"))
        self.assertNotIn("/another/repo", str(collect.call_args_list))

    def test_edits_arriving_during_a_refresh_keep_the_marker(self):
        repos.stage(self.home, SID, {"repo": "acme/first"}, runtime="codex", env={})
        original = self.fake.answer
        def answer(tool, body):
            if body.get("context", {}).get("repo") == "acme/first":
                repos.stage(self.home, SID, {"repo": "acme/second"}, runtime="codex", env={})
            return original(tool, body)
        self.fake.answer = answer
        boot.refresh_capabilities(self.home, SID, self.fake.conn, "codex/1")
        self.assertTrue(boot.wants_refresh(self.home, SID))
        boot.refresh_capabilities(self.home, SID, self.fake.conn, "codex/1")
        self.assertFalse(boot.wants_refresh(self.home, SID))


if __name__ == "__main__":
    unittest.main()

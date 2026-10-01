"""The local-path scrub (evidence_capture._Local) and the summary clip.

Two leaks this pins shut, both seen in real receipts:

  - Claude Code's session temp dir encodes the cwd with every character
    outside [A-Za-z0-9] as "-" (/private/tmp/claude-501/-Users-alice-git-app/
    <session>/scratchpad/...). The literal cwd/home rules never matched the
    encoded form, so the username went up with every scratchpad path.
  - The summary was clipped to 120 characters BEFORE the scrub. A clip that
    landed inside a path (/Users/mgr…) left a fragment no path rule
    recognised. The normalizers now hand over a long summary and
    _clip_scrubbed makes the only clip, after the scrub.

Fixture: HOME=/Users/mgraff, cwd=/Users/mgraff/git/app.

Run with: cd core && python3 -m unittest tests.test_evidence_local_paths -v
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cardinal_core import evidence_capture as cap
from cardinal_core import evidence_normalizers as norms

HOME = "/Users/mgraff"
CWD = "/Users/mgraff/git/app"
UUID = "1b0e2c9a-0d3f-4e5b-9a7c-2f1e3d4c5b6a"
SCRATCH = f"/private/tmp/claude-501/-Users-mgraff-git-app/{UUID}/scratchpad/x"
OTHER_TMP = "/var/folders/ab/T/claude-501/-Users-mgraff-other/y"


def bash(command: str, stdout: str = "", cwd: str = CWD) -> norms.ToolCall:
    return norms.ToolCall(runtime="claude-code", tool_name="Bash", source=cap.builtin_source("claude-code"),
                          tool="Bash", tool_input={"command": command, "description": "run"},
                          response={"stdout": stdout, "stderr": ""}, session_id="s1", tool_use_id="toolu_r5",
                          cwd=cwd)


def record(call: norms.ToolCall, home: str = HOME) -> dict:
    with TemporaryDirectory() as tmp:
        # A real (empty) spool root for the opt-out checks; the scrub itself
        # uses the fixture HOME passed to build_record.
        got = cap.capture_call(call, Path(tmp), env={}, write=False)
    assert got is not None and not got.withheld, got
    return cap.build_record(call, home=home)


class SessionTmpAndEncodedPathTests(unittest.TestCase):
    def test_bash_call_keeps_no_username_in_args_result_or_summary(self):
        cmd = f"cp {SCRATCH} {OTHER_TMP}"
        e = record(bash(cmd, stdout=f"wrote {SCRATCH}\n{OTHER_TMP}\n"))
        dumped = json.dumps(e)
        self.assertNotIn("mgraff", dumped)
        self.assertNotIn("-Users-mgraff", dumped)
        self.assertEqual(
            e["args"]["command"],
            f"cp /private/tmp/claude-501/[session tmp]/{UUID}/scratchpad/x /var/folders/ab/T/claude-501/[session tmp]/y")
        self.assertEqual(
            e["result"]["structured"]["stdout"],
            f"wrote /private/tmp/claude-501/[session tmp]/{UUID}/scratchpad/x\n"
            "/var/folders/ab/T/claude-501/[session tmp]/y\n")
        self.assertTrue(e["summary"].startswith("cp /private/tmp/claude-501/[session tmp]/"), e["summary"])

    def test_session_tmp_shape_keeps_parent_uid_and_tail(self):
        local = cap._Local(None, None, None)
        self.assertEqual(local.apply(SCRATCH), f"/private/tmp/claude-501/[session tmp]/{UUID}/scratchpad/x")
        self.assertEqual(local.apply("/tmp/claude-0/-home-bob-src"), "/tmp/claude-0/[session tmp]")
        # Not the shape: no uid, or no leading "-" on the encoded segment.
        for s in ("/tmp/claude/-Users-mgraff", "/tmp/claude-501/Users-mgraff", "/tmp/xclaude-501/-a/b"):
            self.assertEqual(local.apply(s), s)

    def test_encoded_cwd_and_home_outside_the_temp_dir(self):
        local = cap._Local(None, CWD, HOME)
        self.assertEqual(local.apply("--project=-Users-mgraff-git-app/abc"), "--project=[cwd]/abc")
        self.assertEqual(local.apply("see -Users-mgraff-other/y"), "see [home]-other/y")
        self.assertEqual(local.apply('"-Users-mgraff"'), '"[home]"')
        self.assertEqual(local.apply("-Users-mgraff-git-app"), "[cwd]")
        self.assertEqual(local.apply("-Users-mgraff…"), "[home]…")
        # The literal rules still run after the encoded ones.
        self.assertEqual(local.apply("/Users/mgraff/git/app/src and /Users/mgraff/notes"), "./src and ~/notes")

    def test_boundary_guards_leave_look_alikes_alone(self):
        local = cap._Local(None, CWD, HOME)
        for s in ("-Users-mgraffiti-x", "x-Users-mgraff-y", "-Users-mgraff_x"):
            self.assertEqual(local.apply(s), s)
        root = cap._Local(None, None, "/root")  # "-root" is under the 6-character minimum
        for s in ("--root-dir", "re-root", "a -root b"):
            self.assertEqual(root.apply(s), s)
        self.assertEqual(root.apply("/root/x"), "~/x")

    def test_the_realpath_of_cwd_is_encoded_too(self):
        with TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-project-dir"
            real.mkdir()
            link = Path(tmp) / "link"
            os.symlink(real, link)
            enc_real = cap.encode_path(os.path.realpath(link))
            local = cap._Local(None, str(link), None)
            self.assertEqual(local.apply(f"x {enc_real}/y"), "x [cwd]/y")
            self.assertEqual(local.apply(f"x {cap.encode_path(str(link))}/y"), "x [cwd]/y")

    def test_encode_path(self):
        self.assertEqual(cap.encode_path("/Users/mgraff/git/app/"), "-Users-mgraff-git-app")
        self.assertEqual(cap.encode_path("/home/a.b_c"), "-home-a-b-c")


class SpillRootBeforeEncodedTests(unittest.TestCase):
    """The spill root (~/.claude/projects) holds the dash-encoded cwd. The
    spill rule must run first: rewriting the encoded segment to [cwd] first
    ended the spill match at its "]" and sent up "[local file]]/<session
    uuid>/tool-results/<file>"."""

    SPILL = f"{HOME}/.claude/projects"
    PERSISTED = f"{HOME}/.claude/projects/-Users-mgraff-git-app/{UUID}/tool-results/b57.txt"

    def test_local_rewrites_a_spill_path_whole(self):
        local = cap._Local(self.SPILL, CWD, HOME)
        self.assertEqual(local.apply(f"cat {self.PERSISTED}"), "cat [local file]")
        self.assertEqual(local.apply(f"ls {self.SPILL}/-Users-mgraff-other/x.jsonl"), "ls [local file]")
        # Outside the spill root the encoded rules still apply.
        self.assertEqual(local.apply("see -Users-mgraff-git-app/abc"), "see [cwd]/abc")

    def _spill_call(self, tool: str, tool_input: dict, response) -> dict:
        call = norms.ToolCall(runtime="claude-code", tool_name=tool, source=cap.builtin_source("claude-code"),
                              tool=tool, tool_input=tool_input, response=response, session_id="s1",
                              tool_use_id="toolu_spill", cwd=CWD, spill_root=self.SPILL)
        return cap.build_record(call, home=HOME)

    def _assert_clean(self, e: dict, exact: bool = True) -> None:
        dumped = json.dumps(e)
        for leak in (UUID, "tool-results", "b57.txt", "mgraff") + (("]]",) if exact else ()):
            self.assertNotIn(leak, dumped)

    def test_bash_args_summary_and_stdout_name_the_spill_file_as_local_file(self):
        e = self._spill_call("Bash", {"command": f"grep -c ERROR {self.PERSISTED}", "description": "run"},
                             {"stdout": f"see {self.PERSISTED} for details\n", "stderr": ""})
        self.assertEqual(e["args"]["command"], "grep -c ERROR [local file]")
        self.assertEqual(e["summary"], "grep -c ERROR [local file]")
        self.assertEqual(e["result"]["structured"]["stdout"], "see [local file] for details\n")
        self._assert_clean(e)

    def test_read_of_a_spill_file(self):
        e = self._spill_call("Read", {"file_path": self.PERSISTED}, "1\tERROR x\n")
        self.assertEqual(e["summary"], "[local file]")
        # Pre-existing (same on 41df576): _path_lines reads the bare path as
        # a grep context line and withholds it at the ~/.claude prefix
        # ("…/projects/-Users-[withheld]") before the spill rule runs, so the
        # stored arg is "[local file]]". Odd, but nothing under it leaks.
        self.assertIn(e["args"]["file_path"], ("[local file]", "[local file]]"))
        self._assert_clean(e, exact=False)

    def test_capture_call_stdout_naming_the_spill_file(self):
        call = norms.ToolCall(runtime="claude-code", tool_name="Bash", source=cap.builtin_source("claude-code"),
                              tool="Bash", tool_input={"command": "make test", "description": "run"},
                              response={"stdout": f"see {self.PERSISTED} for details", "stderr": ""},
                              session_id="s1", tool_use_id="toolu_spill2", cwd=CWD, spill_root=self.SPILL)
        e = record(call)
        self.assertEqual(e["result"]["structured"]["stdout"], "see [local file] for details")
        self._assert_clean(e)


class ScrubBeforeClipTests(unittest.TestCase):
    def _summary(self, command: str) -> str:
        s = record(bash(command))["summary"]
        self.assertLessEqual(len(s), norms.MAX_SUMMARY_CHARS)
        return s

    def test_clip_inside_the_home_path_leaves_no_username_fragment(self):
        # The path starts at column N; before this fix the 120-char clip left
        # "/Users/mgraff…" (N=106) and "/Users/mgra…" (N=108).
        for n in (106, 108):
            s = self._summary("x" * (n - 1) + " /Users/mgraff/git/other/file.txt")
            self.assertNotIn("mgra", s, n)
            self.assertTrue(s.endswith("…"), s)

    def test_clip_inside_the_encoded_path_leaves_no_username_fragment(self):
        for n in (106, 108):
            s = self._summary("x" * (n - 1) + " -Users-mgraff-other/y")
            self.assertNotIn("mgra", s, n)
            s = self._summary("x" * (n - 25) + f" {SCRATCH}")
            self.assertNotIn("mgra", s, n)

    def test_normalizer_summary_is_long_until_the_pipeline_clips_it(self):
        command = "echo " + "a" * 395
        self.assertEqual(len(command), 400)
        for call in (bash(command),
                     norms.ToolCall(runtime="claude-code", tool_name="mcp__x__y", source=cap.mcp_source("x", "claude-code"),
                                    tool="y", tool_input={"query": command}, response={"content": "ok"}),
                     norms.ToolCall(runtime="claude-code", tool_name="Write", source=cap.builtin_source("claude-code"),
                                    tool="Write", tool_input={"file_path": command},
                                    response={"type": "create", "filePath": "a", "structuredPatch": []}),
                     norms.ToolCall(runtime="claude-code", tool_name="Future", source=cap.builtin_source("claude-code"),
                                    tool="Future", tool_input={"zz": command}, response="ok")):
            got, _ = norms.normalize_call(call)
            self.assertGreater(len(got.summary), norms.MAX_SUMMARY_CHARS, call.tool)
        self.assertEqual(len(norms.input_summary("b" * 1000)), norms.MAX_SUMMARY_CHARS * 4)
        self.assertEqual(len(record(bash(command))["summary"]), norms.MAX_SUMMARY_CHARS)

    def test_a_cut_at_the_scrub_window_drops_the_partial_segment(self):
        # 21 cwd paths shrink to "." each, so the raw cut at 480 characters
        # (inside /Users/mgra…) would otherwise land well inside 120.
        local = cap._Local(None, CWD, HOME)
        s = "/Users/mgraff/git/app " * 21 + "zzzzzz " + "/Users/mgraff/x"
        self.assertGreater(len(s), 480)
        out = cap._clip_scrubbed(s, 120, local)
        self.assertNotIn("mgra", out)
        self.assertTrue(out.endswith("…"), out)
        # The same through an upstream clip that ends in "…" at the window.
        clipped = norms.one_line(s, 480)
        self.assertEqual(len(clipped), 480)
        self.assertNotIn("mgra", cap._clip_scrubbed(clipped, 120, local))
        # One long run with no separator in it is kept, then clipped.
        self.assertEqual(cap._clip_scrubbed("a" * 500, 120, local), "a" * 119 + "…")
        self.assertEqual(cap._clip_scrubbed("make check", 120, local), "make check")


if __name__ == "__main__":
    unittest.main()

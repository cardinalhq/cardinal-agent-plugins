"""cardinal_core.evidence_capture.capture_call: one pipeline for every tool.

The headline test is the unknown tool: a tool nobody registered anything for
is captured with zero code changes. The rest pin every real Claude Code
result shape sampled from transcripts (design §0), the pluggable-normalizer
contract (never a gate; failures fall back to generic), the wire mapping
(shared vectors with conductor) and the id scheme (shared vectors with the JS
bridge).

Run with: cd core && python3 -m unittest tests.test_evidence_capture_call -v
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cardinal_core import evidence
from cardinal_core import evidence_capture as cap
from cardinal_core import evidence_normalizers as norms
from cardinal_core import evidence_promote as promote

TESTDATA = Path(__file__).resolve().parent / "testdata"
SESSION = "3f2a9c1e-7b4d-4e0a-9c8b-1a2b3c4d5e6f"
CARDINAL = ("cardinal", "plugin_cardinal_cardinal")

# A Python port of conductor's EvidenceItemSchema (maestro
# storyboard/evidence-upload.ts) and the gateway's checkIdent.
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def schema_errors(item) -> list:
    errs = []
    if not isinstance(item, dict):
        return ["not an object"]
    allowed = {"provenance", "source_server", "client", "client_called_at", "tool", "args", "result"}
    if set(item) - allowed:
        errs.append(f"unknown keys {set(item) - allowed}")
    if item.get("provenance") not in ("captured", "reported"):
        errs.append("provenance")
    for k, n, req in (("source_server", 128, True), ("tool", 200, True), ("client", 64, False)):
        v = item.get(k)
        if v is None and not req:
            continue
        if not isinstance(v, str) or not v or len(v) > n or not cap.IDENT_RE.match(v):
            errs.append(k)
    if "client_called_at" in item and not (isinstance(item["client_called_at"], str)
                                           and _RFC3339.match(item["client_called_at"])):
        errs.append("client_called_at")
    if "args" in item and not isinstance(item["args"], dict):
        errs.append("args")
    r = item.get("result")
    if not isinstance(r, dict):
        errs.append("result")
    else:
        content = r.get("content")
        if content is not None:
            if not isinstance(content, list):
                errs.append("result.content")
            else:
                for b in content:
                    if not isinstance(b, dict) or not isinstance(b.get("type"), str) or not b["type"]:
                        errs.append("result.content block")
                    elif b["type"] == "text" and not isinstance(b.get("text"), str):
                        errs.append("text block without text")
        if "text" in r and not isinstance(r["text"], str):
            errs.append("result.text")
        if "is_error" in r and not isinstance(r["is_error"], bool):
            errs.append("result.is_error")
        if not (r.get("is_error") is True or content or r.get("structured_content") is not None or "text" in r):
            errs.append("result carries nothing")
    return errs


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = evidence.default_root(self.home)
        self.cwd = str(self.home / "repo")
        self.spill = str(self.home / ".claude" / "projects")

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, tool_name, tool_input=None, response=None, error=None, runtime="claude-code", tuid=None,
             source=None, **kw):
        src, tool = cap.classify_mcp_name(tool_name, runtime, CARDINAL)
        return cap.ToolCall(runtime=runtime, tool_name=tool_name, source=source or src, tool=tool,
                            tool_input=tool_input, response=response, error=error, session_id=SESSION,
                            tool_use_id=tuid or f"toolu_{abs(hash((tool_name, repr(tool_input)))) % 10**9}",
                            cwd=self.cwd, spill_root=self.spill, client=f"{runtime}/1.0.0", **kw)

    def capture(self, *a, **kw):
        got = cap.capture_call(self.call(*a, **kw), self.home, env={})
        self.assertIsNotNone(got)
        path = self.root / SESSION / f"{got.entry['evidence_id']}.json"
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text()), got.entry)
        return got.entry

    def wire(self, entry):
        item = promote.wire_item(entry, "claude-code/1.0.0")
        self.assertEqual(schema_errors(item), [], item)
        return item


class UnknownToolTests(_Case):
    def test_unknown_tool_is_captured_with_zero_code_changes(self):
        self.assertNotIn("FrobnicateWidgets_v9", json.dumps([n.id for n in norms.REGISTRY]))
        e = self.capture("FrobnicateWidgets_v9", {"x": [1, {"y": "z"}]}, {"weird": {"n": 1}})
        self.assertEqual(e["source"], {"kind": "builtin", "runtime": "claude-code"})
        self.assertEqual(e["normalizer"], "generic")
        self.assertEqual(e["args"], {"x": [1, {"y": "z"}]})
        self.assertEqual(e["result"], {"structured": {"weird": {"n": 1}}})
        item = self.wire(e)
        self.assertEqual(item["source_server"], "builtin:claude-code")
        self.assertEqual(item["tool"], "FrobnicateWidgets_v9")

    def test_unknown_mcp_tool_is_sanitized_for_the_wire(self):
        e = self.capture("mcp__my-srv__do thing!", {}, "ok")
        self.assertEqual(e["source"]["kind"], "mcp")
        item = self.wire(e)
        self.assertEqual(item["source_server"], "my-srv")
        self.assertEqual(item["tool"], "do thing_")

    def test_non_object_inputs(self):
        for i, (inp, want) in enumerate(((["a", 1], {"input": ["a", 1]}), ("raw", {"input": "raw"}), (7, {"input": 7}),
                                         (None, {}))):
            e = self.capture("Whatever", inp, "r", tuid=f"t_in{i}")
            self.assertEqual(self.wire(e).get("args"), want)

    def test_every_runtime_and_source_kind(self):
        for rt in ("claude-code", "cursor", "gemini", "codex", "pi", "opencode", "some-future-agent"):
            e = self.capture("Bash", {"command": "ls"}, "a", runtime=rt, tuid=f"t_{rt}")
            self.assertEqual(self.wire(e)["source_server"], f"builtin:{rt}")
        e = self.capture("bash", {"command": "ls"}, "a", runtime="opencode", tuid="t_tool",
                         source={"kind": "tool", "runtime": "opencode"})
        self.assertEqual(self.wire(e)["source_server"], "tool:opencode")


class ClaudeShapeTests(_Case):
    """Result shapes sampled from real Claude Code transcripts."""

    def test_bash(self):
        e = self.capture("Bash", {"command": "make check-maestro", "description": "check"},
                         {"stdout": "ok 12 passed", "stderr": "warn", "interrupted": False, "isImage": False,
                          "noOutputExpected": False, "returnCodeInterpretation": "No matches found"})
        self.assertEqual(e["normalizer"], "shell")
        self.assertEqual(e["summary"], "make check-maestro")
        self.assertEqual(e["result"]["structured"], {"stdout": "ok 12 passed", "stderr": "warn",
                                                     "return_code_interpretation": "No matches found"})
        self.assertNotIn("exit_code", e)

    def test_bash_persisted_output_is_followed_only_under_the_spill_root(self):
        inside = Path(self.spill) / "p" / SESSION / "tool-results" / "b1.txt"
        inside.parent.mkdir(parents=True)
        inside.write_text("FULL OUTPUT\n" * 10)
        e = self.capture("Bash", {"command": "big"}, {"stdout": "preview", "stderr": "",
                                                       "persistedOutputPath": str(inside),
                                                       "persistedOutputSize": 120})
        self.assertTrue(e["result"]["structured"]["stdout"].startswith("FULL OUTPUT"))
        self.assertTrue(e["spilled"])
        outside = self.home / "elsewhere.txt"
        outside.write_text("NOT-FOLLOWED")
        e = self.capture("Bash", {"command": "big2"}, {"stdout": "preview", "stderr": "",
                                                        "persistedOutputPath": str(outside)})
        self.assertEqual(e["result"]["structured"]["stdout"], "preview")

    def test_background_bash(self):
        e = self.capture("Bash", {"command": "sleep 100", "run_in_background": True},
                         {"stdout": "", "stderr": "", "interrupted": False, "backgroundTaskId": "bash_3"})
        self.assertEqual(e["result"]["structured"]["background_task_id"], "bash_3")

    def test_failed_bash_and_interrupt_shape(self):
        e = self.capture("Bash", {"command": "make test"}, None, error="Exit code 2\nFAIL x\npassword=hunter2")
        self.assertEqual(e["status"], "error")
        self.assertEqual(e["exit_code"], 2)
        self.assertNotIn("hunter2", json.dumps(e))
        self.assertIs(self.wire(e)["result"]["is_error"], True)

    def test_read_text_and_image(self):
        e = self.capture("Read", {"file_path": f"{self.cwd}/a.py"},
                         {"type": "text", "file": {"filePath": f"{self.cwd}/a.py", "content": "API_KEY=abc123\nx=1\n",
                                                   "numLines": 2, "startLine": 1, "totalLines": 2}})
        self.assertEqual(e["normalizer"], "generic")
        f = e["result"]["structured"]["file"]
        self.assertEqual(f["filePath"], "./a.py")
        self.assertEqual(f["content"], "API_KEY=[redacted]\nx=1\n")
        import base64
        blob = base64.b64encode(os.urandom(6000)).decode()
        e = self.capture("Read", {"file_path": f"{self.cwd}/shot.png"},
                         {"type": "image", "file": {"base64": blob, "type": "image/png", "originalSize": 6000}})
        self.assertEqual(e["result"]["structured"]["file"]["base64"], f"[binary omitted: {len(blob)} bytes]")

    def test_edit_keeps_the_patch_not_the_original_file(self):
        e = self.capture("Edit", {"file_path": f"{self.cwd}/a.py", "old_string": "x=1", "new_string": "x=2"},
                         {"filePath": f"{self.cwd}/a.py", "oldString": "x=1", "newString": "x=2",
                          "originalFile": "ORIGINAL-CONTENTS\nx=1\n", "replaceAll": False,
                          "structuredPatch": [{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
                                               "lines": ["-x=1", "+x=2"]}], "userModified": False})
        self.assertEqual(e["normalizer"], "file-edit")
        self.assertNotIn("ORIGINAL-CONTENTS", json.dumps(e))
        self.assertEqual(e["result"]["structured"]["patch"][0]["lines"], ["-x=1", "+x=2"])
        self.assertEqual(e["result"]["structured"]["replace_all"], False)

    def test_write_content_key_is_not_read_as_mcp_content(self):
        big = "line\n" * 20000
        e = self.capture("Write", {"file_path": f"{self.cwd}/big.txt", "content": "short"},
                         {"type": "create", "filePath": f"{self.cwd}/big.txt", "content": big,
                          "structuredPatch": [{"lines": ["+line"]}], "originalFile": None, "userModified": False})
        self.assertEqual(e["normalizer"], "file-edit")
        self.assertNotIn("content", e["result"]["structured"])
        e = self.capture("Write", {"file_path": f"{self.cwd}/s.txt", "content": "hi"},
                         {"type": "create", "filePath": f"{self.cwd}/s.txt", "content": "hi",
                          "structuredPatch": [], "originalFile": None})
        self.assertEqual(e["result"]["structured"]["content"], "hi")

    def test_search_web_agent_todo_notebook_ask_shapes(self):
        shapes = [
            ("Grep", {"pattern": "TODO", "output_mode": "content"},
             {"mode": "content", "numFiles": 1, "filenames": [], "content": "a.py:3:# TODO", "numLines": 1}),
            ("Glob", {"pattern": "**/*.py"}, {"filenames": [f"{self.cwd}/a.py"], "durationMs": 3, "numFiles": 1,
                                             "truncated": False}),
            ("WebFetch", {"url": "https://example.com", "prompt": "summarize"},
             {"bytes": 1256, "code": 200, "codeText": "OK", "result": "Example Domain", "durationMs": 90,
              "url": "https://example.com"}),
            ("WebSearch", {"query": "otel collector"}, {"query": "otel collector", "results": [
                {"tool_use_id": "srvtoolu_1", "content": [{"title": "OTel", "url": "https://opentelemetry.io"}]}],
                "durationSeconds": 1.2}),
            ("Agent", {"description": "explore", "prompt": "find x", "subagent_type": "Explore"},
             {"status": "completed", "content": [{"type": "text", "text": "found x in a.py"}], "totalTokens": 900,
              "totalToolUseCount": 3, "totalDurationMs": 4000}),
            ("Agent", {"description": "bg", "prompt": "long", "run_in_background": True},
             {"agentId": "a1", "status": "async_launched", "isAsync": True, "outputFile": f"{self.spill}/o.txt",
              "prompt": "long"}),
            ("TodoWrite", {"todos": [{"content": "x", "status": "pending"}]},
             {"oldTodos": [], "newTodos": [{"content": "x", "status": "pending"}]}),
            ("NotebookEdit", {"notebook_path": f"{self.cwd}/n.ipynb", "new_source": "print(1)"},
             {"new_source": "print(1)", "cell_type": "code", "language": "python", "edit_mode": "replace"}),
            ("AskUserQuestion", {"questions": [{"question": "Which?"}]},
             {"questions": [{"question": "Which?"}], "answers": {"Which?": "A"}}),
        ]
        for i, (name, inp, resp) in enumerate(shapes):
            with self.subTest(tool=name):
                e = self.capture(name, inp, resp, tuid=f"t_shape{i}")
                self.assertEqual(e["normalizer"], "generic", name)
                self.assertEqual(e["status"], "ok")
                self.wire(e)
        # A local path under the spill root never reaches the entry.
        self.assertNotIn(self.spill, json.dumps(e))

    def test_mcp_result_shapes(self):
        e = self.capture("mcp__grafana__q", {"expr": "up"}, {"content": [{"type": "text", "text": "a"},
                                                                         {"type": "image", "data": "x"}]})
        self.assertEqual(e["normalizer"], "mcp-content")
        self.assertEqual(e["result"], {"text": ["a"], "other_blocks": 1})
        e = self.capture("mcp__grafana__q2", {}, {"content": "x", "structuredContent": {"rows": [1]}, "isError": True})
        self.assertEqual(e["status"], "error")
        e = self.capture("mcp__s__t", {}, None, error="upstream 403 api_key=sk-abcdefghijklmnopqrstu")
        self.assertEqual(e["status"], "error")
        self.assertNotIn("sk-abcdefghijklmnopqrstu", json.dumps(e))


class ControlPlaneTests(_Case):
    """The investigation's control log is never evidence: a call that runs
    `cardinal-storyboard investigation …` is never captured (tool-neutral:
    any string of any tool's input)."""

    def test_control_log_calls_are_not_captured(self):
        for cmd in ("cardinal-storyboard investigation ack inv_x 3 --disposition noted",
                    "/home/u/.claude/plugins/cache/cardinal/bin/cardinal-storyboard investigation events inv_x",
                    "cd /tmp && cardinal-storyboard investigation link --json",
                    "python3 \"$P/bin/cardinal-storyboard\" investigation question 'why?'",
                    "x=$(cardinal-storyboard investigation link)"):
            self.assertTrue(cap.control_plane({"command": cmd}), cmd)
            self.assertIsNone(cap.capture_call(self.call("Bash", {"command": cmd}, {"stdout": "ok"}), self.home,
                                               env={}), cmd)
        self.assertFalse((self.root / SESSION).exists() and list((self.root / SESSION).glob("ev_*.json")))

    # The Phase-1 gate's lost evidence: the worker's decisive code read
    # shared one Bash command with `investigation question`.
    MIXED = ('cardinal-storyboard investigation question "Why does test_export_report fail?" ; '
             "cat ledgerkit/config.py; git log --format='%h %an %ad %s' -3")
    CLI_OUT = 'investigation inv_0123456789abcdef01234567 now asks: "Why does test_export_report fail?"\n'
    CODE = ('SETTINGS = {"currency": "USD", "decimals": 2, "tz": "UTC"}\n'
            "def load_settings(path):\n    SETTINGS.update(json.load(open(path)))\n    return SETTINGS\n"
            "9bf57e9 Lena Fischer Wed Sep 30 settings: JPY (zero-decimal currencies) support + test\n")

    def mixed(self, cmd=None, stdout=None, record=True, **kw):
        if record:
            cap.record_control_output(self.home, self.CLI_OUT, env={})
        return cap.capture_call(self.call("Bash", {"command": cmd or self.MIXED, "description": "read code"},
                                          {"stdout": self.CLI_OUT + self.CODE if stdout is None else stdout,
                                           "stderr": ""}, **kw), self.home, env={})

    def test_a_mixed_command_keeps_the_rest_without_its_control_log_part(self):
        got = self.mixed()
        self.assertIsNotNone(got)
        e = got.entry
        self.assertEqual(e["args"]["command"], cap.CONTROL_OMITTED + " ; cat ledgerkit/config.py; "
                                               "git log --format='%h %an %ad %s' -3")
        self.assertIn("SETTINGS.update(json.load(open(path)))", e["result"]["structured"]["stdout"])
        self.assertIn("9bf57e9 Lena Fischer", e["result"]["structured"]["stdout"])
        self.assertNotIn("now asks", json.dumps(e))
        self.assertNotIn("Why does test_export_report fail", json.dumps(e))
        self.assertIs(e["control_log_omitted"], True)
        self.assertIn(cap.CONTROL_OMITTED_NOTE, got.line)
        item = self.wire(e)
        # conductor's isControlLogItem would refuse it otherwise.
        self.assertFalse(any(cap._CONTROL_LOG_TEXT_RE.search(s) for s in self._strings(item)))
        self.assertEqual(json.loads((self.root / SESSION / f"{e['evidence_id']}.json").read_text()), e)

    @staticmethod
    def _strings(v):
        if isinstance(v, str):
            yield v
        elif isinstance(v, dict):
            for k, x in v.items():
                yield k
                yield from ControlPlaneTests._strings(x)
        elif isinstance(v, list):
            for x in v:
                yield from ControlPlaneTests._strings(x)

    def test_a_checkpoints_claims_never_reach_the_entry(self):
        cmd = ("cat src/a.py && cardinal-storyboard investigation checkpoint --session S <<'EOF'\n"
               '[{"type":"finding.proposed","id":"finding_x","statement":"THE WORKER CLAIM"}]\nEOF\nmake test')
        cap.record_control_output(self.home, "checkpointed #3 (finding.proposed finding_x)\n", env={})
        got = self.mixed(cmd, "def a(): pass\ncheckpointed #3 (finding.proposed finding_x)\n2 passed\n",
                         record=False)
        self.assertIsNotNone(got)
        text = json.dumps(got.entry)
        for gone in ("THE WORKER CLAIM", "finding_x", "checkpointed", "EOF"):
            self.assertNotIn(gone, text)
        self.assertEqual(got.entry["args"]["command"], f"cat src/a.py && {cap.CONTROL_OMITTED}\nmake test")
        self.assertEqual(got.entry["result"]["structured"]["stdout"], "def a(): pass\n2 passed\n")

    def test_python_launcher_redirects_and_a_piped_stdin_are_salvaged(self):
        for cmd in ('python3 -I "$P/bin/cardinal-storyboard" investigation question \'why?\' 2>&1; grep -n x src/',
                    "echo '[]' | cardinal-storyboard investigation checkpoint --session S >/dev/null; ls src",
                    "CARDINAL_X=1 cardinal-storyboard investigation link --json & pytest -q"):
            got = self.mixed(cmd, "src/a.py:1:x\n")
            self.assertIsNotNone(got, cmd)
            self.assertNotIn("cardinal-storyboard", json.dumps(got.entry), cmd)

    def test_any_doubt_skips_the_whole_call_as_before(self):
        cases = {
            "the event stream itself": "cardinal-storyboard investigation events inv_x; ls",
            "show": "cardinal-storyboard investigation show inv_x && ls",
            "its output piped on": "cardinal-storyboard investigation link | head -3; ls",
            "a command substitution": "x=$(cardinal-storyboard investigation link); echo $x",
            "inside a group": "{ cardinal-storyboard investigation link; ls; }",
            "an unterminated heredoc": "ls; cardinal-storyboard investigation checkpoint <<'EOF'\n[{}]",
            "a heredoc before a ;": "cardinal-storyboard investigation checkpoint <<EOF; ls\n[]\nEOF",
            "an unterminated quote": "ls; cardinal-storyboard investigation question 'why",
            "only a cd beside it": "cd /repo && cardinal-storyboard investigation link",
            "nothing beside it": "cardinal-storyboard investigation link; # done",
        }
        for why, cmd in cases.items():
            self.assertIsNone(self.mixed(cmd, "x\n"), why)
        self.assertFalse((self.root / SESSION).exists() and list((self.root / SESSION).glob("ev_*.json")))

    def test_no_record_of_the_clis_output_or_an_overflow_skips_it(self):
        self.assertIsNone(self.mixed(record=False), "the CLI recorded nothing: its lines cannot be told apart")
        cap.record_control_output(self.home, "x" * (cap.CONTROL_OUTPUT_MAX_BYTES + 1), env={})
        self.assertIsNone(self.mixed())

    def test_control_log_text_left_in_the_output_skips_it(self):
        leaked = self.CODE + '{"seq": 4, "type": "finding.proposed", "authority": "producer_claim"}\n'
        self.assertIsNone(self.mixed(stdout=self.CLI_OUT + leaked))
        self.assertIsNone(self.mixed(stdout=self.CODE + "curl -s $M/mcp-tools/read-investigation-events\n"))

    def test_the_cli_named_outside_the_command_skips_it(self):
        c = self.call("Bash", {"command": self.MIXED, "description": "cardinal-storyboard investigation question"},
                      {"stdout": self.CODE, "stderr": ""})
        cap.record_control_output(self.home, self.CLI_OUT, env={})
        self.assertIsNone(cap.capture_call(c, self.home, env={}))
        c = self.call("SomeTool", {"script": self.MIXED}, {"stdout": self.CODE})
        self.assertIsNone(cap.capture_call(c, self.home, env={}))

    def test_a_secret_in_the_rest_is_still_withheld(self):
        got = self.mixed("cardinal-storyboard investigation link; cat ~/.aws/credentials", "aws_secret_access_key=x\n")
        self.assertIsNotNone(got)
        self.assertTrue(got.withheld)
        self.assertIsNone(got.entry["args"])

    def test_a_failed_command_keeps_its_output_without_the_clis_lines(self):
        cap.record_control_output(self.home, self.CLI_OUT, env={})
        c = self.call("Bash", {"command": self.MIXED}, None,
                      error="Exit code 1\n" + self.CLI_OUT + "cat: ledgerkit/config.py: No such file\n")
        got = cap.capture_call(c, self.home, env={})
        self.assertIsNotNone(got)
        self.assertNotIn("now asks", json.dumps(got.entry))
        self.assertIn("No such file", json.dumps(got.entry))

    def test_the_output_record_is_private_bounded_and_expires(self):
        cap.record_control_output(self.home, "a line\n\nanother  \n", env={})
        d = cap.control_output_dir(self.home)
        (f,) = list(d.iterdir())
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(f.read_text())["lines"], ["a line", "another"])
        self.assertEqual(cap._control_lines(self.home), {"a line", "another"})
        later = time.time() + cap.CONTROL_OUTPUT_TTL_S + 60
        self.assertIsNone(cap._control_lines(self.home, now=later))
        self.assertEqual(list(d.iterdir()), [], "an expired record is removed")
        cap.record_control_output(self.home, "x\n", env={"CARDINAL_EVIDENCE_CAPTURE": "0"})
        self.assertEqual(list(d.iterdir()), [], "nothing is recorded with capture off")

    def test_shell_statements(self):
        self.assertEqual([self.MIXED[s:e] for s, e, _ in cap.shell_statements(self.MIXED)],
                         ['cardinal-storyboard investigation question "Why does test_export_report fail?" ',
                          " cat ledgerkit/config.py", " git log --format='%h %an %ad %s' -3"])
        cmd = "a 2>&1 | b |& c && d || e & f >| g; h <<-X\n\tbody ; | &&\n\tX\ni"
        self.assertEqual([(cmd[s:e], len(p)) for s, e, p in cap.shell_statements(cmd)],
                         [("a 2>&1 | b |& c ", 2), (" d ", 0), (" e ", 0), (" f >| g", 0),
                          (" h <<-X\n\tbody ; | &&\n\tX", 0), ("i", 0)])
        for bad in ("a 'b", 'a "b', "a $(b", "a `b", "a )", "case x in a) b;; esac", "a <<X\nno end"):
            self.assertIsNone(cap.shell_statements(bad), bad)

    def test_other_cardinal_storyboard_calls_are_captured(self):
        for cmd in ("cardinal-storyboard context --json", "cardinal-storyboard state check s.json",
                    "grep investigation cardinal-storyboard.log", "echo cardinal-storyboard-investigation"):
            self.assertFalse(cap.control_plane({"command": cmd}), cmd)
        self.capture("Bash", {"command": "cardinal-storyboard context"}, {"stdout": "{}"})


class PipelineTests(_Case):
    def test_cardinal_source_is_skipped(self):
        c = self.call("mcp__plugin_cardinal_cardinal__lakerunner__x", {}, "x")
        self.assertEqual(c.source["kind"], "cardinal")
        self.assertIsNone(cap.capture_call(c, self.home, env={}))
        self.assertFalse(self.root.exists() and any(self.root.rglob("ev_*.json")))

    def test_opt_out(self):
        self.assertIsNone(cap.capture_call(self.call("Bash", {"command": "ls"}, "x"), self.home,
                                           env={"CARDINAL_EVIDENCE_CAPTURE": "0"}))
        evidence.set_capture_disabled(self.root, True)
        self.assertIsNone(cap.capture_call(self.call("Bash", {"command": "ls"}, "x"), self.home, env={}))

    def test_withheld_stub_keeps_nothing(self):
        planted = "hunter2-PLANTED"
        got = cap.capture_call(self.call("Bash", {"command": f"PGPASSWORD={planted} psql -c 'select 1'"},
                                         {"stdout": planted, "stderr": ""}), self.home, env={})
        self.assertTrue(got.withheld)
        e = got.entry
        self.assertEqual(e["withheld"]["reason"], "credential_header")
        self.assertIsNone(e["args"])
        self.assertIsNone(e["result"])
        self.assertNotIn("summary", e)
        raw = (self.root / SESSION / f"{e['evidence_id']}.json").read_bytes()
        self.assertNotIn(planted.encode(), raw)
        self.assertNotIn(b"psql", raw)
        self.assertIn("withheld:", got.line)
        with self.assertRaises(promote.WithheldEntry):
            promote.wire_item(e, "c/1")

    def test_normalizer_that_raises_or_returns_junk_falls_back_to_generic(self):
        calls = []

        def boom(call):
            calls.append(call.tool)
            raise RuntimeError("normalizer bug")

        try:
            norms.REGISTRY.insert(0, norms.Normalizer("boom", lambda c: True, boom))
            e = self.capture("Bash", {"command": "ls"}, {"stdout": "a", "stderr": ""}, tuid="t_boom")
            self.assertEqual(e["normalizer"], "generic")
            self.assertEqual(calls, ["Bash"])
            norms.REGISTRY[0] = norms.Normalizer("junk", lambda c: True, lambda c: {"not": "Normalized"})
            e = self.capture("X", {}, {"a": 1}, tuid="t_junk")
            self.assertEqual(e["normalizer"], "generic")
            norms.REGISTRY[0] = norms.Normalizer("badmatch", lambda c: 1 / 0, lambda c: norms.Normalized())
            e = self.capture("Bash", {"command": "ls"}, {"stdout": "a", "stderr": ""}, tuid="t_badmatch")
            self.assertEqual(e["normalizer"], "shell")
        finally:
            del norms.REGISTRY[0]

    def test_a_normalizer_cannot_gate(self):
        # A normalizer that would "skip" by returning an empty body still
        # leaves a captured entry, and the gate still withholds.
        try:
            norms.REGISTRY.insert(0, norms.Normalizer("empty", lambda c: True, lambda c: norms.Normalized()))
            e = self.capture("Anything", {"a": 1}, {"b": 2}, tuid="t_empty")
            self.assertEqual(e["normalizer"], "empty")
            got = cap.capture_call(self.call("Read", {"file_path": "/x/.env"}, {"b": 2}, tuid="t_env"), self.home,
                                   env={})
            self.assertTrue(got.withheld)
        finally:
            del norms.REGISTRY[0]

    def test_context_lines(self):
        a = cap.capture_call(self.call("Bash", {"command": "a"}, "x", tuid="c1"), self.home, env={})
        b = cap.capture_call(self.call("Bash", {"command": "b"}, "x", tuid="c2"), self.home, env={})
        self.assertIn("promote", a.line)
        self.assertEqual(b.line, f"[evidence:{b.entry['evidence_id']}]")
        c = cap.capture_call(self.call("Bash", {"command": "c"}, "x", tuid="c3"), self.home,
                             env={"CARDINAL_EVIDENCE_CONTEXT": "0"})
        self.assertIsNone(c.line)
        self.assertTrue((self.root / SESSION / f"{c.entry['evidence_id']}.json").exists())
        d = cap.capture_call(self.call("Bash", {"command": "d"}, "x", tuid="c4"), self.home, env={},
                             promote_cmd="cardinal-pi evidence")
        self.assertEqual(d.line, f"[evidence:{d.entry['evidence_id']}]")

    def test_same_tool_use_id_rewrites_the_same_entry(self):
        a = self.capture("Bash", {"command": "x"}, "1", tuid="dup")
        b = self.capture("Bash", {"command": "x"}, "2", tuid="dup")
        self.assertEqual(a["evidence_id"], b["evidence_id"])
        self.assertEqual(len(list((self.root / SESSION).glob("ev_*.json"))), 1)

    def test_without_a_tool_use_id_ids_are_unique(self):
        c = self.call("Bash", {"command": "x"}, "1")
        c = cap.ToolCall(**{**c.__dict__, "tool_use_id": None, "called_at": "2026-01-01T00:00:00Z"})
        ids = {cap.capture_call(c, self.home, env={}).entry["evidence_id"] for _ in range(5)}
        self.assertEqual(len(ids), 5)

    def test_redaction_is_stricter_than_the_gateway_leaf_rule(self):
        out = "\n".join([
            "DB_PASSWORD=hunter2",
            "export AWS_SECRET_ACCESS_KEY=abc/def+ghi",
            "config/.env:3:API_TOKEN=zzz",
            "src/a.py:10:def main():",
            f"see {self.cwd}/src/a.py and {self.home}/notes and {self.spill}/p/x.txt",
            "Authorization: Bearer abcdefghijklmnop",
            "ghp_" + "a" * 36,
        ])
        e = self.capture("Bash", {"command": "show"}, {"stdout": out, "stderr": ""})
        s = e["result"]["structured"]["stdout"]
        for leak in ("hunter2", "abc/def+ghi", "zzz", "abcdefghijklmnop", "ghp_aaaa"):
            self.assertNotIn(leak, s)
        self.assertIn("config/.env:[withheld]", s)
        self.assertIn("src/a.py:10:def main():", s)
        self.assertIn("see ./src/a.py and ~/notes and [local file]", s)
        self.assertNotIn(str(self.home), json.dumps(e))

    def test_go_test_status_lines_keep_their_test_names(self):
        # PASS is a credential key; `go test -v` / `-json` status lines are
        # the one exempt shape (conductor isGoTestStatus), through the
        # _Hardener's wide pass and the error-text pass alike.
        verbose = ("=== RUN   TestX\n--- PASS: TestX (0.01s)\n=== RUN   TestY\n"
                   "    --- PASS: TestY/sub_case (0.00s)\nPASS\nok  \texample.com/pkg\t0.012s\n")
        e = self.capture("Bash", {"command": "go test -v ./..."}, {"stdout": verbose, "stderr": ""})
        self.assertEqual(e["result"]["structured"]["stdout"], verbose)
        self.assertNotIn(evidence.REDACTED, json.dumps(e))

        events = ('{"Action":"output","Package":"p","Test":"TestX","Output":"--- PASS: TestX (0.00s)\\n"}\n'
                  '{"Action":"pass","Package":"p","Test":"TestX","Elapsed":0}\n')
        e = self.capture("Bash", {"command": "go test -json ./..."}, {"stdout": events, "stderr": ""})
        self.assertEqual(e["result"]["structured"]["stdout"], events)

        one = '{"Action":"output","Package":"p","Test":"TestX","Output":"--- PASS: TestX (0.00s)\\n"}'
        e = self.capture("mcp__ci__run", {"cmd": "go test -json"}, {"content": [{"type": "text", "text": one}]})
        self.assertEqual(e["result"]["structured"]["Output"], "--- PASS: TestX (0.00s)\n")

        failing = "Exit code 1\n--- PASS: TestAlpha (0.00s)\n--- FAIL: TestBeta (0.01s)\n    x_test.go:9: password=hunter2\nFAIL\n"
        e = self.capture("Bash", {"command": "go test -v ./..."}, None, error=failing)
        out = e["result"]["structured"]["output"]
        self.assertIn("--- PASS: TestAlpha (0.00s)", out)
        self.assertIn("--- FAIL: TestBeta (0.01s)", out)
        self.assertNotIn("hunter2", json.dumps(e))

        # Near misses are still credentials.
        e = self.capture("Bash", {"command": "cat run.log"}, {"stdout": "DB_PASS=x\n--- PASS=y\nPASS: z\n", "stderr": ""})
        self.assertEqual(e["result"]["structured"]["stdout"],
                         "DB_PASS=[redacted]\n--- PASS=[redacted]\nPASS: [redacted]\n")

    def test_json_text_leaves_get_the_text_rules_too(self):
        doc = json.dumps({"items": [{"env": "DB_PASSWORD=hunter2", "log": "ok"}]}, indent=2)
        e = self.capture("Bash", {"command": "kubectl get x -o json"}, {"stdout": doc, "stderr": ""})
        self.assertNotIn("hunter2", json.dumps(e))
        clean = json.dumps({"a": [1, 2]}, indent=2)
        e = self.capture("Bash", {"command": "cat a.json"}, {"stdout": clean, "stderr": ""})
        self.assertEqual(e["result"]["structured"]["stdout"], clean)  # kept as written

    def test_large_output_is_capped_quickly(self):
        big = ("line of build output 0123456789 " * 3 + "\n") * 120000  # ~12 MiB
        t = time.monotonic()
        e = self.capture("Bash", {"command": "make"}, {"stdout": big, "stderr": ""})
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertTrue(e["truncated"])
        self.assertTrue(e["result"]["truncated"])
        self.assertGreater(e["result"]["original_bytes"], 10 << 20)
        self.assertLessEqual(len(evidence.encode_json(e["result"]).encode()), evidence.MAX_RESULT_BYTES)

    def test_typical_capture_is_fast(self):
        times = []
        for i in range(30):
            t = time.monotonic()
            self.capture("Bash", {"command": f"git status {i}"},
                         {"stdout": "On branch main\n" + "M file.py\n" * 50, "stderr": ""}, tuid=f"perf{i}")
            times.append(time.monotonic() - t)
        times.sort()
        self.assertLess(times[len(times) // 2], 0.1)

    def test_files_are_private(self):
        e = self.capture("Bash", {"command": "ls"}, "x")
        p = self.root / SESSION / f"{e['evidence_id']}.json"
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)

    def test_unreadable_payload_stub(self):
        head = json.dumps({"session_id": SESSION, "tool_name": "Bash", "tool_use_id": "toolu_big",
                           "tool_input": {"command": "cat huge"}}).encode()[:-1] + b', "tool_response": "' + b"x" * 100
        e = cap.unreadable_record("claude-code", "claude-code/1", head)
        self.assertEqual(e["withheld"]["reason"], "unreadable")
        self.assertEqual(e["session_id"], SESSION)
        self.assertEqual(e["tool"], "Bash")
        self.assertIsNone(e["args"])
        self.assertIsNone(cap.unreadable_record("claude-code", "c", b"garbage"))

    def test_time_guard_interrupts(self):
        if not hasattr(__import__("signal"), "setitimer"):
            self.skipTest("no setitimer")
        with self.assertRaises(cap.BudgetExceeded):
            with cap.time_guard(0.05):
                while True:
                    pass
        with cap.time_guard(1.0):
            pass  # disarmed cleanly


class VectorTests(unittest.TestCase):
    def test_evidence_id_vectors(self):
        data = json.loads((TESTDATA / "evidence_id_vectors.json").read_text())
        self.assertGreaterEqual(len(data["vectors"]), 5)
        for v in data["vectors"]:
            self.assertEqual(cap.evidence_id_v2(v["runtime"], v["session_id"], v["tool_use_id"]), v["evidence_id"])
        self.assertIsNone(cap.evidence_id_v2("claude-code", SESSION, None))
        self.assertIsNone(cap.evidence_id_v2("claude-code", SESSION, "x" * 257))

    def test_wire_vectors(self):
        data = json.loads((TESTDATA / "evidence_wire_vectors.json").read_text())
        names = {v["name"] for v in data["vectors"]}
        self.assertTrue({"builtin-bash", "builtin-unknown-tool", "mcp-grafana", "opencode-tool"} <= names)
        for v in data["vectors"]:
            with self.subTest(v["name"]):
                item = promote.wire_item(v["entry"], v["client"])
                self.assertEqual(item, v["item"])
                self.assertEqual(schema_errors(item), [])

    def test_v1_entries_still_promote(self):
        v1 = evidence.build_entry(server="grafana", tool="q", tool_name="mcp__grafana__q", tool_input={"a": 1},
                                  tool_response="rows: 3", session_id=SESSION, agent="claude-code")
        item = promote.wire_item(v1, "claude-code/1.0.0")
        self.assertEqual(item["source_server"], "grafana")
        self.assertEqual(item["client"], "claude-code/1.0.0")
        self.assertEqual(schema_errors(item), [])


if __name__ == "__main__":
    unittest.main()

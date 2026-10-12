"""Tests for the Checks preview: bin/cardinal-checks, hooks/checks-gate.py,
hooks/_checks.py and the skill template in checks/skill/.

Each test copies the plugin's bin/, hooks/ and checks/ into a temp plugin
root (cardinal-checks installs into the plugin it runs from) and runs the
scripts as subprocesses with HOME pointed at a temp dir, the way Claude Code
invokes them.

Run with: python3 -m unittest tests.test_checks -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ADAPTER = Path(__file__).resolve().parent.parent
TEMPLATE = ADAPTER / "checks" / "skill"
TRACE = "a" * 32
OTHER = "b" * 32


def frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must start with YAML frontmatter"
    out = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


class ChecksTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.home = root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.plugin = root / "plugin"
        for part in ("bin", "hooks", "checks"):
            shutil.copytree(ADAPTER / part, self.plugin / part,
                            ignore=shutil.ignore_patterns("__pycache__", "cardinal_core"))
        (self.plugin / "skills").mkdir()
        self.installed = self.plugin / "skills" / "checks"

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CARDINAL_") and k not in ("OTEL_EXPORTER_OTLP_HEADERS",)}
        env["HOME"] = str(self.home)
        env.update(extra)
        return env

    def cli(self, *args, **env):
        return subprocess.run([sys.executable, str(self.plugin / "bin" / "cardinal-checks"), *args],
                              capture_output=True, text=True, env=self.env(**env), timeout=30)

    def hook(self, payload: dict, **env):
        proc = subprocess.run([sys.executable, str(self.plugin / "hooks" / "checks-gate.py")],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env=self.env(**env), timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def session_start(self, **env):
        return self.hook({"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"}, **env)

    def skill_call(self, name):
        return self.hook({"hook_event_name": "PreToolUse", "tool_name": "Skill", "tool_input": {"skill": name}})


class OptInTests(ChecksTestBase):
    def test_on_installs_the_skill_into_the_plugin_and_off_removes_it(self):
        self.assertFalse(self.installed.exists())
        on = self.cli("on")
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertEqual((self.installed / "SKILL.md").read_text(), (TEMPLATE / "SKILL.md").read_text())
        self.assertEqual((self.installed / "CORE.md").read_text(), (TEMPLATE / "CORE.md").read_text())
        self.assertIn("Checks preview: on", self.cli("status").stdout)
        off = self.cli("off")
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertFalse(self.installed.exists())
        self.assertIn("Checks preview: off", self.cli("status").stdout)

    def test_off_never_removes_a_skills_checks_it_did_not_install(self):
        self.installed.mkdir()
        (self.installed / "SKILL.md").write_text("someone else's skill\n")
        self.assertNotEqual(self.cli("off").returncode, 0)
        self.assertTrue((self.installed / "SKILL.md").exists())
        on = self.cli("on")
        self.assertNotEqual(on.returncode, 0)
        self.assertEqual((self.installed / "SKILL.md").read_text(), "someone else's skill\n")
        self.assertIn("Checks preview: off", self.cli("status").stdout)

    def test_on_twice_is_a_no_op_and_a_changed_template_is_reinstalled(self):
        self.cli("on")
        marker = (self.installed / ".cardinal-checks-install").read_text()
        self.assertEqual(self.cli("on").returncode, 0)
        self.assertEqual((self.installed / ".cardinal-checks-install").read_text(), marker)
        (self.plugin / "checks" / "skill" / "CORE.md").write_text("# revised\n")
        self.assertEqual(self.cli("on").returncode, 0)
        self.assertEqual((self.installed / "CORE.md").read_text(), "# revised\n")


class GateHookTests(ChecksTestBase):
    def test_session_start_installs_a_missing_copy_when_on_and_asks_for_a_skill_reload(self):
        self.cli("on")
        shutil.rmtree(self.installed)   # a plugin update: a fresh version dir without the copy
        out = self.session_start()
        self.assertTrue(out["hookSpecificOutput"]["reloadSkills"])
        self.assertTrue((self.installed / "SKILL.md").exists())
        again = self.session_start()
        self.assertTrue(again is None or "reloadSkills" not in again["hookSpecificOutput"])

    def test_session_start_removes_a_stale_copy_when_off(self):
        self.cli("on")
        (self.home / ".claude" / "cardinal" / "checks" / "enabled.json").write_text('{"enabled": false}')
        out = self.session_start()
        self.assertTrue(out["hookSpecificOutput"]["reloadSkills"])
        self.assertFalse(self.installed.exists())

    def test_routing_rule_is_injected_only_when_opted_in_and_connected(self):
        connected = {"CARDINAL_MCP_API_KEY": "k", "CARDINAL_MCP_URL": "https://x.example/api/orgs/o/mcp"}
        self.assertIsNone(self.session_start(**connected))
        self.cli("on")
        out = self.session_start(**connected)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("answer with the Cardinal query tools first", context)
        self.assertIn("checks skill", context)
        unconnected = self.session_start()
        self.assertTrue(unconnected is None or "additionalContext" not in unconnected["hookSpecificOutput"])
        self.cli("off")
        out = self.session_start(**connected)
        self.assertTrue(out is None or "additionalContext" not in out["hookSpecificOutput"])

    def test_skill_tool_is_denied_for_checks_while_off(self):
        for name in ("cardinal:checks", "checks"):
            out = self.skill_call(name)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("cardinal-checks on", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.skill_call("cardinal:storyboard"))
        self.assertIsNone(self.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                     "tool_input": {"command": "checks"}}))
        self.cli("on")
        self.assertIsNone(self.skill_call("cardinal:checks"))

    def test_a_users_own_skill_named_checks_is_never_denied(self):
        own = self.home / ".claude" / "skills" / "checks"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("---\nname: checks\ndescription: my lint checks\n---\n")
        self.assertIsNone(self.skill_call("checks"))
        self.assertEqual(self.skill_call("cardinal:checks")["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_hooks_json_registers_the_gate(self):
        hooks = json.loads((ADAPTER / "hooks" / "hooks.json").read_text())["hooks"]
        start = [h["command"] for group in hooks["SessionStart"] for h in group["hooks"]]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/checks-gate.py", start)
        skill = [g for g in hooks["PreToolUse"] if g["matcher"] == "Skill"]
        self.assertEqual([h["command"] for h in skill[0]["hooks"]], ["${CLAUDE_PLUGIN_ROOT}/hooks/checks-gate.py"])


class SkillTemplateTests(unittest.TestCase):
    def test_frontmatter_is_model_invoked_and_hidden_from_the_slash_menu(self):
        meta = frontmatter((TEMPLATE / "SKILL.md").read_text())
        self.assertEqual(meta["name"], "checks")
        self.assertEqual(meta["user-invocable"], "false")
        self.assertNotIn("disable-model-invocation", meta)
        for trigger in ("how often does X happen", "find traces where", "flag any"):
            self.assertIn(trigger, meta["description"])
        self.assertIn("Do not use for counts", meta["description"])
        # A plain YAML scalar may not contain ": " or " #"; Claude Code drops a
        # skill whose frontmatter fails to parse from the model's listing.
        for value in meta.values():
            self.assertNotIn(": ", value)
            self.assertNotIn(" #", value)

    def test_the_skill_is_not_shipped_in_the_auto_loaded_skills_dir(self):
        tracked = subprocess.run(["git", "ls-files", "adapters/claude/skills/checks"], cwd=ADAPTER.parent.parent,
                                 capture_output=True, text=True).stdout
        self.assertEqual(tracked.strip(), "")
        self.assertTrue((TEMPLATE / "CORE.md").is_file())

    def test_core_keeps_judge_checks_experimental_and_bans_causal_language(self):
        core = (TEMPLATE / "CORE.md").read_text()
        self.assertIn("Experimental", core)
        self.assertIn("No causal language", core)
        self.assertIn("Missing evidence means UNKNOWN, never NON_MATCH", core)
        self.assertIn("run.trace.trace_id", core)
        self.assertIn("execute_behavior_range", core)
        self.assertIn("cardinal-checks example expected", core)


class LabelLedgerTests(ChecksTestBase):
    def test_a_correction_that_contradicts_insufficient_evidence_is_refused(self):
        r = self.cli("example", "set", "--draft", "megan-claims-v1", "--trace", TRACE, "--evidence", "insufficient",
                     "--proposed", "UNKNOWN", "--missing", "deciding span not retained", "--reason", "manifest has gaps")
        self.assertEqual(r.returncode, 0, r.stderr)
        refused = self.cli("example", "approve", "--draft", "megan-claims-v1", "--trace", TRACE, "--label", "MATCH")
        self.assertEqual(refused.returncode, 3)
        self.assertIn("refused", refused.stderr)
        self.assertIn("deciding span not retained", refused.stderr)
        shown = json.loads(self.cli("example", "show", "--draft", "megan-claims-v1").stdout)
        self.assertEqual(shown["examples"][TRACE]["label"], "UNKNOWN")

    def test_an_insufficient_example_cannot_be_proposed_as_anything_but_unknown(self):
        r = self.cli("example", "set", "--draft", "d1", "--trace", TRACE, "--evidence", "insufficient",
                     "--proposed", "NON_MATCH", "--missing", "no manifest", "--reason", "x")
        self.assertEqual(r.returncode, 3)
        self.assertIn("UNKNOWN by construction", r.stderr)

    def test_sufficient_examples_take_corrections_until_the_first_test(self):
        self.cli("example", "set", "--draft", "d1", "--trace", TRACE, "--evidence", "sufficient",
                 "--proposed", "MATCH", "--reason", "number not in any query result")
        self.cli("example", "set", "--draft", "d1", "--trace", OTHER, "--evidence", "sufficient",
                 "--proposed", "NON_MATCH", "--reason", "every number traced to a result")
        pending = self.cli("example", "expected", "--draft", "d1")
        self.assertEqual(pending.returncode, 3)
        self.assertIn("approve every example", pending.stderr)
        corrected = self.cli("example", "approve", "--draft", "d1", "--trace", TRACE, "--label", "NON_MATCH")
        self.assertEqual(corrected.returncode, 0, corrected.stderr)
        self.assertTrue(json.loads(corrected.stdout)["corrected"])
        self.assertEqual(self.cli("example", "approve", "--draft", "d1", "--trace", OTHER).returncode, 0)
        expected = self.cli("example", "expected", "--draft", "d1")
        self.assertEqual(json.loads(expected.stdout), {TRACE: "NON_MATCH", OTHER: "NON_MATCH"})
        late = self.cli("example", "approve", "--draft", "d1", "--trace", TRACE, "--label", "MATCH")
        self.assertEqual(late.returncode, 3)
        self.assertIn("Revise the contract", late.stderr)

    def test_a_mistyped_candidate_can_be_removed_until_the_labels_are_locked(self):
        self.cli("example", "set", "--draft", "d1", "--trace", TRACE, "--evidence", "sufficient",
                 "--proposed", "MATCH", "--reason", "x")
        self.cli("example", "set", "--draft", "d1", "--trace", OTHER, "--evidence", "sufficient",
                 "--proposed", "NON_MATCH", "--reason", "y")
        removed = self.cli("example", "remove", "--draft", "d1", "--trace", TRACE)
        self.assertEqual(json.loads(removed.stdout), {"removed": True})
        self.cli("example", "approve", "--draft", "d1", "--trace", OTHER)
        self.assertEqual(json.loads(self.cli("example", "expected", "--draft", "d1").stdout), {OTHER: "NON_MATCH"})
        self.assertEqual(self.cli("example", "remove", "--draft", "d1", "--trace", OTHER).returncode, 3)

    def test_a_teaching_batch_holds_at_most_eight_examples(self):
        for i in range(8):
            r = self.cli("example", "set", "--draft", "d1", "--trace", format(i, "032x"), "--evidence", "sufficient",
                         "--proposed", "NON_MATCH", "--reason", "clean")
            self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("example", "set", "--draft", "d1", "--trace", "f" * 32, "--evidence", "sufficient",
                     "--proposed", "MATCH", "--reason", "x")
        self.assertEqual(r.returncode, 3)


class AcceptedChecksTests(ChecksTestBase):
    def test_accepted_lists_the_incumbent_of_each_scope(self):
        output = self.home / ".cardinal" / "behavior-executions"
        (output / "programs").mkdir(parents=True)
        (output / "compilation" / ("c" * 64)).mkdir(parents=True)
        context = {"description": "Flag answers stating facts no query returned", "service_name": "megan"}
        for version, accepted in (("1" * 64, True), ("2" * 64, True), ("3" * 64, False)):
            program = {"diagnostic_version": version, "compilation_context": context, "compilation_scope": "c" * 64}
            if accepted:
                program["acceptance_id"] = "acc-" + version[:4]
            (output / "programs" / f"{version}.json").write_text(json.dumps(program))
        (output / "compilation" / ("c" * 64) / "regression.json").write_text(json.dumps({"incumbent": "2" * 64}))
        listed = json.loads(self.cli("accepted", "--service", "megan").stdout)
        self.assertEqual([c["diagnostic_version"] for c in listed], ["2" * 64])
        self.assertEqual(listed[0]["description"], context["description"])
        self.assertEqual(json.loads(self.cli("accepted", "--service", "traefik").stdout), [])


class RangeWatchTests(ChecksTestBase):
    RANGE = "c" * 32

    def write_events(self, *events):
        directory = self.home / ".cardinal" / "behavior-executions" / "ranges" / self.RANGE
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "events.jsonl").open("a") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
        return directory

    def test_watch_prints_one_line_per_event_and_exits_on_a_terminal_event(self):
        self.write_events(
            {"event": "started", "population": "maestro", "start": "S", "end": "E", "windows": 24},
            {"event": "window_submitted", "window": ["a", "b"], "execution_id": "x"},
            {"event": "window_done", "window": ["a", "b"], "status": "COMPLETED",
             "verdicts": {"MATCH": 0, "NON_MATCH": 3589, "UNKNOWN": 1, "NOT_APPLICABLE": 243, "ERROR": 0},
             "progress": {"windows_done": 1, "windows_left": 23}},
            {"event": "read_retry", "window": ["a", "b"], "failures": 3, "final": True, "error": "timed out"},
            {"event": "completed", "population": 3833, "counts": {"MATCH": 0}, "flagged": 0, "edge_traces": 0,
             "conflicts": 0, "coverage_holes": 0, "judge_calls": 0, "summary_file": "/x/summary.json"})
        proc = self.cli("range", "watch", self.RANGE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 4)   # window_submitted is not a notification
        self.assertIn("NON_MATCH 3589", lines[1])
        self.assertIn("1 done, 23 left", lines[1])
        self.assertIn("giving up on this execution", lines[2])
        self.assertTrue(lines[3].startswith("completed:"))
        self.assertIn("/x/summary.json", lines[3])

    def test_a_watch_after_continue_starts_at_the_new_segment(self):
        self.write_events(
            {"event": "started", "population": "p", "start": "S", "end": "E", "windows": 1},
            {"event": "needs_extents", "multi_window_trace_count": 0, "boundaries": ["S", "E"]},
            {"event": "extents_received", "traces": 0, "whole_trace_windows": 0},
            {"event": "completed", "population": 5, "counts": {}, "flagged": 0, "edge_traces": 0,
             "conflicts": 0, "coverage_holes": 0, "judge_calls": 0, "summary_file": "/s.json"})
        lines = self.cli("range", "watch", self.RANGE).stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("extents received"))
        self.assertTrue(lines[1].startswith("completed:"))

    def test_a_watch_does_not_stop_at_a_superseded_terminal_event(self):
        sys.path.insert(0, str(self.plugin / "hooks"))
        try:
            import importlib
            import io
            import _checks
            importlib.reload(_checks)
            directory = self.write_events(
                {"event": "needs_extents", "multi_window_trace_count": 0, "boundaries": []},
                {"event": "extents_received", "traces": 0, "whole_trace_windows": 1})
            appended = []
            def sleep(_):
                if not appended:
                    appended.append(1)
                    self.write_events({"event": "completed", "population": 1, "counts": {}, "flagged": 0,
                                       "edge_traces": 0, "conflicts": 0, "coverage_holes": 0, "judge_calls": 0,
                                       "summary_file": "/s.json"})
            out = io.StringIO()
            code = _checks.watch_range(self.RANGE, out, idle_s=5, output=directory.parent.parent, sleep=sleep)
        finally:
            sys.path.remove(str(self.plugin / "hooks"))
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().splitlines()[-1].startswith("completed:"))

    def test_watch_reports_a_stalled_worker(self):
        sys.path.insert(0, str(self.plugin / "hooks"))
        try:
            import importlib
            import _checks
            importlib.reload(_checks)
            directory = self.write_events({"event": "started", "population": "p", "start": "S", "end": "E",
                                           "windows": 1})
            import io
            out = io.StringIO()
            code = _checks.watch_range(self.RANGE, out, poll_s=1, idle_s=2,
                                       output=directory.parent.parent, sleep=lambda s: None)
        finally:
            sys.path.remove(str(self.plugin / "hooks"))
        self.assertEqual(code, 0)
        self.assertIn("stalled", out.getvalue().splitlines()[-1])
        self.assertIn("continue_behavior_range", out.getvalue())

    def test_status_reads_local_state(self):
        directory = self.write_events({"event": "started"})
        (directory / "state.json").write_text(json.dumps({"phase": "windows", "population": "maestro",
                                                          "windows": [{}], "pending": [{}, {}]}))
        status = json.loads(self.cli("range", "status", self.RANGE).stdout)
        self.assertEqual((status["phase"], status["windows_done"], status["windows_left"]), ("windows", 1, 2))
        self.assertFalse(status["worker_running"])
        self.assertEqual(self.cli("range", "watch", "d" * 32).returncode, 2)


if __name__ == "__main__":
    unittest.main()

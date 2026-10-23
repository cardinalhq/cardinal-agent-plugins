"""PR linkage on cardinal.git_state and opt-in decision capture in the
Copilot adapter.

Decision flow under test: the agent's (possibly sandboxed) `cardinal-decision
record` only validates and prints a confirmation marker; the postToolUse hook
re-derives the decision from the argv of the bash command the agent actually
ran (toolArgs.command), accepts it only when a matching marker in
toolResult.textResultForLlm confirms the run, applies the on/off gate, writes
the ledger, and hands the network sends (turn_tool, clusters + PR +
cardinal.decision) to the background job (inline here).

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_decisions.py -v
"""

from __future__ import annotations

import json
import shlex
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from fixtures import ADAPTER, DECISION_CLI, HOOK, HookCase, load_script

MARKER = "cardinal-decision-record:v1 "
SESSION = "c1"

# Loaded via PYTHONPATH for the sandbox simulation: any network connect,
# subprocess spawn, or filesystem mutation kills the process with 97.
SANDBOX_SITECUSTOMIZE = '''
import builtins, os, socket, subprocess

def _deny(*_a, **_k):
    os.write(2, b"SANDBOX-VIOLATION\\n")
    os._exit(97)

socket.socket.connect = _deny
socket.create_connection = _deny
subprocess.Popen.__init__ = _deny
for _name in ("mkdir", "makedirs", "replace", "rename", "remove", "unlink", "rmdir"):
    setattr(os, _name, _deny)
_real_open = builtins.open

def _open(file, mode="r", *a, **k):
    if any(c in mode for c in "wax+"):
        _deny()
    return _real_open(file, mode, *a, **k)

builtins.open = _open
'''


def marker_line(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith(MARKER))


class DecisionBase(HookCase):
    RECORD_ARGS = (
        "record",
        "--question", "How do we index billions of trace IDs?",
        "--choice", "Use mutation-run S3 LSM",
        "--why", "Global ID-only lookup without per-ID database rows.",
        "--alt", "Per-ID rows in Postgres",
        "--anchor", "svc/f3.go::Index.Put",
        "--anchor", "config:TRACE_INDEX_ENABLED",
    )

    def setUp(self):
        super().setUp()
        self.connect()
        (self.repo / "svc").mkdir()
        for i in range(12):
            (self.repo / "svc" / f"f{i}.go").write_text("package svc\n")
        from fixtures import git
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "svc")
        self.gh_calls = self.base / "gh-calls"
        self.write_gh('echo \'{"number": 42, "url": "https://github.com/acme/widgets/pull/42"}\'')

    def write_gh(self, body: str) -> None:
        gh = self.bin / "gh"
        gh.write_text(f'#!/bin/sh\necho "$@" >> "{self.gh_calls}"\n{body}\n')
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

    def env(self, **extra: str) -> dict:
        # Evidence capture has its own tests; these pin the decision output.
        extra.setdefault("CARDINAL_EVIDENCE_CAPTURE", "0")
        return super().env(**extra)

    def cli(self, *args: str, expect_rc: int = 0, env: dict | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, str(DECISION_CLI), *args], cwd=self.repo, env=env or self.env(),
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, expect_rc, proc.stdout + proc.stderr)
        return proc

    def enable(self) -> None:
        self.cli("on")

    def shell_payload(self, command: str, stdout: str, **over) -> dict:
        """postToolUse for Copilot's bash tool: toolArgs.command, the output
        in toolResult.textResultForLlm."""
        body = {"sessionId": SESSION, "toolName": "bash", "toolArgs": {"command": command},
                "toolResult": {"resultType": "success", "textResultForLlm": stdout}}
        body.update(over)
        return self.payload(**body)

    def agent_command(self, args) -> str:
        return f'python3 "{DECISION_CLI}" ' + " ".join(shlex.quote(a) for a in args)

    def agent_records(self, *args: str, **env: str) -> str:
        """The agent runs the CLI, then Copilot fires postToolUse with its output."""
        args = args or self.RECORD_ARGS
        stdout = self.cli(*args).stdout
        return self.context(self.run_hook("postToolUse", self.shell_payload(self.agent_command(args), stdout),
                                          **env))

    @property
    def ledger_dir(self) -> Path:
        return self.copilot / "cardinal" / "decisions" / "sessions"


class SandboxedRecordCliTest(DecisionBase):
    def sandbox_env(self) -> dict:
        site = self.base / "sandbox-site"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(SANDBOX_SITECUSTOMIZE)
        return self.env(PYTHONPATH=str(site))

    def test_record_has_no_side_effects_inside_a_sandbox(self):
        (self.copilot / "cardinal" / "decisions").mkdir(parents=True)
        (self.copilot / "cardinal" / "decisions" / "config.json").write_text('{"enabled": false}')
        env = self.sandbox_env()
        self.copilot.chmod(0)
        try:
            proc = self.cli(*self.RECORD_ARGS, env=env)
        finally:
            self.copilot.chmod(0o755)
        self.assertNotIn("SANDBOX-VIOLATION", proc.stderr)
        self.assertNotIn("capture is off", proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(self.stub.log_batches, [])
        self.assertFalse(self.gh_calls.exists())
        marker = json.loads(marker_line(proc.stdout)[len(MARKER):])
        self.assertEqual(marker["v"], 1)
        self.assertEqual(marker["choice"], "Use mutation-run S3 LSM")
        self.assertEqual(marker["alt"], ["Per-ID rows in Postgres"])
        self.assertEqual(marker["anchor"], ["svc/f3.go::Index.Put", "config:TRACE_INDEX_ENABLED"])
        self.assertEqual(marker["by"], "agent")
        self.assertEqual(marker["cwd"], str(self.repo))
        self.assertIn("proposed id use-mutation-run-s3-lsm", proc.stdout)

    def test_sandbox_trap_actually_fires(self):
        proc = subprocess.run([sys.executable, str(DECISION_CLI), "on"], cwd=self.repo, env=self.sandbox_env(),
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 97)

    def test_validation_errors_exit_2_without_marker(self):
        proc = self.cli("record", "--choice", "Use X", "--refines", "Bad Id", expect_rc=2, env=self.sandbox_env())
        self.assertIn("is not a decision id", proc.stderr)
        self.assertNotIn(MARKER, proc.stdout)


class HookRecordsDecisionTest(DecisionBase):
    def test_hook_emits_tagged_decision(self):
        self.enable()
        context = self.agent_records()
        [(resource, attrs)] = self.records("cardinal.decision")
        self.assertEqual(resource["service.name"], "copilot")
        self.assertEqual(resource["agent.runtime"], "copilot")
        self.assertEqual(attrs["session_id"], SESSION)
        self.assertEqual(attrs["cardinal.decision.id"], "use-mutation-run-s3-lsm")
        self.assertEqual(attrs["cardinal.decision.question"], "How do we index billions of trace IDs?")
        self.assertEqual(attrs["cardinal.decision.decided_by"], "agent")
        self.assertEqual(json.loads(attrs["cardinal.decision.alternatives"]), ["Per-ID rows in Postgres"])
        self.assertEqual(json.loads(attrs["cardinal.decision.anchors"]), [
            {"kind": "symbol", "identifier": "Index.Put", "path": "svc/f3.go"},
            {"kind": "config", "identifier": "TRACE_INDEX_ENABLED"},
        ])
        self.assertEqual(json.loads(attrs["cardinal.decision.code_clusters"]), ["d18:svc"])
        self.assertEqual(attrs["cardinal.repo"], "acme/widgets")
        self.assertEqual(attrs["cardinal.branch"], "feat/copilot-thing")
        self.assertEqual(int(attrs["cardinal.pr_number"]), 42)
        self.assertIn("Cardinal recorded decision use-mutation-run-s3-lsm", context)
        self.assertEqual(len(self.records("cardinal.turn_tool")), 1)

    def test_gate_off_means_no_emit_and_no_ledger(self):
        context = self.agent_records()
        self.assertEqual(self.records("cardinal.decision"), [])
        self.assertFalse(self.ledger_dir.exists())
        self.assertIn("decision capture is off", context)
        self.assertIn("own terminal", context)
        self.assertEqual(len(self.records("cardinal.turn_tool")), 1)

    def test_env_override_forces_gate(self):
        self.agent_records(CARDINAL_DECISIONS="1")
        self.assertEqual(len(self.records("cardinal.decision")), 1)
        self.enable()
        self.agent_records(CARDINAL_DECISIONS="0")
        self.assertEqual(len(self.records("cardinal.decision")), 1)

    def test_tool_args_as_a_json_string(self):
        self.enable()
        args = ("record", "--choice", "Use X")
        stdout = self.cli(*args).stdout
        payload = self.shell_payload(self.agent_command(args), stdout)
        payload["toolArgs"] = json.dumps(payload["toolArgs"])
        self.assertIn("Cardinal recorded decision use-x", self.context(self.run_hook("postToolUse", payload)))

    def test_only_shell_tools_record(self):
        self.enable()
        args = ("record", "--choice", "Use X")
        stdout = self.cli(*args).stdout
        payload = self.shell_payload(self.agent_command(args), stdout, toolName="view")
        self.assertEqual(self.run_hook("postToolUse", payload).stdout, "")
        payload["toolName"] = "powershell"
        self.assertIn("Cardinal recorded decision use-x", self.context(self.run_hook("postToolUse", payload)))

    def test_spoofed_markers_without_a_real_run_are_ignored(self):
        self.enable()
        stdout = self.cli("record", "--choice", "Use X").stdout
        marker = marker_line(stdout)
        spoofs = [
            f"echo {shlex.quote(marker)}",
            f"printf '%s\\n' {shlex.quote(marker)}",
            "grep -h cardinal-decision saved-output.txt",
            "cat saved-output.txt",
            f"echo python3 {DECISION_CLI} record --choice X",
            "grep -r 'cardinal-decision record' .",
        ]
        for command in spoofs:
            with self.subTest(command=command):
                self.assertEqual(self.run_hook("postToolUse", self.shell_payload(command, stdout)).stdout, "")
        self.assertEqual(self.records("cardinal.decision"), [])
        self.assertFalse(self.ledger_dir.exists())

    def test_spoof_beside_a_real_run_records_only_the_real_decision(self):
        self.enable()
        real_args = ("record", "--choice", "Use X")
        real_out = self.cli(*real_args).stdout
        spoof = marker_line(self.cli("record", "--choice", "Evil choice", "--by", "user").stdout)
        command = f"echo {shlex.quote(spoof)} ; {self.agent_command(real_args)}"
        context = self.context(self.run_hook("postToolUse", self.shell_payload(command, spoof + "\n" + real_out)))
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertEqual(attrs["cardinal.decision.choice"], "Use X")
        self.assertEqual(attrs["cardinal.decision.decided_by"], "agent")
        self.assertNotIn("Evil", context)

    def test_legit_invocations_with_quoted_paths_and_env(self):
        self.enable()
        spaced = self.base / "plugin dir" / "scripts"
        spaced.mkdir(parents=True)
        shutil.copy(DECISION_CLI, spaced / "cardinal-decision")
        stdout = self.cli("record", "--choice", "Use X").stdout
        commands = [
            f'python3 "{spaced / "cardinal-decision"}" record --choice "Use X"',
            f'cd "{self.repo}" && python3.12 {shlex.quote(str(DECISION_CLI))} record --choice "Use X"',
            f'env CARDINAL_TRACE=1 python3 -B -X utf8 "{DECISION_CLI}" record --choice "Use X"',
            f'FOO=bar python3 "{DECISION_CLI}" record \\\n  --choice "Use X"',
        ]
        for command in commands:
            with self.subTest(command=command):
                context = self.context(self.run_hook("postToolUse", self.shell_payload(command, stdout)))
                self.assertIn("Cardinal recorded decision use-x", context)
        self.assertEqual(len(self.records("cardinal.decision")), len(commands))

    def test_not_recorded_replies_when_a_run_cannot_be_confirmed(self):
        self.enable()
        cases = {
            "no marker in output": (
                self.agent_command(("record", "--choice", "Use X")),
                "Traceback (most recent call last):\n  PermissionError: denied\n",
                'Cardinal did NOT record the decision "Use X": the command output has no matching confirmation',
            ),
            "only a mismatching marker": (
                self.agent_command(("record", "--choice", "Use X")),
                marker_line(self.cli("record", "--choice", "Something else").stdout),
                'Cardinal did NOT record the decision "Use X"',
            ),
            "invalid argv": (
                f'python3 "{DECISION_CLI}" record --by robot --choice X', "",
                "Cardinal did NOT record the decision: invalid cardinal-decision arguments",
            ),
            "unparseable command": (
                f'python3 "{DECISION_CLI}" record --choice "Use X', "",
                "Cardinal did NOT record the decision: could not parse the shell command",
            ),
        }
        for name, (command, stdout, expected) in cases.items():
            with self.subTest(name):
                self.assertIn(expected, self.context(self.run_hook("postToolUse", self.shell_payload(command, stdout))))
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_links_ledger_and_status(self):
        self.enable()
        self.agent_records("record", "--id", "dir", "--choice", "Exact trace directory")
        context = self.agent_records("record", "--id", "lsm", "--choice", "Mutation-run S3 LSM",
                                     "--supersedes", "dir", "--follows", "elsewhere", "--by", "user")
        self.assertIn("no earlier decision in this session has id elsewhere", context)
        attrs = self.records("cardinal.decision")[-1][1]
        self.assertEqual(attrs["cardinal.decision.decided_by"], "user")
        status = self.cli("status", "--session", SESSION)
        self.assertIn("Decision capture: on", status.stdout)
        self.assertIn("- dir: Exact trace directory [superseded by lsm]", status.stdout)

    def test_not_connected_saves_locally(self):
        (self.copilot / "cardinal-secrets.json").unlink()
        self.enable()
        context = self.agent_records("record", "--choice", "Use X")
        self.assertIn("only saved locally", context)
        self.assertIn("- use-x: Use X", self.cli("status", "--session", SESSION).stdout)
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_staged_notify_and_decision_share_one_output(self):
        self.enable()
        limits_dir = self.copilot / "cardinal" / "limits"
        limits_dir.mkdir(parents=True)
        (limits_dir / f"{SESSION}.notify.json").write_text(json.dumps({"message": "Budget at 60%.", "band": 1}))
        context = self.agent_records("record", "--choice", "Use X")
        self.assertTrue(context.startswith("Budget at 60%."), context)
        self.assertIn("Cardinal recorded decision use-x", context)


class UserCommandsTest(DecisionBase):
    def test_on_off_status_use_the_copilot_runtime_dir(self):
        self.cli("on")
        config = self.copilot / "cardinal" / "decisions" / "config.json"
        self.assertEqual(json.loads(config.read_text()), {"enabled": True})
        status = self.cli("status").stdout
        self.assertIn("Decision capture: on", status)
        self.assertIn("Cardinal telemetry: connected", status)
        self.cli("off")
        self.assertIn("Decision capture: off", self.cli("status").stdout)

    def test_help_marks_user_commands(self):
        out = self.cli("--help").stdout
        self.assertIn("(user, own terminal)", out)
        self.assertIn("(agent)", out)
        self.assertEqual(self.cli(expect_rc=2).returncode, 2)


class SessionStartDecisionContextTest(DecisionBase):
    def start(self, cwd: Path | None = None, **env: str) -> str:
        payload = self.payload(sessionId=SESSION, source="startup", cwd=str(cwd or self.repo))
        return self.context(self.run_hook("sessionStart", payload, cwd=cwd, CARDINAL_STORYBOARD_SESSION_START="0",
                                          **env))

    def test_off_injects_only_the_convention_prompt(self):
        context = self.start()
        self.assertNotIn("decision capture", context)
        self.assertIn("Cardinal-instrumented Copilot CLI session", context)

    def test_on_describes_the_sandbox_safe_flow_and_ledger(self):
        self.enable()
        self.agent_records("record", "--id", "dir", "--choice", "Exact trace directory")
        context = self.start()
        self.assertIn("Cardinal decision capture is on for this session", context)
        self.assertIn(f'python3 "{DECISION_CLI}" record --choice', context)
        self.assertIn("works in Copilot's command sandbox", context)
        self.assertIn("Do not run `cardinal-decision on`, `off`, or `status`", context)
        self.assertIn("- dir: Exact trace directory", context)

    def test_on_outside_a_git_repo_still_injects_decision_context(self):
        self.enable()
        plain = self.base / "plain"
        plain.mkdir()
        context = self.start(plain)
        self.assertTrue(context.startswith("Cardinal decision capture is on"), context)
        self.assertIn("(none yet)", context)

    def test_env_override_off_wins(self):
        self.enable()
        self.assertNotIn("decision capture", self.start(CARDINAL_DECISIONS="0"))


class GitStatePrLinkageTest(DecisionBase):
    def prompt(self) -> dict:
        return self.payload(sessionId=SESSION, prompt="hi")

    def test_pr_keys_on_git_state_and_cached(self):
        self.run_hook("userPromptSubmitted", self.prompt())
        self.run_hook("userPromptSubmitted", self.prompt())
        states = self.records("cardinal.git_state")
        self.assertEqual(len(states), 2)
        attrs = states[0][1]
        self.assertEqual(int(attrs["cardinal_pr_number"]), 42)
        self.assertEqual(attrs["cardinal_pr_url"], "https://github.com/acme/widgets/pull/42")
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)

    def test_no_pr_leaves_keys_absent(self):
        self.write_gh("echo 'no pull requests found' >&2; exit 1")
        self.run_hook("userPromptSubmitted", self.prompt())
        [(_, attrs)] = self.records("cardinal.git_state")
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertEqual(attrs["cardinal_repo"], "acme/widgets")


class InvocationParserTest(unittest.TestCase):
    def setUp(self):
        self.hook = load_script(HOOK, "copilot_telemetry_invocations")

    def test_invocations(self):
        parse = self.hook.decision_invocations
        cases = [
            ('python3 "/a b/cardinal-decision" record --choice X', [["record", "--choice", "X"]]),
            ("/p/scripts/cardinal-decision record --choice X", [["record", "--choice", "X"]]),
            ("cd /repo && python3 cardinal-decision record --choice X; python cardinal-decision record --choice Y",
             [["record", "--choice", "X"], ["record", "--choice", "Y"]]),
            ("env FOO=1 BAR=2 python3 -B -X utf8 cardinal-decision record --choice X", [["record", "--choice", "X"]]),
            ("(python3 cardinal-decision record --choice X)", [["record", "--choice", "X"]]),
            ("python3 -c 'cardinal-decision record'", []),
            ("python3 -m cardinal-decision record", []),
            ("echo cardinal-decision record", []),
            ("grep cardinal-decision out.txt", []),
            ("python3 cardinal-decision status", []),
            ("node cardinal-decision record", []),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(parse(command), (expected, None))

    def test_unparseable(self):
        invocations, error = self.hook.decision_invocations("python3 'cardinal-decision record --choice X")
        self.assertEqual(invocations, [])
        self.assertIn("could not parse", error)
        self.assertEqual(self.hook.decision_invocations("grep cardinal-decision 'x"), ([], None))

    def test_decision_cli_path_is_the_plugins(self):
        self.assertEqual(self.hook.DECISION_CLI, ADAPTER / "scripts" / "cardinal-decision")


if __name__ == "__main__":
    unittest.main()

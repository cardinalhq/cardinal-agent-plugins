"""Tests for PR linkage on cardinal.git_state and opt-in decision capture
in the Cursor adapter.

Decision flow under test: the agent's sandboxed `cardinal-decision record`
only validates and prints a confirmation marker; the postToolUse hook
(outside the sandbox) re-derives the decision from the argv of the command
the agent actually ran, accepts it only when a matching marker confirms
the run, applies the on/off gate, writes the ledger, and hands every
network send (turn_tool, clusters + PR + cardinal.decision) to a detached
background child.

Scripts run as subprocesses with HOME pointed at a temp dir whose
~/.cursor connection state routes OTLP to a local stub, inside a temp git
repo with an origin remote and a stub `gh` on PATH. No network. Hooks run
their background job inline (CARDINAL_CURSOR_BACKGROUND_INLINE=1) except
where a test exercises the detached child.

Run: uv run --no-project --with pytest python -m pytest adapters/cursor/tests/test_decisions.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER = TESTS_DIR.parent
REPO_ROOT = ADAPTER.parent.parent
CLI = ADAPTER / "scripts" / "cardinal-decision"
HOOK = ADAPTER / "hooks" / "cardinal-cursor-telemetry.py"
CONNECT = ADAPTER / "scripts" / "cardinal-connect"
GIT = shutil.which("git") or "/usr/bin/git"
MARKER = "cardinal-decision-record:v1 "

if not (ADAPTER / "hooks" / "cardinal_core" / "decisions.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "cursor"],
                   check=True, capture_output=True)

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


def _load_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _OTLPStub(BaseHTTPRequestHandler):
    received: list = []
    delay = 0.0

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append(json.loads(body))
        if type(self).delay:
            time.sleep(type(self).delay)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def _attrs(kvs: list) -> dict:
    out = {}
    for kv in kvs:
        v = kv["value"]
        out[kv["key"]] = v.get("stringValue", v.get("intValue", v.get("boolValue")))
    return out


def _snapshot(*roots: Path) -> dict:
    out = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                p = Path(dirpath) / name
                try:
                    st = p.lstat()
                except OSError:
                    continue
                out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


def _marker_line(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith(MARKER))


class CursorDecisionBase(unittest.TestCase):
    def setUp(self):
        _OTLPStub.received = []
        _OTLPStub.delay = 0.0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OTLPStub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home = self.root / "home"
        self.cursor = self.home / ".cursor"
        self.cursor.mkdir(parents=True)
        (self.cursor / "cardinal.json").write_text(json.dumps({
            "ingest_endpoint": f"http://127.0.0.1:{self.server.server_port}",
            "deployment_environment": "prod",
            "user_email": "eng1@example.com",
            "org_slug": "acme",
        }))
        (self.cursor / "cardinal-secrets.json").write_text(json.dumps({"ingest_api_key": "test-key"}))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "feat/trace-index"],
                     ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                     ["remote", "add", "origin", "git@github.com:acme/widgets.git"]):
            subprocess.run([GIT, *args], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "svc").mkdir()
        for i in range(12):
            (self.repo / "svc" / f"f{i}.go").write_text("package svc\n")
        subprocess.run([GIT, "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run([GIT, "commit", "-q", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.gh_calls = self.root / "gh-calls"
        self.write_gh('echo \'{"number": 42, "url": "https://github.com/acme/widgets/pull/42"}\'')

    def write_gh(self, body: str) -> None:
        gh = self.bin / "gh"
        gh.write_text(f'#!/bin/sh\necho "$@" >> "{self.gh_calls}"\n{body}\n')
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self.cursor.exists():
            self.cursor.chmod(0o755)
        self.tmp.cleanup()

    def env(self, inline: bool = True, **extra) -> dict:
        path = f"{self.bin}:{os.path.dirname(GIT)}:/usr/bin:/bin"
        env = {"HOME": str(self.home), "PATH": path, **extra}
        if inline:
            env["CARDINAL_CURSOR_BACKGROUND_INLINE"] = "1"
        return env

    def enable(self) -> None:
        self.cli("on")

    def cli(self, *args: str, expect_rc: int = 0, env: dict | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(CLI), *args], cwd=self.repo, env=env or self.env(),
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stdout + proc.stderr)
        return proc

    def hook(self, event: str, payload: dict, inline: bool = True, **env) -> str:
        proc = subprocess.run(
            [sys.executable, str(HOOK), "--event", event], input=json.dumps(payload),
            env=self.env(inline=inline, **env), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def records(self, event_name: str) -> list:
        out = []
        for body in list(_OTLPStub.received):
            resource = _attrs(body["resourceLogs"][0]["resource"]["attributes"])
            for rec in body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
                attrs = _attrs(rec["attributes"])
                if attrs.get("event_name") == event_name:
                    out.append((resource, attrs))
        return out

    def wait_for_records(self, event_name: str, count: int = 1, timeout: float = 15.0) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.records(event_name)
            if len(found) >= count:
                return found
            time.sleep(0.1)
        self.fail(f"background child never delivered {count} {event_name}")

    RECORD_ARGS = (
        "record",
        "--question", "How do we index billions of trace IDs?",
        "--choice", "Use mutation-run S3 LSM",
        "--why", "Global ID-only lookup without per-ID database rows.",
        "--alt", "Per-ID rows in Postgres",
        "--anchor", "svc/f3.go::Index.Put",
        "--anchor", "config:TRACE_INDEX_ENABLED",
    )

    def shell_payload(self, command: str, stdout: str, **overrides) -> dict:
        """postToolUse payload in the shape Cursor documents
        (cursor.com/docs/agent/hooks): tool_name "Shell", tool_input.command,
        tool_output as a JSON string with exitCode/stdout, cwd."""
        payload = {
            "hook_event_name": "postToolUse",
            "conversation_id": "c1",
            "generation_id": "g1",
            "model": "claude-4.5-sonnet",
            "cursor_version": "3.6.0",
            "workspace_roots": [str(self.repo)],
            "tool_name": "Shell",
            "tool_input": {"command": command},
            "tool_output": json.dumps({"exitCode": 0, "stdout": stdout}),
            "tool_use_id": "tool-1",
            "cwd": str(self.repo),
            "duration": 120,
        }
        payload.update(overrides)
        return payload

    def agent_command(self, args) -> str:
        return f'python3 "{CLI}" ' + " ".join(shlex.quote(a) for a in args)

    def agent_records(self, *args: str, **hook_env) -> str:
        """Agent runs the CLI (unsandboxed here), then Cursor fires postToolUse."""
        args = args or self.RECORD_ARGS
        stdout = self.cli(*args).stdout
        return self.hook("postToolUse", self.shell_payload(self.agent_command(args), stdout), **hook_env)

    def context(self, out: str) -> str:
        return json.loads(out)["additional_context"]


class SandboxedRecordCliTest(CursorDecisionBase):
    def sandbox_env(self) -> dict:
        site = self.root / "sandbox-site"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(SANDBOX_SITECUSTOMIZE)
        return self.env(PYTHONPATH=str(site))

    def test_record_has_no_side_effects_inside_sandbox(self):
        # Capture is "off" in ~/.cursor, and ~/.cursor is unreadable: the CLI
        # must neither read it nor report the gate.
        (self.cursor / "cardinal" / "decisions").mkdir(parents=True)
        (self.cursor / "cardinal" / "decisions" / "config.json").write_text('{"enabled": false}')
        env = self.sandbox_env()
        # Snapshot while everything is readable, then deny ~/.cursor.
        before = _snapshot(self.home, self.repo, ADAPTER / "hooks", ADAPTER / "scripts")
        self.cursor.chmod(0)

        proc = self.cli(*self.RECORD_ARGS, env=env)

        self.cursor.chmod(0o755)
        self.assertNotIn("SANDBOX-VIOLATION", proc.stderr)
        self.assertNotIn("capture is off", proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(_snapshot(self.home, self.repo, ADAPTER / "hooks", ADAPTER / "scripts"), before)
        self.assertEqual(_OTLPStub.received, [])
        self.assertFalse(self.gh_calls.exists())

        marker = json.loads(_marker_line(proc.stdout)[len(MARKER):])
        self.assertEqual(marker["v"], 1)
        self.assertEqual(marker["choice"], "Use mutation-run S3 LSM")
        self.assertEqual(marker["why"], "Global ID-only lookup without per-ID database rows.")
        self.assertEqual(marker["alt"], ["Per-ID rows in Postgres"])
        self.assertEqual(marker["anchor"], ["svc/f3.go::Index.Put", "config:TRACE_INDEX_ENABLED"])
        self.assertEqual(marker["by"], "agent")
        self.assertEqual(marker["cwd"], str(self.repo))
        self.assertIsNone(marker["id"])
        self.assertIn("proposed id use-mutation-run-s3-lsm", proc.stdout)

    def test_sandbox_trap_actually_fires(self):
        # Guard against a silently ineffective trap: `on` writes ~/.cursor.
        proc = subprocess.run([sys.executable, str(CLI), "on"], cwd=self.repo, env=self.sandbox_env(),
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 97)

    def test_validation_errors_exit_2_without_marker(self):
        proc = self.cli("record", "--choice", "Use X", "--refines", "Bad Id", expect_rc=2,
                        env=self.sandbox_env())
        self.assertIn("is not a decision id", proc.stderr)
        self.assertNotIn(MARKER, proc.stdout)
        proc = self.cli("record", "--choice", "   ", expect_rc=2, env=self.sandbox_env())
        self.assertIn("--choice is required", proc.stderr)

    def test_record_does_not_emit_even_when_on_and_connected(self):
        # Run Everything mode: unsandboxed CLI, capture on — still no emit.
        self.enable()
        proc = self.cli(*self.RECORD_ARGS)
        self.assertIn(MARKER, proc.stdout)
        self.assertEqual(_OTLPStub.received, [])
        self.assertFalse((self.cursor / "cardinal" / "decisions" / "sessions").exists())
        self.assertFalse(self.gh_calls.exists())


class HookRecordsDecisionTest(CursorDecisionBase):
    def test_hook_emits_tagged_decision_from_documented_payload(self):
        self.enable()
        context = self.context(self.agent_records())

        [(resource, attrs)] = self.records("cardinal.decision")
        self.assertEqual(resource["service.name"], "cursor")
        self.assertEqual(resource["user.email"], "eng1@example.com")
        self.assertEqual(resource["cursor.model"], "claude-4.5-sonnet")
        self.assertEqual(attrs["session_id"], "c1")
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
        self.assertEqual(attrs["cardinal.branch"], "feat/trace-index")
        self.assertEqual(len(attrs["cardinal.head_sha"]), 40)
        self.assertEqual(int(attrs["cardinal.pr_number"]), 42)
        self.assertEqual(attrs["cardinal.pr_url"], "https://github.com/acme/widgets/pull/42")

        self.assertIn("Cardinal recorded decision use-mutation-run-s3-lsm", context)
        # turn_tool telemetry for the same call is unaffected.
        self.assertEqual(len(self.records("cardinal.turn_tool")), 1)

    def test_single_emitter_across_cli_and_hook(self):
        self.enable()
        self.agent_records()
        self.assertEqual(len(self.records("cardinal.decision")), 1)

    def test_gate_off_means_no_emit_and_no_ledger(self):
        context = self.context(self.agent_records())
        self.assertEqual(self.records("cardinal.decision"), [])
        self.assertFalse((self.cursor / "cardinal" / "decisions" / "sessions").exists())
        self.assertIn("decision capture is off", context)
        self.assertIn("own terminal", context)
        self.assertEqual(len(self.records("cardinal.turn_tool")), 1)

    def test_env_override_forces_gate(self):
        self.agent_records(CARDINAL_DECISIONS="1")
        self.assertEqual(len(self.records("cardinal.decision")), 1)
        self.enable()
        self.agent_records(CARDINAL_DECISIONS="0")
        self.assertEqual(len(self.records("cardinal.decision")), 1)

    def test_spoofed_markers_without_a_real_run_are_ignored(self):
        self.enable()
        stdout = self.cli("record", "--choice", "Use X").stdout
        marker = _marker_line(stdout)
        spoofs = [
            f"echo {shlex.quote(marker)}",
            f"printf '%s\\n' {shlex.quote(marker)}",
            "grep -h cardinal-decision saved-output.txt",
            "grep cardinal-decision-record saved-output.txt",
            "cat saved-output.txt",
            f"echo python3 {CLI} record --choice X",
            "grep -r 'cardinal-decision record' .",
        ]
        for command in spoofs:
            with self.subTest(command=command):
                self.assertEqual(self.hook("postToolUse", self.shell_payload(command, stdout)), "")
        self.assertEqual(self.records("cardinal.decision"), [])
        self.assertFalse((self.cursor / "cardinal" / "decisions" / "sessions").exists())

    def _spoof_and_real(self):
        real_args = ("record", "--choice", "Use X")
        real_out = self.cli(*real_args).stdout
        spoof = _marker_line(self.cli("record", "--choice", "Evil choice", "--by", "user").stdout)
        return real_args, real_out, spoof

    def test_spoof_before_real_records_only_the_real_decision(self):
        self.enable()
        real_args, real_out, spoof = self._spoof_and_real()
        command = f"echo {shlex.quote(spoof)} ; {self.agent_command(real_args)}"
        context = self.context(self.hook("postToolUse", self.shell_payload(command, spoof + "\n" + real_out)))
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertEqual(attrs["cardinal.decision.choice"], "Use X")
        self.assertEqual(attrs["cardinal.decision.decided_by"], "agent")
        self.assertNotIn("Evil", context)
        self.assertIn("Cardinal recorded decision use-x", context)

    def test_spoof_after_real_records_only_the_real_decision(self):
        self.enable()
        real_args, real_out, spoof = self._spoof_and_real()
        command = f"{self.agent_command(real_args)} && echo {shlex.quote(spoof)}"
        self.hook("postToolUse", self.shell_payload(command, real_out + spoof + "\n"))
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertEqual(attrs["cardinal.decision.choice"], "Use X")

    def test_spoof_matching_real_content_is_harmless(self):
        self.enable()
        real_args, real_out, _ = self._spoof_and_real()
        echoed = _marker_line(real_out)
        command = f"echo {shlex.quote(echoed)}; {self.agent_command(real_args)}"
        self.hook("postToolUse", self.shell_payload(command, echoed + "\n" + real_out))
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertEqual(attrs["cardinal.decision.choice"], "Use X")

    def test_legit_invocations_with_quoted_paths_newlines_flags_and_env(self):
        self.enable()
        spaced = self.root / "plugin dir" / "scripts"
        spaced.mkdir(parents=True)
        shutil.copy(CLI, spaced / "cardinal-decision")
        stdout = self.cli("record", "--choice", "Use X").stdout
        commands = [
            f'python3 "{spaced / "cardinal-decision"}" record --choice "Use X"',
            f"'{spaced / 'cardinal-decision'}' record --choice 'Use X'",
            f'cd "{self.repo}" && python3.12 {shlex.quote(str(CLI))} record --choice "Use X"',
            f'/usr/bin/python3 "{CLI}" record --choice "Use X" | tee /tmp/out',
            f'cd "{self.repo}"\npython3 "{CLI}" record --choice "Use X"\n',
            f'python3 -u "{CLI}" record --choice "Use X"',
            f'env CARDINAL_TRACE=1 python3 -B -X utf8 "{CLI}" record --choice "Use X"',
            f'FOO=bar python3 "{CLI}" record \\\n  --choice "Use X"',
        ]
        for command in commands:
            with self.subTest(command=command):
                context = self.context(self.hook("postToolUse", self.shell_payload(command, stdout)))
                self.assertIn("Cardinal recorded decision use-x", context)
        self.assertEqual(len(self.records("cardinal.decision")), len(commands))

    def test_two_records_on_separate_lines(self):
        self.enable()
        first = ("record", "--choice", "Use X")
        second = ("record", "--choice", "Use Y")
        stdout = self.cli(*first).stdout + self.cli(*second).stdout
        command = f"{self.agent_command(first)}\n{self.agent_command(second)}"
        self.hook("postToolUse", self.shell_payload(command, stdout))
        choices = [a["cardinal.decision.choice"] for _, a in self.records("cardinal.decision")]
        self.assertEqual(choices, ["Use X", "Use Y"])

    def test_not_recorded_replies_when_a_record_run_cannot_be_confirmed(self):
        self.enable()
        cases = {
            "no marker in output": (
                self.agent_command(("record", "--choice", "Use X")),
                "Traceback (most recent call last):\n  PermissionError: denied\n",
                'Cardinal did NOT record the decision "Use X": the command output has no matching confirmation',
            ),
            "only a mismatching marker": (
                self.agent_command(("record", "--choice", "Use X")),
                _marker_line(self.cli("record", "--choice", "Something else").stdout),
                'Cardinal did NOT record the decision "Use X"',
            ),
            "invalid argv": (
                f'python3 "{CLI}" record --by robot --choice X',
                "",
                "Cardinal did NOT record the decision: invalid cardinal-decision arguments",
            ),
            "unparseable command": (
                f'python3 "{CLI}" record --choice "Use X',
                "",
                "Cardinal did NOT record the decision: could not parse the shell command",
            ),
        }
        for name, (command, stdout, expected) in cases.items():
            with self.subTest(name):
                context = self.context(self.hook("postToolUse", self.shell_payload(command, stdout)))
                self.assertIn(expected, context)
                self.assertNotIn("Traceback", context.replace("Traceback (most", ""))
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_marker_from_other_command_is_ignored(self):
        self.enable()
        stdout = self.cli(*self.RECORD_ARGS).stdout
        out = self.hook("postToolUse", self.shell_payload("cat build.log", stdout))
        self.assertEqual(out, "")
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_legacy_dict_tool_output_shape(self):
        self.enable()
        args = ("record", "--choice", "Use X")
        stdout = self.cli(*args).stdout
        payload = {
            "hook_event_name": "postToolUse", "conversationId": "c1", "generation_id": "g1",
            "workspaceRoots": [str(self.repo)], "toolName": "run_terminal_cmd",
            "toolInput": {"command": f"python3 {CLI} record --choice 'Use X'"},
            "toolOutput": {"exit_code": 0, "stdout": stdout},
        }
        self.hook("postToolUse", payload)
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertEqual(attrs["cardinal.decision.id"], "use-x")

    def test_links_ledger_and_status(self):
        self.enable()
        self.agent_records("record", "--id", "dir", "--choice", "Exact trace directory")
        context = self.context(self.agent_records(
            "record", "--id", "lsm", "--choice", "Mutation-run S3 LSM",
            "--supersedes", "dir", "--follows", "elsewhere", "--by", "user",
        ))
        self.assertIn("no earlier decision in this session has id elsewhere", context)
        attrs = self.records("cardinal.decision")[-1][1]
        self.assertEqual(attrs["cardinal.decision.decided_by"], "user")
        self.assertEqual(json.loads(attrs["cardinal.decision.links"]), [
            {"relation": "follows_from", "to": "elsewhere"},
            {"relation": "supersedes", "to": "dir"},
        ])
        status = self.cli("status", "--session", "c1")
        self.assertIn("Decision capture: on", status.stdout)
        self.assertIn("- dir: Exact trace directory [superseded by lsm]", status.stdout)

    def test_auto_id_made_unique_against_ledger(self):
        self.enable()
        self.agent_records("record", "--choice", "Use X")
        self.agent_records("record", "--choice", "Use X", "--question", "again")
        self.agent_records("record", "--id", "use-x", "--choice", "Use Y")
        ids = [a["cardinal.decision.id"] for _, a in self.records("cardinal.decision")]
        self.assertEqual(ids, ["use-x", "use-x", "use-x"])  # same choice/explicit id = revision
        self.agent_records("record", "--choice", "use x!")
        self.assertEqual(self.records("cardinal.decision")[-1][1]["cardinal.decision.id"], "use-x-2")

    def test_recording_failure_is_reported_not_swallowed(self):
        self.enable()
        decisions_dir = self.cursor / "cardinal" / "decisions"
        (decisions_dir / "sessions").write_text("not a directory")
        context = self.context(self.agent_records("record", "--choice", "Use X"))
        self.assertIn('Cardinal did NOT record the decision "Use X"', context)
        self.assertNotIn("Traceback", context)
        self.assertLess(len(context), 400)
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_not_connected_saves_locally(self):
        (self.cursor / "cardinal-secrets.json").unlink()
        self.enable()
        context = self.context(self.agent_records("record", "--choice", "Use X"))
        self.assertIn("only saved locally", context)
        self.assertIn("- use-x: Use X", self.cli("status", "--session", "c1").stdout)

    def test_notify_and_decision_share_one_output(self):
        self.enable()
        limits_dir = self.cursor / "cardinal" / "limits"
        limits_dir.mkdir(parents=True)
        (limits_dir / "c1.notify.json").write_text(json.dumps({"message": "Budget at 60%.", "band": 1}))
        context = self.context(self.agent_records("record", "--choice", "Use X"))
        self.assertTrue(context.startswith("Budget at 60%."))
        self.assertIn("Cardinal recorded decision use-x", context)


class DetachedBackgroundTest(CursorDecisionBase):
    def test_hook_returns_fast_while_ingest_stalls(self):
        self.enable()
        _OTLPStub.delay = 3.0
        args = ("record", "--choice", "Use X", "--anchor", "svc/f3.go")
        stdout = self.cli(*args).stdout
        payload = self.shell_payload(self.agent_command(args), stdout)

        started = time.monotonic()
        out = self.hook("postToolUse", payload, inline=False)
        elapsed = time.monotonic() - started
        print(f"\npostToolUse hook returned in {elapsed:.3f}s with ingest stalled 3s")

        self.assertLess(elapsed, 1.0, f"hook took {elapsed:.2f}s with ingest stalled")
        self.assertIn("Cardinal recorded decision use-x", self.context(out))
        self.assertTrue((self.cursor / "cardinal" / "decisions" / "sessions").is_dir())

        # The detached child still delivers turn_tool, then the decision
        # with clusters and PR, and removes its spool file.
        self.wait_for_records("cardinal.turn_tool")
        [(_, attrs)] = self.wait_for_records("cardinal.decision")
        self.assertEqual(int(attrs["cardinal.pr_number"]), 42)
        self.assertEqual(json.loads(attrs["cardinal.decision.code_clusters"]), ["d18:svc"])
        self.assertEqual(list((self.cursor / "cardinal" / "spool").glob("*")), [])

    def test_plain_tool_call_also_returns_fast(self):
        _OTLPStub.delay = 3.0
        payload = self.shell_payload("ls -la", "total 0")
        started = time.monotonic()
        self.assertEqual(self.hook("postToolUse", payload, inline=False), "")
        self.assertLess(time.monotonic() - started, 1.0)
        self.wait_for_records("cardinal.turn_tool")


class UserCommandsTest(CursorDecisionBase):
    def test_on_off_status_use_cursor_runtime_dir(self):
        self.cli("on")
        config = self.cursor / "cardinal" / "decisions" / "config.json"
        self.assertEqual(json.loads(config.read_text()), {"enabled": True})
        status = self.cli("status").stdout
        self.assertIn("Decision capture: on", status)
        self.assertIn("Cardinal telemetry: connected", status)
        self.cli("off")
        self.assertIn("Decision capture: off", self.cli("status").stdout)
        self.assertIn("(CARDINAL_DECISIONS=1)", self.cli("status", env=self.env(CARDINAL_DECISIONS="1")).stdout)

    def test_help_marks_user_commands(self):
        out = self.cli("--help").stdout
        self.assertIn("(user, own terminal)", out)
        self.assertIn("(agent)", out)
        self.assertEqual(self.cli(expect_rc=2).returncode, 2)


class SessionStartDecisionContextTest(CursorDecisionBase):
    def payload(self, cwd: Path | None = None) -> dict:
        return {"hook_event_name": "sessionStart", "conversation_id": "c1",
                "session_id": "c1", "workspace_roots": [str(cwd or self.repo)]}

    def test_off_injects_only_convention_prompt(self):
        context = self.context(self.hook("sessionStart", self.payload()))
        self.assertNotIn("decision capture", context)
        self.assertIn("Cardinal-instrumented Cursor session", context)

    def test_on_describes_sandbox_safe_flow_and_ledger(self):
        self.enable()
        self.agent_records("record", "--id", "dir", "--choice", "Exact trace directory")
        context = self.context(self.hook("sessionStart", self.payload()))
        self.assertTrue(context.startswith("You are running inside a Cardinal-instrumented Cursor session"))
        self.assertIn("Cardinal decision capture is on for this session", context)
        self.assertIn(f'python3 "{CLI}" record --choice', context)
        self.assertNotIn("--session", context)
        self.assertIn("works in the sandbox", context)
        self.assertIn("ask to run it outside the sandbox", context)
        self.assertIn("Do not run `cardinal-decision on`, `off`, or", context)
        self.assertIn("the user in their own terminal", context)
        self.assertIn("- dir: Exact trace directory", context)

    def test_on_outside_git_repo_still_injects_decision_context(self):
        self.enable()
        plain = self.home / "plain"
        plain.mkdir()
        context = self.context(self.hook("sessionStart", self.payload(plain)))
        self.assertTrue(context.startswith("Cardinal decision capture is on"))
        self.assertIn("(none yet)", context)

    def test_off_outside_git_repo_is_silent(self):
        plain = self.home / "plain"
        plain.mkdir()
        self.assertEqual(self.hook("sessionStart", self.payload(plain)), "")

    def test_env_override_off_wins(self):
        self.enable()
        context = self.context(self.hook("sessionStart", self.payload(), CARDINAL_DECISIONS="0"))
        self.assertNotIn("decision capture", context)


class GitStatePrLinkageTest(CursorDecisionBase):
    def prompt(self) -> dict:
        return {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c1",
                "workspace_roots": [str(self.repo)], "prompt": "hi"}

    def test_pr_keys_on_git_state(self):
        self.hook("beforeSubmitPrompt", self.prompt())
        [(_, attrs)] = self.records("cardinal.git_state")
        self.assertEqual(attrs["cardinal_branch"], "feat/trace-index")
        self.assertEqual(int(attrs["cardinal_pr_number"]), 42)
        self.assertEqual(attrs["cardinal_pr_url"], "https://github.com/acme/widgets/pull/42")

    def test_pr_cached_across_prompts(self):
        self.hook("beforeSubmitPrompt", self.prompt())
        self.hook("beforeSubmitPrompt", self.prompt())
        self.assertEqual(len(self.records("cardinal.git_state")), 2)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)

    def test_no_pr_leaves_keys_absent(self):
        self.write_gh("echo 'no pull requests found' >&2; exit 1")
        self.hook("beforeSubmitPrompt", self.prompt())
        [(_, attrs)] = self.records("cardinal.git_state")
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertNotIn("cardinal_pr_url", attrs)
        self.assertEqual(attrs["cardinal_repo"], "acme/widgets")

    def test_missing_gh_never_fails_the_record(self):
        (self.bin / "gh").unlink()
        self.hook("beforeSubmitPrompt", self.prompt())
        [(_, attrs)] = self.records("cardinal.git_state")
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertEqual(attrs["cardinal_branch"], "feat/trace-index")

    def test_protected_branch_skips_gh(self):
        subprocess.run([GIT, "checkout", "-q", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        self.hook("beforeSubmitPrompt", self.prompt())
        [(_, attrs)] = self.records("cardinal.git_state")
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertFalse(self.gh_calls.exists())


class InvocationParserTest(unittest.TestCase):
    """Unit coverage for the shell-aware `cardinal-decision record` parser."""

    def setUp(self):
        self.hook = _load_module("cursor_telemetry_invocations", HOOK)

    def test_invocations(self):
        parse = self.hook.decision_invocations
        cases = [
            ('python3 "/a b/cardinal-decision" record --choice X', [["record", "--choice", "X"]]),
            ("/p/scripts/cardinal-decision record --choice X", [["record", "--choice", "X"]]),
            ("python3.11 cardinal-decision record --choice 'a; b'", [["record", "--choice", "a; b"]]),
            ("cd /repo && python3 cardinal-decision record --choice X; python cardinal-decision record --choice Y",
             [["record", "--choice", "X"], ["record", "--choice", "Y"]]),
            ("cd /repo\npython3 -u cardinal-decision record --choice X\n", [["record", "--choice", "X"]]),
            ("env FOO=1 BAR=2 python3 -B -X utf8 cardinal-decision record --choice X", [["record", "--choice", "X"]]),
            ("FOO=1 /usr/bin/env -u BAZ python3 -Ximporttime cardinal-decision record --choice X",
             [["record", "--choice", "X"]]),
            ("python3 cardinal-decision record \\\n  --choice X", [["record", "--choice", "X"]]),
            ("(python3 cardinal-decision record --choice X)", [["record", "--choice", "X"]]),
            ("python3 cardinal-decision record --why 'line one\nline two' --choice X",
             [["record", "--why", "line one\nline two", "--choice", "X"]]),
            ("python3 -c 'cardinal-decision record'", []),
            ("python3 -uc 'print(1)' cardinal-decision record", []),
            ("python3 -m cardinal-decision record", []),
            ("echo cardinal-decision record", []),
            ("printf 'cardinal-decision-record:v1 {}'", []),
            ("grep cardinal-decision out.txt", []),
            ("cat out.txt | grep cardinal-decision", []),
            ("python3 cardinal-decision status", []),
            ("python3 cardinal-decision-record record", []),
            ("node cardinal-decision record", []),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(parse(command), (expected, None))

    def test_unparseable(self):
        invocations, error = self.hook.decision_invocations("python3 'cardinal-decision record --choice X")
        self.assertEqual(invocations, [])
        self.assertIn("could not parse", error)
        # Unbalanced quotes in an unrelated command stay silent.
        self.assertEqual(self.hook.decision_invocations("grep cardinal-decision 'x"), ([], None))


class ResolvePrTimeoutTest(unittest.TestCase):
    """The synchronous beforeSubmitPrompt git_state path bounds the gh call."""

    def test_timeout_is_bounded_and_errors_swallowed(self):
        hook = _load_module("cursor_telemetry_pr", HOOK)
        self.assertLessEqual(hook.PR_RESOLVE_TIMEOUT_SEC, 1.5)
        with mock.patch.object(hook.decisions, "resolve_pr", return_value=(9, "u")) as rp:
            self.assertEqual(hook.resolve_pr("/x", "a/b", "feat/y"), (9, "u"))
        self.assertEqual(rp.call_args.kwargs["timeout"], hook.PR_RESOLVE_TIMEOUT_SEC)
        with mock.patch.object(hook.decisions, "resolve_pr", side_effect=RuntimeError("boom")):
            self.assertEqual(hook.resolve_pr("/x", "a/b", "feat/y"), (None, None))


class RecordingHookInstallTest(unittest.TestCase):
    """postToolUse (the recorder) is registered at both user and --project
    hooks.json locations."""

    def test_post_tool_use_registered_user_and_project(self):
        with TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            connect = _load_module("cardinal_connect_decisions", CONNECT)
            user = Path(tmp) / ".cursor" / "hooks.json"
            project = Path(tmp) / "repo" / ".cursor" / "hooks.json"
            for path in (user, project):
                connect.write_hooks_config(path)
                [entry] = json.loads(path.read_text())["hooks"]["postToolUse"]
                self.assertIn(str(HOOK), entry["command"])
                self.assertIn("--event postToolUse", entry["command"])
                self.assertIn("cardinal-cursor-plugin", entry["command"])
                self.assertGreaterEqual(entry["timeout"], 10)
                # Re-running connect replaces, never duplicates, the recorder.
                connect.write_hooks_config(path)
                self.assertEqual(len(json.loads(path.read_text())["hooks"]["postToolUse"]), 1)


if __name__ == "__main__":
    unittest.main()

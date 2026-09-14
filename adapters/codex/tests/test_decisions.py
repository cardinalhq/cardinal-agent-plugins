"""Decision capture + PR linkage for the Codex adapter.

Design under test (see hooks/_codex_decisions.py): Codex sandboxes the
agent's shell commands (no network, no writes to ~/.codex), so
`cardinal-decision record` only validates and prints a
`cardinal-decision-record:v1 {json}` marker (the Cursor adapter's wire
format) without touching ~/.codex, gh or the network. The telemetry
hook's PostToolUse handler is the single cardinal.decision emitter: gate,
re-validation, final id, ledger, emission, report back to the agent.
`record --emit` is the human terminal path and prints no marker.
UserPromptSubmit injects the recording instructions (only when the
PostToolUse hook is registered) and reads PR facts from cache only,
refreshing it in a detached child.

Every subprocess runs with HOME in a temp dir holding a prefabricated
~/.codex connection pointing at a local OTLP stub, and a stub `gh` on PATH
(tests/fixtures.py) that logs its calls — no network.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent   # adapters/codex
REPO_ROOT = ROOT.parent.parent
HOOK = ROOT / "hooks" / "cardinal-codex-telemetry.py"
CLI = ROOT / "scripts" / "cardinal-decision"
MARKER = "cardinal-decision-record:v1 "
MARKER_KEYS = {"v", "session", "cwd", "id", "choice", "question", "why", "alt", "by",
               "anchor", "follows", "refines", "supersedes"}
REPO = "acme/widgets"
BRANCH = "feat/trace-index"


def setUpModule() -> None:  # noqa: N802
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "codex"],
        check=True, capture_output=True,
    )


def _attrs(kvs: list) -> dict:
    return {kv["key"]: next(iter(kv["value"].values())) for kv in kvs}


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def reported(proc: subprocess.CompletedProcess) -> str:
    """additionalContext the PostToolUse hook returned to the agent."""
    if not proc.stdout.strip():
        return ""
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse", out
    return out["hookSpecificOutput"]["additionalContext"]


class DecisionTestBase(unittest.TestCase):
    def setUp(self):
        self.received: list[dict] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/v1/logs":
                    received.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()
        fixtures.write_connection(self.home, f"http://127.0.0.1:{self.server.server_port}")
        self.register_post_tool_use()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git_env = {**os.environ, **fixtures.GIT_ENV}
        for args in (["init", "-q", "-b", BRANCH],
                     ["remote", "add", "origin", f"git@github.com:{REPO}.git"]):
            subprocess.run(["git", *args], cwd=self.repo, env=git_env, check=True, capture_output=True)
        (self.repo / "svc").mkdir()
        for i in range(12):
            (self.repo / "svc" / f"f{i}.go").write_text("package svc\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, env=git_env, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, env=git_env,
                       check=True, capture_output=True)
        self.bin = fixtures.install_gh_stub(self.home)
        self.gh_log = self.root / "gh.log"
        # A fresh cache entry keeps prompt runs from spawning background
        # refreshes that could outlive the test; PR tests remove it.
        fixtures.seed_pr_cache(self.home, REPO, BRANCH)

    def tearDown(self):
        self.restore_codex_perms()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    # --- helpers --------------------------------------------------------

    def restore_codex_perms(self) -> None:
        codex = self.home / ".codex"
        codex.chmod(0o755)
        for path in codex.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)

    def register_post_tool_use(self, registered: bool = True) -> None:
        hooks = {"hooks": {}}
        if registered:
            hooks["hooks"]["PostToolUse"] = [{"matcher": "Bash", "hooks": [{
                "type": "command", "timeout": 15,
                "command": "python3 /x/cardinal-codex-telemetry.py --event PostToolUse # cardinal-codex-plugin",
            }]}]
        (self.home / ".codex" / "hooks.json").write_text(json.dumps(hooks))

    def env(self, **extra: str) -> dict:
        env = os.environ.copy()
        for k in ("CODEX_SESSION_ID", "OPENAI_CODEX_SESSION_ID", "CARDINAL_DECISIONS",
                  "CARDINAL_FIXTURE_GH"):
            env.pop(k, None)
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["CARDINAL_FIXTURE_GH_LOG"] = str(self.gh_log)
        env.update(extra)
        return env

    def cli(self, *args: str, expect_rc: int = 0, **env: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(CLI), *args], cwd=self.repo, env=self.env(**env),
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stdout + proc.stderr)
        return proc

    def hook(self, event: str, payload: dict, **env: str) -> subprocess.CompletedProcess:
        proc = fixtures.run_hook(HOOK, event, self.home, payload,
                                 env_extra={"CARDINAL_FIXTURE_GH_LOG": str(self.gh_log), **env})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def post_tool_use(self, command: str, response: str, session: str | None = "s1",
                      tool_name: str = "Bash", **env: str) -> subprocess.CompletedProcess:
        """A PostToolUse stdin shaped like Codex's PostToolUseRequest for
        exec_command (tool_input.command, tool_response = output string)."""
        payload = {
            "turn_id": "turn-1", "cwd": str(self.repo),
            "transcript_path": None, "model": "gpt-5-codex", "permission_mode": "default",
            "hook_event_name": "PostToolUse", "tool_name": tool_name,
            "tool_use_id": "call-1", "tool_input": {"command": command},
            "tool_response": response,
        }
        if session:
            payload["session_id"] = session
        return self.hook("PostToolUse", payload, **env)

    def record(self, *args: str, session: str = "s1",
               **env: str) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess]:
        """The agent's sandboxed call, then Codex's PostToolUse delivery."""
        argv = ["record", "--session", session, *args]
        proc = self.cli(*argv, **env)
        hook = self.post_tool_use(shlex.join(["python3", str(CLI), *argv]), proc.stdout,
                                  session=session, **env)
        return proc, hook

    def prompt(self, session_id: str = "s1", **env: str) -> subprocess.CompletedProcess:
        return self.hook("UserPromptSubmit", {
            "session_id": session_id, "cwd": str(self.repo), "prompt": "next",
        }, **env)

    def records(self, event: str) -> list[tuple[dict, dict]]:
        out = []
        for body in self.received:
            resource = _attrs(body["resourceLogs"][0]["resource"]["attributes"])
            for rec in body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
                attrs = _attrs(rec["attributes"])
                if attrs.get("event_name") == event:
                    out.append((resource, attrs))
        return out

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(p.relative_to(self.home)): p.read_bytes()
            for p in sorted(self.home.rglob("*")) if p.is_file()
        }

    def pr_cache(self) -> dict:
        path = self.home / ".codex" / "cardinal" / "decisions" / "cache" / "prs.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def clear_pr_cache(self) -> None:
        (self.home / ".codex" / "cardinal" / "decisions" / "cache" / "prs.json").unlink()


class SandboxedCliTest(DecisionTestBase):
    def test_record_prints_cursor_compatible_marker(self):
        proc = self.cli(
            "record", "--session", "s1", "--choice", "  Use mutation-run   S3 LSM ",
            "--question", "How do we index trace IDs?", "--why", "Cheap lookups.",
            "--alt", "Per-ID rows", "--anchor", "svc/f3.go::Index.Put",
            "--supersedes", "dir", "--by", "user",
        )
        first, second = proc.stdout.splitlines()[:2]
        self.assertTrue(first.startswith(MARKER), proc.stdout)
        body = json.loads(first[len(MARKER):])
        self.assertEqual(set(body), MARKER_KEYS)
        self.assertEqual(body["v"], 1)
        self.assertEqual(body["session"], "s1")
        self.assertEqual(body["cwd"], str(self.repo))
        self.assertIsNone(body["id"], "id is only sent when --id was given")
        self.assertEqual(body["choice"], "Use mutation-run S3 LSM")
        self.assertEqual(body["why"], "Cheap lookups.")
        self.assertEqual(body["alt"], ["Per-ID rows"])
        self.assertEqual(body["by"], "user")
        self.assertEqual(body["anchor"], ["svc/f3.go::Index.Put"])
        self.assertEqual((body["follows"], body["refines"], body["supersedes"]), ([], [], ["dir"]))
        self.assertIn("proposed id use-mutation-run-s3-lsm", second)
        self.assertEqual(json.loads(self.cli("record", "--choice", "X", "--id", "Pick")
                                    .stdout.splitlines()[0][len(MARKER):])["id"], "pick")

    def test_record_touches_no_codex_state_gh_or_network(self):
        before = self.snapshot()
        started = time.time() - 1
        # Deny even reads of ~/.codex: record must not need them (capture
        # state is the hook's to check).
        (self.home / ".codex").chmod(0)
        try:
            proc = self.cli("record", "--session", "s1", "--choice", "Use X",
                            "--anchor", "svc/f3.go")
        finally:
            self.restore_codex_perms()
        self.assertTrue(proc.stdout.startswith(MARKER), proc.stdout + proc.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.gh_log.exists(), "record must not run gh")
        self.assertEqual(self.received, [], "record must not POST")
        # No bytecode written next to the plugin (outside sandbox roots).
        fresh_pyc = [p for p in ROOT.rglob("*.pyc") if p.stat().st_mtime >= started]
        self.assertEqual(fresh_pyc, [])

    def test_on_off_explain_terminal_path_when_sandboxed(self):
        self.cli("on")
        decisions_dir = self.home / ".codex" / "cardinal" / "decisions"
        decisions_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        proc = self.cli("off", expect_rc=1)
        self.assertIn("own terminal", proc.stderr)
        self.assertIn(f"python3 {shlex.quote(str(CLI.resolve()))} off", proc.stderr)
        decisions_dir.chmod(0o755)
        self.assertIn("Decision capture: on", self.cli("status").stdout)

    def test_usage_errors(self):
        proc = self.cli("record", "--session", "s1", "--choice", "Use X", "--refines", "Bad Id", expect_rc=2)
        self.assertIn("is not a decision id", proc.stderr)
        self.assertNotIn(MARKER, proc.stdout)
        self.cli("record", "--session", "s1", expect_rc=2)  # --choice required
        proc = self.cli("record", "--session", "s1", "--choice", "X", "--emit", expect_rc=3)
        self.assertIn("capture is off", proc.stderr)

    def test_status_and_help(self):
        status = self.cli("status").stdout
        self.assertIn("Decision capture: off", status)
        self.assertIn("Cardinal telemetry: connected", status)
        self.assertIn("Recording hook (PostToolUse): registered", status)
        self.assertIn(f"python3 {shlex.quote(str(CLI.resolve()))} on", status)
        self.register_post_tool_use(False)
        self.assertIn("--repair-hooks", self.cli("status").stdout)
        self.assertIn("record", self.cli("--help").stdout)
        self.cli(expect_rc=2)


class HookSideEmissionTest(DecisionTestBase):
    def test_post_tool_use_emits_tagged_decision_and_reports_back(self):
        self.cli("on")
        _, hook = self.record(
            "--question", "How do we index billions of trace IDs?",
            "--choice", "Use mutation-run S3 LSM",
            "--why", "Global ID-only lookup without per-ID database rows.",
            "--alt", "Per-ID rows in Postgres",
            "--anchor", "svc/f3.go::Index.Put",
            "--anchor", "config:TRACE_INDEX_ENABLED",
        )
        report = reported(hook)
        self.assertIn("Cardinal recorded decision use-mutation-run-s3-lsm", report)
        self.assertIn(f"PR #{fixtures.FIXTURE_PR_NUMBER}", report)
        self.assertIn("clusters d18:svc", report)
        [(resource, attrs)] = self.records("cardinal.decision")
        self.assertEqual(resource["service.name"], "codex")
        self.assertEqual(resource["agent.runtime"], "codex")
        self.assertEqual(resource["user.email"], "golden@example.com")
        self.assertEqual(resource["cardinal.org"], "golden-org")
        self.assertEqual(attrs["session_id"], "s1")
        self.assertEqual(attrs["cardinal.decision.id"], "use-mutation-run-s3-lsm")
        self.assertEqual(attrs["cardinal.decision.question"], "How do we index billions of trace IDs?")
        self.assertEqual(attrs["cardinal.decision.rationale"],
                         "Global ID-only lookup without per-ID database rows.")
        self.assertEqual(attrs["cardinal.decision.decided_by"], "agent")
        self.assertEqual(json.loads(attrs["cardinal.decision.alternatives"]), ["Per-ID rows in Postgres"])
        self.assertEqual(json.loads(attrs["cardinal.decision.anchors"]), [
            {"kind": "symbol", "identifier": "Index.Put", "path": "svc/f3.go"},
            {"kind": "config", "identifier": "TRACE_INDEX_ENABLED"},
        ])
        self.assertEqual(json.loads(attrs["cardinal.decision.code_clusters"]), ["d18:svc"])
        self.assertEqual(attrs["cardinal.repo"], REPO)
        self.assertEqual(attrs["cardinal.branch"], BRANCH)
        self.assertEqual(len(attrs["cardinal.head_sha"]), 40)
        self.assertEqual(int(attrs["cardinal.pr_number"]), fixtures.FIXTURE_PR_NUMBER)
        self.assertEqual(attrs["cardinal.pr_url"], fixtures.FIXTURE_PR_URL)
        self.assertTrue(
            (self.home / ".codex" / "cardinal" / "decisions" / "sessions" / "s1.json").is_file())

    def test_capture_off_is_reported_not_recorded(self):
        _, hook = self.record("--choice", "Use X")
        self.assertIn("capture is off", reported(hook))
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_links_ledger_and_final_id(self):
        self.cli("on")
        self.record("--id", "dir", "--choice", "Exact trace directory")
        _, hook = self.record("--id", "lsm", "--choice", "Mutation-run S3 LSM",
                              "--supersedes", "dir", "--follows", "elsewhere", "--by", "user")
        self.assertIn("no earlier decision in this session has id elsewhere", reported(hook))
        attrs = self.records("cardinal.decision")[-1][1]
        self.assertEqual(attrs["cardinal.decision.decided_by"], "user")
        self.assertEqual(json.loads(attrs["cardinal.decision.links"]), [
            {"relation": "follows_from", "to": "elsewhere"},
            {"relation": "supersedes", "to": "dir"},
        ])
        status = self.cli("status", "--session", "s1")
        self.assertIn("- dir: Exact trace directory [superseded by lsm]", status.stdout)
        # Same auto id for a different choice: the hook makes it unique.
        self.record("--choice", "Use X")
        _, hook = self.record("--choice", "Use  X!")
        self.assertIn("Cardinal recorded decision use-x-2", reported(hook))

    def test_hook_uses_payload_session_only(self):
        self.cli("on")
        output = self.cli("record", "--choice", "Use X", CODEX_SESSION_ID="env-sess").stdout
        self.post_tool_use("python3 cardinal-decision record --choice 'Use X'", output,
                           session="thread-1")
        self.assertEqual([a["session_id"] for _, a in self.records("cardinal.decision")], ["thread-1"])
        output = self.cli("record", "--choice", "Use Y", CODEX_SESSION_ID="env-sess").stdout
        hook = self.post_tool_use("python3 cardinal-decision record --choice 'Use Y'", output, session=None)
        self.assertIn("did NOT record", reported(hook))
        self.assertIn("no session id", reported(hook))
        self.assertEqual(len(self.records("cardinal.decision")), 1)

    def test_hook_ignores_unrelated_payloads(self):
        self.cli("on")
        output = self.cli("record", "--session", "s1", "--choice", "Use X").stdout
        cases = [
            ("python3 cardinal-decision record", output, "apply_patch"),  # not a shell call
            ("python3 cardinal-decision record", "  x " + output, "Bash"),  # marker not at line start
            ("python3 cardinal-decision record", MARKER + "{not json", "Bash"),
            ("python3 cardinal-decision record", MARKER + '{"v":2,"choice":"X","cwd":"/"}', "Bash"),
        ]
        for command, response, tool in cases:
            with self.subTest(command=command, tool=tool, response=response[:20]):
                self.assertEqual(reported(self.post_tool_use(command, response, tool_name=tool)), "")
        self.assertEqual(self.records("cardinal.decision"), [])
        self.assertFalse((self.home / ".codex" / "cardinal" / "decisions" / "sessions").exists())

    def test_spoofed_markers_are_not_recorded(self):
        self.cli("on")
        marker_line = self.cli("record", "--session", "s1", "--choice", "Use X").stdout.splitlines()[0]
        log = self.root / "cardinal-decision.log"
        spoofs = [
            f"echo {shlex.quote(marker_line)}",
            f"printf '%s\\n' {shlex.quote(marker_line)}",
            f"grep cardinal-decision {log}",
            f"grep -h cardinal-decision-record {log} | head -1",
            f"cat {log}",
            "cat /opt/plugin/scripts/cardinal-decision",
            "echo cardinal-decision record && cat notes.txt",
            "rg 'cardinal-decision record' .",
        ]
        for command in spoofs:
            with self.subTest(command=command):
                self.assertEqual(reported(self.post_tool_use(command, marker_line + "\n")), "")
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_legitimate_invocation_forms_are_recorded(self):
        self.cli("on")
        forms = [
            'python3 "/opt/my plugins/cardinal/scripts/cardinal-decision" record --session s1 --choice "A"',
            f"cd {shlex.quote(str(self.repo))} && python3 -u /opt/p/cardinal-decision record --choice B",
            "CARDINAL_X=1 /opt/p/scripts/cardinal-decision record --choice C 2>&1 | tail -5",
            "python3.11 '/opt/p/cardinal-decision' record --choice D; echo done",
        ]
        for n, command in enumerate(forms):
            with self.subTest(command=command):
                output = self.cli("record", "--choice", f"Choice {n}").stdout
                self.assertIn(f"Cardinal recorded decision choice-{n}",
                              reported(self.post_tool_use(command, output)))
        self.assertEqual(len(self.records("cardinal.decision")), len(forms))

    def test_recording_failure_is_reported_not_silent(self):
        self.cli("on")
        decisions_dir = self.home / ".codex" / "cardinal" / "decisions"
        decisions_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # sessions/ can't be created
        _, hook = self.record("--choice", "Use X")
        decisions_dir.chmod(0o755)
        report = reported(hook)
        self.assertIn("Cardinal did NOT record the decision", report)
        self.assertIn("PermissionError", report)
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_decision_path_uses_cached_pr_only(self):
        self.cli("on")
        self.clear_pr_cache()
        started = time.monotonic()
        _, hook = self.record("--choice", "Use X", CARDINAL_FIXTURE_GH="slow")
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertNotIn("PR #", reported(hook))
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertNotIn("cardinal.pr_number", attrs)
        # A detached refresh populates the cache for later decisions.
        wait_for(lambda: f"{REPO}#{BRANCH}" in self.pr_cache(), timeout=12)

    def test_no_double_emission(self):
        self.cli("on")
        # Marker path: nothing before the hook, exactly one after — even
        # when the marker appears twice in the tool response.
        argv = ["record", "--session", "s1", "--choice", "Use X"]
        proc = self.cli(*argv)
        self.assertEqual(self.records("cardinal.decision"), [])
        self.post_tool_use(shlex.join(["python3", str(CLI), *argv]), proc.stdout + proc.stdout)
        self.assertEqual(len(self.records("cardinal.decision")), 1)
        # --emit (unsandboxed terminal, or danger-full-access): the CLI
        # emits once and prints no marker, so PostToolUse adds nothing.
        argv = ["record", "--session", "s1", "--choice", "Use Y", "--emit"]
        proc = self.cli(*argv)
        self.assertIn("Recorded decision use-y", proc.stdout)
        self.assertIn(f"PR #{fixtures.FIXTURE_PR_NUMBER}", proc.stdout)
        self.assertNotIn(MARKER, proc.stdout)
        self.assertEqual(len(self.records("cardinal.decision")), 2)
        hook = self.post_tool_use(shlex.join(["python3", str(CLI), *argv]), proc.stdout)
        self.assertEqual(reported(hook), "")
        self.assertEqual(
            [a["cardinal.decision.id"] for _, a in self.records("cardinal.decision")],
            ["use-x", "use-y"],
        )

    def test_emit_saved_locally_when_not_connected(self):
        (self.home / ".codex" / "cardinal-secrets.json").unlink()
        self.cli("on")
        proc = self.cli("record", "--session", "s1", "--choice", "Use X", "--emit")
        self.assertIn("only saved locally", proc.stdout)
        self.assertEqual(self.records("cardinal.decision"), [])
        self.assertIn("not connected", self.cli("status").stdout)


class DecisionPromptInjectionTest(DecisionTestBase):
    def test_silent_when_off(self):
        self.assertEqual(self.prompt().stdout, "")

    def test_silent_when_post_tool_use_not_registered(self):
        self.cli("on")
        self.register_post_tool_use(False)
        self.assertEqual(self.prompt().stdout, "")

    def test_injects_protocol_and_ledger_when_on(self):
        self.cli("on")
        self.record("--id", "dir", "--choice", "Exact trace directory")
        out = json.loads(self.prompt().stdout)
        specific = out["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        context = specific["additionalContext"]
        self.assertIn(f"python3 {shlex.quote(str(CLI.resolve()))} record --session s1", context)
        self.assertNotIn("--emit", context)
        self.assertIn("- dir: Exact trace directory", context)
        self.assertNotIn("decision", out)

    def test_instructed_command_records_end_to_end(self):
        self.cli("on")
        context = json.loads(self.prompt(session_id="sess-run").stdout)[
            "hookSpecificOutput"]["additionalContext"]
        line = next(l for l in context.splitlines() if " record --session " in l)
        command = line.split(" --choice ", 1)[0] + " --choice 'Use the ledger'"
        argv = shlex.split(command)
        argv[0] = sys.executable
        proc = subprocess.run(argv, cwd=self.repo, env=self.env(), capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.post_tool_use(command, proc.stdout, session="sess-run")
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertEqual(attrs["session_id"], "sess-run")
        self.assertEqual(attrs["cardinal.decision.choice"], "Use the ledger")

    def test_env_override_enables_injection(self):
        out = json.loads(self.prompt(CARDINAL_DECISIONS="1").stdout)
        self.assertIn("(none yet)", out["hookSpecificOutput"]["additionalContext"])

    def _write_verdict(self, session_id: str, verdict: dict) -> None:
        limits = self.home / ".codex" / "cardinal" / "limits"
        limits.mkdir(parents=True, exist_ok=True)
        (limits / f"{session_id}.verdict.json").write_text(
            json.dumps({"fetched_at": time.time(), **verdict}))

    def test_merges_with_spend_gate_warning(self):
        self.cli("on")
        self._write_verdict("s1", {
            "decision": "warn", "band": 2,
            "agent_context": "Economize: budget at 90%.",
            "user_message": "Cardinal: session budget at 90%.",
        })
        out = json.loads(self.prompt().stdout)
        self.assertEqual(out["systemMessage"], "Cardinal: session budget at 90%.")
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("Economize: budget at 90%.\n\n"), context)
        self.assertIn("Cardinal decision capture is on", context)

    def test_block_verdict_is_not_diluted(self):
        self.cli("on")
        self._write_verdict("s1", {
            "decision": "block", "band": 3, "block_reason": "Session budget of $100 reached.",
        })
        out = json.loads(self.prompt().stdout)
        self.assertEqual(out, {"decision": "block", "reason": "Session budget of $100 reached."})


class GitStatePrLinkageTest(DecisionTestBase):
    def git_state(self) -> dict:
        return self.records("cardinal.git_state")[-1][1]

    def test_cached_pr_is_used_without_running_gh(self):
        self.prompt()
        attrs = self.git_state()
        self.assertEqual(int(attrs["cardinal_pr_number"]), fixtures.FIXTURE_PR_NUMBER)
        self.assertEqual(attrs["cardinal_pr_url"], fixtures.FIXTURE_PR_URL)
        self.assertEqual(attrs["cardinal_repo"], REPO)
        self.assertFalse(self.gh_log.exists())

    def test_cache_miss_is_refreshed_in_background(self):
        self.clear_pr_cache()
        self.prompt()
        self.assertNotIn("cardinal_pr_number", self.git_state())
        key = f"{REPO}#{BRANCH}"
        self.assertTrue(wait_for(lambda: key in self.pr_cache()), "background refresh never ran")
        self.prompt()
        self.assertEqual(int(self.git_state()["cardinal_pr_number"]), fixtures.FIXTURE_PR_NUMBER)

    def test_no_pr_keys_absent_and_negative_cached(self):
        self.clear_pr_cache()
        self.prompt(CARDINAL_FIXTURE_GH="none")
        key = f"{REPO}#{BRANCH}"
        self.assertTrue(wait_for(lambda: key in self.pr_cache()))
        self.prompt(CARDINAL_FIXTURE_GH="none")
        attrs = self.git_state()
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertNotIn("cardinal_pr_url", attrs)
        self.assertEqual(attrs["cardinal_branch"], BRANCH)
        time.sleep(0.5)
        self.assertEqual(len(self.gh_log.read_text().splitlines()), 1, "fresh miss must not re-run gh")

    def test_slow_gh_never_blocks_the_prompt(self):
        self.clear_pr_cache()
        started = time.monotonic()
        self.prompt(CARDINAL_FIXTURE_GH="slow")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0, "UserPromptSubmit must not wait on gh")
        self.assertNotIn("cardinal_pr_number", self.git_state())
        # Let the detached child finish before the temp HOME goes away.
        wait_for(lambda: f"{REPO}#{BRANCH}" in self.pr_cache(), timeout=12)

    def test_stale_entry_is_used_and_refreshed(self):
        fixtures.seed_pr_cache(self.home, REPO, BRANCH, number=7,
                               url="https://github.com/acme/widgets/pull/7", at=time.time() - 3600)
        self.prompt()
        self.assertEqual(int(self.git_state()["cardinal_pr_number"]), 7)
        self.assertTrue(wait_for(
            lambda: self.pr_cache().get(f"{REPO}#{BRANCH}", {}).get("number") == fixtures.FIXTURE_PR_NUMBER))

    def test_protected_branch_skips_gh(self):
        subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=self.repo,
                       check=True, capture_output=True)
        self.prompt()
        self.assertNotIn("cardinal_pr_number", self.git_state())
        time.sleep(0.5)
        self.assertFalse(self.gh_log.exists())


if __name__ == "__main__":
    unittest.main()

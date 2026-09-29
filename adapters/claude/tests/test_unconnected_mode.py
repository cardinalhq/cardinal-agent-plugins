"""The plugin before /cardinal:connect (the unconnected mode).

  not connected   .mcp.json -> https://app.cardinalhq.io/mcp, MCP OAuth by
                  Claude Code. Storyboard, preview and evidence hooks work;
                  every telemetry / limits / initiative / plan / decision /
                  git-state / usage hook exits 0 at once: no output, no
                  network, no subprocess.
  connected       /cardinal:connect's key or state: everything as before
                  (test_parity's byte-equal goldens run connected).

The gated hooks run here under a guard (runpy inside a wrapper) that logs and
refuses every socket connect / DNS lookup and logs every subprocess, so "no
network" is checked directly, not inferred from an empty stub.

Requires cardinal_core vendored: python3 build/vendor.py claude
Run with: python3 -m unittest tests.test_unconnected_mode -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS = PLUGIN_ROOT / "hooks"
VENDORED = HOOKS / "cardinal_core" / "otlp.py"
SESSION = "3f2a9c1e-7b4d-4e0a-9c8b-1a2b3c4d5e6f"

GATED_HOOKS = (
    "initiative-convention.py", "plan-state.py", "git-state.py", "limits-gate.py", "decision-prompt.py",
    "turn-usage.py", "plan-usage.py", "subagent-usage.py",
)
# Run in both modes: storyboards work before connect.
STORYBOARD_HOOKS = ("storyboard-session.py", "storyboard-preview.py", "storyboard-token.py", "evidence-capture.py")
# Not gated: never touches the network or Cardinal (a local Invariant
# checkout's check-pr.ts, or nothing).
LOCAL_ONLY_HOOKS = ("invariant-check.py",)

GUARD = textwrap.dedent('''\
    import os, runpy, socket, subprocess, sys
    LOG = os.environ["NET_GUARD_LOG"]
    def _log(line):
        with open(LOG, "a") as f:
            f.write(line + "\\n")
    def _deny(*a, **k):
        _log("net")
        raise OSError("network is off in this test")
    socket.socket.connect = _deny
    socket.socket.connect_ex = _deny
    socket.create_connection = _deny
    socket.getaddrinfo = _deny
    _popen = subprocess.Popen.__init__
    def _spy(self, args, *a, **k):
        _log("exec " + os.path.basename(str(args[0] if isinstance(args, (list, tuple)) else args)))
        return _popen(self, args, *a, **k)
    subprocess.Popen.__init__ = _spy
    hook = sys.argv[1]
    sys.argv = [hook]
    runpy.run_path(hook, run_name="__main__")
''')


def hermetic(env: dict) -> dict:
    """No CARDINAL_MCP_* / OTEL_* from the developer's shell (Claude Code
    exports CARDINAL_MCP_API_KEY to its children)."""
    return {k: v for k, v in env.items() if not (k.startswith("CARDINAL_MCP_") or k.startswith("OTEL_"))}


def other_vendor_settings(home: Path, endpoint: str) -> None:
    """An OTel env block that is NOT Cardinal's (no x-cardinalhq-api-key):
    before this change the hooks would have POSTed their events to it. Not
    connected: they must not."""
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(json.dumps({"env": {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer other-vendor-token",
        "OTEL_RESOURCE_ATTRIBUTES": fixtures.RESOURCE_ATTRS_CSV,
        "CARDINAL_DECISIONS": "1",
    }}))


def _load(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"under_test_{name}", HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ConnectionHelperTests(unittest.TestCase):
    def setUp(self):
        self.conn = _load("_connection")
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, env) -> None:
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": env}))

    def test_nothing_configured_is_not_connected(self):
        self.assertFalse(self.conn.is_connected(self.home, {}))
        self.settings({"THEME": "dark", "CARDINAL_MCP_URL": "https://self-hosted.example/mcp"})
        self.assertFalse(self.conn.is_connected(self.home, {}), "a URL alone is not a connection")

    def test_mcp_key_in_settings_or_environment(self):
        self.settings({"CARDINAL_MCP_API_KEY": "ck_1"})
        self.assertTrue(self.conn.is_connected(self.home, {}))
        self.settings({"CARDINAL_MCP_API_KEY": ""})
        self.assertFalse(self.conn.is_connected(self.home, {}), "an empty key is no key")
        self.assertTrue(self.conn.is_connected(self.home, {"CARDINAL_MCP_API_KEY": "ck_1"}))
        self.assertFalse(self.conn.is_connected(self.home, {"CARDINAL_MCP_API_KEY": "  "}))

    def test_cardinal_ingest_key_counts_telemetry_only_connect(self):
        self.settings({"OTEL_EXPORTER_OTLP_HEADERS": "X-CardinalHQ-API-Key=ing_1"})
        self.assertTrue(self.conn.is_connected(self.home, {}))
        self.settings({"OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer x,x-cardinalhq-api-key="})
        self.assertFalse(self.conn.is_connected(self.home, {}), "another vendor's OTel is not Cardinal")
        self.assertTrue(self.conn.is_connected(
            self.home, {"OTEL_EXPORTER_OTLP_HEADERS": "a=b, x-cardinalhq-api-key=ing_2"}))

    def test_connect_state_file(self):
        (self.home / ".claude" / "cardinal.json").write_text("{}")
        self.assertTrue(self.conn.is_connected(self.home, {}))

    def test_unreadable_settings_fail_closed(self):
        (self.home / ".claude" / "settings.json").write_text("{not json")
        self.assertFalse(self.conn.is_connected(self.home, {}))
        (self.home / ".claude" / "settings.json").write_text('{"env": ["x"]}')
        self.assertFalse(self.conn.is_connected(self.home, {}))

    def test_home_is_read_per_call(self):
        self.settings({"CARDINAL_MCP_API_KEY": "ck_1"})
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False):
            os.environ.pop("CARDINAL_MCP_API_KEY", None)
            self.assertTrue(self.conn.is_connected())


class HookRegistrationTests(unittest.TestCase):
    def test_every_hook_is_classified_and_every_gated_hook_checks_the_connection(self):
        hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
        commands = {h["command"].rsplit("/", 1)[-1] for groups in hooks.values() for g in groups for h in g["hooks"]}
        self.assertEqual(commands, set(GATED_HOOKS) | set(STORYBOARD_HOOKS) | set(LOCAL_ONLY_HOOKS),
                         "a new hook must be gated on _connection or listed as working unconnected")
        for name in GATED_HOOKS:
            text = (HOOKS / name).read_text()
            self.assertIn("import _connection", text, name)
            self.assertIn("if not _connection.is_connected():", text, name)
        for name in STORYBOARD_HOOKS:
            self.assertNotIn("_connection", (HOOKS / name).read_text(), name)

    def test_local_only_hooks_have_no_network_code(self):
        for name in LOCAL_ONLY_HOOKS:
            text = (HOOKS / name).read_text()
            for mod in ("urllib", "http.client", "socket", "requests", "cardinal_core.otlp"):
                self.assertNotRegex(text, rf"^\s*(import|from)\s+{re.escape(mod)}\b", f"{name}: {mod}")

    def test_storyboard_matchers_and_server_lists_cover_the_plugin_server(self):
        # Same server name connected or not: mcp__plugin_cardinal_cardinal__*.
        hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["PostToolUse"]
        tool = "mcp__plugin_cardinal_cardinal__storyboard__"
        by_cmd = {h["command"].rsplit("/", 1)[-1]: g["matcher"] for g in hooks for h in g["hooks"]}
        self.assertTrue(re.fullmatch(by_cmd["storyboard-preview.py"], tool + "preview"))
        for t in ("create", "preview"):
            self.assertTrue(re.fullmatch(by_cmd["storyboard-token.py"], tool + t))
        self.assertTrue(re.fullmatch(by_cmd["evidence-capture.py"], tool + "create"))
        for name in ("evidence-capture", "storyboard-token", "storyboard-preview"):
            self.assertIn('"plugin_cardinal_cardinal"', (HOOKS / f"{name}.py").read_text(), name)


class _GuardedCase(unittest.TestCase):
    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        self.guard_dir = Path(self.tmp.name)
        self.guard = self.guard_dir / "guard.py"
        self.guard.write_text(GUARD)
        self.guard_log = self.guard_dir / "guard.log"

    def tearDown(self):
        self.tmp.cleanup()

    def guarded_run(self, hook_path: Path, payload, env: dict, timeout: float = 30.0):
        env = hermetic(dict(env))
        env["NET_GUARD_LOG"] = str(self.guard_log)
        return subprocess.run([sys.executable, str(self.guard), str(hook_path)],
                              input=json.dumps(payload) if not isinstance(payload, str) else payload,
                              text=True, capture_output=True, env=env, timeout=timeout)

    def guard_lines(self) -> list:
        return self.guard_log.read_text().splitlines() if self.guard_log.exists() else []


class GatedHooksUnconnectedTests(_GuardedCase):
    """Every golden scenario (the connected runs test_parity pins byte-equal),
    replayed with no Cardinal connection: silent, no network, no subprocess."""

    def _replay(self, name: str, connected: bool) -> tuple:
        procs: list = []

        def settings(home: Path, endpoint: str) -> None:
            other_vendor_settings(home, endpoint)
            if connected:
                (home / ".claude" / "cardinal.json").write_text('{"mode": "telemetry-only"}\n')

        def run_hook(hook_path, payload, env, timeout=30.0):
            if connected:
                proc = subprocess.run([sys.executable, str(hook_path)], input=json.dumps(payload), text=True,
                                      capture_output=True, env=hermetic(env), timeout=timeout)
            else:
                proc = self.guarded_run(hook_path, payload, env, timeout)
            procs.append(proc)
            return proc

        with mock.patch.object(fixtures, "write_settings", settings), \
                mock.patch.object(fixtures, "run_hook", run_hook), \
                tempfile.TemporaryDirectory(prefix=f"unconnected-{name}-") as tmp:
            try:
                result = fixtures.SCENARIOS[name](HOOKS, Path(tmp))
            except FileNotFoundError:
                # limits_gate_warn reads the ack file the gate writes when it
                # speaks; a silent gate writes none.
                result = None
        return result, procs

    def test_scenarios_are_silent_without_network_when_not_connected(self):
        for name in fixtures.SCENARIOS:
            with self.subTest(name):
                if self.guard_log.exists():
                    self.guard_log.unlink()
                result, procs = self._replay(name, connected=False)
                self.assertEqual(len(procs), 1)
                proc = procs[0]
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
                if result is not None:
                    self.assertEqual(result["batches"], [])
                self.assertEqual(self.guard_lines(), [], "no network and no subprocess")

    def test_same_scenarios_emit_when_connected(self):
        # The control: with the connect state file, the same setup emits
        # (to whatever OTel endpoint is configured) or speaks, as before.
        for name in fixtures.SCENARIOS:
            with self.subTest(name):
                result, procs = self._replay(name, connected=True)
                self.assertEqual(procs[0].returncode, 0, procs[0].stderr)
                if name == "subagent_usage_missing_transcript":
                    continue  # emits nothing either way: no transcript
                self.assertTrue(result["batches"] or result["stdout"], f"{name} did nothing when connected")

    def test_decision_prompt_is_silent_when_not_connected(self):
        home = self.guard_dir / "home"
        other_vendor_settings(home, "http://127.0.0.1:9")  # CARDINAL_DECISIONS=1: capture forced on
        payload = {"session_id": "s1", "prompt": "next", "hook_event_name": "UserPromptSubmit"}
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        proc = self.guarded_run(HOOKS / "decision-prompt.py", payload, env)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual(self.guard_lines(), [])
        (home / ".claude" / "cardinal.json").write_text("{}")
        proc = self.guarded_run(HOOKS / "decision-prompt.py", payload, env)
        self.assertIn("Cardinal decision capture is on", json.loads(proc.stdout)
                      ["hookSpecificOutput"]["additionalContext"])

    def test_initiative_convention_never_mentions_cardinal_when_not_connected(self):
        home = self.guard_dir / "home"
        repo = fixtures.make_git_repo(self.guard_dir / "repo", "feat/x")
        payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "CARDINAL_MCP_API_KEY": "leaks-from-a-shell-but-is-stripped"}
        proc = self.guarded_run(HOOKS / "initiative-convention.py", payload, env)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.guard_lines(), [])


class StoryboardHooksUnconnectedTests(_GuardedCase):
    """The storyboard hooks with no connection at all: no settings.json, no
    state file, no key. They run, locally, without the network."""

    def setUp(self):
        super().setUp()
        self.home = Path(os.path.realpath(self.guard_dir)) / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def test_session_hook_puts_the_session_id_in_context(self):
        proc = self.guarded_run(HOOKS / "storyboard-session.py",
                                {"session_id": SESSION, "hook_event_name": "SessionStart"}, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(SESSION, json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.guard_lines(), [])

    def test_evidence_capture_records_other_servers_and_skips_cardinal(self):
        base = {"session_id": SESSION, "hook_event_name": "PostToolUse", "tool_input": {"expr": "up"},
                "tool_response": {"content": [{"type": "text", "text": "series: 3"}]}}
        proc = self.guarded_run(HOOKS / "evidence-capture.py",
                                dict(base, tool_name="mcp__grafana__query_prometheus"), self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"], r"^\[evidence:ev_")
        spool = self.home / ".cardinal" / "evidence" / SESSION
        self.assertEqual(len(list(spool.glob("ev_*.json"))), 1)
        # Cardinal's own server (same name connected or not) is never captured.
        for name in ("mcp__plugin_cardinal_cardinal__lakerunner__list_services",
                     "mcp__plugin_cardinal_cardinal__storyboard__create", "mcp__cardinal__common__current_time"):
            proc = self.guarded_run(HOOKS / "evidence-capture.py", dict(base, tool_name=name), self.env)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""), name)
        self.assertEqual(len(list(spool.glob("ev_*.json"))), 1)
        self.assertEqual(self.guard_lines(), [])

    def test_token_hook_stores_the_evidence_token_from_the_plugin_server(self):
        sb = "sb_" + "a1" * 12
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzYiI6InNiIn0.c2ln"
        result = {"storyboard_id": sb, "status": "draft", "evidence_token": token,
                  "evidence_token_expires_at": "2099-01-01T00:00:00.000Z",
                  "evidence_upload": {"method": "POST", "path": f"/api/orgs/org-1/storyboards/{sb}/evidence",
                                      "authorization": "CardinalEvidence <evidence_token>"}}
        payload = {"session_id": SESSION, "hook_event_name": "PostToolUse",
                   "tool_name": "mcp__plugin_cardinal_cardinal__storyboard__create", "tool_input": {},
                   "tool_response": {"content": json.dumps(result), "structuredContent": result}}
        proc = self.guarded_run(HOOKS / "storyboard-token.py", payload, self.env)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        stored = json.loads((self.home / ".cardinal" / "evidence" / SESSION / "token.json").read_text())
        self.assertIn(token, json.dumps(stored))
        self.assertEqual(self.guard_lines(), [])


if __name__ == "__main__":
    unittest.main()

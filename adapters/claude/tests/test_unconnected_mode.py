"""The plugin before /cardinal:connect (the local-only mode).

  not connected   .mcp.json's URL is ${CARDINAL_MCP_URL} with no default, so
                  the `cardinal` server has no URL and never connects (no
                  OAuth, no network). Evidence capture and the storyboard
                  hooks run locally; the session hook adds a one-line connect
                  hint; every telemetry / limits / initiative / plan /
                  decision / git-state / usage hook exits 0 at once: no
                  output, no network, no subprocess.
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
    "turn-usage.py", "plan-usage.py", "subagent-usage.py", "storyboard-discovery.py",
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


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str, env: dict) -> tuple:
    """Claude Code's ${VAR} / ${VAR:-default} substitution in plugin .mcp.json
    (2.1.284): -> (expanded, missing vars). A missing var without a default
    is left in place and reported, and the server is not connected."""
    missing: list = []

    def sub(m):
        name, default = m.group(1), m.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        missing.append(name)
        return m.group(0)

    return _VAR.sub(sub, value), missing


def resolve_server(env: dict) -> dict | None:
    """The `cardinal` server Claude Code would connect to with `env`, or None
    when it has no usable URL (never connects, sends nothing)."""
    entry = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())["cardinal"]
    url, missing = expand(entry["url"], env)
    if missing or not re.match(r"^https?://[^/\s]+", url):
        return None
    headers = {k: expand(v, env)[0] for k, v in entry.get("headers", {}).items()}
    return {"url": url, "headers": headers}


class McpServerRegistrationTests(unittest.TestCase):
    """Not connected: no cardinal server is reachable (no URL, no OAuth
    fallback, no key sent anywhere). Connected: the org URL + key, as before."""

    ORG_URL = "https://app.cardinalhq.io/api/orgs/o1/mcp"

    def test_unconnected_registers_no_reachable_server(self):
        self.assertIsNone(resolve_server({}))
        entry = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())["cardinal"]
        self.assertNotIn(":-", entry["url"], "no default URL: an unset URL must not reach any host")
        self.assertNotIn("oauth", json.dumps(entry).lower())
        self.assertNotIn("app.cardinalhq.io", json.dumps(entry))

    def test_stray_key_is_sent_nowhere(self):
        self.assertIsNone(resolve_server({"CARDINAL_MCP_API_KEY": "ck_stray"}))

    def test_connected_resolves_to_the_org_url_with_the_key(self):
        self.assertEqual(resolve_server({"CARDINAL_MCP_URL": self.ORG_URL, "CARDINAL_MCP_API_KEY": "ck_1"}),
                         {"url": self.ORG_URL, "headers": {"X-CardinalHQ-API-Key": "ck_1"}})
        # Only the URL reports as missing when unconnected (the key has a default).
        entry = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())["cardinal"]
        self.assertEqual(expand(entry["url"], {})[1], ["CARDINAL_MCP_URL"])
        self.assertEqual(expand(entry["headers"]["X-CardinalHQ-API-Key"], {})[1], [])


class StrayKeyHelperTests(unittest.TestCase):
    """hooks/_stray_key.py: the key-without-URL check."""

    def setUp(self):
        self.mod = _load("_stray_key")
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, env) -> None:
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": env}))

    def test_key_without_url(self):
        self.assertFalse(self.mod.key_without_url(self.home, {}))
        self.assertTrue(self.mod.key_without_url(self.home, {"CARDINAL_MCP_API_KEY": "ck"}))
        self.assertFalse(self.mod.key_without_url(
            self.home, {"CARDINAL_MCP_API_KEY": "ck", "CARDINAL_MCP_URL": "https://m.example/mcp"}))
        # An empty URL is no URL.
        self.assertTrue(self.mod.key_without_url(self.home, {"CARDINAL_MCP_API_KEY": "ck", "CARDINAL_MCP_URL": ""}))
        self.settings({"CARDINAL_MCP_API_KEY": "ck"})
        self.assertTrue(self.mod.key_without_url(self.home, {}))
        self.assertFalse(self.mod.key_without_url(self.home, {"CARDINAL_MCP_URL": "https://m.example/mcp"}))
        self.settings({"CARDINAL_MCP_API_KEY": "ck", "CARDINAL_MCP_URL": "https://m.example/mcp"})
        self.assertFalse(self.mod.key_without_url(self.home, {}))

    def test_mcp_url_has_no_default_and_unreadable_settings(self):
        self.assertIsNone(self.mod.mcp_url(self.home, {}))
        self.assertEqual(self.mod.mcp_url(self.home, {"CARDINAL_MCP_URL": "https://m.example/mcp"}),
                         "https://m.example/mcp")
        (self.home / ".claude" / "settings.json").write_text("{not json")
        self.assertTrue(self.mod.key_without_url(self.home, {"CARDINAL_MCP_API_KEY": "ck"}))
        self.assertFalse(self.mod.key_without_url(self.home, {}))

    def test_warning_names_the_fixes_and_no_cloud_or_oauth(self):
        for s in ("/cardinal:connect", "--host", "unset CARDINAL_MCP_API_KEY", "has no URL"):
            self.assertIn(s, self.mod.WARNING)
        for s in ("Cardinal Cloud", "OAuth", "app.cardinalhq.io/mcp"):
            self.assertNotIn(s, self.mod.WARNING)


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
            if name == "storyboard-session.py":
                continue  # reads _connection for the connect hint only, never gates on it
            self.assertNotIn("_connection", (HOOKS / name).read_text(), name)

    def test_local_only_hooks_have_no_network_code(self):
        for name in LOCAL_ONLY_HOOKS:
            text = (HOOKS / name).read_text()
            for mod in ("urllib", "http.client", "socket", "requests", "cardinal_core.otlp"):
                self.assertNotRegex(text, rf"^\s*(import|from)\s+{re.escape(mod)}\b", f"{name}: {mod}")

    def test_storyboard_matchers_and_server_lists_cover_the_plugin_server(self):
        # The plugin's server name: mcp__plugin_cardinal_cardinal__*.
        hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["PostToolUse"]
        tool = "mcp__plugin_cardinal_cardinal__storyboard__"
        by_cmd = {h["command"].rsplit("/", 1)[-1]: g["matcher"] for g in hooks for h in g["hooks"]}
        self.assertTrue(re.fullmatch(by_cmd["storyboard-preview.py"], tool + "preview"))
        for t in ("create", "preview", "add_act"):
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

    def guarded_run(self, hook_path: Path, payload, env: dict, timeout: float = 30.0, raw_env: dict | None = None):
        """`env` is made hermetic; `raw_env` is added after, as-is (to put a
        CARDINAL_MCP_* var in the hook's process environment on purpose)."""
        env = hermetic(dict(env))
        env.update(raw_env or {})
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

    def test_storyboard_discovery_is_silent_in_a_git_repo_when_not_connected(self):
        home = self.guard_dir / "home"
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        repo = fixtures.make_git_repo(self.guard_dir / "repo", "feat/x")
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        for event in ("SessionStart", "UserPromptSubmit"):
            payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": event}
            proc = self.guarded_run(HOOKS / "storyboard-discovery.py", payload, env)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""), event)
        self.assertEqual(self.guard_lines(), [], "no network and no subprocess")

    def test_initiative_convention_is_silent_in_a_git_repo_when_not_connected(self):
        # A git repo on a feature branch is exactly where the connected hook
        # speaks (the initiative convention); not connected it says nothing.
        home = self.guard_dir / "home"
        repo = fixtures.make_git_repo(self.guard_dir / "repo", "feat/x")
        payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        proc = self.guarded_run(HOOKS / "initiative-convention.py", payload, env)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.guard_lines(), [])
        # The control: connected, the same repo gets the convention prompt.
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / "cardinal.json").write_text("{}")
        proc = self.guarded_run(HOOKS / "initiative-convention.py", payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"])


class StoryboardHooksUnconnectedTests(_GuardedCase):
    """The storyboard hooks with no connection at all: no settings.json, no
    state file, no key. They run, locally, without the network."""

    def setUp(self):
        super().setUp()
        self.home = Path(os.path.realpath(self.guard_dir)) / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def test_session_hook_omits_the_session_id_when_not_connected(self):
        # No cardinal server, so no storyboard__create to pass the id to.
        proc = self.guarded_run(HOOKS / "storyboard-session.py",
                                {"session_id": SESSION, "hook_event_name": "SessionStart"}, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn(SESSION, ctx)
        self.assertNotIn("storyboard__create", ctx)
        self.assertIn("Cardinal is not connected", ctx)
        self.assertEqual(self.guard_lines(), [])

    def _session(self, sid=SESSION, source="startup", raw_env=None) -> str:
        payload = {"hook_event_name": "SessionStart", "source": source}
        if sid:
            payload["session_id"] = sid
        proc = self.guarded_run(HOOKS / "storyboard-session.py", payload, self.env, raw_env=raw_env)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(self.guard_lines(), [])
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"] if proc.stdout else ""

    def _settings(self, env: dict) -> None:
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": env}))

    def test_session_hook_warns_once_about_a_stray_key(self):
        # Key in settings.json env, no URL: the server has no URL, the hooks
        # count the machine as connected. Warn; no connect hint (connected).
        self._settings({"CARDINAL_MCP_API_KEY": "ck_selfhosted"})
        ctx = self._session()
        self.assertIn(SESSION, ctx)
        self.assertIn("Tell the user", ctx)
        self.assertIn("CARDINAL_MCP_API_KEY is set but CARDINAL_MCP_URL is not", ctx)
        self.assertIn("has no URL", ctx)
        self.assertNotIn("app.cardinalhq.io/mcp", ctx)
        self.assertNotIn("Cardinal is not connected", ctx)
        self.assertIn("/cardinal:connect --host", ctx)
        self.assertNotIn("ck_selfhosted", ctx, "never echo the key")
        # resume / clear / compact of the same session: the id again, no repeat.
        for source in ("resume", "clear", "compact"):
            ctx = self._session(source=source)
            self.assertIn(SESSION, ctx)
            self.assertNotIn("CARDINAL_MCP_API_KEY", ctx, source)
        # A new session is warned again.
        self.assertIn("CARDINAL_MCP_API_KEY", self._session(sid="another-session"))

    def test_session_hook_warns_for_a_key_in_the_process_environment(self):
        # Exported in a shell profile (the Codex manual-config docs' route).
        ctx = self._session(raw_env={"CARDINAL_MCP_API_KEY": "ck_from_shell"})
        self.assertIn("CARDINAL_MCP_API_KEY is set but CARDINAL_MCP_URL is not", ctx)
        self.assertNotIn("ck_from_shell", ctx)
        # URL from settings.json + key from the shell: the key goes to that URL.
        self._settings({"CARDINAL_MCP_URL": "https://maestro.example/mcp"})
        ctx = self._session(sid="s2", raw_env={"CARDINAL_MCP_API_KEY": "ck_from_shell"})
        self.assertNotIn("CARDINAL_MCP_API_KEY", ctx)

    def test_session_hook_does_not_warn_when_connected_or_keyless(self):
        # (label, settings env, process env, connected -> session id in context)
        cases = (
            ("nothing", {}, None, False),
            ("connected", {"CARDINAL_MCP_URL": "https://app.cardinalhq.io/api/orgs/o1/mcp",
                           "CARDINAL_MCP_API_KEY": "ck_1"}, None, True),
            ("empty key", {"CARDINAL_MCP_API_KEY": ""}, None, False),
            ("blank key", {"CARDINAL_MCP_API_KEY": "  "}, None, False),
            ("url only", {"CARDINAL_MCP_URL": "https://maestro.example/mcp"}, None, False),
            ("url from shell", {"CARDINAL_MCP_API_KEY": "ck_1"}, {"CARDINAL_MCP_URL": "https://maestro.example/mcp"}, True),
        )
        for i, (label, settings, raw_env, connected) in enumerate(cases):
            with self.subTest(label):
                self._settings(settings)
                ctx = self._session(sid=f"s-{i}", raw_env=raw_env)
                (self.assertIn if connected else self.assertNotIn)(f"s-{i}", ctx)
                self.assertNotIn("CARDINAL_MCP_API_KEY", ctx)

    def test_session_hook_gives_unconnected_users_a_one_line_hint_once(self):
        # First unconnected startup on this machine: "tell the user once".
        ctx = self._session()
        self.assertNotIn(SESSION, ctx, "no storyboard__create to pass the id to")
        self.assertTrue(ctx.startswith("Tell the user once"), ctx)
        hint = ctx[ctx.index("Tell the user once"):]
        self.assertNotIn("\n", hint, "one line")
        self.assertIn("Cardinal is not connected", hint)
        self.assertIn("sign up at https://app.cardinalhq.io, then run /cardinal:connect and approve it in the browser", hint)
        self.assertIn("missing CARDINAL_MCP_URL", hint)
        for s in ("OAuth", "sign in", "authenticate", "app.cardinalhq.io/mcp"):
            self.assertNotIn(s, hint)
        self.assertTrue((self.home / ".cardinal" / "connect-hint").exists())
        # Later sessions and resume / compact: context only, never "tell the user".
        for sid, source in (("s-later", "startup"), (SESSION, "resume"), (SESSION, "compact")):
            ctx = self._session(sid=sid, source=source)
            self.assertIn("Cardinal is not connected", ctx, source)
            self.assertNotIn("Tell the user", ctx, source)
            self.assertIn("Mention it only if the user asks", ctx)

    def test_session_hook_gives_no_hint_when_connected(self):
        for i, settings in enumerate((
            {"CARDINAL_MCP_URL": "https://app.cardinalhq.io/api/orgs/o1/mcp", "CARDINAL_MCP_API_KEY": "ck_1"},
            {"OTEL_EXPORTER_OTLP_HEADERS": "x-cardinalhq-api-key=ing_1"},
        )):
            with self.subTest(i):
                self._settings(settings)
                ctx = self._session(sid=f"c-{i}")
                self.assertIn(f"c-{i}", ctx)
                self.assertNotIn("Cardinal is not connected", ctx)
        self.assertFalse((self.home / ".cardinal" / "connect-hint").exists())

    def test_session_hook_warns_without_a_session_id_on_startup_only(self):
        self._settings({"CARDINAL_MCP_API_KEY": "ck_1"})
        self.assertIn("CARDINAL_MCP_API_KEY", self._session(sid=None, source="startup"))
        self.assertEqual(self._session(sid=None, source="compact"), "")

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

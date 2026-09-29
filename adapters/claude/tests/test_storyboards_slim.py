"""The storyboard hooks as the slim cardinal-storyboards plugin runs them.

The slim plugin (adapters/claude-storyboards, composed by build/release.py)
ships byte-identical copies of the storyboard hooks, bin/cardinal-evidence and
the canvas renderer. These tests build it into a temp dir and check:

  - hooks/_plugin_mode.py: when the full `cardinal` plugin counts as active;
  - the slim hooks step aside while the full plugin is active, and work
    without it (evidence capture, token store, session id, preview);
  - Cardinal's own servers (including the slim plugin's) are never captured,
    and only they can hand the hooks a token;
  - the renderer fetches preview pages with the result's preview_token when
    no API key is configured, and only from the Cardinal this machine chose;
  - cardinal-evidence uploads to the plugin's own Cardinal without a URL.

Requires cardinal_core vendored: python3 build/vendor.py claude

Run with: python3 -m unittest tests.test_storyboards_slim -v
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGIN_ROOT.parent.parent
VENDORED = PLUGIN_ROOT / "hooks" / "cardinal_core" / "evidence.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_storyboard_preview as tsp  # noqa: E402  (fake Chromium, bundle refs)

SESSION = "3f2a9c1e-7b4d-4e0a-9c8b-1a2b3c4d5e6f"
SB = "sb_" + "a1" * 12
ORG = "org-7f3a"
EV_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzYiI6InNiIn0.c2lnbmF0dXJl"
PREVIEW_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6InByZXZpZXcifQ.eyJzY29wZSI6InByZXZpZXc6cmVhZCJ9.cHJldmlldy1zaWc"
API_KEY = "ck_live_plugin_key_0123456789"
SLIM_TOOL = "mcp__plugin_cardinal-storyboards_cardinal__"
FULL_TOOL = "mcp__plugin_cardinal_cardinal__"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _build_slim(dest: Path) -> Path:
    release = _load("cardinal_release_for_slim_tests", REPO_ROOT / "build" / "release.py")
    release.build_artifact("claude-storyboards", dest)
    return dest


class _SlimCase(unittest.TestCase):
    """A built slim plugin in <tmp>/slim and a clean HOME in <tmp>/home."""

    @classmethod
    def setUpClass(cls):
        cls._build = TemporaryDirectory()
        cls.slim = _build_slim(Path(os.path.realpath(cls._build.name)) / "cardinal-storyboards")

    @classmethod
    def tearDownClass(cls):
        cls._build.cleanup()

    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        (self.home / ".claude").mkdir()
        self.spool = self.home / ".cardinal" / "evidence"

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra) -> dict:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(extra)
        return env

    def settings(self, name: str = "settings.json", base: Path | None = None, **body) -> None:
        d = (base or self.home) / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(body))

    def enable_full_plugin(self, on: bool = True, connected: bool = True) -> None:
        """Both plugins enabled; the full one connected (/cardinal:connect's key
        in settings.json env) unless `connected` is False."""
        body = {"enabledPlugins": {"cardinal@cardinalhq-claude-plugin": on,
                                   "cardinal-storyboards@cardinalhq-claude-plugin": True}}
        if connected:
            body["env"] = {"CARDINAL_MCP_API_KEY": API_KEY}
        self.settings(**body)

    def install(self, slim_root: Path) -> None:
        """installed_plugins.json (v2) listing the full plugin (this repo's
        adapters/claude) and the slim one at `slim_root`."""
        d = self.home / ".claude" / "plugins"
        d.mkdir(parents=True, exist_ok=True)
        (d / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            "cardinal@cardinalhq-claude-plugin": [{"scope": "user", "installPath": str(PLUGIN_ROOT)}],
            "cardinal-storyboards@cardinalhq-claude-plugin": [{"scope": "user", "installPath": str(slim_root)}],
        }}))

    def slim_at(self, mcp_url: str) -> Path:
        """A copy of the built slim plugin whose .mcp.json names `mcp_url`
        (a local stub standing in for https://app.cardinalhq.io/mcp)."""
        dest = self.home / "plugins-cache" / "cardinal-storyboards"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.slim, dest)
        (dest / ".mcp.json").write_text(json.dumps({"cardinal": {"type": "http", "url": mcp_url}}))
        return dest

    def run_hook(self, root: Path, script: str, payload: dict, env: dict | None = None):
        res = subprocess.run([sys.executable, str(root / "hooks" / script)], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=60, env=env or self.env(), cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        return res

    def capture_payload(self, tool_name="mcp__grafana__query_prometheus") -> dict:
        return {"session_id": SESSION, "cwd": str(self.home), "hook_event_name": "PostToolUse",
                "tool_name": tool_name, "tool_input": {"expr": "up"}, "tool_response": "series: 3",
                "tool_use_id": "toolu_1"}

    def spooled(self) -> list:
        d = self.spool / SESSION
        return sorted(p.name for p in d.glob("ev_*.json")) if d.is_dir() else []


# ---------------------------------------------------------------------------
# hooks/_plugin_mode.py
# ---------------------------------------------------------------------------

class PluginModeTests(_SlimCase):
    def setUp(self):
        super().setUp()
        self.pm = _load("plugin_mode_under_test", self.slim / "hooks" / "_plugin_mode.py")

    def active(self, environ=None, cwd=None) -> bool:
        return self.pm.full_plugin_active(self.home, environ or {}, cwd)

    def test_names(self):
        self.assertEqual(self.pm.plugin_name(self.slim), "cardinal-storyboards")
        self.assertTrue(self.pm.is_slim(self.slim))
        self.assertEqual(self.pm.plugin_name(PLUGIN_ROOT), "cardinal")
        self.assertFalse(self.pm.is_slim(PLUGIN_ROOT))
        self.assertIn("plugin_cardinal-storyboards_cardinal", self.pm.CARDINAL_SERVERS)
        self.assertIn("plugin_cardinal_cardinal", self.pm.CARDINAL_SERVERS)
        self.assertIn("cardinal", self.pm.CARDINAL_SERVERS)

    def test_hooks_fallback_server_list_matches(self):
        # A hook copied without _plugin_mode.py falls back to its own literal list.
        import ast
        import re
        for name in ("evidence-capture.py", "storyboard-token.py", "storyboard-preview.py"):
            text = (PLUGIN_ROOT / "hooks" / name).read_text()
            m = re.search(r"else:  # same list as _plugin_mode.CARDINAL_SERVERS\n\s+CARDINAL_SERVERS = (\(.*?\))\n",
                          text, re.S)
            self.assertIsNotNone(m, name)
            self.assertEqual(ast.literal_eval(m.group(1)), self.pm.CARDINAL_SERVERS, name)

    def test_nothing_installed_is_inactive(self):
        self.assertFalse(self.active())

    def test_enabled_plugins_decides(self):
        self.enable_full_plugin(True)
        self.assertTrue(self.active())
        self.enable_full_plugin(False)
        self.assertFalse(self.active())
        # Off in enabledPlugins wins over a key left in settings.
        self.settings(enabledPlugins={"cardinal@m": False}, env={"CARDINAL_MCP_API_KEY": API_KEY})
        self.assertFalse(self.active({"CARDINAL_MCP_API_KEY": API_KEY}))

    def test_enabled_but_never_connected_is_not_active(self):
        # /plugin install turns the full plugin on before /cardinal:connect ran:
        # its copy has no Cardinal URL, so the slim hooks must keep working.
        self.enable_full_plugin(True, connected=False)
        self.assertFalse(self.active())
        self.assertTrue(self.active({"CARDINAL_MCP_API_KEY": API_KEY}))
        (self.home / ".claude" / "cardinal.json").write_text(json.dumps({"mcp_key_id": "k_1"}))
        self.assertTrue(self.active())

    def test_project_and_local_settings_override_user_settings(self):
        project = self.home / "repo"
        self.enable_full_plugin(True)
        self.settings(base=project, enabledPlugins={"cardinal@m": False})
        self.assertFalse(self.active(cwd=project))
        self.settings("settings.local.json", base=project, enabledPlugins={"cardinal@m": True})
        self.assertTrue(self.active(cwd=project))

    def test_only_the_exact_plugin_name_counts(self):
        self.settings(enabledPlugins={"cardinal-storyboards@m": True, "cardinalx@m": True, "cardinal": 1})
        self.assertFalse(self.active())

    def test_unlisted_but_installed_and_connected_is_active(self):
        installed = self.home / ".claude" / "plugins"
        installed.mkdir()
        (installed / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"cardinal@cardinalhq-claude-plugin": [{}]}}))
        self.assertFalse(self.active(), "installed but never connected")
        self.assertTrue(self.active({"CARDINAL_MCP_API_KEY": API_KEY}))
        self.settings(env={"CARDINAL_MCP_API_KEY": API_KEY})
        self.assertTrue(self.active())
        (self.home / ".claude" / "settings.json").unlink()
        (self.home / ".claude" / "cardinal.json").write_text(json.dumps({"mcp_url": "https://x/api/orgs/o/mcp"}))
        self.assertTrue(self.active())

    def test_a_leftover_key_without_the_full_plugin_does_not_silence_the_slim_hooks(self):
        installed = self.home / ".claude" / "plugins"
        installed.mkdir()
        (installed / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"cardinal-storyboards@cardinalhq-claude-plugin": [{}]}}))
        self.settings(env={"CARDINAL_MCP_API_KEY": API_KEY})
        self.assertFalse(self.active({"CARDINAL_MCP_API_KEY": API_KEY}))

    def test_unreadable_files_fail_to_inactive(self):
        (self.home / ".claude" / "settings.json").write_text("{not json")
        (self.home / ".claude" / "cardinal.json").write_text("[]")
        self.assertFalse(self.active())

    def test_only_the_slim_copy_yields(self):
        self.enable_full_plugin(True)
        self.assertTrue(self.pm.slim_should_yield(self.home, {}, None, root=self.slim))
        self.assertFalse(self.pm.slim_should_yield(self.home, {}, None, root=PLUGIN_ROOT))
        self.assertEqual(self.yields(), (True, False))

    def yields(self, environ=None, cwd=None) -> tuple:
        """(slim copy yields, full copy yields)."""
        return (self.pm.should_yield(self.home, environ or {}, cwd, root=self.slim),
                self.pm.should_yield(self.home, environ or {}, cwd, root=PLUGIN_ROOT))

    def test_full_copy_yields_while_never_connected_and_the_slim_plugin_is_enabled(self):
        self.enable_full_plugin(True, connected=False)
        self.assertEqual(self.yields(), (False, True))
        self.assertTrue(self.pm.full_should_yield(self.home, {}, None, root=PLUGIN_ROOT))
        self.assertFalse(self.pm.full_should_yield(self.home, {}, None, root=self.slim))
        # Connected: the full copy runs and the slim copy yields.
        self.assertEqual(self.yields({"CARDINAL_MCP_API_KEY": API_KEY}), (True, False))

    def test_full_copy_keeps_running_without_the_slim_plugin(self):
        # Never connected, slim plugin absent / off / unreadable: the full copy
        # is the only one, so it never yields.
        self.settings(enabledPlugins={"cardinal@m": True})
        self.assertEqual(self.yields(), (False, False))
        self.settings(enabledPlugins={"cardinal@m": True, "cardinal-storyboards@m": False})
        self.assertEqual(self.yields(), (False, False))
        (self.home / ".claude" / "plugins").mkdir()
        (self.home / ".claude" / "plugins" / "installed_plugins.json").write_text("{not json")
        self.settings(enabledPlugins={"cardinal@m": True})
        self.assertEqual(self.yields(), (False, False))
        # Only the exact slim name counts.
        self.settings(enabledPlugins={"cardinal@m": True, "cardinal-storyboards-x@m": True})
        self.assertEqual(self.yields(), (False, False))

    def test_slim_listed_only_in_installed_plugins_counts_as_enabled(self):
        self.install(self.slim)
        self.assertEqual(self.yields(), (False, True))
        self.settings("settings.json", base=self.home / "repo", enabledPlugins={"cardinal-storyboards@m": False})
        self.assertEqual(self.yields(cwd=self.home / "repo"), (False, False))

    def test_at_most_one_copy_runs_in_every_state(self):
        for full_on in (True, False, None):
            for slim_on in (True, False, None):
                for connected in (True, False):
                    plugins = {}
                    if full_on is not None:
                        plugins["cardinal@m"] = full_on
                    if slim_on is not None:
                        plugins["cardinal-storyboards@m"] = slim_on
                    body = {"enabledPlugins": plugins}
                    if connected:
                        body["env"] = {"CARDINAL_MCP_API_KEY": API_KEY}
                    self.settings(**body)
                    self.assertNotEqual(self.yields(), (True, True), (full_on, slim_on, connected))


# ---------------------------------------------------------------------------
# The slim hooks, run from the built artifact
# ---------------------------------------------------------------------------

class SlimHookTests(_SlimCase):
    def test_evidence_capture_works_without_the_full_plugin(self):
        res = self.run_hook(self.slim, "evidence-capture.py", self.capture_payload())
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertRegex(ctx, r"^\[evidence:ev_[0-9a-f]{12}\] captured locally from grafana/query_prometheus;")
        self.assertEqual(len(self.spooled()), 1)

    def test_evidence_capture_yields_to_the_full_plugin(self):
        self.enable_full_plugin(True)
        res = self.run_hook(self.slim, "evidence-capture.py", self.capture_payload())
        self.assertEqual(res.stdout, "")
        self.assertEqual(self.spooled(), [])
        # The full plugin's own copy captures it: exactly once overall.
        res = self.run_hook(PLUGIN_ROOT, "evidence-capture.py", self.capture_payload())
        self.assertIn("[evidence:ev_", res.stdout)
        self.assertEqual(len(self.spooled()), 1)

    def test_cardinal_servers_are_never_captured(self):
        for root in (self.slim, PLUGIN_ROOT):
            for name in (SLIM_TOOL + "lakerunner__execute_logs_query", SLIM_TOOL + "storyboard__preview",
                         FULL_TOOL + "storyboard__create", "mcp__cardinal__lakerunner__list_services"):
                res = self.run_hook(root, "evidence-capture.py", self.capture_payload(name))
                self.assertEqual(res.stdout, "", name)
        self.assertEqual(self.spooled(), [])

    def create_payload(self, tool_name: str) -> dict:
        result = {"storyboard_id": SB, "evidence_token": EV_TOKEN,
                  "evidence_token_expires_at": "2099-01-01T00:00:00.000Z",
                  "evidence_upload": {"method": "POST", "path": f"/api/orgs/{ORG}/storyboards/{SB}/evidence",
                                      "authorization": "CardinalEvidence <evidence_token>"}}
        return {"session_id": SESSION, "cwd": str(self.home), "hook_event_name": "PostToolUse",
                "tool_name": tool_name, "tool_input": {},
                "tool_response": {"content": json.dumps(result), "structuredContent": result}}

    def token_file(self) -> Path:
        return self.spool / SESSION / "token.json"

    def test_token_hook_stores_the_slim_servers_token(self):
        res = self.run_hook(self.slim, "storyboard-token.py", self.create_payload(SLIM_TOOL + "storyboard__create"))
        self.assertEqual(res.stdout, "")
        rec = json.loads(self.token_file().read_text())["storyboards"][SB]
        self.assertEqual((rec["org"], rec["evidence_token"]), (ORG, EV_TOKEN))

    def test_token_hook_ignores_other_servers(self):
        for name in ("mcp__evil__storyboard__create", "mcp__plugin_cardinal-storyboards_evil__storyboard__create",
                     SLIM_TOOL + "storyboard__publish"):
            self.run_hook(self.slim, "storyboard-token.py", self.create_payload(name))
        self.assertFalse(self.token_file().exists())

    def test_token_hook_yields_and_the_full_copy_stores_it(self):
        self.enable_full_plugin(True)
        self.run_hook(self.slim, "storyboard-token.py", self.create_payload(SLIM_TOOL + "storyboard__create"))
        self.assertFalse(self.token_file().exists())
        self.run_hook(PLUGIN_ROOT, "storyboard-token.py", self.create_payload(SLIM_TOOL + "storyboard__create"))
        self.assertTrue(self.token_file().exists())

    def test_session_hook(self):
        payload = {"session_id": SESSION, "cwd": str(self.home), "hook_event_name": "SessionStart"}
        res = self.run_hook(self.slim, "storyboard-session.py", payload)
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx, f"Cardinal session id for this session: {SESSION}. "
                              "Pass it as session_id to storyboard__create.")
        self.enable_full_plugin(True)
        self.assertEqual(self.run_hook(self.slim, "storyboard-session.py", payload).stdout, "")
        self.assertNotEqual(self.run_hook(PLUGIN_ROOT, "storyboard-session.py", payload).stdout, "")


# ---------------------------------------------------------------------------
# Preview pages with the preview_token
# ---------------------------------------------------------------------------

class TokenMaestro:
    """GET .../preview.html that admits only `Authorization: CardinalPreview <PREVIEW_TOKEN>`."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.requests: list = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                stub.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                                      "key": self.headers.get("X-CardinalHQ-API-Key")})
                scene = self.path.split("/scenes/", 1)[-1].split("/", 1)[0]
                body = stub.pages.get(scene)
                if self.headers.get("Authorization") != "CardinalPreview " + PREVIEW_TOKEN:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"invalid_token"}')
                    return
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body or b"")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PreviewTokenTests(_SlimCase):
    def setUp(self):
        super().setUp()
        self.rp_slim = _load("render_preview_slim", self.slim / "skills" / "canvas" / "scripts" / "render_preview.py")
        self.rp_full = _load("render_preview_full", PLUGIN_ROOT / "skills" / "canvas" / "scripts" / "render_preview.py")
        self.body = b"<!doctype html><html data-cv-state=loading>shipping</html>"
        self.maestro = TokenMaestro({"shipping": self.body})

    def tearDown(self):
        self.maestro.close()
        super().tearDown()

    def result(self, token=PREVIEW_TOKEN, url=None) -> dict:
        ref = tsp.bundle_ref("shipping", self.body, url=url)
        r = tsp.preview_result([{"id": "shipping", "ok": True, "preview_bundle": ref}])
        if token is not None:
            r["preview_token"] = token
        return r

    def test_slim_plugin_uses_the_token_against_its_own_cardinal(self):
        conn = self.rp_slim.connection(self.result(), self.home, {})
        self.assertEqual(conn, {"origin": "https://app.cardinalhq.io", "org": None, "key": None,
                                "preview_token": PREVIEW_TOKEN})
        self.assertEqual(self.rp_slim.auth_headers(conn), {"Authorization": "CardinalPreview " + PREVIEW_TOKEN})

    def test_a_url_the_result_names_never_receives_the_token(self):
        conn = self.rp_slim.connection(self.result(url="https://evil.example/x"), self.home, {})
        self.assertEqual(conn["origin"], "https://app.cardinalhq.io")

    def test_the_api_key_wins_when_configured(self):
        env = {"CARDINAL_MCP_URL": f"{self.maestro.origin}/api/orgs/{tsp.ORG}/mcp", "CARDINAL_MCP_API_KEY": API_KEY}
        conn = self.rp_slim.connection(self.result(), self.home, env)
        self.assertEqual(conn["key"], API_KEY)
        self.assertNotIn("preview_token", conn)
        self.assertEqual(self.rp_slim.auth_headers(conn), {"X-CardinalHQ-API-Key": API_KEY})

    def test_no_credential_or_no_trusted_cardinal_is_no_connection(self):
        self.assertEqual(self.rp_slim.connection(self.result(token=None), self.home, {}), {})
        for bad in ("a.b", "x.y.z\r\nX-Evil: 1", "", 7, "a.b.c.d"):
            self.assertEqual(self.rp_slim.connection(self.result(token=bad), self.home, {}), {}, repr(bad))
        # The full plugin's .mcp.json URL is ${CARDINAL_MCP_URL}: nothing to trust without it.
        self.assertIsNone(self.rp_full.plugin_mcp_url())
        self.assertEqual(self.rp_full.connection(self.result(), self.home, {}), {})

    def test_plugin_url_needs_a_cardinal_plugin_manifest(self):
        other = self.home / "not-a-plugin"
        (other / ".claude-plugin").mkdir(parents=True)
        (other / ".claude-plugin" / "plugin.json").write_text('{"name": "evil"}')
        (other / ".mcp.json").write_text('{"cardinal": {"type": "http", "url": "https://evil.example/mcp"}}')
        self.assertIsNone(self.rp_slim.plugin_mcp_url(other))
        self.assertEqual(self.rp_slim.plugin_mcp_url(self.slim), "https://app.cardinalhq.io/mcp")

    def test_fetch_sends_only_the_token(self):
        env = {"CARDINAL_MCP_URL": f"{self.maestro.origin}/mcp"}  # a URL, no key
        conn = self.rp_slim.connection(self.result(), self.home, env)
        self.assertEqual(conn["origin"], self.maestro.origin)
        ref = tsp.bundle_ref("shipping", self.body)
        self.assertEqual(self.rp_slim.fetch_bundle(conn, ref, 1 << 20), self.body)
        self.assertEqual(self.maestro.requests[-1]["authorization"], "CardinalPreview " + PREVIEW_TOKEN)
        self.assertIsNone(self.maestro.requests[-1]["key"])

    def test_a_refused_token_says_to_preview_again(self):
        conn = self.rp_slim.connection(self.result(token=PREVIEW_TOKEN + "x"), self.home,
                                       {"CARDINAL_MCP_URL": f"{self.maestro.origin}/mcp"})
        with self.assertRaises(self.rp_slim.FetchError) as cm:
            self.rp_slim.fetch_bundle(conn, tsp.bundle_ref("shipping", self.body), 1 << 20)
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("preview token was refused", str(cm.exception))
        self.assertIn("call storyboard__preview again", str(cm.exception))
        self.assertNotIn("/cardinal:connect", str(cm.exception))

    def test_preview_hook_renders_with_the_token_end_to_end(self):
        (self.home / "empty-bin").mkdir()
        chrome = tsp.make_fake_chromium(self.home)
        result = self.result()
        payload = {"session_id": SESSION, "cwd": str(self.home), "hook_event_name": "PostToolUse",
                   "tool_name": SLIM_TOOL + "storyboard__preview", "tool_input": {},
                   "tool_response": {"content": json.dumps(result), "structuredContent": result}}
        env = tsp.hermetic_env(self.home, CARDINAL_CHROMIUM=str(chrome), FAKE_CHROME_LOG=str(self.home / "c.log"),
                               CARDINAL_MCP_URL=f"{self.maestro.origin}/mcp")
        res = self.run_hook(self.slim, "storyboard-preview.py", payload, env=env)
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("rendered locally", ctx)
        self.assertIn("shipping-0.png", ctx)
        self.assertEqual([r["authorization"] for r in self.maestro.requests], ["CardinalPreview " + PREVIEW_TOKEN])
        self.assertEqual([r["key"] for r in self.maestro.requests], [None])

    def test_preview_hook_ignores_another_servers_preview(self):
        result = self.result()
        for name in ("mcp__evil__storyboard__preview", "mcp__plugin_cardinal-storyboards_evil__storyboard__preview"):
            payload = {"session_id": SESSION, "tool_name": name, "tool_input": {},
                       "tool_response": {"structuredContent": result}}
            res = self.run_hook(self.slim, "storyboard-preview.py", payload,
                                env=self.env(CARDINAL_MCP_URL=f"{self.maestro.origin}/mcp"))
            self.assertEqual(res.stdout, "")
        self.assertEqual(self.maestro.requests, [])

    def test_preview_hook_yields_to_the_full_plugin(self):
        self.enable_full_plugin(True)
        result = self.result()
        payload = {"session_id": SESSION, "cwd": str(self.home), "tool_name": SLIM_TOOL + "storyboard__preview",
                   "tool_input": {}, "tool_response": {"structuredContent": result}}
        res = self.run_hook(self.slim, "storyboard-preview.py", payload,
                            env=self.env(CARDINAL_MCP_URL=f"{self.maestro.origin}/mcp"))
        self.assertEqual(res.stdout, "")
        self.assertEqual(self.maestro.requests, [])


# ---------------------------------------------------------------------------
# Both installed, the full plugin enabled but never connected (/plugin install
# without /cardinal:connect): every storyboard call goes to the slim plugin's
# server, and the slim copy (not the full one) handles it, exactly once.
# ---------------------------------------------------------------------------

class FullEnabledNeverConnectedTests(_SlimCase):
    def setUp(self):
        super().setUp()
        self.enable_full_plugin(True, connected=False)
        self.install(self.slim)

    def load(self, root: Path, rel: str, tag: str):
        return _load(f"{tag}_{hashlib.sha1(str(root).encode()).hexdigest()[:8]}", root / rel)

    def rp(self, root: Path):
        return self.load(root, "skills/canvas/scripts/render_preview.py", "rp")

    def cli(self, root: Path):
        return self.load(root, "bin/cardinal-evidence", "cli")

    def preview_result(self, body: bytes) -> dict:
        r = tsp.preview_result([{"id": "shipping", "ok": True, "preview_bundle": tsp.bundle_ref("shipping", body)}])
        r["preview_token"] = PREVIEW_TOKEN
        return r

    def test_either_copy_fetches_and_uploads_against_app_cardinalhq_io(self):
        result = self.preview_result(b"x")
        for root in (self.slim, PLUGIN_ROOT):
            self.assertEqual(self.rp(root).connection(result, self.home, {}),
                             {"origin": "https://app.cardinalhq.io", "org": None, "key": None,
                              "preview_token": PREVIEW_TOKEN}, str(root))
            self.assertEqual(self.cli(root).connection(self.home, {}),
                             {"origin": "https://app.cardinalhq.io", "org": None, "key": None}, str(root))

    def test_full_copy_falls_back_only_to_a_real_slim_install(self):
        rp = self.rp(PLUGIN_ROOT)
        result = self.preview_result(b"x")
        installed = self.home / ".claude" / "plugins" / "installed_plugins.json"
        evil = self.home / "evil"
        (evil / ".claude-plugin").mkdir(parents=True)
        (evil / ".claude-plugin" / "plugin.json").write_text('{"name": "evil"}')
        (evil / ".mcp.json").write_text('{"cardinal": {"type": "http", "url": "https://evil.example/mcp"}}')
        for plugins in (
            {},  # slim not installed
            {"cardinal-storyboards@m": [{"installPath": str(evil)}]},  # not a cardinal-storyboards manifest
            {"cardinal-storyboards-x@m": [{"installPath": str(self.slim)}]},  # another plugin's id
            {"cardinal-storyboards@m": [{"installPath": 7}, {}]},
        ):
            installed.write_text(json.dumps({"version": 2, "plugins": plugins}))
            self.assertEqual(rp.connection(result, self.home, {}), {}, plugins)
            self.assertEqual(self.cli(PLUGIN_ROOT).connection(self.home, {}), {}, plugins)
        # A URL the result names is never used.
        installed.write_text(json.dumps({"version": 2, "plugins": {}}))
        r = tsp.preview_result([{"id": "shipping", "ok": True,
                                 "preview_bundle": tsp.bundle_ref("shipping", b"x", url="https://evil.example/x")}])
        r["preview_token"] = PREVIEW_TOKEN
        self.assertEqual(rp.connection(r, self.home, {}), {})
        # v1 layout (one record per plugin) still resolves.
        installed.write_text(json.dumps({"version": 1, "plugins": {
            "cardinal-storyboards@m": {"installPath": str(self.slim)}}}))
        self.assertEqual(rp.connection(result, self.home, {})["origin"], "https://app.cardinalhq.io")

    def test_capture_session_and_token_hooks_run_exactly_once(self):
        # evidence-capture
        full = self.run_hook(PLUGIN_ROOT, "evidence-capture.py", self.capture_payload())
        self.assertEqual(full.stdout, "")
        self.assertEqual(self.spooled(), [])
        slim = self.run_hook(self.slim, "evidence-capture.py", self.capture_payload())
        self.assertIn("[evidence:ev_", slim.stdout)
        self.assertEqual(len(self.spooled()), 1)
        # storyboard-session
        payload = {"session_id": SESSION, "cwd": str(self.home), "hook_event_name": "SessionStart"}
        self.assertEqual(self.run_hook(PLUGIN_ROOT, "storyboard-session.py", payload).stdout, "")
        self.assertIn(SESSION, self.run_hook(self.slim, "storyboard-session.py", payload).stdout)
        # storyboard-token
        create = SlimHookTests.create_payload(self, SLIM_TOOL + "storyboard__create")
        token_file = self.spool / SESSION / "token.json"
        self.run_hook(PLUGIN_ROOT, "storyboard-token.py", create)
        self.assertFalse(token_file.exists())
        self.run_hook(self.slim, "storyboard-token.py", create)
        self.assertEqual(json.loads(token_file.read_text())["storyboards"][SB]["evidence_token"], EV_TOKEN)

    def test_a_slim_server_preview_renders_exactly_once_with_the_token(self):
        body = b"<!doctype html><html data-cv-state=loading>shipping</html>"
        maestro = TokenMaestro({"shipping": body})
        self.addCleanup(maestro.close)
        slim = self.slim_at(f"{maestro.origin}/mcp")
        self.install(slim)
        (self.home / "empty-bin").mkdir()
        chrome = tsp.make_fake_chromium(self.home)
        result = self.preview_result(body)
        payload = {"session_id": SESSION, "cwd": str(self.home), "hook_event_name": "PostToolUse",
                   "tool_name": SLIM_TOOL + "storyboard__preview", "tool_input": {},
                   "tool_response": {"content": json.dumps(result), "structuredContent": result}}
        env = tsp.hermetic_env(self.home, CARDINAL_CHROMIUM=str(chrome), FAKE_CHROME_LOG=str(self.home / "c.log"))
        outs = [self.run_hook(root, "storyboard-preview.py", payload, env=env).stdout for root in (PLUGIN_ROOT, slim)]
        self.assertEqual(outs[0], "", "the never-connected full copy steps aside")
        ctx = json.loads(outs[1])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("rendered locally", ctx)
        self.assertIn("shipping-0.png", ctx)
        self.assertEqual([(r["authorization"], r["key"]) for r in maestro.requests],
                         [("CardinalPreview " + PREVIEW_TOKEN, None)])
        # The full copy's renderer, run by hand (the canvas skill's glob can pick
        # it), reaches the same Cardinal through the slim install.
        rp = self.rp(PLUGIN_ROOT)
        conn = rp.connection(result, self.home, {})
        self.assertEqual(conn["origin"], maestro.origin)
        self.assertEqual(rp.fetch_bundle(conn, tsp.bundle_ref("shipping", body), 1 << 20), body)
        self.assertEqual(maestro.requests[-1]["authorization"], "CardinalPreview " + PREVIEW_TOKEN)

    def test_promote_from_the_full_copy_reaches_the_slim_plugins_cardinal(self):
        import test_evidence_promote as tep
        stub = tep.StubEvidenceRoute()
        self.addCleanup(stub.close)
        slim = self.slim_at(f"http://127.0.0.1:{stub.port}/mcp")
        self.install(slim)
        self.run_hook(slim, "storyboard-token.py", SlimHookTests.create_payload(self, SLIM_TOOL + "storyboard__create"))
        self.run_hook(slim, "evidence-capture.py", self.capture_payload())
        [name] = self.spooled()
        ev_id = name[:-len(".json")]
        for root in (PLUGIN_ROOT, slim):
            res = subprocess.run([sys.executable, str(root / "bin" / "cardinal-evidence"), "promote",
                                  "--storyboard", SB, ev_id],
                                 capture_output=True, text=True, timeout=60, env=self.env(), cwd=str(self.home))
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            req = stub.requests[-1]
            self.assertEqual(req["path"], f"/api/orgs/{ORG}/storyboards/{SB}/evidence")
            self.assertEqual(req["headers"]["authorization"], "CardinalEvidence " + EV_TOKEN)
            self.assertNotIn("x-cardinalhq-api-key", req["headers"])
        self.assertEqual(len(stub.requests), 2)


# ---------------------------------------------------------------------------
# bin/cardinal-evidence in the slim plugin
# ---------------------------------------------------------------------------

class SlimEvidenceCliTests(_SlimCase):
    def cli(self, root: Path):
        return _load(f"cardinal_evidence_{hashlib.sha1(str(root).encode()).hexdigest()[:8]}",
                     root / "bin" / "cardinal-evidence")

    def test_uploads_go_to_the_plugins_own_cardinal_with_no_key(self):
        conn = self.cli(self.slim).connection(self.home, {})
        self.assertEqual(conn, {"origin": "https://app.cardinalhq.io", "org": None, "key": None})

    def test_configured_url_still_wins(self):
        env = {"CARDINAL_MCP_URL": "https://maestro.example/api/orgs/o1/mcp", "CARDINAL_MCP_API_KEY": API_KEY}
        conn = self.cli(self.slim).connection(self.home, env)
        self.assertEqual(conn, {"origin": "https://maestro.example", "org": "o1", "key": API_KEY})

    def test_full_plugin_without_a_url_is_still_not_connected(self):
        self.assertEqual(self.cli(PLUGIN_ROOT).connection(self.home, {}), {})

    def test_status_names_the_plugins_cardinal(self):
        res = subprocess.run([sys.executable, str(self.slim / "bin" / "cardinal-evidence"), "status"],
                             capture_output=True, text=True, timeout=30, env=self.env(), cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("uploads go to: https://app.cardinalhq.io, no Cardinal MCP key", res.stdout)


if __name__ == "__main__":
    unittest.main()

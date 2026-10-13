"""Hook-registration tests for scripts/cardinal-connect and cardinal-status.

Gemini CLI reads hook `timeout` in milliseconds and loads extension hooks
only from a top-level `hooks` object (packages/cli/src/config/
extension-manager.ts, loadExtensionHooks). These tests pin what
cardinal-connect writes, that a re-run repairs registrations from older
plugin versions, and that cardinal-status reports a stale registration.
No network: only the local writers are exercised.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ADAPTER = Path(__file__).resolve().parent.parent
MONOREPO_ROOT = ADAPTER.parents[1]
CONNECT = ADAPTER / "scripts" / "cardinal-connect"
STATUS = ADAPTER / "scripts" / "cardinal-status"
SOURCE_HOOKS = ADAPTER / "extension" / "hooks" / "hooks.json"
MARKER = "cardinal-gemini-plugin"


def _ensure_vendored() -> None:
    if (ADAPTER / "hooks" / "cardinal_core" / "__init__.py").exists():
        return
    subprocess.run(
        [sys.executable, str(MONOREPO_ROOT / "build" / "vendor.py"), "gemini"],
        check=True, capture_output=True,
    )


def _handlers(event_map: dict) -> list[tuple[str, dict]]:
    return [
        (event, handler)
        for event, groups in event_map.items() if isinstance(groups, list)
        for group in groups
        for handler in group.get("hooks", [])
        if MARKER in handler.get("command", "")
    ]


class ConnectHookRegistrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_vendored()

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".gemini").mkdir()
        self._old_home = os.environ.get("HOME")
        # cardinal-connect resolves ~/.gemini at import time.
        os.environ["HOME"] = str(self.home)
        name = f"cardinal_connect_{id(self)}"
        loader = importlib.machinery.SourceFileLoader(name, str(CONNECT))
        spec = importlib.util.spec_from_loader(name, loader)
        self.connect = importlib.util.module_from_spec(spec)
        loader.exec_module(self.connect)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self.tmp.cleanup()

    @property
    def settings_path(self) -> Path:
        return self.home / ".gemini" / "settings.json"

    @property
    def ext_hooks(self) -> Path:
        return self.home / ".gemini" / "extensions" / "cardinal" / "hooks" / "hooks.json"

    def _groups(self, event_map: dict) -> list:
        """(event, matcher, command, timeout) of every Cardinal handler."""
        return [(event, group.get("matcher", ""), handler["command"], handler["timeout"])
                for event, groups in event_map.items() if isinstance(groups, list)
                for group in groups for handler in group.get("hooks", [])
                if MARKER in handler.get("command", "")]

    def expected_groups(self, script: str) -> list:
        return [(event, matcher, self.connect.hook_command(script, event, role), timeout)
                for event, matcher, role, timeout in self.connect.HOOK_GROUPS]

    def test_hook_groups(self):
        self.assertEqual(self.connect.HOOK_TIMEOUT_MS, 10000)
        groups = self.connect.HOOK_GROUPS
        # AfterAgent is registered: the turn end (flush + stage, never a block).
        self.assertIn(("AfterAgent", "", "", 10000), groups)
        self.assertIn("AfterAgent", self.connect.REGISTERED_EVENTS)
        # The preview render has its own group, command and ~150 s timeout.
        self.assertIn(("AfterTool", "storyboard__preview$", "preview", 150000), groups)
        self.assertIn(("AfterTool", "", "", 10000), groups)
        self.assertEqual(self.connect.PREVIEW_TIMEOUT_MS, 150000)
        # The Checks preview's activate_skill deny: its own BeforeTool group.
        self.assertIn(("BeforeTool", "^activate_skill$", "checks-gate", 10000), groups)

    def test_handler_commands_are_distinct_per_event(self):
        # Gemini dedupes an event's hooks by name:command (getHookKey): two
        # groups with one command would run once.
        commands = [(e, c) for e, _, c, _ in self.expected_groups("/p/hook.py")]
        self.assertEqual(len(commands), len(set(commands)))
        self.assertIn(("AfterTool", f"python3 /p/hook.py --event AfterTool --role preview # {MARKER}"), commands)

    def test_source_hooks_json_is_hook_groups(self):
        source = json.loads(SOURCE_HOOKS.read_text())
        self.assertEqual(tuple(source["hooks"]), self.connect.REGISTERED_EVENTS)
        self.assertEqual(self._groups(source["hooks"]), self.expected_groups("__HOOK_SCRIPT__"))

    def test_settings_json_hooks_are_the_groups(self):
        self.connect.apply_settings(
            {"endpoint": "https://ingest.example", "api_key": "k"}, None, None, include_hooks=True,
        )
        settings = json.loads(self.settings_path.read_text())
        script = shlex.quote(str(ADAPTER / "hooks" / "cardinal-gemini-telemetry.py"))
        self.assertEqual(self._groups(settings["hooks"]), self.expected_groups(script))
        for event, handler in _handlers(settings["hooks"]):
            with self.subTest(event=event):
                self.assertGreaterEqual(handler["timeout"], 10000)
                self.assertIn(f"--event {event}", handler["command"])
        # Gemini CLI's own exporter is opt-in; nothing carries a marker.
        self.assertNotIn("telemetry", settings)
        self.assertNotIn("cardinalManaged", json.dumps(settings))

    def test_extension_hooks_json_shape(self):
        self.connect.install_extension_bundle(None, None)
        data = json.loads(self.ext_hooks.read_text())
        self.assertIsInstance(data["hooks"], dict)
        script = shlex.quote(str(ADAPTER / "hooks" / "cardinal-gemini-telemetry.py")).strip("'")
        self.assertEqual(self._groups(data["hooks"]), self.expected_groups(script))
        for event, handler in _handlers(data["hooks"]):
            with self.subTest(event=event):
                self.assertNotIn("__HOOK_SCRIPT__", handler["command"])

    def test_rerun_repairs_old_extension_hooks_json(self):
        self.connect.install_extension_bundle(None, None)
        old = {"_comment": "pre-0.17 shape", "BeforeAgent": [{"matcher": "", "hooks": [{
            "type": "command", "command": f"python3 /x --event BeforeAgent # {MARKER}", "timeout": 5}]}]}
        self.ext_hooks.write_text(json.dumps(old))
        self.assertEqual(self.connect.repair_hook_registration(), [str(self.ext_dir)])
        data = json.loads(self.ext_hooks.read_text())
        self.assertIn("BeforeAgent", data["hooks"])
        self.assertEqual(self.connect.repair_hook_registration(), [], "repair must be idempotent")

    def test_rerun_repairs_old_settings_hooks(self):
        user_group = {"matcher": "", "hooks": [{"type": "command", "command": "echo mine", "timeout": 3000}]}
        old = {"hooks": {
            "BeforeAgent": [user_group, {"matcher": "", "cardinalManaged": True, "hooks": [{
                "type": "command", "command": f"python3 /x --event BeforeAgent # {MARKER}", "timeout": 5}]}],
            "AfterAgent": [{"matcher": "", "cardinalManaged": True, "hooks": [{
                "type": "command", "command": f"python3 /x --event AfterAgent # {MARKER}", "timeout": 5}]}],
        }, "general": {"vimMode": True}}
        self.settings_path.write_text(json.dumps(old))
        self.assertEqual(self.connect.repair_hook_registration(), [str(self.settings_path)])
        settings = json.loads(self.settings_path.read_text())
        self.assertEqual(settings["general"], {"vimMode": True})
        self.assertIn(user_group, settings["hooks"]["BeforeAgent"])
        # The old AfterAgent registration is replaced by the current one.
        self.assertEqual([h["command"] for _, h in _handlers({"AfterAgent": settings["hooks"]["AfterAgent"]})],
                         [self.connect.hook_command(shlex.quote(str(self.connect.HOOK_SCRIPT)), "AfterAgent")])
        for _, handler in _handlers(settings["hooks"]):
            self.assertGreaterEqual(handler["timeout"], 10000)
        self.assertEqual(self.connect.repair_hook_registration(), [], "repair must be idempotent")

    # --- the extension bundle: skills, agents, mcpServers merge -------------

    @property
    def ext_dir(self) -> Path:
        return self.home / ".gemini" / "extensions" / "cardinal"

    def manifest(self) -> dict:
        return json.loads((self.ext_dir / "gemini-extension.json").read_text())

    def test_install_copies_skills_with_the_plugin_root(self):
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        for name in ("connect", "status", "disconnect", "storyboard", "canvas", "decision"):
            with self.subTest(skill=name):
                self.assertTrue((self.ext_dir / "skills" / name / "SKILL.md").is_file())
        status = (self.ext_dir / "skills" / "status" / "SKILL.md").read_text()
        self.assertIn(f'python3 "{ADAPTER}/scripts/cardinal-status"', status)
        for md in self.ext_dir.rglob("*.md"):
            self.assertNotIn("__CARDINAL_PLUGIN_ROOT__", md.read_text(), md)
        # The renderer ships with the storyboard skill; caches are not copied.
        self.assertTrue((self.ext_dir / "skills" / "storyboard" / "scripts" / "render_preview.py").is_file())
        self.assertFalse(list(self.ext_dir.rglob("__pycache__")))

    def test_the_bundle_is_staged_outside_the_extensions_dir(self):
        # Gemini CLI loads every subdirectory of ~/.gemini/extensions/: a
        # staging copy there is a second "cardinal" extension.
        seen = {}
        real = self.connect.build_extension_bundle

        def build(target, *args, **kwargs):
            seen["target"] = Path(target)
            seen["extensions"] = sorted(p.name for p in self.ext_dir.parent.iterdir())
            return real(target, *args, **kwargs)

        self.connect.install_extension_bundle(None, None)   # an existing install to replace
        with mock.patch.object(self.connect, "build_extension_bundle", build):
            self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        self.assertEqual(seen["target"].parent, self.home / ".gemini" / "cardinal")
        self.assertEqual(seen["extensions"], ["cardinal"])
        self.assertEqual(sorted(p.name for p in self.ext_dir.parent.iterdir()), ["cardinal"])
        self.assertEqual(self.manifest()["mcpServers"]["cardinal"]["url"], "https://c.example/mcp")
        self.assertFalse(seen["target"].exists(), "renamed into place")
        with mock.patch.object(self.connect, "build_extension_bundle", build):
            self.connect.refresh_extension_bundle("https://c.example/mcp", "ck_1")
        self.assertEqual(seen["target"].parent, self.home / ".gemini" / "cardinal")
        self.assertEqual(sorted(p.name for p in (self.home / ".gemini" / "cardinal").iterdir()
                                if p.name.startswith(".extension-staging-")), [])

    def test_leftover_staging_copies_are_removed_on_the_next_connect(self):
        extensions = self.home / ".gemini" / "extensions"
        old = extensions / ".cardinal.staging-4242"
        (old / "hooks").mkdir(parents=True)
        (old / "gemini-extension.json").write_text(json.dumps({"name": "cardinal"}))
        runtime = self.home / ".gemini" / "cardinal"
        dead = runtime / ".extension-staging-999999999"
        dead.mkdir(parents=True)
        live = runtime / f".extension-staging-{os.getppid()}"   # a running process: another connect
        live.mkdir()
        self.connect.install_extension_bundle(None, None)
        self.assertEqual(sorted(p.name for p in extensions.iterdir()), ["cardinal"])
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())

    def test_install_copies_agents(self):
        self.connect.install_extension_bundle(None, None)
        agent = self.ext_dir / "agents" / "brainstorm.md"
        self.assertEqual(agent.read_text(), (ADAPTER / "agents" / "brainstorm.md").read_text())

    def test_install_writes_concrete_cardinal_server(self):
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        # Lane G: the local cardinal-behavior MCP server is registered next to
        # Cardinal's endpoint (no key in its entry: it reads ~/.gemini's files).
        self.assertEqual(self.manifest()["mcpServers"], {
            "cardinal": {"url": "https://c.example/mcp", "headers": {"X-CardinalHQ-API-Key": "ck_1"}},
            "cardinal-behavior": {"command": "python3",
                                  "args": [str(ADAPTER / "scripts" / "cardinal-behavior"), "--runtime", "gemini"]}})
        self.assertTrue(self.manifest()["cardinalManaged"])
        self.assertNotIn("${", (self.ext_dir / "gemini-extension.json").read_text())

    def test_install_merges_mcp_servers(self):
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        m = self.manifest()
        other = {"command": "python3", "args": ["/opt/other.py"]}
        m["mcpServers"]["other"] = other
        (self.ext_dir / "gemini-extension.json").write_text(json.dumps(m))
        self.connect.install_extension_bundle("https://c.example/mcp2", "ck_2")
        servers = self.manifest()["mcpServers"]
        self.assertEqual(servers["other"], other)
        self.assertEqual(servers["cardinal"]["url"], "https://c.example/mcp2")
        # Without an MCP credential only Cardinal's own entry goes.
        self.connect.install_extension_bundle(None, None)
        self.assertEqual(self.manifest()["mcpServers"], {"other": other})

    def test_install_without_mcp_has_no_servers(self):
        self.connect.install_extension_bundle(None, None)
        self.assertNotIn("mcpServers", self.manifest())

    def test_rerun_refreshes_a_bundle_without_skills_keeping_credentials(self):
        self.connect.install_extension_bundle("https://c.example/mcp", "ck_1")
        m = self.manifest()
        m["mcpServers"]["other"] = {"url": "https://o.example"}
        (self.ext_dir / "gemini-extension.json").write_text(json.dumps(m))
        shutil.rmtree(self.ext_dir / "skills")   # an install from before skills were copied
        shutil.rmtree(self.ext_dir / "agents")
        self.assertEqual(self.connect.repair_hook_registration(), [str(self.ext_dir)])
        self.assertTrue((self.ext_dir / "skills" / "canvas" / "SKILL.md").is_file())
        self.assertTrue((self.ext_dir / "agents" / "brainstorm.md").is_file())
        servers = self.manifest()["mcpServers"]
        self.assertEqual(servers["cardinal"]["headers"]["X-CardinalHQ-API-Key"], "ck_1")
        self.assertEqual(servers["other"], {"url": "https://o.example"})
        self.assertEqual(self.connect.repair_hook_registration(), [], "repair must be idempotent")
        self.assertEqual(sorted(p.name for p in self.ext_dir.parent.iterdir()), ["cardinal"])

    def test_rerun_leaves_an_unmanaged_extension_alone(self):
        self.ext_dir.mkdir(parents=True)
        (self.ext_dir / "gemini-extension.json").write_text(json.dumps({"name": "cardinal"}))
        self.assertEqual(self.connect.repair_hook_registration(), [])
        self.assertEqual(sorted(p.name for p in self.ext_dir.iterdir()), ["gemini-extension.json"])

    def test_rerun_leaves_settings_without_cardinal_hooks_alone(self):
        self.settings_path.write_text(json.dumps({"hooks": {"BeforeAgent": []}}))
        self.assertEqual(self.connect.repair_hook_registration(), [])


class ConnectWithoutIngestTest(unittest.TestCase):
    """maestro grants mcp:invoke but no ingest key when the org has no
    Lakerunner (or the server has no ingest endpoint). Connect must still
    wire MCP, skip the ingest probe and telemetry block, and say why. The
    device flow and probes are stubbed at the cardinal_core.deviceflow
    boundary."""

    MCP = {
        "url": "https://cardinal.example/api/orgs/org-1/mcp",
        "api_key": "MCPPLAINTEXT" + "x" * 52,
        "key_id": "mcp-key-1",
        "key_prefix": "MCPPLAIN",
    }

    @classmethod
    def setUpClass(cls):
        _ensure_vendored()

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".gemini").mkdir()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        name = f"cardinal_connect_no_ingest_{id(self)}"
        loader = importlib.machinery.SourceFileLoader(name, str(CONNECT))
        spec = importlib.util.spec_from_loader(name, loader)
        self.connect = importlib.util.module_from_spec(spec)
        loader.exec_module(self.connect)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self.tmp.cleanup()

    @property
    def settings_path(self) -> Path:
        return self.home / ".gemini" / "settings.json"

    @property
    def state_path(self) -> Path:
        return self.home / ".gemini" / "cardinal.json"

    def _bundle(self, reason: str, mcp: bool = True) -> dict:
        return {
            "org": {"id": "org-1", "slug": "acme"},
            "user": {"email": "dev@example.com"},
            "mcp": dict(self.MCP) if mcp else None,
            "ingest": None,
            "ingest_unavailable_reason": reason,
            "limits": None,
        }

    def _run(self, bundle: dict, telemetry_only: bool = False, no_extension: bool = False):
        args = argparse.Namespace(
            host="https://cardinal.example", telemetry_only=telemetry_only,
            no_extension=no_extension, deployment_env=None, dry_run=False,
        )
        df = self.connect.deviceflow
        out = io.StringIO()
        code = rc = None
        with mock.patch.object(df, "poll_device_token", return_value=bundle), \
                mock.patch.object(df, "verify_mcp_reachable", return_value=(True, "HTTP 405")), \
                mock.patch.object(df, "verify_ingest_reachable",
                                  side_effect=AssertionError("ingest probed")) as probe, \
                contextlib.redirect_stdout(out):
            try:
                rc = self.connect.continue_after_grant(args, {"device_code": "dc", "interval": 1})
            except SystemExit as exc:
                code = exc.code
        return rc, code, out.getvalue(), probe

    def test_no_lakerunner_installs_extension_with_mcp_and_no_telemetry(self):
        rc, code, out, probe = self._run(self._bundle("no_lakerunner_integration"))
        self.assertEqual((rc, code), (0, None), out)
        probe.assert_not_called()

        manifest = json.loads(
            (self.home / ".gemini" / "extensions" / "cardinal" / "gemini-extension.json").read_text()
        )
        self.assertEqual(manifest["mcpServers"]["cardinal"]["url"], self.MCP["url"])
        # Nothing for settings.json: the extension carries MCP, telemetry is off.
        self.assertFalse(self.settings_path.exists())

        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["mode"], "mcp-only")
        self.assertEqual(state["telemetry"], {
            "enabled": False, "unavailable_reason": "no_lakerunner_integration",
        })
        self.assertNotIn("ingest_endpoint", state)
        self.assertIn(
            "WARN telemetry ingest unavailable: no_lakerunner_integration "
            "(this Cardinal org has no active Lakerunner integration); MCP tools connected",
            out,
        )
        self.assertNotIn("Telemetry endpoint:", out)

    def test_settings_install_drops_stale_managed_telemetry_keeps_user_keys(self):
        # A prior connect left a Cardinal-managed telemetry block pointing at
        # an ingest key the org no longer backs; it must go. Foreign keys stay.
        self.settings_path.write_text(json.dumps({
            "theme": "dark",
            "telemetry": {"enabled": True, "otlpEndpoint": "https://old/v1/logs",
                          "cardinalManaged": True},
        }))
        rc, code, out, _ = self._run(
            self._bundle("ingest_endpoint_not_configured"), no_extension=True,
        )
        self.assertEqual((rc, code), (0, None), out)
        settings = json.loads(self.settings_path.read_text())
        self.assertNotIn("telemetry", settings)
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["mcpServers"]["cardinal"]["url"], self.MCP["url"])
        self.assertIn("telemetry ingest unavailable: ingest_endpoint_not_configured", out)

    def test_user_owned_telemetry_block_is_left_alone(self):
        user_block = {"enabled": True, "target": "local"}
        self.settings_path.write_text(json.dumps({"telemetry": user_block}))
        rc, code, out, _ = self._run(self._bundle("no_lakerunner_integration"))
        self.assertEqual((rc, code), (0, None), out)
        self.assertEqual(json.loads(self.settings_path.read_text())["telemetry"], user_block)

    def test_no_ingest_and_no_mcp_fails_and_writes_nothing(self):
        rc, code, out, _ = self._run(
            self._bundle("no_lakerunner_integration", mcp=False), telemetry_only=True,
        )
        self.assertIsNone(rc)
        self.assertIn("telemetry ingest unavailable: no_lakerunner_integration", str(code))
        self.assertIn("Nothing to connect", str(code))
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.settings_path.exists())

    def test_status_reports_unavailable_reason_without_failing(self):
        self.state_path.write_text(json.dumps({
            "mode": "mcp-only",
            "telemetry": {"enabled": False, "unavailable_reason": "no_lakerunner_integration"},
        }))
        proc = subprocess.run(
            [sys.executable, str(STATUS)], capture_output=True, text=True, timeout=30,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
        )
        self.assertIn("Telemetry:       unavailable (no_lakerunner_integration)", proc.stdout)
        self.assertNotIn("FAIL Cardinal ingest", proc.stdout)


class StatusHookRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        gemini = self.home / ".gemini"
        (gemini / "extensions" / "cardinal" / "hooks").mkdir(parents=True)
        # Connected state without endpoints → status makes no network probes.
        (gemini / "cardinal.json").write_text(json.dumps({"mode": "telemetry-and-mcp"}))
        self.hooks_file = gemini / "extensions" / "cardinal" / "hooks" / "hooks.json"

    def tearDown(self):
        self.tmp.cleanup()

    def status(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(STATUS)], capture_output=True, text=True, timeout=30,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
        )

    def test_reports_pre_017_extension_hooks(self):
        self.hooks_file.write_text(json.dumps({"BeforeAgent": [{"hooks": [{
            "type": "command", "command": f"python3 /x --event BeforeAgent # {MARKER}", "timeout": 5}]}]}))
        proc = self.status()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("no top-level \"hooks\" object", proc.stdout)
        self.assertIn("Re-run cardinal-connect", proc.stdout)

    def test_reports_seconds_style_settings_timeouts(self):
        self.hooks_file.write_text(SOURCE_HOOKS.read_text())
        (self.home / ".gemini" / "settings.json").write_text(json.dumps({"hooks": {"AfterTool": [{"hooks": [{
            "type": "command", "command": f"python3 /x --event AfterTool # {MARKER}", "timeout": 5}]}]}}))
        proc = self.status()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("under 1000ms for AfterTool", proc.stdout)

    def test_current_registration_is_clean(self):
        self.hooks_file.write_text(SOURCE_HOOKS.read_text())
        proc = self.status()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn("FAIL", proc.stdout)


class ActTokenTests(unittest.TestCase):
    """The control-plane token (maestro:act + dashboards:write / alerts:write /
    telemetry:query) that migrate-from-grafana reads from the secrets and state
    files. Device flow and probes are stubbed at cardinal_core.deviceflow."""

    ALL = ["ingest:write", "mcp:invoke", "maestro:act",
           "dashboards:write", "alerts:write", "telemetry:query"]
    ACT = {
        "endpoint": "https://cardinal.example",
        "api_key": "ACTPLAINTEXT" + "z" * 52,
        "key_id": "act-key-1",
        "key_prefix": "ACTPLAIN",
        "created_at": "2026-10-02T00:00:00Z",
        "scopes": ["maestro:act", "dashboards:write", "alerts:write", "telemetry:query"],
    }
    MCP = {
        "url": "https://cardinal.example/api/orgs/org-1/mcp",
        "api_key": "MCPPLAINTEXT" + "x" * 52,
        "key_id": "mcp-key-1",
        "key_prefix": "MCPPLAIN",
    }

    @classmethod
    def setUpClass(cls):
        _ensure_vendored()

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".gemini").mkdir()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        name = f"cardinal_connect_act_{id(self)}"
        loader = importlib.machinery.SourceFileLoader(name, str(CONNECT))
        spec = importlib.util.spec_from_loader(name, loader)
        self.connect = importlib.util.module_from_spec(spec)
        loader.exec_module(self.connect)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self.tmp.cleanup()

    def _bundle(self, act: dict | None) -> dict:
        return {
            "org": {"id": "org-1", "slug": "acme"},
            "user": {"email": "dev@example.com"},
            "mcp": dict(self.MCP), "ingest": None,
            "ingest_unavailable_reason": "no_lakerunner_integration", "limits": None,
            "act": act,
        }

    def _run(self, bundle: dict, extra_scopes=("dashboards:write", "alerts:write", "telemetry:query")):
        args = argparse.Namespace(
            host="https://cardinal.example", telemetry_only=False, deployment_env=None,
            dry_run=False, no_extension=False,
            extra_scopes=list(extra_scopes), act_requested=True,
        )
        df = self.connect.deviceflow
        out = io.StringIO()
        with mock.patch.object(df, "poll_device_token", return_value=bundle), \
                mock.patch.object(df, "verify_mcp_reachable", return_value=(True, "HTTP 405")), \
                mock.patch.object(df, "revoke_maestro_key", return_value=(True, "HTTP 204")) as revoke, \
                contextlib.redirect_stdout(out):
            rc = self.connect.continue_after_grant(args, {"device_code": "dc", "interval": 1})
        return rc, out.getvalue(), revoke

    def _requested_scopes(self, argv: list[str]) -> list[list[str]]:
        calls = []
        df = self.connect.deviceflow

        def start(host, scopes, client_id):
            calls.append(list(scopes))
            raise df.DeviceFlowError("stop here")

        with mock.patch.object(sys, "argv", ["cardinal-connect", *argv]), \
                mock.patch.object(df, "start_device_code", side_effect=start), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.connect.main()
        return calls

    def test_requests_act_token_and_extra_scopes_by_default(self) -> None:
        calls = self._requested_scopes([])
        # Rejected (as an older server would): retried once without the defaults.
        self.assertEqual(calls, [self.ALL, ["ingest:write", "mcp:invoke"]])

    def test_minimal_scopes_requests_no_act_token(self) -> None:
        self.assertEqual(self._requested_scopes(["--minimal-scopes"]),
                         [["ingest:write", "mcp:invoke"]])

    def test_named_scope_is_required(self) -> None:
        calls = self._requested_scopes(["--minimal-scopes", "telemetry:query"])
        self.assertEqual(calls, [["ingest:write", "mcp:invoke", "maestro:act", "telemetry:query"]])

    def test_writes_act_secret_and_state(self) -> None:
        rc, out, revoke = self._run(self._bundle(dict(self.ACT)))
        self.assertEqual(rc, 0, out)
        secrets = json.loads(((self.home / ".gemini") / "cardinal-secrets.json").read_text())
        self.assertEqual(secrets["act_api_key"], self.ACT["api_key"])
        self.assertEqual(secrets["act_endpoint"], self.ACT["endpoint"])
        state_text = ((self.home / ".gemini") / "cardinal.json").read_text()
        self.assertNotIn("ACTPLAINTEXT", state_text)
        state = json.loads(state_text)
        self.assertEqual(state["act_key_id"], "act-key-1")
        self.assertEqual(state["act_scopes"], self.ACT["scopes"])
        self.assertIn("Actions: enabled (maestro:act, dashboards:write", out)
        self.assertNotIn("Not granted", out)
        revoke.assert_not_called()

    def test_scopes_not_echoed_warns_not_granted(self) -> None:
        act = dict(self.ACT)
        del act["scopes"]
        rc, out, _ = self._run(self._bundle(act))
        self.assertEqual(rc, 0, out)
        self.assertIn("Not granted: dashboards:write, alerts:write, telemetry:query", out)
        state = json.loads(((self.home / ".gemini") / "cardinal.json").read_text())
        self.assertEqual(state["act_scopes"], ["maestro:act"])

    def test_reconnect_without_act_token_revokes_prior_one(self) -> None:
        ((self.home / ".gemini") / "cardinal.json").write_text(json.dumps(
            {"act_key_id": "old-act", "act_key_prefix": "OLDACT"}))
        ((self.home / ".gemini") / "cardinal-secrets.json").write_text(json.dumps(
            {"act_api_key": "OLDPLAINTEXT"}))
        rc, out, revoke = self._run(self._bundle(None))
        self.assertEqual(rc, 0, out)
        revoke.assert_called_once_with("https://cardinal.example", "old-act", "OLDPLAINTEXT")
        self.assertIn("Revoked prior control-plane token OLDACT", out)
        self.assertIn("Cardinal returned no control-plane token", out)
        self.assertNotIn("act_api_key", json.loads(((self.home / ".gemini") / "cardinal-secrets.json").read_text()))


if __name__ == "__main__":
    unittest.main()

"""Hook-registration tests for scripts/cardinal-connect and cardinal-status.

Gemini CLI reads hook `timeout` in milliseconds and loads extension hooks
only from a top-level `hooks` object (packages/cli/src/config/
extension-manager.ts, loadExtensionHooks). These tests pin what
cardinal-connect writes, that a re-run repairs registrations from older
plugin versions, and that cardinal-status reports a stale registration.
No network: only the local writers are exercised.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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

    def test_registered_events_and_timeout_constants(self):
        self.assertEqual(self.connect.HOOK_TIMEOUT_MS, 10000)
        self.assertNotIn("AfterAgent", self.connect.REGISTERED_EVENTS)
        source = json.loads(SOURCE_HOOKS.read_text())
        self.assertEqual(tuple(source["hooks"]), self.connect.REGISTERED_EVENTS)

    def test_settings_json_hooks_use_millisecond_timeouts(self):
        self.connect.apply_settings(
            {"endpoint": "https://ingest.example", "api_key": "k"}, None, None, include_hooks=True,
        )
        settings = json.loads(self.settings_path.read_text())
        handlers = _handlers(settings["hooks"])
        self.assertEqual(sorted(e for e, _ in handlers), sorted(self.connect.REGISTERED_EVENTS))
        for event, handler in handlers:
            with self.subTest(event=event):
                self.assertEqual(handler["timeout"], 10000)
                self.assertIn(f"--event {event}", handler["command"])
        self.assertTrue(settings["telemetry"]["cardinalManaged"])

    def test_extension_hooks_json_shape(self):
        self.connect.install_extension_bundle(None, None)
        data = json.loads(self.ext_hooks.read_text())
        self.assertIsInstance(data["hooks"], dict)
        handlers = _handlers(data["hooks"])
        self.assertEqual(sorted(e for e, _ in handlers), sorted(self.connect.REGISTERED_EVENTS))
        for event, handler in handlers:
            with self.subTest(event=event):
                self.assertEqual(handler["timeout"], 10000)
                self.assertNotIn("__HOOK_SCRIPT__", handler["command"])
                self.assertIn(str(ADAPTER / "hooks" / "cardinal-gemini-telemetry.py"), handler["command"])

    def test_rerun_repairs_old_extension_hooks_json(self):
        self.connect.install_extension_bundle(None, None)
        old = {"_comment": "pre-0.17 shape", "BeforeAgent": [{"matcher": "", "hooks": [{
            "type": "command", "command": f"python3 /x --event BeforeAgent # {MARKER}", "timeout": 5}]}]}
        self.ext_hooks.write_text(json.dumps(old))
        self.assertEqual(self.connect.repair_hook_registration(), [str(self.ext_hooks)])
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
        self.assertNotIn("AfterAgent", settings["hooks"])
        self.assertIn(user_group, settings["hooks"]["BeforeAgent"])
        for _, handler in _handlers(settings["hooks"]):
            self.assertEqual(handler["timeout"], 10000)
        self.assertEqual(self.connect.repair_hook_registration(), [], "repair must be idempotent")

    def test_rerun_leaves_settings_without_cardinal_hooks_alone(self):
        self.settings_path.write_text(json.dumps({"hooks": {"BeforeAgent": []}}))
        self.assertEqual(self.connect.repair_hook_registration(), [])


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


if __name__ == "__main__":
    unittest.main()

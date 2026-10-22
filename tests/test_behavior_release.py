"""Exercise the behavior MCP and the Checks preview using the same artifact
shipped to users of every adapter (Claude, Codex, Cursor, Gemini CLI, Copilot CLI)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("behavior_release_build", ROOT / "build" / "release.py")
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


ADAPTERS = ("claude", "codex", "cursor", "gemini", "copilot")
CHECKS_TOOLS = {'get_behavior_range', 'list_accepted_checks', 'set_check_example', 'approve_check_example',
                'remove_check_example', 'get_check_draft', 'lock_check_examples',
                'execute_behavior_range', 'continue_behavior_range'}


def bin_dir(adapter):
    return "bin" if adapter == "claude" else "scripts"


class BehaviorReleaseTests(unittest.TestCase):
    def test_packaged_launcher_exposes_authoring_without_source_checkout_or_config(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter):
                self.check_packaged_launcher(adapter)

    def check_packaged_launcher(self, adapter):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "plugins" / "cardinal"
            RELEASE.build_artifact(adapter, package)
            launcher = package / bin_dir(adapter) / "cardinal-behavior"
            help_result = subprocess.run([sys.executable, str(launcher), "--help"],
                                         cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("--config", help_result.stdout)
            # The normal installed MCP starts without frozen local artifacts/config.
            environment = dict(os.environ, CARDINAL_BEHAVIOR_OUTPUT_DIR=str(root / 'receipts'), HOME=str(root / 'home'))
            environment.pop('CARDINAL_BEHAVIOR_CONFIG', None)
            environment.pop('CARDINAL_BEHAVIOR_RUNTIME', None)
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            ]
            result = subprocess.run([sys.executable, str(launcher)],
                input="".join(json.dumps(request) + "\n" for request in requests),
                cwd=root, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(responses), 3)
            self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "cardinal-behavior")
            self.assertIn("get_behavior_execution", [tool["name"] for tool in responses[1]["result"]["tools"]])
            names = {tool['name'] for tool in responses[1]['result']['tools']}
            self.assertTrue({'get_behavior_sdk', 'compile_behavior', 'inspect_behavior', 'test_behavior', 'accept_behavior',
                             'get_behavior_compilation', 'run_behavior_regressions',
                             'execute_behavior', 'next_behavior_result', 'render_storyboard'} <= names)
            self.assertNotIn('select_behavior', names)
            self.assertTrue(CHECKS_TOOLS <= names, CHECKS_TOOLS - names)
            if adapter == "claude":
                self.assertEqual((package / ".mcp.json").read_bytes(),
                                 (ROOT / "adapters" / "claude" / ".mcp.json").read_bytes())
            self.assertFalse((package / "lib" / "behavior" / "tests").exists())
            # The Checks preview: template (opt-in, never pre-installed) and a CLI
            # that runs from the artifact alone (core + lib/behavior inside it).
            self.assertTrue((package / "checks" / "skill" / "SKILL.md").is_file())
            self.assertEqual((package / "checks" / "skill" / "CORE.md").read_bytes(),
                             (ROOT / "common" / "checks" / "CORE.md").read_bytes())
            self.assertFalse((package / "skills" / "checks").exists())
            status = subprocess.run([sys.executable, str(package / bin_dir(adapter) / "cardinal-checks"), "status"],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Checks preview: off", status.stdout)
            self.assertIn("Accepted checks: 0", status.stdout)
            RELEASE.validate_artifact(adapter, package)
            # Offline components must import from a built artifact with no source
            # checkout or external experiment on sys.path. SDK imports are lazy.
            probe = subprocess.run([sys.executable, '-c',
                'import sys; sys.path.insert(0, sys.argv[1]); '
                'from compilation.workflow import compile_and_qualify; '
                'from compilation.sdk_adapter import SDK; '
                'from compilation.lifecycle import requalify, collect_sample; '
                'from compilation.identity import identity_from_files; '
                'from compilation.llm import Model; '
                'from compilation.engine import promotion_gate; '
                'print("offline compilation package available")', str(package / 'lib/behavior')],
                cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertIn('offline compilation package available', probe.stdout)


if __name__ == "__main__":
    unittest.main()

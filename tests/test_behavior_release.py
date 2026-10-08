"""Exercise the behavior MCP using the same artifact shipped to Claude users."""
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


class BehaviorReleaseTests(unittest.TestCase):
    def test_packaged_launcher_exposes_authoring_without_source_checkout_or_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "plugins" / "cardinal"
            RELEASE.build_artifact("claude", package)
            launcher = package / "bin" / "cardinal-behavior"
            help_result = subprocess.run([sys.executable, str(launcher), "--help"],
                                         cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("--config", help_result.stdout)
            # The normal installed MCP starts without frozen local artifacts/config.
            environment = dict(os.environ, CARDINAL_BEHAVIOR_OUTPUT_DIR=str(root / 'receipts'))
            environment.pop('CARDINAL_BEHAVIOR_CONFIG', None)
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
            self.assertTrue({'get_behavior_sdk', 'compile_behavior', 'inspect_behavior', 'accept_behavior',
                             'execute_behavior', 'next_behavior_result', 'render_storyboard'} <= names)
            self.assertNotIn('select_behavior', names)
            self.assertEqual((package / ".mcp.json").read_bytes(),
                             (ROOT / "adapters" / "claude" / ".mcp.json").read_bytes())
            self.assertFalse((package / "lib" / "behavior" / "tests").exists())


if __name__ == "__main__":
    unittest.main()

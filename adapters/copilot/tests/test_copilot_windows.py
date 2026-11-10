"""Windows support checks that run without Windows (issue #244).

  - PowerShellCommandTests: every `powershell` command in hooks/hooks.json,
    run by PowerShell 7 (`pwsh`, which also runs on Linux and macOS) the way
    the `bash` commands are run by the hook tests. Skipped without `pwsh`;
    CI sets CARDINAL_REQUIRE_PWSH=1 so a missing `pwsh` fails there.
  - WindowsEncodingTests: the hook with a Windows-style stdin encoding.

Neither proves Windows works: Copilot CLI is not run, so how it invokes the
`powershell` command and delivers stdin on Windows is not covered.

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_windows.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from fixtures import ADAPTER, SID, HookCase, clean_env

HOOKS_JSON = ADAPTER / "hooks" / "hooks.json"
PWSH = shutil.which("pwsh")
TEXT = "say “hi” \U0001f642"  # curly quotes and an emoji: not ASCII, not cp1252-safe

# Stands in for the hook script: reports what the command line delivered.
ECHO_HOOK = """import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.buffer.read().decode("utf-8")}))
sys.exit(int(os.environ.get("ECHO_HOOK_EXIT", "0")))
"""


def powershell_entries() -> list:
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    return [(event, entry) for event, entries in hooks.items() for entry in entries]


class PowerShellCommandTests(HookCase):
    def setUp(self) -> None:
        if not PWSH:
            if os.environ.get("CARDINAL_REQUIRE_PWSH") == "1":
                self.fail("pwsh is not installed and CARDINAL_REQUIRE_PWSH=1")
            self.skipTest("pwsh is not installed")
        super().setUp()
        self.echo_root = self.base / "echo-plugin"
        (self.echo_root / "hooks").mkdir(parents=True)
        (self.echo_root / "hooks" / "cardinal-copilot-telemetry.py").write_text(ECHO_HOOK, encoding="utf-8")

    def pwsh(self, command: str, payload: dict, root: Path, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
                              input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True,
                              env={**self.env(CARDINAL_COPILOT_PLUGIN_ROOT=str(root)), **env}, timeout=120)

    def test_every_command_delivers_the_event_and_the_payload(self):
        payload = self.tool(args={"command": TEXT})
        for event, entry in powershell_entries():
            with self.subTest(command=entry["powershell"]):
                res = self.pwsh(entry["powershell"], payload, self.echo_root)
                self.assertEqual(res.returncode, 0, res.stderr)
                seen = json.loads(res.stdout.decode("utf-8"))
                self.assertEqual(seen["argv"][:2], ["--event", event])
                self.assertEqual("--role" in seen["argv"], "--role" in entry["bash"])
                self.assertEqual(json.loads(seen["stdin"]), payload)

    def test_every_command_exits_zero_when_the_hook_fails(self):
        # A non-zero preToolUse exit denies the tool on Copilot.
        for _, entry in powershell_entries():
            with self.subTest(command=entry["powershell"]):
                res = self.pwsh(entry["powershell"], self.tool(), self.echo_root, ECHO_HOOK_EXIT="3")
                self.assertEqual(res.returncode, 0, res.stderr)

    def test_every_command_exits_zero_without_an_interpreter(self):
        empty = self.base / "empty-path"
        empty.mkdir()
        for _, entry in powershell_entries():
            with self.subTest(command=entry["powershell"]):
                res = self.pwsh(entry["powershell"], self.tool(), self.echo_root, PATH=str(empty))
                self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)

    def test_the_real_hook_answers_through_powershell(self):
        [entry] = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["sessionStart"]
        res = self.pwsh(entry["powershell"], self.payload(source="startup"), ADAPTER)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("cardinal-connect", json.loads(res.stdout.decode("utf-8"))["additionalContext"])


class WindowsEncodingTests(HookCase):
    # Fixed by #249: stdin is decoded in the ANSI code page on Windows before Python 3.15.
    @unittest.expectedFailure
    def test_utf8_payload_survives_a_cp1252_stdin(self):
        self.connect()
        self.run_hook("postToolUse", raw=json.dumps(self.tool(args={"command": TEXT}), ensure_ascii=False),
                      PYTHONIOENCODING="cp1252")
        [(_, result)] = self.records("tool_result")
        self.assertEqual(json.loads(result["tool_input"]), {"command": TEXT})
        self.assertEqual(result["session_id"], SID)


if __name__ == "__main__":
    unittest.main()

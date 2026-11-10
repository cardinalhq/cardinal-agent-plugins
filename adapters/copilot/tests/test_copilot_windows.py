"""Windows support checks that run without Windows (issue #244).

  - PowerShellCommandTests: every `powershell` command in hooks/hooks.json,
    run by PowerShell 7 (`pwsh`, which also runs on Linux and macOS) the way
    the `bash` commands are run by the hook tests. Skipped without `pwsh`;
    CI sets CARDINAL_REQUIRE_PWSH=1 so a missing `pwsh` fails there.
  - WindowsEncodingTests: the hook with a Windows-style stdin encoding.
  - PythonLauncherTests: the Windows branches that name the interpreter
    (issue #245), called directly.

Neither proves Windows works: Copilot CLI is not run, so how it invokes the
`powershell` command and delivers stdin on Windows is not covered.

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_windows.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from fixtures import ADAPTER, DECISION_CLI, HOOK, SID, HookCase, load_script

sys.path.insert(0, str(ADAPTER / "hooks"))  # the vendored cardinal_core
from cardinal_core.paths import script_command  # noqa: E402

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

    def fake_path(self, name: str, programs: dict) -> str:
        """A PATH directory holding only the given programs (name -> sh script body)."""
        bin_dir = self.base / name
        bin_dir.mkdir()
        for program, body in programs.items():
            (bin_dir / program).write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
            (bin_dir / program).chmod(0o755)
        return str(bin_dir)

    def test_the_first_interpreter_that_works_runs_the_hook(self):
        # python.org installs have no python3; the Microsoft Store stub is a
        # python3 that prints a message and fails; the py launcher takes -3.
        real = f'exec "{sys.executable}" "$@"'
        stub = 'echo "Python was not found; run without arguments to install from the Microsoft Store"; exit 49'
        cases = {
            "python-only": {"python": real},
            "store-stub-then-python": {"python3": stub, "python": real},
            "py-launcher-only": {"py": f'[ "$1" = -3 ] || exit 2; shift; {real}'},
        }
        payload = self.tool(args={"command": TEXT})
        for label, programs in cases.items():
            path = self.fake_path(label, programs)
            for event, entry in powershell_entries():
                with self.subTest(case=label, command=entry["powershell"]):
                    res = self.pwsh(entry["powershell"], payload, self.echo_root, PATH=path)
                    self.assertEqual(res.returncode, 0, res.stderr)
                    seen = json.loads(res.stdout.decode("utf-8"))  # nothing but the hook's JSON on stdout
                    self.assertEqual(seen["argv"][:2], ["--event", event])
                    self.assertEqual(json.loads(seen["stdin"]), payload)

    def test_a_script_command_runs_from_any_folder_name(self):
        # script_command's Windows form is PowerShell: $ and ` must stay literal.
        for folder in ("plain", "a b", "do$llar", "ba`ck", "apo's"):
            with self.subTest(folder=folder):
                script = self.base / folder / "scripts" / "cardinal-decision"
                script.parent.mkdir(parents=True)
                script.write_text("import sys; print('|'.join(sys.argv[1:]))\n", encoding="utf-8")
                command = script_command(script, "", windows=True) + " record --choice 'Use X'"
                res = self.pwsh(command, {}, self.echo_root)
                self.assertEqual((res.returncode, res.stdout.decode("utf-8").strip()), (0, "record|--choice|Use X"),
                                 res.stderr)
                hook = load_script(HOOK, "copilot_hook_pwsh")
                self.assertEqual(hook.decision_invocations(command, windows=False)[1], None)
                self.assertEqual(hook.decision_invocations(command.replace("/", "\\"), windows=True),
                                 ([["record", "--choice", "Use X"]], None))

    def test_the_real_hook_answers_through_powershell(self):
        [entry] = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["sessionStart"]
        res = self.pwsh(entry["powershell"], self.payload(source="startup"), ADAPTER)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("cardinal-connect", json.loads(res.stdout.decode("utf-8"))["additionalContext"])


class WindowsEncodingTests(HookCase):
    """Windows before Python 3.15 opens piped streams and text files in the
    ANSI code page (cp1252); Copilot and Cardinal use UTF-8 (issue #249)."""

    def test_utf8_payload_survives_a_cp1252_stdin(self):
        self.connect()
        self.run_hook("postToolUse", raw=json.dumps(self.tool(args={"command": TEXT}), ensure_ascii=False),
                      PYTHONIOENCODING="cp1252")
        [(_, result)] = self.records("tool_result")
        self.assertEqual(json.loads(result["tool_input"]), {"command": TEXT})
        self.assertEqual(result["session_id"], SID)

    def test_a_utf8_mcp_config_is_read_whatever_the_default_encoding(self):
        # A server Copilot names in UTF-8: its tools are split from the server name only if the file is read.
        (self.copilot / "mcp-config.json").write_text(json.dumps({"mcpServers": {"caf\u00e9-\U0001f642": {}}},
                                                                 ensure_ascii=False), encoding="utf-8")
        self.connect()
        self.run_hook("postToolUse", self.tool(name="caf\u00e9-\U0001f642-do_thing", args={}))
        turn = self.records("cardinal.turn_tool")[-1][1]
        self.assertEqual((turn["mcp_server_name"], turn["mcp_tool_name"]), ("caf\u00e9-\U0001f642", "do_thing"))


class PythonLauncherTests(unittest.TestCase):
    """What the plugin writes or tells the agent on Windows, where `python3`
    may not exist. Each function takes `windows=` so the branch runs here."""

    @classmethod
    def setUpClass(cls):
        cls.hook = load_script(HOOK, "copilot_hook_windows")
        cls.config = load_script(ADAPTER / "hooks" / "_copilot_config.py", "copilot_config_windows")

    def test_the_behavior_mcp_entry_names_the_running_interpreter(self):
        script = Path("C:/plugin/scripts/cardinal-behavior")
        entry = self.config.build_behavior_entry(script, windows=True)
        self.assertEqual((entry["command"], entry["args"]), (sys.executable, [str(script), "--runtime", "copilot"]))
        self.assertEqual(self.config.build_behavior_entry(script, windows=False)["command"], "python3")

    def test_a_script_command_is_powershell_with_the_running_interpreter(self):
        script = r"C:\Users\a b\plugin\scripts\cardinal-decision"
        self.assertEqual(script_command(script, "python3 x", windows=True), f"& '{sys.executable}' '{script}'")
        self.assertTrue(script_command("C:\\it's\\x", "", windows=True).endswith(" 'C:\\it''s\\x'"))
        self.assertEqual(script_command(script, "python3 x", windows=False), "python3 x")

    def test_the_decision_command_handed_out_on_windows_is_recognized(self):
        script = r"C:\Users\a b\plugin\scripts\cardinal-decision"
        handed_out = script_command(script, "", windows=True).replace(sys.executable, r"C:\Program Files\Python312\python.exe")
        want = [["record", "--choice", "Use X"]]
        for command in (
            f"{handed_out} record --choice 'Use X'",
            rf"C:\Python312\Python.EXE C:\plugin\scripts\cardinal-decision record --choice 'Use X'",
            rf"py -3 C:/plugin/scripts/cardinal-decision record --choice 'Use X'",
            rf'cd C:\repo; python "C:\plugin\scripts\cardinal-decision" record --choice "Use X"',
        ):
            with self.subTest(command=command):
                self.assertEqual(self.hook.decision_invocations(command, windows=True), (want, None))
        # POSIX parsing is unchanged: a backslash is still an escape there, and .exe is not an interpreter.
        self.assertEqual(self.hook.decision_invocations(
            r"C:\Python312\python.exe C:\plugin\scripts\cardinal-decision record --choice X", windows=False), ([], None))

    def test_the_hook_hands_out_a_command_it_recognizes(self):
        # Whatever this OS's form is (PowerShell with the interpreter's path under a Windows Python).
        command = f"{self.hook.DECISION_CMD} record --choice 'Use X'"
        self.assertEqual(self.hook.decision_invocations(command), ([["record", "--choice", "Use X"]], None))
        self.assertIn(str(DECISION_CLI), self.hook.DECISION_CMD)


if __name__ == "__main__":
    unittest.main()

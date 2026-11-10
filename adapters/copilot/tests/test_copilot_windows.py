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
from tempfile import TemporaryDirectory
from unittest import mock

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
        """A PATH directory holding only the given programs (name -> kind):
        `real` runs this interpreter, `stub` prints the Microsoft Store hint
        and fails, `py` is the launcher (requires -3, then runs this
        interpreter). sh scripts, or .cmd files under a Windows Python."""
        hint = "Python was not found; run without arguments to install from the Microsoft Store"
        bin_dir = self.base / name
        bin_dir.mkdir()
        for program, kind in programs.items():
            if os.name == "nt":
                body = {"real": f'@"{sys.executable}" %*', "stub": f"@echo {hint}\r\n@exit /b 49",
                        "py": f'@if not "%1"=="-3" exit /b 2\r\n@"{sys.executable}" %2 %3 %4 %5 %6 %7 %8 %9'}[kind]
                (bin_dir / (program + ".cmd")).write_text(body + "\r\n", encoding="utf-8")
            else:
                body = {"real": f'exec "{sys.executable}" "$@"', "stub": f'echo "{hint}"; exit 49',
                        "py": f'[ "$1" = -3 ] || exit 2; shift; exec "{sys.executable}" "$@"'}[kind]
                (bin_dir / program).write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
                (bin_dir / program).chmod(0o755)
        return str(bin_dir)

    def test_the_first_interpreter_that_works_runs_the_hook(self):
        # python.org installs have no python3; the Microsoft Store stub is a
        # python3 that prints a message and fails; the py launcher takes -3.
        cases = {
            "python-only": {"python": "real"},
            "store-stub-then-python": {"python3": "stub", "python": "real"},
            "py-launcher-only": {"py": "py"},
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

    def test_quoted_arguments_arrive_as_written(self):
        # shell_quote's Windows form, as the watch / grant commands use it for paths (issue #247).
        from cardinal_core.paths import shell_quote
        script = self.base / "argv.py"
        script.write_text("import json, sys; print(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        args = ["inv_abc123", "C:\\Users\\O'Brien folder\\permit.json", "do$llar", "ba`ck", "a b", "",
                "O\u2019Brien \u2018x\u201a\u201b", "a,b", "@splat", "x; Write-Output injected"]
        command = script_command(script, "", windows=True) + " " + " ".join(shell_quote(a, windows=True) for a in args)
        res = self.pwsh(command, {}, self.echo_root)
        self.assertEqual((res.returncode, json.loads(res.stdout.decode("utf-8"))), (0, args), res.stderr)

    def test_the_grant_env_line_sets_both_variables(self):
        from cardinal_core import investigation_grants as grants
        token, origin = "abc123", "https://app.example.com/o'brien\u2019;x"  # fake values
        line = grants.env_line(token, origin, windows=True)
        show = f"; Write-Output $env:{grants.TOKEN_ENV}; Write-Output $env:{grants.ORIGIN_ENV}"
        res = self.pwsh(line + show, {}, self.echo_root)
        self.assertEqual((res.returncode, res.stdout.decode("utf-8").split()), (0, [token, origin]), res.stderr)
        self.assertEqual(grants.env_line("t", "o", windows=False), f"export {grants.TOKEN_ENV}=t {grants.ORIGIN_ENV}=o")

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
        self.assertEqual((entry["command"], entry["args"]),
                         (sys.executable, ["-X", "utf8", str(script), "--runtime", "copilot"]))
        self.assertEqual(entry["tools"], ["*"])
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


class PreviewDiscoveryTests(unittest.TestCase):
    """Where the local storyboard preview looks for a browser on Windows (issue #251)."""

    @classmethod
    def setUpClass(cls):
        cls.render = load_script(ADAPTER / "skills" / "storyboard" / "scripts" / "render_preview.py", "render_preview_win")

    ENV = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
           "LOCALAPPDATA": r"C:\Users\a\AppData\Local"}

    def test_chrome_edge_and_chromium_installs_then_path(self):
        found = self.render.chromium_candidates(self.ENV, "Windows", Path("/nonexistent-home"), which=lambda c: None)
        paths = [path for _, path in found]
        self.assertEqual(paths[:3], [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                                     r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                                     r"C:\Program Files\Chromium\Application\chrome.exe"])
        self.assertIn(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", paths)
        self.assertIn(r"C:\Users\a\AppData\Local\Google\Chrome\Application\chrome.exe", paths)
        self.assertEqual([path for source, path in found if source == "PATH"], [])  # nothing on PATH: no bare names

    def test_the_explicit_binary_is_the_only_candidate(self):
        env = dict(self.ENV, CARDINAL_CHROMIUM=r"D:\tools\chrome.exe")
        self.assertEqual(self.render.chromium_candidates(env, "Windows", Path("/h")),
                         [("$CARDINAL_CHROMIUM", r"D:\tools\chrome.exe")])

    def test_other_systems_are_unchanged(self):
        for system in ("Darwin", "Linux"):
            found = self.render.chromium_candidates(self.ENV, system, Path("/nonexistent-home"), which=lambda c: None)
            self.assertFalse([p for _, p in found if "Program Files" in p or p.endswith(".exe")], system)

    def test_the_first_existing_browser_is_used(self):
        with TemporaryDirectory() as d:
            # ntpath.join, as the renderer builds it: nested folders on Windows, one odd file name elsewhere.
            edge = self.render.ntpath.join(d, self.render.WINDOWS_APPS[1])
            os.makedirs(os.path.dirname(edge), exist_ok=True)
            Path(edge).write_text("")
            os.chmod(edge, 0o755)
            found, searched = self.render.find_chromium({"ProgramFiles": d}, "Windows", Path("/nonexistent-home"),
                                                        which=lambda c: None, version_of=lambda p: None)
            self.assertEqual((found["path"], found["source"]), (edge, "app"), searched)

    def test_a_browser_in_the_workspace_or_a_bare_name_is_never_taken_from_path(self):
        here = self.render.ntpath.abspath(os.getcwd())
        planted = self.render.ntpath.join(here, "chrome.exe")
        elsewhere = r"C:\Tools\Chromium\chrome.exe"
        for which, expected in ((lambda c: planted, []), (lambda c: "chrome.exe", []), (lambda c: None, []),
                                (lambda c: elsewhere, [elsewhere] * 3)):
            found = self.render.chromium_candidates({}, "Windows", Path("/nonexistent-home"), which=which)
            self.assertEqual([path for source, path in found if source == "PATH"], expected)

    def test_environment_names_are_matched_in_any_case(self):
        found = self.render.chromium_candidates({"PROGRAMFILES(X86)": r"C:\Program Files (x86)"}, "Windows",
                                                Path("/nonexistent-home"), which=lambda c: None)
        self.assertIn(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", [p for _, p in found])


# A bundle page that speaks the preview protocol: two steps, each settling after a moment.
PREVIEW_PAGE = """<!doctype html><body style="margin:0"><script>
window.cardinalPreview = {version: 1, steps: 2, state: "loading", step: null, errors: [],
  reveal(n) { return new Promise((r) => setTimeout(() => { this.state = "settled"; this.step = n; r(); }, 300)); },
  abort(reason) { this.aborted = reason; },
  report() { return {state: this.state, step: this.step, steps: 2, height: 800, error: null, frameErrors: [],
                     protocolErrors: this.aborted ? [this.aborted] : []}; }};
</script><iframe id="cv" sandbox="allow-scripts" style="border:0;width:1280px;height:800px;background:#246"
srcdoc="preview"></iframe></body>"""


@unittest.skipUnless(os.environ.get("CARDINAL_CHROMIUM_SMOKE") == "1",
                     "real-browser smoke test: set CARDINAL_CHROMIUM_SMOKE=1")
class RealBrowserPreviewTests(unittest.TestCase):
    """`render_preview.py --runtime copilot --from-json` with a real browser,
    a Copilot-format connection (~/.copilot/cardinal.json + cardinal-secrets.json)
    and a local stub serving the bundle page: issue #251's check, without
    Cardinal. With CARDINAL_PREVIEW_SMOKE_OUT set, the PNGs and the
    renderer's output are copied there."""

    def test_copilot_from_json_writes_pngs(self):
        import hashlib
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        key, org, sb = "test-mcp-key", "0f0f0f0f-0000-4000-8000-000000000001", "sb_0123456789abcdef01234567"
        body = PREVIEW_PAGE.encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                ok = "/scenes/rhythm/" in self.path and self.headers.get("X-CardinalHQ-API-Key") == key
                self.send_response(200 if ok else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body) if ok else 2))
                self.end_headers()
                self.wfile.write(body if ok else b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".copilot").mkdir()
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            (home / ".copilot" / "cardinal.json").write_text(json.dumps(
                {"schema_version": 1, "mcp_url": f"{origin}/api/orgs/{org}/mcp"}), encoding="utf-8")
            (home / ".copilot" / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": key}),
                                                                     encoding="utf-8")
            result = {
                "storyboard_id": sb, "revision": 3, "ok": True, "errors": [], "warnings": [], "materialization": [],
                "scenes": [{"id": "rhythm", "ok": True, "preview_bundle": {
                    "path": f"/api/orgs/{org}/storyboards/{sb}/scenes/rhythm/preview.html?revision=3",
                    "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "revision": 3}}],
                "local_preview": {"contract": "test", "viewport": {"width": 1280, "height": 800,
                                                                  "device_scale_factor": 2},
                                  "ready_timeout_ms": 10000, "settle_timeout_ms": 5000,
                                  "max_bundle_bytes": 33554432},
                "view_url": f"https://app.example/storyboards/{sb}",
            }
            src = home / "preview.json"
            src.write_text(json.dumps(result), encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_") or k == "CARDINAL_CHROMIUM"}
            env["HOME"] = str(home)  # where the renderer looks; the real profile stays, as the browser needs it
            res = subprocess.run([sys.executable, str(ADAPTER / "skills" / "storyboard" / "scripts" / "render_preview.py"),
                                  "--runtime", "copilot", "--from-json", str(src)], capture_output=True,
                                 encoding="utf-8", env=env, timeout=600)
            keep = os.environ.get("CARDINAL_PREVIEW_SMOKE_OUT")
            if keep:
                Path(keep).mkdir(parents=True, exist_ok=True)
                (Path(keep) / "copilot-from-json.stdout.jsonl").write_text(res.stdout, encoding="utf-8")
                (Path(keep) / "copilot-from-json.stderr.txt").write_text(res.stderr, encoding="utf-8")
            self.assertEqual(res.returncode, 0, res.stderr[-2000:] + res.stdout[-2000:])
            lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
            steps = [ln for ln in lines if ln.get("scene_id") == "rhythm"]
            self.assertEqual([s["step"] for s in steps], [0, 1])
            out_dir = home / ".copilot" / "cardinal" / "storyboards" / sb / "r3"
            for step in steps:
                png = Path(step["png"])
                self.assertEqual(png.parent, out_dir)
                data = png.read_bytes()
                self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 1000, data[:16])
                self.assertIsNone(step["error"])
                if keep:
                    shutil.copy(str(png), str(Path(keep) / ("copilot-" + png.name)))
            self.assertEqual(lines[-1]["summary"]["rendered"], 2)


if __name__ == "__main__":
    unittest.main()

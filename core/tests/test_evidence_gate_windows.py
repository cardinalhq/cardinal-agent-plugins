"""The evidence gate on Windows spellings (issue #250): PowerShell reads of
the environment and paths written with a drive letter and backslashes.

The rules read these on every OS, so the table runs everywhere; under a
Windows Python (the Wine job) it also runs with os.path being ntpath.
"""

from __future__ import annotations

import unittest
from unittest import mock

from cardinal_core import evidence_gate as gate

HOME = "/Users/alice"


class WindowsGateTests(unittest.TestCase):
    def setUp(self):
        self.rules = gate.load_rules(None)

    def command(self, text: str):
        return gate.check_command(text, self.rules, cwd="/repo", home=HOME)

    def path(self, text: str):
        return gate.check_paths(text, "/repo", HOME, self.rules, gate._Budget())

    def test_powershell_environment_reads_are_withheld(self):
        for text, rule in (
            ("Get-ChildItem Env:", "cmd.env-dump"), ("gci env:", "cmd.env-dump"), ("dir env:\\", "cmd.env-dump"),
            ("ls Env:*", "cmd.env-dump"), ("Get-Item Env:", "cmd.env-dump"),
            ("echo $env:GITHUB_TOKEN", "cmd.secret-var"), ("Write-Output $env:AWS_SECRET_ACCESS_KEY", "cmd.secret-var"),
            ('Write-Host "token: $env:NPM_TOKEN"', "cmd.secret-var"),
            # names are case-insensitive on Windows; braces; the Env: drive item; the .NET API
            ("echo $env:github_token", "cmd.secret-var"), ("echo ${env:GITHUB_TOKEN}", "cmd.secret-var"),
            ("Get-Content Env:GITHUB_TOKEN", "cmd.secret-var"), (r"gc env:\api_key", "cmd.secret-var"),
            ("[Environment]::GetEnvironmentVariable('GITHUB_TOKEN')", "cmd.secret-var"),
            ("[System.Environment]::GetEnvironmentVariables()", "cmd.env-dump"),
            # through a nested PowerShell
            ('powershell -NoProfile -Command "Get-ChildItem Env:"', "cmd.env-dump"),
            ("pwsh -c 'echo $env:GITHUB_TOKEN'", "cmd.env-dump"),  # the existing interpreter rule sees it first
            ("powershell.exe -Command echo $env:GITHUB_TOKEN", "cmd.secret-var"),
            ("powershell -EncodedCommand RwBlAHQALQBDAGgAaQBsAGQASQB0AGUAbQA=", "cmd.decoded-exec"),
        ):
            with self.subTest(command=text):
                hit = self.command(text)
                self.assertIsNotNone(hit, text)
                self.assertEqual(hit.rule, rule)

    def test_secret_files_by_windows_path_are_withheld(self):
        for text in (r"gc C:\Users\alice\.ssh\id_rsa", r"Get-Content C:\Users\alice\app\.env",
                     r"type $env:USERPROFILE\.aws\credentials", r"gc ~\.aws\credentials",
                     r"type %USERPROFILE%\.kube\config", r"cat D:\work\secrets\prod.json"):
            with self.subTest(command=text):
                self.assertIsNotNone(self.command(text), text)
        for text in (r"C:\Users\alice\.ssh\id_rsa", r"C:\Users\alice\app\.env", "C:/Users/alice/.aws/credentials",
                     r"\\fileserver\share\team\.env", r"c:\users\ALICE\.SSH\id_ed25519"):
            with self.subTest(path=text):
                self.assertIsNotNone(self.path(text), text)

    def test_ordinary_powershell_and_windows_paths_are_still_captured(self):
        for text in ("Get-ChildItem C:\\src", "echo $env:PATH", r"type C:\Users\alice\app\notes.txt",
                     "Get-ChildItem -Recurse src", "dir", r"gc C:\repo\README.md", "Write-Output $env:USERNAME",
                     "Get-Content Env:PATH", "[Environment]::GetEnvironmentVariable('PATH')",
                     'powershell -Command "Get-ChildItem C:\\src"'):
            with self.subTest(command=text):
                self.assertIsNone(self.command(text), text)
        for text in (r"C:\Users\alice\app\main.py", "C:/repo/src/app.ts", r"\\fileserver\share\docs\plan.md"):
            with self.subTest(path=text):
                self.assertIsNone(self.path(text), text)

    def test_profile_paths_with_spaces_through_the_public_check(self):
        key = r"C:\Users\Alice Smith\.ssh\id_rsa"
        for tool, tool_input in (("view", {"path": key}), ("Read", {"file_path": key}),
                                 ("powershell", {"command": f'Get-Content "{key}"'}),
                                 ("powershell", {"command": f"gc '{key}'"}),
                                 ("powershell", {"command": "Write-Output ${env:GITHUB_TOKEN}"}),
                                 ("view", {"path": r"C:\Users\Alice Smith\my app\.env"})):
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertIsNotNone(gate.check(tool, tool_input, cwd="/repo", home=HOME, rules=self.rules))
        for tool, tool_input in (("view", {"path": r"C:\Users\Alice Smith\my app\main.py"}),
                                 ("powershell", {"command": r'Get-Content "C:\Users\Alice Smith\notes for me.txt"'}),
                                 ("powershell", {"command": r"cd 'C:\Program Files\Git'; git status"})):
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertIsNone(gate.check(tool, tool_input, cwd="/repo", home=HOME, rules=self.rules))

    def test_long_path_prefixes_and_on_windows_any_backslash_path(self):
        self.assertIsNotNone(self.path("\\\\?\\C:\\Users\\alice\\.ssh\\id_rsa"))
        relative = (r"app\.env", r"..\.ssh\id_rsa", r".\secrets\prod.json")
        for text in relative:  # ambiguous with an escape elsewhere, so only where `\` is the separator
            self.assertIsNone(gate.windows_path(text), text)
        with mock.patch.object(gate, "WINDOWS", True):
            for text in relative:
                with self.subTest(path=text):
                    self.assertIsNotNone(self.path(text), text)
                    self.assertIsNotNone(self.command("gc " + text), text)
            self.assertIsNone(self.path(r"src\app\main.py"))

    def test_a_windows_path_is_read_as_its_posix_shape(self):
        self.assertEqual(gate.windows_path(r"C:\Users\a\.ssh\id_rsa"), "/Users/a/.ssh/id_rsa")
        self.assertEqual(gate.windows_path(r"$env:USERPROFILE\.aws\credentials"), "~/.aws/credentials")
        self.assertEqual(gate.windows_path(r"\\srv\share\x\.env"), "/share/x/.env")
        for not_windows in ("/Users/a/.env", "src/app.py", "a:b", "https://example.com/x", r"foo\.env"):
            self.assertIsNone(gate.windows_path(not_windows), not_windows)


if __name__ == "__main__":
    unittest.main()

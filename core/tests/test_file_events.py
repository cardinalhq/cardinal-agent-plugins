from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cardinal_core.file_events import file_events_from_hook


class FileEventAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.cwd = Path(self.temporary.name)
        self.first = self.cwd / "first.py"
        self.second = self.cwd / "second.py"
        self.first.write_text("print('first')\n")
        self.second.write_text("print('second')\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self, tool: str, tool_input: dict, event: str = "PostToolUse") -> dict:
        return {
            "hook_event_name": event,
            "tool_name": tool,
            "tool_input": tool_input,
            "cwd": str(self.cwd),
        }

    def test_claude_file_tools_are_exact(self) -> None:
        self.assertEqual(
            file_events_from_hook(self.payload("Read", {"file_path": str(self.first)})),
            [("read", str(self.first))],
        )
        for tool in ("Write", "Edit", "MultiEdit"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    file_events_from_hook(
                        self.payload(tool, {"file_path": str(self.second)})
                    ),
                    [("updated", str(self.second))],
                )

    def test_codex_apply_patch_extracts_every_target(self) -> None:
        patch = """*** Begin Patch
*** Update File: first.py
@@
-old
+new
*** Add File: created.py
+created
*** Delete File: second.py
*** End Patch"""
        self.assertEqual(
            file_events_from_hook(self.payload("apply_patch", {"command": patch})),
            [
                ("updated", str(self.first)),
                ("updated", str(self.cwd / "created.py")),
                ("updated", str(self.second)),
            ],
        )

    def test_shell_reads_existing_files_and_tracks_redirects(self) -> None:
        output = self.cwd / "summary.txt"
        command = f"sed -n '1,20p' {self.first} && rg first {self.second} > {output}"
        self.assertEqual(
            file_events_from_hook(self.payload("Bash", {"command": command})),
            [
                ("updated", str(output)),
                ("read", str(self.first)),
                ("read", str(self.second)),
            ],
        )

    def test_shell_copy_and_move_distinguish_source_disposition(self) -> None:
        copy = self.cwd / "copy.py"
        moved = self.cwd / "moved.py"
        self.assertEqual(
            file_events_from_hook(
                self.payload("Bash", {"command": f"cp {self.first} {copy}"})
            ),
            [("read", str(self.first)), ("updated", str(copy))],
        )
        self.assertEqual(
            file_events_from_hook(
                self.payload("Bash", {"command": f"mv {self.second} {moved}"})
            ),
            [("updated", str(self.second)), ("updated", str(moved))],
        )

    def test_pre_tool_use_does_not_claim_files_before_success(self) -> None:
        self.assertEqual(
            file_events_from_hook(
                self.payload("Read", {"file_path": str(self.first)}, "PreToolUse")
            ),
            [],
        )

    def test_file_changed_is_supported_for_claude_watchers(self) -> None:
        self.assertEqual(
            file_events_from_hook({
                "hook_event_name": "FileChanged",
                "cwd": str(self.cwd),
                "file_path": str(self.second),
                "event": "change",
            }),
            [("updated", str(self.second))],
        )

    def test_sensitive_paths_are_not_exposed(self) -> None:
        secret = self.cwd / ".env"
        secret.write_text("TOKEN=secret\n")
        self.assertEqual(
            file_events_from_hook(self.payload("Read", {"file_path": str(secret)})),
            [],
        )


if __name__ == "__main__":
    unittest.main()

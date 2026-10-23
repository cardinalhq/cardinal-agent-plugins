"""The plugin's own registration: hooks/hooks.json (Copilot CLI's hook file
format) and .plugin/plugin.json.

hooks.json: `version: 1`, camelCase event keys, entries with `type`,
`bash`, `powershell`, `env`, `timeoutSec` and an optional `matcher`. A
non-zero exit of a preToolUse command DENIES the tool on Copilot, so every
`bash` command ends in `|| true` (and every `powershell` one in `exit 0`).
The authoring entry's matcher is full-matched against toolName.

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_manifest.py -v
"""

from __future__ import annotations

import json
import re
import unittest

from fixtures import ADAPTER, load_script

HOOKS_JSON = ADAPTER / "hooks" / "hooks.json"
PLUGIN_JSON = ADAPTER / ".plugin" / "plugin.json"
SCRIPT_REF = '"$CARDINAL_COPILOT_PLUGIN_ROOT/hooks/cardinal-copilot-telemetry.py"'
PS_SCRIPT_REF = '"$env:CARDINAL_COPILOT_PLUGIN_ROOT/hooks/cardinal-copilot-telemetry.py"'
ENTRY_KEYS = {"type", "bash", "powershell", "env", "timeoutSec", "matcher"}


class HooksJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(HOOKS_JSON.read_text())
        cls.hook = load_script(ADAPTER / "hooks" / "cardinal-copilot-telemetry.py", "copilot_hook_manifest")

    def entries(self):
        for event, entries in self.data["hooks"].items():
            for entry in entries:
                yield event, entry

    def test_version_and_events_match_the_scripts_handlers(self):
        self.assertEqual(self.data["version"], 1)
        self.assertEqual(set(self.data), {"version", "hooks"})
        handled = set(self.hook.HANDLERS) | {event for event, _ in self.hook.ROLE_HANDLERS}
        self.assertEqual(set(self.data["hooks"]), handled)
        self.assertEqual(handled, {"sessionStart", "userPromptSubmitted", "preToolUse", "postToolUse",
                                   "postToolUseFailure", "agentStop", "subagentStart", "subagentStop",
                                   "preCompact"})
        for event in self.data["hooks"]:
            self.assertRegex(event, r"^[a-z][A-Za-z]*$", "camelCase event key")

    def test_every_entry_runs_the_script_for_its_event_and_never_fails(self):
        for event, entry in self.entries():
            with self.subTest(event=event, bash=entry["bash"]):
                self.assertLessEqual(set(entry), ENTRY_KEYS)
                self.assertEqual(entry["type"], "command")
                self.assertTrue(entry["bash"].startswith(f"python3 {SCRIPT_REF} --event {event}"), entry["bash"])
                self.assertTrue(entry["bash"].endswith(" || true"), entry["bash"])
                self.assertTrue(entry["powershell"].startswith(f"python3 {PS_SCRIPT_REF} --event {event}"))
                self.assertTrue(entry["powershell"].endswith("; exit 0"), entry["powershell"])
                self.assertEqual(entry["env"], {"CARDINAL_COPILOT_PLUGIN_ROOT": "${PLUGIN_ROOT}"})
                self.assertIsInstance(entry["timeoutSec"], int)
                self.assertGreater(entry["timeoutSec"], 0)

    def test_role_entries_match_role_handlers(self):
        roles = set()
        for event, entry in self.entries():
            m = re.search(r"--role (\S+)", entry["bash"])
            if m:
                roles.add((event, m.group(1)))
                self.assertIn(f"--role {m.group(1)}", entry["powershell"])
        self.assertEqual(roles, set(self.hook.ROLE_HANDLERS))

    def test_one_plain_entry_per_event(self):
        for event, entries in self.data["hooks"].items():
            plain = [e for e in entries if "--role" not in e["bash"]]
            self.assertEqual(len(plain), 1, event)
            self.assertNotIn("matcher", plain[0], event)

    def test_authoring_entry_matcher_and_timeout(self):
        [authoring] = [e for e in self.data["hooks"]["postToolUse"] if "--role authoring" in e["bash"]]
        matcher = authoring["matcher"]
        for name in ("cardinal-storyboard__preview", "cardinal-storyboard__publish",
                     "cardinal-lakerunner__storyboard__preview"):
            self.assertIsNotNone(re.fullmatch(f"^(?:{matcher})$", name), name)
        for name in ("cardinal-storyboard__find", "cardinal-storyboard__create", "bash", "edit",
                     "cardinal-storyboard__preview_x"):
            self.assertIsNone(re.fullmatch(f"^(?:{matcher})$", name), name)
        from cardinal_core import storyboard_authoring
        self.assertGreaterEqual(authoring["timeoutSec"], storyboard_authoring.PREVIEW_HOOK_TIMEOUT_S)

    def test_the_script_side_regex_agrees_with_the_matcher(self):
        # The main entry stays silent exactly for the tools the authoring
        # entry answers: both sides must name the same tools.
        for raw in ("cardinal-storyboard__preview", "cardinal-storyboard__publish"):
            canonical = self.hook.canonical_tool_name(raw)
            self.assertTrue(self.hook.AUTHORING_TOOL_RE.search(canonical), canonical)
        self.assertIsNone(self.hook.AUTHORING_TOOL_RE.search(self.hook.canonical_tool_name(
            "cardinal-storyboard__find")))


class PluginJsonTests(unittest.TestCase):
    def test_manifest_names_cardinal_and_its_paths_exist(self):
        manifest = json.loads(PLUGIN_JSON.read_text())
        self.assertEqual(manifest["name"], "cardinal")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+")
        self.assertTrue(manifest.get("description"))
        for key in ("skills", "agents", "hooks"):
            self.assertTrue((ADAPTER / manifest[key]).exists(), f"{key}: {manifest[key]}")
        self.assertTrue((ADAPTER / manifest["skills"]).is_dir())
        self.assertTrue((ADAPTER / manifest["agents"]).is_dir())
        self.assertEqual((ADAPTER / manifest["hooks"]).resolve(), HOOKS_JSON.resolve())

    def test_plugin_version_is_read_from_the_manifest(self):
        version = load_script(ADAPTER / "hooks" / "_plugin_version.py", "copilot_plugin_version")
        self.assertEqual(version.plugin_version(), json.loads(PLUGIN_JSON.read_text())["version"])


if __name__ == "__main__":
    unittest.main()

"""The slim cardinal-storyboards Claude Code plugin: its built file set and
its release wiring (build/release.py, .github/workflows/release-mirrors.yml).

The artifact is composed from adapters/claude by
adapters/claude-storyboards/compose.json and ships as a second plugin
(plugins/cardinal-storyboards) in the cardinal-claude-plugin mirror.

Run from repo root:  python3 -m unittest tests.test_storyboards_plugin -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


release = _load("cardinal_release", ROOT / "build" / "release.py")
vendor = _load("cardinal_vendor", ROOT / "build" / "vendor.py")

SLIM = "claude-storyboards"
SLIM_SERVER_TOOL = "mcp__plugin_cardinal-storyboards_cardinal__storyboard__{}"
FULL_SERVER_TOOL = "mcp__plugin_cardinal_cardinal__storyboard__{}"

# Every file the slim plugin ships besides the vendored cardinal_core.
EXPECTED_FILES = {
    ".claude-plugin/plugin.json",
    ".mcp.json",
    ".gitignore",
    "LICENSE",
    "README.md",
    "bin/cardinal-evidence",
    "hooks/hooks.json",
    "hooks/_plugin_mode.py",
    "hooks/_plugin_version.py",
    "hooks/evidence-capture.py",
    "hooks/storyboard-preview.py",
    "hooks/storyboard-session.py",
    "hooks/storyboard-token.py",
    "skills/storyboard/SKILL.md",
    "skills/canvas/SKILL.md",
    "skills/canvas/scripts/render_preview.py",
    "skills/canvas/exemplars/cohort-rows.js",
    "skills/canvas/exemplars/roof-and-timeline.js",
    "skills/canvas/exemplars/scalar-and-series.js",
}

# Full-plugin surfaces the slim plugin must never ship.
FORBIDDEN = (
    "turn-usage", "subagent-usage", "plan-usage", "plan-state", "_plan_cache", "_otel_settings",
    "limits-gate", "initiative-convention", "decision-prompt", "git-state", "invariant-check",
    "cardinal-connect", "cardinal-disconnect", "cardinal-status", "cardinal-decision",
    "cardinal-install-site", "agents/", "skills/connect", "skills/status", "skills/mechanize",
    "skills/optimize-toolkit", "skills/install-site", "skills/deploy-sentinel", "skills/migrate-from-grafana",
)


def _files(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


class _Built(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "plugins" / "cardinal-storyboards"
        release.build_artifact(SLIM, cls.out)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class SlimArtifactFileSetTests(_Built):
    def test_exact_file_set(self):
        files = _files(self.out)
        core = {f for f in files if f.startswith("hooks/cardinal_core/")}
        self.assertEqual(files - core, EXPECTED_FILES)
        # The whole core is vendored (the hooks and the CLI import it).
        self.assertEqual(core, {"hooks/cardinal_core/" + f for f in _files(ROOT / "core" / "cardinal_core")})

    def test_no_full_plugin_surfaces(self):
        for f in _files(self.out):
            for bad in FORBIDDEN:
                self.assertNotIn(bad, f, f"{f} is a full-plugin surface")
        self.assertFalse((self.out / "tests").exists())
        self.assertFalse((self.out / "compose.json").exists())

    def test_plugin_manifest(self):
        m = json.loads((self.out / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(m["name"], "cardinal-storyboards")
        self.assertRegex(m["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(m["version"], release.adapter_version(SLIM))
        self.assertEqual(m["license"], "Apache-2.0")
        self.assertNotIn("hooks", m, "hooks/hooks.json is loaded by convention")
        # What the marketplace sync reads from the laid-down plugin dir.
        self.assertEqual(release.plugin_manifest(self.out), m)

    def test_mcp_json_is_one_oauth_server_with_no_headers(self):
        mcp = json.loads((self.out / ".mcp.json").read_text())
        self.assertEqual(mcp, {"cardinal": {"type": "http", "url": "https://app.cardinalhq.io/mcp"}})
        text = (self.out / ".mcp.json").read_text()
        for bad in ("headers", "API-Key", "CARDINAL_MCP_API_KEY", "${"):
            self.assertNotIn(bad, text)

    def test_hooks_json_runs_only_the_storyboard_hooks(self):
        hooks = json.loads((self.out / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(set(hooks), {"PostToolUse", "SessionStart"})
        commands = {h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]}
        self.assertEqual(commands, {
            "${CLAUDE_PLUGIN_ROOT}/hooks/storyboard-session.py",
            "${CLAUDE_PLUGIN_ROOT}/hooks/storyboard-preview.py",
            "${CLAUDE_PLUGIN_ROOT}/hooks/storyboard-token.py",
            "${CLAUDE_PLUGIN_ROOT}/hooks/evidence-capture.py",
        })
        for groups in hooks.values():
            for g in groups:
                for h in g["hooks"]:
                    self.assertFalse(h.get("async"), "additionalContext is dropped from async hooks")
                    self.assertIsInstance(h.get("timeout"), int)

    def _matcher(self, script: str) -> str:
        groups = json.loads((self.out / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUse"]
        found = [g["matcher"] for g in groups for h in g["hooks"] if script in h["command"]]
        self.assertEqual(len(found), 1)
        return "^(?:" + found[0] + ")$"

    def test_matchers_target_the_slim_server_only(self):
        preview, token, capture = (self._matcher(s) for s in
                                   ("storyboard-preview.py", "storyboard-token.py", "evidence-capture.py"))
        self.assertRegex(SLIM_SERVER_TOOL.format("preview"), preview)
        self.assertRegex(SLIM_SERVER_TOOL.format("create"), token)
        self.assertRegex(SLIM_SERVER_TOOL.format("preview"), token)
        for name in (SLIM_SERVER_TOOL.format("publish"), FULL_SERVER_TOOL.format("preview"),
                     "mcp__cardinal__storyboard__preview", "mcp__evil__storyboard__preview",
                     "mcp__plugin_cardinal-storyboards_evil__storyboard__preview"):
            self.assertNotRegex(name, preview)
        for name in (SLIM_SERVER_TOOL.format("publish"), FULL_SERVER_TOOL.format("create"),
                     "mcp__evil__storyboard__create"):
            self.assertNotRegex(name, token)
        self.assertEqual(capture, "^(?:mcp__.*)$")

    def test_hook_and_cli_files_are_executable(self):
        for rel in ("bin/cardinal-evidence", "hooks/evidence-capture.py", "hooks/storyboard-preview.py",
                    "hooks/storyboard-session.py", "hooks/storyboard-token.py",
                    "skills/canvas/scripts/render_preview.py"):
            self.assertTrue(os.access(self.out / rel, os.X_OK), rel)

    def test_shared_files_are_byte_identical_to_the_full_plugin(self):
        base = ROOT / "adapters" / "claude"
        spec = json.loads((ROOT / "adapters" / SLIM / "compose.json").read_text())
        for rel in spec["include"]:
            src = base / rel
            for f in ([src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]):
                r = f.relative_to(base)
                self.assertEqual((self.out / r).read_bytes(), f.read_bytes(), str(r))

    def test_full_plugin_hooks_also_match_the_slim_server(self):
        # Both installed: the slim hooks step aside, so the full plugin's own
        # hooks must cover the slim plugin's server.
        groups = json.loads((ROOT / "adapters" / "claude" / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUse"]
        for script, tools in (("storyboard-preview.py", ("preview",)), ("storyboard-token.py", ("create", "preview"))):
            m = [g["matcher"] for g in groups for h in g["hooks"] if script in h["command"]]
            self.assertEqual(len(m), 1)
            for t in tools:
                for name in (SLIM_SERVER_TOOL.format(t), FULL_SERVER_TOOL.format(t), f"mcp__cardinal__storyboard__{t}"):
                    self.assertRegex(name, "^(?:" + m[0] + ")$")
            self.assertNotRegex("mcp__evil__storyboard__preview", "^(?:" + m[0] + ")$")


class FullArtifactStillWholeTests(unittest.TestCase):
    def test_full_claude_artifact_is_the_whole_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cardinal"
            release.build_artifact("claude", out)
            files = _files(out)
            for rel in ("hooks/turn-usage.py", "hooks/limits-gate.py", "bin/cardinal-connect",
                        "hooks/_plugin_mode.py", "skills/storyboard/SKILL.md", "hooks/cardinal_core/evidence.py"):
                self.assertIn(rel, files)
            self.assertEqual(json.loads((out / ".claude-plugin" / "plugin.json").read_text())["name"], "cardinal")
            self.assertFalse(any(f.startswith("tests/") for f in files))


class ComposeGuardTests(unittest.TestCase):
    """A broken compose.json fails the build before anything is tagged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapters = Path(self.tmp.name) / "adapters"
        base = self.adapters / "base"
        (base / "hooks").mkdir(parents=True)
        (base / "hooks" / "a.py").write_text("print('a')\n")
        (base / "tests").mkdir()
        (base / "tests" / "t.py").write_text("")
        self.slim = self.adapters / "slim"
        (self.slim / ".claude-plugin").mkdir(parents=True)
        (self.slim / ".claude-plugin" / "plugin.json").write_text('{"name": "slim", "version": "0.1.0"}')
        (self.slim / "hooks").mkdir()
        self.set_hooks("a.py")
        p = patch.object(release, "ADAPTERS_DIR", self.adapters)
        p.start()
        self.addCleanup(p.stop)
        self.out = Path(self.tmp.name) / "out"

    def set_hooks(self, script):
        (self.slim / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/" + script}]}]}}))

    def compose(self, include):
        (self.slim / "compose.json").write_text(json.dumps({"base": "base", "include": include}))

    def test_builds(self):
        self.compose(["hooks/a.py"])
        release.build_artifact("slim", self.out)
        self.assertTrue((self.out / "hooks" / "a.py").is_file())
        self.assertTrue((self.out / "hooks" / "cardinal_core" / "evidence.py").is_file())

    def test_missing_include_fails(self):
        self.compose(["hooks/a.py", "hooks/gone.py"])
        with self.assertRaisesRegex(RuntimeError, "gone.py does not exist"):
            release.build_artifact("slim", self.out)

    def test_escaping_include_fails(self):
        for bad in ("../base/hooks/a.py", "/etc/passwd", "hooks/../hooks/a.py"):
            self.compose([bad])
            with self.assertRaisesRegex(RuntimeError, "relative and normalized"):
                release.build_artifact("slim", self.out)

    def test_monorepo_only_include_fails(self):
        self.compose(["tests/t.py"])
        with self.assertRaisesRegex(RuntimeError, "monorepo-only"):
            release.build_artifact("slim", self.out)

    def test_hook_without_its_file_fails(self):
        self.compose(["hooks/a.py"])
        self.set_hooks("b.py")
        with self.assertRaisesRegex(RuntimeError, "does not ship: hooks/b.py"):
            release.build_artifact("slim", self.out)

    def test_composed_base_is_refused(self):
        (self.slim / "compose.json").write_text(json.dumps({"base": "slim", "include": ["hooks/a.py"]}))
        with self.assertRaisesRegex(RuntimeError, "base must name a whole-directory adapter"):
            release.build_artifact("slim", self.out)


class ReleaseWiringTests(unittest.TestCase):
    def test_mirror_entry_is_a_second_subpath_of_the_claude_mirror(self):
        slim, full = release.MIRRORS[SLIM], release.MIRRORS["claude"]
        self.assertEqual(slim.slug, "cardinalhq/cardinal-claude-plugin")
        self.assertEqual(slim.slug, full.slug)
        self.assertEqual(slim.subpath, "plugins/cardinal-storyboards")
        self.assertEqual(full.subpath, "plugins/cardinal")
        self.assertEqual(full.tag_prefix, "v")
        self.assertEqual(slim.tag_prefix, "cardinal-storyboards/v")

    def test_no_two_plugins_in_a_mirror_share_a_subpath_or_tag_namespace(self):
        by_slug: dict = {}
        for m in release.MIRRORS.values():
            by_slug.setdefault(m.slug, []).append(m)
        for ms in by_slug.values():
            self.assertEqual(len({m.subpath for m in ms}), len(ms))
            self.assertEqual(len({m.tag_prefix for m in ms}), len(ms))

    def test_slim_is_not_vendored_as_its_own_adapter(self):
        self.assertIn("claude", vendor.known_adapters())
        self.assertNotIn(SLIM, vendor.known_adapters())

    def test_workflow_offers_and_releases_the_slim_plugin(self):
        wf = (ROOT / ".github" / "workflows" / "release-mirrors.yml").read_text()
        self.assertIn("options: [claude, claude-storyboards, codex, cursor, gemini]", wf)
        self.assertIn('adapters=["claude","claude-storyboards","codex","cursor","gemini"]', wf)
        self.assertIn('claude|claude-storyboards) KEY="$KEY_CLAUDE" ;;', wf)
        self.assertIn("claude-storyboards) suite=claude ;;", wf)
        self.assertIn("max-parallel: 1", wf)
        # The slim plugin.json sits on the push path filter, so merging it releases.
        self.assertIn("'adapters/*/.claude-plugin/plugin.json'", wf)
        self.assertTrue((ROOT / "adapters" / SLIM / ".claude-plugin" / "plugin.json").is_file())

    def test_existing_tag_lookup_uses_the_full_ref(self):
        text = (ROOT / "build" / "release.py").read_text()
        self.assertIn('"ls-remote", "--tags", "origin", f"refs/tags/{tag}"', text)
        self.assertNotIn('entry.get("name") == "cardinal"', text)


# The claude mirror's marketplace.json as of the first slim release.
MARKETPLACE = {
    "name": "cardinalhq-claude-plugin",
    "plugins": [{
        "name": "cardinal", "version": "0.34.1", "description": "full", "source": "./plugins/cardinal",
        "category": "observability", "license": "Apache-2.0",
    }],
}


class MarketplaceSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "marketplace.json"
        self.path.write_text(json.dumps(MARKETPLACE))

    def entries(self) -> dict:
        return {e["name"]: e for e in json.loads(self.path.read_text())["plugins"]}

    def slim_manifest(self, version="0.1.0"):
        m = json.loads((ROOT / "adapters" / SLIM / ".claude-plugin" / "plugin.json").read_text())
        m["version"] = version
        return m

    def test_first_slim_release_adds_its_entry_and_leaves_cardinal_alone(self):
        self.assertTrue(release.sync_marketplace(self.path, self.slim_manifest(), "plugins/cardinal-storyboards"))
        e = self.entries()
        self.assertEqual(e["cardinal"], MARKETPLACE["plugins"][0])
        self.assertEqual(e["cardinal-storyboards"]["version"], "0.1.0")
        self.assertEqual(e["cardinal-storyboards"]["source"], "./plugins/cardinal-storyboards")
        self.assertEqual(e["cardinal-storyboards"]["license"], "Apache-2.0")

    def test_each_release_moves_only_its_own_entry(self):
        release.sync_marketplace(self.path, self.slim_manifest(), "plugins/cardinal-storyboards")
        release.sync_marketplace(self.path, {"name": "cardinal", "version": "0.35.0"}, "plugins/cardinal")
        e = self.entries()
        self.assertEqual((e["cardinal"]["version"], e["cardinal-storyboards"]["version"]), ("0.35.0", "0.1.0"))
        release.sync_marketplace(self.path, self.slim_manifest("0.2.0"), "plugins/cardinal-storyboards")
        e = self.entries()
        self.assertEqual((e["cardinal"]["version"], e["cardinal-storyboards"]["version"]), ("0.35.0", "0.2.0"))
        self.assertEqual(len(e), 2)

    def test_up_to_date_is_unchanged(self):
        self.assertFalse(release.sync_marketplace(self.path, {"name": "cardinal", "version": "0.34.1"},
                                                  "plugins/cardinal"))

    def test_name_match_is_exact(self):
        # A plugin whose name merely starts with, or whose source ends with, the
        # other's is never touched.
        mf = json.loads(json.dumps(MARKETPLACE))
        mf["plugins"].append({"name": "cardinal-x", "version": "9.9.9", "source": "./plugins/claude"})
        self.path.write_text(json.dumps(mf))
        release.sync_marketplace(self.path, {"name": "cardinal", "version": "0.35.0"}, "plugins/cardinal")
        self.assertEqual(self.entries()["cardinal-x"]["version"], "9.9.9")


if __name__ == "__main__":
    unittest.main()

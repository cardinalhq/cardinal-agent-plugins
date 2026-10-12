"""cardinal_core.checks_install and checks_cli: the Checks preview opt-in for
every runtime (per-runtime state, where each runtime loads the skill from,
the SessionStart reconcile + routing rule) and the shared cardinal-checks CLI
over lib/behavior/checks_ledger.py."""

from __future__ import annotations

import io
import json
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from cardinal_core import checks_cli, checks_install

REPO = Path(__file__).resolve().parents[2]
TRACE = "a" * 32


class Base(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        # A plugin as a release lays it out: the template and lib/behavior.
        self.root = Path(tmp.name) / "plugin"
        (self.root / "checks" / "skill").mkdir(parents=True)
        (self.root / "checks" / "skill" / "SKILL.md").write_text("---\nname: cardinal-checks\n---\nbody\n")
        (self.root / "checks" / "skill" / "CORE.md").write_text("# core\n")
        (self.root / "skills").mkdir()
        shutil.copytree(REPO / "common" / "behavior", self.root / "lib" / "behavior",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        sys.modules.pop(checks_cli.LEDGER_MODULE, None)
        self.addCleanup(sys.modules.pop, checks_cli.LEDGER_MODULE, None)

    def gemini_extension(self, managed=True):
        ext = self.home / ".gemini" / "extensions" / "cardinal"
        ext.mkdir(parents=True, exist_ok=True)
        (ext / "gemini-extension.json").write_text(json.dumps({"name": "cardinal", "cardinalManaged": managed}))
        return ext

    def cli(self, runtime, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = checks_cli.main(runtime, list(argv), plugin_root=self.root, home=self.home)
        return code, out.getvalue(), err.getvalue()


class InstallTests(Base):
    def test_state_is_per_runtime(self):
        checks_install.set_enabled("codex", True, self.home)
        self.assertTrue(checks_install.is_enabled("codex", self.home))
        self.assertFalse(checks_install.is_enabled("cursor", self.home))
        self.assertTrue((self.home / ".codex" / "cardinal" / "checks" / "enabled.json").is_file())
        with self.assertRaises(ValueError):
            checks_install.state_dir("vim", self.home)

    def test_skills_dir_per_runtime(self):
        for runtime in ("claude", "codex", "cursor"):
            self.assertEqual(checks_install.skills_dir(runtime, self.root, self.home), self.root / "skills")
        # Gemini loads skills from Cardinal's extension, only when it is Cardinal's.
        self.assertIsNone(checks_install.skills_dir("gemini", self.root, self.home))
        self.gemini_extension(managed=False)
        self.assertIsNone(checks_install.skills_dir("gemini", self.root, self.home))
        ext = self.gemini_extension()
        self.assertEqual(checks_install.skills_dir("gemini", self.root, self.home), ext / "skills")

    def test_reconcile_installs_and_removes_with_a_deterministic_marker(self):
        dest = checks_install.install_dir("cursor", self.root, self.home)
        self.assertFalse(checks_install.reconcile("cursor", self.root, self.home))
        checks_install.set_enabled("cursor", True, self.home)
        self.assertTrue(checks_install.reconcile("cursor", self.root, self.home))
        marker = (dest / checks_install.MARKER).read_text()
        self.assertEqual(json.loads(marker), {"template_sha256": checks_install.template_digest(self.root)})
        self.assertFalse(checks_install.reconcile("cursor", self.root, self.home))
        checks_install.set_enabled("cursor", False, self.home)
        self.assertTrue(checks_install.reconcile("cursor", self.root, self.home))
        self.assertFalse(dest.exists())

    def test_a_skills_checks_this_plugin_did_not_install_is_left_alone(self):
        dest = self.root / "skills" / "checks"
        dest.mkdir()
        (dest / "SKILL.md").write_text("mine\n")
        checks_install.set_enabled("codex", True, self.home)
        with self.assertRaises(FileExistsError):
            checks_install.reconcile("codex", self.root, self.home)
        changed, context = checks_install.session_start("codex", self.root, connected=True, home=self.home)
        self.assertFalse(changed)
        self.assertEqual((dest / "SKILL.md").read_text(), "mine\n")
        checks_install.set_enabled("codex", False, self.home)
        with self.assertRaises(FileExistsError):
            checks_install.uninstall(dest)

    def test_session_start_reconciles_and_routes_only_while_on_and_connected(self):
        for runtime in ("codex", "cursor", "gemini"):
            with self.subTest(runtime=runtime):
                if runtime == "gemini":
                    self.gemini_extension()
                dest = checks_install.install_dir(runtime, self.root, self.home)
                self.assertEqual(checks_install.session_start(runtime, self.root, connected=True, home=self.home),
                                 (False, None))
                checks_install.set_enabled(runtime, True, self.home)
                changed, context = checks_install.session_start(runtime, self.root, connected=True, home=self.home)
                self.assertTrue(changed)
                self.assertEqual(context, checks_install.ROUTING_RULE)
                self.assertTrue((dest / "SKILL.md").is_file())
                self.assertEqual(checks_install.session_start(runtime, self.root, connected=False, home=self.home),
                                 (False, None))
                checks_install.set_enabled(runtime, False, self.home)
                self.assertEqual(checks_install.session_start(runtime, self.root, connected=True, home=self.home),
                                 (True, None))
                self.assertFalse(dest.exists())


class CliTests(Base):
    def test_on_off_status_for_a_non_claude_runtime(self):
        code, out, _ = self.cli("codex", "on")
        self.assertEqual(code, 0)
        self.assertIn("Start a new Codex session", out)
        self.assertNotIn("/reload-plugins", out)
        self.assertTrue((self.root / "skills" / "checks" / "SKILL.md").is_file())
        code, out, _ = self.cli("codex", "status")
        self.assertIn("Checks preview: on", out)
        self.assertIn("Accepted checks: 0", out)
        code, out, _ = self.cli("codex", "off")
        self.assertEqual(code, 0)
        self.assertIn("a session already running keeps it", out)
        self.assertFalse((self.root / "skills" / "checks").exists())

    def test_gemini_on_needs_the_extension(self):
        code, _, err = self.cli("gemini", "on")
        self.assertEqual(code, 1)
        self.assertIn("extension", err)
        self.assertFalse(checks_install.is_enabled("gemini", self.home))
        ext = self.gemini_extension()
        code, out, _ = self.cli("gemini", "on")
        self.assertEqual(code, 0, out)
        self.assertTrue((ext / "skills" / "checks" / "SKILL.md").is_file())
        self.assertFalse((self.root / "skills" / "checks").exists())

    def test_ledger_commands_write_this_runtimes_state(self):
        code, out, err = self.cli("cursor", "example", "set", "--draft", "d1", "--trace", TRACE, "--evidence",
                                  "insufficient", "--proposed", "UNKNOWN", "--missing", "no span", "--reason", "r")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.home / ".cursor" / "cardinal" / "checks" / "drafts" / "d1.json").is_file())
        code, _, err = self.cli("cursor", "example", "approve", "--draft", "d1", "--trace", TRACE, "--label", "MATCH")
        self.assertEqual(code, checks_cli.EXIT_REFUSED)
        self.assertIn("no span", err)
        code, out, _ = self.cli("cursor", "example", "expected", "--draft", "d1")
        self.assertEqual(json.loads(out), {TRACE: "UNKNOWN"})
        self.assertEqual(self.cli("cursor", "example", "show", "--draft", "../x")[0], checks_cli.EXIT_USAGE)

    def test_a_plugin_without_the_behavior_runtime_says_so(self):
        shutil.rmtree(self.root / "lib")
        code, _, err = self.cli("codex", "accepted")
        self.assertEqual(code, 1)
        self.assertIn("reinstall the plugin", err)


if __name__ == "__main__":
    unittest.main()

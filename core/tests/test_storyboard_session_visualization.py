"""Session guidance and the packaged local preview connection boundary."""
import json
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cardinal_core.investigation_agent import describe

REPO = Path(__file__).resolve().parents[2]


class SessionVisualizationTests(unittest.TestCase):
    def test_codex_preview_renders_reveal_steps_under_its_own_home(self):
        fixture_path = REPO / "adapters/claude/tests/test_storyboard_preview.py"
        spec = importlib.util.spec_from_file_location("preview_fixture", fixture_path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        case = fixture.EndToEndTests()
        case.setUp()
        try:
            if not (REPO / "adapters/codex/hooks/cardinal_core").exists():
                subprocess.run([sys.executable, str(REPO / "build/vendor.py"), "codex"],
                               check=True, capture_output=True)
            preview = fixture.preview_result([
                {"id": "rhythm", "ok": True, "preview_bundle": fixture.bundle_ref("rhythm", case.body)}])
            env = case._env(CARDINAL_MCP_URL=case.maestro.origin + "/api/orgs/" + fixture.ORG + "/mcp",
                            CARDINAL_MCP_API_KEY=fixture.KEY)
            result = subprocess.run([sys.executable, "-I", str(REPO / "adapters/codex/skills/storyboard/scripts/render_preview.py"),
                                     "--runtime", "codex"], input=json.dumps(preview), env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [json.loads(line) for line in result.stdout.splitlines()]
            steps = [line for line in lines if line.get("scene_id") == "rhythm"]
            self.assertEqual([line["step"] for line in steps], [0, 1])
            for line in steps:
                png = Path(line["png"])
                self.assertTrue(png.is_relative_to(case.home / ".codex"))
                self.assertEqual(png.read_bytes(), fixture.TINY_PNG)
                self.assertIsNone(line["error"])
        finally:
            case.tearDown()

    def test_session_guidance_never_prompts_for_visualization(self):
        wiring = SimpleNamespace(cli="/installed/cardinal-storyboard")
        for enabled in (True, False):
            binding = {"investigation_id": "inv_" + "1" * 24, "storyboard_id": "sb_" + "2" * 24,
                       "is_author": True, "capabilities": {"projection": {"enabled": enabled}}}
            text = describe(wiring, "session-1", binding)
            self.assertNotIn("Wait for an affirmative reply", text)
            self.assertNotIn("Would you like", text)
            self.assertNotIn("automatically projects", text)
            binding["is_author"] = False
            text = describe(wiring, "session-1", binding)
            self.assertNotIn("Would you like", text)
            self.assertIn("Do not checkpoint, frame, or edit it", text)

    def test_packaged_renderers_use_the_selected_agents_connection_in_isolated_python(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            for runtime in ("codex", "cursor", "gemini"):
                root = home / ("." + runtime)
                root.mkdir()
                (root / "cardinal.json").write_text(json.dumps({"mcp_url": f"https://{runtime}.example/api/orgs/org-{runtime}/mcp"}))
                (root / "cardinal-secrets.json").write_text(json.dumps({"mcp_api_key": "test-" + runtime}))
            for runtime in ("codex", "cursor", "gemini"):
                script = REPO / "adapters" / runtime / "skills/storyboard/scripts/render_preview.py"
                if not (REPO / "adapters" / runtime / "hooks/cardinal_core").exists():
                    subprocess.run([sys.executable, str(REPO / "build/vendor.py"), runtime],
                                   check=True, capture_output=True)
                # -I is the exact isolation used by the skill; no PYTHONPATH or cwd imports.
                code = ("import json,runpy,sys; from pathlib import Path; "
                        "m=runpy.run_path(sys.argv[1]); "
                        "print(json.dumps(m['connect_info'](Path(sys.argv[2]),{},sys.argv[3])))")
                result = subprocess.run([sys.executable, "-I", "-c", code, str(script), tmp, runtime],
                                        capture_output=True, text=True, check=True, cwd=tmp)
                self.assertEqual(json.loads(result.stdout), {"origin": f"https://{runtime}.example",
                                 "org": "org-" + runtime, "key": "test-" + runtime})
                (home / ("." + runtime) / "cardinal.json").unlink()
                result = subprocess.run([sys.executable, "-I", "-c", code, str(script), tmp, runtime],
                                        capture_output=True, text=True, check=True, cwd=tmp)
                self.assertEqual(json.loads(result.stdout), {}, "never borrow another agent's credentials")


if __name__ == "__main__":
    unittest.main()

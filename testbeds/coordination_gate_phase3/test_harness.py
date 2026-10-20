"""Isolation checks for the real-runtime harness's targeted trust cleanup."""
import importlib.util
from pathlib import Path
import tempfile
import tomllib
import unittest

spec = importlib.util.spec_from_file_location("phase3_harness", Path(__file__).with_name("run.py"))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class Cleanup(unittest.TestCase):
    def test_removes_only_owned_fixture_project_tables_and_retains_other_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('model = "keep-model"\n\n[projects."/fixture/run/owned"]\ntrust_level = "trusted"\n'
                            '\n[projects."/user/project"]\ntrust_level = "trusted"\n# keep this comment\n')
            self.assertEqual(harness.clean_project_trust(Path("/fixture/run"), path), 1)
            value = path.read_text()
            self.assertEqual(tomllib.loads(value)["model"], "keep-model")
            self.assertIn('[projects."/user/project"]\ntrust_level = "trusted"\n# keep this comment\n', value)
            self.assertNotIn("owned", value)
            self.assertEqual(harness.clean_project_trust(Path("/fixture/run"), path), 0)


if __name__ == "__main__":
    unittest.main()

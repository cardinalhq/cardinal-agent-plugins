"""The shipped packages, not just source imports, have identical repo behavior."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("repo_release_build", ROOT / "build/release.py")
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)

PROBE = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "hooks"))
from cardinal_core import investigation_events as ie, session_repos
home, runtime = Path(sys.argv[2]), sys.argv[3]
sid = "packaged-backfill"
ie.adopt(home, sid, "inv_" + "a"*24, "auto", {"org": "org-1", "is_author": True})
files = home / ("." + runtime) / "cardinal/storyboard-files"
files.mkdir(parents=True)
(files / (sid + ".json")).write_text(json.dumps({"files": [
    {"repo": "acme/first", "path": "a.py"}, {"repo": "acme/second", "path": "b.py"}]}))
session_repos.replay(home, sid, runtime, runtime + "/1", env={})
print(json.dumps(sorted(c["repo"] for c in session_repos.pending(home, sid, "org-1"))))
'''


class SessionRepoReleaseTests(unittest.TestCase):
    def test_every_release_contains_the_same_core_and_replays_multiple_repos(self):
        source = ROOT / "core/cardinal_core"
        for adapter in ("claude", "codex", "cursor", "gemini", "copilot"):
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package = root / "package"
                RELEASE.build_artifact(adapter, package)
                for path in source.rglob("*.py"):
                    self.assertEqual(path.read_bytes(), (package / "hooks/cardinal_core" / path.relative_to(source)).read_bytes())
                environment = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
                result = subprocess.run([sys.executable, "-c", PROBE, str(package), str(root / "home"), adapter],
                                        cwd=root, env=environment, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), ["acme/first", "acme/second"])
                launcher = package / ("bin" if adapter == "claude" else "scripts") / "cardinal-storyboard"
                result = subprocess.run([sys.executable, str(launcher), "investigation", "repos", "--help"],
                                        cwd=root, env=environment, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--cwd", result.stdout)


if __name__ == "__main__":
    unittest.main()

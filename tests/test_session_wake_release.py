"""Every shipped plugin carries cardinal-wake, and the packaged copy runs:
--help (with `resume`), `enable --launch` is refused (unattended launch was
removed), and `enable` (with an injected launchctl, a temp HOME and platform
darwin, so nothing is installed) says the waker only notifies and installs
its stable copy. Never starts an agent or touches the real launchd."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("wake_release_build", ROOT / "build/release.py")
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)
ADAPTERS = ("claude", "codex", "cursor", "gemini")

PROBE = r'''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "hooks"))
from cardinal_core import session_wake as w
calls, out = [], []
system = w.System(out=out.append, now=lambda: 1_800_000_000.0)
launchctl = lambda args: (calls.append(args) or (0, ""))
code = w.cli_main(["enable", "--launch"], runtime=sys.argv[3], home=Path(sys.argv[2]), system=system,
                  launchctl=launchctl, platform="darwin", environ={"PATH": "/nonexistent"})
assert code == 2 and calls == [] and "unattended launch was removed" in out[0], out
out.clear()
code = w.cli_main(["enable"], runtime=sys.argv[3], home=Path(sys.argv[2]), system=system,
                  launchctl=launchctl, platform="darwin", environ={"PATH": "/nonexistent"})
assert code == 0, out
assert [c[0] for c in calls] == ["bootout", "bootstrap"], calls
print("\n".join(out))
'''


def launcher(package, adapter):
    return package / ("bin" if adapter == "claude" else "scripts") / "cardinal-wake"


class SessionWakeReleaseTests(unittest.TestCase):
    def test_every_adapter_requires_the_wake_artifacts(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter):
                required = RELEASE.REQUIRED_ARTIFACTS[adapter]
                self.assertIn("hooks/cardinal_core/session_wake.py", required)
                self.assertNotIn("hooks/cardinal_core/allowlist_proxy.py", required)
                self.assertIn(str(launcher(Path(""), adapter)), required)

    def test_the_packaged_cardinal_wake_runs_and_only_notifies(self):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package = root / "package"
                RELEASE.build_artifact(adapter, package)
                script = launcher(package, adapter)
                self.assertTrue(os.access(str(script), os.X_OK))
                result = subprocess.run([sys.executable, str(script), "--help"], cwd=root, env=environment,
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("enable", result.stdout)
                self.assertIn("resume", result.stdout)
                home = root / "home"
                home.mkdir()
                env = dict(environment, HOME=str(home))
                result = subprocess.run([sys.executable, "-c", PROBE, str(package), str(home), adapter],
                                        cwd=root, env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Nothing is ever launched unattended", result.stdout)
                self.assertIn("cardinal-wake resume <session>", result.stdout)
                self.assertEqual("claude --remote-control" in result.stdout, adapter == "claude")
                self.assertTrue((home / ".cardinal/wake/lib/cardinal_core/session_wake.py").is_file())
                self.assertTrue((home / ".cardinal/wake/lib/cardinal-wake").is_file())


if __name__ == "__main__":
    unittest.main()

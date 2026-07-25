"""Golden parity: the migrated claude adapter must emit byte-equal OTLP
(and hook stdout) to the goldens captured from the SHIPPED plugin.

Goldens were produced by capture_goldens.py running the pre-migration
hooks at cardinal-claude-plugin/plugins/cardinal/hooks against the exact
scenarios in fixtures.py. This test replays the identical scenarios
against adapters/claude/hooks (with cardinal_core vendored — run
`python3 build/vendor.py claude` first) and compares after normalizing
only the volatile fields (timestamps, ts, cardinal.core_version,
cardinal.plugin_version, scope version, cardinal.cwd).

Run: python3 -m unittest test_parity -v   (from this directory)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from fixtures import (
    ADAPTER_HOOKS_DIR,
    GOLDENS_DIR,
    SCENARIOS,
    StubIngest,
    _GIT_IDENTITY_ENV,
    base_env,
    collect,
    run_hook,
    write_settings,
)


class TestGoldenParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (ADAPTER_HOOKS_DIR / "cardinal_core" / "otlp.py").exists():
            raise unittest.SkipTest(
                "cardinal_core not vendored — run: python3 build/vendor.py claude"
            )

    def _assert_parity(self, name: str) -> None:
        golden_path = GOLDENS_DIR / f"{name}.json"
        self.assertTrue(golden_path.exists(), f"missing golden: {golden_path}")
        golden = json.loads(golden_path.read_text())
        with tempfile.TemporaryDirectory(prefix=f"parity-{name}-") as tmp:
            result = SCENARIOS[name](ADAPTER_HOOKS_DIR, Path(tmp))
        self.assertEqual(
            golden, result,
            f"scenario '{name}' diverged from the shipped plugin's golden",
        )


def _make_test(name: str):
    def test(self: TestGoldenParity) -> None:
        self._assert_parity(name)
    test.__name__ = f"test_{name}"
    test.__doc__ = f"byte-parity with shipped plugin: {name}"
    return test


for _name in SCENARIOS:
    setattr(TestGoldenParity, f"test_{_name}", _make_test(_name))


class GitStateCredentialScrubTests(unittest.TestCase):
    """Regression tests for docs/privacy-redaction.md §7: `git remote
    get-url origin` can return a URL with embedded credentials
    (`https://x-access-token:TOKEN@host/...`, common on CI-cloned or
    PAT-authenticated checkouts). cardinal.remote_url must never carry
    the token, and cardinal.remote_url_credential_scrubbed flags that
    scrubbing happened without revealing what was removed."""

    def _run_git_state(self, remote_url: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="creds-scrub-") as tmp_str:
            tmp = Path(tmp_str)
            stub = StubIngest().start()
            try:
                home = tmp / "home"
                write_settings(home, stub.endpoint)
                repo = tmp / "repo"
                repo.mkdir(parents=True)
                env = {**os.environ, **_GIT_IDENTITY_ENV, "HOME": str(repo)}

                def _git(*args: str) -> None:
                    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

                _git("init", "-q", "-b", "feat/creds-test")
                (repo / "README.md").write_text("x\n")
                _git("add", "README.md")
                _git("commit", "-q", "-m", "init")
                _git("remote", "add", "origin", remote_url)

                payload = {
                    "session_id": "sess-creds-1",
                    "cwd": str(repo),
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "hi",
                }
                proc = run_hook(ADAPTER_HOOKS_DIR / "git-state.py", payload, base_env(home))
                result = collect(stub, proc)
            finally:
                stub.stop()
        records = result["batches"][0]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        (rec,) = records
        return {a["key"]: list(a["value"].values())[0] for a in rec["attributes"]}

    def test_strips_origin_url_credentials(self) -> None:
        token = "ghp_" + "s" * 36
        attrs = self._run_git_state(f"https://x-access-token:{token}@github.com/cardinalhq/private-repo.git")
        self.assertEqual(attrs["cardinal.remote_url"], "https://github.com/cardinalhq/private-repo.git")
        self.assertEqual(attrs["cardinal.remote_url_credential_scrubbed"], True)
        self.assertEqual(attrs["cardinal.repo"], "cardinalhq/private-repo")
        raw = json.dumps(attrs)
        self.assertNotIn(token, raw)
        self.assertNotIn("x-access-token", raw)

    def test_clean_origin_url_not_flagged(self) -> None:
        attrs = self._run_git_state("https://github.com/cardinalhq/public-repo.git")
        self.assertEqual(attrs["cardinal.remote_url"], "https://github.com/cardinalhq/public-repo.git")
        self.assertEqual(attrs["cardinal.remote_url_credential_scrubbed"], False)


if __name__ == "__main__":
    unittest.main()

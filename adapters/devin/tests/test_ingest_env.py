"""Non-interactive Cardinal credentials from the environment."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support
from support import FakeDevin, FakeIngest, load_fixture

from cardinal_core.paths import AgentPaths, atomic_write_json
from cardinal_devin import cli, ingest
from cardinal_devin.connect import run_status

SECRET = "ingest-env-secret-9f3a"
STATE_SECRET = "ingest-state-secret-77"


def base_env() -> dict:
    """os.environ without anything this adapter reads."""
    drop = ("DEVIN_API_KEY", "DEVIN_ORG_ID", "DEVIN_API_BASE_URL", "GITHUB_TOKEN", "GITHUB_API_URL",
            "CARDINAL_DEVIN_HOME", "CARDINAL_DEVIN_LOOKBACK_DAYS", "CARDINAL_DEVIN_MAX_PAGES",
            ingest.ENV_ENDPOINT, ingest.ENV_API_KEY, ingest.ENV_ORG, ingest.ENV_DEPLOYMENT_ENV)
    return {k: v for k, v in os.environ.items() if k not in drop}


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cardinal-devin-ingest-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.paths = AgentPaths(home=Path(tmp) / "home")

    def connect_state(self) -> None:
        atomic_write_json(self.paths.state_path, {
            "host": "https://app.cardinalhq.io", "org_slug": "state-org",
            "deployment_environment": "prod", "ingest_endpoint": "https://state-ingest.example",
        })
        atomic_write_json(self.paths.secrets_path, {"ingest_api_key": STATE_SECRET,
                                                    "ingest_api_header": "x-cardinalhq-api-key"})

    def test_env_only_without_state(self) -> None:
        config = ingest.resolve(self.paths, {
            ingest.ENV_ENDPOINT: "https://ingest.example/ ", ingest.ENV_API_KEY: f" {SECRET}\n",
            ingest.ENV_ORG: "acme", ingest.ENV_DEPLOYMENT_ENV: "customer",
        })
        self.assertEqual(config.source, ingest.SOURCE_ENV)
        conn = config.connection
        self.assertEqual((conn.endpoint, conn.api_key, conn.api_header),
                         ("https://ingest.example", SECRET, "x-cardinalhq-api-key"))
        self.assertEqual(config.connection_state, {"org_slug": "acme", "deployment_environment": "customer"})
        self.assertNotIn(SECRET, repr(config.connection_state))

    def test_env_takes_precedence_over_state(self) -> None:
        self.connect_state()
        config = ingest.resolve(self.paths, {ingest.ENV_ENDPOINT: "https://env-ingest.example",
                                             ingest.ENV_API_KEY: SECRET})
        self.assertEqual((config.connection.endpoint, config.connection.api_key),
                         ("https://env-ingest.example", SECRET))
        # Resource facts come from the environment only, not the other connection.
        self.assertEqual(config.connection_state, {})

    def test_state_when_env_unset(self) -> None:
        self.connect_state()
        config = ingest.resolve(self.paths, {ingest.ENV_ORG: "ignored"})
        self.assertEqual(config.source, ingest.SOURCE_STATE)
        self.assertEqual(config.connection.api_key, STATE_SECRET)
        self.assertEqual(config.connection_state["org_slug"], "state-org")
        self.assertEqual(ingest.resolve(AgentPaths(home=self.paths.home / "none"), {}).connection, None)

    def test_partial_env_is_an_error(self) -> None:
        self.connect_state()
        with self.assertRaises(ingest.IngestConfigError) as ctx:
            ingest.resolve(self.paths, {ingest.ENV_ENDPOINT: "https://ingest.example"})
        self.assertIn(ingest.ENV_API_KEY, str(ctx.exception))
        with self.assertRaises(ingest.IngestConfigError) as ctx:
            ingest.resolve(self.paths, {ingest.ENV_API_KEY: SECRET})
        self.assertIn(ingest.ENV_ENDPOINT, str(ctx.exception))
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_malformed_endpoint(self) -> None:
        for bad in ("ingest.example", "https://ingest.example/v1/logs"):
            with self.assertRaises(ingest.IngestConfigError):
                ingest.resolve(self.paths, {ingest.ENV_ENDPOINT: bad, ingest.ENV_API_KEY: SECRET})


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cardinal-devin-ingest-status-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.paths = AgentPaths(home=Path(tmp) / "home")
        self.lines: list = []

    def status(self, environ: dict) -> int:
        return run_status(self.paths, devin_key_source="$DEVIN_API_KEY", devin_org_id=None,
                          devin_base_url="https://api.devin.ai", api_version="auto", github_token_set=False,
                          out=self.lines.append, environ=environ)

    def test_status_reports_env_source_without_key(self) -> None:
        atomic_write_json(self.paths.state_path, {"ingest_endpoint": "https://state.example", "host": "h"})
        atomic_write_json(self.paths.secrets_path, {"ingest_api_key": STATE_SECRET})
        code = self.status({ingest.ENV_ENDPOINT: "https://ingest.example", ingest.ENV_API_KEY: SECRET,
                            ingest.ENV_ORG: "acme"})
        text = "\n".join(self.lines)
        self.assertEqual(code, 0, text)
        self.assertIn("credentials from environment", text)
        self.assertIn("precedence", text)
        self.assertIn("https://ingest.example", text)
        self.assertIn("cardinal.org: acme", text)
        self.assertIn("ignored while these are set", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(SECRET[:8], text)
        self.assertNotIn(STATE_SECRET, text)

    def test_status_partial_env(self) -> None:
        code = self.status({ingest.ENV_API_KEY: SECRET})
        text = "\n".join(self.lines)
        self.assertEqual(code, 2)
        self.assertIn(f"{ingest.ENV_ENDPOINT}", text)
        self.assertNotIn(SECRET, text)


class PollWithEnvTests(unittest.TestCase):
    """The CLI end to end with env credentials and an empty state dir."""

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cardinal-devin-ingest-poll-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.state_dir = Path(tmp) / "data"
        fixture = load_fixture("v1_sessions.json")
        self.devin = FakeDevin("v1", fixture["sessions"])
        self.ingest = FakeIngest()
        self.addCleanup(self.devin.close)
        self.addCleanup(self.ingest.close)

    def run_cli(self, *extra: str, env: dict):
        out, err = io.StringIO(), io.StringIO()
        argv = ["poll", "--once", "-v", "--state-dir", str(self.state_dir), "--devin-base-url", self.devin.url,
                "--lookback-days", "20000", *extra]
        with mock.patch.dict(os.environ, env, clear=True), support.captured_logs() as logs, \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue(), logs.text

    def env(self, **extra: str) -> dict:
        env = base_env()
        env.update({"DEVIN_API_KEY": "cog_test", ingest.ENV_ENDPOINT: self.ingest.url,
                    ingest.ENV_API_KEY: SECRET, ingest.ENV_ORG: "acme", ingest.ENV_DEPLOYMENT_ENV: "customer"})
        env.update(extra)
        return env

    def test_env_only_poll_sends_with_env_key(self) -> None:
        code, out, err, logs = self.run_cli(env=self.env())
        self.assertEqual(code, 0, err + logs)
        self.assertFalse((self.state_dir / "cardinal.json").exists())
        posts = [r for r in self.ingest.requests if r.path == "/v1/logs"]
        self.assertTrue(posts, logs)
        self.assertTrue(all(r.headers.get("x-cardinalhq-api-key") == SECRET for r in posts))
        resources = [res for res, _ in support.records_in([r.json() for r in posts])]
        self.assertTrue(all(res["cardinal.org"] == "acme" for res in resources))
        self.assertTrue(all(res["deployment.environment"] == "customer" for res in resources))
        self.assertTrue(all(res["agent.runtime"] == "devin" for res in resources))
        self.assertIn("credentials from environment", logs)
        for text in (out, err, logs):
            self.assertNotIn(SECRET, text)

    def test_dry_run_never_prints_key(self) -> None:
        code, out, err, logs = self.run_cli("--dry-run", env=self.env())
        self.assertEqual(code, 0, err + logs)
        self.assertIn("resourceLogs", out)
        self.assertEqual(self.ingest.requests, [])
        for text in (out, err, logs):
            self.assertNotIn(SECRET, text)

    def test_partial_env_exits_2(self) -> None:
        env = self.env()
        del env[ingest.ENV_ENDPOINT]
        code, out, err, logs = self.run_cli(env=env)
        self.assertEqual(code, 2)
        self.assertIn(ingest.ENV_ENDPOINT, err)
        self.assertEqual(self.ingest.requests, [])
        self.assertEqual(self.devin.requests, [])
        for text in (out, err, logs):
            self.assertNotIn(SECRET, text)

    def test_no_credentials_exits_1(self) -> None:
        env = self.env()
        del env[ingest.ENV_ENDPOINT], env[ingest.ENV_API_KEY]
        code, _, err, _ = self.run_cli(env=env)
        self.assertEqual(code, 1)
        self.assertIn("Not connected", err)
        self.assertIn(ingest.ENV_ENDPOINT, err)


if __name__ == "__main__":
    unittest.main()

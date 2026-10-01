"""migrate-from-grafana: what the scripts say when the connect token can't do a step.

Run: python3 -m unittest discover tests -p test_migrate_connect_scopes.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common", "migrate-from-grafana", "scripts"))

import cardinal_catalog as cc  # noqa: E402

ALL = "dashboards:write alerts:write telemetry:query"


class MissingScopesMessageTest(unittest.TestCase):
    def test_connected_without_the_step_scope(self):
        conn = {"act_key": "k", "act_scopes": ["telemetry:query"]}
        msg = cc.missing_scopes_message(conn, ["dashboards:write", "alerts:write"])
        self.assertIn("lacks dashboards:write, alerts:write", msg)
        self.assertIn(f"cardinal-connect --rotate {ALL}", msg)
        self.assertIn("CARDINAL_TOKEN", msg)

    def test_connected_without_act_token(self):
        # e.g. connected for telemetry only, or an agent whose connect mints no act token
        conn = {"mcp_key": "m", "act_scopes": ["telemetry:query"]}
        msg = cc.missing_scopes_message(conn, ["telemetry:query"])
        self.assertIn("lacks telemetry:query", msg)
        self.assertIn(f"--rotate {ALL}", msg)

    def test_not_connected(self):
        msg = cc.missing_scopes_message({}, ["telemetry:query"])
        self.assertIn(f"run `cardinal-connect {ALL}`", msg)
        self.assertNotIn("--rotate", msg)

    def test_no_connect_scopes(self):
        self.assertEqual(cc.missing_scopes_message({"act_key": "k"}, None),
                         "no Cardinal login token: put CARDINAL_TOKEN (or CARDINAL_API_KEY) in .env.cardinal")


class FromEnvTest(unittest.TestCase):
    def test_from_env_exits_with_full_reconnect_command(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, "cardinal.json"), "w") as f:
                json.dump({"host": "https://h", "org_id": "o", "act_scopes": ["telemetry:query"]}, f)
            with open(os.path.join(home, "cardinal-secrets.json"), "w") as f:
                json.dump({"act_api_key": "k", "act_endpoint": "https://h"}, f)
            env = {"CARDINAL_AGENT_HOME": home}
            with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit) as cm:
                cc.Cardinal.from_env(connect_scopes=["dashboards:write"])
        self.assertIn("lacks dashboards:write", str(cm.exception.code))
        self.assertIn(f"--rotate {ALL}", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()

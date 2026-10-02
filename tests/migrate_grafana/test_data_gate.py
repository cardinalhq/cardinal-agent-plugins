"""migrate-from-grafana: the step 0 "is Cardinal receiving data?" gate.

Run: python3 -m unittest discover -s tests/migrate_grafana -v

The expected behaviour comes from a real run against Cardinal SaaS: an org with two
data lakes where the default one held only the connected Claude Code's own usage
telemetry (claude_code_* metrics, all with agent_runtime=claude-code) and the other
held a demo cluster's data. The old check passed on stored metric names alone (the
listing ignores time ranges), and the agent then switched to the demo lake by itself.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "common", "migrate-from-grafana", "scripts"))

import cardinal_catalog as cc  # noqa: E402

CONNECTED = {"host": "https://h", "org_id": "o", "act_key": "k", "act_endpoint": "https://h",
             "act_scopes": list(cc.MIGRATION_SCOPES)}
AGENT_ONLY = {"id": "lake-a", "slug": "prod-saas", "name": "prod-saas"}
DEMO = {"id": "lake-b", "slug": "otel-demo", "name": "Demo Applications"}


def no_agent(name):
    return f'count({name}{{{cc.AGENT_LABEL}=""}})'


class FakeCardinal:
    """Cardinal REST stand-in: the instance list and each lake's stored metric names."""

    def __init__(self, lakes):
        self.lakes = lakes  # [(lake, [metric names])]

    def req(self, method, path, params=None, body=None):
        if path == "/api/lakerunner/instances":
            return 200, {"instances": [lake for lake, _ in self.lakes]}
        for lake, names in self.lakes:
            if path == f"/api/lakerunner/{lake['id']}/prometheus/api/v1/label/__name__/values":
                return 200, {"status": "success", "data": names}
        return 404, "not found"


def fake_native(live):
    """NativeQuery stand-in: only the (lake id, expression) pairs in `live` return points;
    every expression asked is recorded in .asked."""

    class Native:
        asked = []

        def __init__(self, cardinal, instance_id, window_ms=3600_000):
            self.id = instance_id
            Native.window_ms = window_ms

        def values(self, signal, expr, step=60):
            Native.asked.append((self.id, expr))
            return ([1.0] if (self.id, expr) in live else []), None

    return Native


def run_main(argv, lakes, live=(), conn=CONNECTED):
    """Run cardinal_catalog.main(); return (exit code or None, stdout + stderr, Native)."""
    native = fake_native(set(live))
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["cardinal_catalog.py", *argv]), \
            mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch.object(cc, "connect_info", return_value=conn), \
            mock.patch.object(cc.Cardinal, "from_env", return_value=FakeCardinal(lakes)), \
            mock.patch.object(cc, "NativeQuery", native), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            cc.main()
            code = None
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), native


class CheckTest(unittest.TestCase):
    def test_agent_telemetry_alone_is_not_data(self):
        # A freshly connected org: the only metrics are the agent's own usage telemetry.
        names = ["claude_code_token_usage", "claude_code_cost_usage"]
        code, out, native = run_main(["--check"], [(AGENT_ONLY, names)])
        self.assertIn("isn't receiving data", str(code))
        self.assertIn("none current outside agent telemetry", str(code))
        self.assertEqual({e for _, e in native.asked}, {no_agent(n) for n in names})

    def test_stored_names_without_recent_samples_are_not_data(self):
        code, out, _ = run_main(["--check"], [(DEMO, ["go_memory_used", "process_cpu_time"])])
        self.assertIn("isn't receiving data", str(code))

    def test_no_metrics(self):
        code, out, native = run_main(["--check"], [(DEMO, [])])
        self.assertIn("no metrics", str(code))
        self.assertEqual(native.asked, [])

    def test_live_collector_metric_passes(self):
        code, out, native = run_main(["--check"], [(DEMO, ["go_memory_used", "process_cpu_time"])],
                                     live=[("lake-b", no_agent("process_cpu_time"))])
        self.assertIsNone(code)
        self.assertIn("Cardinal is receiving data on 'otel-demo'", out)
        self.assertIn("process_cpu_time in the last 15 min", out)
        self.assertEqual(native.window_ms, cc.LIVE_WINDOW_MIN * 60_000)

    def test_several_lakes_ask_the_user(self):
        lakes = [(AGENT_ONLY, ["claude_code_token_usage"]), (DEMO, ["go_memory_used"])]
        code, out, _ = run_main(["--check"], lakes, live=[("lake-b", no_agent("go_memory_used"))])
        self.assertEqual(code, cc.EXIT_PICK_INSTANCE)
        self.assertIn("ask the user which one their collector sends to", out)
        self.assertIn("'prod-saas' (prod-saas): no data from a collector", out)
        self.assertIn("'otel-demo' (Demo Applications): receiving data", out)

    def test_chosen_lake_is_the_only_one_checked(self):
        lakes = [(AGENT_ONLY, ["claude_code_token_usage"]), (DEMO, ["go_memory_used"])]
        code, out, native = run_main(["--check", "--instance", "prod-saas"], lakes,
                                     live=[("lake-b", no_agent("go_memory_used"))])
        self.assertIn("isn't receiving data on 'prod-saas'", str(code))
        self.assertEqual({lake for lake, _ in native.asked}, {"lake-a"})

    def test_export_with_several_lakes_needs_instance(self):
        lakes = [(AGENT_ONLY, ["claude_code_token_usage"]), (DEMO, ["go_memory_used"])]
        code, out, native = run_main(["--export", "export", "--out", "catalog"], lakes)
        self.assertEqual(code, cc.EXIT_PICK_INSTANCE)
        self.assertIn("'otel-demo' (Demo Applications)", out)
        self.assertEqual(native.asked, [])  # listing the lakes probes nothing

    def test_without_telemetry_query_reconnect(self):
        # The MCP key alone can't tell live collector data from stale names / agent telemetry.
        conn = {"org_id": "o", "mcp_key": "m", "mcp_url": "https://h/mcp", "act_key": "k",
                "act_scopes": ["dashboards:write"]}
        code, out, _ = run_main(["--check"], [(DEMO, ["go_memory_used"])], conn=conn)
        self.assertEqual(code, cc.EXIT_CONNECT_REJECTED)
        self.assertIn("--rotate dashboards:write alerts:write telemetry:query", out)

    def test_not_connected(self):
        code, out, _ = run_main(["--check"], [(DEMO, ["go_memory_used"])], conn={})
        self.assertEqual(code, cc.EXIT_NOT_CONNECTED)


class LiveMetricTest(unittest.TestCase):
    def test_probes_are_spread_and_capped(self):
        names = [f"m{i}" for i in range(400)]
        native = fake_native(set())(None, "lake")
        self.assertIsNone(cc.live_metric(native, names, probes=8))
        asked = [e for _, e in native.asked]
        self.assertEqual(len(asked), 8)
        self.assertEqual(asked[0], no_agent("m0"))
        self.assertEqual(asked[-1], no_agent("m350"))

    def test_skips_names_promql_cant_reference(self):
        native = fake_native({("lake", no_agent("ok_metric"))})(None, "lake")
        self.assertEqual(cc.live_metric(native, ["http.server.duration", "ok_metric"]), "ok_metric")
        self.assertNotIn(("lake", no_agent("http.server.duration")), native.asked)


if __name__ == "__main__":
    unittest.main()

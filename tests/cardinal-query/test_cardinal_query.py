"""Fixture tests for the Python Cardinal Query client.

Run with: python3 -m unittest discover -s tests/cardinal-query -p 'test_*.py'
"""

from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).resolve().parents[2] / ".github/skills/cardinal-query/scripts/cardinal_query.py"
KEY = "fixture-only"
WINDOW = ["--start", "2026-06-01T12:00:00Z", "--end", "2026-06-01T12:05:00Z"]
PATTERN = "-9223372036854775807"
BUCKET = 1780315260000
ROW = {
    "timestamp": BUCKET,
    "timestamp_ns": 1780315260000000001,
    "tags": {"service_name": "checkout", "level": "ERROR", "chq_fingerprint": PATTERN,
             "message": "Timeouts [db] :: retry"},
}
ORG_TAGS = ["service_name", "k8s_namespace_name", "chq_fingerprint", "message", "deployment_environment"]
NAMESPACES = ["checkout-prod", "checkout-staging", "payments", "x" * 300]
CORE_FIELDS = ["service_name", "level", "message", "chq_fingerprint"]
EMOJI = "\U0001F600"
# Two distinct long values share their first 256 characters; one repeats.
LONG_VALUES = ["y" * 300, "y" * 300, "y" * 299 + "z", "a" + EMOJI * 300, "2026-11-16T10:00:00Z",
               "2026-11-16T10:00:00.000+00:00"]
# A key the fixture echoes back in an error body.
ECHO_KEY = "echo-me-0123456789"


class Fixture(BaseHTTPRequestHandler):
    requests = []
    seen = []  # (path, query ID) for every request, including rejected ones
    attempts = defaultdict(int)
    null_tags = False

    def log_message(self, *args):
        pass

    def send(self, status, body, content_type, headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-CardinalHQ-Query-Id", self.headers.get("X-CardinalHQ-Query-Id", ""))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def raw_events(self, lines):
        # Send without a done event or Content-Length, then drop the connection.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(("\n".join(lines) + "\n").encode("utf-8"))
        self.wfile.flush()
        self.close_connection = True

    def transient(self, query):
        """Answer the retry scenarios, which a service name selects. Return True if handled."""
        for name in ("flaky", "busy", "down", "downlong", "broken", "drop", "cut"):
            if f'service_name="{name}"' in query:
                break
        else:
            return False
        Fixture.attempts[name] += 1
        first = Fixture.attempts[name] == 1
        if name == "flaky" and first:
            self.send(503, "upstream unavailable", "text/plain")
        elif name == "busy" and first:
            self.send(429, "slow down", "text/plain", {"Retry-After": "1"})
        elif name == "down":
            self.send(503, "upstream unavailable", "text/plain")
        elif name == "downlong":
            self.send(503, "upstream unavailable", "text/plain", {"Retry-After": "30"})
        elif name == "broken":
            self.send(500, json.dumps({"status": 500, "code": "QUERY_FAILED", "message": "worker failed"}),
                      "application/json")
        elif name == "drop":
            self.raw_events(["data: " + json.dumps({"type": "result", "data": ROW}), ""])
        elif name == "cut":
            self.raw_events(['data: {"type":"heartbeat","data":{"status":"waiting"}}', ""])
        else:
            self.events([ROW])
        return True

    def events(self, rows, status="ok"):
        lines = ['data: {"type":"heartbeat","data":{"status":"waiting"}}', ""]
        for row in rows:
            lines += ["data: " + json.dumps({"type": "result", "data": row}), ""]
        lines += ["data: " + json.dumps({"type": "done", "data": {"status": status}}), ""]
        self.send(200, "\n".join(lines) + "\n", "text/event-stream")

    def do_POST(self):
        url = urlsplit(self.path)
        Fixture.seen.append((url.path, self.headers.get("X-CardinalHQ-Query-Id")))
        if self.headers.get("X-CardinalHQ-API-Key") == ECHO_KEY:
            return self.send(400, "rejected key " + ECHO_KEY, "text/plain")
        if self.headers.get("X-CardinalHQ-API-Key") != KEY:
            return self.send(401, "unauthorized", "text/plain")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fixture.requests.append((url.path, parse_qs(url.query), body))
        if body.get("s") != "2026-06-01T12:00:00.000Z" or body.get("e") != "2026-06-01T12:05:00.000Z":
            return self.send(400, "invalid start/end time", "text/plain")
        query = body.get("q", "")
        if url.path == "/gateway/api/v1/logs/tags":
            if query and "bad" in query:
                return self.send(400, "invalid query expression: parse error at 1:5", "text/plain")
            if 'service_name="flaky"' in query:
                Fixture.attempts["flaky-tags"] += 1
                if Fixture.attempts["flaky-tags"] == 1:
                    return self.send(502, "bad gateway", "text/plain")
            if not query and Fixture.null_tags:
                # Go encodes an empty rollup as a nil slice.
                return self.send(200, '{"tags":null}', "application/json")
            tags = ORG_TAGS if not query else ["service_name", "level", "chq_fingerprint", "message", "http_route"]
            return self.send(200, json.dumps({"tags": tags}), "application/json")
        if url.path == "/gateway/api/v1/logs/tagvalues":
            tag = parse_qs(url.query).get("tagName", [""])[0]
            if tag == "failing":
                self.send(200, "data: " + json.dumps({"type": "error", "data": {
                    "code": "QUERY_TOO_LARGE", "message": "query limit exceeded for " + KEY}}) + "\n\n",
                    "text/event-stream")
                return
            values = {"k8s_namespace_name": NAMESPACES, "long": LONG_VALUES}.get(tag, [])
            if body.get("contains"):
                values = [value for value in values if body["contains"] in value]
            if body.get("limit"):
                values = values[:body["limit"]]
            return self.events([{"value": value} for value in values])
        if url.path == "/gateway/api/v1/logs/query":
            if self.transient(query):
                return
            if "bad" in query:
                return self.send(400, json.dumps({"status": 400, "code": "INVALID_EXPR",
                                                  "message": "unexpected token at 1:2"}), "application/json")
            if body.get("reverse") and 'service_name="checkout"' in query:
                return self.events([ROW][:body.get("limit", 1)])
            return self.events([])
        return self.send(404, "not found", "text/plain")


class CardinalQueryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.env = dict(os.environ, CARDINAL_QUERY_URL=f"http://127.0.0.1:{cls.server.server_port}/gateway/api/v1",
                       CARDINAL_QUERY_API_KEY=KEY, CARDINAL_QUERY_RETRY_BASE_MS="20")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Fixture.requests.clear()
        Fixture.seen.clear()
        Fixture.attempts.clear()
        Fixture.null_tags = False
        self.stderr = ""

    def run_skill(self, *args, expect_error=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env, capture_output=True,
                                text=True, timeout=30)
        self.stderr = result.stderr
        if expect_error:
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertRegex(result.stderr, expect_error)
            self.assertNotIn(KEY, result.stderr)
            return result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_logs_with_dotted_label(self):
        logs = self.run_skill("logs", *WINDOW, "--service", "checkout", "--label", "deployment.environment=test",
                              "--limit", "1")
        self.assertEqual(logs["returned"], 1)
        self.assertTrue(logs["limit_reached"])
        self.assertIn('deployment_environment="test"', logs["query"])

    def test_tags_unscoped_uses_rollup_and_flags_missing_core_fields(self):
        tags = self.run_skill("tags", *WINDOW)
        path, _, body = Fixture.requests[-1]
        self.assertEqual(path, "/gateway/api/v1/logs/tags")
        self.assertNotIn("q", body)
        self.assertEqual(tags["source"], "org_daily_rollup")
        self.assertEqual(tags["tags"], sorted(ORG_TAGS))
        self.assertEqual(tags["core_fields_not_listed"], ["level"])
        self.assertIn("note", tags)

    def test_tags_scoped_match_and_limit(self):
        tags = self.run_skill("tags", *WINDOW, "--service", "checkout", "--level", "error", "--match", "_", "--limit", "2")
        _, _, body = Fixture.requests[-1]
        self.assertEqual(body["q"], '{service_name="checkout", level=~"(?i)^(?:error)$"}')
        self.assertEqual(tags["source"], "matching_rows")
        self.assertEqual(tags["matched"], 3)  # chq_fingerprint, http_route, service_name
        self.assertEqual(tags["tags"], ["chq_fingerprint", "http_route"])
        self.assertEqual(tags["returned"], 2)
        self.assertTrue(tags["truncated"])
        self.assertEqual(tags["core_fields_not_listed"], [])
        # Name matching ignores case.
        tags = self.run_skill("tags", *WINDOW, "--service", "checkout", "--match", "ROUTE")
        self.assertEqual(tags["tags"], ["http_route"])

    def test_values_scope_match_limit_and_shortening(self):
        values = self.run_skill("values", *WINDOW, "--tag", "k8s.namespace.name", "--query",
                                '{service_name="checkout"}', "--match", "checkout", "--limit", "5")
        path, params, body = Fixture.requests[-1]
        self.assertEqual(path, "/gateway/api/v1/logs/tagvalues")
        self.assertEqual(params["tagName"], ["k8s_namespace_name"])
        self.assertEqual((body["q"], body["contains"], body["limit"]), ('{service_name="checkout"}', "checkout", 5))
        self.assertEqual(values["values"], ["checkout-prod", "checkout-staging"])
        self.assertFalse(values["limit_reached"])

        values = self.run_skill("values", *WINDOW, "--tag", "k8s_namespace_name", "--limit", "4")
        self.assertNotIn("q", Fixture.requests[-1][2])
        self.assertTrue(values["limit_reached"])
        self.assertEqual(values["shortened_values"], 1)
        self.assertTrue(all(len(value) <= 257 for value in values["values"]))

    def test_server_error_text_reaches_the_agent(self):
        self.run_skill("tags", *WINDOW, "--query", "{bad=}", expect_error=r"HTTP 400\. invalid query expression: parse error")
        self.run_skill("logs", *WINDOW, "--query", "{bad", expect_error=r"HTTP 400\. unexpected token at 1:2")

    def test_input_validation(self):
        self.run_skill("values", *WINDOW, "--tag", "message", expect_error="Use logs or issues")
        self.run_skill("values", *WINDOW, "--tag", "1bad", expect_error="label name")
        self.run_skill("values", *WINDOW, "--tag", "level", "--limit", "1001", expect_error="at most 1000")
        self.run_skill("tags", *WINDOW, "--service", "a", "--query", "{x=\"y\"}", expect_error="in --query")
        self.run_skill("tags", *WINDOW, "--query", '{x="y"} |= "z"', expect_error="stream selector")

    def test_bad_key_is_redacted(self):
        env, self.env = self.env, dict(self.env, CARDINAL_QUERY_API_KEY="invalid-key")
        try:
            self.run_skill("tags", *WINDOW, expect_error="HTTP 401")
            # This fixture key comes back in the error body, so redaction must remove it.
            self.env["CARDINAL_QUERY_API_KEY"] = ECHO_KEY
            for command in (["tags"], ["values", "--tag", "level"], ["logs"]):
                stderr = self.run_skill(*command, *WINDOW, expect_error=r"HTTP 400\. rejected key \[redacted\]")
                self.assertNotIn(ECHO_KEY, stderr)
        finally:
            self.env = env

    def test_tags_null_rollup_is_an_empty_list(self):
        Fixture.null_tags = True
        tags = self.run_skill("tags", *WINDOW)
        self.assertNotIn("q", Fixture.requests[-1][2])
        self.assertEqual((tags["tags"], tags["matched"], tags["returned"]), ([], 0, 0))
        self.assertEqual(tags["source"], "org_daily_rollup")
        self.assertEqual(tags["core_fields_not_listed"], CORE_FIELDS)

    def test_values_deduplicate_full_values_before_shortening(self):
        # The server sends y*300 twice and y*299+z, which shorten to the same text.
        values = self.run_skill("values", *WINDOW, "--tag", "long", "--limit", "3")
        self.assertEqual(values["returned"], 2)
        self.assertFalse(values["limit_reached"])
        self.assertEqual(values["values"], ["y" * 256 + "\u2026"] * 2)
        self.assertEqual(values["shortened_values"], 2)

        # Four rows hold three distinct values, so the limit of 4 was not reached.
        values = self.run_skill("values", *WINDOW, "--tag", "long", "--limit", "4")
        self.assertEqual(values["returned"], 3)
        self.assertFalse(values["limit_reached"])

    def test_values_shorten_by_code_point_and_keep_timestamp_text(self):
        values = self.run_skill("values", *WINDOW, "--tag", "long")
        self.assertEqual(values["returned"], 5)
        self.assertEqual(values["values"], [
            "2026-11-16T10:00:00.000+00:00", "2026-11-16T10:00:00Z", "a" + EMOJI * 255 + "\u2026",
            "y" * 256 + "\u2026", "y" * 256 + "\u2026"])
        self.assertNotIn("\ufffd", "".join(values["values"]))
        self.assertEqual(values["shortened_values"], 3)

    def test_values_source(self):
        values = self.run_skill("values", *WINDOW, "--tag", "k8s_namespace_name")
        self.assertEqual(values["source"], "org_daily_rollup")
        self.assertIn("note", values)
        for scope in (["--service", "checkout"], ["--match", "prod"], ["--query", '{service_name="checkout"}']):
            values = self.run_skill("values", *WINDOW, "--tag", "k8s_namespace_name", *scope)
            self.assertEqual(values["source"], "matching_rows")
            self.assertNotIn("note", values)

    def test_names_reject_a_trailing_newline(self):
        self.run_skill("values", *WINDOW, "--tag", "level\n", expect_error="label name")
        self.run_skill("logs", *WINDOW, "--label", "level\n=x", expect_error="LABEL=VALUE")
        self.run_skill("logs", "--since", "1h\n", expect_error="duration")
        self.run_skill("issues", *WINDOW, "--fingerprint", "1\n", expect_error="64-bit")
        self.assertEqual(Fixture.seen, [])

    def test_stream_error_event_message_is_reported_and_redacted(self):
        stderr = self.run_skill("values", *WINDOW, "--tag", "failing",
                                expect_error=r"query limit exceeded for \[redacted\] Query ID: ")
        self.assertNotIn("retrying", stderr)
        self.assertEqual(len(Fixture.seen), 1)

    def assert_distinct_ids(self, count):
        ids = [query_id for _, query_id in Fixture.seen]
        self.assertEqual(len(ids), count)
        self.assertEqual(len(set(ids)), count)

    def test_retry_after_503(self):
        logs = self.run_skill("logs", *WINDOW, "--service", "flaky")
        self.assertEqual(logs["returned"], 1)
        self.assert_distinct_ids(2)
        self.assertEqual(logs["query_ids"], [Fixture.seen[-1][1]])
        self.assertEqual(self.stderr, "cardinal-query: retrying after HTTP 503 (attempt 2 of 3)\n")

        Fixture.seen.clear()
        tags = self.run_skill("tags", *WINDOW, "--service", "flaky")
        self.assertEqual(tags["source"], "matching_rows")
        self.assert_distinct_ids(2)
        self.assertEqual(self.stderr, "cardinal-query: retrying after HTTP 502 (attempt 2 of 3)\n")

    def test_retry_after_429_honors_retry_after(self):
        started = time.monotonic()
        logs = self.run_skill("logs", *WINDOW, "--service", "busy")
        self.assertGreaterEqual(time.monotonic() - started, 0.95)
        self.assertEqual(logs["returned"], 1)
        self.assert_distinct_ids(2)
        self.assertIn("retrying after HTTP 429 (attempt 2 of 3)", self.stderr)

    def test_query_and_auth_errors_are_not_retried(self):
        for args, error in (((*WINDOW, "--query", "{bad"), "HTTP 400"),
                            ((*WINDOW, "--service", "broken"), r"HTTP 500\. worker failed Query ID")):
            Fixture.seen.clear()
            stderr = self.run_skill("logs", *args, expect_error=error)
            self.assertEqual(len(Fixture.seen), 1)
            self.assertNotIn("retrying", stderr)
            self.assertNotIn("attempts", stderr)
        Fixture.seen.clear()
        env, self.env = self.env, dict(self.env, CARDINAL_QUERY_API_KEY="invalid-key")
        try:
            self.run_skill("tags", *WINDOW, expect_error="HTTP 401")
        finally:
            self.env = env
        self.assertEqual(len(Fixture.seen), 1)

    def test_persistent_503_reports_attempts(self):
        stderr = self.run_skill("logs", *WINDOW, "--service", "down", "--retries", "3",
                                expect_error=r"HTTP 503\. upstream unavailable Failed after 4 attempts\. Query ID: ")
        self.assert_distinct_ids(4)
        self.assertEqual(stderr.count("retrying after HTTP 503"), 3)
        self.assertIn(Fixture.seen[-1][1], stderr)
        Fixture.seen.clear()
        stderr = self.run_skill("logs", *WINDOW, "--service", "down", "--retries", "0",
                                expect_error=r"HTTP 503\. upstream unavailable Query ID: ")
        self.assertEqual(len(Fixture.seen), 1)
        self.assertNotIn("attempts", stderr)
        self.run_skill("logs", *WINDOW, "--retries", "6", expect_error="0 to 5")

    def test_retry_stops_when_the_wait_exceeds_the_budget(self):
        started = time.monotonic()
        stderr = self.run_skill("logs", *WINDOW, "--service", "downlong", "--timeout", "2",
                                expect_error=r"HTTP 503\. upstream unavailable Query ID: ")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(Fixture.seen), 1)
        self.assertNotIn("retrying", stderr)

    def test_retry_after_is_capped_at_eight_seconds(self):
        # Retry-After: 30 would exceed the 20s budget; the 8s cap lets one retry fit.
        started = time.monotonic()
        stderr = self.run_skill("logs", *WINDOW, "--service", "downlong", "--timeout", "20", "--retries", "1",
                                expect_error=r"HTTP 503\. upstream unavailable Failed after 2 attempts\.")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 7.9)
        self.assertLess(elapsed, 15)
        self.assertEqual(len(Fixture.seen), 2)
        self.assertIn("retrying after HTTP 503 (attempt 2 of 2)", stderr)

    def test_stream_is_retried_only_before_the_first_result(self):
        stderr = self.run_skill("logs", *WINDOW, "--service", "drop", expect_error="ended before completion")
        self.assertEqual(len(Fixture.seen), 1)
        self.assertNotIn("retrying", stderr)
        Fixture.seen.clear()
        stderr = self.run_skill("logs", *WINDOW, "--service", "cut",
                                expect_error=r"ended before completion\. Failed after 3 attempts\.")
        self.assertEqual(len(Fixture.seen), 3)
        self.assertIn("retrying after an interrupted stream (attempt 3 of 3)", stderr)

    def test_connection_refused_is_retried_within_the_budget(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env, self.env = self.env, dict(self.env, CARDINAL_QUERY_URL=f"http://127.0.0.1:{port}/api/v1")
        try:
            started = time.monotonic()
            stderr = self.run_skill("tags", *WINDOW, "--timeout", "5",
                                    expect_error=r"Query API connection failed: .* Failed after 3 attempts\. Query ID: ")
            self.assertLess(time.monotonic() - started, 5)
            self.assertIn("retrying after a connection error (attempt 2 of 3)", stderr)
            self.assertIn("retrying after a connection error (attempt 3 of 3)", stderr)
        finally:
            self.env = env


if __name__ == "__main__":
    unittest.main()

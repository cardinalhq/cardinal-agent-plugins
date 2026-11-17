"""Fixture tests for the Python Cardinal Query client.

Run with: python3 -m unittest discover -s tests/cardinal-query -p 'test_*.py'
"""

from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).resolve().parents[2] / ".github/skills/cardinal-query/scripts/cardinal_query.py"
PREFIX = f"python3 {shlex.quote(str(SCRIPT))} logs --start 2026-06-01T12:00:00.000Z --end 2026-06-01T12:05:00.000Z"
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
ROW_FIELDS = ["message", "service_name", "level", "chq_fingerprint"]
START = "2026-06-01T12:00:00.000Z"
END = "2026-06-01T12:05:00.000Z"
# With CARDINAL_QUERY_NOW set to NOW and no --end, the window ends at NOW and pattern
# counts query to END, the next bucket boundary.
NOW = "2026-06-01T12:04:30.000Z"
MAX_BYTES = 16384
# Pattern counts for service "shop", stamped at the end of each 60-second bucket.
# Window buckets end at 12:01 to 12:05; 12:00 and 12:06 fall outside the window.
SHOP_POINTS = [
    (BUCKET, PATTERN, 3), (BUCKET + 120000, PATTERN, 4), (BUCKET + 60000, 42, 5), (BUCKET + 60000, "7", 5),
    (BUCKET + 60000, None, 2), (BUCKET + 180000, "0", 1), (BUCKET - 60000, PATTERN, 99),
    (BUCKET + 300000, PATTERN, 99), (BUCKET, "9", 0), (BUCKET, "9", 2.4),
]
LONG_MESSAGE = "\u00e9" + EMOJI * 400
MINUTE = 60000
# (service, fingerprint, minutes after 12:00, count). The server emits points stamped from
# the start to the query end. 12:00 closes a bucket before the window, 12:05 closes the last
# bucket of a window ending at 12:05 or at NOW, and 12:06 is after every query end. 2.6 and
# 3.5 round to 3 and 4. a/9, b/1 and b/5 tie at 4 and rank by service, then fingerprint.
EDGE_POINTS = [("a", "1", 0, 50), ("a", "1", 1, 2.6), ("b", "5", 3, 3.5), ("a", "9", 2, 2), ("a", "9", 4, 2),
               ("b", "1", 2, 4), ("a", "1", 5, 7), ("a", "1", 6, 99)]
# A multi-KB stack trace, and a message longer than the full-message cap.
STACK = "Traceback (most recent call last):\n" + "".join(
    f'  File "/srv/app/module_{index:03d}.py", line {index}, in handler_{index}\n\traise Error({index})\n'
    for index in range(80))
EMOJI_STACK = EMOJI * 9000
# A float, integer and string the serializer must write as Python does.
EXACT_TAGS = {"f1": 0.7999999999999999, "f2": 0.30000000000000004, "f3": 1.25, "f4": -0.5,
              "i1": 9007199254740993, "s1": "del\x7f sep\u2028 nul\x00"}
EXACT_TEXT = ['"f1":0.7999999999999999', '"f2":0.30000000000000004', '"f3":1.25', '"f4":-0.5',
              '"i1":9007199254740993', '"s1":"del\\u007f sep\\u2028 nul\\u0000"']


def edge_points(body):
    """The edge scenario's points, as the Query API emits them for the query window."""
    start, end = (int(datetime.fromisoformat(body[key].replace("Z", "+00:00")).timestamp() * 1000)
                  for key in ("s", "e"))
    base = BUCKET - MINUTE
    return [{"timestamp": base + minutes * MINUTE, "value": value,
             "tags": {"service_name": service, "chq_fingerprint": fingerprint, "level": "ERROR"}}
            for service, fingerprint, minutes, value in EDGE_POINTS if start <= base + minutes * MINUTE <= end]


def wide_row(index, attributes, length):
    tags = {"service_name": "attrs", "level": "INFO", "chq_fingerprint": str(index), "message": f"row {index}"}
    tags.update((f"attr_{key:03d}", "v" * length) for key in range(attributes))
    return {"timestamp": BUCKET - index, "tags": tags}


def perf_row(index):
    # The same rows as windows-smoke.ps1: 150 attributes, half long strings, half objects.
    tags = {"service_name": "perf", "level": "ERROR", "chq_fingerprint": str(index), "message": "m" * 2000}
    for key in range(150):
        tags[f"attr_{key:03d}"] = ("v" * (5 + (key * 37 + index * 11) % 396) if key % 2 else
                                   {"a": [1, 2, {"b": "c" * 50}], "d": 1.25})
    return {"timestamp": BUCKET - index, "timestamp_ns": 1, "tags": tags}


def keep_points():
    # Long service names and fingerprints, which the budget must never shorten.
    return [{"timestamp": BUCKET, "value": 100 - index, "tags": {
        "service_name": f"svc{index:02d}-" + "s" * 300, "chq_fingerprint": "1" * 150 + f"{index:02d}"}}
        for index in range(40)]


def shop_row(fingerprint, message, offset=0):
    tags = {"service_name": "shop", "level": "ERROR", "message": message}
    if fingerprint is not None:
        tags["chq_fingerprint"] = fingerprint
    return {"timestamp": BUCKET + 100000 - offset, "tags": tags}


SHOP_ROWS = [shop_row(PATTERN, LONG_MESSAGE), shop_row(PATTERN, "older", 1000), shop_row("7", "seven", 2000),
             shop_row(None, "no fingerprint", 3000)]


def load_client():
    spec = importlib.util.spec_from_file_location("cardinal_query", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode, saved = True, sys.dont_write_bytecode
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


def flood_rows(limit):
    # Rows of non-ASCII and emoji text, larger than the output budget.
    return [{"timestamp": BUCKET + 200000 - index * 1000, "tags": {
        "service_name": "flood", "level": "INFO", "chq_fingerprint": str(1000 + index),
        "message": f"{index:03d} " + "\u00e9" * 100 + EMOJI * 200}} for index in range(limit)]


def count_points(query):
    """Answer count_over_time scenarios, selected by service or label. Return None if not one."""
    if 'service_name="shop"' in query:
        return [{"timestamp": ts, "tags": {"service_name": "shop", **({} if fp is None else {"chq_fingerprint": fp})},
                 "value": value} for ts, fp, value in SHOP_POINTS]
    if 'service_name="many"' in query:
        return [{"timestamp": BUCKET, "tags": {"service_name": "many", "chq_fingerprint": str(index)},
                 "value": 100 - index} for index in range(1, 31)]
    if 'service_name="wide"' in query:
        return [{"timestamp": BUCKET, "tags": {"service_name": "wide", "chq_fingerprint": str(5000 + index)},
                 "value": 1000 - index} for index in range(150)]
    if 'scenario="keep"' in query:
        return keep_points()
    if 'service_name="rawonly"' in query:
        # No fingerprint rollups: only a raw scan, forced by a message predicate, has counts.
        return [{"timestamp": BUCKET, "tags": {"service_name": "rawonly", "chq_fingerprint": "5"}, "value": 4}] \
            if '|~ "(?s:.*)"' in query else []
    if 'scenario="services"' in query:
        points = [{"timestamp": BUCKET, "tags": {"service_name": f"svc{index:02d}", "level": "ERROR",
                                                 "chq_fingerprint": str(100 + index)}, "value": 12 - index}
                  for index in range(12)]
        return points + [{"timestamp": BUCKET, "tags": {"service_name": "svc00", "level": "WARN",
                                                        "chq_fingerprint": "200"}, "value": 1}]
    return None


def row_scenario(query, limit):
    """Answer newest-first row scenarios. Return None if not one."""
    if 'service_name="shop"' in query:
        if 'chq_fingerprint="42"' in query:
            return [shop_row("42", "forty-two")]
        if f'chq_fingerprint="{PATTERN}"' in query:
            return [shop_row(PATTERN, LONG_MESSAGE)]
        return [] if "chq_fingerprint=" in query else SHOP_ROWS[:limit]
    if 'service_name="flood"' in query:
        return flood_rows(limit)
    if 'service_name="wide"' in query and "chq_fingerprint=" not in query:
        return [{"timestamp": BUCKET, "tags": {"service_name": "wide", "chq_fingerprint": str(5000 + index),
                                               "message": EMOJI * 300}} for index in range(10)]
    if 'scenario="keep"' in query:
        return [{"timestamp": BUCKET, "tags": {**point["tags"], "message": "k" * 300}} for point in keep_points()]
    if 'service_name="stack"' in query:
        message = EMOJI_STACK if 'chq_fingerprint="78"' in query else STACK
        return [{"timestamp": BUCKET - index, "tags": {"service_name": "stack", "chq_fingerprint": "77",
                                                      "message": message}} for index in range(limit)]
    if 'service_name="attrs"' in query:
        return [wide_row(index, 150, 50) for index in range(limit)]
    if 'service_name="perf"' in query:
        return [perf_row(index) for index in range(limit)]
    if 'service_name="exact"' in query:
        return [{"timestamp": BUCKET, "tags": dict(EXACT_TAGS, service_name="exact")}]
    if 'service_name="edge"' in query or 'service_name="rawonly"' in query:
        return []
    if 'scenario="services"' in query and 'chq_fingerprint="100"' in query:
        return [{"timestamp": BUCKET, "tags": {"service_name": "svc00", "message": "x" * 500}}]
    if 'service_name="many"' in query:
        return []
    return None


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
        if body.get("s") != START or body.get("e") not in (END, NOW):
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
            values = {"k8s_namespace_name": NAMESPACES, "long": LONG_VALUES, "emoji": [EMOJI * 200]}.get(tag, [])
            if body.get("contains"):
                values = [value for value in values if body["contains"] in value]
            if body.get("limit"):
                values = values[:body["limit"]]
            return self.events([{"value": value} for value in values])
        if url.path == "/gateway/api/v1/logs/query":
            if self.transient(query):
                return
            if 'service_name="partial"' in query:
                return self.events([], status="error")
            if "count_over_time" in query and 'service_name="edge"' in query:
                return self.events(edge_points(body))
            points = count_points(query) if "count_over_time" in query else None
            if points is not None:
                return self.events(points)
            rows = row_scenario(query, body.get("limit", 1)) if body.get("reverse") else None
            if rows is not None:
                return self.events(rows)
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
        self.stdout = result.stdout
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
                              "--limit", "1", "--raw")
        self.assertEqual(logs["returned"], 1)
        self.assertTrue(logs["limit_reached"])
        self.assertIn('deployment_environment="test"', logs["query"])

    def requests_for(self, needle):
        return [body for path, _, body in Fixture.requests if needle in body.get("q", "")]

    def test_logs_default_ranks_patterns_with_window_counts(self):
        logs = self.run_skill("logs", *WINDOW, "--service", "shop")
        count_query, exemplar_fetch, *fills = [body for _, _, body in Fixture.requests]
        self.assertEqual(count_query["q"],
                         'sum by (service_name, chq_fingerprint)(count_over_time({service_name="shop"}[60s]))')
        self.assertEqual(count_query["step"], 60)
        self.assertNotIn("limit", count_query)
        # One bounded newest-first fetch with only the exemplar fields, then one query per missed pattern.
        self.assertEqual((exemplar_fetch["q"], exemplar_fetch["limit"], exemplar_fetch["reverse"], exemplar_fetch["fields"]),
                         ('{service_name="shop"}', 50, True, ROW_FIELDS))
        self.assertEqual(sorted(body["q"] for body in fills),
                         ['{service_name="shop", chq_fingerprint="42"}', '{service_name="shop", chq_fingerprint="9"}'])
        self.assertTrue(all(body["limit"] == 1 and body["fields"] == ROW_FIELDS for body in fills))

        self.assertEqual(logs["mode"], "patterns")
        self.assertEqual((logs["total"], logs["unfingerprinted"], logs["matched_patterns"], logs["limit"]), (22, 3, 4, 10))
        # Counts reconcile: every in-window count is a pattern or unfingerprinted.
        self.assertEqual(sum(p["count"] for p in logs["patterns"]) + logs["unfingerprinted"], logs["total"])
        self.assertEqual([(p["fingerprint"], p["count"]) for p in logs["patterns"]],
                         [(PATTERN, 7), ("42", 5), ("7", 5), ("9", 2)])
        top = logs["patterns"][0]
        self.assertEqual((top["service"], top["first_seen"], top["last_seen"]),
                         ("shop", "2026-06-01T12:00:00.000Z", "2026-06-01T12:03:00.000Z"))
        # The newest row picks the exemplar; the message is cut to 300 code points, not UTF-16 units.
        self.assertEqual(top["exemplar"]["message"], LONG_MESSAGE[:300] + "\u2026")
        self.assertEqual(top["exemplar"]["message_chars"], 401)
        self.assertEqual(top["exemplar"]["timestamp"], BUCKET + 100000)
        self.assertEqual(logs["patterns"][1]["exemplar"], {"message": "forty-two", "timestamp": BUCKET + 100000})
        self.assertEqual(logs["patterns"][2]["exemplar"]["message"], "seven")
        self.assertNotIn("exemplar", logs["patterns"][3])
        self.assertIn("60-second buckets", logs["note"])
        self.assertEqual(len(logs["query_ids"]), 4)

        self.assertEqual((logs["unlisted_patterns"], logs["unlisted_events"]), (0, 0))

        logs = self.run_skill("logs", *WINDOW, "--service", "shop", "--limit", "1")
        self.assertEqual((len(logs["patterns"]), logs["matched_patterns"]), (1, 4))
        # Patterns beyond the limit, and their events, are counted.
        self.assertEqual((logs["unlisted_patterns"], logs["unlisted_events"]), (3, 12))

    def test_logs_patterns_bound_exemplar_requests_and_clamp_the_limit(self):
        logs = self.run_skill("logs", *WINDOW, "--service", "many", "--limit", "300")
        self.assertEqual((logs["limit"], logs["limit_clamped_from"], len(logs["patterns"])), (200, 300, 30))
        fills = [body for body in self.requests_for("chq_fingerprint=")]
        self.assertEqual(len(Fixture.requests), 2 + 10)
        self.assertEqual(sorted(body["q"] for body in fills),
                         sorted(f'{{service_name="many", chq_fingerprint="{index}"}}' for index in range(1, 11)))

    def test_logs_patterns_with_a_raw_query(self):
        logs = self.run_skill("logs", *WINDOW, "--query", '{service_name="shop"}', "--contains", "o'k")
        self.assertEqual(Fixture.requests[0][2]["q"], 'sum by (service_name, chq_fingerprint)(count_over_time('
                         '{service_name="shop"} |~ "(?s:.*o\'k.*)"[60s]))')
        self.assertIn('{chq_fingerprint="42", service_name="shop"} |~ "(?s:.*o\'k.*)"',
                      [body["q"] for _, _, body in Fixture.requests])
        self.assertEqual(logs["next"][0]["run"], PREFIX + ' --query \'{service_name="shop"}\' --contains '
                         + shlex.quote("o'k") + f" --fingerprint {PATTERN} --raw")

    def test_next_hint_drills_into_the_top_pattern(self):
        logs = self.run_skill("logs", *WINDOW, "--service", "shop", "--level", "error",
                              "--label", "k8s.namespace.name=prod east", "--not-contains", "health")
        hint = logs["next"][0]
        self.assertEqual(hint["run"], PREFIX + " --service shop --level error --label 'k8s_namespace_name=prod east'"
                         f" --not-contains health --fingerprint {PATTERN} --raw")
        # The hint is a command the agent can run as is, with the full signed fingerprint.
        words = shlex.split(hint["run"])
        self.assertEqual(words[:2], ["python3", str(SCRIPT)])
        Fixture.requests.clear()
        rows = self.run_skill(*words[2:])
        query = Fixture.requests[-1][2]["q"]
        self.assertEqual(query, '{service_name="shop", level=~"(?i)^(?:error)$", k8s_namespace_name="prod east", '
                                f'chq_fingerprint="{PATTERN}"}} !~ "(?s:.*health.*)"')
        self.assertEqual((rows["mode"], rows["returned"]), ("raw", 1))
        self.assertEqual(rows["logs"][0]["tags"]["chq_fingerprint"], PATTERN)
        self.assertEqual(rows["logs"][0]["message_chars"], 401)

    def test_label_drill_down_still_works(self):
        rows = self.run_skill("logs", *WINDOW, "--service", "shop", "--label", f"chq_fingerprint={PATTERN}", "--raw")
        self.assertEqual(Fixture.requests[-1][2]["q"], f'{{service_name="shop", chq_fingerprint="{PATTERN}"}}')
        self.assertEqual(rows["returned"], 1)

    def test_raw_rows_use_a_default_projection(self):
        rows = self.run_skill("logs", *WINDOW, "--service", "shop", "--raw")
        body = Fixture.requests[-1][2]
        self.assertEqual((body["fields"], body["limit"], body["reverse"]), (ROW_FIELDS, 20, True))
        self.assertEqual((rows["mode"], rows["fields"], rows["returned"], rows["limit_reached"]), ("raw", ROW_FIELDS, 4, False))
        self.assertEqual(rows["logs"][0]["tags"]["message"], LONG_MESSAGE[:300] + "\u2026")
        self.assertEqual((rows["logs"][0]["message_chars"], rows["truncated_messages"]), (401, 1))
        self.assertNotIn("next", rows)

        rows = self.run_skill("logs", *WINDOW, "--service", "shop", "--raw", "--all-fields", "--limit", "2")
        self.assertNotIn("fields", Fixture.requests[-1][2])
        self.assertIsNone(rows["fields"])
        self.assertTrue(rows["limit_reached"])
        # A full sample points back to the true counts.
        self.assertEqual(rows["next"][0]["run"], PREFIX + " --service shop")

        rows = self.run_skill("logs", *WINDOW, "--service", "shop", "--raw", "--field", "message", "--field", "level")
        self.assertEqual(Fixture.requests[-1][2]["fields"], ["message", "level"])
        self.assertEqual(rows["fields"], ["message", "level"])

    def test_raw_limit_is_clamped(self):
        rows = self.run_skill("logs", *WINDOW, "--service", "shop", "--raw", "--limit", "500")
        self.assertEqual(Fixture.requests[-1][2]["limit"], 200)
        self.assertEqual((rows["limit"], rows["limit_clamped_from"]), (200, 500))

    def test_logs_option_validation(self):
        self.run_skill("logs", *WINDOW, "--raw", "--field", "message", "--all-fields", expect_error="either --field")
        self.run_skill("logs", *WINDOW, "--fingerprint", "1.5", expect_error="64-bit")
        self.run_skill("logs", *WINDOW, "--field", "1bad", expect_error="label name")
        many = [word for index in range(51) for word in ("--field", f"f{index}")]
        self.run_skill("logs", *WINDOW, *many, expect_error="at most 50")
        self.assertEqual(Fixture.seen, [])

    def test_projection_and_full_message_imply_raw(self):
        for extra, fields in ((["--field", "message"], ["message"]), (["--all-fields"], None),
                              (["--full-message"], ROW_FIELDS)):
            Fixture.requests.clear()
            rows = self.run_skill("logs", *WINDOW, "--service", "shop", *extra)
            self.assertEqual((rows["mode"], rows["fields"], rows["limit"], rows["returned"]), ("raw", fields, 20, 4))
            # One row fetch, and no count query.
            self.assertEqual([(body["reverse"], body["limit"]) for _, _, body in Fixture.requests], [(True, 20)])

    def test_partial_results_fail_closed(self):
        for mode in ([], ["--raw"]):
            self.run_skill("logs", *WINDOW, "--service", "partial", *mode, expect_error="Results may be incomplete")
        self.run_skill("logs", "--since", "10s", "--end", "2026-06-01T12:00:30Z", expect_error="no completed bucket")

    def assert_within_budget(self):
        text = self.stdout.rstrip("\n")
        self.assertTrue(text.isascii())
        self.assertLessEqual(len(text.encode("utf-8")), MAX_BYTES)
        json.loads(text)

    def test_budget_shrinks_raw_rows_of_non_ascii_text(self):
        rows = self.run_skill("logs", *WINDOW, "--service", "flood", "--raw", "--limit", "200")
        self.assert_within_budget()
        self.assertTrue(rows["budget_applied"])
        # Each emoji counts as 12 bytes (\ud83d\ude00) and each e-acute as 6, in both clients.
        # windows-smoke.ps1 asserts the same numbers from the same fixture data. Strings are
        # shortened, then the oldest rows are dropped; shortened_strings counts returned strings.
        self.assertEqual((rows["shortened_strings"], rows["omitted"], len(rows["logs"])), (20, {"logs": 180}, 20))
        self.assertEqual(rows["returned"], 200)
        self.assertEqual(rows["logs"][0]["tags"]["message"], "000 " + "\u00e9" * 96 + "\u2026")
        self.assertEqual(rows["logs"][0]["tags"]["chq_fingerprint"], "1000")
        self.assertEqual(rows["logs"][-1]["tags"]["chq_fingerprint"], "1019")
        self.assertEqual(rows["next"][0]["run"], PREFIX + " --service flood")

    def test_budget_drops_lowest_ranked_patterns(self):
        logs = self.run_skill("logs", *WINDOW, "--service", "wide", "--limit", "150")
        self.assert_within_budget()
        self.assertTrue(logs["budget_applied"])
        self.assertEqual((logs["omitted"], len(logs["patterns"]), logs["matched_patterns"]), ({"patterns": 134}, 16, 150))
        self.assertEqual([p["fingerprint"] for p in logs["patterns"]], [str(5000 + index) for index in range(16)])
        self.assertEqual(logs["shortened_strings"], 10)
        self.assertEqual(logs["patterns"][0]["exemplar"]["message"], EMOJI * 100 + "\u2026")
        self.assertEqual(logs["next"][0]["run"], PREFIX + " --service wide --fingerprint 5000 --raw")

    def assert_stdout_within_budget(self):
        data = self.stdout.encode("utf-8")
        self.assertLessEqual(len(data), MAX_BYTES)
        self.assertTrue(data.isascii())
        result = json.loads(data)
        for item in result.get("next", []):
            self.assertLessEqual(len(json.dumps(item["run"])), 1024)
        return result

    def test_budget_holds_for_long_arguments(self):
        # Arguments are echoed in query and next[].run. Whatever their length, stdout stays
        # within the budget, for small and large results alike.
        for size in (1500, 3000, 9000, 14000):
            grep = "(a|" + "b" * (size - 4) + ")"
            literal = "c" * size
            cases = (
                (["logs", "--service", "shop", "--grep", grep], False),
                (["logs", "--service", "wide", "--limit", "150", "--grep", grep], True),
                (["logs", "--service", "flood", "--raw", "--limit", "200", "--grep", grep], True),
                (["logs", "--query", '{service_name="shop"} |~ "' + literal + '"'], False),
                (["logs", "--query", '{service_name="flood"} !~ "' + literal + '"', "--raw", "--limit", "200"], True),
                (["issues", "--label", "scenario=services", "--contains", literal], False),
                (["tags", "--query", '{service_name="checkout", x="' + literal + '"}'], False),
            )
            for args, large in cases:
                with self.subTest(size=size, args=args[:3]):
                    self.run_skill(args[0], *WINDOW, *args[1:])
                    result = self.assert_stdout_within_budget()
                    self.assertEqual(result.get("budget_applied", False), large)
                    echo = result.get("query", result.get("scope"))
                    name = "query" if "query" in result else "scope"
                    if len(json.dumps(echo)) > 1900:
                        # Shortened to 2048 bytes of JSON text, with its full length reported.
                        self.assertTrue(echo.endswith("\u2026"))
                        self.assertGreater(result[name + "_chars"], 2048)
                        self.assertLessEqual(len(json.dumps(echo)), 2048)
                    for item in result.get("next", []):
                        # The command is too long to echo, so the hint names what to change.
                        self.assertRegex(item["run"], "^Run (the same )?logs ")

    def test_limits_beyond_int32_are_rejected(self):
        # The PowerShell client's [int] parameters reject them too.
        self.run_skill("logs", *WINDOW, "--limit", "2147483648", expect_error="positive integer")
        self.run_skill("issues", *WINDOW, "--limit", "9" * 5000, expect_error="positive integer|invalid")

    def test_full_message_reads_a_whole_stack_trace(self):
        rows = self.run_skill("logs", *WINDOW, "--service", "stack", "--fingerprint", "77", "--raw", "--limit", "1",
                              "--full-message")
        self.assertEqual(rows["logs"][0]["tags"]["message"], STACK)
        self.assertNotIn("message_chars", rows["logs"][0])
        self.assertNotIn("budget_applied", rows)
        # Without it, the message is cut to 300 code points.
        rows = self.run_skill("logs", *WINDOW, "--service", "stack", "--fingerprint", "77", "--raw", "--limit", "1")
        self.assertEqual((rows["logs"][0]["tags"]["message"], rows["logs"][0]["message_chars"]),
                         (STACK[:300] + "\u2026", len(STACK)))
        # Under budget pressure, rows are dropped before any message is shortened.
        rows = self.run_skill("logs", *WINDOW, "--service", "stack", "--limit", "5", "--full-message")
        self.assert_stdout_within_budget()
        self.assertEqual((rows["mode"], rows["returned"], rows["omitted"], len(rows["logs"])), ("raw", 5, {"logs": 3}, 2))
        self.assertTrue(all(row["tags"]["message"] == STACK for row in rows["logs"]))
        self.assertNotIn("shortened_strings", rows)
        # A message too long for the budget on its own is shortened to fit in the one row kept.
        rows = self.run_skill("logs", *WINDOW, "--service", "stack", "--fingerprint", "78", "--full-message")
        self.assert_stdout_within_budget()
        row = rows["logs"][0]
        self.assertEqual((rows["omitted"], rows["shortened_strings"], row["message_chars"]), ({"logs": 19}, 1, 9000))
        self.assertEqual(row["tags"]["message"], EMOJI * 1230 + "\u2026")

    def test_budget_drops_extra_fields_before_rows(self):
        # Each row has 150 attributes. The newest row keeps them all; once a row does not fit
        # with all of its fields, it and every later row keep only the default fields.
        rows = self.run_skill("logs", *WINDOW, "--service", "attrs", "--all-fields", "--limit", "200")
        self.assert_stdout_within_budget()
        self.assertTrue(rows["budget_applied"])
        self.assertEqual(len(rows["logs"][0]["tags"]), 154)
        self.assertTrue(all(sorted(row["tags"]) == sorted(ROW_FIELDS) for row in rows["logs"][1:]))
        self.assertEqual((len(rows["logs"]), rows["omitted"]), (45, {"fields": 44, "logs": 155}))
        self.assertEqual(rows["logs"][-1]["tags"]["chq_fingerprint"], "44")

    def test_budget_handles_wide_rows_quickly(self):
        # 200 rows of 150 attributes, about 5 MB. windows-smoke.ps1 times the same rows.
        started = time.monotonic()
        rows = self.run_skill("logs", *WINDOW, "--service", "perf", "--all-fields", "--limit", "200")
        self.assertLess(time.monotonic() - started, 5)
        self.assert_stdout_within_budget()
        self.assertEqual((len(rows["logs"]), rows["omitted"], rows["shortened_strings"]),
                         (58, {"fields": 58, "logs": 142}, 58))

    def test_budget_never_shortens_identifiers(self):
        logs = self.run_skill("logs", *WINDOW, "--label", "scenario=keep", "--limit", "40")
        self.assert_stdout_within_budget()
        self.assertTrue(logs["budget_applied"])
        self.assertEqual((len(logs["patterns"]), logs["omitted"]), (22, {"patterns": 18}))
        for index, pattern in enumerate(logs["patterns"]):
            self.assertEqual(pattern["service"], f"svc{index:02d}-" + "s" * 300)
            self.assertEqual(pattern["fingerprint"], "1" * 150 + f"{index:02d}")
        self.assertEqual(logs["patterns"][0]["exemplar"]["message"], "k" * 100 + "\u2026")

    def test_counts_include_the_newest_bucket_when_the_window_ends_now(self):
        env, self.env = self.env, dict(self.env, CARDINAL_QUERY_NOW=NOW)
        try:
            logs = self.run_skill("logs", "--start", START, "--service", "edge")
            counts = Fixture.requests[0][2]
            fetches = [body["e"] for _, _, body in Fixture.requests[1:]]
            Fixture.requests.clear()
            issues = self.run_skill("issues", "--start", START, "--service", "edge")
        finally:
            self.env = env
        # Counts query to the next bucket boundary; rows and the echoed window end at now.
        self.assertEqual((counts["s"], counts["e"], set(fetches)), (START, END, {NOW}))
        self.assertEqual((logs["start"], logs["end"], logs["total"]), (START, NOW, 22))
        self.assertEqual([(p["service"], p["fingerprint"], p["count"]) for p in logs["patterns"]],
                         [("a", "1", 10), ("a", "9", 4), ("b", "1", 4), ("b", "5", 4)])
        self.assertEqual((logs["patterns"][0]["first_seen"], logs["patterns"][0]["last_seen"]), (START, NOW))
        self.assertIn("the newest bucket runs to now", logs["note"])
        self.assertEqual(logs["next"][0]["run"], f"python3 {shlex.quote(str(SCRIPT))} logs --start {START} --end {NOW}"
                                                 " --service a --fingerprint 1 --raw")
        self.assertEqual(Fixture.requests[0][2]["e"], END)
        self.assertEqual((issues["total"], [row["timestamp"] for row in issues["trend"]][-1]), (22, BUCKET + 4 * MINUTE))
        self.assertEqual([(p["service"], p["fingerprint"], p["count"]) for p in issues["patterns"]],
                         [("a", "1", 10), ("a", "9", 4), ("b", "1", 4), ("b", "5", 4)])

    def test_explicit_end_keeps_exact_bucket_boundaries(self):
        # The bucket that straddles an explicit end is left out.
        logs = self.run_skill("logs", "--start", START, "--end", NOW, "--service", "edge")
        self.assertEqual(Fixture.requests[0][2]["e"], NOW)
        self.assertEqual(logs["total"], 15)
        self.assertEqual([(p["service"], p["fingerprint"], p["count"]) for p in logs["patterns"]],
                         [("a", "9", 4), ("b", "1", 4), ("b", "5", 4), ("a", "1", 3)])
        self.assertIn("the bucket that straddles the end is left out", logs["note"])
        issues = self.run_skill("issues", "--start", START, "--end", NOW, "--service", "edge")
        self.assertEqual(issues["total"], 15)
        # An aligned end leaves nothing out: the point stamped at the end is counted.
        logs = self.run_skill("logs", *WINDOW, "--service", "edge")
        self.assertEqual((logs["total"], logs["patterns"][0]["count"], logs["patterns"][0]["last_seen"]), (22, 10, END))
        self.assertEqual(logs["note"], "Counts are totals in fixed 60-second buckets. first_seen and last_seen are "
                                       "bucket bounds. Use raw rows for exact boundaries.")

    def test_logs_fall_back_to_a_raw_scan_without_rollups(self):
        logs = self.run_skill("logs", *WINDOW, "--service", "rawonly")
        queries = [body["q"] for _, _, body in Fixture.requests if "count_over_time" in body["q"]]
        self.assertEqual(queries, [
            'sum by (service_name, chq_fingerprint)(count_over_time({service_name="rawonly"}[60s]))',
            'sum by (service_name, chq_fingerprint)(count_over_time({service_name="rawonly"} |~ "(?s:.*)"[60s]))'])
        self.assertEqual((logs["total"], logs["patterns"][0]["fingerprint"], logs["patterns"][0]["count"]), (4, "5", 4))
        # A message filter already scans raw logs.
        Fixture.requests.clear()
        logs = self.run_skill("logs", *WINDOW, "--service", "rawonly", "--contains", "x")
        self.assertEqual((logs["total"], len(Fixture.requests)), (0, 1))

    def test_raw_query_patterns_keep_their_service(self):
        # The selector pins no service, so exemplar fetches and the drill-down add the pattern's.
        logs = self.run_skill("logs", *WINDOW, "--query", '{scenario="services"}', "--limit", "1")
        self.assertIn('{chq_fingerprint="100", service_name="svc00", scenario="services"}',
                      [body["q"] for _, _, body in Fixture.requests])
        self.assertEqual(logs["patterns"][0]["exemplar"]["message_chars"], 500)
        run = logs["next"][0]["run"]
        self.assertEqual(run, PREFIX + " --query '{service_name=\"svc00\", scenario=\"services\"}' --fingerprint 100 --raw")
        Fixture.requests.clear()
        rows = self.run_skill(*shlex.split(run)[2:])
        self.assertEqual(Fixture.requests[-1][2]["q"], '{chq_fingerprint="100", service_name="svc00", scenario="services"}')
        self.assertEqual(rows["returned"], 1)

    def test_long_windows_bound_their_buckets(self):
        # windows-smoke.ps1 checks Get-CountStep against the same steps.
        module = load_client()
        self.assertEqual([module.count_step(days * 86400) for days in (8, 120, 121, 364, 3650)],
                         [86400, 86400, 172800, 345600, 2678400])
        self.assertIsInstance(module.count_step(364 * 86400), int)

    def test_budget_holds_when_identifiers_alone_are_too_long(self):
        # Service names and fingerprints are never shortened, so only the last resort fits.
        module = load_client()
        rows = [{"timestamp": 1, "tags": {"service_name": "s" * 20000, "message": "m" * 7000}} for _ in range(3)]
        text = module.fit_output({"mode": "raw", "returned": 3, "logs": rows}, full_message=True)
        result = json.loads(text)
        self.assertLessEqual(len(text), module.OUTPUT_LIMIT)
        self.assertEqual((result["logs"], result["omitted"]["logs"], result["returned"]), ([], 3, 3))

    def test_serializer_matches_the_powershell_client(self):
        # windows-smoke.ps1 checks the PowerShell serializer against the same text.
        module = load_client()
        bits = (0x3FE9999999999999, 0x3FD3333333333334, 1, 0x8000000000000000, 0x3E7AD7F29ABCAF48, 0x4415AF1D78B58C40,
                0x3E70000000000000, 0x43B0000000000000)
        floats = [struct.unpack("<d", struct.pack("<Q", value))[0] for value in bits]
        self.assertEqual(floats, [0.7999999999999999, 0.30000000000000004, 5e-324, -0.0, 1e-7, 1e20,
                                  2.0**-24, 2.0**60])
        values = floats + [2**53 + 1, 2**64, 10**29, float("nan"), float("inf"), float("-inf"), "\x7f\u2028\x00"]
        self.assertEqual(module.dumps(values), '[0.7999999999999999,0.30000000000000004,5e-324,-0.0,1e-07,1e+20,'
                         '5.960464477539063e-08,1.152921504606847e+18,'
                         '9007199254740993,18446744073709551616,100000000000000000000000000000,NaN,Infinity,'
                         '-Infinity,"\\u007f\\u2028\\u0000"]')
        # Selectors are sent and echoed as the PowerShell client writes them.
        self.assertEqual(module.logql_string("o'k<>&\x01\x7f"), '"o\'k<>&\\u0001\x7f"')
        self.run_skill("logs", *WINDOW, "--service", "exact", "--all-fields")
        for text in EXACT_TEXT:
            self.assertIn(text, self.stdout)

    def test_budget_is_not_applied_to_small_output(self):
        for args in (["logs", "--service", "shop"], ["values", "--tag", "long"], ["tags"]):
            result = self.run_skill(args[0], *WINDOW, *args[1:])
            for key in ("budget_applied", "omitted", "shortened_strings"):
                self.assertNotIn(key, result)

    def test_output_is_canonical_ascii_json(self):
        # Compact, keys sorted by UTF-16 code unit (so U+1F600 sorts before U+FFFF), non-ASCII
        # escaped: the text whose length the budget counts.
        module = load_client()
        self.assertEqual(module.dumps({"b": "\u00e9" + EMOJI, "a": [1, None], "\uffff": 1, EMOJI: 2}),
                         '{"a":[1,null],"b":"\\u00e9\\ud83d\\ude00","\\ud83d\\ude00":2,"\\uffff":1}')
        self.run_skill("logs", *WINDOW, "--service", "shop")
        text = self.stdout.rstrip("\n")
        self.assertEqual(text, module.dumps(json.loads(text)))

    def test_issues_cap_service_counts_and_truncate_samples(self):
        issues = self.run_skill("issues", *WINDOW, "--label", "scenario=services")
        self.assertEqual(issues["total"], 79)
        self.assertEqual([row["service"] for row in issues["service_counts"]], [f"svc{index:02d}" for index in range(10)])
        self.assertEqual(issues["service_counts"][0], {"service": "svc00", "warn": 1, "error": 12, "fatal": 0, "total": 13})
        self.assertEqual(issues["service_counts_other"], {"services": 2, "warn": 0, "error": 3, "fatal": 0, "total": 3})
        self.assertEqual(sum(row["total"] for row in issues["service_counts"]) + issues["service_counts_other"]["total"],
                         issues["total"])
        sample = issues["patterns"][0]["sample"]
        self.assertEqual((sample["message"], sample["message_chars"]), ("x" * 300 + "\u2026", 500))
        self.assertEqual(issues["next"][0]["run"], PREFIX + " --service svc00 --level error --label scenario=services"
                         " --fingerprint 100 --raw")
        # Ties rank by service, then severity and fingerprint.
        self.assertEqual([(p["service"], p["severity"]) for p in issues["patterns"][-2:]],
                         [("svc00", "warn"), ("svc11", "error")])

        self.assertEqual((issues["unlisted_patterns"], issues["unlisted_events"]), (0, 0))
        issues = self.run_skill("issues", *WINDOW, "--label", "scenario=services", "--limit", "10", "--samples", "0")
        self.assertEqual((len(issues["patterns"]), issues["unlisted_patterns"], issues["unlisted_events"]), (10, 3, 4))

        issues = self.run_skill("issues", *WINDOW, "--service", "checkout", "--samples", "0")
        self.assertNotIn("service_counts_other", issues)

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

    def test_values_under_the_limit_in_code_points_are_not_shortened(self):
        # 400 UTF-16 code units but 200 code points: under the 256 code point limit.
        values = self.run_skill("values", *WINDOW, "--tag", "emoji")
        self.assertEqual(values["values"], [EMOJI * 200])
        self.assertNotIn("shortened_values", values)

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
        logs = self.run_skill("logs", *WINDOW, "--service", "flaky", "--raw")
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
        logs = self.run_skill("logs", *WINDOW, "--service", "busy", "--raw")
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

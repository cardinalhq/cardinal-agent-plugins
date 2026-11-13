#!/usr/bin/env python3
"""Read Cardinal logs and issue patterns directly from the Data Lake Query API."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import http.client
import json
import math
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


LOG_PATH = "/api/v1/logs/query"
LEVELS = {"fatal": "fatal", "error": "error", "warn": "warn|warning"}
LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DURATION = re.compile(r"([1-9][0-9]*)([smhdw])")
DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
UTC = timezone.utc


class QueryError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def query_endpoint(url):
    parts = urlsplit(url)
    if (
        parts.scheme not in ("https", "http")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or "?" in url
        or "#" in url
    ):
        raise QueryError("Set CARDINAL_QUERY_URL to the Data Lake Query API URL.")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise QueryError("Use HTTPS for remote Query API URLs.")
    url = url.rstrip("/")
    if url.endswith(LOG_PATH):
        return url
    return url.removesuffix("/api/v1") + LOG_PATH


class QueryAPI:
    def __init__(self, url, key, timeout):
        self.url = query_endpoint(url)
        self.key = key
        self.timeout = timeout

    def logs(self, body, query_ids):
        request_id = str(uuid4())
        request = Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "X-CardinalHQ-API-Key": self.key,
                "X-CardinalHQ-Query-Id": request_id,
            },
            method="POST",
        )
        deadline = time.monotonic() + self.timeout
        try:
            with build_opener(NoRedirects()).open(request, timeout=self.timeout) as response:
                request_id = response.headers.get("X-CardinalHQ-Query-Id", request_id)
                query_ids.append(request_id)
                if response.headers.get_content_type() != "text/event-stream":
                    raise QueryError("Expected a Query API event stream. Check CARDINAL_QUERY_URL.")
                for line in response:
                    if time.monotonic() > deadline:
                        raise QueryError("Query timed out. Try a shorter time range.")
                    if not line.startswith(b"data:"):
                        continue
                    event = json.loads(line[5:])
                    if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                        raise QueryError("Invalid Query API event.")
                    event_type, data = event.get("type"), event["data"]
                    if event_type == "result":
                        yield data
                    elif event_type == "error":
                        raise QueryError(self.redact(data.get("message", "Query execution failed.")))
                    elif event_type == "done":
                        if data.get("status") == "ok":
                            return
                        raise QueryError("Query API reported a failure. Results may be incomplete.")
                raise QueryError("Query API stream ended before completion.")
        except HTTPError as error:
            with error:
                request_id = error.headers.get("X-CardinalHQ-Query-Id", request_id)
                message = f"Query API returned HTTP {error.code}."
                if error.code in (401, 403):
                    message += " Check that the key has query access to this Data Lake."
                elif error.code == 404 or 300 <= error.code < 400:
                    message += " Check CARDINAL_QUERY_URL for the direct Query API endpoint."
                else:
                    try:
                        details = json.loads(error.read(8192))
                        if isinstance(details, dict) and isinstance(details.get("message"), str):
                            message += " " + self.redact(details["message"])
                    except (ValueError, OSError, http.client.HTTPException):
                        pass
            raise QueryError(f"{message} Query ID: {request_id}") from None
        except (ValueError, OSError, http.client.HTTPException, QueryError) as error:
            raise QueryError(f"{self.redact(error)} Query ID: {request_id}") from None

    def redact(self, value):
        return str(value).replace(self.key, "[redacted]")


def timestamp(value):
    if re.fullmatch(r"[0-9]{10}(?:[0-9]{3})?", value):
        return datetime.fromtimestamp(int(value) / (1000 if len(value) == 13 else 1), UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Use RFC 3339 timestamps with a timezone or Unix seconds/milliseconds.")
    return parsed.astimezone(UTC)


def time_range(args):
    end = timestamp(args.end) if args.end else datetime.now(UTC)
    if args.start:
        start = timestamp(args.start)
    else:
        duration = DURATION.fullmatch(args.since or "1h")
        if not duration:
            raise ValueError("Use a duration such as 30m, 2h, or 1d for --since.")
        amount, unit = duration.groups()
        start = end - timedelta(seconds=int(amount) * DURATION_SECONDS[unit])
    start = start.replace(microsecond=start.microsecond // 1000 * 1000)
    end = end.replace(microsecond=end.microsecond // 1000 * 1000)
    if start >= end:
        raise ValueError("The start time must be earlier than the end time.")
    return start, end


def iso(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def logql_string(value):
    return json.dumps(value, ensure_ascii=False)


def literal_regex(value):
    return re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", value)


def stream_selector(args, levels=None, fingerprint=None):
    matchers = [
        f"service_name={logql_string(args.service)}" if args.service else 'service_name=~".+"'
    ]
    if levels:
        matchers.append(f'level=~"(?i)^(?:{levels})$"')
    matchers.extend(f"{key}={logql_string(value)}" for key, value in args.label)
    if fingerprint is not None:
        matchers.append(f"chq_fingerprint={logql_string(fingerprint)}")
    return "{" + ", ".join(matchers) + "}"


def line_filters(args, ignore_case=False):
    # Keep literal substring semantics across Query API token-search modes.
    flags = "is" if ignore_case else "s"
    contains = [f"(?{flags}:.*{literal_regex(value)}.*)" for value in args.contains]
    excludes = [f"(?{flags}:.*{literal_regex(value)}.*)" for value in args.not_contains]
    return "".join(
        f" {operator} {logql_string(value)}"
        for operator, values in (("|~", contains + args.grep), ("!~", excludes + args.not_grep))
        for value in values
    )


def fetch_logs(client, payload, selector, limit, fields=None):
    body = {**payload, "q": selector, "limit": limit, "reverse": True}
    if fields:
        body["fields"] = fields
    query_ids = []
    records = list(client.logs(body, query_ids))
    return records, query_ids


def raw_logs(client, args, payload):
    if args.query and (args.service or args.level or args.label):
        raise ValueError("Put service, level and label matchers in --query when using a raw selector.")
    if args.query and not args.query.lstrip().startswith("{"):
        raise ValueError("--query expects a raw LogQL stream selector beginning with '{'.")
    selector = (args.query or stream_selector(args, args.level)) + line_filters(args)
    rows, query_ids = fetch_logs(client, payload, selector, args.limit, args.field)
    return {
        "query": selector,
        "query_ids": query_ids,
        "limit": args.limit,
        "returned": len(rows),
        "limit_reached": len(rows) >= args.limit,
        "logs": rows,
    }


def issue_step(seconds):
    # Conductor Explore uses these same distribution steps to bound the
    # number of buckets. The count window and step must be the same length.
    for max_minutes, step in ((30, 60), (120, 300), (480, 1200), (1440, 3600),
                              (4320, 7200), (10080, 21600)):
        if seconds <= max_minutes * 60:
            return step
    return 86400


def issue_query(selector, step):
    return f"sum by (service_name, level, chq_fingerprint)(count_over_time({selector}[{step}s]))"


def issue_points(client, payload, selector, filters, step, query_ids):
    query = issue_query(selector + filters, step)
    points = client.logs({**payload, "q": query, "step": step}, query_ids)
    for point in points:
        yield point
        yield from points
        return
    if not filters:
        # An installation can have raw logs for a window with no fingerprint
        # rollups. A match-all message predicate makes the API scan raw logs.
        query = issue_query(selector + ' |~ "(?s:.*)"', step)
        yield from client.logs({**payload, "q": query, "step": step}, query_ids)


def issue_patterns(client, args, payload, start, end):
    levels = LEVELS[args.severity] if args.severity else "|".join(LEVELS.values())
    step = issue_step((end - start).total_seconds())
    filters = line_filters(args, ignore_case=True)
    selector = stream_selector(args, levels, args.fingerprint)
    query_ids = []
    totals = defaultdict(int)
    services = defaultdict(lambda: dict.fromkeys(LEVELS, 0))
    trends = defaultdict(lambda: dict.fromkeys(LEVELS, 0))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step_ms = step * 1000
    if start_ms // step_ms == end_ms // step_ms:
        raise QueryError("The Issues window has no completed bucket. Use a wider window or search raw logs.")

    for point in issue_points(client, payload, selector, filters, step, query_ids):
        bucket_end = point["timestamp"]
        # The Query API stamps a count_over_time point at the end of its bucket.
        if not start_ms < bucket_end <= end_ms:
            continue
        tags = point["tags"]
        service = tags.get("service_name")
        severity = str(tags.get("level", "")).lower()
        if severity == "warning":
            severity = "warn"
        if not service or severity not in LEVELS:
            continue
        count = point.get("value")
        if not isinstance(count, (int, float)) or not math.isfinite(count) or count <= 0:
            continue
        count = round(count)
        fingerprint = tags.get("chq_fingerprint")
        if fingerprint not in (None, "", 0, "0"):
            totals[(service, severity, str(fingerprint))] += count
        services[service][severity] += count
        trends[bucket_end][severity] += count

    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    patterns = [
        {"service": service, "severity": severity, "fingerprint": fingerprint, "count": count}
        for (service, severity, fingerprint), count in ranked[:args.limit]
    ]

    def sample(pattern):
        pattern_args = argparse.Namespace(service=pattern["service"], label=args.label)
        selector = stream_selector(
            pattern_args, LEVELS[pattern["severity"]], pattern["fingerprint"]
        ) + filters
        return fetch_logs(
            client, payload, selector, 1,
            ["message", "service_name", "level", "chq_fingerprint"],
        )

    warnings = []
    if args.samples:
        with ThreadPoolExecutor(max_workers=4) as pool:
            samples = patterns[:args.samples]
            for pattern, future in zip(samples, [pool.submit(sample, row) for row in samples]):
                try:
                    rows, sample_ids = future.result()
                    query_ids.extend(sample_ids)
                    if rows:
                        row = rows[0]
                        pattern["sample"] = {
                            "message": row["tags"].get("message"),
                            "timestamp": row["timestamp"],
                        }
                except QueryError as error:
                    warnings.append(f"Could not load sample for {pattern['fingerprint']}: {error}")

    service_counts = [
        {"service": service, **counts, "total": sum(counts.values())}
        for service, counts in services.items()
    ]
    return {
        "query_ids": query_ids,
        "step_seconds": step,
        "total": sum(row["total"] for row in service_counts),
        "service_counts": sorted(service_counts, key=lambda row: (-row["total"], row["service"])),
        "trend": [{"timestamp": bucket, **trends[bucket]} for bucket in sorted(trends)],
        "matched_patterns": len(ranked),
        "patterns": patterns,
        "warnings": warnings,
    }


def label_filter(value):
    key, separator, label_value = value.partition("=")
    if not separator or not LABEL.fullmatch(key):
        raise argparse.ArgumentTypeError("Expected LABEL=VALUE with a valid LogQL label name.")
    return key, label_value


def fingerprint_id(value):
    if not re.fullmatch(r"-?[0-9]+", value) or not -(2**63) <= int(value) < 2**63:
        raise argparse.ArgumentTypeError("Expected a signed 64-bit fingerprint.")
    return str(int(value))


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive integer.")
    return number


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    logs = commands.add_parser("logs", help="Search raw logs, newest first.")
    issues = commands.add_parser("issues", help="Rank warning, error and fatal patterns.")
    for command in (logs, issues):
        window = command.add_mutually_exclusive_group()
        window.add_argument("--since", help="Duration ending at --end (default: 1h).")
        window.add_argument("--start", help="RFC 3339 timestamp or Unix seconds/milliseconds.")
        command.add_argument("--end", help="RFC 3339 or Unix timestamp (default: now).")
        command.add_argument("--service", help="Exact service_name.")
        command.add_argument("--label", type=label_filter, action="append", default=[])
        command.add_argument("--contains", action="append", default=[], help="Literal message filter.")
        command.add_argument("--not-contains", action="append", default=[])
        command.add_argument("--grep", action="append", default=[], help="RE2 message regex.")
        command.add_argument("--not-grep", action="append", default=[])
        command.add_argument("--limit", type=positive_int, default=20)
        command.add_argument("--timeout", type=positive_int, default=90, help="Seconds per request.")
    logs.add_argument("--query", help="Raw LogQL stream selector and optional line filters.")
    logs.add_argument("--field", action="append", help="Return only the specified log tag. Repeatable.")
    logs.add_argument("--level", choices=LEVELS, help="Filter by normalized severity.")
    issues.add_argument("--severity", choices=LEVELS)
    issues.add_argument("--fingerprint", type=fingerprint_id)
    issues.add_argument("--samples", type=int, default=5, help="Load exemplars for the first N patterns (0–20).")
    args = parser.parse_args()
    if args.command == "issues" and not 0 <= args.samples <= 20:
        issues.error("--samples must be between 0 and 20.")
    return args


def main():
    args = parse_args()
    try:
        url = os.environ.get("CARDINAL_QUERY_URL", "").strip()
        key = os.environ.get("CARDINAL_QUERY_API_KEY", "").strip()
        if not url or not key:
            raise QueryError("Set CARDINAL_QUERY_URL and CARDINAL_QUERY_API_KEY in the agent environment.")
        if "\r" in key or "\n" in key:
            raise QueryError("CARDINAL_QUERY_API_KEY must be a single line.")
        start, end = time_range(args)
        client = QueryAPI(url, key, args.timeout)
        payload = {"s": iso(start), "e": iso(end)}
        if args.command == "logs":
            args.level = LEVELS.get(args.level)
            result = raw_logs(client, args, payload)
        else:
            result = issue_patterns(client, args, payload, start, end)
        print(json.dumps({"start": payload["s"], "end": payload["e"], **result}, ensure_ascii=True))
    except (QueryError, ValueError, OverflowError, KeyError, TypeError) as error:
        print(f"cardinal-query: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

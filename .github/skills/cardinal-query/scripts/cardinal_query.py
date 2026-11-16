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
import random
import re
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


API_PATH = "/api/v1"
LOG_PATH = API_PATH + "/logs/query"
TAGS_PATH = API_PATH + "/logs/tags"
TAG_VALUES_PATH = API_PATH + "/logs/tagvalues"
LEVELS = {"fatal": "fatal", "error": "error", "warn": "warn|warning"}
# The Query API stores dotted OpenTelemetry names with underscores.
LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
CORE_FIELDS = ("service_name", "level", "message", "chq_fingerprint")
MAX_TAG_VALUES = 1000
MAX_VALUE_CHARS = 256
DURATION = re.compile(r"([1-9][0-9]*)([smhdw])")
DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
UTC = timezone.utc
TIMED_OUT = "Query timed out. Try a shorter time range."
# Transient failures: rate limiting and gateway errors. HTTP 500 is a query error.
RETRY_STATUSES = (429, 502, 503, 504)
MAX_RETRIES = 5
MAX_RETRY_WAIT = 8.0


class QueryError(Exception):
    """A Query API failure. `retry` names a transient cause that may be retried."""

    def __init__(self, message, retry=None, retry_after=None, query_id=None):
        super().__init__(message)
        self.retry = retry
        self.retry_after = retry_after
        self.query_id = query_id


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_base(url):
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
    url = url.rstrip("/").removesuffix(LOG_PATH).removesuffix(API_PATH)
    return url + API_PATH


def retry_base():
    # Undocumented test hook. The default first backoff is 0.5 seconds.
    value = os.environ.get("CARDINAL_QUERY_RETRY_BASE_MS", "").strip()
    if re.fullmatch(r"[0-9]{1,9}", value) and int(value) > 0:
        return int(value) / 1000
    return 0.5


def retry_after(value):
    # Honor the delay-seconds form only; an HTTP-date falls back to backoff.
    value = (value or "").strip()
    return int(value) if re.fullmatch(r"[0-9]{1,9}", value) else None


def sort_key(value):
    # Order strings by UTF-16 code unit, as the PowerShell client's ordinal sort does.
    return value.encode("utf-16-be", "surrogatepass")


def tag_value(data):
    value = data.get("value")
    if not isinstance(value, str):
        raise QueryError("Invalid Query API tag value.")
    return value


class QueryAPI:
    def __init__(self, url, key, timeout, retries=0):
        self.base = api_base(url)
        self.key = key
        self.timeout = timeout
        self.retries = retries
        self.backoff = retry_base()

    def send(self, path, body, accept, query, deadline):
        """Make one attempt. Raise QueryError with `retry` set for a transient failure."""
        request_id = str(uuid4())
        request = Request(
            self.base + path.removeprefix(API_PATH) + query,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Accept": accept,
                "Content-Type": "application/json",
                "X-CardinalHQ-API-Key": self.key,
                "X-CardinalHQ-Query-Id": request_id,
            },
            method="POST",
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QueryError(TIMED_OUT, query_id=request_id)
        try:
            response = build_opener(NoRedirects()).open(request, timeout=remaining)
        except HTTPError as error:
            with error:
                request_id = error.headers.get("X-CardinalHQ-Query-Id", request_id)
                message = f"Query API returned HTTP {error.code}."
                retry = wait = None
                if error.code in (401, 403):
                    message += " Check that the key has query access to this Data Lake."
                elif error.code == 404 or 300 <= error.code < 400:
                    message += " Check CARDINAL_QUERY_URL for the direct Query API endpoint."
                else:
                    message += self.error_details(error)
                if error.code in RETRY_STATUSES:
                    retry = f"HTTP {error.code}"
                    if error.code in (429, 503):
                        wait = retry_after(error.headers.get("Retry-After"))
            raise QueryError(message, retry, wait, request_id) from None
        except (ValueError, OSError, http.client.HTTPException) as error:
            raise self.failure(error, request_id) from None
        return response, response.headers.get("X-CardinalHQ-Query-Id", request_id)

    def failure(self, error, request_id):
        """Classify a transport exception as a QueryError."""
        if isinstance(error, QueryError):
            if error.query_id is None:
                error.query_id = request_id
            return error
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, socket.timeout):
            return QueryError(TIMED_OUT, "a timeout", query_id=request_id)
        if isinstance(reason, (OSError, http.client.HTTPException)):
            detail = str(reason) or type(reason).__name__
            return QueryError(f"Query API connection failed: {detail}", "a connection error", query_id=request_id)
        return QueryError(str(error), query_id=request_id)

    def retry_or_raise(self, error, attempt, deadline, retryable=True):
        """Wait before the next attempt, or raise the final error."""
        attempts = self.retries + 1
        if retryable and error.retry and attempt < attempts:
            if error.retry_after is not None:
                wait = min(float(error.retry_after), MAX_RETRY_WAIT)
            else:
                wait = random.uniform(0, min(MAX_RETRY_WAIT, self.backoff * 2 ** (attempt - 1)))
            if wait < deadline - time.monotonic():
                print(f"cardinal-query: retrying after {error.retry} (attempt {attempt + 1} of {attempts})",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
                return
        message = self.redact(error)
        if attempt > 1:
            message += f" Failed after {attempt} attempts."
        raise QueryError(f"{message} Query ID: {error.query_id}") from None

    def error_details(self, error):
        # Query endpoints return JSON errors; tag endpoints return plain text.
        try:
            raw = error.read(4096).decode("utf-8", "replace").strip()
        except (OSError, http.client.HTTPException):
            return ""
        try:
            details = json.loads(raw)
            if isinstance(details, dict) and isinstance(details.get("message"), str):
                raw = details["message"]
        except ValueError:
            pass
        raw = " ".join(raw.split())[:500]
        return " " + self.redact(raw) if raw else ""

    def stream(self, path, body, query_ids, query="", row=None):
        deadline = time.monotonic() + self.timeout
        attempt = 1
        while True:
            request_id, yielded = None, False
            try:
                response, request_id = self.send(path, body, "text/event-stream", query, deadline)
                with response:
                    if response.headers.get_content_type() != "text/event-stream":
                        raise QueryError("Expected a Query API event stream. Check CARDINAL_QUERY_URL.")
                    for line in response:
                        if time.monotonic() > deadline:
                            raise QueryError(TIMED_OUT)
                        if not line.startswith(b"data:"):
                            continue
                        event = json.loads(line[5:])
                        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                            raise QueryError("Invalid Query API event.")
                        event_type, data = event.get("type"), event["data"]
                        if event_type == "result":
                            data = row(data) if row else data
                            if not yielded:
                                # Never retry once the caller has a row.
                                yielded = True
                                query_ids.append(request_id)
                            yield data
                        elif event_type == "error":
                            message = data.get("message")
                            if not isinstance(message, str) or not message.strip():
                                message = "Query execution failed."
                            raise QueryError(message)
                        elif event_type == "done":
                            if data.get("status") == "ok":
                                if not yielded:
                                    query_ids.append(request_id)
                                return
                            raise QueryError("Query API reported a failure. Results may be incomplete.")
                    raise QueryError("Query API stream ended before completion.", "an interrupted stream")
            except (ValueError, OSError, http.client.HTTPException, QueryError) as error:
                failure = self.failure(error, request_id)
            self.retry_or_raise(failure, attempt, deadline, retryable=not yielded)
            attempt += 1

    def logs(self, body, query_ids):
        return self.stream(LOG_PATH, body, query_ids)

    def tag_values(self, tag, body, query_ids):
        return self.stream(TAG_VALUES_PATH, body, query_ids, "?" + urlencode({"tagName": tag}), tag_value)

    def tags(self, body, query_ids):
        deadline = time.monotonic() + self.timeout
        attempt = 1
        while True:
            request_id = None
            try:
                response, request_id = self.send(TAGS_PATH, body, "application/json", "", deadline)
                with response:
                    if response.headers.get_content_type() != "application/json":
                        raise QueryError("Expected a JSON tag list. Check CARDINAL_QUERY_URL.")
                    result = json.load(response)
                    if not isinstance(result, dict) or "tags" not in result:
                        raise QueryError("Invalid Query API tag list.")
                    # The server encodes an empty rollup as null.
                    tags = [] if result["tags"] is None else result["tags"]
                    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                        raise QueryError("Invalid Query API tag list.")
                    query_ids.append(request_id)
                    return tags
            except (ValueError, OSError, http.client.HTTPException, QueryError) as error:
                failure = self.failure(error, request_id)
            self.retry_or_raise(failure, attempt, deadline)
            attempt += 1

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


def stream_selector(args, levels=None, fingerprint=None, any_service=True):
    matchers = []
    if args.service:
        matchers.append(f"service_name={logql_string(args.service)}")
    elif any_service:
        matchers.append('service_name=~".+"')
    if levels:
        matchers.append(f'level=~"(?i)^(?:{levels})$"')
    matchers.extend(f"{key}={logql_string(value)}" for key, value in args.label)
    if fingerprint is not None:
        matchers.append(f"chq_fingerprint={logql_string(fingerprint)}")
    return "{" + ", ".join(matchers) + "}" if matchers else ""


def discovery_scope(args):
    """Return the selector that scopes tag discovery, or "" for the whole org."""
    if args.query:
        if args.service or args.level or args.label:
            raise ValueError("Put service, level and label matchers in --query when using a raw selector.")
        selector = args.query.strip()
        if not selector.startswith("{") or not selector.endswith("}"):
            raise ValueError("--query for tags and values expects a LogQL stream selector such as {service_name=\"checkout\"}.")
        return selector
    return stream_selector(args, LEVELS.get(args.level), any_service=False)


def list_tags(client, args, payload):
    scope = discovery_scope(args)
    query_ids = []
    body = dict(payload, q=scope) if scope else dict(payload)
    names = sorted(set(client.tags(body, query_ids)), key=sort_key)
    listed = set(names)
    if args.match:
        needle = args.match.lower()
        names = [name for name in names if needle in name.lower()]
    result = {
        "scope": scope or None,
        "source": "matching_rows" if scope else "org_daily_rollup",
        "query_ids": query_ids,
        "matched": len(names),
        "returned": min(len(names), args.limit),
        "truncated": len(names) > args.limit,
        "tags": names[:args.limit],
        "core_fields_not_listed": [name for name in CORE_FIELDS if name not in listed],
    }
    if not scope:
        result["note"] = ("Unscoped names come from a daily rollup for the whole organization and may include "
                          "fields with no logs in this window. Scope with --service or --label for names on "
                          "matching rows.")
    return result


def shorten(value):
    """Shorten a long value for display, counting Unicode code points."""
    return value[:MAX_VALUE_CHARS] + "\u2026" if len(value) > MAX_VALUE_CHARS else value


def list_tag_values(client, args, payload):
    tag = args.tag.replace(".", "_")
    if tag == "message":
        raise ValueError("Use logs or issues to read messages; values lists label values.")
    if len(args.match.encode("utf-8")) > 256:
        raise ValueError("--match must be at most 256 bytes.")
    scope = discovery_scope(args)
    body = dict(payload, limit=args.limit)
    if scope:
        body["q"] = scope
    if args.match:
        body["contains"] = args.match
    query_ids = []
    # De-duplicate full values; shorten only for display, so counts stay exact.
    distinct = set()
    for value in client.tag_values(tag, body, query_ids):
        distinct.add(value)
        if len(distinct) >= args.limit:
            break
    values = [shorten(value) for value in sorted(distinct, key=sort_key)]
    shortened = sum(1 for value in distinct if len(value) > MAX_VALUE_CHARS)
    # The server answers an unscoped request without --match from a daily
    # org-wide rollup when it tracks the label, and scans rows otherwise.
    rollup = not scope and not args.match
    result = {
        "tag": tag,
        "scope": scope or None,
        "source": "org_daily_rollup" if rollup else "matching_rows",
        "query_ids": query_ids,
        "limit": args.limit,
        "returned": len(distinct),
        "limit_reached": len(distinct) >= args.limit,
        "values": values,
    }
    if shortened:
        result["shortened_values"] = shortened
    if rollup:
        result["note"] = ("Unscoped values usually come from a daily rollup for the whole organization and may "
                          "include values with no logs in this window. Scope with --service or --label for values "
                          "on matching rows.")
    return result


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
    return key.replace(".", "_"), label_value


def tag_name(value):
    if not LABEL.fullmatch(value):
        raise argparse.ArgumentTypeError("Expected a label name such as service_name or k8s_namespace_name.")
    return value


def fingerprint_id(value):
    if not re.fullmatch(r"-?[0-9]+", value) or not -(2**63) <= int(value) < 2**63:
        raise argparse.ArgumentTypeError("Expected a signed 64-bit fingerprint.")
    return str(int(value))


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive integer.")
    return number


def retry_count(value):
    number = int(value)
    if not 0 <= number <= MAX_RETRIES:
        raise argparse.ArgumentTypeError(f"Expected 0 to {MAX_RETRIES}.")
    return number


def tag_value_limit(value):
    number = positive_int(value)
    if number > MAX_TAG_VALUES:
        raise argparse.ArgumentTypeError(f"Expected at most {MAX_TAG_VALUES}.")
    return number


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    logs = commands.add_parser("logs", help="Search raw logs, newest first.")
    issues = commands.add_parser("issues", help="Rank warning, error and fatal patterns.")
    tags = commands.add_parser("tags", help="Discover log label names to filter, group or project by.")
    values = commands.add_parser("values", help="List the values one log label takes.")
    for command in (logs, issues, tags, values):
        window = command.add_mutually_exclusive_group()
        window.add_argument("--since", help="Duration ending at --end (default: 1h).")
        window.add_argument("--start", help="RFC 3339 timestamp or Unix seconds/milliseconds.")
        command.add_argument("--end", help="RFC 3339 or Unix timestamp (default: now).")
        command.add_argument("--service", help="Exact service_name.")
        command.add_argument("--label", type=label_filter, action="append", default=[],
                             help="Exact LABEL=VALUE match. Repeatable.")
        command.add_argument("--timeout", type=positive_int, default=90,
                             help="Seconds per request, including retries.")
        command.add_argument("--retries", type=retry_count, default=2,
                             help=f"Retries for transient failures (0-{MAX_RETRIES}, default: 2).")
    for command in (tags, values):
        command.add_argument("--level", choices=LEVELS, help="Scope to a normalized severity.")
        command.add_argument("--query", help="Raw LogQL stream selector that scopes discovery.")
    tags.add_argument("--match", help="Case-insensitive substring of the label name.")
    tags.add_argument("--limit", type=positive_int, default=200)
    values.add_argument("--tag", type=tag_name, required=True, help="Label name, e.g. k8s_namespace_name.")
    values.add_argument("--match", default="", help="Case-sensitive substring of the value (server-side).")
    values.add_argument("--limit", type=tag_value_limit, default=50, help=f"Maximum values (1-{MAX_TAG_VALUES}).")
    for command in (logs, issues):
        command.add_argument("--contains", action="append", default=[], help="Literal message filter.")
        command.add_argument("--not-contains", action="append", default=[])
        command.add_argument("--grep", action="append", default=[], help="RE2 message regex.")
        command.add_argument("--not-grep", action="append", default=[])
        command.add_argument("--limit", type=positive_int, default=20)
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
        client = QueryAPI(url, key, args.timeout, args.retries)
        payload = {"s": iso(start), "e": iso(end)}
        if args.command == "logs":
            args.level = LEVELS.get(args.level)
            result = raw_logs(client, args, payload)
        elif args.command == "tags":
            result = list_tags(client, args, payload)
        elif args.command == "values":
            result = list_tag_values(client, args, payload)
        else:
            result = issue_patterns(client, args, payload, start, end)
        print(json.dumps({"start": payload["s"], "end": payload["e"], **result}, ensure_ascii=True))
    except (QueryError, ValueError, OverflowError, KeyError, TypeError) as error:
        print(f"cardinal-query: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

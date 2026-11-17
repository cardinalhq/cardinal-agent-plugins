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
import shlex
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4


API_PATH = "/api/v1"
LOG_PATH = API_PATH + "/logs/query"
TAGS_PATH = API_PATH + "/logs/tags"
TAG_VALUES_PATH = API_PATH + "/logs/tagvalues"
LEVELS = {"fatal": "fatal", "error": "error", "warn": "warn|warning"}
# The Query API stores dotted OpenTelemetry names with underscores.
LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
CORE_FIELDS = ("service_name", "level", "message", "chq_fingerprint")
# Fields returned for raw rows and exemplars unless --field or --all-fields says otherwise.
ROW_FIELDS = ["message", "service_name", "level", "chq_fingerprint"]
MAX_TAG_VALUES = 1000
MAX_VALUE_CHARS = 256
MESSAGE_CHARS = 300
# --full-message raises the per-message cap for raw rows.
FULL_MESSAGE_CHARS = 8000
MAX_FIELDS = 50
MAX_NAME_CHARS = 256
DEFAULT_ROWS = 20
DEFAULT_PATTERNS = 10
MAX_LOG_LIMIT = 200
# One newest-first fetch picks exemplars; patterns it misses get their own query.
EXEMPLARS = 10
EXEMPLAR_ROWS = 50
MAX_SERVICES = 10
# Long count windows use day buckets, at most this many.
MAX_BUCKETS = 120
# Output budget, in bytes of stdout: compact JSON with keys sorted by UTF-16 code unit
# and only ASCII, so every byte is one character in both clients, plus a newline (CRLF
# on Windows). The budget measures everything it prints. Only next[].run differs by
# platform, so each is measured as RUN_BYTES long, and a longer command is replaced by
# a short hint. Both clients therefore make the same decisions.
MAX_OUTPUT_BYTES = 16384
OUTPUT_LIMIT = MAX_OUTPUT_BYTES - 2
RUN_BYTES = 1024
RUN_PLACEHOLDER = "x" * (RUN_BYTES - 2)
# An echoed query or scope longer than this many bytes of JSON text is shortened.
ECHO_BYTES = 2048
ECHO_FIELDS = ("query", "scope")
# Budget counters are measured at this value until their real value is known.
COUNTER_RESERVE = 9999999999
BUDGET_CAPS = (200, 100)
BUDGET_LISTS = ("logs", "patterns", "values", "tags", "service_counts", "trend", "warnings")
BUDGET_KEEP = frozenset(("fingerprint", "chq_fingerprint", "service", "service_name", "severity", "level",
                         "first_seen", "last_seen", "timestamp", "timestamp_ns"))
BUDGET_FAILED = "The result does not fit the output budget. Narrow the filters or the window."
DURATION = re.compile(r"([1-9][0-9]*)([smhdw])")
DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
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


def query_id(value, default):
    # Report a server query ID only when it is a UUID, as the PowerShell client does.
    try:
        return str(UUID(value))
    except (TypeError, ValueError):
        return default


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
                request_id = query_id(error.headers.get("X-CardinalHQ-Query-Id"), request_id)
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
        return response, query_id(response.headers.get("X-CardinalHQ-Query-Id"), request_id)

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


def current_time():
    # Undocumented test hook: a fixed current time.
    value = os.environ.get("CARDINAL_QUERY_NOW", "").strip()
    return timestamp(value) if value else datetime.now(UTC)


def time_range(args):
    end = timestamp(args.end) if args.end else current_time()
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


def iso_ms(value):
    return iso(EPOCH + timedelta(milliseconds=value))


def epoch_ms(value):
    return (value - EPOCH) // timedelta(milliseconds=1)


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
                          "fields with no logs in this window. Scope to a service, label or level for names on "
                          "matching rows.")
    return result


def shorten(value, limit=MAX_VALUE_CHARS):
    """Shorten a long value for display, counting Unicode code points."""
    return value[:limit] + "\u2026" if len(value) > limit else value


def row_tags(row):
    tags = row.get("tags")
    return tags if isinstance(tags, dict) else {}


def sample_of(row):
    """An exemplar row's message and time, with the message cut to MESSAGE_CHARS code points."""
    message = row_tags(row).get("message")
    sample = {"message": message, "timestamp": row.get("timestamp")}
    if isinstance(message, str) and len(message) > MESSAGE_CHARS:
        sample["message"] = shorten(message, MESSAGE_CHARS)
        sample["message_chars"] = len(message)
    return sample


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
                          "include values with no logs in this window. Scope to a service, label or level for "
                          "values on matching rows.")
    return result


def filter_patterns(args, ignore_case=False):
    """Return the include and exclude RE2 patterns for the message filters."""
    # Keep literal substring semantics across Query API token-search modes.
    flags = "is" if ignore_case else "s"
    contains = [f"(?{flags}:.*{literal_regex(value)}.*)" for value in args.contains]
    excludes = [f"(?{flags}:.*{literal_regex(value)}.*)" for value in args.not_contains]
    return contains + args.grep, excludes + args.not_grep


def line_filters(args, ignore_case=False):
    includes, excludes = filter_patterns(args, ignore_case)
    return "".join(
        f" {operator} {logql_string(value)}"
        for operator, values in (("|~", includes), ("!~", excludes))
        for value in values
    )


def fetch_logs(client, payload, selector, limit, fields=None):
    body = {**payload, "q": selector, "limit": limit, "reverse": True}
    if fields:
        body["fields"] = fields
    query_ids = []
    records = list(client.logs(body, query_ids))
    return records, query_ids


def sample_rows(client, payload, selectors, query_ids):
    """Fetch the newest row for each selector, four at a time. Return a row, None or a QueryError for each."""
    def one(selector):
        try:
            return fetch_logs(client, payload, selector, 1, ROW_FIELDS)
        except QueryError as error:
            return error

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for outcome in pool.map(one, selectors):
            if isinstance(outcome, QueryError):
                results.append(outcome)
            else:
                rows, ids = outcome
                query_ids.extend(ids)
                results.append(rows[0] if rows else None)
    return results


def command_line(command, payload, options):
    """The exact follow-up command for this platform, with the same absolute window."""
    words = ["python3", os.path.abspath(__file__), command, "--start", payload["s"], "--end", payload["e"]]
    for flag, value in options:
        words.append(flag)
        if value is not None:
            words.append(value)
    return " ".join(shlex.quote(word) for word in words)


# The stream selector at the start of a raw query, and an exact service_name matcher in it.
SELECTOR = re.compile(r'\s*\{(?:[^}"`]|"(?:[^"\\]|\\.)*"|`[^`]*`)*\}')
PINNED_SERVICE = re.compile(r'(?<![A-Za-z0-9_.])service_name\s*=(?![=~])')


def pins_service(query):
    """Whether a raw query's stream selector has an exact service_name matcher."""
    selector = SELECTOR.match(query)
    return bool(selector and PINNED_SERVICE.search(selector.group()))


def with_matchers(query, matchers):
    """Add matchers to the front of a raw stream selector."""
    if not matchers:
        return query
    rest = query.lstrip()[1:]
    separator = "" if rest.lstrip().startswith("}") else ", "
    return "{" + ", ".join(matchers) + separator + rest


def pattern_matchers(args, fingerprint=None, service=None):
    """Matchers that narrow a raw query to one pattern: its fingerprint, and its service
    unless the selector already pins one."""
    matchers = []
    if fingerprint:
        matchers.append(f"chq_fingerprint={logql_string(fingerprint)}")
    if service and not pins_service(args.query):
        matchers.append(f"service_name={logql_string(service)}")
    return matchers


def log_options(args, service=None, fingerprint=None, raw=False):
    """Options that repeat this logs search, optionally narrowed to one pattern."""
    options = []
    if args.query:
        options.append(("--query", with_matchers(args.query, pattern_matchers(args, service=service))))
    else:
        if service or args.service:
            options.append(("--service", service or args.service))
        if args.level_name:
            options.append(("--level", args.level_name))
        options.extend(("--label", f"{key}={value}") for key, value in args.label)
    for flag, values in (("--contains", args.contains), ("--not-contains", args.not_contains),
                         ("--grep", args.grep), ("--not-grep", args.not_grep)):
        options.extend((flag, value) for value in values)
    if fingerprint or args.fingerprint:
        options.append(("--fingerprint", fingerprint or args.fingerprint))
    if raw:
        options.append(("--raw", None))
    return options


def hint(why, *runs):
    """A follow-up with the first run whose JSON text fits in RUN_BYTES. The last run is a short text."""
    for run in runs:
        if len(dumps(run)) <= RUN_BYTES:
            break
    return {"why": why, "run": run}


def pattern_hint(args, payload, service, fingerprint):
    """The drill-down into one pattern's rows."""
    if args.query:
        extra = ""
        if service and not pins_service(args.query):
            extra = f", adding service_name={logql_string(service)} to --query"
        short = f"Run the same logs command with --fingerprint {fingerprint} --raw{extra}."
    else:
        extra = f" --service {shlex.quote(service)}" if service and service != args.service else ""
        short = f"Run the same logs command with{extra} --fingerprint {fingerprint} --raw."
    return hint("Rows for the top pattern, newest first.",
                command_line("logs", payload, log_options(args, service, fingerprint, raw=True)), short,
                "Run the same logs command with the top pattern's service and fingerprint, and --raw.")


def log_selector(args, fingerprint=None, service=None):
    if args.query:
        return with_matchers(args.query, pattern_matchers(args, fingerprint, service))
    scope = argparse.Namespace(service=service or args.service, label=args.label)
    return stream_selector(scope, args.level, fingerprint)


def count_step(seconds):
    # Conductor Explore uses these same distribution steps to bound the
    # number of buckets. The count window and step must be the same length.
    for max_minutes, step in ((30, 60), (120, 300), (480, 1200), (1440, 3600),
                              (4320, 7200), (10080, 21600)):
        if seconds <= max_minutes * 60:
            return step
    # Longer windows use whole days, at most MAX_BUCKETS of them, so trend stays bounded.
    return 86400 * max(1, math.ceil(seconds / (86400 * MAX_BUCKETS)))


def count_window(start, end, open_end, message):
    """Return the bucket step, the window bounds and the end to query, in milliseconds.

    The Query API stamps each bucket at its end and leaves out the bucket that straddles
    the query end. When the window ends now, query up to the next bucket boundary so the
    newest bucket is counted: nothing exists after now, so the counts stay exact."""
    step = count_step((end - start).total_seconds())
    step_ms = step * 1000
    start_ms = epoch_ms(start)
    end_ms = epoch_ms(end)
    query_end_ms = -(-end_ms // step_ms) * step_ms if open_end else end_ms
    if start_ms // step_ms == query_end_ms // step_ms:
        raise QueryError(message)
    return step, start_ms, end_ms, query_end_ms


def bucket_note(step, start_ms, end_ms, open_end):
    clauses = []
    if start_ms % (step * 1000):
        clauses.append("the bucket that straddles the start is counted in full")
    if open_end:
        clauses.append("the newest bucket runs to now")
    elif end_ms % (step * 1000):
        clauses.append("the bucket that straddles the end is left out")
    note = f"Counts are totals in fixed {step}-second buckets"
    if clauses:
        note += ": " + " and ".join(clauses)
    return note + ". first_seen and last_seen are bucket bounds. Use raw rows for exact boundaries."


def bucket_count(point, start_ms, end_ms):
    """Return a point's rounded count, or None outside the window or when invalid."""
    # The Query API stamps a count_over_time point at the end of its bucket.
    bucket = point.get("timestamp")
    if not isinstance(bucket, (int, float)) or not start_ms < bucket <= end_ms:
        return None
    count = point.get("value")
    if not isinstance(count, (int, float)) or not math.isfinite(count) or count <= 0:
        return None
    return round(count)


def unfingerprinted(fingerprint):
    return fingerprint in (None, "", 0, "0")


def count_points(client, payload, selector, filters, step, query_ids, labels):
    def query(pipeline):
        return f"sum by ({', '.join(labels)})(count_over_time({pipeline}[{step}s]))"

    points = client.logs({**payload, "q": query(selector + filters), "step": step}, query_ids)
    for point in points:
        yield point
        yield from points
        return
    if not filters:
        # An installation can have raw logs for a window with no fingerprint
        # rollups. A match-all message predicate makes the API scan raw logs.
        yield from client.logs({**payload, "q": query(selector + ' |~ "(?s:.*)"'), "step": step}, query_ids)


def log_limit(args):
    requested = args.limit or (DEFAULT_ROWS if args.raw else DEFAULT_PATTERNS)
    return min(requested, MAX_LOG_LIMIT), requested


def search_logs(client, args, payload, start, end, open_end):
    """Return the result and a follow-up to add if the output budget trims it."""
    if args.query and (args.service or args.level or args.label):
        raise ValueError("Put service, level and label matchers in --query when using a raw selector.")
    if args.query and not args.query.lstrip().startswith("{"):
        raise ValueError("--query expects a raw LogQL stream selector beginning with '{'.")
    if args.field and args.all_fields:
        raise ValueError("Use either --field or --all-fields.")
    if args.field and len(args.field) > MAX_FIELDS:
        raise ValueError(f"Use at most {MAX_FIELDS} --field options.")
    # Projection and full messages apply to rows.
    args.raw = args.raw or bool(args.field) or args.all_fields or args.full_message
    limit, requested = log_limit(args)
    filters = line_filters(args)
    selector = log_selector(args, args.fingerprint) + filters
    if args.raw:
        result, budget_hint = raw_logs(client, args, payload, selector, limit)
    else:
        result, budget_hint = log_patterns(client, args, payload, start, end, open_end, selector, filters, limit)
    if limit < requested:
        result["limit_clamped_from"] = requested
    return result, budget_hint


def raw_logs(client, args, payload, selector, limit):
    fields = None if args.all_fields else (args.field or list(ROW_FIELDS))
    rows, query_ids = fetch_logs(client, payload, selector, limit, fields)
    cap = FULL_MESSAGE_CHARS if args.full_message else MESSAGE_CHARS
    truncated = 0
    for row in rows:
        tags = row_tags(row)
        message = tags.get("message")
        if isinstance(message, str) and len(message) > cap:
            tags["message"] = shorten(message, cap)
            row["message_chars"] = len(message)
            truncated += 1
    result = {
        "mode": "raw",
        "query": selector,
        "query_ids": query_ids,
        "fields": fields,
        "limit": limit,
        "returned": len(rows),
        "limit_reached": len(rows) >= limit,
        "logs": rows,
    }
    if truncated:
        result["truncated_messages"] = truncated
    patterns = hint("Count every match in the window by pattern.",
                    command_line("logs", payload, log_options(args)),
                    "Run the same logs command without --raw, --field, --all-fields or --full-message.")
    if result["limit_reached"]:
        result["next"] = [patterns]
    return result, patterns


def log_patterns(client, args, payload, start, end, open_end, selector, filters, limit):
    step, start_ms, end_ms, query_end_ms = count_window(
        start, end, open_end, "The window has no completed bucket for pattern counts. Use a wider window or --raw.")
    query_ids = []
    counts = defaultdict(int)
    seen = {}
    total = missing = 0
    base = log_selector(args, args.fingerprint)
    for point in count_points(client, dict(payload, e=iso_ms(query_end_ms)), base, filters, step, query_ids,
                              ("service_name", "chq_fingerprint")):
        count = bucket_count(point, start_ms, query_end_ms)
        if count is None:
            continue
        total += count
        tags = row_tags(point)
        fingerprint = tags.get("chq_fingerprint")
        if unfingerprinted(fingerprint):
            missing += count
            continue
        key = (tags.get("service_name") or None, str(fingerprint))
        counts[key] += count
        first, last = seen.get(key, (point["timestamp"], point["timestamp"]))
        seen[key] = (min(first, point["timestamp"]), max(last, point["timestamp"]))

    ranked = sorted(counts.items(), key=lambda item: (-item[1], sort_key(item[0][0] or ""), sort_key(item[0][1])))
    patterns = []
    for (service, fingerprint), count in ranked[:limit]:
        first, last = seen[(service, fingerprint)]
        patterns.append({
            "fingerprint": fingerprint, "service": service, "count": count,
            "first_seen": iso_ms(first - step * 1000),
            # The newest bucket can end after now; the window ends at now.
            "last_seen": iso_ms(min(last, end_ms)),
        })

    warnings = []
    targets = patterns[:EXEMPLARS]
    if targets:
        rows = []
        try:
            rows, ids = fetch_logs(client, payload, selector, EXEMPLAR_ROWS, ROW_FIELDS)
            query_ids.extend(ids)
        except QueryError as error:
            warnings.append(f"Could not load exemplars: {error}")
        newest = {}
        for row in rows:
            tags = row_tags(row)
            if not unfingerprinted(tags.get("chq_fingerprint")):
                newest.setdefault((tags.get("service_name") or None, str(tags["chq_fingerprint"])), row)
        for pattern in targets:
            row = newest.get((pattern["service"], pattern["fingerprint"]))
            if row:
                pattern["exemplar"] = sample_of(row)
        missing_rows = [pattern for pattern in targets if "exemplar" not in pattern]
        selectors = [log_selector(args, pattern["fingerprint"], pattern["service"]) + filters
                     for pattern in missing_rows]
        for pattern, row in zip(missing_rows, sample_rows(client, payload, selectors, query_ids)):
            if isinstance(row, QueryError):
                warnings.append(f"Could not load exemplar for {pattern['fingerprint']}: {row}")
            elif row:
                pattern["exemplar"] = sample_of(row)

    unlisted = ranked[limit:]
    result = {
        "mode": "patterns",
        "query": selector,
        "query_ids": query_ids,
        "step_seconds": step,
        "total": total,
        "unfingerprinted": missing,
        "matched_patterns": len(ranked),
        "limit": limit,
        "unlisted_patterns": len(unlisted),
        "unlisted_events": sum(count for _, count in unlisted),
        "patterns": patterns,
        "warnings": warnings,
        "note": bucket_note(step, start_ms, end_ms, open_end),
    }
    if patterns:
        result["next"] = [pattern_hint(args, payload, patterns[0]["service"], patterns[0]["fingerprint"])]
    elif total:
        result["next"] = [hint("Matching rows, newest first.",
                               command_line("logs", payload, log_options(args, raw=True)),
                               "Run the same logs command with --raw.")]
    return result, None


def issue_patterns(client, args, payload, start, end, open_end):
    levels = LEVELS[args.severity] if args.severity else "|".join(LEVELS.values())
    step, start_ms, end_ms, query_end_ms = count_window(
        start, end, open_end, "The Issues window has no completed bucket. Use a wider window or search raw logs.")
    filters = line_filters(args, ignore_case=True)
    selector = stream_selector(args, levels, args.fingerprint)
    query_ids = []
    totals = defaultdict(int)
    services = defaultdict(lambda: dict.fromkeys(LEVELS, 0))
    trends = defaultdict(lambda: dict.fromkeys(LEVELS, 0))

    for point in count_points(client, dict(payload, e=iso_ms(query_end_ms)), selector, filters, step, query_ids,
                              ("service_name", "level", "chq_fingerprint")):
        count = bucket_count(point, start_ms, query_end_ms)
        tags = row_tags(point)
        service = tags.get("service_name")
        severity = str(tags.get("level", "")).lower()
        if severity == "warning":
            severity = "warn"
        if count is None or not service or severity not in LEVELS:
            continue
        fingerprint = tags.get("chq_fingerprint")
        if not unfingerprinted(fingerprint):
            totals[(service, severity, str(fingerprint))] += count
        services[service][severity] += count
        trends[point["timestamp"]][severity] += count

    ranked = sorted(totals.items(), key=lambda item: (-item[1], *(sort_key(part) for part in item[0])))
    patterns = [
        {"service": service, "severity": severity, "fingerprint": fingerprint, "count": count}
        for (service, severity, fingerprint), count in ranked[:args.limit]
    ]

    warnings = []
    samples = patterns[:args.samples]
    selectors = [stream_selector(argparse.Namespace(service=pattern["service"], label=args.label),
                                 LEVELS[pattern["severity"]], pattern["fingerprint"]) + filters
                 for pattern in samples]
    for pattern, row in zip(samples, sample_rows(client, payload, selectors, query_ids)):
        if isinstance(row, QueryError):
            warnings.append(f"Could not load sample for {pattern['fingerprint']}: {row}")
        elif row:
            pattern["sample"] = sample_of(row)

    service_counts = sorted(
        ({"service": service, **counts, "total": sum(counts.values())} for service, counts in services.items()),
        key=lambda row: (-row["total"], sort_key(row["service"])))
    unlisted = ranked[args.limit:]
    result = {
        "query_ids": query_ids,
        "step_seconds": step,
        "total": sum(row["total"] for row in service_counts),
        "service_counts": service_counts[:MAX_SERVICES],
        "trend": [{"timestamp": bucket, **trends[bucket]} for bucket in sorted(trends)],
        "matched_patterns": len(ranked),
        "unlisted_patterns": len(unlisted),
        "unlisted_events": sum(count for _, count in unlisted),
        "patterns": patterns,
        "warnings": warnings,
    }
    rest = service_counts[MAX_SERVICES:]
    if rest:
        result["service_counts_other"] = {
            "services": len(rest), **{level: sum(row[level] for row in rest) for level in LEVELS},
            "total": sum(row["total"] for row in rest)}
    if patterns:
        top = patterns[0]
        includes, excludes = filter_patterns(args, ignore_case=True)
        options = [("--service", top["service"]), ("--level", top["severity"])]
        options += [("--label", f"{key}={value}") for key, value in args.label]
        options += [("--grep", value) for value in includes] + [("--not-grep", value) for value in excludes]
        options += [("--fingerprint", top["fingerprint"]), ("--raw", None)]
        result["next"] = [hint(
            "Rows for the top pattern, newest first.", command_line("logs", payload, options),
            f"Run logs with the same window and filters and --service {shlex.quote(top['service'])} "
            f"--level {top['severity']} --fingerprint {top['fingerprint']} --raw.",
            "Run logs with the same window and filters and the top pattern's service, level and fingerprint, "
            "and --raw.")]
    return result


def canonical(value):
    """Sort object keys by UTF-16 code unit, as the PowerShell client's ordinal sort does."""
    if isinstance(value, dict):
        return {key: canonical(value[key]) for key in sorted(value, key=sort_key)}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def dumps(value):
    # ASCII-only compact JSON: the printed size in bytes equals its length.
    return json.dumps(canonical(value), ensure_ascii=True, separators=(",", ":"))


def escaped_bytes(char):
    """Bytes of JSON text that dumps writes for one code point."""
    if char in '"\\\b\f\n\r\t':
        return 2
    code = ord(char)
    if 0x20 <= code < 0x7F:
        return 1
    return 6 if code < 0x10000 else 12


def shorten_bytes(value, room):
    """Shorten value so its JSON text, without quotes, is at most room bytes, ending in an ellipsis."""
    sizes = [escaped_bytes(char) for char in value]
    if sum(sizes) <= room:
        return value
    if room < 6:
        return ""
    used, keep = 6, 0  # The ellipsis is written as ….
    for size in sizes:
        if used + size > room:
            break
        used += size
        keep += 1
    return value[:keep] + "…"


def shorten_copy(value, cap, keep, key=None):
    """Copy value, shortening strings longer than cap code points unless their key is in keep.
    Return the copy and the number of strings shortened."""
    if isinstance(value, str):
        if key not in keep and len(value) > cap:
            return shorten(value, cap), 1
        return value, 0
    if isinstance(value, dict):
        copy, count = {}, 0
        for name, item in value.items():
            copy[name], changed = shorten_copy(item, cap, keep, name)
            count += changed
        return copy, count
    if isinstance(value, list):
        copy, count = [], 0
        for item in value:
            item, changed = shorten_copy(item, cap, keep, key)
            copy.append(item)
            count += changed
        return copy, count
    return value, 0


def default_fields(row):
    """The row with only the default fields in its tags, or None if it has no others."""
    tags = row.get("tags") if isinstance(row, dict) else None
    if not isinstance(tags, dict) or all(name in ROW_FIELDS for name in tags):
        return None
    return dict(row, tags={name: value for name, value in tags.items() if name in ROW_FIELDS})


def measure(result):
    """The printed size, counting each next[].run as RUN_BYTES."""
    hints = result.get("next")
    if hints:
        result = dict(result, next=[dict(item, run=RUN_PLACEHOLDER) for item in hints])
    return len(dumps(result))


def shorten_message(result):
    """With --full-message, shorten the one kept row's message to fit the budget."""
    row = result["logs"][0] if result.get("logs") else None
    tags = row.get("tags") if isinstance(row, dict) else None
    message = tags.get("message") if isinstance(tags, dict) else None
    if not isinstance(message, str):
        return
    if "message_chars" not in row:
        row["message_chars"] = len(message)
        result["truncated_messages"] = result.get("truncated_messages", 0) + 1
    result["shortened_strings"] = result.get("shortened_strings", 0) + 1
    tags["message"] = ""
    tags["message"] = shorten_bytes(message, OUTPUT_LIMIT - measure(result))


def fit_output(result, budget_hint=None, full_message=False):
    """Serialize the result within OUTPUT_LIMIT bytes.

    Shorten an echoed query or scope over ECHO_BYTES. Then, only until the result fits:
    shorten strings in lists to 200 and then 100 code points, never identifiers, and never
    messages with --full-message; keep the leading items of each list that fit, where raw
    rows keep only the default fields from the first row that does not fit with all of its
    fields; with --full-message, keep the first row and shorten its message to fit."""
    for name in ECHO_FIELDS:
        value = result.get(name)
        if isinstance(value, str) and len(dumps(value)) > ECHO_BYTES:
            result[name] = shorten_bytes(value, ECHO_BYTES - 2)
            result[name + "_chars"] = len(value)
    if measure(result) <= OUTPUT_LIMIT:
        return dumps(result)
    result["budget_applied"] = True
    if budget_hint and budget_hint not in result.get("next", []):
        result["next"] = result.get("next", []) + [budget_hint]
    keep = BUDGET_KEEP | {"message"} if full_message else BUDGET_KEEP
    names = [name for name in BUDGET_LISTS if isinstance(result.get(name), list) and result[name]]
    source = {name: result[name] for name in names}
    rows = "logs" if result.get("mode") == "raw" and "logs" in source else None
    cache = {}

    def item(name, index, cap, strip=False):
        """One list item shortened at cap: (value, size, strings shortened, fields dropped)."""
        key = (name, index, cap, strip)
        if key not in cache:
            value = source[name][index]
            stripped = default_fields(value) if strip else None
            value, count = shorten_copy(stripped or value, cap, keep, name)
            cache[key] = (value, len(dumps(value)), count, stripped is not None)
        return cache[key]

    def whole(name, cap):
        return [item(name, index, cap) for index in range(len(source[name]))]

    def size(entries):
        return sum(entry[1] for entry in entries) + max(len(entries) - 1, 0)

    # Measure everything but the list items once, with every counter at its largest value.
    for name in names:
        result[name] = []
    omitted = {name: len(source[name]) for name in names}
    if rows:
        omitted["fields"] = len(source[rows])
    fixed = measure(dict(result, shortened_strings=COUNTER_RESERVE, omitted=omitted))

    def fits(cap):
        total = fixed
        for name in names:
            for index in range(len(source[name])):
                total += item(name, index, cap)[1] + (1 if index else 0)
                if total > OUTPUT_LIMIT:
                    return False
        return True

    chosen = {}
    for cap in BUDGET_CAPS:
        if fits(cap):
            chosen = {name: whole(name, cap) for name in names}
            break
    else:
        for position, name in enumerate(names):
            total = fixed + sum(size(chosen[other]) for other in names[:position])
            total += sum(size(whole(other, cap)) for other in names[position + 1:])
            kept, strip = [], False
            for index in range(len(source[name])):
                comma = 1 if kept else 0
                entry = item(name, index, cap, strip)
                if name == rows and not strip and total + comma + entry[1] > OUTPUT_LIMIT:
                    strip = True
                    entry = item(name, index, cap, strip)
                if total + comma + entry[1] > OUTPUT_LIMIT:
                    if full_message and name == rows and not kept:
                        kept.append(entry)
                        total += entry[1]
                    break
                kept.append(entry)
                total += comma + entry[1]
            chosen[name] = kept
            if total <= OUTPUT_LIMIT:
                for other in names[position + 1:]:
                    chosen[other] = whole(other, cap)
                break

    omitted = {}
    shortened = 0
    for name in names:
        result[name] = [entry[0] for entry in chosen[name]]
        shortened += sum(entry[2] for entry in chosen[name])
        if len(chosen[name]) < len(source[name]):
            omitted[name] = len(source[name]) - len(chosen[name])
    if rows and any(entry[3] for entry in chosen[rows]):
        omitted["fields"] = sum(1 for entry in chosen[rows] if entry[3])
    if omitted:
        result["omitted"] = omitted
    if shortened:
        result["shortened_strings"] = shortened
    if full_message and rows and measure(result) > OUTPUT_LIMIT:
        shorten_message(result)
    if measure(result) > OUTPUT_LIMIT:
        # Reached when identifiers the shrink steps never shorten (service names,
        # fingerprints, timestamps) are too long on their own: drop every list item.
        for name in names:
            result[name] = []
        result["omitted"] = {name: len(source[name]) for name in names}
        result.pop("shortened_strings", None)
        if measure(result) > OUTPUT_LIMIT:
            result = {"start": result["start"], "end": result["end"], "budget_applied": True, "error": BUDGET_FAILED}
    return dumps(result)


def label_filter(value):
    key, separator, label_value = value.partition("=")
    if not separator or not LABEL.fullmatch(key):
        raise argparse.ArgumentTypeError("Expected LABEL=VALUE with a valid LogQL label name.")
    return key.replace(".", "_"), label_value


def tag_name(value):
    if not LABEL.fullmatch(value) or len(value) > MAX_NAME_CHARS:
        raise argparse.ArgumentTypeError("Expected a label name such as service_name or k8s_namespace_name.")
    return value


def fingerprint_id(value):
    if not re.fullmatch(r"-?[0-9]+", value) or not -(2**63) <= int(value) < 2**63:
        raise argparse.ArgumentTypeError("Expected a signed 64-bit fingerprint.")
    return str(int(value))


def positive_int(value):
    number = int(value)
    if number <= 0 or number > 2147483647:
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
    logs = commands.add_parser("logs", help="Rank log patterns by count, or return raw rows with --raw.")
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
        command.add_argument("--fingerprint", type=fingerprint_id)
    logs.add_argument("--limit", type=positive_int,
                      help=f"Patterns (default {DEFAULT_PATTERNS}) or --raw rows (default {DEFAULT_ROWS}); "
                           f"at most {MAX_LOG_LIMIT}.")
    logs.add_argument("--query", help="Raw LogQL stream selector and optional line filters.")
    logs.add_argument("--raw", action="store_true", help="Return matching rows, newest first.")
    logs.add_argument("--field", action="append", type=tag_name,
                      help=f"Return rows with only this log tag (implies --raw). Repeatable, up to {MAX_FIELDS}.")
    logs.add_argument("--all-fields", action="store_true", help="Return rows with every log tag (implies --raw).")
    logs.add_argument("--full-message", action="store_true",
                      help=f"Return rows with messages up to {FULL_MESSAGE_CHARS} characters (implies --raw).")
    logs.add_argument("--level", choices=LEVELS, help="Filter by normalized severity.")
    issues.add_argument("--limit", type=positive_int, default=20)
    issues.add_argument("--severity", choices=LEVELS)
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
        # Without --end the window ends now, and counts include the newest, partial bucket.
        open_end = not args.end
        client = QueryAPI(url, key, args.timeout, args.retries)
        payload = {"s": iso(start), "e": iso(end)}
        budget_hint = None
        full_message = False
        if args.command == "logs":
            args.level_name, args.level = args.level, LEVELS.get(args.level)
            result, budget_hint = search_logs(client, args, payload, start, end, open_end)
            full_message = args.full_message
        elif args.command == "tags":
            result = list_tags(client, args, payload)
        elif args.command == "values":
            result = list_tag_values(client, args, payload)
        else:
            result = issue_patterns(client, args, payload, start, end, open_end)
        print(fit_output({"start": payload["s"], "end": payload["e"], **result}, budget_hint, full_message))
    except (QueryError, ValueError, OverflowError, KeyError, TypeError) as error:
        print(f"cardinal-query: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

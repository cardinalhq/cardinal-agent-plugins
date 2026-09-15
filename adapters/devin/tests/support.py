"""Test helpers: import paths, fixture loading, and local fake servers.

Every fake binds 127.0.0.1 on an ephemeral port. Nothing here touches
the real network.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = TESTS_DIR.parent
REPO_ROOT = ADAPTER_DIR.parent.parent
for _p in (REPO_ROOT / "core", ADAPTER_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cardinal_core import otlp  # noqa: E402
from cardinal_core.paths import AgentPaths, atomic_write_json  # noqa: E402

from cardinal_devin.client import DevinClient, GitHubClient, JsonHttp  # noqa: E402
from cardinal_devin.poller import PollOptions, Poller  # noqa: E402
from cardinal_devin.sessions import to_ns  # noqa: E402
from cardinal_devin.state import STATE_FILE, PollState  # noqa: E402

FIXTURES = TESTS_DIR / "fixtures"
GOLDENS = TESTS_DIR / "goldens"


def load_fixture(name: str) -> Dict[str, Any]:
    """Fixture payload without its `_source` provenance note."""
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    data.pop("_source", None)
    return data


class Request:
    def __init__(self, method: str, path: str, query: Dict[str, List[str]], headers: Dict[str, str], body: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


Response = Tuple[int, Dict[str, str], Any]


class FakeServer:
    def __init__(self) -> None:
        self.requests: List[Request] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _dispatch(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urllib.parse.urlsplit(self.path)
                req = Request(
                    self.command, parts.path, urllib.parse.parse_qs(parts.query),
                    {k.lower(): v for k, v in self.headers.items()}, body,
                )
                outer.requests.append(req)
                status, headers, payload = outer.handle(req)
                raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = _dispatch
            do_POST = _dispatch

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def handle(self, req: Request) -> Response:
        return 404, {}, {"detail": "Not Found"}

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeDevin(FakeServer):
    """Serves documented v1 or v3 session routes from a list of payloads."""

    def __init__(self, api: str, sessions: List[Dict[str, Any]], *, users: Optional[Dict[str, Any]] = None,
                 org_id: str = "org-test", api_key: str = "cog_test") -> None:
        self.api = api
        self.sessions = copy.deepcopy(sessions)
        self.users = users or {}
        self.org_id = org_id
        self.api_key = api_key
        self.rate_limit_remaining = 0
        self.retry_after: Optional[str] = "0"
        # v3 only. Sessions are stored in unix seconds; these choose how
        # updated_at/created_at are serialised and how updated_after is read,
        # so tests can model a unit mismatch in either direction.
        self.response_unit = "s"
        self.filter_unit = "s"
        super().__init__()

    def _v3_out(self, session: Dict[str, Any]) -> Dict[str, Any]:
        out = copy.deepcopy(session)
        if self.response_unit == "ms":
            for key in ("created_at", "updated_at"):
                if isinstance(out.get(key), int):
                    out[key] *= 1000
        return out

    def update(self, session_id: str, **fields: Any) -> None:
        for session in self.sessions:
            if session["session_id"] == session_id:
                session.update(fields)
                return
        raise KeyError(session_id)

    def _find(self, session_id: str) -> Optional[Dict[str, Any]]:
        for session in self.sessions:
            if session["session_id"] == session_id:
                return copy.deepcopy(session)
        return None

    def handle(self, req: Request) -> Response:
        if req.headers.get("authorization") != f"Bearer {self.api_key}":
            return 401, {}, {"detail": "Unauthorized"}
        if self.rate_limit_remaining > 0:
            self.rate_limit_remaining -= 1
            headers = {"Retry-After": self.retry_after} if self.retry_after is not None else {}
            return 429, headers, {"detail": "Too Many Requests"}
        q = {k: v[0] for k, v in req.query.items()}
        if self.api == "v1":
            if req.path == "/v1/sessions":
                limit, offset = int(q.get("limit", "100")), int(q.get("offset", "0"))
                return 200, {}, {"sessions": copy.deepcopy(self.sessions[offset:offset + limit])}
            if req.path.startswith("/v1/sessions/"):
                found = self._find(urllib.parse.unquote(req.path[len("/v1/sessions/"):]))
                if found is None:
                    # v1 documents only 422 (HTTPValidationError) for this route.
                    return 422, {}, {"detail": [{"loc": ["path", "session_id"], "msg": "Session not found",
                                                 "type": "value_error"}]}
                found.pop("requesting_user_email", None)  # not on the detail schema
                found.setdefault("messages", [])
                return 200, {}, found
            return 404, {}, {"detail": "Not Found"}
        prefix = f"/v3/organizations/{self.org_id}/sessions"
        if req.path == prefix:
            first = int(q.get("first", "100"))
            start = int(q.get("after", "0"))
            items = self.sessions
            if "updated_after" in q:
                scale = 1000 if self.filter_unit == "ms" else 1
                items = [s for s in items if s["updated_at"] * scale >= int(q["updated_after"])]
            page = items[start:start + first]
            end = start + len(page)
            has_next = end < len(items)
            return 200, {}, {
                "items": [self._v3_out(s) for s in page], "end_cursor": str(end) if has_next else None,
                "has_next_page": has_next, "total": len(items),
            }
        if req.path.startswith(prefix + "/"):
            # The path takes devin_id: "the session ID prefixed with `devin-`".
            devin_id = urllib.parse.unquote(req.path[len(prefix) + 1:])
            found = None
            if devin_id.startswith("devin-"):
                found = self._find(devin_id[len("devin-"):]) or self._find(devin_id)
            return (200, {}, self._v3_out(found)) if found else (404, {}, {"detail": "Not Found"})
        users_prefix = f"/v3beta1/organizations/{self.org_id}/members/users/"
        if req.path.startswith(users_prefix):
            user = self.users.get(urllib.parse.unquote(req.path[len(users_prefix):]))
            return (200, {}, user) if user else (404, {}, {"detail": "Not Found"})
        return 404, {}, {"detail": "Not Found"}


class FakeGitHub(FakeServer):
    def __init__(self, pulls: Dict[str, Any], token: str = "gh-test") -> None:
        self.pulls = copy.deepcopy(pulls)
        self.token = token
        super().__init__()

    def handle(self, req: Request) -> Response:
        if req.headers.get("authorization") != f"Bearer {self.token}":
            return 401, {}, {"message": "Bad credentials"}
        parts = req.path.strip("/").split("/")
        if len(parts) == 5 and parts[0] == "repos" and parts[3] == "pulls":
            pull = self.pulls.get(f"{parts[1]}/{parts[2]}/{parts[4]}")
            if pull:
                return 200, {}, pull
        return 404, {}, {"message": "Not Found"}


class FakeIngest(FakeServer):
    """OTLP/HTTP receiver. Also answers the connect-time /v1/metrics probe."""

    def __init__(self) -> None:
        self.status = 200
        self.posts: List[Tuple[int, Any]] = []
        super().__init__()

    def handle(self, req: Request) -> Response:
        if req.path == "/v1/logs":
            self.posts.append((self.status, req.json()))
            return self.status, {}, {}
        if req.path == "/v1/metrics":
            return 200, {}, {}
        return 404, {}, None

    def accepted_bodies(self) -> List[Any]:
        return [body for status, body in self.posts if 200 <= status < 300]


class FakeCardinal(FakeIngest):
    """Device-code endpoints plus an ingest endpoint on the same server."""

    def __init__(self) -> None:
        self.token_error: Optional[str] = None
        super().__init__()

    def handle(self, req: Request) -> Response:
        if req.path == "/api/auth/device/code":
            return 201, {}, {
                "device_code": "dev-code-1", "user_code": "ABCD-EFGH",
                "verification_uri": self.url + "/device", "expires_in": 60, "interval": 1,
            }
        if req.path == "/api/auth/device/token":
            if self.token_error:
                return 400, {}, {"error": self.token_error}
            return 200, {}, {
                "org": {"id": "org-1", "slug": "acme"},
                "user": {"email": "admin@example.com"},
                "ingest": {
                    "endpoint": self.url, "api_key": "ingest-secret-key-123",
                    "api_header": "x-cardinalhq-api-key", "key_id": "key-1",
                },
            }
        return super().handle(req)


class captured_logs:
    """Collect `cardinal_devin` log lines (any level) without requiring
    that there are any, unlike unittest's assertLogs."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def __enter__(self) -> "captured_logs":
        outer = self

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                outer.lines.append(f"{record.levelname}:{record.getMessage()}")

        self._logger = logging.getLogger("cardinal_devin")
        self._handler = _Collect()
        self._level = self._logger.level
        self._propagate = self._logger.propagate
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        return self

    def __exit__(self, *exc: Any) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)
        self._logger.propagate = self._propagate

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def attrs_of(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items:
        value = item["value"]
        if "intValue" in value:
            out[item["key"]] = int(value["intValue"])
        elif "boolValue" in value:
            out[item["key"]] = value["boolValue"]
        elif "doubleValue" in value:
            out[item["key"]] = value["doubleValue"]
        else:
            out[item["key"]] = value.get("stringValue")
    return out


def records_in(bodies: List[Any], event: Optional[str] = None) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(resource attrs, record attrs) for every log record, optionally one event."""
    out = []
    for body in bodies:
        for rl in body["resourceLogs"]:
            resource = attrs_of(rl["resource"]["attributes"])
            for sl in rl["scopeLogs"]:
                for rec in sl["logRecords"]:
                    attrs = attrs_of(rec["attributes"])
                    if event is None or attrs.get("event_name") == event:
                        out.append((resource, attrs))
    return out


class Harness:
    """Fake Devin + GitHub + ingest, a connected state dir, and a fixed clock."""

    def __init__(self, add_cleanup: Callable[..., None], *, api: str = "v1", page_size: int = 2) -> None:
        tmp = tempfile.mkdtemp(prefix="cardinal-devin-test-")
        add_cleanup(shutil.rmtree, tmp, True)
        fixture = load_fixture("v1_sessions.json" if api == "v1" else "v3_sessions.json")
        self.api = api
        self.page_size = page_size
        self.devin = FakeDevin(api, fixture["sessions"], users=fixture.get("users"))
        self.github = FakeGitHub(load_fixture("github_pulls.json")["pulls"])
        self.ingest = FakeIngest()
        for server in (self.devin, self.github, self.ingest):
            add_cleanup(server.close)
        self.paths = AgentPaths(home=Path(tmp) / "home")
        atomic_write_json(self.paths.state_path, {
            "schema_version": 2, "host": "https://cardinal.test", "org_slug": "acme",
            "deployment_environment": "test", "ingest_endpoint": self.ingest.url,
        })
        atomic_write_json(self.paths.secrets_path, {"ingest_api_key": "ingest-key"})
        latest = max(to_ns(s["updated_at"]) for s in fixture["sessions"])
        self.now = latest / 1e9 + 3600
        self.sleeps: List[float] = []

    @property
    def state_path(self) -> Path:
        return self.paths.runtime_dir / STATE_FILE

    def poller(self, *, github: bool = True, dry_run: bool = False, **options: Any) -> Poller:
        http = JsonHttp(timeout=5.0, max_retries=3, sleep=self.sleeps.append)
        devin = DevinClient(
            "cog_test", base_url=self.devin.url, org_id="org-test" if self.api == "v3" else None,
            page_size=self.page_size, http=http,
        )
        gh = GitHubClient("gh-test", base_url=self.github.url, web_host="github.com",
                          http=JsonHttp(timeout=5.0, sleep=self.sleeps.append)) if github else None
        return Poller(
            devin=devin,
            state=PollState(self.state_path, readonly=dry_run),
            connection=otlp.connection_from_paths(self.paths),
            connection_state=self.paths.read_state(),
            github=gh,
            options=PollOptions(dry_run=dry_run, **options),
            clock=lambda: self.now,
        )

    def records(self, event: Optional[str] = None) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        return records_in(self.ingest.accepted_bodies(), event)

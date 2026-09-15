"""HTTP clients for the Devin REST API and GitHub REST.

stdlib only. Every request has a bounded timeout. HTTP 429 is retried
with backoff that honours `Retry-After` (delta-seconds or HTTP-date);
Devin's docs list 429 but give no limits or header details, so the
header is optional and exponential backoff is the fallback.

Two Devin API shapes are supported because the docs publish both:

- v1 (deprecated, still served): `GET /v1/sessions` with `limit`/`offset`,
  response `{"sessions": [...]}`; `GET /v1/sessions/{session_id}`.
- v3 (current, needs an org id): `GET /v3/organizations/{org_id}/sessions`
  with `first`/`after` cursor pagination, response
  `{"items", "end_cursor", "has_next_page", "total"}`;
  `GET /v3/organizations/{org_id}/sessions/{devin_id}`.
"""

from __future__ import annotations

import email.utils
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterator, Optional

log = logging.getLogger("cardinal_devin")

DEFAULT_DEVIN_BASE_URL = "https://api.devin.ai"
DEFAULT_GITHUB_API_URL = "https://api.github.com"
USER_AGENT = "cardinal-devin-poller"
API_VERSIONS = ("auto", "v1", "v3")
V3_MAX_PAGE_SIZE = 200


class ApiError(RuntimeError):
    """A request failed. `status` is the HTTP status, or 0 for a network
    or decoding failure."""

    def __init__(self, status: int, url: str, message: str = "") -> None:
        detail = f": {message}" if message else ""
        super().__init__(f"HTTP {status} from {url}{detail}" if status else f"{url}{detail}")
        self.status = status
        self.url = url


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Seconds to wait from a Retry-After header, or None if absent/unparseable."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    current = time.time() if now is None else now
    return max(0.0, when.timestamp() - current)


class JsonHttp:
    """GET-with-retry for JSON APIs."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        max_backoff: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self.sleep = sleep

    def get(self, url: str, headers: Dict[str, str]) -> Any:
        attempt = 0
        while True:
            req = urllib.request.Request(
                url,
                method="GET",
                headers={"accept": "application/json", "user-agent": USER_AGENT, **headers},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                message = _error_text(exc)
                exc.close()
                if exc.code == 429 and attempt < self.max_retries:
                    delay = parse_retry_after(retry_after)
                    if delay is None:
                        delay = self.backoff_base * (2 ** attempt)
                    delay = min(delay, self.max_backoff)
                    attempt += 1
                    log.warning(
                        "rate limited by %s; retrying in %.1fs (attempt %d/%d)",
                        _host(url), delay, attempt, self.max_retries,
                    )
                    self.sleep(delay)
                    continue
                raise ApiError(exc.code, url, message) from None
            except (urllib.error.URLError, OSError) as exc:
                raise ApiError(0, url, f"network error: {exc}") from None
            except ValueError as exc:
                raise ApiError(0, url, f"invalid JSON: {exc}") from None


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")[:300].strip()


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url


class DevinClient:
    """Reads sessions from the Devin API with a service-user key."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_DEVIN_BASE_URL,
        org_id: Optional[str] = None,
        api_version: str = "auto",
        page_size: int = 100,
        max_pages: int = 50,
        http: Optional[JsonHttp] = None,
    ) -> None:
        if api_version not in API_VERSIONS:
            raise ValueError(f"api_version must be one of {', '.join(API_VERSIONS)}")
        if api_version == "auto":
            api_version = "v3" if org_id else "v1"
        if api_version == "v3" and not org_id:
            raise ValueError("the v3 API needs an org id (DEVIN_ORG_ID or --devin-org-id)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.org_id = org_id
        self.api_version = api_version
        self.page_size = max(1, page_size)
        self.max_pages = max(1, max_pages)
        self.http = http or JsonHttp()
        # Set by iter_sessions when it stops at max_pages with more to read.
        self.last_list_truncated = False

    def _headers(self) -> Dict[str, str]:
        return {"authorization": f"Bearer {self.api_key}"}

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        query = urllib.parse.urlencode(params, doseq=True) if params else ""
        return f"{self.base_url}{path}" + (f"?{query}" if query else "")

    def _org_path(self) -> str:
        return urllib.parse.quote(str(self.org_id), safe="")

    def iter_sessions(self, updated_after: Optional[int] = None) -> Iterator[Dict[str, Any]]:
        """Every session the key can see. `updated_after` (unix seconds) is
        sent as a server-side filter on v3 only; v1 documents no such filter."""
        self.last_list_truncated = False
        if self.api_version == "v1":
            yield from self._iter_v1()
        else:
            yield from self._iter_v3(updated_after)

    def _iter_v1(self) -> Iterator[Dict[str, Any]]:
        offset = 0
        for _ in range(self.max_pages):
            url = self._url("/v1/sessions", {"limit": self.page_size, "offset": offset})
            data = self.http.get(url, self._headers())
            items = data.get("sessions") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ApiError(0, url, "response has no 'sessions' array")
            for item in items:
                if isinstance(item, dict):
                    yield item
            if len(items) < self.page_size:
                return
            offset += len(items)
        self.last_list_truncated = True
        log.warning("stopped listing Devin sessions after %d pages", self.max_pages)

    def _iter_v3(self, updated_after: Optional[int]) -> Iterator[Dict[str, Any]]:
        after: Optional[str] = None
        path = f"/v3/organizations/{self._org_path()}/sessions"
        for _ in range(self.max_pages):
            params: Dict[str, Any] = {"first": min(self.page_size, V3_MAX_PAGE_SIZE)}
            if updated_after is not None:
                params["updated_after"] = int(updated_after)
            if after:
                params["after"] = after
            url = self._url(path, params)
            data = self.http.get(url, self._headers())
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ApiError(0, url, "response has no 'items' array")
            for item in items:
                if isinstance(item, dict):
                    yield item
            cursor = data.get("end_cursor")
            if not data.get("has_next_page") or not isinstance(cursor, str) or not cursor:
                return
            after = cursor
        self.last_list_truncated = True
        log.warning("stopped listing Devin sessions after %d pages", self.max_pages)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Session detail, or None when the API says it does not exist."""
        sid = urllib.parse.quote(session_id, safe="")
        if self.api_version == "v1":
            path = f"/v1/sessions/{sid}"
        else:
            path = f"/v3/organizations/{self._org_path()}/sessions/{sid}"
        url = self._url(path)
        try:
            data = self.http.get(url, self._headers())
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            raise ApiError(0, url, "session detail is not an object")
        return data

    def get_user_email(self, user_id: str) -> Optional[str]:
        """v3 sessions carry `user_id`, not an email. The org user lookup is
        documented as beta (`/v3beta1/...`) and needs ViewOrgMembership."""
        if self.api_version != "v3":
            return None
        uid = urllib.parse.quote(user_id, safe="")
        url = self._url(f"/v3beta1/organizations/{self._org_path()}/members/users/{uid}")
        try:
            data = self.http.get(url, self._headers())
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        email_value = data.get("email") if isinstance(data, dict) else None
        return email_value if isinstance(email_value, str) and email_value else None


class GitHubClient:
    """`GET /repos/{owner}/{repo}/pulls/{pull_number}` with a token."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_GITHUB_API_URL,
        http: Optional[JsonHttp] = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.http = http or JsonHttp(timeout=10.0, max_retries=2)

    def get_pull(self, owner: str, repo: str, number: int) -> Optional[Dict[str, Any]]:
        path = "/repos/{}/{}/pulls/{}".format(
            urllib.parse.quote(owner, safe=""), urllib.parse.quote(repo, safe=""), int(number),
        )
        url = self.base_url + path
        try:
            data = self.http.get(url, {
                "authorization": f"Bearer {self.token}",
                "accept": "application/vnd.github+json",
                "x-github-api-version": "2022-11-28",
            })
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

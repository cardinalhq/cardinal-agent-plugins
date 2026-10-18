"""MCP URL parsing and redirect-safe HTTP setup, without evidence CLI imports."""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
from typing import Any, Optional

MCP_URL_PATH_RE = re.compile(r"^/api/orgs/([^/?#]+)/mcp(?:/|$)")


def origin_of(url: str) -> Optional[str]:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default = 443 if parts.scheme == "https" else 80
    host = parts.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def connection_for(url: Any, key: Any) -> dict:
    """{origin, org, key} for an MCP URL and key; {} when the URL is not a
    usable http(s) URL."""
    if not (isinstance(url, str) and url):
        return {}
    origin = origin_of(url)
    if origin is None:
        return {}
    m = MCP_URL_PATH_RE.match(urllib.parse.urlsplit(url).path)
    return {"origin": origin, "org": urllib.parse.unquote(m.group(1)) if m else None,
            "key": key if isinstance(key, str) and key else None}



class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward credential headers across a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def no_redirect_opener():
    return urllib.request.build_opener(_NoRedirect())

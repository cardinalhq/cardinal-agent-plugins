"""Would Claude Code send a Cardinal API key to Cardinal Cloud by default?

The plugin's .mcp.json is

    "url":     "${CARDINAL_MCP_URL:-https://app.cardinalhq.io/mcp}"
    "headers": {"X-CardinalHQ-API-Key": "${CARDINAL_MCP_API_KEY:-}"}

and cannot express "send the key only with a configured URL". So when
CARDINAL_MCP_API_KEY is set (exported in a shell profile for a manual Codex
config, or a self-hosted maestro's key) but CARDINAL_MCP_URL is not,
Claude Code sends that key to Cardinal Cloud. /cardinal:connect always writes
both, so a connected install never hits this; a stray key does.

key_without_url() detects it from ~/.claude/settings.json `env` (which Claude
Code applies to its own environment) and the process environment, the same
two places Claude Code substitutes from. An empty or blank value counts as
unset, as `${VAR:-default}` does for the URL and as an empty header does for
the key.

Used by storyboard-session.py (SessionStart, runs connected or not) and
bin/cardinal-status. Reads only local files; never raises.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MCP_URL_ENV = "CARDINAL_MCP_URL"
MCP_KEY_ENV = "CARDINAL_MCP_API_KEY"
CLOUD_MCP_URL = "https://app.cardinalhq.io/mcp"

WARNING = (
    "Cardinal: CARDINAL_MCP_API_KEY is set but CARDINAL_MCP_URL is not, so "
    "Claude Code sends that API key to Cardinal Cloud (" + CLOUD_MCP_URL + ") "
    "when it connects the cardinal MCP server. Run /cardinal:connect (or "
    "/cardinal:connect --host <your maestro URL> for self-hosted Cardinal), "
    "or unset CARDINAL_MCP_API_KEY (check your shell profile and "
    "~/.claude/settings.json env)."
)


def _home(home: Path | None) -> Path:
    if home is not None:
        return home
    return Path(os.environ.get("HOME") or str(Path.home()))


def _settings_env(home: Path) -> dict:
    try:
        with open(home / ".claude" / "settings.json", encoding="utf-8") as f:
            env = json.load(f).get("env")
    except (OSError, ValueError, AttributeError):
        return {}
    return env if isinstance(env, dict) else {}


def _value(sources, name: str) -> str | None:
    for source in sources:
        value = source.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _sources(home: Path | None, environ: dict | None) -> tuple:
    return (_settings_env(_home(home)), os.environ if environ is None else environ)


def key_without_url(home: Path | None = None, environ: dict | None = None) -> bool:
    """True when a Cardinal MCP key is set and no CARDINAL_MCP_URL is."""
    try:
        sources = _sources(home, environ)
        return bool(_value(sources, MCP_KEY_ENV)) and not _value(sources, MCP_URL_ENV)
    except Exception:
        return False


def mcp_url(home: Path | None = None, environ: dict | None = None) -> str:
    """The URL .mcp.json resolves to: CARDINAL_MCP_URL, else Cardinal Cloud."""
    try:
        return _value(_sources(home, environ), MCP_URL_ENV) or CLOUD_MCP_URL
    except Exception:
        return CLOUD_MCP_URL

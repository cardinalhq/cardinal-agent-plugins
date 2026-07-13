"""Claude Code OTel settings acquisition — adapter-side BY DESIGN.

Unlike codex/cursor/gemini (which read connection facts from
~/.<agent>/cardinal.json + cardinal-secrets.json via core AgentPaths),
the Claude plugin's hooks get their OTLP connection from Claude Code's
own OTel settings: the `env` block cardinal-connect wrote into
~/.claude/settings.json. Claude Code's native exporter reads those keys
at process start but does NOT propagate OTEL_* into hook subprocess
environments (empirically validated 2026-06-06), so hook scripts read
the source of truth directly and construct core's otlp.IngestConnection
from it. See conductor docs/specs/agent-sessions.md §Plugin hook
contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_core.otlp import IngestConnection  # noqa: E402
import _plugin_version  # noqa: E402

API_KEY_HEADER = "x-cardinalhq-api-key"

# Bound at import time (hooks are one process per invocation) — the same
# semantics the pre-migration _limits_common module had, which the
# importlib-based tests rely on.
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def load_otel_settings() -> dict[str, str]:
    """The OTel env block from ~/.claude/settings.json (string values only).
    settings.json wins over the process env, because Claude Code strips
    OTEL_* and CLAUDE_PROJECT_DIR from hook subprocess envs in practice."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        env = data.get("env") or {}
        return {k: v for k, v in env.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        return {}


def parse_kv_csv(raw: str) -> dict[str, str]:
    """Comma-separated k=v pairs (OTEL_RESOURCE_ATTRIBUTES /
    OTEL_EXPORTER_OTLP_HEADERS spelling)."""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


def _setting(settings_env: dict[str, str], key: str, default: str = "") -> str:
    return settings_env.get(key) or os.environ.get(key, default)


def otlp_headers(settings_env: dict[str, str]) -> dict[str, str]:
    return parse_kv_csv(_setting(settings_env, "OTEL_EXPORTER_OTLP_HEADERS"))


def ingest_connection(settings_env: dict[str, str]) -> IngestConnection | None:
    """core IngestConnection from the Claude OTel settings. None when the
    endpoint is missing (emit becomes a no-op — same silent-exit contract
    as before). When OTEL_EXPORTER_OTLP_HEADERS carries several pairs the
    Cardinal key header wins, else the first pair rides along (core's
    IngestConnection speaks exactly one auth header — see CORE_GAPS.md)."""
    endpoint = _setting(settings_env, "OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    headers = otlp_headers(settings_env)
    header, key = API_KEY_HEADER, ""
    for k, v in headers.items():
        if k.lower() == API_KEY_HEADER:
            header, key = k, v
            break
    else:
        if headers:
            header, key = next(iter(headers.items()))
    return IngestConnection(
        endpoint=endpoint.rstrip("/"), api_key=key, api_header=header,
    )


def ingest_api_key(settings_env: dict[str, str] | None = None) -> str | None:
    """The plugin's ingest key, from OTEL_EXPORTER_OTLP_HEADERS — the same
    credential the spend-limits status endpoint authenticates."""
    env = settings_env if settings_env is not None else load_otel_settings()
    for k, v in otlp_headers(env).items():
        if k.lower() == API_KEY_HEADER and v:
            return v
    return None


def resource_attrs(settings_env: dict[str, str]) -> dict[str, str]:
    """Resource attributes: OTEL_RESOURCE_ATTRIBUTES verbatim, with
    service.name/agent.runtime defaults and the plugin version stamped at
    emit time from the on-disk plugin.json (self-heals on upgrade — the
    value baked into settings.json at install time goes stale).

    NOT core's otlp.resource_attrs(): that fixed shape adds
    cardinal.core_version and unknown-defaults for identity keys, which
    would change the wire bytes; here the connect-time CSV is already the
    complete identity."""
    attrs = parse_kv_csv(_setting(settings_env, "OTEL_RESOURCE_ATTRIBUTES"))
    attrs.setdefault("service.name", "claude-code")
    attrs.setdefault("agent.runtime", "claude-code")
    attrs["cardinal.plugin_version"] = _plugin_version.plugin_version()
    return attrs

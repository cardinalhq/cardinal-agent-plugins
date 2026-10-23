"""Copilot CLI's user MCP config, as cardinal-connect / -disconnect /
-status manage it.

Copilot CLI reads user MCP servers from `$COPILOT_HOME/mcp-config.json`
(`~/.copilot/mcp-config.json` by default; `copilot mcp --help`). cardinal-
connect writes two entries there:

  cardinal           the remote Cardinal MCP gateway (type http, the MCP
                     key in `headers`; Copilot substitutes nothing in a
                     remote entry, so the key cannot ship in the plugin)
  cardinal-behavior  the plugin's local stdio server (scripts/cardinal-behavior
                     --runtime copilot), which reads the connection from
                     ~/.copilot/cardinal.json + cardinal-secrets.json

Copilot validates MCP entries and may skip one with keys it does not know,
so nothing Cardinal-specific is written into an entry. What Cardinal owns is
recorded in its own state file instead (`managed_mcp` in
~/.copilot/cardinal.json): disconnect removes an entry only when it is still
the one recorded there.

Stdlib only; the plugin's hooks never import this.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

MCP_SERVER_NAME = "cardinal"
BEHAVIOR_SERVER_NAME = "cardinal-behavior"
API_KEY_HEADER = "X-CardinalHQ-API-Key"


def copilot_home(environ: Optional[dict] = None) -> Path:
    """$COPILOT_HOME when set to an absolute path, else ~/.copilot."""
    env = os.environ if environ is None else environ
    custom = env.get("COPILOT_HOME")
    return Path(custom) if custom and os.path.isabs(custom) else Path.home() / ".copilot"


def mcp_config_path(environ: Optional[dict] = None) -> Path:
    return copilot_home(environ) / "mcp-config.json"


class ConfigError(Exception):
    """mcp-config.json cannot be changed safely (unreadable, not an object,
    or an entry named like Cardinal's that Cardinal did not write)."""


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} is not a JSON object")
    return data


def servers_of(data: dict) -> dict:
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def build_mcp_entry(mcp_url: str, api_key: str, plugin_version: str) -> dict:
    """The remote gateway entry. X-Cardinal-Client names this agent on every
    MCP request: Cardinal records it as the producing client of the
    investigation events posted through its MCP tools (a display claim,
    never identity)."""
    return {
        "type": "http",
        "url": mcp_url,
        "headers": {API_KEY_HEADER: api_key, "X-Cardinal-Client": f"copilot/{plugin_version}"},
        "tools": ["*"],
    }


def build_behavior_entry(script: Path) -> dict:
    return {
        "type": "stdio",
        "command": "python3",
        "args": [str(script), "--runtime", "copilot"],
        "tools": ["*"],
    }


def fingerprint(entry: Any) -> Optional[dict]:
    """What identifies an entry as the one Cardinal wrote: the URL of the
    gateway entry, the command + args of the behavior entry."""
    if not isinstance(entry, dict):
        return None
    if entry.get("url"):
        return {"url": entry.get("url")}
    return {"command": entry.get("command"), "args": entry.get("args")}


def is_managed(name: str, entry: Any, managed: Any) -> bool:
    """Whether `entry` is still the `name` entry Cardinal recorded writing
    (an absent entry or an empty record is never Cardinal's)."""
    record = managed.get(name) if isinstance(managed, dict) else None
    if not isinstance(record, dict) or not isinstance(entry, dict):
        return False
    return fingerprint(entry) == record


def write_entries(path: Path, mcp_url: str, api_key: str, plugin_version: str, behavior_script: Path,
                  managed: Any) -> tuple[dict, list[str]]:
    """The config with Cardinal's entries written in (returned, not saved),
    and warnings. An existing `cardinal` entry Cardinal did not write stops
    the write (ConfigError); one for `cardinal-behavior` is left alone with a
    warning. `managed`: the prior connection's record (state managed_mcp)."""
    data = load(path)
    servers = servers_of(data)
    warnings: list[str] = []
    existing = servers.get(MCP_SERVER_NAME)
    if existing is not None and not is_managed(MCP_SERVER_NAME, existing, managed):
        raise ConfigError(
            f"{path} already has an mcpServers.{MCP_SERVER_NAME} entry that cardinal-connect did not "
            "write. Remove or rename that entry, then rerun.")
    servers[MCP_SERVER_NAME] = build_mcp_entry(mcp_url, api_key, plugin_version)
    current = servers.get(BEHAVIOR_SERVER_NAME)
    if current is not None and not is_managed(BEHAVIOR_SERVER_NAME, current, managed) \
            and current != build_behavior_entry(behavior_script):
        warnings.append(f"{path} already has an mcpServers.{BEHAVIOR_SERVER_NAME} entry cardinal-connect "
                        "did not write; left as it is.")
    else:
        servers[BEHAVIOR_SERVER_NAME] = build_behavior_entry(behavior_script)
    data["mcpServers"] = servers
    return data, warnings


def managed_record(data: dict, behavior_script: Path) -> dict:
    """The `managed_mcp` record for state after write_entries: the
    fingerprint of each entry in `data` that is the one Cardinal writes."""
    servers = servers_of(data)
    out: dict = {}
    gateway = servers.get(MCP_SERVER_NAME)
    if isinstance(gateway, dict):
        out[MCP_SERVER_NAME] = fingerprint(gateway)
    if servers.get(BEHAVIOR_SERVER_NAME) == build_behavior_entry(behavior_script):
        out[BEHAVIOR_SERVER_NAME] = fingerprint(servers[BEHAVIOR_SERVER_NAME])
    return out


def remove_entries(path: Path, managed: Any) -> tuple[Optional[dict], list[str], list[str]]:
    """(the config without Cardinal's entries, or None when nothing changes;
    the names removed; the names kept because Cardinal did not write them)."""
    data = load(path)
    servers = servers_of(data)
    removed, kept = [], []
    for name in (MCP_SERVER_NAME, BEHAVIOR_SERVER_NAME):
        if name not in servers:
            continue
        if is_managed(name, servers[name], managed):
            servers.pop(name)
            removed.append(name)
        else:
            kept.append(name)
    if not removed:
        return None, removed, kept
    if servers:
        data["mcpServers"] = servers
    else:
        data.pop("mcpServers", None)
    return data, removed, kept


def gateway_key(path: Path) -> Optional[str]:
    """The MCP key in the `cardinal` entry's headers, or None. Never raises."""
    try:
        entry = servers_of(load(path)).get(MCP_SERVER_NAME)
    except ConfigError:
        return None
    headers = entry.get("headers") if isinstance(entry, dict) else None
    key = headers.get(API_KEY_HEADER) if isinstance(headers, dict) else None
    return key if isinstance(key, str) and key else None

"""Which Cardinal plugin these hooks run in, and whether the slim one should step aside.

The storyboard hooks (storyboard-session, storyboard-preview, storyboard-token,
evidence-capture) and bin/cardinal-evidence ship in two Claude Code plugins,
both built from adapters/claude:

  - `cardinal`              the full plugin (telemetry, limits, initiative,
                            decisions, storyboards; build/release.py claude)
  - `cardinal-storyboards`  the slim storyboards-only plugin
                            (build/release.py claude-storyboards;
                            adapters/claude-storyboards/compose.json)

The files are byte-identical in both. A hook tells them apart by the `name` in
its own ../.claude-plugin/plugin.json.

When both plugins are active, both sets of hooks fire on the same events.
Evidence would then be captured twice under two ev_ ids, and the preview
rendered twice. So the slim plugin's hooks do nothing whenever the full plugin
is active (`slim_should_yield`). The full plugin's hooks.json also matches the
slim plugin's MCP server (SLIM_SERVERS), so nothing is lost.

"The full plugin is active" means:
  1. Claude Code's effective `enabledPlugins` (~/.claude/settings.json, then
     <cwd>/.claude/settings.json, then <cwd>/.claude/settings.local.json; a
     later file wins per key) turns on a plugin named `cardinal`: True.
     Turned off there: False.
  2. Not listed there: the full plugin is installed
     (~/.claude/plugins/installed_plugins.json, or that file cannot be read)
     AND connected. Connected means CARDINAL_MCP_API_KEY is in the
     settings.json env or the environment, or /cardinal:connect's state file
     ~/.claude/cardinal.json records an MCP URL or key id.
A leftover key from an uninstalled full plugin does not silence the slim
hooks: point 2 also requires the full plugin to be installed.

Standard library only; never raises (every reader fails closed to "not
active", so the slim plugin keeps working).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FULL_PLUGIN = "cardinal"
SLIM_PLUGIN = "cardinal-storyboards"

# Claude Code names a plugin's MCP server "plugin_<plugin>_<server>" (it keeps
# "-"; the "_" spelling is accepted in case a client normalizes it).
FULL_SERVERS = ("cardinal", "plugin_cardinal_cardinal")
SLIM_SERVERS = ("plugin_cardinal-storyboards_cardinal", "plugin_cardinal_storyboards_cardinal")
# Every server that is Cardinal's own gateway: it mints witnessed receipts, so
# evidence capture skips it, and only it may hand the hooks a storyboard token.
CARDINAL_SERVERS = FULL_SERVERS + SLIM_SERVERS

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def plugin_name(root: Path = PLUGIN_ROOT):
    """The `name` in <root>/.claude-plugin/plugin.json, or None."""
    data = _read_json(root / ".claude-plugin" / "plugin.json")
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) else None


def is_slim(root: Path = PLUGIN_ROOT) -> bool:
    return plugin_name(root) == SLIM_PLUGIN


def _name_of(plugin_id) -> str:
    # "cardinal@cardinalhq-claude-plugin" -> "cardinal"
    return plugin_id.split("@", 1)[0] if isinstance(plugin_id, str) else ""


def _enabled_state(home: Path, cwd):
    """True / False from the effective enabledPlugins entry for `cardinal`,
    or None when no settings file names it."""
    files = [home / ".claude" / "settings.json"]
    if cwd:
        files += [Path(cwd) / ".claude" / "settings.json", Path(cwd) / ".claude" / "settings.local.json"]
    state = None
    for path in files:
        data = _read_json(path)
        plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
        if not isinstance(plugins, dict):
            continue
        for plugin_id, on in plugins.items():
            if _name_of(plugin_id) == FULL_PLUGIN:
                state = on is True
    return state


def _installed(home: Path) -> bool:
    """Whether Claude Code's installed_plugins.json lists the full plugin.
    Unreadable or absent counts as installed: the connection check decides."""
    data = _read_json(home / ".claude" / "plugins" / "installed_plugins.json")
    if not isinstance(data, dict):
        return True
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return True
    return any(_name_of(plugin_id) == FULL_PLUGIN for plugin_id in plugins)


def _connected(home: Path, environ) -> bool:
    data = _read_json(home / ".claude" / "settings.json")
    env = data.get("env") if isinstance(data, dict) else None
    if isinstance(env, dict) and isinstance(env.get("CARDINAL_MCP_API_KEY"), str) and env["CARDINAL_MCP_API_KEY"]:
        return True
    if environ.get("CARDINAL_MCP_API_KEY"):
        return True
    state = _read_json(home / ".claude" / "cardinal.json")
    return isinstance(state, dict) and bool(state.get("mcp_url") or state.get("mcp_key_id"))


def full_plugin_active(home: Path, environ=None, cwd=None) -> bool:
    environ = os.environ if environ is None else environ
    try:
        enabled = _enabled_state(home, cwd)
        if enabled is not None:
            return enabled
        return _installed(home) and _connected(home, environ)
    except Exception:
        return False


def slim_should_yield(home: Path, environ=None, cwd=None, root: Path = PLUGIN_ROOT) -> bool:
    """True when this copy is the slim plugin and the full plugin is active:
    the calling hook then does nothing."""
    try:
        return is_slim(root) and full_plugin_active(home, environ, cwd)
    except Exception:
        return False

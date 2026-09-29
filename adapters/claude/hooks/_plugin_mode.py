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
rendered twice. So exactly one copy runs (`should_yield`):

  - The full plugin is active AND connected: the slim plugin's hooks do
    nothing (`slim_should_yield`). The full plugin's hooks.json also matches
    the slim plugin's MCP server (SLIM_SERVERS), so nothing is lost.
  - The full plugin is not connected but the slim plugin is enabled: the full
    plugin's storyboard hooks do nothing (`full_should_yield`). Every
    storyboard call then goes to the slim plugin's server (the full plugin's
    .mcp.json is ${CARDINAL_MCP_URL}, unset), and only the slim copy knows
    that Cardinal's URL (its literal .mcp.json).

"The full plugin is active" means:
  1. Claude Code's effective `enabledPlugins` (~/.claude/settings.json, then
     <cwd>/.claude/settings.json, then <cwd>/.claude/settings.local.json; a
     later file wins per key) does not turn off the plugin named `cardinal`.
     Turned off there: never active.
  2. Not listed there: the full plugin is installed
     (~/.claude/plugins/installed_plugins.json, or that file cannot be read).
  3. And, in either case, connected: CARDINAL_MCP_API_KEY is in the
     settings.json env or the environment, or /cardinal:connect's state file
     ~/.claude/cardinal.json records an MCP URL or key id. `/plugin install`
     turns the full plugin on in enabledPlugins before /cardinal:connect ever
     ran, and that alone must not silence the slim hooks.
A leftover key from an uninstalled full plugin does not silence the slim
hooks: point 2 also requires the full plugin to be installed.

"The slim plugin is enabled" means its effective enabledPlugins entry is on,
or, not listed there, installed_plugins.json lists it (unreadable: no, so the
full plugin keeps working).

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


def _enabled_state(home: Path, cwd, name: str = FULL_PLUGIN):
    """True / False from the effective enabledPlugins entry for plugin `name`,
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
            if _name_of(plugin_id) == name:
                state = on is True
    return state


def _installed_plugins(home: Path):
    """installed_plugins.json's `plugins` map, or None when unreadable."""
    data = _read_json(home / ".claude" / "plugins" / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, dict) else None


def _installed(home: Path, name: str = FULL_PLUGIN, unreadable: bool = True) -> bool:
    """Whether Claude Code's installed_plugins.json lists plugin `name`.
    Unreadable or absent counts as `unreadable`."""
    plugins = _installed_plugins(home)
    if plugins is None:
        return unreadable
    return any(_name_of(plugin_id) == name for plugin_id in plugins)


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
        enabled = _enabled_state(home, cwd, FULL_PLUGIN)
        if enabled is False:
            return False
        if enabled is None and not _installed(home, FULL_PLUGIN):
            return False
        # Enabled (or installed) is not enough: /plugin install enables the
        # full plugin before /cardinal:connect, and until then its copy has no
        # Cardinal URL to render or upload with.
        return _connected(home, environ)
    except Exception:
        return False


def slim_plugin_enabled(home: Path, cwd=None) -> bool:
    try:
        enabled = _enabled_state(home, cwd, SLIM_PLUGIN)
        if enabled is not None:
            return enabled
        return _installed(home, SLIM_PLUGIN, unreadable=False)
    except Exception:
        return False


def slim_should_yield(home: Path, environ=None, cwd=None, root: Path = PLUGIN_ROOT) -> bool:
    """True when this copy is the slim plugin and the full plugin is active
    (enabled and connected): the calling hook then does nothing."""
    try:
        return is_slim(root) and full_plugin_active(home, environ, cwd)
    except Exception:
        return False


def full_should_yield(home: Path, environ=None, cwd=None, root: Path = PLUGIN_ROOT) -> bool:
    """True when this copy is the full plugin, it was never connected, and the
    slim plugin is enabled: the slim copy (which knows its Cardinal) handles
    the storyboard hooks, and the calling hook does nothing."""
    environ = os.environ if environ is None else environ
    try:
        return plugin_name(root) == FULL_PLUGIN and not _connected(home, environ) and slim_plugin_enabled(home, cwd)
    except Exception:
        return False


def should_yield(home: Path, environ=None, cwd=None, root: Path = PLUGIN_ROOT) -> bool:
    """Whether this copy of a storyboard hook steps aside for the other
    plugin's copy. At most one copy runs for any one tool call."""
    return slim_should_yield(home, environ, cwd, root) or full_should_yield(home, environ, cwd, root)

# Cardinal for OpenCode

Session, model usage, and tool telemetry plus Cardinal's remote MCP tools.
Supports **OpenCode 1.18.30 / the 1.x server-plugin API**, Node 20+, and
Python 3.9+ on macOS/Linux. OpenCode V2 is not yet supported.

## Install from this repository

From the repository root:

```sh
python3 build/native.py opencode
node dist/native/opencode/bin/cardinal-opencode.js connect
```

Add the built entry point to your OpenCode config's `plugin` array, preserving
any existing plugins (replace `/absolute/path` with your checkout):

```json
{
  "plugin": [
    "file:///absolute/path/cardinal-agent-plugins/dist/native/opencode/index.js"
  ]
}
```

Restart OpenCode. The plugin supplies `mcp.cardinal` to the runtime config
in memory using the saved credential; it does not rewrite your config or
put credentials in project files. An existing `mcp.cardinal` entry is retained.

To produce a portable artifact:

```sh
npm pack ./dist/native/opencode --pack-destination ./dist/native
```

The tarball can be installed with `npm install /path/to/artifact.tgz` into an
installation directory. Point OpenCode at that directory's
`node_modules/@cardinalhq/opencode-plugin/index.js`. Its `node_modules/.bin/cardinal-opencode`
command manages the connection.

After the package is published to npm, the plugin config may instead name
`@cardinalhq/opencode-plugin@0.1.0` and connection commands can use
`npx --package @cardinalhq/opencode-plugin cardinal-opencode connect`.
The initial implementation does not publish the package automatically.

## Connection commands

```sh
cardinal-opencode connect [--host https://app.cardinalhq.io] [--telemetry-only]
cardinal-opencode status
cardinal-opencode disconnect
```

Use the built `bin/cardinal-opencode.js` with `node` if the command is not on PATH.
Connect uses browser consent for `ingest:write` and, by default, `mcp:invoke`.
Status probes both endpoints. Disconnect revokes the MCP key and deletes the
local connection. Ingest-key revocation requires Cardinal's API-key settings;
disconnect prints the URL and retains only the key IDs in `cardinal-revocations.json`.
`disconnect --local-only` skips remote revocation. Restart OpenCode after changing
the connection. Existing non-Cardinal settings are untouched.

State lives in `$XDG_CONFIG_HOME/opencode` (default `~/.config/opencode`):
`cardinal.json` contains connection metadata and `cardinal-secrets.json` has
mode 0600. Set `CARDINAL_OPENCODE_HOME` to an absolute path to override the
Cardinal state directory; set `CARDINAL_PYTHON` to select a Python executable.

## Telemetry

- `cardinal.git_state`: repository, branch, HEAD, cwd, initiative attribution.
- `api_request` and `cardinal.turn_usage`: completed assistant-message usage,
  model/provider, cache and reasoning tokens, runtime-reported cost.
- `cardinal.turn_tool` and `tool_result`: completed/failed tool calls,
  duration, sequence IDs, and a coarse shell-command class.
- Native child sessions retain `parent_session_id` when supplied by OpenCode.

The adapter consumes final message and tool-part events, not streaming deltas.
It filters events to the plugin's workspace and uses persistent, bounded event
deduplication across reloads. Telemetry is best-effort: the queue is bounded,
delivery is not retried, and failures never interrupt the agent. No prompts,
assistant text, tool outputs, or raw shell commands are emitted. Unknown cost
is omitted; a runtime-reported cost of zero is preserved.

Spend enforcement, subscription-plan reporting, subagent cost aggregation,
and skill/command attribution are not part of this initial adapter. See
`docs/RELEASING-NATIVE.md` in the source repository for the backend rollout
requirements: the session analytics service must accept the `opencode` runtime.

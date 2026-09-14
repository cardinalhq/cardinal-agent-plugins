# Cardinal for OpenCode

Session, model usage, and tool telemetry plus Cardinal's remote MCP tools.
Supports **OpenCode 1.18.30 / the 1.x server-plugin API**, Node 20+, and
Python 3.9+ on macOS/Linux. OpenCode V2 is not yet supported.

## Install from this repository

From the repository root:

```sh
python3 build/native.py opencode
npm install --omit=dev --prefix ./dist/native/opencode
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

- `cardinal.git_state`: repository, branch, HEAD, cwd, initiative attribution,
  and the branch's PR (`cardinal_pr_number`, `cardinal_pr_url`) when `gh pr view`
  resolves one. The lookup is cached per repo + branch (10 min, 2 min for a miss),
  bounded to 1.5s, and skipped on protected branches; no PR means no PR keys.
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

## Decision capture

Off by default. `cardinal-opencode decision on|off|status [--session ID]` toggles it;
`CARDINAL_DECISIONS=1/0` in OpenCode's environment overrides the switch. While it
is on, the plugin appends the recording instructions and this session's decision
ledger to the system prompt through `experimental.chat.system.transform`, and the
agent records choices with the `cardinal_record_decision` tool. Each recorded
decision is one `cardinal.decision` event (repo, branch, head sha, PR, anchors,
code clusters) and is kept in the ledger under `cardinal/decisions/`. The same
record is available as `cardinal-opencode decision record --session ID --choice …`.

Limits: the system-prompt hook is marked experimental in `@opencode-ai/plugin`
1.18.30 and may change. The ledger is refreshed on each new user message and after
the tool records a decision. The tool is registered in every session, and returns
an error while capture is off. It needs the package's `zod` dependency; an install
without dependencies gets instructions to run the CLI through the shell instead.

Spend enforcement, subscription-plan reporting, subagent cost aggregation,
and skill/command attribution are not part of this initial adapter. See
`docs/RELEASING-NATIVE.md` in the source repository for the backend rollout
requirements: the session analytics service must accept the `opencode` runtime.

# Cardinal for Pi

Session, model usage, and tool telemetry plus Cardinal MCP access through a
native Pi extension. Tested with **`@earendil-works/pi-coding-agent` 0.85.1**,
Node 22.19+, and Python 3.9+ on macOS/Linux. The older
`@mariozechner/pi-coding-agent` distribution is not part of this compatibility target.

## Install from this repository

From the repository root:

```sh
python3 build/native.py pi
npm install --omit=dev --prefix ./dist/native/pi
pi install ./dist/native/pi
node dist/native/pi/bin/cardinal-pi.js connect
```

Restart Pi. To produce a portable package:

```sh
npm pack ./dist/native/pi --pack-destination ./dist/native
```

Install the tarball with `npm install /path/to/artifact.tgz` into an installation
directory, then run `pi install /absolute/installation/directory/node_modules/@cardinalhq/pi-plugin`.
Its `node_modules/.bin/cardinal-pi` command manages the connection.

After publication, installation can use `pi install npm:@cardinalhq/pi-plugin@0.2.0`
and `npx --package @cardinalhq/pi-plugin cardinal-pi connect`.
The initial implementation does not publish the package automatically.

## Cardinal tools

Pi does not have a built-in MCP client. This extension provides two tools:

- `cardinal_list_tools`: discover the connected account's tools and input schemas.
- `cardinal_call_tool`: call a discovered tool with its exact name and arguments.

The bundled MCP SDK uses authenticated Streamable HTTP, connects lazily on
the first tool invocation, follows discovery pagination, propagates tool
failures, and closes on session shutdown. It rejects redirects so the Cardinal
credential cannot be forwarded to another endpoint. No separate MCP extension
is required. A `--telemetry-only` connection leaves these tools unavailable
until MCP consent is granted through a new connection.

## Connection commands

```sh
cardinal-pi connect [--host https://app.cardinalhq.io] [--telemetry-only]
cardinal-pi status
cardinal-pi disconnect
```

Use the built `bin/cardinal-pi.js` with `node` if the command is not on PATH.
Connect uses browser consent for `ingest:write` and, by default, `mcp:invoke`.
Status probes both endpoints. Disconnect revokes the MCP key and removes the
local connection. Ingest-key revocation requires Cardinal's API-key settings;
disconnect prints the URL and retains only the key IDs in `cardinal-revocations.json`.
`disconnect --local-only` skips remote revocation. Restart Pi after changing
the connection.

State lives in `$PI_CODING_AGENT_DIR` (default `~/.pi/agent`): `cardinal.json`
contains connection metadata and `cardinal-secrets.json` has mode 0600.
Set `CARDINAL_PI_HOME` to an absolute path to override the Cardinal state
directory; set `CARDINAL_PYTHON` to select a Python executable. Pi's existing
settings and other extensions are preserved.

## Telemetry

Emits the same five events as the OpenCode adapter: `cardinal.git_state`,
`api_request`, `cardinal.turn_usage`, `cardinal.turn_tool`, and `tool_result`.
`cardinal.git_state` carries the branch's PR (`cardinal_pr_number`,
`cardinal_pr_url`) when `gh pr view` resolves one. The lookup is cached per
repo + branch, bounded to 1.5s, and never fails the record.
Model, provider, token/cache usage and runtime-reported cost come from finalized
assistant messages. Tool events include failures, duration and a coarse shell
class. Cardinal MCP calls include the remote server and tool identity.

Streaming updates do not emit usage. Session IDs come from Pi's session manager;
model and tool sequence IDs are captured at lifecycle boundaries. Persistent
deduplication prevents repeated terminal messages from inflating usage.
Telemetry is best-effort with bounded queues and deduplication history; no
delivery retry is attempted. Prompts, assistant text, tool outputs and raw shell
commands are not emitted. Unknown cost is omitted; a runtime-reported zero is retained.

## Decision capture

Off by default. `cardinal-pi decision on|off|status [--session ID]` toggles it;
`CARDINAL_DECISIONS=1/0` in Pi's environment overrides the switch. While it is on,
the extension's `before_agent_start` handler appends the recording instructions and
this session's decision ledger to that turn's system prompt, and the agent records
choices with the `cardinal_record_decision` tool. Each recorded decision is one
`cardinal.decision` event (repo, branch, head sha, PR, anchors, code clusters) and
is kept in the ledger under `cardinal/decisions/`. The same record is available as
`cardinal-pi decision record --session ID --choice …`. The tool is registered in
every session and returns an error while capture is off. When capture is on, each
prompt waits up to 1.5s for the ledger lookup.

Spend enforcement, subscription-plan reporting, third-party subagent extension
attribution, and skill/command attribution are not part of this initial adapter.
See `docs/RELEASING-NATIVE.md` in the source repository for backend rollout
requirements: the session analytics service must accept the `pi` runtime.

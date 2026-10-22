# Cardinal GitHub Copilot CLI plugin

Connect GitHub Copilot CLI to Cardinal telemetry and the unified MCP endpoint in
one browser-approved consent.

This is a Copilot-CLI-native port of the command surface shared by the
[Claude Code](https://github.com/cardinalhq/cardinal-claude-plugin),
[Codex](https://github.com/cardinalhq/cardinal-codex-plugin),
[Cursor](https://github.com/cardinalhq/cardinal-cursor-plugin) and
[Gemini CLI](https://github.com/cardinalhq/cardinal-gemini-plugin) plugins. It is
built from the Cursor adapter (the closest host: both have no context channel
on the prompt hook and no safe turn-end block) over the shared
`cardinal_core`.

| Script | What it does |
| --- | --- |
| `cardinal-connect` | Runs Cardinal's device-code flow, mints ingest and MCP keys, and writes the managed `cardinal` (remote gateway) and `cardinal-behavior` (local stdio) entries into `~/.copilot/mcp-config.json` (`$COPILOT_HOME/mcp-config.json` when set). |
| `cardinal-status` | Shows the recorded Cardinal workspace and probes the configured ingest and MCP endpoints. |
| `cardinal-disconnect` | Best-effort revokes Cardinal keys, removes the managed MCP entries and deletes local state. |
| `cardinal-decision` | Opt-in decision capture: `on` / `off` / `status` / `record`. |
| `cardinal-connect --owner-input on\|off` | Your consent to recording the prompts you type as owner input of the session's Investigation. Doesn't connect. |
| `cardinal-evidence`, `cardinal-storyboard`, `cardinal-checks`, `cardinal-behavior`, `cardinal-install-site` | The shared evidence, storyboard, Checks preview and site-install CLIs. |

Skills (`skills/`): `cardinal-connect`, `cardinal-disconnect`, `cardinal-status`,
`cardinal-decision` (status only), `cardinal-storyboard`, `canvas`, `supervise`,
`cardinal-install-site`, `cardinal-migrate-from-grafana`,
`cardinal-optimize-toolkit`. Custom agent (`agents/`): `brainstorm`.

## Install

The plugin uses Copilot CLI's plugin format (`.plugin/plugin.json`, with
`skills/`, `agents/` and `hooks/hooks.json`). Users install it from the release
mirror, which is also a Copilot plugin marketplace:

```sh
copilot plugin marketplace add cardinalhq/cardinal-copilot-plugin
copilot plugin install cardinal@cardinal-copilot-plugin
```

Then connect, in a Copilot CLI session (the `cardinal-connect` skill) or in your
own terminal:

```sh
python3 ~/.copilot/installed-plugins/cardinal-copilot-plugin/cardinal/scripts/cardinal-connect
```

and start a new session (or `/restart`). `copilot plugin update cardinal`
picks up later releases.

### From a checkout (development)

```sh
python3 build/vendor.py copilot          # vendor cardinal_core into hooks/
copilot --plugin-dir adapters/copilot    # load the plugin for one session
```

`--plugin-dir` loads the plugin live (no install); hooks, skills and agents
come from the checkout. Run `scripts/cardinal-connect` from the checkout once
to write the MCP entries; they point at the checkout's `scripts/cardinal-behavior`.

## Hooks

`hooks/hooks.json` ships with the plugin (Copilot hook format, `version: 1`,
camelCase events). Every entry runs
`python3 "$CARDINAL_COPILOT_PLUGIN_ROOT/hooks/cardinal-copilot-telemetry.py" --event <event>`,
with `env` mapping `CARDINAL_COPILOT_PLUGIN_ROOT` to `${PLUGIN_ROOT}` (the
pattern Copilot's own bundled plugins use), and ends in `|| true`: Copilot
**denies** a tool call when a `preToolUse` hook exits non-zero, so the script
can never be why a tool fails.

| Event | What the plugin does |
| --- | --- |
| `sessionStart` | Initiative branch-naming convention, spend-limit standing, decision-capture instructions, the Investigation bootstrap + guidance + poller, related storyboards, Checks rule (`additionalContext`) |
| `userPromptSubmitted` | `cardinal.git_state` with PR, a new user turn, owner input (with consent), the discovery re-look and the spend-limit notice (both staged) — prints nothing: Copilot drops a config-file hook's output here |
| `preToolUse` | Denies the call while a Cardinal spend-limit block is in force; fills an absent `session_id` / `context` on Cardinal's storyboard tools (`modifiedArgs`). Never answers `allow` |
| `postToolUse` | Local evidence capture, `cardinal.turn_tool` + `tool_result`, decision recording, edited files + edit lookup, staged notices and the Investigation's deliveries (`additionalContext`) |
| `postToolUse` (`--role authoring`, matcher `.*storyboard__(preview\|publish)`, 150 s) | Local storyboard preview render and hero upload |
| `postToolUseFailure` | Evidence capture of the failed call + deliveries |
| `agentStop` | The Investigation's turn end (flush, sync check); stages, never blocks |
| `subagentStart` | The stored discovery block for the subagent |
| `subagentStop` | `cardinal.subagent_usage` |
| `preCompact` | `cardinal.plan_usage` compaction trigger |

Until `cardinal-connect` has written `~/.copilot/cardinal.json`, the hooks only
capture evidence locally and give a one-time connect hint: no telemetry, no
network, no other context.

## Telemetry scope

The plugin sends the Cardinal event contract shared with the sibling plugins
(see `docs/specs/adapter-parity.md` and `docs/parity.md` at the repository
root): `cardinal.git_state` (with `cardinal_pr_number` / `cardinal_pr_url`),
`cardinal.turn_tool` + `tool_result` (Copilot's `bash` / `view` / `edit` /
`create` / ... mapped to `Bash` / `Read` / `Edit` / `Write` / ..., MCP tools as
`mcp__<server>__<tool>`), `cardinal.subagent_usage`, `cardinal.plan_usage`
(compaction) and `cardinal.decision`. Resources carry `agent.runtime=copilot`.

Copilot names an MCP tool `<server>-<tool>` (`github-mcp-server-search_code`).
The hook rewrites it to `mcp__<server>__<tool>` against the servers it knows
(Cardinal's, `github-mcp-server`, and the names in `mcp-config.json`,
`.mcp.json` and `.github/mcp.json`), longest name first.

Every network send leaves the hook for a detached background process (0600
spool under `~/.copilot/cardinal/spool/`); the only synchronous network call is
the PR lookup on `userPromptSubmitted` (1.5 s cap, cached).

## Limits on Copilot CLI

- **No token usage.** No hook payload carries token counts or the model, and
  Copilot's per-call `assistant.usage` events are ephemeral (never written to
  the session's `events.jsonl`; see the bundled `session-events.schema.json`).
  No `cardinal.turn_usage` / `api_request`, no cached tokens, no plan or
  rate-limit stamp. Copilot CLI's own OpenTelemetry export
  (`copilot help monitoring`) carries token metrics; pointing it at Cardinal is
  a follow-up, not part of this plugin.
- **No context or block on the prompt.** `userPromptSubmitted` output from a
  config-file hook is dropped
  ([hooks reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference)).
  Spend-limit warnings, the decision ledger refresh and discovery re-looks are
  staged and arrive at the next tool call; a spend-limit block denies tool
  calls instead of the prompt (`CARDINAL_COPILOT_STRICT_WARN=1` escalates a warn
  to a deny).
- **No relayed owner decisions.** Cardinal relays `decide: …` / `approve` /
  `reject` by holding the prompt back from the model; Copilot cannot, so the
  owner posts decisions from their Cardinal web session.
- **No turn-end block.** `agentStop` can block, but Copilot does not document
  whether the reason re-enters as hook-originated text or as a user turn
  (plan R1, `docs/plans/cross-agent-parity.md`), so turn-end deliveries are
  staged for the next tool call or session start.
- **No per-session owner-input disclosure.** `sessionStart` context reaches
  only the model, so owner input needs your connect-time consent
  (`cardinal-connect --owner-input on`), as on Cursor.
- **Subagent tool calls are not told apart.** Copilot documents no agent id on
  tool hooks, so a subagent's tool call may be handed the main thread's
  advisories.
- **Checks preview on / off applies to new sessions.** No hook output reloads
  skills, and no documented tool names a skill being used, so there is nothing
  to deny in a running session.
- **No step backs.** A read-only headless `copilot -p` pass was not verified,
  so the supervise skill keeps the plan and tells the owner a step back needs a
  Claude, Codex or Gemini supervisor.
- **Command sandbox.** With `/sandbox enable` (off by default) agent shell
  commands have no network: the connect / status / evidence / storyboard CLIs
  need a per-command bypass. Whether Copilot also sandboxes hooks was not
  verified; if it does, telemetry sends fail quietly while it is on.
- **Windows** is not supported (local storyboard preview needs macOS or Linux;
  the hooks have `powershell` commands but are untested there).

## Owner input (opt-in consent)

Owner input is recorded only after you consent in your own terminal:

```sh
python3 <plugin directory>/scripts/cardinal-connect --owner-input on   # prints the text, asks you to type yes
python3 <plugin directory>/scripts/cardinal-connect --owner-input off
```

`CARDINAL_OWNER_INPUT=0` or `{"copilot": "off"}` in `~/.cardinal/owner-input.json`
turns it off whatever the consent says.

## Decision capture (opt-in)

`cardinal-decision on|off|status` in your own terminal. When on, the
`sessionStart` context tells the agent to run `cardinal-decision record …`; the
CLI only validates and prints a marker line (so it works in Copilot's command
sandbox), and the `postToolUse` hook re-parses the command it ran, records the
decision in the session ledger and emits `cardinal.decision`.

## Evidence capture

Every tool call (built-in tools, any MCP server, except Cardinal's own gateway)
goes through the shared pipeline into `~/.cardinal/evidence/<session>/ev_<id>.json`
(0700/0600, scrubbed, capped, withheld when sensitive), and its id is returned
as context. Nothing leaves the machine until `scripts/cardinal-evidence promote`.
Opt out with `CARDINAL_EVIDENCE_CAPTURE=0` or `~/.cardinal/evidence/disabled`.

## Payload capture and verification

The hook contract here comes from GitHub's
[hooks reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference),
the [plugin reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)
and the bundled plugins and schemas of Copilot CLI 1.0.103; it has not yet been
exercised in a live Copilot session. Set `CARDINAL_COPILOT_DEBUG_PAYLOADS=1`
before starting Copilot CLI to dump every raw hook payload to
`~/.copilot/cardinal/telemetry/debug/<event>-<ns>.json`. Things to confirm on
first live use: the `toolArgs` shape (object or JSON string), MCP tool naming,
that `modifiedArgs` without a `permissionDecision` is applied, and the
`subagentStart` / `subagentStop` session id.

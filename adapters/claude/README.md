# cardinal (Claude Code plugin)

Cardinal's Claude Code plugin: the `cardinal` MCP server, Investigation
Storyboards with local preview and captured evidence, and (once connected)
session telemetry, spend limits and initiative attribution.

## Two modes

```
                    before /cardinal:connect          after /cardinal:connect
                    ------------------------          -----------------------
MCP server          https://app.cardinalhq.io/mcp     the org's URL
                    (Cardinal Cloud, org-less)        (CARDINAL_MCP_URL)
sign-in             MCP OAuth, run by Claude Code     the org's API key
                    (browser, via /mcp)               (CARDINAL_MCP_API_KEY)
storyboards,        yes (preview_token /              yes (API key, or the
local preview,      evidence_token from the tools)    same tokens)
evidence capture
telemetry, spend    off: those hooks exit at once,    on
limits, initiative, with no context and no network
plan, decision,
git-state, usage
```

- **Not connected.** Install the plugin and use it. The first Cardinal tool
  call opens Claude Code's OAuth sign-in for Cardinal Cloud (or run `/mcp`).
  Storyboards, `storyboard__preview` with local PNG rendering, and
  `cardinal-evidence` work with the storyboard-scoped tokens the tools return.
- **Connected.** `/cardinal:connect` picks the org, writes its MCP URL and API
  key and the OTel ingest settings into `~/.claude/settings.json` `env`, and
  turns on telemetry (Outcomes Dashboard), spend limits, initiative, plan,
  decision and usage hooks. `/cardinal:disconnect` goes back.

**Self-hosted Cardinal must connect** (`/cardinal:connect --host <url>`): the
unconnected default is Cardinal Cloud.

The switch is `hooks/_connection.py`: connected means a `CARDINAL_MCP_API_KEY`,
a Cardinal ingest key in `OTEL_EXPORTER_OTLP_HEADERS` (settings `env` or the
environment), or the connect state file `~/.claude/cardinal.json`.

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

- **Not connected.** Install the plugin, then sign in to Cardinal Cloud:
  run `/mcp`, pick `cardinal` and authenticate in the browser (or let Claude
  call the server's `authenticate` tool, which opens the same sign-in). The
  Cardinal tools appear once you have authenticated. Storyboards,
  `storyboard__preview` with local PNG rendering, and `cardinal-evidence`
  work with the storyboard-scoped tokens the tools return.
- **Evidence capture.** In either mode, `hooks/evidence-capture.py` stores
  the results of calls to *other* MCP servers locally, under
  `~/.cardinal/evidence/<session_id>/` (credentials scrubbed, removed after
  14 days). Nothing is uploaded automatically: only a result a storyboard
  cites is uploaded, by `cardinal-evidence promote`.
- **Connected.** `/cardinal:connect` picks the org, writes its MCP URL and API
  key and the OTel ingest settings into `~/.claude/settings.json` `env`, and
  turns on telemetry (Outcomes Dashboard), spend limits, initiative, plan,
  decision and usage hooks. `/cardinal:disconnect` goes back.

**Self-hosted Cardinal must connect** (`/cardinal:connect --host <url>`): the
unconnected default is Cardinal Cloud.

**A key without a URL goes to Cardinal Cloud.** `.mcp.json` sends
`CARDINAL_MCP_API_KEY` to `${CARDINAL_MCP_URL:-https://app.cardinalhq.io/mcp}`
and cannot make the header conditional on the URL. So if
`CARDINAL_MCP_API_KEY` is set but `CARDINAL_MCP_URL` is not — say you
exported the key in a shell profile for a manual Codex config, or it is a
self-hosted maestro's key — Claude Code sends that key to Cardinal Cloud.
`/cardinal:connect` writes both (except `--telemetry-only`, which writes
neither), so only a key set outside it is affected. The plugin warns when it sees this (once per session, at session
start, and in `/cardinal:status`); fix it by running `/cardinal:connect`
(`--host <your maestro URL>` for self-hosted) or unsetting the key.

**After `/cardinal:disconnect`** the `cardinal` server falls back to Cardinal
Cloud's `/mcp` over OAuth (and may reconnect silently with a stored OAuth
token). Disable `cardinal` in `/mcp` if you don't want it.

The switch is `hooks/_connection.py`: connected means a `CARDINAL_MCP_API_KEY`,
a Cardinal ingest key in `OTEL_EXPORTER_OTLP_HEADERS` (settings `env` or the
environment), or the connect state file `~/.claude/cardinal.json`.

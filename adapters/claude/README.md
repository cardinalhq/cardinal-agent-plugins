# cardinal (Claude Code plugin)

Cardinal's Claude Code plugin: the `cardinal` MCP server, Investigation
Storyboards with local preview and captured evidence, and (once connected)
session telemetry, spend limits and initiative attribution.

## Two modes

```
                    before /cardinal:connect          after /cardinal:connect
                    ------------------------          -----------------------
MCP server          none: no URL, never connects      the org's URL
                    (/mcp: missing CARDINAL_MCP_URL)  (CARDINAL_MCP_URL)
credential          none                              the org's API key
                                                      (CARDINAL_MCP_API_KEY)
storyboards         no (no Cardinal tools)            yes
evidence capture    yes, local only                   yes; cited results
                                                      uploaded on promote
telemetry, spend    off: those hooks exit at once,    on
limits, initiative, with no context and no network
plan, decision,
git-state, usage
```

- **Not connected (local-only).** `.mcp.json` takes its URL from
  `${CARDINAL_MCP_URL}`, so with it unset the `cardinal` server has no URL.
  Claude Code contacts nothing and lists the server in `/mcp` as failed with
  "Missing environment variables: CARDINAL_MCP_URL". That entry is expected:
  Claude Code has a quiet "not configured" state only for a literally empty
  URL, which a plugin whose URL comes from an env var cannot declare. The
  session-start hook says once how to connect and keeps one line of context
  for later sessions; `/cardinal:status` says the same.
- **Getting write access.** Writes (Cardinal's tools, publishing
  storyboards) need an API key; there is no OAuth sign-in. Sign up at
  `https://app.cardinalhq.io` (a personal workspace is created), create an
  API key, then run `/cardinal:connect`. Self-hosted Cardinal:
  `/cardinal:connect --host <url>`. Published storyboards are read without
  an account through a public link when the org allows them.
- **Evidence capture.** In either mode, `hooks/evidence-capture.py` stores
  the results of calls to *other* MCP servers locally, under
  `~/.cardinal/evidence/<session_id>/` (credentials scrubbed, removed after
  14 days). Nothing is uploaded automatically: only a result a storyboard
  cites is uploaded, by `cardinal-evidence promote`, which needs a connection.
- **Connected.** `/cardinal:connect` picks the org, writes its MCP URL and API
  key and the OTel ingest settings into `~/.claude/settings.json` `env`, and
  turns on telemetry (Outcomes Dashboard), spend limits, initiative, plan,
  decision and usage hooks. `/cardinal:disconnect` goes back to local-only.

**A key without a URL (stray key).** If `CARDINAL_MCP_API_KEY` is set but
`CARDINAL_MCP_URL` is not — say you exported the key in a shell profile for a
manual Codex config — the `cardinal` server still has no URL (the key is sent
nowhere), but the hooks count the machine as connected and Cardinal's tools
are missing. `/cardinal:connect` writes both (except `--telemetry-only`,
which writes neither). The plugin warns when it sees this (once per session,
at session start, and in `/cardinal:status`); fix it by running
`/cardinal:connect` (`--host <your maestro URL>` for self-hosted) or unsetting
the key.

The switch is `hooks/_connection.py`: connected means a `CARDINAL_MCP_API_KEY`,
a Cardinal ingest key in `OTEL_EXPORTER_OTLP_HEADERS` (settings `env` or the
environment), or the connect state file `~/.claude/cardinal.json`.

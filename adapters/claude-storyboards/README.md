# cardinal-storyboards

Cardinal Investigation Storyboards for Claude Code, without the rest of the
Cardinal plugin. Turn an investigation into an evidence-bound, scene-by-scene
storyboard, preview every scene on your machine, and publish it in Cardinal.

## What it installs

- **MCP server `cardinal`** at `https://app.cardinalhq.io/mcp`. Claude Code
  signs you in with OAuth the first time you use it (`/mcp`); no API key is
  stored in your settings.
- **Skills** `storyboard` and `canvas`.
- **Hooks**
  - `storyboard-session`: puts this session's id in context for `storyboard__create`.
  - `evidence-capture`: keeps results from your *other* MCP servers on this machine
    (`~/.cardinal/evidence/`, scrubbed, removed after 14 days). Nothing is uploaded
    until you cite it.
  - `storyboard-token`: keeps the storyboard-scoped evidence token that
    `storyboard__create` returns (24 h, that storyboard's evidence upload only).
  - `storyboard-preview`: renders each scene of a `storyboard__preview` in your local,
    sandboxed, network-locked Chrome/Chromium, fetching the pages with the result's
    own `preview_token` (10 min).
- **CLI** `cardinal-evidence` (`promote`, `list`, `status`, `off`, `on`): uploads the
  captured results a storyboard cites, with the evidence token.

It sends no telemetry and has no limits, initiative, decision, git-state or usage
hooks.

## With the full `cardinal` plugin

Install one or the other. If the full `cardinal` plugin is enabled (or connected
with `/cardinal:connect`), this plugin's hooks do nothing and the full plugin's
hooks handle storyboards, so evidence is never captured twice.

## Turn capture off

`cardinal-evidence off` (or `CARDINAL_EVIDENCE_CAPTURE=0`). `cardinal-evidence on`
turns it back on.

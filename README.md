# cardinal-agent-plugins

Source of truth for Cardinal's agent plugins and their shared core.

One contract, written once, shipped to every agent runtime:

```
core/cardinal_core/     shared runtime — OTLP emission, initiative
                        resolution, spend limits, device consent,
                        session counters
adapters/               per-agent surfaces (codex, gemini, cursor,
                        claude, opencode, pi, devin)
build/vendor.py         copies core into each plugin artifact so shipped
                        plugins stay self-contained (no pip for users)
docs/specs/             the extraction spec and migration plan
```

## Status

P0–P4 code-complete: core extracted (52 unit tests) and all four adapters
migrated with byte-equal golden parity against their shipped plugins
(~226 adapter tests + a cross-adapter contract test in `tests/`). See
`docs/specs/agent-core.md` for the plan and
`docs/specs/core-gaps-followup.md` for the core 0.2.0 reconciliation.

The Claude adapter also ships an auto-installed Invariant `PreToolUse`
hook (`adapters/claude/hooks/invariant-check.py`) — advisory-only, fires
before Edit/Write/MultiEdit on code covered by an Invariant guarantee.

Claude also ships opt-in decision capture (`cardinal-decision on`): a
`UserPromptSubmit` hook asks the agent to record the choices it makes with
`bin/cardinal-decision`, and each one is sent as a `cardinal.decision` event
tagged with engineer, session, repo, branch, PR, anchors, and D18 code
clusters. See [docs/specs/decision-telemetry.md](docs/specs/decision-telemetry.md).

Before `/cardinal:connect` the Claude plugin is local-only: its `cardinal` MCP
server has no URL and never connects, evidence capture keeps other MCP servers'
results on the machine, and every telemetry, spend-limit, initiative, plan,
decision, git-state and usage hook stays silent (no context, no network).
Writing to Cardinal needs an API key (sign up at https://app.cardinalhq.io,
then `/cardinal:connect` creates one for the machine); there is no OAuth sign-in. See
[adapters/claude/README.md](adapters/claude/README.md).

Claude also ships two model-invocable skills for Cardinal's Investigation
Storyboards: [`/cardinal:storyboard`](adapters/claude/skills/storyboard/README.md)
turns an investigation into an evidence-bound, scene-by-scene storyboard
through the `storyboard__*` MCP tools, and
[`/cardinal:canvas`](adapters/claude/skills/canvas/README.md) draws each
scene's visual and previews it. `storyboard__preview` returns one
self-contained page per scene. A synchronous `PostToolUse` hook on
`storyboard__preview` (`hooks/storyboard-preview.py`) passes the result to
`canvas/scripts/render_preview.py`, which renders each page in the user's own
Chrome/Chromium, over the DevTools pipe, with the sandbox on and the network
locked; the hook hands Claude the PNG paths (and any per-scene render errors)
to Read. `render_preview.py` stays the manual fallback (dark theme, a subset,
a timed-out run). Nothing is rendered server-side, and publish trust is
deterministic. Local preview supports macOS and Linux, not Windows. A
synchronous `SessionStart` hook (`hooks/storyboard-session.py`) puts the
session id in context for `storyboard__create`. The skills are Claude-only
for now.

Claude also ships local evidence capture for storyboards: a synchronous
`PostToolUse` hook on every MCP tool (`hooks/evidence-capture.py`, matcher
`mcp__.*`) records the result of a call to any non-Cardinal MCP server in
`~/.cardinal/evidence/<session_id>/ev_<id>.json` (directories 0700, files
0600, credentials scrubbed with a port of the gateway's receipt scrub,
results capped at 256 KiB, entries removed after 14 days) and tells Claude
the `ev_…` id. Nothing leaves the machine: only a result cited in a
storyboard is uploaded, by an explicit
`cardinal-evidence promote --storyboard sb_… ev_…` (`bin/cardinal-evidence`),
which posts it to the draft's evidence route as a *captured* receipt. It
authenticates with the storyboard's own 24 h evidence token, which a
`PostToolUse` hook on Cardinal's `storyboard__create` / `storyboard__preview`
(`hooks/storyboard-token.py`) keeps in the session's `token.json` (0600), and
falls back to the Cardinal MCP key. Cardinal's own gateway tools are skipped;
they already get witnessed receipts. `cardinal-evidence list` shows the
session's captures; opt out with `cardinal-evidence off`,
`CARDINAL_EVIDENCE_CAPTURE=0` or the flag file
`~/.cardinal/evidence/disabled`. The spool lives in
`core/cardinal_core/evidence.py`. The Cursor (`postToolUse`) and Gemini
(`AfterTool`) adapters write the same spool from their telemetry hooks.

Native [OpenCode](adapters/opencode/README.md) and [Pi](adapters/pi/README.md)
packages provide session/tool/usage telemetry and Cardinal MCP access. Build
them with `python3 build/native.py`; test with `npm ci --ignore-scripts` and
`npm run test:native`. See [native package releases](docs/RELEASING-NATIVE.md)
for installable artifacts, compatibility targets, and the backend runtime
filter needed before production session analytics can consume their usage.

[Devin](adapters/devin/README.md) has no plugin surface, so its adapter is a
server-side poller over the Devin REST API. It sends `cardinal.git_state`
with PR linkage and, for sessions created with the Cardinal
structured-output schema, `cardinal.decision`. It has not yet been run
against a live Devin org. It ships as a release tarball and the
`ghcr.io/cardinalhq/cardinal-devin-poller` image on `devin-vX.Y.Z` tags; see
[docs/RELEASING.md](docs/RELEASING.md#3-devin-poller).

## Release flow

Development happens here; each adapter is published to a per-CLI **release
mirror**, where users install from. Install URLs and marketplace slugs keep
working; the mirrors are build outputs, not sources.

- [cardinal-claude-plugin](https://github.com/cardinalhq/cardinal-claude-plugin)
- [cardinal-codex-plugin](https://github.com/cardinalhq/cardinal-codex-plugin)
- [cardinal-cursor-plugin](https://github.com/cardinalhq/cardinal-cursor-plugin)
- [cardinal-gemini-plugin](https://github.com/cardinalhq/cardinal-gemini-plugin)

Bumping an adapter's `plugin.json` version on `main` triggers
`.github/workflows/release-mirrors.yml`, which runs `build/release.py` to
push the built artifact (adapter code + vendored `cardinal_core`) plus a
`vX.Y.Z` tag to that adapter's mirror, syncing `marketplace.json` so the CLI
offers the update. `release.py` is idempotent — mirrors already at the tag
no-op.

The shared core ships to PyPI separately (`.github/workflows/release.yml`,
via OIDC trusted publishing) on `core-vX.Y.Z` tags as `cardinal-agent-core`.

The omnigent policy adapter (`cardinal-omnigent-policy`) was removed on
2026-09-14. Its published PyPI releases (up to 0.4.0) stay available but get
no further updates.

## Development

```bash
cd core && python3 -m unittest discover tests -v   # core suite
python3 build/vendor.py --all                       # vendor core into adapters
```

The Python runtime supports Python 3.9+ and uses only the standard library.
The OpenCode and Pi packages also require Node; Pi's MCP client has npm
dependencies. See their adapter READMEs for supported runtime versions.

## License

Apache 2.0. See [LICENSE](./LICENSE).

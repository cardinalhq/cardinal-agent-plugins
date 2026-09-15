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

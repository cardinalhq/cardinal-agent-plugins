# cardinal-agent-plugins

Source of truth for Cardinal's agent plugins and their shared core.

One contract, written once, shipped to every agent runtime:

```
core/cardinal_core/     the contract — OTLP emission, initiative
                        resolution, Bash classification, spend-limits
                        delivery, device-code consent, session counters
adapters/               per-agent surfaces (codex, gemini, cursor,
                        claude; omnigent later)
build/vendor.py         copies core into each plugin artifact so shipped
                        plugins stay self-contained (no pip for users)
docs/specs/             the extraction spec and migration plan
```

## Status

P0–P4 code-complete: core extracted (37 unit tests) and all four adapters
migrated with byte-equal golden parity against their shipped plugins
(~226 adapter tests + a cross-adapter contract test in `tests/`). See
`docs/specs/agent-core.md` for the plan and
`docs/specs/core-gaps-followup.md` for the core 0.2.0 reconciliation.

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

The two pip packages ship to PyPI separately (`.github/workflows/release.yml`,
via OIDC trusted publishing) on tags: `core-vX.Y.Z` → `cardinal-agent-core`,
`omnigent-vX.Y.Z` → `cardinal-omnigent-policy`.

## Omnigent

Unlike the CLI adapters, the omnigent integration ships as a pip
package (`cardinal-omnigent-policy`) that loads inside the omnigent
server. Install into the same Python interpreter that runs your
omnigent server:

```sh
# venv
pip install cardinal-omnigent-policy

# pipx-installed omnigent (put the CLI on PATH via --include-apps)
pipx inject omnigent cardinal-omnigent-policy --include-apps

# then, from anywhere:
cardinal-omnigent-connect --config /path/to/omnigent-config.yaml
```

`cardinal-agent-core` is pulled in as a dependency. `cardinal-omnigent-connect`
runs device-flow auth, mints an ingest credential, and merges a
`cardinalManaged` block into your server config. See
[`adapters/omnigent/README.md`](./adapters/omnigent/README.md) for the
manual-config form and what the policies emit.

## Development

```bash
cd core && python3 -m unittest discover tests -v   # core suite
python3 build/vendor.py --all                       # vendor core into adapters
```

Requires Python 3.11+. No third-party dependencies — plugins ship as
stdlib-only scripts.

## License

Apache 2.0. See [LICENSE](./LICENSE).

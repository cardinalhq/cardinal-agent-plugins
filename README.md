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

Until the release-mirror CI lands, the plugin repos remain the shipping
sources:

- [cardinal-claude-plugin](https://github.com/cardinalhq/cardinal-claude-plugin)
- [cardinal-codex-plugin](https://github.com/cardinalhq/cardinal-codex-plugin)
- [cardinal-cursor-plugin](https://github.com/cardinalhq/cardinal-cursor-plugin)
- [cardinal-gemini-plugin](https://github.com/cardinalhq/cardinal-gemini-plugin)

After migration those repos become release mirrors: install URLs and
marketplace slugs keep working; development happens here.

## Omnigent

Unlike the CLI adapters, the omnigent integration ships as a pip
package (`cardinal-omnigent-policy`) that loads inside the omnigent
server. Install into the same Python interpreter that runs your
omnigent server:

```sh
pip install cardinal-omnigent-policy
python3 -m cardinal_omnigent.connect --config /etc/omnigent/config.yaml
```

`cardinal-agent-core` is pulled in as a dependency. `connect` runs
device-flow auth, mints an ingest credential, and merges a
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

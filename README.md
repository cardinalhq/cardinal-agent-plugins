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

P0 complete: core extracted (37 unit tests), fixture harness in place.
Adapters migrate in phases P1–P4 (`docs/specs/agent-core.md`); until each
phase lands, the corresponding plugin repo remains the shipping source:

- [cardinal-claude-plugin](https://github.com/cardinalhq/cardinal-claude-plugin)
- [cardinal-codex-plugin](https://github.com/cardinalhq/cardinal-codex-plugin)
- [cardinal-cursor-plugin](https://github.com/cardinalhq/cardinal-cursor-plugin)
- [cardinal-gemini-plugin](https://github.com/cardinalhq/cardinal-gemini-plugin)

After migration those repos become release mirrors: install URLs and
marketplace slugs keep working; development happens here.

## Development

```bash
cd core && python3 -m unittest discover tests -v   # core suite
python3 build/vendor.py --all                       # vendor core into adapters
```

Requires Python 3.11+. No third-party dependencies — plugins ship as
stdlib-only scripts.

## License

Apache 2.0. See [LICENSE](./LICENSE).

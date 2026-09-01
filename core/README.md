# cardinal-agent-core

The shared Cardinal agent runtime, written once. It owns both the telemetry
contract and the typed Semantic DAG event engine. See
`../docs/specs/agent-core.md` for the extraction spec and
`../README.md` for the monorepo layout.

Ships two ways:

- **Vendored** into each CLI plugin artifact at build time
  (`build/vendor.py`) — plugins stay self-contained, no pip.
- **Installed** as a normal package (`pip install -e core/`) by
  server-side consumers (the omnigent adapter) and the test suite.

```bash
cd core && python3 -m unittest discover tests -v
```

`cardinal_core.semantic_dag` is consumed by thin Codex and Claude entrypoints.
Each adapter supplies its runtime identity, session environment keys, and
packaged viewer asset path. In the monorepo both entrypoints use the canonical
viewer at `../common/semantic-dag/viewer`; release builds copy it into each
self-contained plugin. Codex and Claude intentionally share one Cardinal state
directory and viewer port.

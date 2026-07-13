# cardinal-agent-core

The Cardinal agent-telemetry contract, written once. See
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

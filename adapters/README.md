# Adapters

Per-agent plugin surfaces over `core/cardinal_core`. Populated by
migration phases P1–P4 (see `../docs/specs/agent-core.md` §Migration
sequencing); each phase moves one plugin's adapter code here, captures
its pre-migration goldens, and proves byte-equal OTLP output.

| Adapter | Phase | Status | Source repo (becomes release mirror) |
| --- | --- | --- | --- |
| `codex/` | P1 | pending | cardinalhq/cardinal-codex-plugin |
| `gemini/` | P2 | pending | cardinalhq/cardinal-gemini-plugin |
| `cursor/` | P3 | pending | cardinalhq/cardinal-cursor-plugin |
| `claude/` | P4 | pending | cardinalhq/cardinal-claude-plugin |
| `omnigent/` | P5 | separate spec | — (pip-installed, not vendored) |

Layout convention per adapter: `hooks/`, `scripts/`, `tests/goldens/`,
plus each ecosystem's manifest files. `build/vendor.py` copies
`cardinal_core/` into `hooks/` at build time.

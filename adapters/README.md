# Adapters

Per-agent plugin surfaces over `core/cardinal_core`. Populated by
migration phases P1–P4 (see `../docs/specs/agent-core.md` §Migration
sequencing); each phase moves one plugin's adapter code here, captures
its pre-migration goldens, and proves byte-equal OTLP output.

| Adapter | Phase | Status | Source repo (becomes release mirror) |
| --- | --- | --- | --- |
| `codex/` | P1 | **migrated** — 10/10 goldens byte-equal, 36 tests | cardinalhq/cardinal-codex-plugin |
| `gemini/` | P2 | **migrated** — 8/8 goldens byte-equal, 11 tests | cardinalhq/cardinal-gemini-plugin |
| `cursor/` | P3 | **migrated** — 10/10 goldens byte-equal, 43 tests | cardinalhq/cardinal-cursor-plugin |
| `claude/` | P4 | **migrated** — 12 parity + 124 ported tests | cardinalhq/cardinal-claude-plugin |
| `omnigent/` | P5 | separate spec; blocked on core 0.2.0 (`docs/specs/core-gaps-followup.md`) | — (pip-installed, not vendored) |

Migration is code-complete; the mirror repos still ship the pre-migration
releases until the release-mirror CI lands and each adapter is cut as a
release. CORE_GAPS reconciliation plan: `../docs/specs/core-gaps-followup.md`.

Layout convention per adapter: `hooks/`, `scripts/`, `tests/goldens/`,
plus each ecosystem's manifest files. `build/vendor.py` copies
`cardinal_core/` into `hooks/` at build time.

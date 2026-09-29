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
| `claude-storyboards/` | composed | slim `cardinal-storyboards` plugin: `compose.json` picks the storyboard hooks, skills and `cardinal-evidence` from `claude/`; OAuth MCP, no telemetry | cardinalhq/cardinal-claude-plugin (`plugins/cardinal-storyboards`, tags `cardinal-storyboards/v*`) |
| `opencode/` | native | npm package; OpenCode 1.18.30; telemetry + native MCP | — (`build/native.py`) |
| `pi/` | native | Pi package; Pi 0.85.1; telemetry + bundled MCP client | — (`build/native.py`) |
| `devin/` | poller | server-side Devin API poller; `git_state` + `decision`; unvalidated against a live org | — (not packaged) |

Migration is code-complete; the mirror repos still ship the pre-migration
releases until the release-mirror CI lands and each adapter is cut as a
release. CORE_GAPS reconciliation plan: `../docs/specs/core-gaps-followup.md`.

`claude-storyboards/` has no code of its own: it ships copies of files under
`claude/`. A fix to those shared files (the storyboard hooks,
`hooks/_plugin_mode.py`, `bin/cardinal-evidence`, the storyboard/canvas skills)
reaches slim users only when `claude-storyboards/.claude-plugin/plugin.json` is
bumped too; bumping `claude/` alone releases just the full plugin.

Layout convention per adapter: `hooks/`, `scripts/`, `tests/goldens/`,
plus each ecosystem's manifest files. `build/vendor.py` copies
`cardinal_core/` into `hooks/` at build time.

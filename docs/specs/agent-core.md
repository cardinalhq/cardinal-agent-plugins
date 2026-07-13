# cardinal-agent-core — extraction spec

Status: **approved 2026-07-13, P0 in progress** · Sources audited:
`cardinal-claude-plugin` v0.12.x, `cardinal-codex-plugin` v0.5.x,
`cardinal-cursor-plugin` v0.1.x, `cardinal-gemini-plugin` v0.1.x.

## Problem

The four agent plugins carry four copies of the same contract. Measured
2026-07-13:

| Repo | Plugin LOC (hooks + scripts) |
| --- | --- |
| cardinal-claude-plugin | 3,995 |
| cardinal-codex-plugin | 2,597 |
| cardinal-cursor-plugin | 2,580 |
| cardinal-gemini-plugin | 2,460 |
| **Total** | **11,632** |

Function-level duplication (identical or near-identical implementations):

| Function | claude | codex | cursor | gemini |
| --- | --- | --- | --- | --- |
| initiative resolution (`resolve_initiative` / `_resolve_initiative`) | ✓ | ✓ | ✓ | ✓ |
| worktree-noise stripping | ✓ | ✓ | ✓ | ✓ |
| Bash-verb classifier (`classify_bash_command` / `_classify_bash`) | ✓ | ✓ | ✓ | ✓ |
| OTLP record building (`kv`/`log_record`) | ✓ | ✓ | ✓ | ✓ |
| `_limits_common.py` (whole file) | ✓ | ✓ | ✓ | ✓ |
| device-code flow (`start_device_code`/`poll_device_token`) | ✓ | ✓ | ✓ | ✓ |
| ingest/MCP reachability probes | ✓ | ✓ | ✓ | ✓ |
| slash-command detection | ✓ | ✓ | ✓ | ✓ |

`_limits_common.py` divergence from the Claude original: 84 lines (codex),
86 (gemini), 182 (cursor) — almost entirely path constants, docstrings, and
cursor's camelCase payload accommodations. The algorithms are identical.

Every parity spec carries a "keeping the repos in lockstep" section — a
manually-enforced promise that this core exists. Recent evidence of the
cost: the v0.12.x subagent enrichment was ported three times; the Gemini
plugin was built by mechanically copying Codex code with path
substitutions.

## Goal

One monorepo, `cardinal-agent-plugins`, holding:

- `core/` — the contract, written once, pip-installable.
- `adapters/{claude,codex,cursor,gemini}/` — the per-agent surface.
- CI that vendors core into each adapter's plugin artifact and publishes
  the artifacts to the four existing GitHub repos as release mirrors, so
  install instructions and marketplace slugs do not change.

Velocity target: a contract change (new event, new attribute, classifier
update) lands in ONE commit and ships to all four plugins in the same
release cycle. Supporting agent #5 (omnigent) or #6 is an adapter, not a
port.

## Core module inventory

Everything below is extractable today; "source" names the freshest copy to
extract from.

| Module | Contents | Source | Parameterized by |
| --- | --- | --- | --- |
| `cardinal_core/otlp.py` | `kv`, `log_record`, `emit_records`, resource-attr assembly | gemini | agent runtime name, state paths |
| `cardinal_core/initiative.py` | `resolve_initiative`, `strip_worktree_noise`, `detect_command`, `canonical_repo`, `PREFIX_TO_TYPE`, `PROTECTED_BRANCHES` | gemini | — (pure) |
| `cardinal_core/bashclass.py` | `classify_bash_command`, `BASH_CMD_CLASS`, `BASH_MULTIPLEX_CLASS`, `BASH_CLASS_RANK` | gemini | — (pure) |
| `cardinal_core/limits.py` | verdict/ack/override files, `maybe_refresh_verdict`, `fetch_status`, `standing_lines`, gate logic (`limits_gate_output`), band hysteresis | gemini | state dir, hook-output event name |
| `cardinal_core/deviceflow.py` | `start_device_code`, `poll_device_token`, pending-file lifecycle, `verify_ingest_reachable` (+retry ladder), `verify_mcp_reachable`, `derive_deployment_env` | codex | client_id |
| `cardinal_core/pricing.py` | per-provider price tables + `compute_cost_usd` (OpenAI + Gemini tables; Claude emits cost natively — no table) | codex + gemini | provider |
| `cardinal_core/paths.py` | `AgentPaths` dataclass: home dir, state/secrets/telemetry/limits paths, progress-cursor read/write, `atomic_write_json`, `atomic_write_secret`, `backup` | gemini | agent home (`~/.claude`, `~/.codex`, `~/.cursor`, `~/.gemini`) |
| `cardinal_core/session.py` | convention prompt text, budget-standing assembly, seq-counter state (`user_turn_seq`/`turn_seq`/`tool_seq`), plan-stamp read/write | codex + gemini | agent runtime name |

Estimated core size: ~1,800–2,200 LOC (from ~9,000 LOC of currently
duplicated logic; the remainder is per-agent).

## Adapter variance table

What legitimately stays per-adapter. This is the honest inventory of why
four plugins exist at all:

| Axis | claude | codex | cursor | gemini |
| --- | --- | --- | --- | --- |
| Telemetry acquisition | native OTel emitter does the heavy lifting; hooks fill gaps | transcript-JSONL scraping at Stop | hook payloads (no per-call tokens — product gap) | hook payloads per call + native OTLP passthrough |
| Hook event names | SessionStart / UserPromptSubmit / Stop / SubagentStop … | same names as Claude | camelCase (`beforeSubmitPrompt`, `postToolUse`, …) | SessionStart / BeforeAgent / AfterModel / AfterTool / AfterAgent / PreCompress |
| Hook output protocol | `hookSpecificOutput` JSON | same as Claude | `{continue, user_message}`; no additional_context on prompt hook (divergence E) | same as Claude |
| Config write target | Claude settings + hooks.json | `config.toml` (TOML, marker-block managed) | `~/.cursor/mcp.json` + `hooks.json` (+ project-scope variants) | `settings.json` + extension bundle |
| Payload key spelling | snake_case | snake_case | camelCase | mixed; probes both |
| Cost | native `cost_usd` | plugin-computed (OpenAI table) | n/a (no token counts) | plugin-computed (Gemini table) |
| State dir | `~/.claude` | `~/.codex` | `~/.cursor` | `~/.gemini` |

Adapter size estimate after extraction: 300–600 LOC each (codex largest —
transcript scraper stays adapter-side).

## Packaging: vendor-by-copy

Plugins ship as self-contained artifacts; users never `pip install`.
The build step copies the `cardinal_core/` package directory into each
plugin artifact next to `hooks/`, and hook scripts import it via the
`sys.path.insert(0, …)` mechanism they already use for `_limits_common`:

```
plugins/cardinal-codex-plugin/          (built artifact)
├── hooks/
│   ├── cardinal-codex-telemetry.py    # from adapters/codex/
│   └── cardinal_core/                 # copied verbatim at build time
├── scripts/cardinal-{connect,status,disconnect}
└── .codex-plugin/plugin.json          # version stamped by CI
```

No code transformation, no concatenation — the vendored files are
byte-identical to `core/` at the pinned version, so a bug report against a
shipped plugin maps directly to a core source line.

The same `core/` directory doubles as a normal pip package
(`pyproject.toml` at `core/`) for the omnigent adapter later and for
running the test suite.

## Repo shape and release flow

**Recommendation: monorepo + published mirrors.**

- `cardinalhq/cardinal-agent-plugins` (new) is where development happens.
- CI builds the four artifacts and pushes each to its existing repo
  (`cardinal-claude-plugin`, `cardinal-codex-plugin`,
  `cardinal-cursor-plugin`, `cardinal-gemini-plugin`) as a release commit +
  tag. Marketplace slugs, install URLs, and user-facing docs do not change.
- The four existing repos gain a banner: "source of truth is
  cardinal-agent-plugins; PRs there."

Alternative considered and rejected: core as a fifth repo consumed by the
four plugin repos (submodule or copy-in). Rejected because it keeps four
release processes and reintroduces lockstep drift at the version-bump
layer — the exact failure mode we're eliminating.

## Versioning

- Core gets independent semver (`cardinal-agent-core X.Y.Z`).
- Each adapter keeps its existing plugin version stream (so marketplaces
  see ordinary version bumps).
- New resource attribute `cardinal.core_version` stamped alongside
  `cardinal.plugin_version` on every emitted record — lets lakerunner
  distinguish "old core" fleets when debugging contract issues.

## Test strategy: golden-fixture parity

The migration must be provably behavior-neutral:

1. **Capture goldens pre-migration.** For each plugin, run the existing
   hook scripts against a fixture set (synthetic hook payloads, synthetic
   transcripts for codex, a stub ingest server) and record the exact OTLP
   bodies POSTed. These fixtures already exist in embryo in each repo's
   `tests/` (stub-server pattern from codex `test_cardinal_plugin.py`).
2. **Assert byte-equal post-migration.** The migrated adapter must emit
   identical OTLP bodies for the same fixtures (modulo the new
   `cardinal.core_version` attribute, which goldens are normalized for).
3. **Core unit tests** own the pure logic (classifier tables, initiative
   resolution, pricing) — ported from the existing per-repo tests, kept
   once.
4. **Cross-adapter contract test**: one fixture battery asserting all four
   adapters emit the same event names and attribute keys for equivalent
   inputs — this replaces the "lockstep" prose sections in the parity
   specs.

## Migration sequencing

| Phase | Work | Risk | Proof |
| --- | --- | --- | --- |
| P0 | Create monorepo; extract core from gemini+codex sources; port unit tests; stand up fixture harness | none (no shipping artifact changes) | core tests green |
| P1 | Codex adapter consumes core; capture codex goldens first; publish mirror | low — codex has the richest test suite | golden parity |
| P2 | Gemini adapter (was forked from codex — smallest delta) | low | golden parity |
| P3 | Cursor adapter (camelCase mapping + divergence E stay adapter-side) | medium — least-observed payloads | golden parity + `CARDINAL_CURSOR_DEBUG_PAYLOADS` field check |
| P4 | Claude adapter — consumes `limits`, `initiative`, `deviceflow`, `paths` only; native-OTel hook scripts (`turn-usage.py` etc.) migrate but their transcript-walking stays adapter-side | medium — largest user base; ship last behind goldens | golden parity |
| P5 | Omnigent adapter — separate spec; core consumed as pip dep | — | — |

Rollback story per phase: mirrors are tagged; reverting a phase is
re-releasing the prior tag from the old repo. Nothing about a user's
installed plugin changes until they upgrade.

## Design constraints from the omnigent adapter (forward-looking)

Documented now so the core API doesn't paint us into a corner (full spec
comes later):

- Core functions must not assume file-based state is the only state:
  `limits.py` and `session.py` take a small state-store interface
  (default: the existing file layout) so a server-side policy engine can
  supply its own.
- `emit_records` takes connection config as arguments, not module-level
  path constants, so a server process can hold N org connections.
- Identity (`user_email`) is an argument, not a read from
  `~/.<agent>/cardinal.json` — omnigent supplies `actor.run_as` per
  request.

These are cheap to honor during extraction and expensive to retrofit.

## Non-goals

- No runtime behavior changes in any plugin — this is a pure refactor
  proven by goldens.
- No unification of the four telemetry acquisition modes (transcript
  scraping vs hook payloads vs native OTel) — those differences are real
  and stay in adapters.
- No change to the server-side contract (device-flow bundle shape, limits
  protocol, lakerunner columns).
- Omnigent adapter implementation (separate spec after core lands).

## Resolved questions (2026-07-13)

1. **Monorepo name**: `cardinal-agent-plugins`.
2. **Mirror policy**: release mirrors — CI pushes each built artifact as a
   release commit + tag to the existing four repos; install URLs and
   marketplace slugs unchanged; README banners point contributors here.
3. **`_plan_cache.py`**: stays in the Claude adapter. It is built on
   Anthropic-subscription concepts no other agent has; core stays free of
   vendor-specific billing logic.

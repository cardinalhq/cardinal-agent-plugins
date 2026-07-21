# Releasing a new version

This repo ships two independent kinds of artifact. Pick the one you're publishing.

| What you're shipping | Trigger | Workflow |
| --- | --- | --- |
| Adapter plugin (`claude`, `codex`, `cursor`, `gemini`) | Bump `plugin.json` → merge to `main` | [`.github/workflows/release-mirrors.yml`](../.github/workflows/release-mirrors.yml) |
| PyPI package (`cardinal-agent-core`, `cardinal-omnigent-policy`) | Push a `core-vX.Y.Z` or `omnigent-vX.Y.Z` tag | [`.github/workflows/release.yml`](../.github/workflows/release.yml) |

---

## 1. Adapter plugin (claude / codex / cursor / gemini)

Users install these from mirror repos (`cardinalhq/cardinal-<adapter>-plugin`). Development happens here; the workflow copies the adapter + vendored `cardinal_core` into the mirror as a release commit + tag.

### Steps

1. Bump the `version` field in the adapter's manifest:

   | Adapter | Path |
   | --- | --- |
   | claude | `adapters/claude/.claude-plugin/plugin.json` |
   | codex | `adapters/codex/.codex-plugin/plugin.json` |
   | cursor | `adapters/cursor/.cursor-plugin/plugin.json` |
   | gemini | `adapters/gemini/.gemini-plugin/plugin.json` |

2. Open a PR, merge to `main`.

3. The workflow auto-fires on that path filter, runs the test suites (core + vendor + adapter + contract), then `build/release.py`:
   - Builds the adapter artifact (product code + vendored `cardinal_core`, minus monorepo-only files).
   - Pushes a `release/vX.Y.Z` branch + tag to the mirror over SSH (deploy key).
   - Opens a release PR and auto-merges it (using `MIRROR_PR_TOKEN`).

4. Idempotent: if the mirror already has a tag matching that version, the job no-ops with `already at vX.Y.Z`.

### Manual release

Actions → **Release adapter to mirror** → *Run workflow* → choose adapter. Version is still read from `plugin.json` — bump it first.

### Bumping multiple adapters at once

Merging one commit that changes several `plugin.json`s triggers the matrix over the affected adapters and releases them in parallel. Unchanged adapters no-op.

### Troubleshooting

- **PR didn't auto-merge on the mirror** — `MIRROR_PR_TOKEN` secret is missing or lacks scope. Workflow falls back to `github.token`, which can push the branch but not merge; you'll see a `::warning::` naming the branch. Fix per the inline setup notes in `release-mirrors.yml`.
- **`missing MIRROR_DEPLOY_KEY secret for <adapter>`** — the mirror's deploy-key secret isn't set on this repo. Add `MIRROR_DEPLOY_KEY_<ADAPTER>` in repo settings.

---

## 2. PyPI package (core / omnigent)

Trusted publishing via OIDC — no long-lived token. Tag-driven.

### Steps

```bash
# Core (publish first if both are moving — omnigent depends on it)
git tag core-v0.3.1
git push origin core-v0.3.1

# Omnigent
git tag omnigent-v0.2.1
git push origin omnigent-v0.2.1
```

Bump the `version` in the package's `pyproject.toml` in the same commit the tag points at:

- `core/pyproject.toml` → `cardinal-agent-core`
- `adapters/omnigent/pyproject.toml` → `cardinal-omnigent-policy`

The workflow builds sdist + wheel and publishes to PyPI under the pending publisher config (environment `pypi-core` or `pypi-omnigent`).

### One-time setup (already done)

Each package has a pending publisher on PyPI bound to this repo, `release.yml`, and the matching environment. Details in the header comment of `release.yml`.

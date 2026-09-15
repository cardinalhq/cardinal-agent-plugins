# Releasing a new version

This repo ships several independent kinds of artifact. Pick the one you're publishing.

| What you're shipping | Trigger | Workflow |
| --- | --- | --- |
| Adapter plugin (`claude`, `codex`, `cursor`, `gemini`) | Bump `plugin.json` → merge to `main` | [`.github/workflows/release-mirrors.yml`](../.github/workflows/release-mirrors.yml) |
| PyPI package (`cardinal-agent-core`) | Push a `core-vX.Y.Z` tag | [`.github/workflows/release.yml`](../.github/workflows/release.yml) |
| Devin poller (tarball + `ghcr.io/cardinalhq/cardinal-devin-poller` image) | Push a `devin-vX.Y.Z` tag | [`.github/workflows/devin-release.yml`](../.github/workflows/devin-release.yml) |
| Native OpenCode / Pi packages | Push an `opencode-vX.Y.Z` / `pi-vX.Y.Z` tag | [`.github/workflows/native-adapters.yml`](../.github/workflows/native-adapters.yml), see [RELEASING-NATIVE.md](RELEASING-NATIVE.md) |

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

## 2. PyPI package (core)

Trusted publishing via OIDC — no long-lived token. Tag-driven.

### Steps

```bash
git tag core-v0.4.0
git push origin core-v0.4.0
```

Bump the `version` in `core/pyproject.toml` in the same commit the tag points at. The workflow builds sdist + wheel and publishes `cardinal-agent-core` to PyPI under the pending publisher config (environment `pypi-core`).

The omnigent policy (`cardinal-omnigent-policy`) is no longer released from this repo; its existing PyPI versions remain published.

### One-time setup (already done)

Core has a pending publisher on PyPI bound to this repo, `release.yml`, and the `pypi-core` environment. Details in the header comment of `release.yml`.

---

## 3. Devin poller

The Devin adapter (`adapters/devin`) is a server-side poller, not a plugin. Each release produces:

- a GitHub release `devin-vX.Y.Z` with `cardinal-devin-X.Y.Z.tar.gz` (built by [`build/devin.py`](../build/devin.py): `bin/`, `cardinal_devin/`, vendored `cardinal_core/`, `playbook/`, README, LICENSE)
- a container image `ghcr.io/cardinalhq/cardinal-devin-poller:vX.Y.Z` and `:latest` (`linux/amd64`, `linux/arm64`, from [`adapters/devin/Dockerfile`](../adapters/devin/Dockerfile))

### Steps

1. Bump `__version__` in `adapters/devin/cardinal_devin/__init__.py`. Open a PR, merge to `main`.
2. Tag the merge commit and push the tag:

   ```bash
   git tag devin-v0.1.0 && git push origin devin-v0.1.0
   ```

3. The workflow runs the Devin tests (including the tarball smoke test) and the contract test on Python 3.9 and 3.12, then in parallel:
   - **release**: checks the tag equals `devin-v` + `__version__`, builds the tarball, creates a draft release with notes, uploads the tarball, and publishes it.
   - **image**: checks the tag the same way and builds and pushes the image with OCI labels. It is skipped if `vX.Y.Z` already exists in GHCR.

### Re-running

Both jobs are idempotent. A published release is left untouched; a draft left by a failed run gets the tarball re-uploaded and is then published. An existing image tag is never overwritten, so bump the version to publish a new image. To retry, re-run the failed job, or use Actions → **Release Devin poller** → *Run workflow* with the tag selected as the ref. Dispatched on a branch, or on a pull request touching the packaging, the workflow only tests and builds; it publishes nothing.

### First publish

- A new GHCR package is **private** by default. After the first image push, open the `cardinal-devin-poller` package in the cardinalhq org's Packages → *Package settings* and set visibility to public, or clusters will need an image pull secret.
- Release notes say the adapter is unvalidated against a live Devin org and that Lakerunner must accept runtime `devin` (`agent.runtime=devin`) before sessions appear on Agent Outcomes. Edit them when either changes.

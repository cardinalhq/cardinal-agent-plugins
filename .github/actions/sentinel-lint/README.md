# `sentinel-lint` GitHub Action

Lints mechanize.dev Sentinels at PR time. Runs the same `python3 spike/executor/executor.py lint <sentinel-dir>` CLI that ships in [cardinal-agent-plugins](https://github.com/cardinalhq/cardinal-agent-plugins), aggregates results across every matching Sentinel directory in your repo, and emits GitHub workflow annotations so failures render inline in the PR "Files changed" tab.

## What it does

1. Checks out `cardinalhq/cardinal-agent-plugins` at the pinned `ref` into `.sentinel-lint/` (does not touch your repo's checkout).
2. Sets up Python 3.12 and installs `pyyaml` + `jsonschema`.
3. Expands the `paths` glob(s); skips (with a warning) any match that isn't a directory or lacks a `sentinel.yaml`.
4. Runs `executor.py lint --format=json` against each Sentinel directory using the pinned repo's `common/deployment-schema.yaml` + `common/integrations.yaml`.
5. Emits `::error file=...,line=...::CODE: message — fix: ...` for every FAIL finding and `::warning::` for every WARN.
6. Exits 0 if all Sentinels pass, 1 if any FAIL.

## Consumer usage

```yaml
name: Lint Sentinels
on:
  pull_request:
    paths:
      - '.mechanize/sentinels/**'

jobs:
  sentinel-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: cardinalhq/cardinal-agent-plugins/.github/actions/sentinel-lint@main
        with:
          paths: '.mechanize/sentinels/*/'
          check: all
          ref: main
```

**Inputs.** `paths` is a space-separated glob (or globs) of Sentinel directories (default `mechanize-out/*/`). `check` picks the lint mode: `structural` (Phase 1 universal), `remote` (Phase 2 remote-readiness; a no-op unless the Sentinel declares `metadata.deployment.mode: remote`), or `all` (default; both). `ref` pins the `cardinal-agent-plugins` branch/tag/SHA whose lint code and schemas will be used (default `main`); if you pin the action itself to a version, pin `ref` to the same commit for reproducible output.

## What passes vs. what fails

- **Pass:** no findings, or WARN-only.
- **Fail:** any finding with `severity: FAIL`.

Every finding carries a stable code (`R1`, `R7`, `STRUCT-NODES`, …) so you can grep for it in the plan. See the [sentinel-lint plan](https://github.com/cardinalhq/cardinal-agent-plugins/blob/main/spike/executor/lint.py) for the current R-code catalogue and the rationale behind each check.

## Local reproduction

The Action is a thin wrapper around the CLI. To reproduce a CI result on your laptop:

```bash
# from a cardinal-agent-plugins checkout
cd spike/executor
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python executor.py lint /path/to/your/sentinel-dir --check=all
```

Exit code 0 (PASS or WARN-only) and exit code 1 (any FAIL) match CI. Use `--format=json` to see the exact payload the Action's annotator consumes.

## Required org policy

The Action's first step is `actions/checkout` of `cardinalhq/cardinal-agent-plugins` (public). If your organisation restricts third-party GitHub Actions to an allowlist, allowlist `cardinalhq/cardinal-agent-plugins/*` so this Action's transitive checkout resolves. No secrets or tokens are required — a public-read checkout is enough.

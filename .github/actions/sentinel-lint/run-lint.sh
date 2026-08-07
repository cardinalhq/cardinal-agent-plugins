#!/usr/bin/env bash
# Composite-action driver for sentinel-lint.
#
# Reads the pinned cardinal-agent-plugins checkout at $LINT_ROOT (the
# path the action.yml checks out into), enumerates sentinel directories
# under $LINT_PATHS, runs `executor.py lint ... --format=json` on each,
# emits GitHub workflow annotations for every finding, and aggregates
# exit codes: 0 if all pass, 1 if any FAIL.
#
# Environment (set by action.yml):
#   LINT_ROOT     — absolute path to cardinal-agent-plugins checkout
#   LINT_PATHS    — space-separated glob(s) matching sentinel dirs
#   LINT_CHECK    — 'structural' | 'remote' | 'all'
#   GITHUB_ACTION_PATH — dir this script lives in (from Actions runner)

set -euo pipefail

: "${LINT_ROOT:?LINT_ROOT must be set to the cardinal-agent-plugins checkout}"
: "${LINT_PATHS:?LINT_PATHS must be set (space-separated globs)}"
: "${LINT_CHECK:=all}"
: "${GITHUB_ACTION_PATH:?GITHUB_ACTION_PATH must be set by the Actions runner}"

EXECUTOR="${LINT_ROOT}/spike/executor/executor.py"
SCHEMA="${LINT_ROOT}/common/deployment-schema.yaml"
REGISTRY="${LINT_ROOT}/common/integrations.yaml"
EMIT="${GITHUB_ACTION_PATH}/emit-annotations.py"

for required in "${EXECUTOR}" "${SCHEMA}" "${REGISTRY}" "${EMIT}"; do
    if [ ! -f "${required}" ]; then
        echo "::error::sentinel-lint: expected file not present: ${required}"
        exit 2
    fi
done

# Enumerate sentinel directories. `nullglob` so unmatched patterns
# expand to nothing rather than the literal glob string.
shopt -s nullglob
declare -a dirs=()
for pattern in ${LINT_PATHS}; do
    saw_any=0
    for m in ${pattern}; do
        saw_any=1
        # Strip trailing slash for consistency.
        m="${m%/}"
        if [ ! -d "${m}" ]; then
            echo "::warning::sentinel-lint: skipping non-directory match '${m}' for pattern '${pattern}'"
            continue
        fi
        if [ ! -f "${m}/sentinel.yaml" ]; then
            echo "::warning::sentinel-lint: skipping '${m}' (no sentinel.yaml) for pattern '${pattern}'"
            continue
        fi
        dirs+=("${m}")
    done
    if [ "${saw_any}" -eq 0 ]; then
        echo "::warning::sentinel-lint: no filesystem matches for pattern '${pattern}'"
    fi
done
shopt -u nullglob

if [ "${#dirs[@]}" -eq 0 ]; then
    echo "sentinel-lint: no Sentinel directories to lint (patterns: ${LINT_PATHS})"
    exit 0
fi

echo "sentinel-lint: linting ${#dirs[@]} Sentinel directory/directories (check=${LINT_CHECK})"

overall=0
tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

for d in "${dirs[@]}"; do
    echo "::group::sentinel-lint ${d}"
    payload="${tmpdir}/$(echo "${d}" | tr '/' '_').json"

    # `executor.py lint` returns 0 on PASS / warn-only, 1 on any FAIL.
    # Capture the JSON regardless so the annotator can report every
    # finding (including WARNs on a passing run).
    if python3 "${EXECUTOR}" lint "${d}" \
        --check="${LINT_CHECK}" \
        --schema "${SCHEMA}" \
        --registry "${REGISTRY}" \
        --format=json > "${payload}"; then
        cli_status=0
    else
        cli_status=$?
    fi

    if ! python3 "${EMIT}" "${d}" "${payload}"; then
        overall=1
    fi
    # Belt-and-braces: if the CLI failed for a reason the annotator
    # didn't reflect (e.g. non-zero from an internal error), still fail.
    if [ "${cli_status}" -ne 0 ] && [ "${overall}" -eq 0 ]; then
        overall=1
    fi
    echo "::endgroup::"
done

exit "${overall}"

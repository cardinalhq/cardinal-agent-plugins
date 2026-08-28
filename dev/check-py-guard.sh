#!/usr/bin/env bash
# Fail CI if any adapters/*/bin/cardinal-* or adapters/*/scripts/cardinal-*
# script is missing the Python-version guard sentinel. Guard block is
# inserted by dev/insert-python-guard.py; see that script's docstring for
# the full motivation.
#
# The guard re-execs stock-macOS python3 (3.9.6) into python3.13/12/11
# when available, so users don't hit a SyntaxError-or-TypeError on
# PEP-604 unions (`str | None`) before they've even installed a modern
# Python. Every new cardinal-* script must ship with it.
set -euo pipefail

SENTINEL="cardinal:py311-guard"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

missing=()
while IFS= read -r f; do
  grep -q "$SENTINEL" "$f" || missing+=("$f")
done < <(find "$ROOT/adapters" -type f \
  \( -path '*/bin/cardinal-*' -o -path '*/scripts/cardinal-*' \) \
  ! -name '*.py' ! -name '*.pyc')

if (( ${#missing[@]} > 0 )); then
  {
    echo "cardinal-py311-guard: sentinel '${SENTINEL}' missing in:"
    for f in "${missing[@]}"; do
      echo "  ${f#"$ROOT/"}"
    done
    echo
    echo "Run: python3 dev/insert-python-guard.py"
    echo "See dev/insert-python-guard.py's docstring for the rationale."
  } >&2
  exit 1
fi

echo "cardinal-py311-guard: OK (all $(find "$ROOT/adapters" -type f \
  \( -path '*/bin/cardinal-*' -o -path '*/scripts/cardinal-*' \) \
  ! -name '*.py' ! -name '*.pyc' | wc -l | tr -d ' ') scripts carry the sentinel)"

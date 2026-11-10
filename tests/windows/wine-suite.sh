#!/bin/sh
# Run one suite under the Windows Python in Wine (windows-wine.yml).
# Wine can exit 0 after the interpreter aborts, so the run passes only if
# run_suite.py wrote 0 to its status file, which is the last thing it does.
status="${RUNNER_TEMP:-/tmp}/suite-$1.status"
rm -f "$status"
wine "$WINPY" tests/windows/run_suite.py "$1" "$(winepath -w "$status")"
[ "$(cat "$status" 2>/dev/null)" = 0 ]

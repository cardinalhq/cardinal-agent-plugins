#!/bin/sh
# Run one suite under the Windows Python in Wine (windows-wine.yml).
# Wine can lose the interpreter's last output and exit 0 after it aborts, so
# the run passes only if Wine exited 0 and run_suite.py finished its report,
# whose last line is the exit code.
report="${RUNNER_TEMP:-/tmp}/suite-$1.report"
rm -f "$report"
wine "$WINPY" tests/windows/run_suite.py "$1" "$(winepath -w "$report")"
rc=$?
cat "$report" 2>/dev/null
[ "$rc" = 0 ] && [ "$(tail -n 1 "$report" 2>/dev/null)" = "exit 0" ]

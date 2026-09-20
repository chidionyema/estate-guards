#!/bin/sh
# One tick of stuck_detector.py, with a heartbeat.
#
# WHY THIS WRAPPER EXISTS. `stuck_detector.py --json` prints one row per NOT-OK session, so a
# healthy estate prints nothing. A crashed probe also prints nothing. Those two states were
# byte-identical in the log, which is the "an audit that crashes reports nothing" trap this
# estate has already paid for once: the alarm looks calm in exactly the case where it is dead.
#
# So every tick writes exactly one heartbeat line carrying the timestamp, the detector's exit
# code and how many findings it produced. Silence in this file now means the JOB did not run,
# which is a different and checkable fault.
set -u
PY=/usr/local/bin/python3
GUARDS_ROOT="${GUARDS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
OUT=$("$PY" "$GUARDS_ROOT/stuck_detector.py" --json 2>&1)
RC=$?
if [ -n "$OUT" ]; then
  N=$(printf '%s\n' "$OUT" | grep -c .)
else
  N=0
fi
TICK_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"  # crew#73: "at" is the key sources.json reads; "ts" kept for older readers
printf '{"at":"%s","ts":"%s","kind":"tick","rc":%d,"findings":%d}\n' "$TICK_AT" "$TICK_AT" "$RC" "$N"
[ -n "$OUT" ] && printf '%s\n' "$OUT"
exit 0   # a detector that cannot classify must not make launchd throttle the job that runs it

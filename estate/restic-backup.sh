#!/bin/sh
# Estate backup via restic to Cloudflare R2 (bucket the estate owns).
# Replaces nothing destructively: the old offsite backup jobs keep running
# as fallback (founder order). Run wrapped: hc-wrap.sh estate-restic <this>.
set -eu
GUARDS_ROOT="${GUARDS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
. "$GUARDS_ROOT/estate/restic-env.sh"

# Tranche 1: load-bearing un-versioned state (~50 MB measured 2026-08-24).
# Transcripts (~/.claude/projects) stay with the old offsite job for now.
#
# restic exits 3 when it wrote the snapshot but could not read some source:
# macOS refuses the xattrs on ~/Library/Containers/com.apple.Notes to a process
# without Full Disk Access, so 3 is this job's ordinary exit. Under `set -e` that
# 3 aborted the script before the age check below, so the row went red on every
# run while the snapshot landed, and three of those reds opened its breaker. A
# fatal restic error (1, 2, 10+) is still a failure; 3 is not.
rc=0
restic backup \
  "$HOME/.estate" \
  "$HOME/Library/Containers/com.apple.Notes/Data/Library/Notes" \
  "$HOME/.claude/state" \
  "$HOME/.claude/projects/-Users-chidionyema/memory" \
  "$HOME/.claude/projects/-Users-chidionyema/checkpoints" \
  "$HOME/Library/LaunchAgents" \
  "$HOME/.config/estate" \
  "$HOME/.config/prospector" \
  "$HOME/.claude/ESTATE_BOARD.jsonl" \
  "$HOME/AGENTS.md" \
  "$HOME/.claude/LAWS-INCIDENTS.md" \
  "$HOME/.claude/LAWS.dynamic.md" \
  "$HOME/.claude/settings.json" \
  --tag estate-tranche1 --quiet || rc=$?
[ "$rc" -eq 0 ] || [ "$rc" -eq 3 ] || exit "$rc"

# Retention: a day of hourlies, two weeks of dailies, two months of weeklies.
restic forget --keep-hourly 24 --keep-daily 14 --keep-weekly 8 --prune --quiet

# Receipt the artifact, not the exit code: latest snapshot must be < 26h old.
restic snapshots --latest 1 --json | python3 -c '
import json, sys, datetime
snaps = json.load(sys.stdin)
assert snaps, "no snapshots in repo"
snap = snaps[-1]
t = datetime.datetime.fromisoformat(snap["time"])
age = datetime.datetime.now(datetime.timezone.utc) - t
assert age.total_seconds() < 26*3600, "latest snapshot is %s old" % age
print("latest snapshot %s age %s" % (snap["short_id"], age))
'

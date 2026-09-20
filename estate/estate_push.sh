#!/usr/bin/env bash
# estate_push.sh — get the estate's state off this Mac, and prove it comes back.
#
# WHY. The Mac is the source of truth and it is one laptop. estate_audit.py writes
# ~/.claude/state/estate-audit.{json,html} hourly and both files have only ever
# existed here. If the disk dies, the record of what the estate looked like dies
# with it, and there is no off-Mac way to see the board.
#
# Founder, 2026-08-22, on what replaces the Hermes agent:
#   "Ship audit JSON to R2, have Fly pull and render static HTML. No agent."
#   "Push state, don't replicate logic."
# This is the push half. Nothing here runs an agent, makes a decision, or renders.
#
# WHY rclone AND NOT boto3. hermes_lease.py builds a boto3 R2 client and boto3 is
# installed under no python3 on this machine, so that path has never run here:
#
#     /usr/bin/python3       -> boto3 MISSING
#     /usr/local/bin/python3 -> boto3 MISSING
#
# rclone is installed and is what scripts/exit-drill.sh already uses, with flags
# only and no rclone.conf — a drill that needs a config file somebody set up once
# stops working on the machine that has to do the restoring.
#
# WHY THE READBACK. "rclone said ok" is one angle and it is the weaker one. This
# re-downloads what it just wrote and compares sha256 against the local file. A
# push is proven when the bytes come back, not when the client returns 0. That
# readback is also the LAW 19 restore drill for this object: the command that gets
# the data out runs every time the command that puts it in does.
#
# SECRETS. rclone is configured through RCLONE_S3_* environment variables, never
# flags. exit-drill.sh passes --s3-secret-access-key on the command line, which
# puts the secret in argv where any `ps` on this box can read it. Nothing here
# prints, logs or echoes a credential.
set -euo pipefail

STATE="$HOME/.claude/state"
RECEIPTS="$STATE/estate-push.jsonl"
# Credentials live outside Documents and outside the retiring Hermes tree. ~/.hermes
# is a symlink to ~/Documents/code/hermes, and macOS TCC refuses a bootstrapped
# LaunchAgent that read: this job failed on schedule with "awk: can't open file" while
# the identical command passed by hand. ~/.hermes/.env stays as a fallback only until
# crew #13 retires it.
ENVFILE="$HOME/.config/estate/estate.env"
[ -r "$ENVFILE" ] || ENVFILE="$HOME/.hermes/.env"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() {
  log "PUSH RED: $*"
  python3 -c "
import sys,os
# This script's own directory first. ~/.hermes is retired and symlinks into ~/Documents,
# which macOS TCC hides from a bootstrapped LaunchAgent. estate_cost_sentinel had this
# same line and silently ran the dead tree's copy of estate_alert.
sys.path.insert(0,os.path.join(os.environ.get('GUARDS_ROOT','%s/..' % os.path.dirname(os.path.abspath(__file__))),'estate'))
import estate_alert
estate_alert.send_operator_alert('estate_push RED: $* — the estate board is not leaving this Mac.',
                                 debounce_key='estate-push-red', debounce_s=3600)" 2>/dev/null || true
  exit 1
}

# --- credentials, read into this process only -------------------------------
# Environment wins, then the file, then the default. The default matters: R2_BUCKET
# is set in the interactive shell and in NO file, so a job that relied on inheriting
# it would pass by hand and fail under launchd, which sees almost no environment.
getenv() {
  eval "v=\${$1:-}"
  [ -n "$v" ] && { printf %s "$v"; return; }
  awk -F= -v k="$1" '$1==k{sub(/^[^=]*=/,"");gsub(/^["'"'"']|["'"'"']$/,"");print;exit}' "$ENVFILE"
}
R2_ACCOUNT_ID=$(getenv R2_ACCOUNT_ID)
R2_BUCKET=$(getenv R2_BUCKET); R2_BUCKET="${R2_BUCKET:-prospector-packs}"
# No config file by design (same reasoning as exit-drill.sh). Say so once here rather
# than letting rclone print a NOTICE on every call — noise is what killed the last loop.
export RCLONE_CONFIG=/dev/null
export RCLONE_S3_PROVIDER=Other
export RCLONE_S3_REGION=auto
export RCLONE_S3_FORCE_PATH_STYLE=true
export RCLONE_S3_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_S3_ACCESS_KEY_ID=$(getenv R2_ACCESS_KEY_ID)
export RCLONE_S3_SECRET_ACCESS_KEY=$(getenv R2_SECRET_ACCESS_KEY)

command -v rclone >/dev/null || die "rclone is not installed"
[ -n "$R2_ACCOUNT_ID" ] && [ -n "$R2_BUCKET" ] || die "R2_ACCOUNT_ID or R2_BUCKET missing from $ENVFILE"
[ -n "$RCLONE_S3_SECRET_ACCESS_KEY" ] || die "R2 credentials missing from $ENVFILE"

# --- the artefacts ----------------------------------------------------------
JSON="$STATE/estate-audit.json"
HTML="$STATE/estate-audit.html"
[ -s "$JSON" ] || die "$JSON is missing or empty — estate_audit.py has not produced state"

# Staleness is a real failure and it is silent. An audit file from yesterday
# pushes perfectly and tells the reader a lie about now.
AGE=$(( $(date +%s) - $(stat -f %m "$JSON") ))
[ "$AGE" -lt 10800 ] || die "estate-audit.json is $((AGE/60)) minutes old — the audit has stopped, pushing it would ship a stale board"

DAY=$(date -u +%Y-%m-%d)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

if [ "$DRY" = 1 ]; then
  log "DRY RUN. would push:"
  log "  $JSON -> :s3:$R2_BUCKET/estate/latest.json  and  estate/$DAY/$STAMP.json"
  log "  $HTML -> :s3:$R2_BUCKET/estate/latest.html  and  estate/$DAY/$STAMP.html"
  log "bucket reachable? $(rclone lsd ":s3:$R2_BUCKET" >/dev/null 2>&1 && echo yes || echo NO)"
  exit 0
fi

# --- push: a dated copy that is never overwritten, and a stable latest ------
for pair in "$JSON:json" "$HTML:html"; do
  src="${pair%:*}"; ext="${pair##*:}"
  [ -s "$src" ] || { log "skip .$ext — not present"; continue; }
  rclone copyto "$src" ":s3:$R2_BUCKET/estate/$DAY/$STAMP.$ext" --s3-no-check-bucket \
    || die "upload of the dated .$ext failed"
  rclone copyto "$src" ":s3:$R2_BUCKET/estate/latest.$ext" --s3-no-check-bucket \
    || die "upload of latest.$ext failed"
  log "pushed .$ext"
done

# --- the second angle: read it back and compare the bytes -------------------
LOCAL_SHA=$(shasum -a 256 "$JSON" | cut -d' ' -f1)
REMOTE_SHA=$(rclone cat ":s3:$R2_BUCKET/estate/latest.json" | shasum -a 256 | cut -d' ' -f1)
[ "$LOCAL_SHA" = "$REMOTE_SHA" ] \
  || die "readback mismatch — local $LOCAL_SHA remote $REMOTE_SHA"

BYTES=$(stat -f %z "$JSON")
CRIT=$(python3 -c "
import json,sys
d=json.load(open('$JSON'))
c=d.get('counts') or {}
print(c.get('critical', d.get('critical','?')))" 2>/dev/null || echo '?')

printf '{"ts":%s,"iso":"%s","day":"%s","stamp":"%s","bytes":%s,"sha256":"%s","critical":"%s","verified":"readback"}\n' \
  "$(date +%s)" "$(date -u +%FT%TZ)" "$DAY" "$STAMP" "$BYTES" "$LOCAL_SHA" "$CRIT" >> "$RECEIPTS"

log "PUSH GREEN  bucket=$R2_BUCKET  key=estate/latest.json  bytes=$BYTES  sha256=${LOCAL_SHA:0:12}…  readback=identical"

#!/usr/bin/env bash
# estate_bundle_push.sh — get every commit that exists only on this Mac off this Mac.
#
# WHY. Measured 2026-08-23, after a fetch of every remote so the count is honest:
# 21 commits in ~/.claude exist on no remote and that repo HAS no remote. That is the
# whole control plane, from its first commit onward. Four more repos are in the same
# state (maestro 3, code 2, my-ebook-store 1, AwesomeProject 1), and ten repos that do
# have a remote carry work that was never pushed to it. One disk failure loses all of it.
#
# WHY NOT backup_agent_estate.py. That script exists, is careful, and does not close
# this. Its allow-list has no `.git`, so it copies a working tree and no history; it
# writes to a local --out path and uploads nowhere; it is on no schedule; and it has
# never produced a receipt on this machine. A backup nobody runs is LAW 28's exact case.
#
# WHY NOT `git push`. Pushing somebody's half-finished branch to a shared remote is a
# decision about what other people see, and it is not reversible by me. A bundle is a
# copy: it changes nothing, it needs no repo-visibility ruling from the founder, and
# `git clone the.bundle` restores it.
#
# WHAT IT PUSHES. For a repo with no remote, `--all`: every ref, whole history, because
# there is no other copy anywhere. For a repo that has a remote, only what the remote
# does not have (`--all --not --remotes`), which is a few hundred KB instead of a
# gigabyte. LIMITATION, written down so the next agent inherits it instead of finding
# it: restoring one of those incremental bundles needs the remote as well as the bundle.
# That is the correct trade while the remote exists; it is not a full escrow of GitHub.
#
# WHY THE READBACK AND THE CLONE. "rclone said ok" is one angle (LAW 15). This
# re-downloads the bytes and compares sha256, then clones the downloaded copy into a
# temp directory and checks the tip commit matches. A backup is proven by a restore,
# not by an upload, and the restore runs every time the upload does.
#
# SECRETS. rclone is configured through RCLONE_S3_* environment variables, never flags,
# so nothing reaches argv where `ps` can read it. A repo whose history contains a file
# matching key material is REFUSED, loudly, rather than shipped.
set -uo pipefail

# launchd hands a job almost no environment, and the first run under it died on
# "rclone is not installed" while the identical command passed by hand. The proven
# com.founder.estatepush job sets this in its plist; this line means the script does
# not depend on whoever calls it remembering to.
export PATH="${PATH:-}:/Users/chidionyema/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

STATE="$HOME/.claude/state"
RECEIPTS="$STATE/estate-bundle-push.jsonl"
WORK="${TMPDIR:-/tmp}/estate-bundles.$$"
ENVFILE="$HOME/.config/estate/estate.env"
[ -r "$ENVFILE" ] || ENVFILE="$HOME/.hermes/.env"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

#: One run at a time. launchd fires this every 3600s and a run over the iCloud tree
#: can exceed that, because iCloud stores files dataless and every object read pulls
#: them back over the network. Two overlapping runs would fight for the same $WORK
#: parent, the same receipts file and the same R2 keys. mkdir is atomic on the local
#: filesystem, so it is the lock. A holder whose pid is gone is a crash, not a run.
LOCKDIR="$HOME/.claude/state/estate-bundle-push.lock"
#: A dry run measures, it does not write, so it has no business holding the
#: write lock. It took it once, blocked on an iCloud read that never returned,
#: and every hourly run for the next hour exited 0 saying "already running".
#: The job looked green the whole time and copied nothing, which is the exact
#: shape of a backup that is a lie with a cron schedule.
if [ "$DRY" -eq 0 ]; then
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    holder=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
      held=$(( $(date +%s) - $(stat -f %m "$LOCKDIR" 2>/dev/null || date +%s) ))
      printf '%s already running as pid %s, this run exits without doing anything\n' \
        "$(date -u +%FT%TZ)" "$holder"
      #: A skip leaves a receipt. A run that copies nothing and says nothing is
      #: indistinguishable from a run that copied everything, and the audit reads
      #: whichever one it finds last.
      printf '{"ts":%s,"at":"%s","event":"skipped","reason":"lock held by pid %s for %ss"}\n' \
        "$(date +%s)" "$(date -u +%FT%TZ)" "$holder" "$held" >> "$RECEIPTS"
      #: A skip that has gone on longer than two scheduled runs is not politeness,
      #: it is a wedged holder, and the job must go red so the audit sees it.
      [ "$held" -gt 7200 ] && exit 1
      exit 0
    fi
    printf '%s stale lock from pid %s, taking it\n' "$(date -u +%FT%TZ)" "${holder:-unknown}"
    rm -rf "$LOCKDIR"; mkdir "$LOCKDIR" || { echo "cannot take the lock"; exit 1; }
  fi
  echo $$ > "$LOCKDIR/pid"
  trap 'rm -rf "$LOCKDIR" "$WORK"' EXIT
else
  trap 'rm -rf "$WORK"' EXIT
fi

#: Roots to search. Three levels deep covers ~/Documents/code/<repo>/.git and no more.
# The iCloud root is not decoration. Found 2026-08-23 while chasing a worktree whose
# registration pointed there: a whole second code tree lives inside iCloud Drive, and
# its prospector checkout alone carried 84 commits no remote had. None of it was in
# any earlier version of this list, so none of it was ever backed up.
#: ~/.estate first, because it is the estate's own directory and the guards every
#: repository on this machine now inherits live in it. It was created 2026-08-23 and
#: no earlier version of this list knew about it, so it was covered by nothing.
ROOTS=("$HOME/.estate" "$HOME/.claude" "$HOME/.maestro" "$HOME/Documents/code" "$HOME/dev/code" "$HOME/code" \
       "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Documents/code")

#: A bundle larger than this is reported and skipped rather than silently uploaded.
#: No silent caps (LAW 28): a skip prints and lands in the receipt.
MAX_MB=${ESTATE_BUNDLE_MAX_MB:-250}

#: WEEKLY FULL ESCROW.
#: Everything else in this script bundles only what a remote does not already have.
#: That is the right hourly trade and it is NOT an escrow of GitHub -- the header
#: above says so: "restoring one of those incremental bundles needs the remote as well
#: as the bundle." Worse, a repo whose remote holds every commit is skipped outright by
#: the `[ "${n:-0}" -gt 0 ] || continue` below, so it produces no bundle at all.
#: Measured 2026-08-24 by drills/check_github_gone.py: of five declared load-bearing
#: repos, R2 held a standalone copy of two. The other three -- ~/.claude/scripts,
#: hermes-v2 and crew -- would have been lost with GitHub.
#: So once every FULL_DAYS each DECLARED repo is bundled with --all regardless of what
#: the remote holds, and that bundle is cloned back standalone before the receipt is
#: written. Declared, not every repo: a full pass over all 30 checkouts on this machine
#: is 2.4 GB and most of it is dead client work. The declared twelve are ~500 MB.
FULL_DAYS=${ESTATE_BUNDLE_FULL_DAYS:-7}
#: A full bundle of prospector is 350 MB against an incremental one's kilobytes, so the
#: hourly ceiling would silently skip exactly the repos that matter most. R2 storage is
#: cheap and a silent skip is not (LAW 28).
FULL_MAX_MB=${ESTATE_BUNDLE_FULL_MAX_MB:-2048}
GUARDS_ROOT="${GUARDS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DECLARED_JSON="$GUARDS_ROOT/estate/load-bearing.json"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
ask_remote() {
  GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/usr/bin/true \
    timeout 15 git -C "$1" ls-remote --exit-code "$2" HEAD >/dev/null 2>&1
}
die() { log "BUNDLE PUSH RED: $*"; alert "estate_bundle_push RED: $* — commits that exist only on this Mac are still only on this Mac."; rm -rf "$WORK"; exit 1; }

alert() {
  python3 - "$1" <<'PY' 2>/dev/null || true
import os, sys
# This script's own directory first. ~/.hermes is retired and is a symlink into
# ~/Documents, which macOS TCC hides from a bootstrapped LaunchAgent; putting it at
# sys.path[0] is what made estate_cost_sentinel import the dead tree's copy.
sys.path.insert(0, os.path.join(os.environ.get("GUARDS_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "estate"))
try:
    import estate_alert
except Exception as exc:
    print(f"[bundle-push] alerting unavailable: {exc!r}", file=sys.stderr)
    raise SystemExit(0)
estate_alert.send_operator_alert(sys.argv[1], debounce_key="estate-bundle-push", debounce_s=3600)
PY
}

# --- credentials, read into this process only -------------------------------
getenv() {
  eval "v=\${$1:-}"
  [ -n "$v" ] && { printf %s "$v"; return; }
  awk -F= -v k="$1" '$1==k{sub(/^[^=]*=/,"");gsub(/^["'"'"']|["'"'"']$/,"");print;exit}' "$ENVFILE"
}
R2_ACCOUNT_ID=$(getenv R2_ACCOUNT_ID)
R2_BUCKET=$(getenv R2_BUCKET); R2_BUCKET="${R2_BUCKET:-prospector-packs}"
export RCLONE_CONFIG=/dev/null
export RCLONE_S3_PROVIDER=Other
export RCLONE_S3_REGION=auto
export RCLONE_S3_FORCE_PATH_STYLE=true
export RCLONE_S3_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_S3_ACCESS_KEY_ID=$(getenv R2_ACCESS_KEY_ID)
export RCLONE_S3_SECRET_ACCESS_KEY=$(getenv R2_SECRET_ACCESS_KEY)

command -v rclone >/dev/null || die "rclone is not installed"
[ -n "$R2_ACCOUNT_ID" ] || die "R2_ACCOUNT_ID missing from $ENVFILE"
[ -n "$RCLONE_S3_SECRET_ACCESS_KEY" ] || die "R2 credentials missing from $ENVFILE"

mkdir -p "$WORK" "$STATE" || die "cannot create $WORK"
DAY=$(date -u +%Y-%m-%d); STAMP=$(date -u +%Y%m%dT%H%M%SZ)

# --- which repos, deduplicated by object store ------------------------------
# Every wt-* directory under ~/Documents/code is a git WORKTREE of prospector and
# reports prospector's own .git as its common dir. Bundling each one would upload the
# same 495 commits thirty times.
# --- every declared root must be READABLE, or this job is lying --------------
# Measured 2026-08-23. By hand this found 19 repos; bootstrapped by launchd it found 10
# and printed GREEN. The nine it lost were every repo under ~/Documents, which macOS TCC
# hides from a bootstrapped LaunchAgent:
#
#     ls: /Users/chidionyema/Documents/code: Operation not permitted
#
# A backup that silently covers half of what it is asked to cover is worse than none,
# because the receipt reads as complete. So a root that cannot be listed is RED.
#
# The test is a real listing, not `test -r`. Under that denial `test -d` answers yes and
# `test -r` answers yes; only an actual read fails. A guard built on -r passes exactly
# when it is needed.
denied=""
for root in "${ROOTS[@]}"; do
  [ -d "$root" ] || continue                       # not on this machine, nothing to cover
  ls "$root" >/dev/null 2>&1 || denied="$denied $root"
done
# A denied root is dropped from the search and the run ends RED. Aborting outright was
# the first version and it was wrong: it left ~/.claude, which is readable and is the one
# repo with no remote at all, with no hourly backup because a DIFFERENT root was blocked.
# Cover what is reachable, and be loud about what is not.
DENIED_ROOTS="$denied"
if [ -n "$DENIED_ROOTS" ]; then
  log "DENIED:$DENIED_ROOTS — not listable by this process, every repo under there is skipped"
  keep=()
  for root in "${ROOTS[@]}"; do
    case " $DENIED_ROOTS " in *" $root "*) continue ;; esac
    keep+=("$root")
  done
  ROOTS=("${keep[@]}")
  [ "${#ROOTS[@]}" -gt 0 ] || die "every declared root is unreadable by this process"
fi

#: Every git call in the planning loop below is wrapped, because a repo on the iCloud
#: tree can block one forever. iCloud stores files dataless and a read waits on a
#: network fetch that has no deadline. Measured 2026-08-23: a run sat 1h55m inside
#: `rev-list --count --all --not --remotes` on the CloudDocs copy of haworks-platform,
#: holding the lock above the whole time, so the 15:05 and 16:05 runs both exited
#: "already running" and the estate went three hours with no backup while every line
#: in the log still read fine. The comment at the top of this file already named this
#: class and no guard was ever built. This is the guard. A repo that stalls is
#: UNCOVERED, it is named in the receipt, and it turns the run RED, because a repo
#: silently dropped from the plan is the exact lie this script exists to stop.
GIT_PLAN_TIMEOUT="${GIT_PLAN_TIMEOUT:-25}"
#: The stall goes to a file, never a variable: every caller sits inside a $( ) command
#: substitution, which is a subshell, so an assignment would not survive it. For the
#: same reason this must not print, because its stdout is the caller's value.
gplan() {
  gp_repo="$1"; shift
  GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/usr/bin/true \
    timeout "$GIT_PLAN_TIMEOUT" git -C "$gp_repo" "$@"
  gp_rc=$?
  [ "$gp_rc" -eq 124 ] && printf '%s\t%s\n' "$gp_repo" "$1" >> "$WORK/stalled.tsv"
  return $gp_rc
}

find "${ROOTS[@]}" -maxdepth 3 -name .git -print 2>/dev/null | sed 's|/\.git$||' | sort -u \
  > "$WORK/candidates.txt"

#: A repo inside the iCloud container is enumerated and then NOT planned, and the
#: reason is disk rather than time. iCloud keeps objects dataless, so any command
#: that walks a repo's history materialises every object it touches onto the local
#: disk before it can read it. Measured 2026-08-23: `git log --all --name-only` on
#: the CloudDocs copy of the-introduction-exchange ran 37 minutes and free space on
#: /System/Volumes/Data fell 6.3G -> 1.9G -> 0.42G while macOS evicted as fast as git
#: downloaded, with fileproviderd at 39% CPU. Two sessions measured that independently
#: and agreed. The 25s stall timeout above does not help: each individual call returns,
#: and the disk fills anyway.
#:
#: So these are named, counted and reported UNCOVERED rather than attempted. A silent
#: drop is the exact lie this script exists to stop, and an attempt that fills the disk
#: takes the whole machine down with it.
#:
#: THE FIX IS THE FOUNDER'S AND IT IS ONE TOGGLE: turn iCloud Drive off for that folder
#: with "Keep a Copy", which writes the container out as a plain local directory. After
#: that these repos are ordinary repos and this filter stops matching anything.
ICLOUD="$HOME/Library/Mobile Documents"
: > "$WORK/uncovered.txt"
if [ -s "$WORK/candidates.txt" ]; then
  grep -F "$ICLOUD" "$WORK/candidates.txt" > "$WORK/uncovered.txt" 2>/dev/null || true
  grep -vF "$ICLOUD" "$WORK/candidates.txt" > "$WORK/candidates.local" 2>/dev/null || true
  mv "$WORK/candidates.local" "$WORK/candidates.txt"
fi
UNCOVERED_N=$(wc -l < "$WORK/uncovered.txt" | tr -d ' ')
if [ "${UNCOVERED_N:-0}" -gt 0 ]; then
  log "UNCOVERED BY DESIGN: $UNCOVERED_N repo(s) live inside the iCloud container and are not bundled."
  while read -r u; do log "  uncovered: $u"; done < "$WORK/uncovered.txt"
  log "  reason: walking a dataless repo materialises every object and fills the local disk."
  log "  fix: iCloud Drive off for that folder with Keep a Copy, then they become ordinary repos."
  #: Deliberately no alert() here. alert() debounces on one key per script, so an hourly
  #: informational alert would swallow a genuine RED raised in the same hour. This is a
  #: standing condition with a one-toggle fix that only the founder can make, so it goes
  #: in the log and in the summary line, where it is read without masking anything.
fi

# --- which declared repos are overdue a standalone escrow -------------------
# Read from the receipts, not a second state file: the receipt already records the
# mode and the restore verdict per slug, and a state file that drifts from the
# receipts would be a second answer to one question.
: > "$WORK/full-fresh.txt"; : > "$WORK/declared.txt"
python3 - "$RECEIPTS" "$FULL_DAYS" "$DECLARED_JSON" "$WORK/full-fresh.txt" "$WORK/declared.txt" <<'PYFRESH' || log "escrow planning unavailable, falling back to incremental only"
import json, os, sys, time
receipts, days, declared_p, fresh_p, declared_out = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
try:
    declared = [os.path.realpath(os.path.expanduser(e["path"]))
                for e in json.load(open(declared_p))["repos"]]
except Exception as exc:
    print(f"[escrow] cannot read {declared_p}: {exc!r}", file=sys.stderr); raise SystemExit(1)
open(declared_out, "w").write("\n".join(declared) + "\n")
cut, fresh = time.time() - days * 86400, set()
try:
    for line in open(receipts):
        line = line.strip()
        if not line: continue
        try: r = json.loads(line)
        except Exception: continue
        if (str(r.get("mode", "")).startswith("full") and r.get("restore") == "clone-ok"
                and float(r.get("ts", 0)) > cut and r.get("slug")):
            fresh.add((r["slug"], str(r.get("tip") or "")))
except FileNotFoundError:
    pass
# slug and the tip that bundle captured. Freshness alone let a repo whose
# remote holds everything keep a 1.1-day-old escrow while its disk moved on:
# .claude sat at e0a652c in R2 and a2dc73b on disk, and the age rule called
# that fresh. If GitHub is what you lost, an escrow behind by a day is the
# day of work you lost with it.
open(fresh_p, "w").write("\n".join("%s %s" % t for t in sorted(fresh)) + "\n")
PYFRESH

: > "$WORK/plan.tsv"
seen_common=""
while read -r d; do
  common=$(gplan "$d" rev-parse --git-common-dir 2>/dev/null) || continue
  case "$common" in /*) ;; *) common="$d/$common" ;; esac
  common=$(cd "$common" 2>/dev/null && pwd -P) || continue
  case "$seen_common" in *"|$common|"*) continue ;; esac
  seen_common="$seen_common|$common|"

  remote=$(gplan "$d" remote | head -1)
  #: A shallow clone (git clone --depth N) bundles into a file that `git bundle verify`
  #: calls a complete history and `git clone` refuses: "remote did not send all necessary
  #: objects". Measured 2026-08-27 by the recovery drill (idp run 33100565959, crew#300):
  #: Documents/code/hermes-v2.ARCHIVED.20260822/hermes-agent-self-evolution, depth 1 of
  #: NousResearch's repo, was the one broken bundle of 47. Its history lives on its remote;
  #: what this disk holds cannot be restored from, so it is not escrowed, and the whole
  #: prefix it left in R2 is withdrawn. Run df9d7e69 (2026-08-28 01:05Z) removed only
  #: latest.bundle; the recover drill (idp run 33131027676) then still found full-latest.bundle
  #: and the dated copies -- the same unrestorable bytes under other names. A dated copy of a
  #: shallow bundle is no more a backup than the latest one.
  if [ "$(gplan "$d" rev-parse --is-shallow-repository 2>/dev/null)" = true ]; then
    sslug=$(printf %s "${d#$HOME/}" | tr '/' '-' | tr -cd 'A-Za-z0-9._-')
    log "shallow: $(basename "$d") is a depth-limited clone of ${remote:-no remote}; a bundle of it cannot be cloned back, so it is not escrowed"
    if [ "${DRY:-0}" != 1 ] && [ -n "$sslug" ] && rclone lsf ":s3:$R2_BUCKET/bundles/$sslug/" 2>/dev/null | grep -q .; then
      rclone purge ":s3:$R2_BUCKET/bundles/$sslug" 2>/dev/null \
        && log "withdrew bundles/$sslug/: an unrestorable bundle is not a backup, under any name" \
        || log "could not withdraw bundles/$sslug/"
    fi
    SHALLOW_N=$((${SHALLOW_N:-0}+1)); continue
  fi
  if [ -n "$remote" ]; then
    n=$(gplan "$d" rev-list --count --all --not --remotes 2>/dev/null || echo 0)
    mode=incremental
    # A remote-tracking ref proves a remote answered ONCE, not that it answers now.
    # Measured 2026-08-23: 13 repos under ~/code point at dev.azure.com/OSLSoftware,
    # a former client's server. Every one reported 0 commits no remote has, so every
    # one was skipped, and not one of them can be cloned back. The cached ref was
    # reporting safety on behalf of a server that is gone.
    #
    # Only asked when the count is 0, which is exactly when the answer changes what
    # happens, so the hourly job pays one ls-remote per otherwise-skipped repo.
    # Asked twice, because one failure does not mean the server is gone. Measured
    # 2026-08-23: dev-code-hermes-v2 points at github.com and answers in under a
    # second, but a single ls-remote failed right after a six-minute 114 MB upload
    # and the job turned RED on a repo that was never at risk. One blip is a blip.
    if [ "${n:-0}" -eq 0 ]; then
      if ! ask_remote "$d" "$remote"; then
        sleep 3
        if ! ask_remote "$d" "$remote"; then
          n=$(gplan "$d" rev-list --count --all 2>/dev/null || echo 0)
          mode=full-unreachable-remote
          log "unreachable: $(basename "$d") points at $remote which did not answer twice, so its $n commit(s) exist only on this disk"
        fi
      fi
    fi
  else
    n=$(gplan "$d" rev-list --count --all 2>/dev/null || echo 0)
    mode=full
  fi
  # A declared repo with no standalone bundle newer than FULL_DAYS is planned full,
  # whatever the remote holds. This is the only line that makes R2 a GitHub escrow.
  if grep -qxF "$(cd "$d" && pwd -P)" "$WORK/declared.txt" 2>/dev/null; then
    probe=$(printf %s "${d#$HOME/}" | tr '/' '-' | tr -cd 'A-Za-z0-9._-')
    tip_now=$(gplan "$d" rev-parse HEAD 2>/dev/null || echo none)
    if ! grep -qxF "$probe $tip_now" "$WORK/full-fresh.txt" 2>/dev/null; then
      n=$(gplan "$d" rev-list --count --all 2>/dev/null || echo 0)
      mode=full-escrow
      if grep -qE "^$probe " "$WORK/full-fresh.txt" 2>/dev/null; then
        log "escrow due: $(basename "$d") has a standalone bundle but it is not at ${tip_now:0:12}, planning --all"
      else
        log "escrow due: $(basename "$d") has no standalone bundle in R2 newer than ${FULL_DAYS}d, planning --all"
      fi
    fi
  fi

  #: A repo whose git call timed out has no honest commit count, so it is never
  #: planned on a made-up zero. It is already on the stall list and the run goes RED.
  if cut -f1 "$WORK/stalled.tsv" 2>/dev/null | grep -qxF "$d"; then continue; fi
  [ "${n:-0}" -gt 0 ] || continue
  printf '%s\t%s\t%s\t%s\n' "$d" "$mode" "$n" "${remote:-NO-REMOTE}" >> "$WORK/plan.tsv"
done < "$WORK/candidates.txt"

STALLED_N=0
STALLED_LIST=""
if [ -s "$WORK/stalled.tsv" ]; then
  STALLED_N=$(cut -f1 "$WORK/stalled.tsv" | sort -u | wc -l | tr -d ' ')
  STALLED_LIST=$(cut -f1 "$WORK/stalled.tsv" | sort -u | sed 's|^| |' | tr -d '\n')
  while IFS=$'\t' read -r sr sc; do
    log "STALLED: git $sc did not return in ${GIT_PLAN_TIMEOUT}s for $sr, so that repo is UNCOVERED by this run"
  done < "$WORK/stalled.tsv"
fi

PLANNED=$(wc -l < "$WORK/plan.tsv" | tr -d ' ')
log "at risk: $PLANNED repo(s) carry commits no remote has"
if [ "$PLANNED" -eq 0 ]; then
  if [ "$STALLED_N" -gt 0 ]; then
    log "BUNDLE PUSH RED  nothing was planned and $STALLED_N repo(s) hung git for over ${GIT_PLAN_TIMEOUT}s:$STALLED_LIST"
    alert "estate_bundle_push: $STALLED_N repo(s) hung git and are NOT backed up:$STALLED_LIST"
    rm -rf "$WORK"; exit 1
  fi
  log "BUNDLE PUSH GREEN  nothing at risk  uncovered_icloud=${UNCOVERED_N:-0}"; rm -rf "$WORK"; exit 0
fi

if [ "$DRY" = 1 ]; then
  log "DRY RUN. would bundle and push to :s3:$R2_BUCKET/bundles/<repo>/"
  awk -F'\t' '{printf "  %-28s %-12s %5s commit(s)  remote=%s\n",$1,$2,$3,$4}' "$WORK/plan.tsv"
  log "bucket reachable? $(rclone lsd ":s3:$R2_BUCKET" >/dev/null 2>&1 && echo yes || echo NO)"
  rm -rf "$WORK"; exit 0
fi

# --- bundle, push, read back, restore ---------------------------------------
OK=0; FAILED=0; SKIPPED=0
while IFS=$'\t' read -r d mode n remote; do
  slug=$(printf %s "${d#$HOME/}" | tr '/' '-' | tr -cd 'A-Za-z0-9._-')
  bundle="$WORK/$slug.bundle"

  # LAW 21. A file whose NAME is key material is refused rather than shipped. This is
  # the cheap decisive control; the receipt says plainly that contents were not scanned,
  # so nobody later reads this as a clean bill of health for the bytes inside.
  if git -C "$d" log --all --name-only --pretty=format: 2>/dev/null \
       | grep -qiE '(^|/)(\.env|id_rsa|id_ed25519|id_ecdsa)|\.(pem|key|p12|pfx|jks|der)$'; then
    log "REFUSED $slug — its history contains key material by filename; not uploading"
    SKIPPED=$((SKIPPED+1))
    printf '{"ts":%s,"iso":"%s","repo":"%s","outcome":"refused-key-material"}\n' \
      "$(date +%s)" "$(date -u +%FT%TZ)" "$d" >> "$RECEIPTS"
    continue
  fi

  if case "$mode" in full*) true ;; *) false ;; esac; then
    git -C "$d" bundle create "$bundle" --all >/dev/null 2>&1
  else
    git -C "$d" bundle create "$bundle" --all --not --remotes >/dev/null 2>&1
  fi
  [ -s "$bundle" ] || { log "FAILED $slug — git bundle produced nothing"; FAILED=$((FAILED+1)); continue; }

  bytes=$(stat -f %z "$bundle")
  # A full escrow is meant to be big; judging it by the incremental ceiling would skip
  # every repo the escrow exists for.
  # Only the declared escrow gets the bigger ceiling. full-unreachable-remote fires on
  # thirteen dead client repos under ~/code, one of them 7,454 commits, and raising
  # their ceiling would quietly start paying to store a former client's history.
  ceiling=$MAX_MB; ceiling_var=ESTATE_BUNDLE_MAX_MB
  case "$mode" in full-escrow) ceiling=$FULL_MAX_MB; ceiling_var=ESTATE_BUNDLE_FULL_MAX_MB ;; esac
  if [ "$bytes" -gt $((ceiling * 1048576)) ]; then
    log "SKIPPED $slug — bundle is $((bytes/1048576)) MB, over the ${ceiling} MB ceiling. Raise $ceiling_var or push the branch."
    SKIPPED=$((SKIPPED+1)); rm -f "$bundle"; continue
  fi

  # A bundle git itself will not open is not a backup. Checked before it is uploaded.
  git -C "$d" bundle verify "$bundle" >/dev/null 2>&1 \
    || { log "FAILED $slug — git bundle verify refused the file it just wrote"; FAILED=$((FAILED+1)); rm -f "$bundle"; continue; }

  key="bundles/$slug"
  rclone copyto "$bundle" ":s3:$R2_BUCKET/$key/$DAY/$STAMP.bundle" --s3-no-check-bucket 2>/dev/null \
    || { log "FAILED $slug — dated upload failed"; FAILED=$((FAILED+1)); rm -f "$bundle"; continue; }
  rclone copyto "$bundle" ":s3:$R2_BUCKET/$key/latest.bundle" --s3-no-check-bucket 2>/dev/null \
    || { log "FAILED $slug — latest upload failed"; FAILED=$((FAILED+1)); rm -f "$bundle"; continue; }
  # latest.bundle is whatever ran most recently, and next hour that is an incremental
  # one. The standalone copy therefore needs its own key or the escrow lasts an hour.
  if case "$mode" in full*) true ;; *) false ;; esac; then
    rclone copyto "$bundle" ":s3:$R2_BUCKET/$key/full-latest.bundle" --s3-no-check-bucket 2>/dev/null \
      || { log "FAILED $slug — full-latest upload failed"; FAILED=$((FAILED+1)); rm -f "$bundle"; continue; }
  fi

  # Angle two: the bytes come back identical.
  local_sha=$(shasum -a 256 "$bundle" | cut -d' ' -f1)
  back="$WORK/$slug.readback"
  rclone cat ":s3:$R2_BUCKET/$key/latest.bundle" > "$back" 2>/dev/null
  remote_sha=$(shasum -a 256 "$back" | cut -d' ' -f1)
  if [ "$local_sha" != "$remote_sha" ]; then
    log "FAILED $slug — readback mismatch"; FAILED=$((FAILED+1)); rm -f "$bundle" "$back"; continue
  fi

  # Angle three, and the only one that is a restore: clone the DOWNLOADED copy and
  # check its tip is the commit this repo is on. A full bundle clones standalone; an
  # incremental one cannot, by construction, so it is verified against this repo.
  tip=$(git -C "$d" rev-parse HEAD 2>/dev/null)
  drill=skipped
  if case "$mode" in full*) true ;; *) false ;; esac; then
    if git clone --quiet "$back" "$WORK/restore-$slug" >/dev/null 2>&1 \
       && [ "$(git -C "$WORK/restore-$slug" rev-parse HEAD 2>/dev/null)" = "$tip" ]; then
      drill=clone-ok
    else
      log "FAILED $slug — the uploaded bundle does not clone back to $tip"; FAILED=$((FAILED+1))
      rm -rf "$bundle" "$back" "$WORK/restore-$slug"; continue
    fi
  else
    if git -C "$d" bundle verify "$back" >/dev/null 2>&1; then drill=verify-ok; else
      log "FAILED $slug — the downloaded bundle does not verify"; FAILED=$((FAILED+1))
      rm -f "$bundle" "$back"; continue
    fi
  fi

  printf '{"ts":%s,"iso":"%s","repo":"%s","slug":"%s","mode":"%s","commits":%s,"remote":"%s","bytes":%s,"sha256":"%s","tip":"%s","restore":"%s","contents_scanned":false}\n' \
    "$(date +%s)" "$(date -u +%FT%TZ)" "$d" "$slug" "$mode" "$n" "$remote" "$bytes" "$local_sha" "$tip" "$drill" >> "$RECEIPTS"
  log "pushed $slug  $mode  $n commit(s)  $((bytes/1024)) KB  restore=$drill"
  OK=$((OK+1))
  rm -rf "$bundle" "$back" "$WORK/restore-$slug"
done < "$WORK/plan.tsv"

rm -rf "$WORK"

if [ "${STALLED_N:-0}" -gt 0 ]; then
  log "BUNDLE PUSH RED  repos=$OK skipped=$SKIPPED  but $STALLED_N repo(s) hung git for over ${GIT_PLAN_TIMEOUT}s and were never planned:$STALLED_LIST"
  alert "estate_bundle_push: covered $OK repo(s), but $STALLED_N hung git and are NOT backed up:$STALLED_LIST"
  exit 1
fi

if [ "$FAILED" -gt 0 ]; then
  log "BUNDLE PUSH RED  ok=$OK failed=$FAILED skipped=$SKIPPED"
  alert "estate_bundle_push: $FAILED repo(s) failed to reach R2. ok=$OK skipped=$SKIPPED. Commits with no other copy are still on one disk."
  exit 1
fi
if [ -n "$DENIED_ROOTS" ]; then
  log "BUNDLE PUSH RED  repos=$OK skipped=$SKIPPED  but$DENIED_ROOTS could not be listed, so every repo under there is UNCOVERED by this run"
  alert "estate_bundle_push: covered $OK repo(s), but$DENIED_ROOTS is not readable by this job, so nothing under it is backed up. macOS TCC hides it from a bootstrapped LaunchAgent. One fix, once: grant Full Disk Access to /bin/bash in System Settings."
  exit 1
fi
log "BUNDLE PUSH GREEN  bucket=$R2_BUCKET  repos=$OK  skipped=$SKIPPED  shallow=${SHALLOW_N:-0}  uncovered_icloud=${UNCOVERED_N:-0}  key=bundles/<repo>/latest.bundle  restore=git clone <bundle>"
[ "$SKIPPED" -gt 0 ] && exit 2
exit 0

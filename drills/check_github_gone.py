#!/usr/bin/env python3
"""GitHub is gone. Prove the estate's history is still restorable without it.

WHY THIS IS NOT check_bundle_restore.py. That drill asks whether the newest bundle
receipt says a restore worked. It passes on a `verify-ok` receipt, and `verify-ok` is
an INCREMENTAL bundle checked against the local repo -- estate_bundle_push.sh says so
in its own header: "restoring one of those incremental bundles needs the remote as
well as the bundle ... it is not a full escrow of GitHub." So a green
estate-bundle-restore says nothing about surviving GitHub.

Worse, a repo whose remote already has every commit is skipped by the pusher outright
(`[ "${n:-0}" -gt 0 ] || continue`). Measured 2026-08-24: every load-bearing repo
except ~/.claude points at github.com, so R2 held no standalone copy of any of them.

WHAT A PASS MEANS. For every repo in estate/load-bearing.json, R2 holds a bundle that
was written with `git bundle create --all` and was cloned back standalone at upload
time (receipt mode starts with "full", restore == "clone-ok"), and it is fresher than
MAX_AGE_DAYS. That is a copy which needs rclone and git and nothing from GitHub.

BLIND, NOT GREEN. If the receipts file or the declaration cannot be read, this prints
BLIND and exits 2. A drill that loses its evidence reports BLIND, never a verdict.
"""
# --- guards root bootstrap: find the repo root from anywhere, no ~/.claude path ---
import pathlib as _gr_pl, sys as _gr_sys
_gr_here = _gr_pl.Path(__file__).resolve()
for _gr_p in [_gr_here.parent, *_gr_here.parents]:
    if (_gr_p / "guards_root.py").exists():
        _gr_sys.path.insert(0, str(_gr_p)); break
else:
    raise RuntimeError("guards_root.py not found above " + str(_gr_here))
# --- end bootstrap ---
import json
import os
import sys
import time

RECEIPTS = os.path.expanduser("~/.claude/state/estate-bundle-push.jsonl")
DECLARED = str(__import__("guards_root").GUARDS_ROOT / "estate" / "load-bearing.json")
#: One day of slack over the pusher's 7-day escrow cadence, so a single missed
#: weekly run is not an alert and two of them are.
MAX_AGE_DAYS = float(os.environ.get("GITHUB_GONE_MAX_AGE_DAYS", 8))


def slug_for(path):
    """The pusher's own slug rule, copied exactly: path under $HOME, slashes to dashes."""
    home = os.path.expanduser("~") + "/"
    rel = path[len(home):] if path.startswith(home) else path
    out = rel.replace("/", "-")
    return "".join(c for c in out if c.isalnum() or c in "._-")


def main():
    try:
        with open(DECLARED) as fh:
            repos = [e["path"] for e in json.load(fh)["repos"]]
    except Exception as exc:
        print(f"BLIND: cannot read {DECLARED}: {exc!r}")
        return 2
    if not repos:
        print(f"BLIND: {DECLARED} declares no repos, so this drill has nothing to grade")
        return 2

    try:
        with open(RECEIPTS) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        print(f"BLIND: no receipts at {RECEIPTS}; nothing has ever been pushed to R2")
        return 2
    except Exception as exc:
        print(f"BLIND: cannot read {RECEIPTS}: {exc!r}")
        return 2

    # Newest STANDALONE escrow per slug. A full bundle that failed its clone-back is
    # not an escrow, so restore == "clone-ok" is required and not merely the mode.
    newest = {}
    for r in rows:
        if not str(r.get("mode", "")).startswith("full"):
            continue
        if r.get("restore") != "clone-ok":
            continue
        s = r.get("slug")
        if not s:
            continue
        if r.get("ts", 0) > newest.get(s, {}).get("ts", 0):
            newest[s] = r

    cut = time.time() - MAX_AGE_DAYS * 86400
    missing, stale, ok = [], [], []
    for path in repos:
        s = slug_for(os.path.expanduser(path))
        row = newest.get(s)
        if row is None:
            missing.append((path, s))
        elif row["ts"] < cut:
            stale.append((path, (time.time() - row["ts"]) / 86400))
        else:
            ok.append((path, (time.time() - row["ts"]) / 86400, row.get("bytes", 0)))

    for path, age, size in ok:
        print(f"  ok       {path:<26} standalone bundle {age:.1f}d old, {size/1048576:.1f} MB")
    for path, age in stale:
        print(f"  STALE    {path:<26} newest standalone bundle is {age:.1f}d old, over {MAX_AGE_DAYS}")
    for path, s in missing:
        print(f"  MISSING  {path:<26} R2 holds no standalone bundle (slug {s})")

    if missing or stale:
        print(f"\nGITHUB-GONE RED: {len(ok)}/{len(repos)} load-bearing repos have a copy that "
              f"restores without github.com. {len(missing)} have none and {len(stale)} are stale.")
        print("Losing GitHub loses the history of every repo listed above.")
        print("Fix: estate_bundle_push.sh escrows a full bundle per declared repo every "
              "ESTATE_BUNDLE_FULL_DAYS days; run it and check the receipts.")
        return 1

    print(f"\nGITHUB-GONE GREEN: all {len(repos)} load-bearing repos restore from R2 alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

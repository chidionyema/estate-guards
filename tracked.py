#!/usr/bin/env python3
"""LAW 24: if it is load-bearing, it is in git.

Files this estate depends on live outside any repository. A scheduled job, the
laws, the settings that wire up every session. Any of them can be changed by an
agent with no record but a chat transcript. This tracks copies of them here and
fails when the copy and the live file drift apart.

    tracked.py --check    exit 1 on any difference. For CI and for any agent
                          about to trust the committed copy.
    tracked.py --pull     bring the live files in so the difference can be
                          committed.
    tracked.py --sync     pull, commit and push, with no person involved.
                          This is what the scheduled job runs. LAW 31: the
                          founder does not run scripts.

A stale copy is worse than none, because it reads as a record. That is what
--check is for.

LAW 21 outranks this and is enforced here rather than trusted. Every file is
scanned before it is copied in, and one that looks like it holds a credential is
refused, named, and left out. Directories that exist only to hold keys are not
listed at all -- they are in rebuild/PREREQUISITES.md, which records that they
must exist and what for, and never what is in them.
"""
import argparse
import filecmp
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "tracked.json")


HOME = os.path.expanduser("~")

#: Where the mirrored copies are written. HERE for every mode a person runs by
#: hand, because they are looking at this working tree. --sync replaces it, see
#: own_checkout below.
REPO_ROOT = HERE

#: A checkout that belongs to this job and to nothing else. Until 2026-08-24
#: --sync committed into HERE, which is ~/.claude/scripts: the tree every session
#: edits and 33 launchd jobs execute. Whenever a session had it on a branch, the
#: mirror was committed onto that session's branch and the push was skipped by
#: design. The board records what that cost on 2026-08-24 alone -- 15:53, 17:54,
#: 18:24, 18:54 and 19:43 committed 32, 1, 1, 3 and 34 files and pushed none of
#: them. The law that says everything is in git put nothing in git for four hours,
#: and the failure was reported as normal operation each time.
#:
#: A worktree detached at origin/main has no branch for anyone to be standing on,
#: so there is no case left where the commit lands somewhere it must not be pushed
#: from. Under Caches because it is derived: `git worktree add` rebuilds it, and
#: nothing here is the only copy of anything.
WORKTREE = os.path.join(HOME, "Library", "Caches", "estate", "tracked-worktree")


def own_checkout():
    """(path, "") for a worktree of this repository detached at origin/main, or
    (None, reason) when one cannot be had.

    None is not an outage. The caller falls back to HERE, which is exactly what
    this function replaced, so the worst case is the behaviour that shipped
    yesterday. A mirror taken in an awkward place is recoverable; a mirror not
    taken at all is the thing LAW 24 was written about.
    """
    import subprocess

    def run(*args, cwd=HERE):
        return subprocess.run(["git", "-C", cwd, *args],
                              capture_output=True, text=True, timeout=120)

    try:
        if not os.path.exists(os.path.join(WORKTREE, ".git")):
            os.makedirs(os.path.dirname(WORKTREE), exist_ok=True)
            r = run("worktree", "add", "--detach", WORKTREE, "origin/main")
            if r.returncode:
                return None, (r.stderr.strip() or r.stdout.strip())[:200]
        # Offline is not a reason to stop. A tip fetched an hour ago is still a
        # tip nobody is standing on, which is the property this whole function is
        # for; the push at the end is where being behind actually shows up.
        run("fetch", "--quiet", "origin", "main", cwd=WORKTREE)
        r = run("checkout", "--detach", "--quiet", "--force", "origin/main", cwd=WORKTREE)
        if r.returncode:
            return None, (r.stderr.strip() or r.stdout.strip())[:200]
        return WORKTREE, ""
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)[:200]


def entries(into=None):
    """into=DIR aims every live path at a throwaway home instead of this one.

    That is what makes the rebuild drill runnable without an admin password and
    without a second user account. A drill you cannot run is the reason this one
    had never been run (LAW 19: an exit that has never been drilled is a hope)."""
    for e in json.load(open(MANIFEST)):
        live = os.path.expanduser(e["live"])
        if into:
            live = os.path.join(into, os.path.relpath(live, HOME)) \
                if live.startswith(HOME + os.sep) else live
        e["live"] = live
        e["repo_abs"] = os.path.join(REPO_ROOT, e["repo"])
        yield e


# Enough to catch a key pasted into a config file. It does not have to be
# clever, only refuse rather than pass when it is unsure.
SECRET = re.compile(
    r"(sk-ant-|sk-proj-|sk-[A-Za-z0-9]{20,}|ghp_|gho_|ghu_|ghs_|github_pat_"
    r"|xox[baprs]-|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|AIza[0-9A-Za-z_-]{30,}|\b\d{8,10}:[A-Za-z0-9_-]{35}\b"
    r"|AGE-SECRET-KEY-1|<RSAKeyValue>"
    r'''|["']?(api[_-]?key|secret|password|token)["']?\s*[:=]\s*["'][^"'\s]{16,}'''
    # A bare token NAME is not a token. `oauth_token` on its own matched every
    # sentence that mentions the variable, which made the gate refuse prose and
    # taught people to reach for --no-verify. Require an assignment and a value.
    r'''|\b(oauth[_-]?token|refresh[_-]?token|access[_-]?token)\b\s*[:=]\s*\S{16,}''' r")",
    re.IGNORECASE)


def looks_secret(path):
    """The reason the file is refused, or None."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read(262144)
    except OSError as e:
        return f"unreadable: {e}"
    if b"\0" in blob:
        return None  # binary. Not a config file, so not tracked either way.
    m = SECRET.search(blob.decode("utf-8", "replace"))
    return f"looks like a credential ({m.group(0)[:6]}...)" if m else None


def names(d, pattern):
    if not os.path.isdir(d):
        return set()
    import fnmatch
    return {f for f in os.listdir(d) if fnmatch.fnmatch(f, pattern)}


def walk(root, exclude):
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if d != ".git" and not any(
            fnmatch.fnmatch(os.path.normpath(os.path.join(rel_dir, d)), x) for x in exclude)]
        for f in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, f))
            if any(fnmatch.fnmatch(rel, x) for x in exclude):
                continue
            out.add(rel)
    return out


def diff_one(e):
    """Returns (missing_in_repo, gone_from_live, changed) as lists of labels."""
    if e.get("tree"):
        ex = e.get("exclude", [])
        live = walk(e["live"], ex) if os.path.isdir(e["live"]) else set()
        repo = walk(e["repo_abs"], ex) if os.path.isdir(e["repo_abs"]) else set()
        changed = [f for f in sorted(live & repo)
                   if not filecmp.cmp(os.path.join(e["live"], f),
                                      os.path.join(e["repo_abs"], f), shallow=False)]
        return sorted(live - repo), sorted(repo - live), changed
    if "glob" in e:
        live, repo = names(e["live"], e["glob"]), names(e["repo_abs"], e["glob"])
        changed = [f for f in sorted(live & repo)
                   if not filecmp.cmp(os.path.join(e["live"], f),
                                      os.path.join(e["repo_abs"], f), shallow=False)]
        return sorted(live - repo), sorted(repo - live), changed
    # single file
    live_there, repo_there = os.path.exists(e["live"]), os.path.exists(e["repo_abs"])
    label = os.path.basename(e["live"])
    if live_there and not repo_there:
        return [label], [], []
    if repo_there and not live_there:
        return [], [label], []
    if live_there and repo_there and not filecmp.cmp(e["live"], e["repo_abs"], shallow=False):
        return [], [], [label]
    return [], [], []


REFUSED = []


def _copy(src, dst):
    """Copy unless the file looks like it holds a secret. LAW 21 outranks LAW 24."""
    why = looks_secret(src)
    if why:
        REFUSED.append((src, why))
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


#: Generated entries whose live copy drifted from the committed one. The repo is the
#: source for these, so the drift is reported, never mirrored (incident crew#13).
STALE_GENERATED = []


#: Entries whose live copy is byte-identical to a version already committed BEFORE HEAD.
#: Incident 2026-08-27 (crew#13, claude-guards aae334c): ~/.claude/settings.json and
#: ~/AGENTS-FULL.md had never been reinstalled after #118, #122 and the LAW 50 merge, so
#: this job read the stale live copies as "changed outside git" and pushed them over
#: main: LAW 50 deleted from the law text, every hook unwired from hook-run.py, main red.
#: A live copy that equals an old commit is behind git, not ahead of it; the repo is the
#: source and the copy runs the other way.
STALE_BEHIND = []


def behind_git(live_path, repo_rel):
    """True when live_path matches a blob of repo_rel committed before HEAD's version."""
    try:
        def run(*a):
            return subprocess.run(["git", "-C", REPO_ROOT, *a], capture_output=True,
                                  text=True, timeout=60, check=True).stdout.strip()
        blob = run("hash-object", live_path)
        shas = run("log", "--format=%H", "-n", "200", "HEAD", "--", repo_rel).split()
        for sha in shas[1:]:
            if run("rev-parse", "--verify", "-q", "%s:%s" % (sha, repo_rel)) == blob:
                return True
    except Exception:
        return False
    return False


def _restore_behind(e, live_path, repo_abs, repo_rel):
    STALE_BEHIND.append((repo_rel, live_path))
    shutil.copy2(repo_abs, live_path)
    return True


def pull_one(e):
    a, b, c = diff_one(e)
    if e.get("generated"):
        # Incident 2026-08-26 (crew#13, claude-guards#80 -> 442675d): a PR moved eight
        # committed plists off ~/.hermes; nobody re-rendered the live directory; this
        # job then copied the stale live plists back over the merged fix and pushed
        # straight to main. For an entry the repo generates, the committed copy is
        # the source and the live one is the derivative, so the copy only ever runs
        # the other way. Record the drift and touch nothing.
        if a or b or c:
            STALE_GENERATED.append((e["repo"], e["generated"], a + b + c))
        return 0, 0, 0
    if e.get("tree"):
        c = [f for f in c if not (behind_git(os.path.join(e["live"], f), e["repo"] + "/" + f)
             and _restore_behind(e, os.path.join(e["live"], f), os.path.join(e["repo_abs"], f), e["repo"] + "/" + f))]
        for f in a + c:
            _copy(os.path.join(e["live"], f), os.path.join(e["repo_abs"], f))
        for f in b:
            os.remove(os.path.join(e["repo_abs"], f))
        return len(a), len(b), len(c)
    if "glob" in e:
        os.makedirs(e["repo_abs"], exist_ok=True)
        c = [f for f in c if not (behind_git(os.path.join(e["live"], f), e["repo"] + "/" + f)
             and _restore_behind(e, os.path.join(e["live"], f), os.path.join(e["repo_abs"], f), e["repo"] + "/" + f))]
        for f in a + c:
            _copy(os.path.join(e["live"], f), os.path.join(e["repo_abs"], f))
        for f in b:
            os.remove(os.path.join(e["repo_abs"], f))
    else:
        if c and behind_git(e["live"], e["repo"]):
            _restore_behind(e, e["live"], e["repo_abs"], e["repo"])
            return 0, 0, 0
        if a or c:
            _copy(e["live"], e["repo_abs"])
        elif b:
            os.remove(e["repo_abs"])
    return len(a), len(b), len(c)


def restore_one(e, force):
    """Copy the committed copy back onto the machine. The reverse of pull_one.

    Refuses to overwrite a live file that differs unless --force. On a fresh
    machine nothing exists so nothing is refused; on a machine that already has
    an estate, silently overwriting is how a restore destroys the thing it was
    run to protect.
    """
    written, skipped = 0, []
    if e.get("tree") or "glob" in e:
        ex = e.get("exclude", [])
        repo = walk(e["repo_abs"], ex) if os.path.isdir(e["repo_abs"]) else set()
        if "glob" in e and not e.get("tree"):
            repo = names(e["repo_abs"], e["glob"])
        for f in sorted(repo):
            src, dst = os.path.join(e["repo_abs"], f), os.path.join(e["live"], f)
            if os.path.exists(dst) and not force:
                if not filecmp.cmp(src, dst, shallow=False):
                    skipped.append(f)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            written += 1
        return written, skipped
    if not os.path.exists(e["repo_abs"]):
        return 0, []
    if os.path.exists(e["live"]) and not force:
        if not filecmp.cmp(e["repo_abs"], e["live"], shallow=False):
            skipped.append(os.path.basename(e["live"]))
        return 0, skipped
    os.makedirs(os.path.dirname(e["live"]), exist_ok=True)
    shutil.copy2(e["repo_abs"], e["live"])
    return 1, []


BOARD = os.path.expanduser("~/.claude/ESTATE_BOARD.jsonl")


def board(kind, text, source="tracked.py"):
    """Every session is handed the board at startup. LAW 28: an instrument
    nobody reads is not an instrument, and a log file is nobody.

    Other estate jobs post here too -- rebuild/drill.sh is the first -- so the
    source is a parameter rather than a second copy of this function."""
    try:
        import datetime
        with open(BOARD, "a") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "from": source, "kind": kind, "text": text}) + "\n")
    except OSError:
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 215))
        except Exception: pass



#: The issue this guard's commits belong to. The commit-msg hook `ticket-default`
#: (crew#53) refuses any commit whose subject names no issue, whichever tool wrote
#: it, and on 2026-08-27T02:11Z it refused this guard's own commit: LAW 24 drift sat
#: uncommitted and the board said "could not commit". A guard that commits must
#: name its ticket like anyone else.
TICKET = "crew#13"


def commit_message(changed: list[str]) -> str:
    return ("LAW 24: %d load-bearing file(s) changed outside git (%s)\n\n%s\n\n"
            "Committed by the scheduled guard, not by a person.\n"
            % (len(changed), TICKET, "\n".join("  " + c for c in changed)))

def sync():
    """Pull the drift, commit it, push it. Report only what a person would act on."""
    import subprocess
    global REPO_ROOT
    root, why = own_checkout()
    if root is None:
        board("guard-broken",
              "tracked.py could not open its own checkout (%s), so it is mirroring "
              "into %s, which a session may be using. The commit may land on "
              "somebody's branch and go unpushed." % (why, HERE))
        root = HERE
    REPO_ROOT = root
    paths = sorted({e["repo"].split("/")[0] for e in entries()})

    moved = 0
    for e in entries():
        moved += sum(pull_one(e))

    if STALE_GENERATED:
        board("stale-generated",
              "tracked.py left %d generated entr%s alone because the live copy has "
              "drifted from the committed source: %s. The fix runs the generator, "
              "never a mirror back into git."
              % (len(STALE_GENERATED), "y" if len(STALE_GENERATED) == 1 else "ies",
                 "; ".join("%s (%d file(s), regenerate with `%s`)" % (r, len(f), g)
                           for r, g, f in STALE_GENERATED)))

    if STALE_BEHIND:
        board("stale-behind",
              "tracked.py reinstalled %d live file(s) that matched a version already committed "
              "before HEAD, instead of pushing the old copy over main: %s. The repo was ahead; "
              "the machine had not been re-rendered (crew#13, aae334c)."
              % (len(STALE_BEHIND), "; ".join("%s -> %s" % (r, lv) for r, lv in STALE_BEHIND)))

    if REFUSED:
        board("secret-refused",
              "tracked.py refused to commit %d file(s) that look like credentials: %s. "
              "They belong in rebuild/PREREQUISITES.md by name, never by value."
              % (len(REFUSED), ", ".join(os.path.basename(p) for p, _ in REFUSED)))

    def git(*args):
        return subprocess.run(["git", "-C", root, *args],
                              capture_output=True, text=True, timeout=120)

    # -uall, because plain --porcelain collapses a new directory to one line and
    # the count then reports 1 where 8 files changed.
    status = git("status", "--porcelain", "-uall", "--", *paths).stdout
    if not status.strip():
        return 0

    changed = [l[3:] for l in status.splitlines()]
    git("add", "--", *paths)
    # TOCTOU: `state/` is written continuously by other scheduled jobs, so the
    # drift `status` just saw can self-resolve (a file settles back to what
    # HEAD already has) before `add` reaches it. Confirmed 2026-08-24: `status`
    # found drift, `commit` then found nothing staged and failed with an empty
    # stderr, because git's "nothing to commit" message is on stdout, which
    # this function never read -- so the board alert said "could not commit: "
    # with no reason. Nothing was actually broken; check before alerting.
    if git("diff", "--cached", "--quiet").returncode == 0:
        return 0  # nothing actually staged: the drift self-resolved
    c = git("commit", "-m", commit_message(changed))
    if c.returncode:
        reason = (c.stderr.strip() or c.stdout.strip())[:300]
        board("guard-broken", "tracked.py could not commit: " + reason)
        return 1
    # Only main is this job's business to push to. In its own worktree that is
    # always true, because own_checkout detached it at origin/main a moment ago.
    # The check below is for the fallback path, where root is the shared checkout
    # and a session may have it on a branch: pushing that collides with the
    # session's own push and rewrites nothing usefully -- measured 2026-08-24,
    # "! [rejected] HEAD -> fix/spend-sentinel-refuses-false-zero
    # (non-fast-forward)". The commit above already satisfies LAW 24 locally.
    if root == HERE:
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != "main":
            board("tracked",
                  "committed %d load-bearing file(s) changed outside git on '%s', "
                  "not pushed: this checkout belongs to another session while it "
                  "is off main. Push reaches origin next time this runs on main."
                  % (len(changed), branch))
            return 0
    p = git("push", "origin", "HEAD:main")
    if p.returncode:
        board("guard-broken",
              "tracked.py committed %d changed file(s) but could not push: %s. "
              "The record is local only until someone pushes it."
              % (len(changed), p.stderr.strip()[:200]))
        return 1
    board("tracked", "committed and pushed %d load-bearing file(s) changed outside git: %s"
          % (len(changed), ", ".join(changed[:8])))
    return 0


def stale_gitlink():
    """The outer repo records a commit for scripts/. A clone reads that, not the
    submodule's own HEAD, so committing in the submodule and not in the parent
    leaves the remote describing a machine that no longer exists. It is silent:
    everything works here, and only a fresh clone finds out.

    Found by the first rebuild drill, which cloned a scripts/ three commits
    behind and failed on an unrecognised --restore."""
    import subprocess
    outer = os.path.dirname(HERE)
    r = subprocess.run(["git", "-C", outer, "submodule", "status", "--", HERE],
                       capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        return None
    line = r.stdout.strip()
    if not line.startswith("+"):
        return None
    head = subprocess.run(["git", "-C", HERE, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return (f"the parent repository pins an OLD scripts/ commit. Its HEAD is {head}, "
            f"the parent records something else.\n"
            f"    A fresh clone gets the recorded one. Fix with:\n"
            f"        git -C {outer} add scripts && git -C {outer} commit -m 'point at {head}'")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--restore", action="store_true",
                    help="copy the committed files back onto the machine")
    ap.add_argument("--into", default=None,
                    help="treat DIR as the home directory. For the drill.")
    ap.add_argument("--force", action="store_true",
                    help="with --restore, overwrite live files that differ")
    a = ap.parse_args()

    if a.sync:
        return sync()

    if a.restore:
        total, blocked, generated = 0, [], []
        for e in entries(a.into):
            if e.get("generated"):
                generated.append((e["repo"], e["generated"]))
                continue
            n, skipped = restore_one(e, a.force)
            total += n
            blocked += [f"{e['repo']}/{f}" for f in skipped]
            if n:
                print(f"{e['repo']}: restored {n} file(s) to {e['live']}")
        print(f"\nrestored {total} file(s)"
              + (f" into {a.into}" if a.into else " onto this machine"))
        if blocked:
            print(f"{len(blocked)} live file(s) differ and were left alone. "
                  f"--force overwrites them:")
            for f in blocked[:20]:
                print(f"    {f}")
        for repo, how in generated:
            print(f"\n{repo}: NOT restored, it is generated. Run:  {how}")
            print("    These files have the home directory baked into them. Copying "
                  "them onto a\n    new machine installs jobs pointing at the old one.")
        print("\nWhat is NOT restored, because it was never committed: every "
              "credential.\nrebuild/PREREQUISITES.md lists each one by name and "
              "how a new machine gets it.")
        return 0

    drift = 0
    for e in entries():
        miss, gone, chg = diff_one(e)
        if a.pull:
            n = pull_one(e)
            if any(n):
                print(f"{e['repo']}: pulled {n[0]} new, {n[2]} changed, {n[1]} removed")
                #: Naming them is the whole fix. On 2026-08-23 a pull silently put
                #: five vulnerable pins back into migration/requirements after a
                #: session had bumped them to close 36 dependabot alerts, and the
                #: only thing on screen was "2 changed". The 30-minute --sync job
                #: would have committed that revert with nobody's name on it. A
                #: pull that says WHICH committed file it overwrote turns a silent
                #: revert into one the agent sees in its own tool output.
                for f in sorted(miss) + sorted(chg):
                    where = "new" if f in miss else "OVERWROTE the committed copy"
                    print(f"    {where}: {e['repo']}/{f}" if e.get("tree") or "glob" in e
                          else f"    {where}: {e['repo']}")
                if chg:
                    print("    if a commit of yours was in there, it is gone from the "
                          "working tree now; check git diff before the sync job runs")
            continue
        if miss or gone or chg:
            drift += len(miss) + len(gone) + len(chg)
            print(f"{e['repo']}  ({e['why']})")
            for label, g in (("not committed", miss), ("gone from the machine", gone),
                             ("differs from the live file", chg)):
                for f in g:
                    print(f"    {label:26s} {f}")
        elif not a.check:
            print(f"{e['repo']}: in step")

    if a.pull:
        if REFUSED:
            print(f"\nREFUSED {len(REFUSED)} file(s). LAW 21: a secret is never committed.")
            for path, why in REFUSED:
                print(f"    {path}\n        {why}")
            print("    Record what these must contain in rebuild/PREREQUISITES.md, by name.")
        print("commit the result to record it")
        return 0
    stale = stale_gitlink()
    if stale:
        print(f"\n{stale}")
        drift += 1

    if drift:
        print(f"\n{drift} difference(s). LAW 24: run `tracked.py --pull`, then commit.")
        return 1 if a.check else 0
    print("in step: every tracked path matches its committed copy")
    return 0


if __name__ == "__main__":
    sys.exit(main())

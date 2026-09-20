#!/usr/bin/env python3
"""Everything load-bearing is in git, or this exits 1 (LAW 24).

Six classes, because answering the question for one of them and calling it
closed is how the last four holes survived:

  runners   every program a launchd job executes
  declared  the laws, settings, agent definitions, skills, job definitions
  repos     every estate repo clean and pushed, not just committed locally
  mirrors   the live copy and the committed copy still identical
  secrets   a credential file has a committed example naming its keys, and the
            values are still absent from the repo (LAW 21 outranks LAW 24)
  offsite   every repo has a recent, verified bundle off this machine, because
            "it is on GitHub" is one suspended account away from being wrong

What it is NOT allowed to do is pass because it did not look. Every class that
cannot run reports UNKNOWN and fails the sweep, never a quiet zero.
"""
import glob, json, os, plistlib, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from estate.ingit import covered, mirrors, HOME, REPO   # noqa: E402

DECL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "load-bearing.json")
FOREIGN = ("com.apple", "com.valvesoftware", "com.adobe", "com.docker",
           "com.cisco", "com.ollama", "com.openssh", "com.google", "com.microsoft")


def _generated(d):
    """Live paths are written with a ~ in the manifest; covered() compares absolutes."""
    return {os.path.expanduser(k): v for k, v in d.get("generated", {}).items()}


def _skipped(name, rules):
    """A vendor's job, or a definition somebody deliberately parked, is not ours."""
    return (name.startswith(tuple(rules.get("prefixes", [])))
            or name.endswith(tuple(rules.get("suffixes", [])))
            or any(c in name for c in rules.get("contains", [])))


def sh(args, cwd=None, t=60):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=t, cwd=cwd)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 127, "", f"{type(e).__name__}: {e}"


_PORCELAIN = re.compile(r"^\s*([A-Z?!ARCMDU ]{1,2})\s+(.*)$")


def porcelain(line):
    """Split one `git status --porcelain` line into (code, path).

    Never slice a porcelain line by column. `sh()` returns `stdout.strip()`,
    which eats the leading space of the FIRST line, so ' M gateway/x' arrives
    as 'M gateway/x' and a fixed `l[3:]` yields 'ateway/x'. That silently
    defeated always_dirty for hermes-v2's gateway/restart_loop.json on
    2026-08-24: the file was listed as noisy and still reported as a hole,
    every hour, because the path it was compared against was off by one.
    Swept 2026-08-24: four sites in ~/.claude/scripts column-slice porcelain;
    the other three (tracked.py:245, guard-autocommit.py:85/211,
    session-recorder.py:143) read raw unstripped stdout and are correct.
    """
    m = _PORCELAIN.match(line)
    if not m:
        return "", line.strip()
    return m.group(1).strip(), m.group(2).strip()


def _is_system(p):
    return (p.startswith(("/usr/", "/bin/", "/sbin/", "/System/", "/opt/homebrew/"))
            or os.path.basename(p) in ("python3", "python", "node", "bash", "sh",
                                       "zsh", "env", "caffeinate"))


def _programs(argv):
    """Program arguments naming a file, minus interpreters and minus flag values.
    `--out /x/y.json` is where a job WRITES, not what it runs."""
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            if "=" not in a and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                skip = True
            continue
        if a.startswith("/") and not _is_system(a):
            out.append(a)
    return out


def check_runners(d, mm):
    holes, ok, gen = [], 0, _generated(d)
    for f in sorted(glob.glob(os.path.join(HOME, "Library/LaunchAgents/*.plist"))):
        label = os.path.basename(f)[:-6]
        if _skipped(os.path.basename(f), d.get("skip_names", {})):
            continue
        try:
            argv = plistlib.load(open(f, "rb")).get("ProgramArguments", [])
        except Exception as e:
            holes.append(f"{label}: unreadable plist: {e}")
            continue
        for a in _programs(argv):
            if not os.path.exists(a):
                holes.append(f"{label}: target missing: {a}")
            elif covered(a, mm, gen) is None:
                holes.append(f"{label}: not in git: {a}")
            else:
                ok += 1
    return ok, holes


def check_declared(d, mm):
    holes, ok, gen = [], 0, _generated(d)
    skip = d.get("skip_names", {})
    for e in d["paths"]:
        p = os.path.expanduser(e["path"])
        if not os.path.exists(p):
            holes.append(f"{e['path']}: missing ({e['why']})")
            continue
        if os.path.isdir(p):
            # A directory is kept only if its files are. One uncovered file is a hole.
            files = [os.path.join(r, n) for r, _, ns in os.walk(p) for n in ns
                     if not n.startswith(".") and not _skipped(n, skip)]
            bad = [f for f in files if covered(f, mm, gen) is None]
            if bad:
                holes.append(f"{e['path']}: {len(bad)} of {len(files)} not in git "
                             f"(first: {os.path.relpath(bad[0], HOME)})")
            else:
                ok += 1
        elif covered(p, mm, gen) is None:
            holes.append(f"{e['path']}: not in git ({e['why']})")
        else:
            ok += 1
    return ok, holes


def check_repos(d, _mm):
    holes, ok = [], 0
    for e in d["repos"]:
        p = os.path.expanduser(e["path"])
        if not os.path.isdir(os.path.join(p, ".git")):
            holes.append(f"{e['path']}: not a git repo")
            continue
        # A shared checkout parked on a stray branch is how one session's
        # `git add -A` strands another session's files where main cannot see them.
        # It happened three times in one afternoon before anything looked for it.
        want = e.get("branch")
        cur = sh(["git", "-C", p, "rev-parse", "--abbrev-ref", "HEAD"])[1]
        if want and cur != want:
            holes.append(f"{e['path']}: checked out on '{cur}', not '{want}'; "
                         f"work committed here does not reach {want}")
        dirty = sh(["git", "-C", p, "status", "--porcelain"])[1].splitlines()
        # Untracked files are somebody's work in flight. Modified TRACKED files are
        # an edit to something the estate already depends on, which is the hole.
        noisy = tuple(d.get("always_dirty", {}).get("paths", []))
        mod = []
        for l in dirty:
            code, path = porcelain(l)
            if code == "??" or path.startswith(noisy):
                continue
            mod.append(l)
        sh(["git", "-C", p, "fetch", "-q", "origin"], t=90)
        # LAW 24 asks whether anything off this machine holds the commit, so
        # the measure is every remote ref, not the one branch @{u} names.
        # Grading against @{u} reported ".claude/scripts: 27 commit(s) never
        # pushed" on 2026-08-24 while 26 of them sat on a rescue branch that
        # had been pushed; and it reports a hole for a detached HEAD whose
        # commits are all on origin. Both are the same defect: a proxy for
        # "no remote has this" that is only true when HEAD tracks a branch.
        rcu, unre, _ = sh(["git", "-C", p, "rev-list", "--count", "HEAD",
                           "--not", "--remotes"])
        unreachable = unre.strip() if rcu == 0 and unre.strip() else "?"
        nrem = sh(["git", "-C", p, "remote"])[1].strip()
        if mod:
            holes.append(f"{e['path']}: {len(mod)} tracked file(s) edited and not committed")
        if not nrem:
            holes.append(f"{e['path']}: no remote configured, nothing off this machine holds it")
        elif unreachable == "?":
            holes.append(f"{e['path']}: could not count commits against its remotes, so it is unproven")
        elif unreachable != "0":
            holes.append(f"{e['path']}: {unreachable} commit(s) exist only on this disk")
        # A repository holding transcripts and this machine's paths can be
        # flipped to public in one click, and nothing on this machine notices.
        # claude-guards was found public on 2026-08-23 with 48 files carrying
        # the home path and six production hostnames.
        if e.get("private"):
            url = sh(["git", "-C", p, "remote", "get-url", "origin"])[1]
            m = re.search(r"github\.com[:/](\S+?/\S+?)(?:\.git)?$", url)
            if not m:
                holes.append(f"{e['path']}: must be private, and its origin "
                             f"is not a GitHub url this check can read")
            else:
                rcv, out, _ = sh(["gh", "repo", "view", m.group(1),
                                  "--json", "isPrivate", "-q", ".isPrivate"], t=30)
                if rcv != 0:
                    holes.append(f"{e['path']}: could not read {m.group(1)} "
                                 f"visibility, so it is unproven, not private")
                elif out.strip() != "true":
                    holes.append(f"{m.group(1)}: PUBLIC. It holds "
                                 f"{e['why']} and anyone can read it")
        if not mod and nrem and unreachable == "0":
            ok += 1
    return ok, holes


def check_mirrors(_d, _mm):
    rc, out, err = sh([sys.executable, os.path.join(REPO, "tracked.py"), "--check"], t=180)
    if rc == 0:
        return 1, []
    bad = [l.strip() for l in out.splitlines()
           if l.strip().startswith(("not committed", "differs", "missing"))]
    return 0, bad or [(out or err).strip().splitlines()[-1][:160] or f"tracked.py exit {rc}"]


def check_secrets(d, mm):
    """The names are kept so the estate can be rebuilt. The values never are."""
    holes, ok = [], 0
    for e in d["secrets"]:
        p = os.path.expanduser(e["path"])
        ex = os.path.join(REPO, e["example"])
        if not os.path.exists(p):
            holes.append(f"{e['path']}: missing ({e['why']})")
            continue
        if not os.path.exists(ex):
            holes.append(f"{e['path']}: no committed example at {e['example']}")
            continue
        if covered(ex, mm) is None:
            holes.append(f"{e['example']}: the example itself is not in git")
            continue
        live_keys = {l.split("=")[0].strip().lstrip("export ").strip()
                     for l in open(p) if "=" in l and not l.strip().startswith("#")}
        ex_keys = {l.split("=")[0].strip().lstrip("export ").strip()
                   for l in open(ex) if "=" in l and not l.strip().startswith("#")}
        missing = live_keys - ex_keys
        if missing:
            holes.append(f"{e['example']}: does not name {len(missing)} key(s) the live file sets")
            continue
        # And the values must still be absent. A leaked example is worse than none.
        leaked = [k for k in ex_keys
                  if len(next((l.split("=", 1)[1].strip().strip('"\'')
                               for l in open(ex) if l.split("=")[0].strip().endswith(k)), "")) > 8]
        if leaked:
            holes.append(f"{e['example']}: {len(leaked)} key(s) carry a value, LAW 21")
            continue
        ok += 1
    return ok, holes


def check_escrow(d, _mm):
    """A repo pushed only to GitHub is one suspended account from gone.

    This is the class that made two agents give two answers to one question:
    a bundle failed, was fixed an hour later, and each of them reported the
    moment they happened to look. A receipt with an age on it does not have
    that problem.
    """
    cfg = d.get("escrow")
    if not cfg:
        return 0, ["no escrow declared, so nothing checks the offsite copy"]
    p = os.path.expanduser(cfg["receipt"])
    if not os.path.exists(p):
        return 0, ["no bundle receipts at " + cfg["receipt"] + ", the offsite copy is unproven"]
    last, malformed = {}, 0
    for line in open(p):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event"):
            # A run-level record: the pusher writing down that a whole run was
            # skipped because the lock was held. It names no repo because it is
            # not about one, and the pusher already goes red on a long skip
            # streak, so reading it here would report the same fault twice.
            continue
        key = r.get("slug") or r.get("repo")
        if not key:
            # Neither a repo receipt nor a run event. It proves nothing, and
            # sorting it against real slugs raised a TypeError that took the
            # whole class down and reported six classes as unknown.
            malformed += 1
            continue
        last[key] = r
    if not last:
        return 0, ["the receipts file is empty, no repo has an offsite copy"]
    now, holes, ok = __import__("time").time(), [], 0
    want = {os.path.basename(os.path.expanduser(e["path"]).rstrip("/")) for e in d["repos"]}
    for slug, r in sorted(last.items()):
        state = r.get("restore") or r.get("status") or "unknown"
        age_h = (now - r.get("ts", 0)) / 3600.0
        # Age is a proxy. The question is whether the offsite copy holds what
        # this disk holds, and the receipt already records the tip it bundled.
        # The pusher writes a receipt only when it pushes, so a repo nobody has
        # committed to in 26h ages past the threshold forever while its copy is
        # complete. Measured 2026-08-24: of three slugs the age rule called
        # stale, Documents-code-popdd-py and Documents-code-sentinel-loop had
        # tip == local HEAD (false alarms) and .claude did not (a real hole).
        # A guard that cannot tell those apart is LAW 28's cry-wolf.
        tip = str(r.get("tip") or "")
        repo = os.path.expanduser(str(r.get("repo") or ""))
        local = ""
        if tip and repo and os.path.isdir(os.path.join(repo, ".git")):
            local = sh(["git", "-C", repo, "rev-parse", "HEAD"])[1]
        if state not in cfg["ok_states"]:
            holes.append("%s: last offsite copy is %s" % (slug, state))
        elif local and tip == local:
            ok += 1          # the copy is at this disk's commit; age is moot
        elif local and tip != local:
            holes.append("%s: offsite copy is at %s, this disk is at %s" %
                         (slug, tip[:12], local[:12]))
        elif age_h > cfg["max_age_hours"]:
            holes.append("%s: offsite copy is %.0fh old, older than %sh "
                         "(no tip recorded, so age is all this can grade)" %
                         (slug, age_h, cfg["max_age_hours"]))
        else:
            ok += 1
    # A declared repo with no receipt at all is the quiet failure: nothing red,
    # nothing kept. Match on the tail of the slug, which is how the pusher names them.
    for name in want:
        if not any(s and s.endswith(name) for s in last):
            holes.append("%s: has no offsite copy at all" % name)
    if malformed:
        holes.append("%d bundle receipt(s) are neither a repo nor a run event" % malformed)
    return ok, holes


STATE = os.path.join(HOME, ".estate", "state", "in-git-status.json")
GREEN_EVERY_S = 24 * 3600


def deliver(holes, lines):
    """Alert when the set of holes changes, and once a day when it has not.

    On the set, not the count: two holes closing while two open is not a quiet
    day, and reporting every hour that the same hole is still open is how an
    alert channel gets muted (LAW 28).
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import estate_alert
    except Exception as exc:                       # alerting never breaks the sweep
        print("[in-git] cannot load estate_alert: %r" % (exc,), file=sys.stderr)
        return

    try:
        prev = json.load(open(STATE)).get("holes", [])
    except (OSError, ValueError):
        prev = None                                # first run ever: say something

    if prev is None or set(prev) != set(holes):
        body = ("Load-bearing files: %d not kept anywhere\n\n" % len(holes)
                + "\n".join("- " + h for h in holes)) if holes else \
               "Load-bearing files: every one is in git again"
        ok = estate_alert.send_operator_alert(body, debounce_key="in-git-change",
                                              debounce_s=600)
        print("[in-git] change alert delivered=%s" % ok)
        return

    stamp = os.path.join(HOME, ".estate", "state", "in-git-last-green.txt")
    try:
        last = float(open(stamp).read().strip())
    except (OSError, ValueError):
        last = 0.0
    if time.time() - last < GREEN_EVERY_S:
        return
    ok = estate_alert.send_operator_alert(
        "Load-bearing files, no change\n\n" + "\n".join(lines),
        debounce_key="in-git-green", debounce_s=GREEN_EVERY_S - 60)
    print("[in-git] daily green delivered=%s" % ok)
    if ok:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w") as f:
            f.write(str(time.time()))


def check_parked(_d, _mm):
    """Scheduled jobs that run code out of a checkout standing on some branch.

    The sixth class, added 2026-08-24. The other five ask whether the work is
    committed and whether a copy exists off this disk. Both can be true while
    every scheduled job on the machine executes a branch nobody merged, because
    a shared checkout is a mutable pointer and launchd holds no opinion about
    which commit it is standing on.

    What it cost: ~/dev/code/crew was left on feat/mature-platform-gate, the
    hourly snapshot refused to publish to a stranded branch, and STATE.md went
    3.9 hours stale on main while launchctl reported exit 0.

    The measurement lives in launchd_drift.py, which owns this check and proves
    it both ways in its own selftest. This class only counts it, so the alert
    the founder already reads carries it.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import launchd_drift as ld
    ok, holes = 0, []
    for label in ld.loaded_labels():
        paths = ld.loaded_paths(label)
        if paths is None:
            continue
        hits = ld.parked(label, [x for x in paths if os.path.exists(x)],
                         ld.loaded_workdir(label))
        if not hits:
            ok += 1
            continue
        for repo, on, want in hits:
            if not want:
                holes.append("%s: runs code from %s on '%s', which has no published "
                             "branch to compare against" % (label, repo, on))
            else:
                holes.append("%s: runs code from %s, checked out on '%s', not '%s'"
                             % (label, repo, on, want))
    return ok, holes


def check_repo_only(d, mm):
    """The part of the declaration a CI runner can honestly answer.

    Five of the six classes read this machine: launchd jobs, live checkouts, the
    live env file, the bundle receipts. A runner has none of that, so running the
    full sweep there would report every class as a hole and teach everyone to
    ignore the step. What a runner does hold is the repository, and two things in
    it can be wrong on their own:

      * a declaration naming a repo copy that nobody committed, which is how a
        mirror entry silently stops mirroring anything;
      * an example file that carries a value, which is LAW 21 breached in git
        history where deleting it does not help.

    Both are caught by reading the checkout alone, so both belong on a runner.
    """
    holes, ok = [], 0
    #: The checkout this file is sitting in, not ~/.claude/scripts. Every other
    #: class is about this machine and is right to look there. This one is about
    #: whatever tree CI just checked out, and pointing it at the live repo made
    #: the clone test pass three times with the file it was checking deleted.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    #: The mirror declaration lives in tracked.json, next to the tool that acts
    #: on it, not in load-bearing.json. Reading the wrong file here reported
    #: zero mirrors and a clean pass, which is the shape of a gate that guards
    #: nothing.
    try:
        tracked = json.load(open(os.path.join(root, "tracked.json")))
    except (OSError, ValueError) as exc:
        return 0, ["tracked.json cannot be read: %s" % exc]
    for e in tracked:
        if os.path.exists(os.path.join(root, e["repo"])):
            ok += 1
        else:
            holes.append(f"{e['repo']}: declared as the kept copy of {e['live']}, "
                         f"but no such path is committed")
    for e in d.get("secrets", []):
        ex = os.path.join(root, e["example"])
        if not os.path.exists(ex):
            holes.append(f"{e['example']}: declared as the example for {e['path']}, "
                         f"but no such file is committed")
            continue
        leaked = [l.split("=", 1)[0].strip() for l in open(ex)
                  if "=" in l and not l.strip().startswith("#")
                  and len(l.split("=", 1)[1].strip().strip('"\'')) > 8]
        if leaked:
            holes.append(f"{e['example']}: {len(leaked)} key(s) carry a value, LAW 21")
        else:
            ok += 1
    return ok, holes


CLASSES = [("runners", check_runners), ("declared", check_declared),
           ("repos", check_repos), ("mirrors", check_mirrors),
           ("secrets", check_secrets), ("offsite", check_escrow),
           #: last, because it shells out to launchctl once per loaded job and
           #: is the slowest of the six.
           ("parked", check_parked)]


def main():
    quiet = "--quiet" in sys.argv
    #: A runner has no launchd, no live checkouts and no env file, so it runs the
    #: one class that reads the repository alone. It also writes no state and
    #: sends no message: the founder's board is about this machine.
    ci = "--ci" in sys.argv
    d = json.load(open(DECL))
    mm = {} if ci else mirrors()
    all_holes, lines, counts = [], [], {}
    for name, fn in ([("repo-only", check_repo_only)] if ci else CLASSES):
        try:
            ok, holes = fn(d, mm)
        except Exception as e:
            ok, holes = 0, [f"the {name} check could not run: {type(e).__name__}: {e}"]
        all_holes += holes
        counts[name] = {"kept": ok, "holes": len(holes)}
        lines.append("  %-9s kept=%-3d holes=%d" % (name, ok, len(holes)))
        for h in holes:
            lines.append("     HOLE  " + h)
    if not quiet or all_holes:
        print("\n".join(lines))
    print("load-bearing holes: %d" % len(all_holes))

    if ci:
        return 1 if all_holes else 0
    if "--no-deliver" not in sys.argv:
        deliver(all_holes, lines)
    # Written last, so the next run compares against a state the founder was
    # actually told about. A crash in delivery must not silence the next alert.
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump({"ts": time.time(),
                       "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "holes": all_holes, "classes": counts}, f, indent=2)
    except OSError as e:
        print("[in-git] cannot write %s: %s" % (STATE, e), file=sys.stderr)
    return 1 if all_holes else 0


if __name__ == "__main__":
    sys.exit(main())

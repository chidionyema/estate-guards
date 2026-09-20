#!/usr/bin/env python3
"""Run the estate's recovery drills and record which ones actually pass.

A drill is a command that proves a recovery path still works: a rebuild, a
restore, a rollback, a rotation. LAW 19 grades every dependency by its exit, and
an exit that has never been taken is a hope. This is the register of those exits
and the date each one last ran green.

    run.py --list        what is registered and when each last passed
    run.py --all         run every drill that has a command
    run.py --run <id>    run one
    run.py --check       exit 1 if a drill is failing or has gone stale

--all and --check post one line to ESTATE_BOARD.jsonl, which every session is
handed at startup, so PASS and NOT-RUN are different lines somebody reads rather
than two kinds of silence (LAW 28, LAW 31).

Drills with no command yet are listed as NOT WRITTEN with the thing that needs
writing. They are counted in every report and they do NOT make --check red: a
gate that is red forever is a gate people stop reading, which is the failure this
file exists to stop.
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
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTER = os.path.join(HERE, "register.json")
STATE = os.path.expanduser("~/.claude/state/drills.jsonl")
LOGS = os.path.expanduser("~/.claude/state/drills")
TIMEOUT = 900


def load():
    with open(REGISTER) as fh:
        return json.load(fh)


def history():
    """{id: newest record} from the append-only log."""
    out = {}
    if not os.path.exists(STATE):
        return out
    with open(STATE) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("status") == "PASS" or r["id"] not in out:
                out[r["id"]] = r
            elif out[r["id"]].get("ts", 0) <= r.get("ts", 0):
                out[r["id"]] = r
    return out


def last_green(rid):
    """The newest PASS for one drill, or None. Kept separate from the newest
    record: a drill that passed on Monday and failed today has both facts, and
    the age of the last green is the one that says how exposed we are."""
    if not os.path.exists(STATE):
        return None
    best = None
    with open(STATE) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("id") == rid and r.get("status") == "PASS":
                if best is None or r.get("ts", 0) > best.get("ts", 0):
                    best = r
    return best


def orphans(reg):
    """Drill scripts on disk that no register entry runs.

    Nothing on this estate registers a drill. A script gets written, a human or
    an agent types an entry into register.json, and if they forget, the drill
    sits there passing nothing forever while every report says the register is
    green. That is the exact failure this function refuses: the register cannot
    discover a drill, but it can be made unable to miss one.

    The exemption list is data, not code, and lives in the register beside the
    drills. A new helper script under drills/ therefore forces somebody to say
    out loud that it is not a drill, which is the whole point.
    """
    pointed, found = set(), {}
    for d in reg["drills"]:
        for arg in d.get("cmd") or []:
            base = os.path.basename(arg)
            if base.endswith((".py", ".sh")):
                pointed.add(base)
    for sub, pats in (("", (".py", ".sh")), ("../rebuild", (".sh",))):
        path = os.path.normpath(os.path.join(HERE, sub))
        for name in sorted(os.listdir(path)):
            if name.endswith(pats):
                found[name] = os.path.join(path, name)
    exempt = reg.get("not_drills", {})
    return [(n, p) for n, p in sorted(found.items())
            if n not in pointed and n not in exempt]


def status_of(d, reg):
    """(status, detail) for one registered drill, without running it."""
    if not d.get("cmd"):
        return "NOT WRITTEN", d.get("todo", "")
    green = last_green(d["id"])
    newest = history().get(d["id"])
    if newest and newest.get("status") == "FAIL":
        return "FAIL", newest.get("note", "")
    if green is None:
        return "NEVER RUN", ""
    age_d = (time.time() - green["ts"]) / 86400
    cap = d.get("max_age_days", reg.get("max_age_days_default", 8))
    if age_d > cap:
        return "STALE", f"last green {age_d:.1f}d ago, bar is {cap}d"
    return "PASS", f"{age_d:.1f}d ago"


def run_one(d):
    cmd = [c.replace("{HERE}", HERE).replace("{HOME}", os.path.expanduser("~")).replace("{IDP}", os.environ.get("IDP_DIR", os.path.expanduser("~/dev/code/idp"))).replace("{CREW}", os.environ.get("CREW_DIR", os.path.expanduser("~/dev/code/crew")))
           for c in d["cmd"]]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        rc, tail = p.returncode, (p.stdout + p.stderr).strip().splitlines()
    except subprocess.TimeoutExpired:
        rc, tail = 124, [f"timed out after {TIMEOUT}s"]
    note = tail[-1][:300] if tail else ""
    rec = {"ts": int(time.time()),
           "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "id": d["id"], "status": "PASS" if rc == 0 else "FAIL",
           "rc": rc, "seconds": round(time.time() - t0, 1), "note": note}
    # A failure that keeps only its last line cannot be attributed to a step
    # (LAW 29), so keep the whole run and point the record at it.
    if rc != 0:
        os.makedirs(LOGS, exist_ok=True)
        log = os.path.join(LOGS, "%s-%s.log" % (d["id"], rec["iso"].replace(":", "")))
        with open(log, "w") as fh:
            fh.write("$ %s\nrc=%s  %ss\n\n%s\n" % (
                " ".join(cmd), rc, rec["seconds"], "\n".join(tail)))
        rec["log"] = log
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def post(kind, text):
    """Put the verdict where sessions read it, and say so when that fails.

    This is the whole delivery half of LAW 28. A drill that runs, passes and
    reaches nobody has proved nothing, and the swallow that used to sit here made
    that failure look exactly like a quiet estate: no row, no error, no reader.
    A board write that cannot happen is itself news, so it goes to the one
    reporter that has its own failure path.
    """
    try:
        sys.path.insert(0, os.path.dirname(HERE))
        import tracked
        tracked.board(kind, text, "drills")
    except Exception as exc:
        sys.stderr.write("drills: the verdict did not reach the board: "
                         "%s: %s\n" % (type(exc).__name__, exc))
        try:
            sys.path.insert(0, str(__import__("guards_root").GUARDS_ROOT))
            import guard_report
            guard_report.broken(__file__, 128,
                                "the drill verdict could not be posted to the board: "
                                "%s: %s. The drills may be green and unread." % (
                                    type(exc).__name__, exc))
        except Exception:
            pass   # the reporter is the last resort; stderr above already carried it


DOC = os.path.join(HERE, "..", "docs", "onboarding", "drills.md")
MARK_A = "<!-- generated by drills/run.py --docs. Do not edit between the markers. -->"
MARK_B = "<!-- end generated -->"


def render_docs(reg):
    """The onboarding table, built from the register rather than typed beside it.

    The hand-written version of this table went stale twice inside two days: it
    said eleven entries when there were thirteen, and called telegram-delivery
    unwritten after it had been written. A copy of the register maintained by
    hand is a second register that disagrees with the first, so there is now one
    register and one renderer.
    """
    written = [d for d in reg["drills"] if d.get("cmd")]
    rows = [
        "`drills/register.json` holds %d entries. %d have a command. %d have no"
        % (len(reg["drills"]), len(written), len(reg["drills"]) - len(written)),
        "command yet, and each one carries the sentence describing what needs",
        "writing.",
        "",
        "| drill | written | what breaks without it |",
        "|---|---|---|",
    ]
    for d in reg["drills"]:
        breaks = (d.get("what_breaks") or "").split(". ")[0].rstrip(".")
        rows.append("| %s | %s | %s |" % (
            d["id"], "yes" if d.get("cmd") else "no", breaks))
    stray = orphans(reg)
    rows.append("")
    if stray:
        rows.append("**%d drill script(s) on disk are in no register entry, so nothing "
                    "runs them: %s.**" % (len(stray), ", ".join(n for n, _ in stray)))
    else:
        rows.append("Every drill-shaped script under `drills/` and `rebuild/` is either "
                    "run by an entry above or named in the register's `not_drills` list, "
                    "so none of them is quietly unrun.")
    return "\n".join(rows)


def write_docs(reg):
    """Returns True when the file on disk already matched."""
    # No trailing newline after MARK_B: the text after the marker already starts
    # with one. Adding a second grew the file by a blank line on every render,
    # which meant the file never matched and --check would have been red forever
    # (LAW 38: a guard that refuses correct work is an outage).
    body = "%s\n%s\n%s" % (MARK_A, render_docs(reg), MARK_B)
    with open(DOC) as fh:
        old = fh.read()
    if MARK_A not in old or MARK_B not in old:
        raise SystemExit("%s has no generated block; add the two markers" % DOC)
    head, rest = old.split(MARK_A, 1)
    new = head + body + rest.split(MARK_B, 1)[1]
    if new == old:
        return True
    with open(DOC, "w") as fh:
        fh.write(new)
    return False


def table(reg):
    print(f"{'drill':<24} {'status':<12} detail")
    counts = {}
    for d in reg["drills"]:
        st, detail = status_of(d, reg)
        counts[st] = counts.get(st, 0) + 1
        print(f"  {d['id']:<22} {st:<12} {detail[:70]}")
    for name, path in orphans(reg):
        counts["UNREGISTERED"] = counts.get("UNREGISTERED", 0) + 1
        print(f"  {name:<22} {'UNREGISTERED':<12} on disk, no register entry runs it")
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--run")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--docs", action="store_true",
                    help="rewrite the generated block in docs/onboarding/drills.md")
    a = ap.parse_args()
    reg = load()

    if a.docs:
        print("docs/onboarding/drills.md: %s" % (
            "already current" if write_docs(reg) else "rewritten from the register"))
        return 0

    if a.run:
        d = next((x for x in reg["drills"] if x["id"] == a.run), None)
        if d is None:
            sys.exit(f"no drill called {a.run}")
        if not d.get("cmd"):
            print(f"{a.run}: NOT WRITTEN. {d.get('todo','')}")
            return 2
        rec = run_one(d)
        print(f"{rec['id']}  {rec['status']}  rc={rec['rc']}  {rec['seconds']}s  {rec['note']}")
        if rec.get("log"):
            print(f"  what actually happened: {rec['log']}")
        return 0 if rec["status"] == "PASS" else 1

    if a.all:
        # The register answers "does this recovery path work". The audit answers the
        # question one step earlier: "is there a recovery path for this dependency at
        # all". A green register with an unclassified vendor in the tree is the more
        # dangerous of the two states, because it reads as covered.
        #
        # --sweep, not --ci: this repository is one of six, and auditing only the
        # one the auditor happens to live in is how the estate ended up with five
        # repositories nobody had ever listed the vendors of.
        audit = subprocess.run(
            [sys.executable, os.path.join(HERE, "audit.py"), "--sweep", "--ci"],
            capture_output=True, text=True)
        print(audit.stdout.rstrip())
        if audit.returncode != 0:
            post("dependency-unclassified",
                 "A dependency in the tree is neither drilled nor dismissed: "
                 + " ".join(audit.stdout.split())[-400:])

        # audit.py asks whether a dependency has a recovery path. coverage.py
        # asks it of every asset the estate owns, not only the dependencies, and
        # it asks three times: restart, rebuild, replace. Asked as one question,
        # ai.aiden.watch read as covered because recovery-posture restarts it --
        # and it is one of the jobs jobs.json cannot re-render, so it does not
        # come back on a new machine at all.
        #
        # --gate, so a hole in the estate reaches the board as a number instead
        # of turning this run red every night for weeks.
        cov = subprocess.run(
            [sys.executable, os.path.join(HERE, "coverage.py"), "--gate"],
            capture_output=True, text=True)
        print(cov.stdout.rstrip())
        summary = next((l for l in cov.stdout.splitlines()
                        if l.startswith("SUMMARY:")), "").replace("SUMMARY:", "").strip()
        if cov.returncode != 0:
            post("coverage-key-broken",
                 "The drill coverage answer key is broken, so its numbers cannot be "
                 "believed: " + " ".join(cov.stdout.split())[-400:])
        elif summary:
            post("coverage", "Drill coverage of the whole estate: " + summary + ".")

        # An unregistered drill is worse news than a failing one: a failure is
        # loud and this is silent. It goes to the board on its own line so it is
        # not buried under a green verdict.
        stray = orphans(reg)
        if stray:
            post("drill-unregistered",
                 "%d drill script(s) exist that no register entry runs, so they have "
                 "never proved anything: %s. Either register them or name them in the "
                 "register's not_drills." % (len(stray), ", ".join(n for n, _ in stray)))
        if not write_docs(reg):
            print("  docs/onboarding/drills.md was stale and has been regenerated")

        results = [run_one(d) for d in reg["drills"] if d.get("cmd")]
        unwritten = [d["id"] for d in reg["drills"] if not d.get("cmd")]
        failed = [r for r in results if r["status"] == "FAIL"]
        for r in results:
            print(f"  {r['id']:<22} {r['status']:<6} rc={r['rc']:<4} {r['seconds']}s  {r['note'][:80]}")
            if r.get("log"):
                print(f"  {'':22} {'':6} {r['log']}")
        if failed:
            post("drills-failed",
                 f"{len(failed)} of {len(results)} recovery drills failed: "
                 + "; ".join(f"{r['id']} ({r['note'][:80]}) -> {r.get('log','no log')}"
                              for r in failed)
                 + f". {len(unwritten)} more recovery paths have no drill at all: "
                 + ", ".join(unwritten) + ".")
        else:
            post("drills-passed",
                 f"All {len(results)} written recovery drills passed. "
                 f"{len(unwritten)} recovery paths still have no drill and are therefore "
                 f"unproven: " + ", ".join(unwritten) + ".")
        print()
        return 1 if failed else 0

    if a.check:
        counts = table(reg)
        broken = counts.get("FAIL", 0) + counts.get("STALE", 0) + counts.get("NEVER RUN", 0)
        # UNREGISTERED is red, unlike NOT WRITTEN. NOT WRITTEN is a recovery path
        # somebody decided not to prove yet and said so. UNREGISTERED is a drill
        # that exists, cost somebody a day to write, and runs nowhere.
        stray = counts.get("UNREGISTERED", 0)
        stale_doc = not write_docs(reg)
        if stale_doc:
            print("  docs/onboarding/drills.md disagreed with the register "
                  "and has been regenerated")
        print(f"\n{counts.get('PASS', 0)} passing, {broken} needing a run, "
              f"{counts.get('NOT WRITTEN', 0)} with no drill written, "
              f"{stray} written but registered nowhere")
        return 1 if (broken or stray or stale_doc) else 0

    table(reg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

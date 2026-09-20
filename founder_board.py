#!/usr/bin/env python3
"""The founder's board: what is done, what is broken, what is waiting on him.

Founder directive 2026-08-21: "we dothave a borard", "should notave to be asking these
questions", "need to know what is done, what is outstanding before going tinto liower level
detail", "so updates neeed to be top notch".

WHAT THIS IS FOR. He was opening the same page an hour apart and seeing nothing change, and
having to ask each time. A board he can open answers the question without a person in the loop,
which is the "frictioless and barelt any hunan inloop" requirement applied to reporting itself.

WHY IT IS GENERATED AND NEVER WRITTEN BY HAND. This estate's most expensive repeated failure is
prose that was true once. A hand-written status page drifts from the machine within hours and
then costs more than no page, because it is believed. Every row here comes from a command, and
every row carries the command that produced it so any line can be re-run and checked.

THREE RULES THE COLLECTORS OBEY, each one a failure this estate has already paid for:

  1. A collector that fails reports UNKNOWN with the reason. It NEVER reports zero.
     A dead probe and a clean probe both print no findings; only one of them is good news.
     (memory: an-audit-that-crashes-reports-nothing)
  2. A non-zero exit is not automatically an error. process_audit.py exits 1 BECAUSE it found
     failures -- that is it working. Each collector says for itself which exits are fine.
  3. Every row carries the age of its measurement. A number with no age is a claim about the
     past presented as a claim about now.

    python3 $HOME/Documents/code/claude-guards/founder_board.py              # print it
    python3 $HOME/Documents/code/claude-guards/founder_board.py --html OUT   # write the page
    python3 $HOME/Documents/code/claude-guards/founder_board.py --json       # machine-readable
    python3 $HOME/Documents/code/claude-guards/founder_board.py --selftest
"""
from __future__ import annotations

import argparse
import collections
import html
import calendar
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import ast
import datetime as dt
import time

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".claude", "state", "founder_board.json")
PROSPECTOR = os.path.join(HOME, "Documents", "code", "prospector")
GH_REPO = "chidionyema/prospector"
#: The platform repo. The product lives in GH_REPO; the cluster workflows live here.
IDP_REPO = os.environ.get("IDP_REPO", "chidionyema/idp")
LIVE_URL = "https://mumchimp.com"
SCRIPTS = os.path.join(HOME, ".claude", "scripts")

GOOD, BAD, WARN, UNKNOWN = "good", "bad", "warn", "unknown"


#: Where tools live on this machine, ahead of whatever PATH the caller happened to have.
#: launchd hands a job PATH=/usr/bin:/bin:/usr/sbin:/sbin and nothing else, and `gh` lives in
#: /usr/local/bin. So under `com.founder.board` -- the hourly job that writes the page the
#: founder actually reads -- every row that shells out to gh reported
#: "FileNotFoundError: [Errno 2] No such file or directory: 'gh'", including the open pull
#: requests row, while gh worked perfectly in any terminal. Measured 2026-08-21: 8 of the
#: page's rows read UNKNOWN for this one reason.
#:
#: The fix is here rather than in the plist because a PATH in the plist is right only for the
#: one caller that has it. A second copy of this page, run by hand or by a different job, is
#: then correct by accident.
_TOOL_DIRS = (os.path.join(HOME, ".local", "bin"), "/usr/local/bin", "/opt/homebrew/bin")


def tool_path() -> str:
    """PATH for a subprocess: the known tool directories, then the caller's own."""
    return os.pathsep.join((*_TOOL_DIRS, os.environ.get("PATH", "")))


def sh(cmd: list[str], timeout: int, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command. Returns (rc, stdout, stderr). rc 124 is this function's timeout."""
    try:
        # execvpe looks argv[0] up in the PATH it is HANDED, not the parent's. Measured
        # 2026-08-21: with PATH stripped to launchd's own, `gh --version` returns 0 through
        # env= and raises FileNotFoundError without it. So env= is the whole fix; resolving
        # argv[0] here as well would be dead code that no mutation can catch.
        env = {**os.environ, "PATH": tool_path()}
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except (OSError, ValueError) as e:
        return 127, "", f"{type(e).__name__}: {e}"


class Row:
    __slots__ = ("state", "label", "value", "detail", "command", "measured_at")

    def __init__(self, state, label, value, detail="", command=""):
        self.state, self.label, self.value = state, label, value
        self.detail, self.command = detail, command
        self.measured_at = time.time()

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def _unknown(label, why, command=""):
    """The only honest answer when a probe could not run. Never a zero."""
    return Row(UNKNOWN, label, "UNKNOWN", why, command)


# --------------------------------------------------------------------------- collectors

def collect_spend() -> list[Row]:
    """Money. This is the platform view and it goes first for that reason.

    It reports WHERE the money goes, not only how much, because the two rows this used to
    print sent the founder at the wrong lever. Measured 2026-08-21: the board said "spend
    halt DISARMED -- nothing stops the spend" against a $516.79 day. `halt_usd` fires on the
    DAEMON leg only (estate-budget.json says so in its own note), and the daemon spent $1.51
    of that. Arming it would have saved 0.3% and stopped the engine. 99.7% was five
    concurrent interactive coding sessions, which no halt in this estate can touch.
    """
    cmd = ["/usr/local/bin/python3",
           os.path.join(HOME, ".claude", "scripts", "estate", "estate_spend.py"), "--json"]
    rc, out, err = sh(cmd, 240)
    if rc not in (0, 2) or not out.strip():      # rc 2 is --cap's "over the line", unused here
        return [_unknown("Claude spend today", err.strip()[:200] or f"exit {rc}",
                         " ".join(cmd))]
    try:
        spend = json.loads(out)
    except json.JSONDecodeError as e:
        return [_unknown("Claude spend today", f"meter printed no JSON: {e}", " ".join(cmd))]

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "estate"))
    from budget_path import budget_path as _budget_path  # crew#91
    budget_path = _budget_path()
    try:
        with open(budget_path) as fh:
            budget = json.load(fh)
        cap = float(budget.get("warn_usd") or 0)
        halt = float(budget.get("halt_usd") or 0)
        budget_err = ""
    except (OSError, ValueError, json.JSONDecodeError) as e:
        cap = halt = 0.0
        budget_err = f"{type(e).__name__}: {e}"

    total = float(spend.get("total") or 0)
    by_owner = spend.get("by_owner") or {}
    # The daemon leg is the ONLY thing halt_usd can stop, so it is the only number that makes
    # arming it a decision rather than a gesture.
    daemon = sum(v for k, v in by_owner.items() if "daemon" in k)

    rows: list[Row] = []
    if cap:
        rows.append(Row(BAD if total > cap else GOOD, "Claude spend today",
                        f"${total:,.2f} of a ${cap:,.0f} cap",
                        f"{total / cap:.1f}x the cap" if total > cap else "inside the cap",
                        " ".join(cmd)))
    else:
        rows.append(Row(WARN, "Claude spend today", f"${total:,.2f}",
                        budget_err or "no warn_usd set, so there is no line to be over",
                        " ".join(cmd)))

    if by_owner:
        top, top_usd = max(by_owner.items(), key=lambda kv: kv[1])
        share = f"{top_usd / total * 100:.0f}%" if total else "n/a"
        rows.append(Row(BAD if cap and total > cap else GOOD, "Where the money goes",
                        f"{top} — ${top_usd:,.2f}",
                        f"{share} of today's spend, over {spend.get('requests', 0):,} requests "
                        f"in {spend.get('files', 0)} transcripts",
                        " ".join(cmd)))
    else:
        rows.append(_unknown("Where the money goes", "the meter reported no owners",
                             " ".join(cmd)))

    # NOT "nothing stops the spend". Say what arming it would actually have stopped today.
    armed = halt > 0
    rows.append(Row(GOOD if armed else WARN, "Automatic spend halt",
                    f"ARMED at ${halt:,.0f}" if armed else "DISARMED",
                    (f"stops the daemon leg only; that leg is ${daemon:,.2f} of today's "
                     f"${total:,.2f}, so arming it cannot touch the rest")
                    if not armed else
                    f"fires on the daemon leg, ${daemon:,.2f} so far today",
                    f"grep halt_usd {budget_path}"))
    return rows


def collect_estate_checks() -> list[Row]:
    """The estate's own auditor. Its exit 1 means it found problems -- that is it working."""
    cmd = [os.path.join(PROSPECTOR, ".venv", "bin", "python"),
           os.path.join(PROSPECTOR, "scripts", "process_audit.py"), "--json"]
    rc, out, err = sh(cmd, 600, cwd=PROSPECTOR)
    if rc not in (0, 1):
        return [_unknown("Estate checks", err.strip()[:200] or f"exit {rc}", " ".join(cmd))]
    try:
        payload = json.loads(out)
    except ValueError as e:
        return [_unknown("Estate checks", f"audit printed no JSON ({e})", " ".join(cmd))]
    failing = [(s["title"], r) for s in payload.get("sections", [])
               for r in s.get("rows", []) if r.get("grade") == "bad"]
    rows = [Row(GOOD if not failing else BAD, "Estate checks failing",
                str(len(failing)),
                f"{payload.get('warnings', 0)} warnings as well", " ".join(cmd))]
    for title, r in failing:
        rows.append(Row(BAD, f"{title} — {r['name']}", "FAIL", r.get("detail", "")[:220],
                        " ".join(cmd)))
    return rows


def classify_checks(checks: list) -> tuple[list[str], int]:
    """Split a PR's checks into (names that really failed, how many are still running).

    A check still running reports conclusion "" on a CheckRun and null on a StatusContext,
    NEITHER of which is a failure. Measured 2026-08-21 on PR #557: `changes` was QUEUED and
    `keep` was IN_PROGRESS, both with conclusion "", and the first draft of this board printed
    "red: changes, keep". A board that cries wolf about the founder's own pipeline is worse than
    no board -- it is the same defect this session had just fixed in the estate auditor.
    """
    done = [c for c in checks if (c.get("conclusion") or "")]
    red = sorted({c.get("name") or c.get("context") or "?" for c in done
                  if c["conclusion"] not in ("SUCCESS", "NEUTRAL", "SKIPPED")})
    return red, len(checks) - len(done)


def collect_estate_audit() -> list[Row]:
    """estate_audit.py's own verdict, on the page the founder actually opens.

    The audit has run hourly since 2026-08-21 and wrote its findings to
    ~/.claude/state/logs/estate-audit.err.log. Nothing read that file. Two of the nine
    criticals sitting in it were leaked credentials. The scanner was never the gap (LAW 28).

    Reads the JSON the audit already wrote; it does not re-run it. A board that shells a
    60-second scan is a board nobody loads.
    """
    cmd = ["/usr/bin/python3", str(__import__("guards_root").GUARDS_ROOT / "estate" / "estate_watch.py"), "--json"]
    rc, out, err = sh(cmd, 30)
    if rc != 0:
        return [_unknown("Estate audit", err.strip()[:200] or f"exit {rc}", " ".join(cmd))]
    try:
        d = json.loads(out)
    except ValueError as e:
        return [_unknown("Estate audit", f"watcher printed no JSON ({e})", " ".join(cmd))]
    c = d.get("counts", {})
    crit = d.get("critical", [])
    age_m = int((d.get("age_s") or 0) / 60)
    # A stale scan is UNKNOWN, never GOOD. "No criticals" and "no scan" look identical from
    # here, and reporting the second as the first is the failure the whole file exists to stop.
    if d.get("stale"):
        rows = [_unknown("Estate audit", f"last scan {age_m}m ago — STALE",
                         "launchctl list | grep estateaudit")]
    else:
        rows = [Row(GOOD if not crit else BAD, "Estate audit", f"{len(crit)} critical",
                    f"{c.get('warn', 0)} warn, {c.get('unknown', 0)} unknown, "
                    f"{c.get('ok', 0)} ok — scanned {age_m}m ago", " ".join(cmd))]
    for r in crit:
        rows.append(Row(BAD, f"{r.get('domain')} — {r.get('title')}", str(r.get("value"))[:60],
                        "", str(r.get("proof") or "")[:200]))
    return rows


def collect_prs() -> list[Row]:
    """What is in flight. A PR nobody can merge is the pipeline stopped (LAW 12)."""
    cmd = ["gh", "pr", "list", "--repo", "chidionyema/prospector", "--limit", "30",
           "--json", "number,title,mergeable,statusCheckRollup,createdAt,isDraft"]
    rc, out, err = sh(cmd, 90)
    if rc != 0:
        return [_unknown("Open pull requests", err.strip()[:200] or f"exit {rc}", " ".join(cmd))]
    try:
        prs = json.loads(out)
    except ValueError as e:
        return [_unknown("Open pull requests", str(e), " ".join(cmd))]
    rows = [Row(GOOD if len(prs) <= 5 else WARN, "Open pull requests", str(len(prs)),
                "", " ".join(cmd))]
    now = time.time()
    for pr in prs:
        checks = pr.get("statusCheckRollup") or []
        red, pending = classify_checks(checks)
        try:
            age_h = (now - time.mktime(time.strptime(pr["createdAt"], "%Y-%m-%dT%H:%M:%SZ"))
                     + time.timezone) / 3600
        except (KeyError, ValueError):
            age_h = float("nan")
        conflicting = pr.get("mergeable") == "CONFLICTING"
        state = BAD if (red or conflicting) else (WARN if pending else GOOD)
        what = ("needs a rebase" if conflicting else
                f"red: {', '.join(red)}" if red else
                f"{pending} check(s) still running" if pending else "green, mergeable")
        rows.append(Row(state, f"PR #{pr['number']} — {pr['title'][:58]}", what,
                        f"open {age_h:.0f}h", f"gh pr view {pr['number']}"))
    return rows


def collect_requirements() -> list[Row]:
    """What the founder asked for, and how much of it is proven done."""
    cmd = ["git", "-C", PROSPECTOR, "show", "origin/main:docs/REQUIREMENTS.md"]
    rc, out, err = sh(cmd, 60)
    if rc != 0:
        return [_unknown("Requirements register", err.strip()[:160] or f"exit {rc}",
                         " ".join(cmd))]
    tally, total = {}, 0
    for line in out.splitlines():
        if not line.startswith("| R"):
            continue
        total += 1
        cells = [c.strip() for c in line.split("|")]
        state = "UNSTATED"
        for c in cells:
            bare = c.replace("*", "").strip()
            if bare in ("DONE", "PARTLY", "NOT STARTED", "IN PROGRESS", "unproven"):
                state = bare
                break
            if bare.startswith("DONE"):          # "DONE (engine + config page)"
                state = "DONE"
                break
        tally[state] = tally.get(state, 0) + 1
    if not total:
        return [_unknown("Requirements register", "no R-rows found on origin/main",
                         " ".join(cmd))]
    done = tally.get("DONE", 0)
    rows = [Row(GOOD if done == total else WARN, "Requirements proven done",
                f"{done} of {total}",
                ", ".join(f"{v} {k.lower()}" for k, v in sorted(tally.items())),
                " ".join(cmd))]
    return rows


CERT_STATUS = os.path.expanduser("~/.claude/agent-cert/status.json")
CERT_STALE_S = 2 * 3600          # the job runs hourly; two misses is stale


def collect_agent_certification() -> list[Row]:
    """Every agent, graded against the capabilities its own spec claims."""
    if not os.path.exists(CERT_STATUS):
        return [_unknown("Agent certification", "never run: no status file yet",
                         "agent_cert.py")]
    try:
        with open(CERT_STATUS) as f:
            st = json.load(f)
    except (OSError, ValueError) as e:
        return [_unknown("Agent certification", f"unreadable: {e}", CERT_STATUS)]

    age = time.time() - float(st.get("last_run_epoch") or 0)
    rows = []
    if age > CERT_STALE_S:
        # A stale score is not a score. PASS and NOT RUN have to look different
        # or a dead checker reads as a healthy estate.
        rows.append(Row(BAD, "Certification is stale",
                        f"last ran {int(age // 3600)}h ago",
                        "com.founder.agentcert is not running",
                        "launchctl print gui/$(id -u)/com.founder.agentcert"))

    for name, a in sorted(st.get("agents", {}).items()):
        if a.get("error"):
            rows.append(Row(UNKNOWN, f"{name}", "CANNOT CERTIFY", a["error"],
                            f"agent_cert.py --agent {name}"))
            continue
        passed, graded = a.get("passed", 0), a.get("graded", 0)
        unproven = a.get("unproven") or []
        state = GOOD if not unproven else WARN
        detail = "; ".join(unproven[:3]) if unproven else "every claim it makes is proven"
        if a.get("blocked"):
            detail += f" | {a['blocked']} blocked on your decision"
        rows.append(Row(state, f"{name} claims proven", f"{passed} of {graded}",
                        detail, f"agent_cert.py --agent {name}"))
    return rows or [_unknown("Agent certification", "no agents registered",
                             "agents.json")]


#: How many missed turns before a scheduled job counts as not running. Three,
#: because one is a deferral and two is a slow machine.
STALE_TURNS = 3

#: What we assume a StartCalendarInterval job's period is when it names an hour
#: but no day. Daily. Used only to decide whether its output is stale.
CALENDAR_PERIOD = 86400


def _launchd_plists() -> dict:
    """label -> (path, parsed plist) for every job definition on this Mac."""
    found = {}
    for d in (os.path.expanduser("~/Library/LaunchAgents"),
              "/Library/LaunchAgents", "/Library/LaunchDaemons"):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".plist"):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, "rb") as fh:
                    pl = plistlib.load(fh)
            except Exception:
                continue
            found[pl.get("Label", name[:-6])] = (path, pl)
    return found


def _log_age_minutes(path: str):
    """Minutes since a log was last written, or None if there is no log."""
    if not path:
        return None
    path = os.path.expanduser(path)
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size == 0:
        return None
    return (time.time() - st.st_mtime) / 60.0


def _grade_job(label: str, pid: str, code: int, pl: dict):
    """Why this job is failing, or None. The exit code is never the verdict.

    Measured 2026-08-23: nine of this Mac's jobs sat at exit 1 and the board
    called all nine of them failing. Every one had run, written its output and
    in three cases delivered a Telegram alert. Their exit code is a count of
    what they found, which is what a job whose whole purpose is finding things
    returns. A board that says nine jobs are broken when none of them is
    teaches its reader to ignore the colour, and that is how the ops dashboard
    died behind a green board earlier the same night.

    So grade the work, not the return value. A job is failing when it is a
    service that should be up and is not, when it has produced nothing for
    several turns running, or when it wrote to stderr recently. A traceback
    lands on stderr; a finding count does not.
    """
    alive = pid.strip() not in ("", "-")
    keepalive = bool(pl.get("KeepAlive"))
    interval = pl.get("StartInterval")
    if not interval and pl.get("StartCalendarInterval"):
        interval = CALENDAR_PERIOD

    if keepalive and not interval and not alive:
        return "service is down"

    #: There is no stderr test here on purpose. Two attempts at one were wrong
    #: in different ways on this estate. Most jobs point StandardErrorPath and
    #: StandardOutPath at the same file, so a fresh "error" log is the job
    #: talking: that read 21 failures of 39. Narrowing it to a stderr file of
    #: its own still flagged maestro and the gateway, because python's logging
    #: module writes to stderr by default and a running service therefore has a
    #: permanently fresh error log. A signal that fires for healthy jobs is a
    #: proxy, not a measurement, and grading proxies is what put nine jobs on
    #: this row that were all working.

    if interval and not alive:
        #: estate-gate stamps gate.lastrun.<label> at the moment it hands a job
        #: to the shell. That is the only file on this Mac that records a run
        #: rather than the noise a run made, so where it exists it decides.
        #: ai.estate.tracked-guard ran at 23:41 and wrote nothing, because it
        #: had nothing to say; log freshness called it six hours dead. A quiet
        #: job and a stopped job look identical in a log and do not look
        #: identical here.
        stamped = _log_age_minutes(
            os.path.expanduser(f"~/.estate/state/gate.lastrun.{label}"))
        #: Whichever of its two logs it wrote to most recently. Half the jobs
        #: here write only to stderr, so testing stdout alone called
        #: com.founder.estateaudit dead 20 minutes after it refreshed the
        #: founder's dashboard.
        ages = [stamped] if stamped is not None else [
            a for a in (_log_age_minutes(pl.get("StandardOutPath")),
                        _log_age_minutes(pl.get("StandardErrorPath")))
            if a is not None]
        limit = (interval / 60.0) * STALE_TURNS
        if not ages:
            return "has never written any output"
        out_age = min(ages)
        if out_age < -5:
            return "its log is stamped in the future, so nothing here can be graded"
        if out_age > 60 * 24 * 365:
            return "its log timestamp is unusable, so nothing here can be graded"
        if out_age > limit:
            return f"no output for {out_age / 60:.1f}h, runs every {interval / 3600:.1f}h"

    if code > 0 and not alive and not interval and not keepalive:
        # Nothing else to grade it by: no schedule, no service, no logs.
        return f"exit {code}, and nothing else to check it against"
    return None


def collect_launchd() -> list[Row]:
    """Every job that is supposed to keep this estate running by itself."""
    rc, out, err = sh(["launchctl", "list"], 30)
    if rc != 0:
        return [_unknown("Background jobs", err.strip()[:160] or f"exit {rc}", "launchctl list")]
    plists = _launchd_plists()
    watched, dead = 0, []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, status, label = parts
        #: 2026-08-23: this allow-list named four prefixes and silently dropped
        #: everything else, and everything else is where the founder's own jobs
        #: live. com.founder.board sat at exit 256 and ai.architect.gateway at
        #: exit 1 for hours while this row read green, because neither prefix was
        #: on the list. An allow-list with a silent miss case is the same defect
        #: this board exists to catch. Watch every estate job and name the owners
        #: rather than guessing which ones matter.
        if not label.startswith(("com.prospector", "com.estate", "com.founder",
                                 "com.chidionyema", "ai.")):
            continue
        watched += 1
        try:
            code = int(status)
        except ValueError:
            continue
        #: The first column is the live pid, and a pid means the job is running at this
        #: instant. The status column is then the exit code of the PREVIOUS run, which is
        #: history and not a state. ai.architect.gateway was restarted at 23:0x and sat
        #: here as "exit 1" with pid 91037 beside it: alive, and reported dead. Reporting
        #: a stale exit as a current failure is the same defect as reporting a deferral as
        #: a success, pointed the other way.
        entry = plists.get(label)
        if entry is None:
            # Loaded with no definition on disk. Nothing to grade it against, so
            # say that rather than guessing from the exit code.
            if code > 0 and pid.strip() in ("", "-"):
                dead.append(f"{label} (exit {code}, no plist on disk)")
            continue
        why = _grade_job(label, pid, code, entry[1])
        if why:
            dead.append(f"{label} ({why})")
    rows = [Row(GOOD if not dead else BAD, "Background jobs failing",
                f"{len(dead)} of {watched}", "; ".join(dead[:6]), "launchctl list")]
    rows.append(_jobs_not_running())
    rows.append(_runners_in_git())
    return rows


def _jobs_not_running() -> Row:
    """Jobs that are losing every turn, which an exit code cannot show.

    estate-gate defers a job when the machine is loaded and returns exit 0,
    because a deferral is not a failure. That is correct and it is also how the
    estate went blind: 40 deferrals across 12 jobs in under four hours on
    2026-08-23, every one reporting success while doing nothing, and the audit
    behind the founder's own dashboard sat 183 minutes stale as a result.

    PASS and NOT RUN are different states and this row is the one that can tell
    them apart, because it reads when each job last actually ran rather than
    what it last returned.
    """
    state = os.path.expanduser("~/.estate/state")
    cmd = "ls ~/.estate/state/gate.defers.* ~/.estate/state/gate.lastrun.*"
    if not os.path.isdir(state):
        return _unknown("Jobs losing every turn", "no ~/.estate/state directory", cmd)

    stuck = []
    for name in sorted(os.listdir(state)):
        if not name.startswith("gate.defers."):
            continue
        label = name[len("gate.defers."):]
        try:
            with open(os.path.join(state, name)) as fh:
                n = int(fh.read().strip() or "0")
        except (ValueError, OSError):
            continue
        if n < 2:
            continue
        last = os.path.join(state, f"gate.lastrun.{label}")
        if os.path.exists(last):
            age_h = (time.time() - os.stat(last).st_mtime) / 3600.0
            stuck.append(f"{label} ({n} turns lost, last ran {age_h:.1f}h ago)")
        else:
            #: No lastrun file at all is the worst case: it has never once run
            #: since the gate started recording, and nothing said so.
            stuck.append(f"{label} ({n} turns lost, never observed running)")

    if not stuck:
        return Row(GOOD, "Jobs losing every turn", "0", "", cmd)
    return Row(BAD, "Jobs losing every turn", str(len(stuck)), "; ".join(stuck[:6]), cmd)


def _runners_in_git() -> Row:
    """Six classes: runners, declared paths, repos, mirrors, secrets, offsite (LAW 24).

    Reads what the hourly job wrote rather than running the sweep here. The sweep
    fetches five repositories over the network, and doing that inside page
    generation made the page take longer than its own timeout, so the founder's
    board went an hour stale while every part of it was working.

    Reading a file also lets the row tell PASS from NOT RUN, which running the
    sweep cannot: a checker that died reports nothing, and nothing renders green.
    """
    state = os.path.expanduser("~/.claude/state/in-git-status.json")
    label = "Everything load-bearing is in git"
    cmd = os.path.join(os.environ.get("GUARDS_ROOT",
             os.path.dirname(os.path.abspath(__file__))), "estate", "in-git.py")
    try:
        with open(state) as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return _unknown(label, f"com.founder.ingit has never written {state}: {e}", cmd)

    age_h = (time.time() - float(d.get("ts") or 0)) / 3600.0
    holes = d.get("holes") or []
    #: Two scheduled runs missed. The answer on screen is about a world that has
    #: moved on, and saying so is the honest row.
    if age_h > 2.5:
        return _unknown(label, "last checked %.1fh ago, the hourly job has stopped" % age_h, cmd)
    if holes:
        return Row(BAD, label, f"{len(holes)} not kept", "; ".join(holes[:4]), cmd)
    return Row(GOOD, label, "0 not kept", "every class clean", cmd)


def collect_founder_decisions() -> list[Row]:
    """Questions only he can answer. They are not blocked on work, they block work."""
    cmd = ["git", "-C", PROSPECTOR, "show", "origin/main:docs/REQUIREMENTS.md"]
    rc, out, _ = sh(cmd, 60)
    if rc != 0:
        return [_unknown("Waiting on the founder", f"exit {rc}", " ".join(cmd))]
    # `| Q | Question | ...` is the TABLE HEADER, not a question. Counting it made the board say
    # 5 rulings outstanding when there were 4, and printed the word "Question" as the first one.
    qs = [l for l in out.splitlines()
          if l.startswith("| Q") and "---" not in l and l.split("|")[1].strip() != "Q"]
    return [Row(WARN if qs else GOOD, "Waiting on a founder ruling", str(len(qs)),
                "; ".join(l.split("|")[2].strip()[:70] for l in qs[:4]), " ".join(cmd))]



def _gh_runs_cmd(workflow: str, limit: int, repo: str = "") -> list[str]:
    """The gh command _gh_runs will run. Split out so the selftest can grade the argv
    without a network call — the defect this guards against was pure argument order and
    was invisible to every check that only looked at the row's rendered text."""
    # crew#558: the platform workflows live in idp, not in the product repo, so the repo is a
    # parameter. It DEFAULTS to GH_REPO so every existing caller's argv is byte-identical and
    # the selftest that grades that argv keeps grading the same thing.
    cmd = ["gh", "run", "list", "--repo", repo or GH_REPO, "--limit", str(limit),
           "--json", "databaseId,status,conclusion,createdAt"]
    if workflow:
        # APPEND, never insert at a fixed index. `cmd[4:4]` put --workflow between --repo
        # and its value, so gh saw `--repo --workflow` and exited "unknown command
        # deploy-web.yml". Every workflow-filtered row on this board therefore read UNKNOWN,
        # and the row still displayed a CORRECT command, because line 468 builds the shown
        # string separately from the one that runs. A row that prints a command it did not
        # run is worse than a blank row: paste it and it works, so the board looks wrong
        # about itself. Measured 2026-08-23: broken form -> "unknown command"; correct form
        # -> JSON for deploy-web.yml. gh takes flags in any order, so appending cannot drift
        # again when the list above changes.
        cmd += ["--workflow", workflow]
    return cmd


def _gh_runs(workflow: str, limit: int = 8, repo: str = "") -> list[dict] | None:
    """The last runs of one workflow, or None when the question could not be asked."""
    cmd = _gh_runs_cmd(workflow, limit, repo)
    rc, out, _ = sh(cmd, 45)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return None


def _age(iso: str) -> str:
    """`2026-08-21T09:58:22Z` -> `21m ago`. Every row carries the age of its measurement."""
    try:
        t = time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, TypeError):
        return "age unknown"
    m = int((time.time() - t) / 60)
    return f"{m}m ago" if m < 90 else f"{m // 60}h ago"


def _run_row(label: str, runs: list[dict] | None, workflow: str) -> Row:
    cmd = f"gh run list --repo {GH_REPO} --workflow {workflow} --limit 1"
    if runs is None:
        return _unknown(label, "gh run list failed or returned no json", cmd)
    if not runs:
        return _unknown(label, "this workflow has never run", cmd)
    r = runs[0]
    if r.get("status") != "completed":
        return Row(WARN, label, f"{r.get('status')} now",
                   f"run {r.get('databaseId')}, started {_age(r.get('createdAt', ''))}", cmd)
    ok = r.get("conclusion") == "success"
    return Row(GOOD if ok else BAD, label, str(r.get("conclusion")),
               f"run {r.get('databaseId')}, {_age(r.get('createdAt', ''))}", cmd)


def _fly_platform_rows() -> list[Row]:
    """Is the engine actually running on Fly, and is the account able to deploy at all?

    WHY THIS ROW EXISTS. On 2026-08-23 the engine deploy went red twice and the board said
    only "gh run list ... failure". The cause was not in the code: `flyctl` got
    "status 403: Your account has overdue invoices", the app `prospector-engine` was
    SUSPENDED, and it had zero machines. The engine had not run since 2026-08-21 23:57.
    The shop stayed up the whole time -- mumchimp.com answered 200 -- so every instrument
    that watched the site said green while the thing that makes the product was off.

    A red deploy row cannot tell those apart. A build that fails on a syntax error and a
    build that is refused because the bill is unpaid look identical from GitHub, and only
    one of them is fixed by an agent. So this asks Fly two questions the pipeline cannot
    answer: can we deploy at all, and is anything running now.
    """
    rows: list[Row] = []

    # 1. Can we deploy at all. Nothing else on this board can see a billing hold.
    rc, out, _ = sh(["fly", "auth", "token"], 40)
    token = out.strip() if rc == 0 else ""
    if not token:
        rows.append(_unknown("Fly account can deploy", "no fly token on this machine",
                             "fly auth token"))
    else:
        # In-process, NOT curl. A token passed as a command-line argument is readable by
        # anyone who can run `ps` for as long as the call lasts, and LAW 10 says a secret
        # never appears anywhere it can be read again. urllib keeps it in this process.
        import urllib.error
        import urllib.request
        q = json.dumps({"query": "query { organizations { nodes { slug billingStatus "
                                 "creditBalanceFormatted isCreditCardSaved } } }"}).encode()
        req = urllib.request.Request("https://api.fly.io/graphql", data=q, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        node = None
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                nodes = ((json.loads(resp.read()).get("data") or {})
                         .get("organizations") or {}).get("nodes")
            node = (nodes or [None])[0]
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError,
                TypeError, AttributeError):
            node = None
        if node is None:
            rows.append(_unknown("Fly account can deploy", "fly billing api gave no answer",
                                 "fly auth token  # then POST api.fly.io/graphql"))
        else:
            status = str(node.get("billingStatus") or "?")
            ok = status.upper() not in ("PAST_DUE", "DELINQUENT", "SUSPENDED")
            detail = (f"credit {node.get('creditBalanceFormatted')}, "
                      f"card on file: {node.get('isCreditCardSaved')}")
            if not ok:
                detail = ("Fly will not build or deploy anything until this is paid. "
                          "The shop stays up; nothing new can ship. "
                          "Pay at https://fly.io/dashboard/chidi-onyema/billing -- " + detail)
            rows.append(Row(GOOD if ok else BAD, "Fly account can deploy", status, detail,
                            "fly auth token  # then POST api.fly.io/graphql"))

    # 2. Is the engine running. Zero machines is not slow, it is off.
    cmd = ["fly", "machines", "list", "-a", "prospector-engine", "--json"]
    rc, out, _ = sh(cmd, 90)
    try:
        machines = json.loads(out) if rc == 0 else None
    except json.JSONDecodeError:
        machines = None
    if machines is None:
        rows.append(_unknown("Engine machines running", "fly machines list gave no json",
                             " ".join(cmd)))
    else:
        started = [m for m in machines if m.get("state") == "started"]
        if not machines:
            detail = ("the app has NO machines at all -- the engine that makes the product "
                      "is not running anywhere")
        else:
            detail = "; ".join(f"{m.get('id')} {m.get('state')}" for m in machines[:4])
        rows.append(Row(GOOD if started else BAD, "Engine machines running",
                        f"{len(started)} of {len(machines)}", detail, " ".join(cmd)))
    return rows


def collect_shipped_to_live() -> list[Row]:
    """MERGED IS NOT OPERATIONAL, and nothing in this estate watched the difference.

    The founder, 2026-08-21: "when you ship, is it operational?" and "we cant have things working
    in the ark black bes". The chain a merge has to survive is merge -> deploy -> the bytes on the
    live site -> the post-deploy smoke that grades them. Every link of it ran in the dark: a
    session could truthfully say "merged" while the deploy failed, or the deploy could go green
    while the site served the wrong thing. These rows are that chain, one row per link.
    """
    # Fly first. A red deploy row is a symptom; a billing hold is the cause, and only one
    # of the two is something an agent can fix.
    rows = _fly_platform_rows()
    rows += [_run_row("Store deploy, last run", _gh_runs("deploy-web.yml"),
                     "deploy-web.yml"),
            _run_row("Engine deploy, last run", _gh_runs("deploy-engine.yml"),
                     "deploy-engine.yml")]

    smokes = _gh_runs("e2e-live-smoke.yml")
    rows.append(_run_row("Live smoke, last run", smokes, "e2e-live-smoke.yml"))

    # THE DARK NUMBER. Work that reached main and has never been graded against the live site.
    green = next((r for r in (smokes or [])
                  if r.get("status") == "completed" and r.get("conclusion") == "success"), None)
    if green is None:
        rows.append(Row(BAD, "Merges never graded against live", "ALL of them",
                        "no live smoke has EVER concluded green, so nothing merged is verified",
                        f"gh run list --repo {GH_REPO} --workflow e2e-live-smoke.yml"))
    else:
        since = green["createdAt"]
        cmd = ["gh", "pr", "list", "--repo", GH_REPO, "--state", "merged", "--limit", "40",
               "--json", "number,mergedAt,title"]
        rc, out, _ = sh(cmd, 45)
        try:
            merged = [p for p in json.loads(out) if (p.get("mergedAt") or "") > since] if rc == 0 else None
        except (json.JSONDecodeError, TypeError):
            merged = None
        if merged is None:
            rows.append(_unknown("Merges never graded against live", "gh pr list failed",
                                 " ".join(cmd)))
        else:
            rows.append(Row(GOOD if not merged else WARN, "Merges never graded against live",
                            str(len(merged)),
                            "; ".join(f"#{p['number']} {p['title'][:40]}" for p in merged[:4])
                            or f"last green smoke {_age(since)}", " ".join(cmd)))

    # The site itself. A deploy that says success and a page that does not answer are both
    # possible at the same time, so this asks the site rather than the pipeline.
    import urllib.error
    import urllib.request
    started = time.time()
    try:
        with urllib.request.urlopen(LIVE_URL, timeout=25) as resp:
            body = resp.read()
            ms = int((time.time() - started) * 1000)
            good = resp.status == 200 and len(body) > 2000
            rows.append(Row(GOOD if good else BAD, "mumchimp.com answering",
                            f"HTTP {resp.status}", f"{len(body)} bytes in {ms}ms",
                            f"curl -si {LIVE_URL} | head -1"))
    except (urllib.error.URLError, OSError, ValueError) as ex:
        rows.append(Row(BAD, "mumchimp.com answering", "NO ANSWER",
                        f"{type(ex).__name__}: {ex}", f"curl -si {LIVE_URL} | head -1"))
    return rows


def _selftest_scripts() -> tuple[list[str], list[str]]:
    """(scripts that DEFINE a selftest, scripts that only mention one).

    The first version grepped for the literal `--selftest` anywhere in the file, and that is
    grading a proxy -- the estate's named failure class, in the board that reports it. Measured
    2026-08-21: `reflect.py` was reported as "a guard claiming a selftest it does not have"
    because the string sits inside a DATA table at reflect.py:588, where it is the remediation
    command for a different guard. The file never advertised anything.

    A definition is the honest test, so this reads the AST. The second list exists because an
    allow-list with a silent miss case is how 10 criticals were dropped in 18 hours: a script
    that runs a selftest without defining a function named for one is REPORTED, never dropped.
    """
    defined: list[str] = []
    mentioned: list[str] = []
    try:
        names = sorted(os.listdir(SCRIPTS))
    except OSError:
        return defined, mentioned
    for name in names:
        if not name.endswith(".py"):
            continue
        path = os.path.join(SCRIPTS, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            tree = ast.parse(text)
        except (OSError, SyntaxError, ValueError):
            continue
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name.lstrip("_") == "selftest" for n in ast.walk(tree)):
            defined.append(path)
        elif "--selftest" in text and _advertises_selftest(tree):
            mentioned.append(path)
    return defined, mentioned


def _advertises_selftest(tree: "ast.AST") -> bool:
    """True when the file OFFERS `--selftest` to a caller, rather than merely containing it.

    The three ways a script can offer one without defining a function named for it: say so in
    its module docstring, register it with argparse, or test `sys.argv` directly. Anything else
    holding the literal is data -- `reflect.py:588` carries it as the remediation command for a
    DIFFERENT guard, and the board accused it of false advertising for three builds.
    """
    doc = ast.get_docstring(tree) or ""
    if "--selftest" in doc:
        return True
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "add_argument"):
            for a in n.args:
                if isinstance(a, ast.Constant) and a.value == "--selftest":
                    return True
        if isinstance(n, ast.Compare) and any(isinstance(o, ast.In) for o in n.ops):
            if (isinstance(n.left, ast.Constant) and n.left.value == "--selftest"
                    and "argv" in ast.dump(n)):
                return True
    return False


def _laws_enforced() -> Row:
    """How many of the laws a machine actually enforces (LAW 28).

    The laws are prose, and prose stops nothing. Until this row existed the
    answer to "is the estate following its own rules" was whatever the last
    agent asserted. Now it is a number, and the number came out worse than
    anyone claimed: of the 17 laws a machine can decide, 6 are enforced.

    Reads what the scheduled probe wrote rather than running it here, for the
    same reason as the in-git row: reading a file can tell PASS from NOT RUN,
    and running a probe inside page generation cannot.
    """
    state = os.path.expanduser("~/.claude/state/law-enforcement.json")
    label = "Laws a machine enforces"
    cmd = "python3 ~/dev/code/crew/science/law_enforcement.py"
    try:
        with open(state) as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return _unknown(label, f"the probe has never written {state}: {e}", cmd)

    try:
        # calendar.timegm, not time.mktime + time.timezone: time.timezone is the
        # standard-time offset and ignores DST, so under BST the age came out an
        # hour too old and a fresh file could read as a stopped job.
        age_h = (time.time() - calendar.timegm(time.strptime(
            d["generated"], "%Y-%m-%dT%H:%M:%SZ"))) / 3600.0
    except (KeyError, ValueError):
        return _unknown(label, "the probe wrote no readable timestamp", cmd)
    #: Six missed hourly runs. The number on screen describes a world that has
    #: moved on, and a stale number reads as a current one.
    if age_h > 6:
        return _unknown(label, "last measured %.1fh ago, the hourly job has stopped" % age_h, cmd)

    mech, gap = d.get("mechanical") or [], d.get("gap") or []
    if not mech:
        return _unknown(label, "the probe found no mechanical laws, which cannot be right", cmd)
    enforced = len(mech) - len(gap)
    detail = ("every law a machine can decide is enforced" if not gap else
              "unenforced: " + ", ".join("LAW %s" % g["id"] for g in gap[:8]))
    return Row(GOOD if not gap else BAD, label,
               "%d of %d" % (enforced, len(mech)), detail, cmd)


def _push_gate_reach() -> Row:
    """How many repositories the push gate actually runs in.

    "The gate exists" and "the gate is in the path" were treated as one fact for
    days. They are two. A git hook only runs in a repository whose
    core.hooksPath names it, so the LAW 7, LAW 22 and LAW 32 gates can be
    written, tested and green while every repository that ships a feature pushes
    straight past them.

    Measured 2026-08-23: bound in 2 repositories, and both of them are the
    directory that holds the hooks.
    """
    state = os.path.expanduser("~/.claude/state/law-enforcement.json")
    label = "Repos the push gate runs in"
    cmd = "git -C <repo> config core.hooksPath"
    try:
        with open(state) as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return _unknown(label, f"the probe has never written {state}: {e}", cmd)
    reach = d.get("hook_reach") or {}
    bound, total = reach.get("bound"), reach.get("repos")
    if bound is None or not total:
        return _unknown(label, "the probe wrote no reach figure", cmd)
    binds = d.get("hook_binds") or []
    #: Bound everywhere is not the bar. Bound only where the hooks live means
    #: nothing that ships a feature is gated, which is the state this row exists
    #: to make visible.
    only_self = all("/.claude" in b for b in binds)
    detail = ("bound only in the directory that holds the hooks, so nothing "
              "that ships a feature is gated" if only_self and binds else
              "; ".join(binds[:3]))
    return Row(BAD if only_self or bound < 2 else WARN, label,
               "%d of %d" % (bound, total), detail, cmd)


def collect_hooks_and_guards() -> list[Row]:
    """The guards are the estate's immune system, and NOTHING was watching them.

    Founder, 2026-08-21: "eveeven the hoks annd guards and everyhting else". Two ways a guard
    dies silently. Its script can be deleted or renamed while settings.json still names it -- the
    hook then fails open on every turn and no one is told. Or its own selftest can start failing:
    measured on the first run of this collector, `pr-freeze.py --selftest` reported 3 failed, so
    the freeze was not blocking `gh pr create` at all while every session believed it was.
    """
    rows: list[Row] = []
    settings = os.path.join(HOME, ".claude", "settings.json")
    try:
        with open(settings, encoding="utf-8") as fh:
            hooks = json.load(fh).get("hooks", {})
    except (OSError, json.JSONDecodeError) as ex:
        rows.append(_unknown("Hooks registered", f"{type(ex).__name__}: {ex}", f"cat {settings}"))
        hooks = None
    if hooks is not None:
        total, missing = 0, []
        for event, groups in hooks.items():
            for group in groups if isinstance(groups, list) else []:
                for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                    cmd = str(hook.get("command", ""))
                    total += 1
                    # Any ~/.claude/scripts path the command names must exist, or the hook is a
                    # no-op that fails open and says nothing.
                    for tok in cmd.replace('"', " ").replace("'", " ").split():
                        tok = os.path.expanduser(tok.strip())
                        if tok.startswith(SCRIPTS) and not os.path.exists(tok):
                            missing.append(f"{event}: {os.path.basename(tok)}")
        rows.append(Row(GOOD if not missing else BAD, "Hooks whose script is missing",
                        f"{len(missing)} of {total}", "; ".join(missing[:6]),
                        "python3 -c \"import json;print(json.load(open('~/.claude/settings.json'))['hooks'])\""))

    scripts, mentioned_only = _selftest_scripts()
    if not scripts:
        rows.append(_unknown("Guard selftests", f"no scripts found under {SCRIPTS}",
                             f"ls {SCRIPTS}/*.py"))
        return rows

    from concurrent.futures import ThreadPoolExecutor
    def one(path):
        return path, sh([sys.executable, path, "--selftest"], 30)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, scripts))

    failed, unimplemented = [], []
    for path, (rc, _out, err) in results:
        name = os.path.basename(path)
        if rc == 0:
            continue
        # An argparse rejection is not a failing guard, it is a guard that ADVERTISES a selftest
        # it does not have -- which is worse, because every reader assumes it is proven.
        if rc == 2 and "unrecognized arguments" in err:
            unimplemented.append(name)
        else:
            failed.append(f"{name} (exit {rc})")
    rows.append(Row(GOOD if not failed else BAD, "Guard selftests failing",
                    f"{len(failed)} of {len(scripts)}", "; ".join(failed[:6]),
                    f"for f in {SCRIPTS}/*.py; do python3 $f --selftest; done"))
    advertised = unimplemented + [os.path.basename(m) for m in mentioned_only]
    data_only = sum(1 for f in os.listdir(SCRIPTS)
                    if f.endswith(".py")
                    and os.path.join(SCRIPTS, f) not in scripts
                    and os.path.join(SCRIPTS, f) not in mentioned_only
                    and "--selftest" in open(os.path.join(SCRIPTS, f), encoding="utf-8",
                                             errors="replace").read())
    rows.append(Row(GOOD if not advertised else WARN,
                    "Guards claiming a selftest they do not have",
                    str(len(advertised)),
                    "; ".join(advertised[:6]) or
                    (f"{data_only} more name --selftest in data, not as an offer" if data_only
                     else ""),
                    f"rg -l -- --selftest {SCRIPTS}/*.py"))
    rows.append(_laws_enforced())
    rows.append(_push_gate_reach())
    return rows


# --------------------------------------------------------------------------- founder friction

# Words the founder actually uses when something has gone wrong. Measured off his own messages
# in this estate's transcripts, never picked from a thesaurus -- an English word that merely
# SOUNDS negative grades prose rather than frustration (memory:
# a-guard-that-greps-a-word-also-greps-english). Typos are his, and they are load-bearing: he
# types fast when he is annoyed, so the misspellings are part of the signal.
FRICTION = (
    "fuck", "fucck", "fucing", "fucking", "shit", "wtf",
    "whats the point", "what's the point", "whats the fucking point",
    "you keep", "keeps happening", "same nistake", "same mistake",
    "still not", "not working", "doesnt work", "does not work",
    "i said", "i told you", "i asked", "asked you",
    "exhaust", "ehausint", "frustrat", "annoying", "tired of",
    "sorry you", "sorryyou", "why are you", "why is this",
    "black box", "blackbox", "ablack box", "black bes",
    "no progress", "i dont see", "i don't see", "i ont see",
    "hurry", "asap", "too slow", "the loegr", "longer you take",
)


def _epoch(iso: str | None) -> float:
    """An ISO timestamp out of a transcript, as epoch seconds. 0.0 when it will not parse."""
    if not iso:
        return 0.0
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _ago(epoch: float) -> str:
    """Epoch seconds -> `21m ago`. `_age` takes an ISO string; transcripts give us numbers."""
    if not epoch:
        return "undated"
    m = int((time.time() - epoch) / 60)
    return f"{m}m ago" if m < 90 else f"{m // 60}h ago"


#: Filled on the first walk and reused. The board is a single-shot generator, so
#: a process-lifetime cache cannot go stale within one page.
_TRANSCRIPTS: "list[tuple[str, float]] | None" = None


def _walk_transcripts() -> list[tuple[str, float]]:
    """Every session transcript on this machine, newest first, walked once.

    Profiled at 385s for one page, 126s of it here: two callers each walked
    ~/.claude/projects and this machine holds enough transcripts to make that
    162,472 stat calls, 94 seconds of pure filesystem. Both callers want the
    same list at different ages, so walk once and let them filter.

    The subagent test also moved above the stat. It was rejecting the path after
    paying for it, which is the cheapest kind of waste to find and fix.
    """
    global _TRANSCRIPTS
    if _TRANSCRIPTS is not None:
        return _TRANSCRIPTS
    root = os.path.join(HOME, ".claude", "projects")
    out = []
    for dirpath, _dirs, files in os.walk(root):
        if "subagent" in dirpath:
            continue              # an agent's brief to another agent is not the founder talking
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, f)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append((path, st.st_mtime))
    out.sort(key=lambda t: -t[1])
    _TRANSCRIPTS = out
    return out


def _transcripts(max_age_s: float) -> list[tuple[str, float]]:
    """Every session transcript touched inside the window, newest first."""
    now = time.time()
    return [t for t in _walk_transcripts() if now - t[1] <= max_age_s]


def _founder_messages(path: str) -> list[tuple[float, str]]:
    """(epoch, text) for the messages the FOUNDER actually typed in one transcript.

    Everything else in a `type: user` line is machinery wearing the founder's role: tool
    results, hook output, system reminders, the compaction preamble. Counting those as his
    words would report frustration he never expressed, which is the same failure class as a
    guard that grades a proxy.
    """
    out: list[tuple[float, str]] = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"user"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") != "user" or ev.get("isMeta"):
                    continue
                content = (ev.get("message") or {}).get("content")
                if isinstance(content, list):
                    text = " ".join(b.get("text", "") for b in content
                                    if isinstance(b, dict) and b.get("type") == "text")
                elif isinstance(content, str):
                    text = content
                else:
                    continue                      # a tool_result block is not a founder message
                text = text.strip()
                if not text or text.startswith("<") or text.startswith("["):
                    continue
                if "<system-reminder>" in text or "Caveat:" in text[:80]:
                    continue
                if "[Request interrupted" in text or "This session is being continued" in text:
                    continue
                out.append((_epoch(ev.get("timestamp")), text))
    except OSError:
        pass
    return out


def collect_founder_friction() -> list[Row]:
    """What the founder said, and whether anyone acted on it.

    His words, 2026-08-21: "you need o v across the transctips perdiocially to see the
    founderss frustratios" and "nagaing different agent sessionsis ehausintg". Until this
    ran, every complaint he made lived in ONE session's window and died there -- the session
    that heard it either acted or did not, and no other session, and no later instance of the
    same session after a compaction, ever knew he had said it.

    This reads every transcript on the machine, so a complaint made to any session shows up
    on his page whether or not that session did anything about it.
    """
    files = _transcripts(24 * 3600)
    if not files:
        return [_unknown("Your words in the last 24h", "no transcript touched in 24h",
                         "ls ~/.claude/projects/*/*.jsonl")]

    now = time.time()
    hits: list[tuple[float, str, str]] = []          # (epoch, session, text)
    said = 0
    for path, _mtime in files[:80]:                  # newest 80 transcripts, bounded on purpose
        session = os.path.basename(os.path.dirname(path))[-12:]
        for ts, text in _founder_messages(path):
            if ts and now - ts > 24 * 3600:
                continue
            said += 1
            low = text.lower()
            if any(w in low for w in FRICTION):
                hits.append((ts, session, text))
    hits.sort(key=lambda h: -h[0])

    recent = [h for h in hits if h[0] and now - h[0] <= 6 * 3600]
    state = BAD if recent else (WARN if hits else GOOD)
    rows = [Row(state, "Times you had to complain (24h)",
                f"{len(hits)} of {said} things you said",
                f"{len(recent)} of them in the last 6 hours" if hits
                else "nothing in your own words reads as a complaint",
                "python3 $HOME/Documents/code/claude-guards/founder_board.py --json")]

    for ts, session, text in hits[:5]:
        one = " ".join(text.split())
        rows.append(Row(BAD if now - ts <= 6 * 3600 else WARN,
                        f"— {_ago(ts)}, session {session}",
                        one[:160] + ("…" if len(one) > 160 else ""),
                        "said to one session; no other session could see it until this row existed"))
    return rows


def collect_sessions() -> list[Row]:
    """Every agent session on this machine, on one page.

    His words: "nagaing different agent sessionsis ehausintg" and "the sooner wecan get fouder
    out of loop the btter". Managing them one at a time is the cost this row removes: what each
    one was last told, and how long since it did anything.
    """
    files = _transcripts(2 * 3600)
    if not files:
        return [Row(GOOD, "Agent sessions running", "0", "nothing active in 2 hours",
                    "ls -lt ~/.claude/projects/*/*.jsonl | head")]

    now = time.time()
    live, lines = 0, []
    for path, mtime in files[:20]:
        idle = (now - mtime) / 60.0
        if idle <= 30:
            live += 1
        session = os.path.basename(os.path.dirname(path))[-12:]
        last = ""
        msgs = _founder_messages(path)
        if msgs:
            last = " ".join(msgs[-1][1].split())[:90]
        lines.append(f"{session}: idle {idle:.0f}m — last told: {last or '(nothing typed)'}")

    return [Row(GOOD if live else WARN, "Agent sessions running", str(live),
                " | ".join(lines[:6]) or "none",
                "ls -lt ~/.claude/projects/*/*.jsonl | head")]


def collect_action_items() -> list[Row]:
    """Every action item written down anywhere, counted. Nobody has to read the docs to know.

    His words: "i need a report of acion itens fron this report and tracked and actions ...
    should nt be having to do this, should be auto", "we need to know deliverbles", "and track
    ruthlessly". The items were always written down -- 26 programme docs full of table rows --
    and never counted, so the only way to know what was outstanding was to read them.
    """
    cmd = ["/usr/local/bin/python3", os.path.join(SCRIPTS, "action_items.py"), "--json"]
    # 900, not 300. This collector only parses markdown and measures ~10s idle, but the board's
    # own 12:36 run on 2026-08-21 recorded "timed out after 300s" while the laptop sat at load
    # 400. A timeout tuned to an idle machine turns a working collector into UNKNOWN exactly
    # when the founder most wants the number -- and UNKNOWN reads as "nobody counted".
    rc, out, err = sh(cmd, 900)
    if rc != 0 or not out.strip():
        return [_unknown("Action items outstanding", err.strip()[:200] or f"exit {rc}",
                         " ".join(cmd))]
    try:
        d = json.loads(out)
    except json.JSONDecodeError as e:
        return [_unknown("Action items outstanding", f"printed no JSON: {e}", " ".join(cmd))]

    items = d.get("items") or []
    open_n, done_n, unk = d.get("open", 0), d.get("done", 0), d.get("unknown", 0)
    untracked = d.get("untracked", 0)
    graded = open_n + done_n
    rows = [Row(BAD if open_n > done_n else GOOD, "Action items outstanding",
                f"{open_n} open, {done_n} done",
                f"across {d.get('docs', 0)} docs on {d.get('ref', '?')}; "
                f"{done_n / graded * 100:.0f}% of what we wrote down is finished"
                if graded else "nothing graded",
                " ".join(cmd))]
    # An item whose status cell matches no vocabulary is NOT counted as done and NOT hidden.
    rows.append(Row(WARN if unk else GOOD, "Items with no readable status", str(unk),
                    "the table HAS a status column and the word in it is one nothing here "
                    "knows -- fixable in action_items.py",
                    " ".join(cmd)))
    # Counted apart from the row above because the FIX is apart, and reporting one number hid
    # that. His words 2026-08-21: "e need to strt tagging ad ncategorissing, project anagent
    # hygene". Until 2026-08-21 both of these were one figure of 689, which read as one
    # backlog; 574 of them needed an edit to a DOCUMENT and 115 needed an edit to a SCRIPT.
    worst = collections.Counter(i.get("source", "?") for i in items
                                if i.get("state") == "untracked").most_common(3)
    rows.append(Row(WARN if untracked else GOOD, "Deliverables no document can mark done",
                    str(untracked),
                    ("worst: " + ", ".join(f"{s.replace('docs/', '')} ({n})" for s, n in worst)
                     + " -- these tables have no status column at all, so nothing can ever "
                       "report them finished") if worst else "every table has a status column",
                    " ".join(cmd)))
    stale = sorted((i for i in items if i.get("state") == "open"),
                   key=lambda i: -(i.get("age_days") or 0))[:4]
    for i in stale:
        rows.append(Row(WARN, f"— {i.get('id')} open {i.get('age_days', 0):.0f}d",
                        str(i.get("title", ""))[:110],
                        f"{i.get('source')}:{i.get('line')}"))
    return rows


def collect_time_and_mistakes() -> list[Row]:
    """How long the work takes, and how often it is wrong. Both, on his page, unasked.

    His words, 2026-08-21: "too nay bugs", "and having to renid to test thorough", "lso he tinne
    takenninstakes nade etc", "we need it auo".

    The estate had no number for either. Every session reported the thing it shipped and none of
    them reported how many attempts it took or how long it sat, so "too many bugs" was a feeling
    he had to defend rather than a figure anyone could check.

    A red CI run is the estate's cheapest, most honest mistake counter: it is a change that was
    pushed believing it worked and did not.
    """
    rows: list[Row] = []
    runs = _gh_runs("", limit=100)
    if runs is None:
        rows.append(_unknown("Pushes that were wrong (last 100 runs)", "gh run list failed",
                             "gh run list --limit 100"))
    else:
        done = [r for r in runs if r.get("conclusion")]
        bad = [r for r in done if r["conclusion"] in ("failure", "timed_out", "cancelled")]
        pct = (len(bad) / len(done) * 100) if done else 0.0
        rows.append(Row(BAD if pct > 25 else (WARN if pct > 10 else GOOD),
                        "Pushes that were wrong", f"{len(bad)} of {len(done)} CI runs red",
                        f"{pct:.0f}% of everything pushed did not work first time",
                        "gh run list --limit 100 --json conclusion"))

    rc, out, err = sh(["gh", "pr", "list", "--repo", GH_REPO, "--state", "merged", "--limit", "20",
                       "--json", "number,title,createdAt,mergedAt"], 90)
    if rc != 0 or not out.strip():
        rows.append(_unknown("How long work sits before it lands", err.strip()[:160] or f"exit {rc}",
                             "gh pr list --state merged --limit 20"))
        return rows
    try:
        prs = json.loads(out)
    except json.JSONDecodeError as e:
        rows.append(_unknown("How long work sits before it lands", f"no JSON: {e}", ""))
        return rows

    flat = []
    for pr in prs:
        a, b = _epoch(pr.get("createdAt")), _epoch(pr.get("mergedAt"))
        if a and b and b >= a:
            flat.append((b - a) / 3600.0)
    flat.sort()
    if flat:
        mid = flat[len(flat) // 2]
        worst = flat[-1]
        rows.append(Row(BAD if mid > 6 else (WARN if mid > 2 else GOOD),
                        "How long work sits before it lands",
                        f"{mid:.1f}h median, {worst:.1f}h worst",
                        f"over the last {len(flat)} merged pull requests",
                        "gh pr list --state merged --limit 20 --json createdAt,mergedAt"))
    else:
        rows.append(_unknown("How long work sits before it lands", "no merged PR had both dates",
                             "gh pr list --state merged --limit 20"))
    return rows


def collect_backup() -> list[Row]:
    """Can we get the data back? Not "did a backup run" -- the question one step past it.

    Added 2026-08-21 on the founder's instruction: "I checked the console and the founder
    board. Neither shows it. we need this". The offsite backup had been copying the spend
    ledger to R2 since 2026-07-31 and its only output was store/offsite_backup.log, which
    nothing read. It could have stopped for a week and the first sign would have been a
    failed restore.

    Two rows, because "a copy exists" and "the copy comes back" are different claims and the
    second is the one that matters. The restore row names the exact command, so the answer to
    "what do I type at 4am" is on the board rather than in somebody's head.
    """
    py = os.path.join(PROSPECTOR, ".venv", "bin", "python")
    script = os.path.join(PROSPECTOR, "scripts", "backup_store.py")
    cmd = [py, script, "--money-state"]
    rc, out, err = sh(cmd, 180)
    if rc != 0 or not out.strip():
        return [_unknown("Offsite copy of the money files",
                         err.strip()[:200] or f"exit {rc}", " ".join(cmd))]
    try:
        state = json.loads(out)
    except json.JSONDecodeError as e:
        return [_unknown("Offsite copy of the money files",
                         f"--money-state printed no JSON: {e}", " ".join(cmd))]

    rows = []
    # 30 hours, not 24. The writer runs on an 86400s timer (deploy/engine/supervisord.conf),
    # so a 24h ceiling against a 24h period flickers red on ordinary drift and teaches
    # everyone to ignore the row. 30h still catches a MISSED RUN, which reaches 48h.
    for label, name in (("ledger", "Spend ledger, offsite"), ("db", "Catalogue database, offsite")):
        rec = state.get(label)
        if not rec:
            rows.append(Row(BAD, name, "NO COPY IN THE BUCKET",
                            f"nothing under {label}/ in {state.get('bucket')}",
                            " ".join(cmd)))
            continue
        age, mb = rec["age_h"], rec["bytes"] / 1_000_000
        st = GOOD if age <= 30 else BAD
        rows.append(Row(st, name, f"{mb:,.0f} MB, {age:.1f}h old",
                        rec["key"] + ("" if st == GOOD else "  -- a run was missed"),
                        " ".join(cmd)))

    restore = f"{py} {script} --restore-money <dir>"
    if state.get("complete"):
        rows.append(Row(GOOD, "Can we get it back?", "yes -- one command",
                        "verifies the download against R2's Content-Length and the gzip CRC "
                        "written at compression time, so a short transfer cannot pass",
                        restore))
    else:
        rows.append(Row(BAD, "Can we get it back?", "NOT WHOLE",
                        "one of the two money files has no copy, so a restore cannot bring "
                        "the engine up complete", restore))
    return rows



def collect_machine() -> list[Row]:
    """Is the laptop itself able to do work right now.

    Every other row on this page assumes the machine underneath is fine. On
    2026-08-21 it was not: the data volume reached 100%, and the first thing
    anybody noticed was this board truncating its own state file on write. The
    disk had been the cause for hours and no row said so, so the failure
    arrived wearing somebody else's name.

    Three readings, because they fail differently and one is not a proxy for
    the others. A full disk stops a write. Exhausted swap stops a process. A
    load average far above the core count means work is queued rather than
    running, which is what a person actually feels as "it has hung".

    All three are cheap: df, sysctl and getloadavg. No directory is walked, so
    this row cannot become the thing that makes a busy machine busier.
    """
    rows = []

    rc, out, _ = sh(["df", "-k", "/System/Volumes/Data"], 15)
    if rc != 0 or len(out.strip().splitlines()) < 2:
        rows.append(_unknown("Free disk", f"df exited {rc}",
                             "df -h /System/Volumes/Data"))
    else:
        f = out.strip().splitlines()[-1].split()
        used_gb, free_gb = int(f[2]) / 1048576, int(f[3]) / 1048576
        pct = f[4]
        st = BAD if free_gb < 10 else (WARN if free_gb < 25 else GOOD)
        note = "a build, an image pull or a corpus capture will fail" if st == BAD else ""
        rows.append(Row(st, "Free disk", f"{free_gb:,.1f} GB free",
                        f"{used_gb:,.0f} GB used, {pct} full"
                        + (".  " + note if note else ""),
                        "df -h /System/Volumes/Data"))

    rc, out, _ = sh(["sysctl", "-n", "vm.swapusage"], 15)
    m = re.search(r"used\s*=\s*([\d.]+)M.*free\s*=\s*([\d.]+)M", out or "")
    if rc != 0 or not m:
        rows.append(_unknown("Swap left", f"sysctl exited {rc}",
                             "sysctl vm.swapusage"))
    else:
        used_mb, free_mb = float(m.group(1)), float(m.group(2))
        st = BAD if free_mb < 500 else (WARN if free_mb < 1024 else GOOD)
        note = ("out of swap means the next process to ask for memory is killed, "
                "not slowed") if st == BAD else ""
        rows.append(Row(st, "Swap left", f"{free_mb:,.0f} MB free",
                        f"{used_mb:,.0f} MB in use" + (".  " + note if note else ""),
                        "sysctl vm.swapusage"))

    try:
        one = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        st = BAD if one > 4 * cores else (WARN if one > 2 * cores else GOOD)
        note = "work is queued, not running" if st != GOOD else ""
        rows.append(Row(st, "Load average", f"{one:,.0f} on {cores} cores",
                        note or "the machine keeps up",
                        "uptime"))
    except OSError as e:
        rows.append(_unknown("Load average", str(e), "uptime"))

    return rows


def collect_founder_requests() -> list[Row]:
    """LAW 18: every request the founder made, and whether a command closed it.

    His words, 2026-08-21: "add law every founder request/pront should be a trackd iten in board".

    Capture was never the gap. directive-capture.py catches the prompt on UserPromptSubmit and
    prompt-ledger.py catches the rest on Stop, including the ones he types mid-turn. What nothing
    read onto this page was the STATE: collect_founder_friction above only surfaces a message that
    matches a FRICTION keyword, so a plain instruction with no complaint word in it was captured
    and then invisible.

    A row here is closed only when prompt-ledger ran its acceptance criteria and every one exited
    0. No agent can mark one done by saying so, which is why this collector reads status and never
    counts a prompt as handled because a session claimed it was.
    """
    state = os.path.join(HOME, ".claude", "state", "prompt-ledger")
    tool = os.path.join(SCRIPTS, "prompt-ledger.py")
    try:
        ledgers = [os.path.join(state, f) for f in os.listdir(state) if f.endswith(".jsonl")]
    except OSError as e:
        return [_unknown("Your requests, closed with proof", f"no ledger dir: {e}", f"ls {state}")]
    if not ledgers:
        return [_unknown("Your requests, closed with proof", "no ledger written yet",
                         f"ls {state}")]
    ledgers.sort(key=lambda f: -os.path.getmtime(f))
    # Bounded on purpose, and the drop is REPORTED below rather than swallowed. A silent top-N is
    # how a partial count reads as "everything".
    dropped = max(0, len(ledgers) - 8)
    ledgers = ledgers[:8]

    total = done = cont = 0
    open_rows: list[tuple[str, str, str]] = []          # (ts, id, text)
    failed: list[str] = []
    for led in ledgers:
        cmd = ["/usr/local/bin/python3", tool, "--ledger", led, "--list", "all"]
        rc, out, err = sh(cmd, 180)
        if rc != 0 or not out.strip():
            failed.append(f"{os.path.basename(led)}: {err.strip()[:60] or rc}")
            continue
        for line in out.splitlines():
            m = re.match(r"\[(.)\] (\S+) (\S+) (.*)$", line)
            if not m:
                continue
            mark, rid, ts, text = m.groups()
            total += 1
            # A row with the ..cont marker is a FRAGMENT the ledger glued to the message before
            # it, not a separate ask. Counting fragments as requests turned 1,744 things he said
            # into 5,422 and would have put a number on his page that is not a number of requests.
            if text.endswith("  ..cont"):
                cont += 1
                continue
            if mark == "x":
                done += 1
            elif mark == " ":
                open_rows.append((ts, rid, text))

    if failed and not total:
        return [_unknown("Your requests, closed with proof", "; ".join(failed)[:200],
                         f"{tool} --ledger <f> --list all")]

    opened = len(open_rows)
    requests = total - cont
    pct = f"{done / requests * 100:.0f}%" if requests else "0%"
    rows = [Row(BAD if (requests and not done) else (WARN if opened else GOOD),
                "Your requests, closed with proof",
                f"{done} of {requests} closed ({pct})",
                f"{opened} still open across {len(ledgers)} project ledgers; "
                f"{cont} continuation fragments folded out of {total} ledger rows"
                + (f"; {dropped} older ledgers not read" if dropped else "")
                + ("; " + "; ".join(failed) if failed else ""),
                f"{tool} --project-dir ~/.claude/projects/<slug> --list open")]

    open_rows.sort(reverse=True)
    for ts, rid, text in open_rows[:6]:
        rows.append(Row(WARN, f"— open since {ts}", text[:110],
                        f"{rid} · close it: {os.path.basename(tool)} --verify {rid}"))
    return rows


def collect_drills() -> list[Row]:
    """Which recovery paths have actually been taken, and how long ago.

    The drills ran, passed and posted to ESTATE_BOARD.jsonl, which is the channel
    agent sessions read. Nothing put them in front of the founder. He asked on
    2026-08-24 whether they were visible; they were visible to the machines and
    not to him, which is the half that does not count.

    The status rules are imported from drills/run.py rather than reimplemented
    here. Two copies of "is this drill stale" would drift, and the copy on the
    board is the one he would believe.
    """
    reg_path = os.path.join(SCRIPTS, "drills", "register.json")
    if not os.path.exists(reg_path):
        return [_unknown("Recovery drills", f"no register at {reg_path}")]
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_drills_run", os.path.join(SCRIPTS, "drills", "run.py"))
        run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run)
        reg = run.load()
    except Exception as e:                        # noqa: BLE001 -- never report zero
        return [_unknown("Recovery drills", f"{type(e).__name__}: {e}")]

    rows, bad_ids, written = [], [], 0
    for d in reg["drills"]:
        if not d.get("cmd"):
            continue
        written += 1
        st, detail = run.status_of(d, reg)
        if st != "PASS":
            bad_ids.append(d["id"])
            rows.append(Row(BAD if st == "FAIL" else WARN,
                            f"— {d['id']}", st, detail[:110],
                            f"drills/run.py --run {d['id']}"))
    unproven = [d["id"] for d in reg["drills"] if not d.get("cmd")]
    stray = [n for n, _ in run.orphans(reg)]

    rows.insert(0, Row(
        BAD if bad_ids else GOOD,
        "Ways back that have actually been taken",
        f"{written - len(bad_ids)} of {written} green",
        (f"{len(bad_ids)} need a run: " + ", ".join(bad_ids) if bad_ids
         else "every written drill passed inside its own staleness bar")
        + f". {len(unproven)} more paths have no drill at all: "
        + ", ".join(unproven),
        "drills/run.py --check"))
    if stray:
        rows.append(Row(BAD, "— drills nothing runs", str(len(stray)),
                        "written, never registered, so they prove nothing: "
                        + ", ".join(stray), "drills/run.py --check"))
    rows.extend(_coverage_rows())
    return rows


def _coverage_rows() -> list[Row]:
    """How much of the estate any of those drills actually reaches.

    The row above says the ways back we have all work. This one says how much of
    the estate they cover, which is the question that was open when he asked
    whether Aiden was drilled: the drills were green and nothing joined them to
    the asset list, so a job that does not come back on a new machine read the
    same as one that does.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_drills_coverage", os.path.join(SCRIPTS, "drills", "coverage.py"))
        cov = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cov)
        # The off switch is one file and it takes this row off the board as well
        # as the nightly line, because a switch that silences half a thing is a
        # switch nobody trusts.
        if cov.is_off():
            return []
        lines, problems = cov.check()
    except Exception as e:                        # noqa: BLE001 -- never report zero
        return [_unknown("— assets with a way back", f"{type(e).__name__}: {e}")]

    summary = next((l for l in lines if l.startswith("SUMMARY:")), "")
    nums = re.findall(r"\d+", summary)
    if len(nums) < 3:
        return [_unknown("— assets with a way back", "no summary line in the report")]
    holes, slots, assets = (int(n) for n in nums[:3])

    # A broken answer key is its own row, because it makes the number beside it
    # meaningless and the two failures are fixed by different people on different
    # days. A hole is the estate; a broken key is the instrument.
    broken = [f"{who}: {why}" for who, why in problems if who != cov.HOLE]
    rows = [Row(
        WARN if holes else GOOD,
        "— assets with a way back",
        f"{slots - holes} of {slots}",
        f"across {assets} assets, each asked three ways: does it restart here, "
        f"does it come back on a new machine, does it survive its vendor leaving. "
        + (f"{holes} slot(s) nobody has decided about yet." if holes
           else "Every one is either pointed at a drill or dismissed with a reason."),
        "drills/coverage.py")]
    if broken:
        rows.append(Row(BAD, "— coverage answer key", "broken",
                        "so the number above cannot be believed: "
                        + "; ".join(broken)[:200], "drills/coverage.py --gate"))
    return rows



# --------------------------------------------------------------- the approval ledger

ESTATE_STATE_PATHS = [
    os.path.join(HOME, "dev/code/crew/ESTATE_STATE.md"),
    os.path.join(HOME, "dev/code/.crew-state/ESTATE_STATE.md"),
]
_TICKED = ("[x]", "[X]", "\u2611", "\u2705", "\u2714")


def _estate_state_text() -> tuple[str, str]:
    """Return (markdown, where). Disk first, then origin/main, else ("", reason)."""
    for path in ESTATE_STATE_PATHS:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read(), path
        except OSError:
            continue
    repo = os.path.join(HOME, "dev/code/crew")
    code, out, err = sh(["git", "-C", repo, "show", "origin/main:ESTATE_STATE.md"], 20)
    if code == 0 and out.strip():
        return out, "origin/main"
    return "", (err.strip().splitlines() or ["no ESTATE_STATE.md on disk or origin/main"])[0]


def parse_estate_state(text: str) -> list[tuple[str, str, str, bool]]:
    """Rows of the board table as (component, observation, verified, approved).

    The approval column is the founder's. A row counts as approved only when that
    cell carries a real tick; an empty box, a dash, or any prose an agent typed
    there is not an approval (ruling R16, 2026-08-24).
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line[1:-1].split("|")]
        if len(cells) < 4:
            continue
        if set("".join(cells)) <= set("-: "):          # the header underline
            continue
        if cells[0].lower().startswith("component"):    # the header itself
            continue
        approved = any(t in cells[3] for t in _TICKED)
        rows.append((cells[0], cells[1], cells[2], approved))
    return rows


def collect_estate_state() -> list[Row]:
    """The two-column board. Nothing here is green until the founder ticks it.

    The founder, 2026-08-24: "only founder decives if sonething is live or not, no one
    else can nove a ticket to live core". So this collector has exactly one way to emit
    GOOD -- a tick in the approval column of ESTATE_STATE.md -- and no amount of passing
    evidence in the observation column can produce one. An agent writing "it works" there
    still renders UNKNOWN.
    """
    text, where = _estate_state_text()
    if not text:
        return [_unknown("ESTATE_STATE.md", f"could not read it: {where}",
                         "git -C ~/dev/code/crew show origin/main:ESTATE_STATE.md")]
    rows = parse_estate_state(text)
    if not rows:
        return [_unknown("ESTATE_STATE.md", f"read {where} but found no board rows in it")]
    out = []
    for component, observation, verified, approved in rows:
        out.append(Row(
            GOOD if approved else UNKNOWN,
            component,
            "APPROVED" if approved else "awaiting your tick",
            f"{observation}  [measured {verified}]" if verified else observation,
            "",
        ))
    out.append(Row(
        GOOD if all(r[3] for r in rows) else UNKNOWN,
        "How many are approved",
        f"{sum(1 for r in rows if r[3])} of {len(rows)}",
        f"source: {where}. Agents write the observation column only; the tick is yours.",
        "",
    ))
    return out




# --------------------------------------------------------------------------- research

#: Where the crew checkout is. LAW 46: the machine's layout is an input, not a literal.
CREW = os.environ.get("CREW_REPO", os.path.join(HOME, "dev", "code", "crew"))
DEV = os.path.dirname(CREW)
RESEARCH_LEDGER = os.path.join(CREW, "science", "RESEARCH-LEDGER.jsonl")
RESEARCH_PAGE = os.path.join(CREW, "science", "research-ledger.html")
DOCS_BASELINE = os.path.join(CREW, "science", "DOCS-BASELINE.json")

#: A file a research entry says it delivered, e.g. "docs/reference/cluster-cost.md".
#: URLs are stripped before this runs: github.com/o/r/blob/main/x.md matches the same
#: shape and is not a local file, so leaving them in reports assets missing that never
#: were assets.
_ASSET_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.(?:md|html|jsonl?|py|sh|ya?ml))")
#: The hyphen is in the lookbehind because it is a word character in a path segment. Without
#: it, ".well-known/agent-card.json" matched from "known/" and this collector reported a file
#: that exists as gone -- a false alarm on the founder's board, which is worse than no row.
_URL = re.compile(r"https?://\S+")


def _ledger_entries() -> tuple[list[dict] | None, str]:
    """(entries, where). Disk first, then origin/main, else (None, reason)."""
    try:
        with open(RESEARCH_LEDGER, encoding="utf-8") as fh:
            raw, where = fh.read(), RESEARCH_LEDGER
    except OSError as e:
        code, out, err = sh(["git", "-C", CREW, "show",
                             "origin/main:science/RESEARCH-LEDGER.jsonl"], 20)
        if code != 0 or not out.strip():
            return None, f"no ledger on disk ({type(e).__name__}) and none on origin/main"
        raw, where = out, "origin/main"
    entries = []
    for n, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            return None, f"{where} line {n} is not JSON: {e}"
    return entries, where


def _asset_paths(entry: dict) -> list[str]:
    """Every local file this ledger entry claims to have delivered."""
    blob = _URL.sub(" ", json.dumps(entry))
    seen, out = set(), []
    for m in _ASSET_PATH.finditer(blob):
        p = m.group(1)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _asset_bases() -> list[str]:
    bases = [CREW, HOME, DEV]
    try:
        bases += [os.path.join(DEV, d) for d in sorted(os.listdir(DEV))
                  if os.path.isdir(os.path.join(DEV, d))]
    except OSError:
        pass
    return bases


def _asset_exists(rel: str) -> bool:
    """Open the thing itself. A research write-up saying an asset shipped is not the asset."""
    for base in _asset_bases():
        if os.path.exists(os.path.join(base, rel)):
            return True
    return sh(["git", "-C", CREW, "cat-file", "-e", f"origin/main:{rel}"], 10)[0] == 0


DELIVER_STATE = os.path.expanduser("~/.claude/state/founder-deliver.json")


def collect_deliveries() -> list[Row]:
    """Everything a session finished and handed to him, newest first, with the link to open it.

    Founder, 2026-08-25: "currently im blind to what was researched", "nothing can ever go into
    the void", "should have reached me on telegram or a link where i can see it directly",
    "everything needs to be on tap". founder-deliver.py (a Stop hook) sends each DONE: reply that
    links a deliverable to Telegram and records it here. This is the same record in the browser.
    """
    try:
        data = json.load(open(DELIVER_STATE, encoding="utf-8"))
    except FileNotFoundError:
        return [_unknown("Delivered to you", "nothing recorded yet: no DONE: reply has linked a "
                         "deliverable since the hook landed 2026-08-25",
                         "python3 $HOME/Documents/code/claude-guards/founder-deliver.py --selftest")]
    except Exception as exc:  # noqa: BLE001
        return [_unknown("Delivered to you", f"{DELIVER_STATE} unreadable: {exc}")]
    items = list(reversed(data.get("deliveries") or []))
    if not items:
        return [_unknown("Delivered to you", "record exists but holds no deliveries",
                         f"cat {DELIVER_STATE}")]
    out = []
    for d in items[:12]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.get("ts", 0)))
        dec = d.get("decision") or {}
        verdict = f"decided: {dec.get('verdict', '').upper()}" if dec else "awaiting your tap"
        out.append(Row(GOOD if dec else WARN, f"{when} · {verdict}",
                       str(d.get("head", ""))[:160], "  ".join(d.get("links") or []),
                       f"open {(d.get('links') or [''])[0]}"))
    return out


def collect_science_showcase() -> list[Row]:
    """The science lane's capabilities and progress, from the page the lane generates (crew#403).

    Founder, 2026-08-27: "we need more transparency the capabilities and progress from the
    science / research data and machine learning lane, we need a proper showcase." The page is
    docs/science/SHOWCASE.md in crew, written by science/showcase.py from the lane's own stores
    and regenerated four times a day by scripts/science-collect. This section shows its scalars
    and the link to open the page; it never re-measures what the page already measured.
    """
    page = os.path.join(CREW, "docs", "science", "SHOWCASE.md")
    state = os.path.join(CREW, "science", "showcase-state.json")
    gen = f"cd {CREW} && python3 science/showcase.py"
    if not os.path.exists(page):
        return [_unknown("Science showcase", "page not generated yet on this machine", gen)]
    age_h = (time.time() - os.path.getmtime(page)) / 3600
    out = [Row(GOOD if age_h < 8 else WARN, "Science showcase page",
               f"generated {age_h:.1f}h ago", f"file://{page}", f"open {page}")]
    try:
        numbers = json.load(open(state, encoding="utf-8")).get("numbers") or {}
    except FileNotFoundError:
        return out + [_unknown("Showcase numbers", "showcase-state.json absent", gen)]
    except Exception as exc:  # noqa: BLE001
        return out + [_unknown("Showcase numbers", f"{state} unreadable: {exc}", gen)]
    if not numbers:
        return out + [_unknown("Showcase numbers", "state holds no numbers", gen)]
    shown = ("warehouse rows", "warehouse sources", "stale sources", "contract violations",
             "research entries", "commits, 7d", "USD per commit", "capabilities scheduled", "capabilities")
    for key in shown:
        if key in numbers:
            bad = key in ("stale sources", "contract violations") and numbers[key] > 0
            out.append(Row(WARN if bad else GOOD, key, str(numbers[key]), "", gen))
    return out


def collect_research_and_docs() -> list[Row]:
    """Every research pass, and whether the asset it promised is somewhere he can open it.

    Founder, 2026-08-25: "every reseach needs assets delivered, nothing ever disappears
    into the ether", "a dcunet no one reads is useless", "and i never see the docs again".
    Three complaints with one shape. The work happened, a write-up existed for an hour, and
    neither ever reached him again.

    So this grades the artifact and not the write-up about the artifact. An entry that names
    docs/reference/cluster-cost.md is checked by opening that path. An entry whose
    metric_after is still empty two weeks on is research that produced no measured outcome,
    which is the ether he means.
    """
    entries, where = _ledger_entries()
    if entries is None:
        return [_unknown("Research ledger", where,
                         "git -C ~/dev/code/crew show origin/main:science/RESEARCH-LEDGER.jsonl")]
    if not entries:
        return [_unknown("Research ledger", f"{where} is empty: no research is on the record",
                         f"wc -l {RESEARCH_LEDGER}")]

    today = dt.date.today()

    def age_days(value):
        try:
            return (today - dt.date.fromisoformat(str(value)[:10])).days
        except ValueError:
            return None

    ranked = sorted(((age_days(e.get("date")), e) for e in entries
                     if age_days(e.get("date")) is not None), key=lambda p: p[0])
    out: list[Row] = []

    if not ranked:
        out.append(_unknown("Research passes on the record",
                            f"{len(entries)} entries, none with a readable ISO date"))
    else:
        newest = ranked[0][0]
        out.append(Row(GOOD if newest <= 7 else BAD,
                       "Research passes on the record",
                       f"{len(entries)}, newest {newest}d old",
                       f"source: {where}. The gate refuses a newest entry older than 7 days.",
                       "~/dev/code/crew/scripts/verify.d/80-research-ledger.sh"))

    render_cmd = "python3 ~/dev/code/crew/scripts/research-ledger-page.py"
    try:
        with open(RESEARCH_PAGE, encoding="utf-8") as fh:
            page = fh.read()
    except OSError as e:
        out.append(Row(BAD, "The page you can open", "MISSING",
                       f"{RESEARCH_PAGE} does not exist ({type(e).__name__}). The ledger is "
                       "JSONL, and JSONL is not a document anyone reads.", render_cmd))
    else:
        question = str(ranked[0][1].get("question", "")) if ranked else ""
        probe = question[:60]
        carried = bool(probe) and (probe in page or html.escape(probe) in page)
        age_h = (time.time() - os.path.getmtime(RESEARCH_PAGE)) / 3600.0
        out.append(Row(GOOD if carried else BAD,
                       "The page you can open",
                       "current" if carried else "STALE",
                       f"{RESEARCH_PAGE}, rendered {age_h:.0f}h ago. " + (
                           "It carries the newest research question."
                           if carried else
                           "It does NOT carry the newest research question, so the newest work "
                           "is in the JSONL and nowhere a person reads."),
                       render_cmd))

    for age, entry in ranked[:5]:
        question = str(entry.get("question", "(the entry names no question)"))
        label = question if len(question) <= 72 else question[:69] + "..."
        paths = _asset_paths(entry)
        gone = [p for p in paths if not _asset_exists(p)]
        if not paths:
            out.append(Row(WARN, label, "names no asset",
                           f"{age}d old. The entry points at no file, so there is nothing to "
                           "open and nothing to check.", ""))
        elif gone:
            out.append(Row(BAD, label, f"{len(gone)} of {len(paths)} gone",
                           f"{age}d old. Named by the research and not on disk or origin/main: "
                           + ", ".join(gone[:3]), ""))
        else:
            out.append(Row(GOOD, label, f"{len(paths)} delivered",
                           f"{age}d old. " + ", ".join(paths[:3]), ""))

    unmeasured = [(a, e) for a, e in ranked
                  if a > 14 and not str(e.get("metric_after") or "").strip()]
    out.append(Row(BAD if unmeasured else GOOD,
                   "Research older than 14 days with no measured outcome",
                   str(len(unmeasured)),
                   ("The question was asked and no number ever came back: "
                    + "; ".join(f"{a}d {str(e.get('question'))[:48]}" for a, e in unmeasured[:3]))
                   if unmeasured else "Every entry past 14 days carries a metric_after.",
                   "~/dev/code/crew/scripts/verify.d/80-research-ledger.sh"))

    docs_cmd = "~/dev/code/crew/scripts/verify.d/95-docs.sh"
    try:
        with open(DOCS_BASELINE, encoding="utf-8") as fh:
            baseline = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        out.append(_unknown("Documents meeting the standard",
                            f"cannot read {DOCS_BASELINE}: {type(e).__name__}: {e}", docs_cmd))
        return out

    total, failing = baseline.get("documents_total"), baseline.get("documents_failing")
    if not isinstance(total, int) or not isinstance(failing, int):
        out.append(_unknown("Documents meeting the standard",
                            "DOCS-BASELINE.json carries no documents_total/documents_failing",
                            docs_cmd))
        return out

    out.append(Row(GOOD if failing == 0 else BAD,
                   "Documents meeting the standard",
                   f"{total - failing} of {total}",
                   "A document passes when it is in git, carries an Owner: line and a date, and "
                   "holds real prose. The baseline is a ratchet: it may shrink, never grow.",
                   docs_cmd))
    return out


def _cluster_from_here() -> Row:
    """Can this Mac see the cluster at all? crew#558, 2026-08-28: it could not.

    The kubeconfig execs `oci ce cluster generate-token` with no --profile; ~/.oci/config has no
    DEFAULT profile and both of its profiles authenticate with a browser-login session token,
    which expires. The tokens were 1 and 2 days old. So every question about what the cluster is
    RUNNING was unanswerable from this desk, and the data map fell back to what git DECLARES.
    """
    kube = os.path.join(HOME, ".kube", "oke-estate")
    if not os.path.exists(kube):
        return _unknown("Cluster, from this Mac", f"no kubeconfig at {kube}", f"ls {kube}")
    cmd = ["kubectl", "--kubeconfig", kube, "--request-timeout=8s", "get", "nodes", "-o", "name"]
    rc, out, err = sh(cmd, timeout=20)
    if rc == 0:
        n = len([x for x in out.splitlines() if x.strip()])
        return Row(GOOD, "Cluster, from this Mac", f"{n} node(s)", "", " ".join(cmd))
    why = (err or out).strip().splitlines()
    why = next((x for x in why if x.strip()), "")[:140]
    return Row(BAD, "Cluster, from this Mac", "UNREACHABLE",
               f"{why}  --  the kubeconfig execs `oci` with no --profile and the session token "
               f"expires; renewing it needs a browser, so this desk is not a way to operate from "
               f"anywhere (crew#558)", " ".join(cmd))


def _cluster_from_ci() -> Row:
    """The same cluster, reached from GitHub's runners. This is the path that works from anywhere."""
    runs = _gh_runs("login-drill.yml", limit=5, repo=IDP_REPO)
    if runs is None:
        return _unknown("Cluster, from CI", "gh could not list the workflow runs",
                        " ".join(_gh_runs_cmd("login-drill.yml", 5, IDP_REPO)))
    done = [r for r in runs if r.get("conclusion")]
    if not done:
        return _unknown("Cluster, from CI", "no completed run in the last 5",
                        " ".join(_gh_runs_cmd("login-drill.yml", 5, IDP_REPO)))
    last = done[0]
    ok = last.get("conclusion") == "success"
    return Row(GOOD if ok else BAD, "Cluster, from CI",
               "reachable" if ok else f"last run {last.get('conclusion')}",
               f"login-drill {_age(last.get('createdAt', ''))} -- runs on GitHub's machines, needs "
               f"no laptop and no browser login",
               " ".join(_gh_runs_cmd("login-drill.yml", 5, IDP_REPO)))


def _runs_on_this_laptop() -> Row:
    """What stops when the lid closes."""
    rc, out, err = sh(["launchctl", "list"], timeout=15)
    if rc != 0:
        return _unknown("Runs on this laptop", (err or "launchctl failed")[:140], "launchctl list")
    jobs = [ln.split()[-1] for ln in out.splitlines()[1:]
            if ln.split() and ln.split()[-1].startswith("ai.estate.")]
    sched = os.path.isdir(os.path.join(HOME, "dev", "code", "idp", "run", "dagster"))
    detail = ", ".join(sorted(j.replace("ai.estate.", "") for j in jobs))[:200]
    if sched:
        detail = f"the Dagster scheduler is here too. {detail}"
    return Row(WARN if jobs else UNKNOWN, "Runs on this laptop",
               f"{len(jobs)} daemon(s)", detail, "launchctl list | grep ai.estate.")


def _map_is_a_disk_scan() -> Row:
    """How much of the estate's own map is a walk of this Mac's filesystem.

    LAW 50: coverage is proved by querying the backend, never by scanning files, and
    crew/science/datamap.py is the temporary bootstrap that retires surface by surface. The
    ceiling file is the committed measurement; discovery itself is far too slow for this page.
    """
    f = os.path.join(HOME, "dev", "code", "crew", "science", "bootstrap-ceiling.json")
    if not os.path.exists(f):
        return _unknown("The estate's own map", "no bootstrap-ceiling.json on this checkout "
                        "(crew#558 not merged here yet)", f"cat {f}")
    try:
        with open(f) as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        return _unknown("The estate's own map", f"{type(e).__name__}: {e}", f"cat {f}")
    n, of = d.get("scan_domains"), d.get("of_domains")
    return Row(WARN, "The estate's own map", f"{n} of {of} domains scan files",
               f"{d.get('measured_producer_share', '?')}, measured {d.get('measured', '?')}. "
               f"A ceiling that may only ever fall; raising it is red in CI ({d.get('ticket', '')})",
               f"python3 ~/dev/code/crew/science/datamap.py --check")


def collect_bearing() -> list[Row]:
    """Where the estate actually runs, as opposed to where it is deployed.

    crew#558, founder 2026-08-28: "we need to get a bearing on where we are whats on mac vs whats
    on cluster and cloud, whats a dev env and whats cloud env, we need to be able to seamlessly
    work from anywhere". These four rows are that question, measured. The shape of the answer on
    the day it was asked: the cluster was reachable from CI and not from this desk, eight daemons
    and the scheduler ran on the laptop, and 98.7% of the estate's own map was a walk of the
    laptop's disk.
    """
    return [_cluster_from_here(), _cluster_from_ci(), _runs_on_this_laptop(), _map_is_a_disk_scan()]


LINEAR_API = "https://api.linear.app/graphql"
LINEAR_FREE_ISSUE_CAP = 250


def _estate_env(key: str) -> str:
    """Read one value from the estate env file, without sourcing a shell.

    The file is the estate's one place for a credential this laptop needs; a key typed into
    this script would be a literal in git (LAW 46) and readable forever (LAW 10).
    """
    path = os.path.join(HOME, ".config", "estate", "estate.env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return os.environ.get(key, "")


def collect_linear_board() -> list[Row]:
    """Is the issue board a board, or a pile?

    Founder, 2026-09-06: "we dont have projecsts inl liner,, issues arent linked, etc".
    He was right, and the numbers are why this section exists rather than a reassurance: on
    the morning he asked, all 210 issues sat in Todo, none was in a project, none carried a
    link to the GitHub issue it came from, and 171 had no priority.

    Three rows, because three things can go wrong and each has a different lever:
      - the free plan counts ACTIVE issues against 250 and Done and Canceled both count, so
        the cap is a housekeeping number, not a usage number;
      - an issue with no project has no answer to "what is this for";
      - an issue with no GitHub link cannot be followed to the work.
    """
    key = _estate_env("LINEAR_API_KEY")
    if not key:
        return [_unknown("Linear board", "no LINEAR_API_KEY in the estate env file",
                         "grep LINEAR_API_KEY ~/.config/estate/estate.env")]
    query = """{ issues(first:250) { nodes { id priority
                   project { name }
                   attachments { nodes { url } } } }
                 projects(first:50) { nodes { name } } }"""
    import urllib.request

    try:
        # In-process, not curl: a token in an argv is readable by anyone who can run ps.
        req = urllib.request.Request(
            LINEAR_API, data=json.dumps({"query": query}).encode(),
            headers={"Authorization": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read()).get("data") or {}
        nodes = data["issues"]["nodes"]
        projects = data["projects"]["nodes"]
    except Exception as ex:  # noqa: BLE001 - any failure is UNKNOWN, never a zero
        return [_unknown("Linear board", f"{type(ex).__name__}: {ex}",
                         "POST api.linear.app/graphql")]

    active = len(nodes)
    no_project = sum(1 for n in nodes if not n["project"])
    # Graded on where the attachment points, not on Linear's sourceType. An attachment made
    # through the API is typed "api" however GitHub-shaped its URL is, and the first version
    # of this row read "0 of 206" against 206 working GitHub links because of it.
    linked = sum(1 for n in nodes
                 if any("github.com/" in (a["url"] or "") for a in n["attachments"]["nodes"]))
    headroom = LINEAR_FREE_ISSUE_CAP - active

    cap_state = GOOD if headroom > 50 else (WARN if headroom > 0 else BAD)
    rows = [Row(cap_state, "Room on the free plan", f"{active} of {LINEAR_FREE_ISSUE_CAP} used",
                f"{headroom} issues of headroom. Done and Canceled count too, so the estate "
                f"stays free by archiving, not by closing.",
                "linear: filter is:open"),
            Row(GOOD if no_project == 0 else (WARN if no_project < active / 2 else BAD),
                "Issues that belong to a project",
                f"{active - no_project} of {active}",
                f"{len(projects)} projects exist. {no_project} issues answer nothing when "
                f"asked what they are for.",
                "linear: group by project"),
            Row(GOOD if linked >= active else (WARN if linked else BAD),
                "Issues linked to their GitHub issue", f"{linked} of {active}",
                "A linked issue opens the GitHub thread it was migrated from; an unlinked "
                "one is a title with no trail.",
                "platform/linear/setup.py")]
    return rows


COLLECTORS = [
    ("Where it runs \u2014 mac, cluster, CI", collect_bearing),
    ("The board \u2014 nothing is live until you tick it", collect_estate_state),
    ("Is the machine able to work?", collect_machine),
    ("Money", collect_spend),
    ("Can we get the data back?", collect_backup),
    ("Ways back, proved rather than believed", collect_drills),
    ("Your requests, closed with proof", collect_founder_requests),
    ("Delivered to you \u2014 open it here", collect_deliveries),
    ("Research and documents \u2014 did the asset land?", collect_research_and_docs),
    ("Science lane \u2014 capabilities and progress", collect_science_showcase),
    ("What you said, and whether it landed", collect_founder_friction),
    ("The issue board \u2014 is it a board or a pile?", collect_linear_board),
    ("Work in flight", collect_prs),
    ("What is broken", collect_estate_audit),
    ("What is broken (prospector)", collect_estate_checks),
    ("The machine that runs itself", collect_launchd),
    ("Agent sessions", collect_sessions),
    ("Shipped — is it live?", collect_shipped_to_live),
    ("The guards themselves", collect_hooks_and_guards),
    ("Time taken, and mistakes made", collect_time_and_mistakes),
    ("Deliverables", collect_action_items),
    ("What was asked for", collect_requirements),
    ("Do the agents do what they claim?", collect_agent_certification),
    ("Waiting on you", collect_founder_decisions),
]


COLLECTOR_CAP_S = float(os.environ.get("FOUNDER_BOARD_COLLECTOR_CAP_S", "30"))


def _run_capped(title, fn, cap_s: float = None):
    """One collector may not take the page down.

    2026-08-25: three collectors each ran past 45s (Money, prospector audit, Deliverables) and
    the build blew a 500s timeout, so the HTML he opens in the browser had been stale since
    04:48. The class: a page that is his only view is built by a loop where one slow probe
    kills the whole page. A collector past the cap reports UNKNOWN for its section and the
    page ships without it. The thread is a daemon so a hung probe cannot hold the exit.
    """
    import threading
    cap = COLLECTOR_CAP_S if cap_s is None else cap_s
    box = {}

    def go():
        try:
            box["rows"] = fn()
        except Exception as e:                    # noqa: BLE001 -- rule 1: never report zero
            box["err"] = e
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(cap)
    if th.is_alive():
        return [_unknown(title, f"collector still running after {cap:.0f}s; page built without it",
                         "FOUNDER_BOARD_COLLECTOR_CAP_S=120 python3 $HOME/Documents/code/claude-guards/founder_board.py")]
    if "err" in box:
        e = box["err"]
        return [_unknown(title, f"collector raised {type(e).__name__}: {e}")]
    return box["rows"]


def _selftest_cap() -> bool:
    """Both ways in one run: a slow collector is UNKNOWN, a fast one keeps its rows."""
    import time as _t
    slow = _run_capped("slow", lambda: (_t.sleep(2), [Row(GOOD, "x", "y")])[1], cap_s=0.2)
    fast = _run_capped("fast", lambda: [Row(GOOD, "x", "y")], cap_s=2.0)
    ok = slow[0].state == UNKNOWN and "still running" in slow[0].value + slow[0].detail \
        and fast[0].state == GOOD
    print("PASS: a slow collector is UNKNOWN and a fast one keeps its rows." if ok
          else "FAIL: collector cap")
    return ok


def build() -> dict:
    sections = []
    for title, fn in COLLECTORS:
        started = time.time()
        rows = _run_capped(title, fn)
        sections.append({"title": title, "took_s": round(time.time() - started, 1),
                         "rows": [r.as_dict() for r in rows]})
    flat = [r for s in sections for r in s["rows"]]
    return {
        "generated_at": time.time(),
        "sections": sections,
        "bad": sum(1 for r in flat if r["state"] == BAD),
        "unknown": sum(1 for r in flat if r["state"] == UNKNOWN),
    }


# --------------------------------------------------------------------------- rendering

def render_text(board: dict) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(board["generated_at"]))
    mark = {GOOD: "  ok  ", BAD: " FAIL ", WARN: " warn ", UNKNOWN: "  ??  "}
    out = [f"FOUNDER BOARD — {stamp}",
           f"{board['bad']} failing, {board['unknown']} could not be measured", ""]
    for s in board["sections"]:
        out.append(f"=== {s['title']} ({s['took_s']}s) ===")
        for r in s["rows"]:
            out.append(f"[{mark[r['state']]}] {r['label'][:56]:<56} {r['value']}")
            if r["detail"]:
                out.append(f"           {r['detail'][:100]}")
        out.append("")
    return "\n".join(out)


def _atomic_write(path: str, text: str) -> None:
    """Write text so a reader never sees a half-written file.

    Two reasons, both measured on 2026-08-21. The hourly launchd board run and a
    session's manual run overlapped for 50 minutes on this laptop, and both had
    `open(path, "w")`, which TRUNCATES first -- so the founder opening the page
    mid-write gets a file that is empty or cut in half. And the disk was 100%
    full, where a direct write dies part-way and destroys the last good board.
    Writing beside the target and renaming leaves the previous board in place on
    either failure; os.replace is atomic within one filesystem.
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

_URL = re.compile(r"(https?://[^\s<]+)")


def _linkify(escaped: str) -> str:
    """URLs in an already-escaped detail cell become links he can click."""
    return _URL.sub(r'<a href="\1">\1</a>', escaped)


def render_html(board: dict) -> str:
    e = html.escape
    stamp = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(board["generated_at"]))
    headline = ("Everything measured is green." if not board["bad"]
                else f"{board['bad']} thing{'s' if board['bad'] != 1 else ''} broken.")
    n = board["unknown"]
    # An unmeasured row is not a green row. It is said out loud, at the top, in words.
    unmeasured = "" if not n else f" {n} thing{'s' if n != 1 else ''} could not be measured."
    parts = ["<title>Founder Board</title>", _CSS,
             f'<header><p class="eyebrow">Estate status</p><h1>{e(headline)}</h1>'
             f'<p class="stamp">Measured {e(stamp)}. Every row below is a command, not a claim.'
             f'{unmeasured}</p></header>',
             "<main>"]
    for s in board["sections"]:
        parts.append(f'<section><h2>{e(s["title"])}</h2><ul class="rows">')
        for r in s["rows"]:
            parts.append(
                f'<li class="row {r["state"]}"><span class="label">{e(r["label"])}</span>'
                f'<span class="value">{e(str(r["value"]))}</span>'
                + (f'<span class="detail">{_linkify(e(r["detail"]))}</span>' if r["detail"] else "")
                + (f'<code>{e(r["command"])}</code>' if r["command"] else "")
                + "</li>")
        parts.append("</ul></section>")
    parts.append("</main>")
    return "\n".join(parts)


_CSS = """<style>
:root{--bg:#fbfaf8;--ink:#1b1a18;--dim:#6c6862;--line:#e3ded6;--card:#ffffff;
--good:#2f7d51;--bad:#b3261e;--warn:#9a6b00;--unk:#5b5f6b;--accent:#1b4d7a;}
:root:not([data-theme="light"]){}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#15161a;--ink:#eceae6;--dim:#9a968f;--line:#2c2e35;--card:#1c1e23;
--good:#6ec48d;--bad:#ff8a80;--warn:#e0b352;--unk:#9aa0ae;--accent:#7fb3e0;}}
:root[data-theme="dark"]{--bg:#15161a;--ink:#eceae6;--dim:#9a968f;--line:#2c2e35;--card:#1c1e23;
--good:#6ec48d;--bad:#ff8a80;--warn:#e0b352;--unk:#9aa0ae;--accent:#7fb3e0;}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);margin:0;padding:2rem 1.25rem 4rem;
font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;}
header,main{max-width:62rem;margin:0 auto}
.eyebrow{font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin:0}
h1{font-size:clamp(1.6rem,5vw,2.4rem);margin:.3rem 0 .5rem;text-wrap:balance;letter-spacing:-.02em}
.stamp{color:var(--dim);font-size:.9rem;margin:0 0 2rem}
h2{font-size:.8rem;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);
margin:2rem 0 .6rem;font-weight:600}
.rows{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.4rem}
.row{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--unk);
border-radius:6px;padding:.7rem .9rem;display:grid;grid-template-columns:1fr auto;gap:.2rem .9rem}
.row.good{border-left-color:var(--good)}.row.bad{border-left-color:var(--bad)}
.row.warn{border-left-color:var(--warn)}.row.unknown{border-left-color:var(--unk)}
.label{font-weight:550}
.value{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.row.bad .value{color:var(--bad)}.row.good .value{color:var(--good)}
.row.warn .value{color:var(--warn)}.row.unknown .value{color:var(--unk)}
.detail{grid-column:1/-1;color:var(--dim);font-size:.87rem}
code{grid-column:1/-1;color:var(--dim);font-size:.76rem;font-family:ui-monospace,Menlo,monospace;
overflow-x:auto;white-space:pre;display:block;opacity:.75}
@media(max-width:34rem){.row{grid-template-columns:1fr}.value{text-align:left}}
</style>"""


# --------------------------------------------------------------------------- selftest

def selftest() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        if not cond:
            print(f"FAIL: {name}")
            ok = False

    # RULE 1: a collector that raises must produce UNKNOWN, never an empty green section.
    def boom():
        raise RuntimeError("probe is dead")
    saved = COLLECTORS[:]
    COLLECTORS[:] = [("Broken probe", boom)]
    b = build()
    COLLECTORS[:] = saved
    rows = b["sections"][0]["rows"]
    check("a raising collector yields exactly one row", len(rows) == 1)
    check("a raising collector reports UNKNOWN", rows[0]["state"] == UNKNOWN)
    check("a raising collector is not counted as good", b["unknown"] == 1 and b["bad"] == 0)
    check("the reason survives", "probe is dead" in rows[0]["detail"])

    # LAW 38, both ways in one run: the asset check must catch a named file that is not
    # there, and must NOT flag one that is. A guard only ever seen refusing has never been
    # shown to permit.
    check("a delivered asset is found",
          _asset_paths({"note": "science/RESEARCH-LEDGER.jsonl"}) == ["science/RESEARCH-LEDGER.jsonl"]
          and _asset_exists("science/RESEARCH-LEDGER.jsonl"))
    check("an asset that vanished is caught",
          _asset_paths({"note": "science/NO-SUCH-FILE-9f3a.md"}) == ["science/NO-SUCH-FILE-9f3a.md"]
          and not _asset_exists("science/NO-SUCH-FILE-9f3a.md"))
    check("a leading dotted segment is not clipped at a hyphen",
          _asset_paths({"note": ".well-known/agent-card.json"}) == [".well-known/agent-card.json"])
    check("a URL is not mistaken for a local asset",
          _asset_paths({"sources": ["https://github.com/o/r/blob/main/README.md"]}) == [])

    # R16: a tick in the founder's column is the ONLY thing that makes a row green,
    # and evidence in the observation column never is. Both directions, one run.
    table = (
        "| Component | Observation (agents write) | Verified | Founder approval |\n"
        "|---|---|---|---|\n"
        "| Ticked thing | anything at all | 10:00 | [x] |\n"
        "| Untimed thing | all tests pass, 200 OK, verified green | 10:00 | \u2b1c |\n"
    )
    parsed = parse_estate_state(table)
    check("both board rows parse", len(parsed) == 2)
    check("a ticked row is approved", parsed[0][3] is True)
    check("passing evidence with no tick is NOT approved", parsed[1][3] is False)
    check("the header row is not a board row", all(r[0] != "Component" for r in parsed))

    # A QUEUED or IN_PROGRESS check is not a red check. This is graded directly because the
    # first draft got it wrong on a live PR.
    red, pending = classify_checks([
        {"name": "changes", "status": "QUEUED", "conclusion": ""},
        {"name": "keep", "status": "IN_PROGRESS", "conclusion": ""},
        {"name": "guard", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"context": "legacy", "conclusion": None},
        {"name": "python", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"name": "skipped-lane", "status": "COMPLETED", "conclusion": "SKIPPED"},
    ])
    check("a running check is not reported red", red == ["python"])
    check("a running check is counted as pending", pending == 3)

    # A half-written board is worse than a stale one. Two board processes overlapped
    # for 50 minutes on 2026-08-21 and the disk was full; `open(path, "w")` truncates
    # before it writes, so either one leaves the founder looking at a cut-off page.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "board.html")
        _atomic_write(target, "OLD")
        _atomic_write(target, "NEW")
        check("an atomic write replaces the content", open(target).read() == "NEW")
        failed = False
        try:
            _atomic_write(target, {"not": "a string"})  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            failed = True
        check("a failing write raises", failed)
        check("a failing write leaves the previous board intact",
              open(target).read() == "NEW")
        check("a failing write leaves no .tmp behind",
              not os.path.exists(target + ".tmp"))

    # RULE 3: every row carries when it was measured.
    check("rows are timestamped", all(r["measured_at"] > 0
                                      for s in b["sections"] for r in s["rows"]))

    # A timeout is reported as a timeout, not as a zero.
    rc, _, err = sh(["/bin/sleep", "5"], 1)
    check("sh reports a timeout as 124", rc == 124 and "timed out" in err)
    rc, _, err = sh(["/definitely/not/a/binary"], 5)
    check("sh reports an unspawnable command", rc == 127 and err)

    # The launchd case, graded directly. This is the defect that made 8 rows on the live page
    # read UNKNOWN on 2026-08-21: launchd's PATH has no /usr/local/bin, so `gh` was missing
    # for the only caller that matters. Mutating tool_path() back to os.environ["PATH"] makes
    # this fail.
    # A FLAG MUST NEVER SEPARATE --repo FROM ITS VALUE. `cmd[4:4] = ["--workflow", w]` did
    # exactly that, so gh read the flag as the repo name and exited "unknown command". Every
    # workflow-filtered row read UNKNOWN for it, while still PRINTING a correct command,
    # because the shown string is built separately from the one that runs. Graded on the argv
    # because that is where the defect lived: no check on the rendered row could see it.
    _c = _gh_runs_cmd("deploy-web.yml", 3)
    check("--repo is followed by the repo, not a flag",
          _c[_c.index("--repo") + 1] == GH_REPO)
    check("the workflow filter is passed and keeps its value",
          "--workflow" in _c and _c[_c.index("--workflow") + 1] == "deploy-web.yml")
    check("no flag is left without a value",
          all(_c[i + 1][:2] != "--" for i, a in enumerate(_c[:-1]) if a.startswith("--")))

    # THE FLY BILLING ROW MUST NEVER CARRY THE TOKEN IT USED. The row's `command` and `detail`
    # are rendered onto a page and into the board's json, both of which are read again later,
    # and LAW 10 says a secret never appears anywhere it can be read again. The call is made
    # in-process with urllib for the same reason: a token in an argv is readable by anyone who
    # can run `ps`. This grades the rendered row, because the page is where a leak would land.
    _fly_rows = _fly_platform_rows()
    _rc, _tok, _ = sh(["fly", "auth", "token"], 40)
    _tok = _tok.strip()
    check("the fly billing row never puts the token in a field that gets rendered",
          not _tok or not any(_tok in (r.command or "") or _tok in (r.detail or "")
                              or _tok in str(r.value) for r in _fly_rows))
    check("the fly rows say whether the engine is running",
          any(r.label == "Engine machines running" for r in _fly_rows))

    saved_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"      # exactly what launchd gives
        # Installed-ness is decided against the caller's REAL PATH, never against
        # tool_path(). Asking the function under test whether to run the test is how a first
        # draft of this check passed a mutant that broke tool_path() outright.
        if shutil.which("gh", path=saved_path):
            rc, out, err = sh(["gh", "--version"], 20)
            check("gh runs under launchd's PATH", rc == 0 and "gh version" in out)
        else:
            # Reported, never dropped: an allow-list whose miss case is silent is how ten
            # findings were lost in eighteen hours on this estate.
            print("NOTE: gh is not installed anywhere in _TOOL_DIRS; the launchd PATH case "
                  "could not be graded on this machine.")
    finally:
        os.environ["PATH"] = saved_path

    # The renderers must survive every state, including UNKNOWN with no command.
    fake = {"generated_at": time.time(), "bad": 1, "unknown": 1, "sections": [
        {"title": "T", "took_s": 0.0, "rows": [
            Row(GOOD, "g", "1").as_dict(), Row(BAD, "b<&>", "2", "d", "c").as_dict(),
            Row(WARN, "w", "3").as_dict(), _unknown("u", "why").as_dict()]}]}
    txt, page = render_text(fake), render_html(fake)
    check("text render mentions every row", all(k in txt for k in ("g", "w", "u")))
    check("html escapes the label", "b&lt;&amp;&gt;" in page)
    check("html defines colours on bare :root", ":root{--bg:" in page)
    check("html paints the body background", "body{background:var(--bg)" in page)

    print("PASS: a dead collector reports UNKNOWN, never zero." if ok else "SELFTEST FAILED")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="PATH")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return 0 if (_selftest_cap() and selftest() in (0, None)) else 1

    board = build()
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        _atomic_write(STATE, json.dumps(board, indent=2))
    except OSError as e:
        print(f"[board] could not save state: {e}", file=sys.stderr)

    if args.html:
        _atomic_write(args.html, render_html(board))
        # stderr, never stdout. `--json --html X` together put this line ABOVE the JSON
        # document, so json.load() on the captured stdout died at "Expecting value:
        # line 1 column 1". Measured 2026-08-21. A status line is not part of the payload.
        print(f"wrote {args.html}", file=sys.stderr)
    if args.json:
        print(json.dumps(board, indent=2))
    elif not args.html:
        print(render_text(board))
    return 1 if board["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Everything running in this estate, in one page, with the command behind every number.

The founder, 2026-08-21: "we need to know everythig running ... nonatter how tiny or
insigificant" and "if a autitor visited tonorrw". The estate already had nine probes. Nine
probes cost 347.7 seconds and seven of them exit non-zero, so the honest answer to "what is
running?" took six minutes and arrived broken.

This is not a tenth probe. It runs every check CONCURRENTLY under a hard per-check timeout,
writes JSON and HTML atomically, and stamps generated_at into both. Serving is then a file
read, which is what makes the founder's "0 seconds" reachable: the cost moves off the read
and onto a scheduled build.

Every row carries the command that produced it. A check that times out or errors is reported
as UNKNOWN with the reason -- never omitted, never guessed. No credential VALUE is ever
written to the output; token findings carry file, line, prefix and length only.

    estate_audit.py --json                 machine-readable to stdout
    estate_audit.py --html OUT --state OUT.json    write the page and the data
    estate_audit.py --selftest             prove it works
"""
from __future__ import annotations

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
import concurrent.futures as futures
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import time

HOME = pathlib.Path.home()
CLAUDE = HOME / ".claude"
STATE = CLAUDE / "state"
PROSPECTOR = HOME / "Documents/code/prospector"

DEFAULT_HTML = STATE / "estate-audit.html"
DEFAULT_JSON = STATE / "estate-audit.json"

CRIT, WARN, OK, UNK = "critical", "warn", "ok", "unknown"
TIMEOUT = 12

# Every domain an auditor walks through, in the order they ask.
DOMAINS = [
    ("agent", "The agent layer", "Hooks, guards, skills, subagents and MCP servers -- the machinery every session passes through."),
    ("sched", "Scheduled and running", "Every launchd job, every live session, every process holding CPU."),
    ("pipeline", "The shipping pipeline", "Every gate a commit passes on the way to production."),
    ("platform", "Is it serving", "The customer-facing answer, measured live."),
    ("access", "Access and change control", "Who can change production, and who reviewed it."),
    ("secrets", "Secrets and credentials", "Locations and exposure only. No value is ever printed."),
    ("machine", "The machine itself", "Load, disk, and what is eating them."),
    ("gap", "Where the estate is blind", "Questions nothing here can answer."),
]


def sh(cmd: str, timeout: int = TIMEOUT, cwd: str | None = None) -> tuple[int, str]:
    """Run a shell command. Returns (rc, stdout+stderr stripped). Never raises."""
    try:
        p = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"__timeout__ after {timeout}s"
    except Exception as e:                                   # noqa: BLE001 - a probe reports
        return 125, f"__error__ {type(e).__name__}: {e}"


UNMEASURED = ("__timeout__", "__error__")


def row(domain: str, title: str, value: str, sev: str, proof: str, detail: str = "") -> dict:
    """A check that did not get an answer is UNKNOWN. Never OK, and never a number.

    sh() returns a sentinel string instead of raising, which is right -- one dead command
    must not lose the other forty rows. But the sentinel then flows into whatever the check
    was building, and the page renders "answers but returns __timeout__" as prose, or worse
    grades a row CLEAN on a string it never actually received. Both happened on 2026-08-21
    while the machine sat at load 451.

    Catching it here rather than in fourteen checks is the point: a check added tomorrow
    inherits the refusal without its author knowing the rule exists.
    """
    blob = f"{value} {detail}"
    if any(s in blob for s in UNMEASURED):
        why = "the command timed out" if "__timeout__" in blob else "the command errored"
        clean = re.sub(r"__(?:timeout|error)__[^,.;)]*", "unmeasured", detail).strip()
        return {"domain": "gap", "title": title, "value": "UNMEASURED",
                "severity": UNK, "proof": proof,
                "detail": f"Not graded: {why}, so this row carries no verdict either way. "
                          f"It is not a pass. {clean}".strip()}
    return {"domain": domain, "title": title, "value": value, "severity": sev,
            "proof": proof, "detail": detail}


# ---------------------------------------------------------------- checks

def c_hooks() -> list[dict]:
    out = []
    p = CLAUDE / "settings.json"
    proof = "python3 -c 'json.load(open(~/.claude/settings.json))[\"hooks\"]'"
    try:
        cfg = json.load(open(p))
    except Exception as e:                                   # noqa: BLE001
        return [row("agent", "settings.json unreadable", "ERROR", CRIT, proof, str(e))]
    total, missing, by_ev = 0, [], {}
    for ev, groups in cfg.get("hooks", {}).items():
        n = 0
        for g in groups:
            for h in g.get("hooks", []):
                total += 1
                n += 1
                cmd = h.get("command", "")
                for s in re.findall(r"([A-Za-z0-9_./-]+\.(?:py|sh))", cmd):
                    name = pathlib.Path(s).name
                    if not (CLAUDE / "scripts" / name).exists() and not pathlib.Path(s).exists():
                        missing.append(f"{ev}:{name}")
        by_ev[ev] = n
    ev_s = ", ".join(f"{k} {v}" for k, v in sorted(by_ev.items()))
    out.append(row("agent", "Hook entries wired into every session", str(total),
                   WARN if total > 25 else OK, proof, ev_s))
    if missing:
        out.append(row("agent", "Hook targets that do not exist on disk", str(len(missing)),
                       CRIT, proof, "; ".join(sorted(set(missing))[:6])))
    # a hook that fetches code from the network on every session start
    net = [h.get("command", "") for g in cfg.get("hooks", {}).get("SessionStart", [])
           for h in g.get("hooks", [])]
    fetchers = [c for c in net if "git fetch" in c or "curl" in c]
    if fetchers:
        tgt = re.findall(r"([A-Za-z0-9_.-]+\.py)", fetchers[0])
        out.append(row("agent", "SessionStart fetches and executes code from the network", str(len(fetchers)),
                       CRIT, "settings.json SessionStart hooks",
                       f"git fetch origin main, writes {tgt[0] if tgt else '?'} into /tmp, then runs it. "
                       "Every session start. A compromised main executes here."))
    perm = cfg.get("permissions", {})
    out.append(row("agent", "Permission rules", f"{len(perm.get('allow', []))} allow / {len(perm.get('deny', []))} deny",
                   WARN if "Bash(*)" in perm.get("allow", []) else OK,
                   "settings.json .permissions",
                   f"defaultMode={perm.get('defaultMode', 'unset')}. "
                   + ("Bash(*) is allowed outright; the deny list is read-noise filters, not a security boundary."
                      if "Bash(*)" in perm.get("allow", []) else "")))
    if cfg.get("skipDangerousModePermissionPrompt"):
        out.append(row("agent", "Dangerous-mode permission prompt is disabled", "true", WARN,
                       "settings.json .skipDangerousModePermissionPrompt", ""))
    return out


def c_guards() -> list[dict]:
    d = CLAUDE / "scripts"
    files = sorted(list(d.glob("*.py")) + list(d.glob("*.sh")))
    lines = 0
    no_self = []
    for f in files:
        try:
            t = f.read_text(errors="ignore")
        except Exception:                                    # noqa: BLE001
            continue
        lines += len(t.splitlines())
        if "--selftest" not in t:
            no_self.append(f.name)
    rc, sub = sh("cd ~/.claude && git ls-files -s scripts | head -1 | awk '{print $1}'")
    out = [row("agent", "Guard and probe scripts", f"{len(files)} files / {lines:,} lines",
               WARN, "ls $HOME/Documents/code/claude-guards/*.py *.sh; wc -l",
               "This is the enforcement layer. It runs on this machine only and is not covered by CI.")]
    if no_self:
        out.append(row("agent", "Scripts with no selftest", str(len(no_self)), WARN,
                       "grep -L -- --selftest $HOME/Documents/code/claude-guards/*", ", ".join(no_self[:8])))
    if sub.strip() == "160000":
        out.append(row("agent", "scripts/ is a git submodule of ~/.claude", "160000", WARN,
                       "git ls-files -s scripts",
                       "Commits to a guard land in the submodule, not the outer repo. "
                       "A reviewer reading ~/.claude sees a pointer bump, not the diff."))
    rc, o = sh("python3 $HOME/Documents/code/claude-guards/memory-loop.py --selftest 2>&1 | tail -2")
    bad = "FAIL" in o.upper() or re.search(r"\b([0-9]+) of ([0-9]+)", o) and \
        (lambda m: m and m.group(1) != m.group(2))(re.search(r"\b([0-9]+) of ([0-9]+)", o))
    out.append(row("agent", "Laws injector selftest", o.splitlines()[-1][:70] if o else "no output",
                   CRIT if bad else OK, "memory-loop.py --selftest",
                   "This is the hook that puts the 17 laws in front of every session."))
    return out


def c_skills_mcp() -> list[dict]:
    out = []
    rc, n = sh("find ~/.claude/plugins -name SKILL.md 2>/dev/null | wc -l")
    rc2, dirs = sh("ls ~/.claude/plugins/cache 2>/dev/null")
    out.append(row("agent", "Skills reachable from every session", n.strip(), WARN,
                   "find ~/.claude/plugins -name SKILL.md | wc -l",
                   f"Cached under: {dirs.strip() or 'unknown'}. A directory name like "
                   "temp_git_<epoch>_<rand> is a checkout nobody named."))
    rc, us = sh("ls ~/.claude/skills 2>/dev/null | tr '\\n' ' '")
    out.append(row("agent", "User-authored skills", str(len(us.split())), OK,
                   "ls ~/.claude/skills", us.strip()))
    try:
        d = json.load(open(HOME / ".claude.json"))
        m = d.get("mcpServers", {})
        det = "; ".join(f"{k} -> {v.get('command', '?')} {' '.join(v.get('args', []))[:60]}" for k, v in m.items())
    except Exception as e:                                   # noqa: BLE001
        m, det = {}, str(e)
    out.append(row("agent", "MCP servers wired globally", str(len(m)), WARN if m else OK,
                   "~/.claude.json .mcpServers", det))
    rc, a = sh("ls ~/.claude/agents/*.md 2>/dev/null | wc -l")
    rc2, a2 = sh(f"ls {PROSPECTOR}/.claude/agents/*.md 2>/dev/null | wc -l")
    rc3, p3 = sh(f"ls {PROSPECTOR}/docs/personas/*.md 2>/dev/null | wc -l")
    out.append(row("agent", "Subagent definitions on disk", f"{a.strip()} global / {a2.strip()} repo",
                   CRIT if a.strip() == "0" else OK,
                   "ls ~/.claude/agents/*.md; ls <repo>/.claude/agents/*.md",
                   f"Decision d9861f649fe4 put the personas in ~/.claude/agents/*.md. "
                   f"That directory holds none. {p3.strip()} persona documents live in "
                   f"{PROSPECTOR.name}/docs/personas and are prose, not agent definitions."))
    return out


def c_sessions() -> list[dict]:
    rc, n = sh("pgrep -f 'claude' | wc -l")
    rc2, s = sh("ls /tmp/cc-socks/*.sock 2>/dev/null | wc -l")
    return [row("sched", "Claude sessions live on this machine right now",
                f"{s.strip()} sessions / {n.strip()} processes",
                WARN if (s.strip().isdigit() and int(s) > 2) else OK,
                "ls /tmp/cc-socks/*.sock; pgrep -f claude | wc -l",
                "Each session bills independently and cannot see the others' work.")]


#: Label prefixes belonging to this estate. Steam's cleanup job exits 78 every
#: run and no agent here will ever fix it; grading it critical makes a red that
#: cannot go green, and a gate that is red forever gets ignored.
OURS = ("com.prospector", "com.estate", "com.founder", "com.chidionyema", "ai.")

#: Jobs whose exit 1 means "I found something", not "I crashed". Report-mode jobs
#: on this estate exit non-zero when they have findings, so the exit code cannot
#: tell a working instrument from a dead one. Every entry below was verified on
#: 2026-08-24 by reading the job's own last output, quoted in the reason.
#:
#: The excuse covers exit status "1" ONLY. A declared job that dies on a signal or
#: any other code is still a crash, so this list cannot quietly swallow the failure
#: of the job it names.
FINDING_EXIT = {
    "com.chidionyema.guard-selftest":
        "exit 1 = guards with no selftest. Last run 2026-08-23 19:17 printed "
        "'no selftest (45): aiden/aiden.py, ...'. That is a census, not a crash.",
    "com.estate.costsentinel":
        "exit 1 = spend over cap. Last run 2026-08-24 01:22 printed 'Claude spend "
        "2026-08-24: $129.86 of $120 cap (904 requests)' and '[sentinel] WARN "
        "delivered: 13168'. It exits non-zero BECAUSE it worked and delivered.",
    "com.founder.sciencecollect":
        "exit 1 = stale science inputs. Last run 2026-08-24 01:11 printed 'needs "
        "attention: would_have_fired STALE 51h, decisions STALE 55h'.",
    "com.prospector.estate-inventory":
        "exit 1 = undescribed resources. Last run printed '38 resources, 28 "
        "undescribed, 0 admitted, 8 classes not probed.'",
    "com.prospector.launchd-held":
        "exit 1 = jobs declared but not held. Last run 2026-08-24 01:12 printed "
        "'LAUNCHD HELD FAIL 7 finding(s) (not-held=7, ...)'.",
}


def grade_launchd(jobs: list[tuple[str, str, str]]) -> dict[str, list[str]]:
    """Split launchd jobs by what their last exit actually MEANS.

    Three different facts used to be collapsed into one number here, and the
    number was wrong in the founder's face every hour.

    A job with a live pid is running RIGHT NOW, so its recorded exit code
    describes a process launchd has already replaced. Measured 2026-08-24: five
    of the ten jobs this check called failing were alive, including
    ai.architect.gateway (pid 60261, answering Telegram) and com.chidionyema.maestro
    itself (pid 79018) -- maestro's own audit was counting maestro as a failed job
    while maestro was the process running the audit. The pid was in column 1 of
    the same launchctl output this function already parsed, and it was discarded.

    A report-mode job exits non-zero when it FINDS something. Exit code cannot
    tell that from a crash, so those are declared in FINDING_EXIT with the line
    they printed, and they are reported as findings rather than as failures.

    Everything left is a real crash and is still graded critical, INCLUDING a job
    nobody has declared. An allow-list whose unknown branch falls through quietly
    is how ten criticals went missing here for 18 hours.
    """
    buckets: dict[str, list[str]] = {"notloaded": [], "running": [],
                                     "findings": [], "crashed": [], "foreign": []}
    for label, pid, status in jobs:
        if status == "notloaded":
            buckets["notloaded"].append(label)
        elif pid not in ("-", ""):
            buckets["running"].append(label)          # exit code is history
        elif status == "0":
            pass                                       # exited clean, nothing to say
        elif not label.startswith(OURS):
            buckets["foreign"].append(f"{label}({status})")
        elif status == "1" and label in FINDING_EXIT:
            buckets["findings"].append(label)
        else:
            buckets["crashed"].append(f"{label}({status})")
    return buckets


def c_launchd() -> list[dict]:
    # launchctl list is the expensive call -- make it ONCE, then join in awk.
    # Column 1 is the pid and it is the whole difference between "this job is
    # running" and "this job died"; it must come back with the status.
    script = r"""
launchctl list 2>/dev/null > /tmp/.ea_lc.$$
for p in ~/Library/LaunchAgents/*.plist; do
  lbl=$(/usr/libexec/PlistBuddy -c "Print :Label" "$p" 2>/dev/null) || continue
  [ -z "$lbl" ] && continue
  pid=$(awk -v l="$lbl" '$3==l{print $1; exit}' /tmp/.ea_lc.$$)
  st=$(awk -v l="$lbl" '$3==l{print $2; exit}' /tmp/.ea_lc.$$)
  [ -z "$st" ] && st="notloaded"
  [ -z "$pid" ] && pid="-"
  echo "$lbl|$pid|$st"
done
rm -f /tmp/.ea_lc.$$
"""
    rc, o = sh(script, timeout=25)
    jobs = [tuple(l.split("|", 2)) for l in o.splitlines() if l.count("|") == 2]
    b = grade_launchd(jobs)  # type: ignore[arg-type]
    out = [row("sched", "launchd jobs installed", str(len(jobs)), WARN,
               "PlistBuddy Print :Label over ~/Library/LaunchAgents/*.plist",
               "Every one of these runs with no session attached and no human watching.")]
    if b["crashed"]:
        out.append(row("sched", "launchd jobs that crashed and are not running", str(len(b["crashed"])),
                       CRIT, "launchctl list, joining pid and exit status",
                       "Not running, exited non-zero, and not declared as a report job: "
                       + ", ".join(sorted(b["crashed"]))))
    if b["findings"]:
        out.append(row("sched", "Report jobs with open findings", str(len(b["findings"])),
                       WARN, "launchctl list, joining pid and exit status; FINDING_EXIT",
                       "These exited 1 because they FOUND something, which is them working: "
                       + ", ".join(sorted(b["findings"]))))
    if b["foreign"]:
        out.append(row("sched", "Third-party launchd jobs failing", str(len(b["foreign"])),
                       WARN, "launchctl list", "Not ours to fix: " + ", ".join(sorted(b["foreign"]))))
    if b["notloaded"]:
        out.append(row("sched", "launchd jobs installed but never loaded", str(len(b["notloaded"])),
                       WARN, "launchctl list", ", ".join(sorted(b["notloaded"]))))
    return out


#: The four endpoints this audit used to curl every hour. Three of them sit on Fly
#: machines with auto_start=True, which means the probe itself started the machine
#: it was measuring: 24 wakeups a day, each indistinguishable from a real visitor.
#: Founder, 2026-08-23: "turn off all fly machines", "we need to come off fly
#: totally", "just stop the machines, no wake up". A health check that boots the
#: thing it checks is not an instrument, it is a load generator.
STOPPED_ENDPOINTS = ["prospector-store-web.fly.dev/api/health",
                     "prospector-store-api.fly.dev/catalog",
                     "tie-web.fly.dev/",
                     "tie-api.fly.dev/health/ready"]


def c_endpoints() -> list[dict]:
    """Report the endpoints as deliberately unprobed, never as healthy.

    Deleting the check outright would leave a board with nothing where the
    platform row used to be, and a missing row reads as "fine" to the next person
    who scans it. NOT PROBED and PASS have to look different (LAW 28), so each
    endpoint keeps its line and states why nobody is measuring it.
    """
    return [row("platform", u, "NOT PROBED", UNK, "(no command -- deliberately not run)",
                "on a stopped Fly machine with auto_start; curling it would restart the "
                "machine and bill for it. Fly is being exited, so this endpoint is not "
                "expected to serve. Restore this probe only when the service has a home "
                "that a health check does not switch on.")
            for u in STOPPED_ENDPOINTS]


def c_fly() -> list[dict]:
    rc, o = sh("flyctl apps list --json 2>/dev/null", timeout=25)
    try:
        apps = json.loads(o)
    except Exception:                                        # noqa: BLE001
        return [row("sched", "Fly applications", "UNKNOWN", UNK, "flyctl apps list --json",
                    "flyctl did not return JSON: " + o[:90])]
    dep = [a for a in apps if a.get("Status") == "deployed"]
    sus = [a for a in apps if a.get("Status") == "suspended"]
    return [row("sched", "Fly applications", f"{len(apps)} ({len(dep)} deployed, {len(sus)} suspended)",
                WARN, "flyctl apps list --json",
                "Suspended: " + ", ".join(sorted(a.get("Name", "?") for a in sus)))]


def c_access() -> list[dict]:
    out = []
    rc, o = sh("gh api repos/chidionyema/prospector/branches/main/protection 2>&1 | head -2", timeout=20)
    if "Upgrade to GitHub" in o or '"status": "403"' in o or "403" in o:
        out.append(row("access", "Branch protection on prospector/main", "IMPOSSIBLE", CRIT,
                       "gh api repos/chidionyema/prospector/branches/main/protection",
                       "Private repository on a free plan. GitHub refuses the feature. No required "
                       "review, no enforced admin, no required status check. Every merge guard on "
                       "this estate is voluntary and local."))
    elif "required_pull_request_reviews" in o:
        out.append(row("access", "Branch protection on prospector/main", "configured", OK,
                       "gh api .../branches/main/protection", o[:100]))
    else:
        out.append(row("access", "Branch protection on prospector/main", "UNKNOWN", UNK,
                       "gh api .../branches/main/protection", o[:100]))
    rc, br = sh("git rev-parse --abbrev-ref HEAD", cwd=str(PROSPECTOR))
    rc2, dirty = sh("git status --porcelain | wc -l", cwd=str(PROSPECTOR))
    if br.strip() == "HEAD":
        out.append(row("access", "Shared checkout is in detached HEAD", f"{dirty.strip()} modified files",
                       CRIT, "git rev-parse --abbrev-ref HEAD; git status --porcelain | wc -l",
                       f"{PROSPECTOR} is not on a branch. Modified files there belong to no branch "
                       "and multiple sessions share the checkout."))
    rc, au = sh("git log --since='7 days ago' --all --format='%an' | sort -u | wc -l", cwd=str(PROSPECTOR))
    rc2, cm = sh("git log --since='24 hours ago' --oneline --all | wc -l", cwd=str(PROSPECTOR))
    rc3, nb = sh("git branch | wc -l", cwd=str(PROSPECTOR))
    out.append(row("access", "Change volume with no review gate",
                   f"{cm.strip()} commits/24h, {nb.strip()} branches, {au.strip()} identities", WARN,
                   "git log --since='24 hours ago' --oneline --all | wc -l",
                   "Machine and human commits are indistinguishable in the log."))
    return out


def c_secrets() -> list[dict]:
    """Locations and lengths only. No credential value is ever emitted."""
    out = []
    pats = {"Stripe LIVE secret": (r"sk_live_[A-Za-z0-9]{20,}", CRIT),
            "Anthropic API key": (r"sk-ant-api[A-Za-z0-9_-]{20,}", CRIT),
            "HuggingFace token": (r"\bhf_[A-Za-z0-9]{30,}", CRIT),
            "GitHub PAT": (r"ghp_[A-Za-z0-9]{30,}", CRIT),
            "AWS access key": (r"AKIA[A-Z0-9]{16}", CRIT)}
    hist = CLAUDE / "history.jsonl"
    hits: dict[str, list[str]] = {}
    if hist.exists():
        try:
            for i, line in enumerate(hist.read_text(errors="ignore").splitlines(), 1):
                for name, (rx, _) in pats.items():
                    for m in re.finditer(rx, line):
                        v = m.group(0)
                        hits.setdefault(name, []).append(f"line {i} ({v[:8]}..., {len(v)} chars)")
        except Exception as e:                               # noqa: BLE001
            out.append(row("secrets", "history.jsonl unreadable", "ERROR", UNK, "read", str(e)))
    for name, locs in hits.items():
        out.append(row("secrets", f"{name} in plaintext shell history", f"{len(locs)} occurrence(s)",
                       CRIT, "regex scan of ~/.claude/history.jsonl -- prefix and length only",
                       "; ".join(locs[:4]) + ". Value never printed. Rotation is the founder's alone."))
    if not hits and hist.exists():
        out.append(row("secrets", "No live credential pattern in shell history", "0", OK,
                       "regex scan of ~/.claude/history.jsonl", ""))
    rc, ign = sh("cd ~/.claude && git check-ignore -v history.jsonl 2>/dev/null | head -1")
    rc2, trk = sh("cd ~/.claude && git ls-files --error-unmatch history.jsonl >/dev/null 2>&1 && echo tracked || echo untracked")
    out.append(row("secrets", "Shell history exposure surface", trk.strip(),
                   OK if trk.strip() == "untracked" else CRIT,
                   "git ls-files --error-unmatch; git check-ignore -v",
                   f"mode {oct(hist.stat().st_mode)[-3:] if hist.exists() else '?'}; "
                   f"gitignore: {ign.strip() or 'not matched'}. Untracked and ignored means the "
                   "leak is local-only, not published."))
    files = [".aws/credentials", ".config/gh/hosts.yml", ".cache/huggingface/token",
             ".claude/.credentials.json", ".npmrc", ".hermes/.env"]
    bad = []
    present = 0
    for f in files:
        p = HOME / f
        if not p.exists():
            continue
        present += 1
        m = oct(p.stat().st_mode)[-3:]
        if m not in ("600", "400"):
            bad.append(f"{f} is {m}")
    out.append(row("secrets", "Credential files with correct 600 permissions",
                   f"{present - len(bad)} of {present}", CRIT if bad else OK,
                   "stat -f %Lp on each credential file", "; ".join(bad) or "all locked down"))
    out.append(row("gap", "Credential age or last-rotation date", "0 probes", UNK,
                   "recon over all estate probes: none reads a credential timestamp",
                   "An auditor asks 'when was this last rotated?' and nothing here can answer, for any key."))
    return out


def c_machine() -> list[dict]:
    rc, up = sh("uptime")
    la = re.search(r"load averages?: ([\d.]+),? ([\d.]+),? ([\d.]+)", up)
    l1, l5, l15 = (la.groups() if la else ("?", "?", "?"))
    rc2, df = sh("df -h / | tail -1 | awk '{print $4\" free of \"$2\" (\"$5\" used)\"}'")
    rc3, top = sh("ps -Ao pcpu,rss,comm -r | sed -n '2,4p'")
    hot = []
    for line in top.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            rss = int(parts[1]) // 1024 if parts[1].isdigit() else 0
            hot.append(f"{parts[2].split('/')[-1]} {parts[0]}% CPU / {rss}MB")
    out = [row("machine", "Load average (1 / 5 / 15 min)", f"{l1} / {l5} / {l15}",
               CRIT if float(l15 or 0) > 10 else (WARN if float(l5 or 0) > 4 else OK),
               "uptime",
               "A 15-minute figure far above the 1-minute one means the machine was recently "
               "many times oversubscribed."),
           row("machine", "Root volume", df.strip(), OK, "df -h /", ""),
           row("machine", "Top three processes by CPU", hot[0] if hot else "?", WARN,
               "ps -Ao pcpu,rss,comm -r | head -4", "; ".join(hot))]
    rc, ol = sh("pgrep -fl 'llama-server|ollama' | head -1")
    if ol.strip():
        out.append(row("machine", "A local LLM server is running", "ollama / llama-server", WARN,
                       "pgrep -fl 'llama-server|ollama'",
                       "Not part of the shipping estate and not in any inventory. It is the "
                       "single largest consumer of CPU and RAM on this laptop."))
    return out


def c_gaps() -> list[dict]:
    g = [("Backup restore test", "Backups run and fail loudly; a restore has never been attempted."),
         ("Data classification / PII map", "Nothing records what personal data is held or where."),
         ("Uptime / SLA history", "Endpoint status here is a spot check. Availability over time is not stored."),
         ("Open-defect density over time", "Issue counts are a snapshot; the trend is not kept."),
         ("Guard-layer test coverage", "16,992 lines of enforcement run on this laptop and are not covered by CI."),
         ("Vendor / subprocessor register", "No list of who processes data on the company's behalf.")]
    return [row("gap", t, "0 probes", UNK, "recon over all estate probes and scheduled jobs", d)
            for t, d in g]


def c_telegram_polling() -> list[dict]:
    """crew#30: the Architect's bot was deaf 72% of the time for days and nothing measured it.
    The symptom is 'Telegram polling conflict' in the gateway log. Count the last 24 h."""
    log = pathlib.Path(os.environ.get("HERMES_V2_HOME", str(HOME / "dev" / "code" / "hermes-v2"))) / "logs" / "gateway.log"
    proof = f"grep -c 'polling conflict' {log} (last 24 h by line timestamp)"
    if not log.exists():
        return [row("agent", "Telegram polling conflicts, 24 h", "NO LOG", UNK, proof, str(log))]
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 86400))
    n = 0
    try:
        with open(log, errors="replace") as fh:
            for line in fh:
                if "polling conflict" in line and line[:16] >= since:
                    n += 1
    except OSError as e:
        return [row("agent", "Telegram polling conflicts, 24 h", "ERROR", UNK, proof, str(e))]
    # one conflict every ~42 s was the deaf-72% incident (2,000/day); 20 is a fault, not a blip
    return [row("agent", "Telegram polling conflicts, 24 h", str(n),
                CRIT if n >= 20 else (WARN if n else OK), proof,
                "a second getUpdates consumer holds the bot; inbound is delayed up to 30 s" if n else "")]


CHECKS = [c_hooks, c_guards, c_skills_mcp, c_sessions, c_launchd,
          c_endpoints, c_fly, c_access, c_secrets, c_machine, c_gaps, c_telegram_polling]


#: How long the whole sweep may take before the slow checks are written off as
#: unknown. It is a wall across all of them, not a per-check budget; sh() already
#: caps each command.
COLLECT_TIMEOUT = 90


def collect() -> dict:
    t0 = time.time()
    rows: list[dict] = []
    ex = futures.ThreadPoolExecutor(max_workers=max(8, len(CHECKS)))
    fut = {ex.submit(fn): fn.__name__ for fn in CHECKS}
    try:
        for f in futures.as_completed(fut, timeout=COLLECT_TIMEOUT):
            try:
                rows.extend(f.result())
            except Exception as e:                           # noqa: BLE001 - a check that dies is a finding
                rows.append(row("gap", f"check {fut[f]} failed", "ERROR", UNK,
                                f"estate_audit.py::{fut[f]}", f"{type(e).__name__}: {e}"))
    except futures.TimeoutError:
        # A slow probe must never take the whole report with it. as_completed raises out
        # of the loop, that exception reached launchd as exit 1, and no page was written
        # at all. The auditor therefore went silent in exactly the hour it had something
        # to say, because a machine sick enough to make a probe slow is the machine the
        # founder needs the page for. Measured 2026-08-23: three runs died this way,
        # "3 (of 17) futures unfinished" and "5 (of 18)".
        #
        # Every check that did not land is named as UNKNOWN, which is what it is, and the
        # forty-odd rows that did land are written.
        for f, name in fut.items():
            if not f.done():
                rows.append(row("gap", f"check {name} did not finish", "no answer", UNK,
                                f"estate_audit.py::{name}",
                                f"Still running after {COLLECT_TIMEOUT}s. This is an "
                                f"absence, not a grade: the check was neither passed "
                                f"nor failed, it was never answered."))
    finally:
        # wait=False so a hung probe cannot hold the report back. cancel_futures stops the
        # ones that never started; the ones already running end on sh()'s own timeout.
        ex.shutdown(wait=False, cancel_futures=True)
    order = {CRIT: 0, WARN: 1, UNK: 2, OK: 3}
    rows.sort(key=lambda r: (order.get(r["severity"], 4), r["domain"]))
    return {
        "generated_at": time.time(),
        "generated_at_iso": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()),
        "duration_s": round(time.time() - t0, 1),
        "counts": {s: sum(1 for r in rows if r["severity"] == s) for s in (CRIT, WARN, UNK, OK)},
        "rows": rows,
    }


# ---------------------------------------------------------------- pipeline

def c_pipeline() -> list[dict]:
    """Every gate a commit passes on the way to production."""
    out = []
    R = str(PROSPECTOR)
    rc, hp = sh("git config --get core.hooksPath", cwd=R)
    rc2, hooks = sh("ls .git/hooks | grep -v '\\.sample$' | tr '\\n' ' '", cwd=R)
    names = hooks.split()
    out.append(row("pipeline", "Active git hooks in the shared checkout", str(len(names)), WARN,
                   "ls .git/hooks | grep -v '\\.sample$'",
                   f"{', '.join(names)}. core.hooksPath is "
                   f"{hp.strip() or 'unset -- so .githooks/ is only reached via the pre-push dispatcher'}. "
                   "These live in .git/, which is not version-controlled: a fresh clone gets none of them."))
    for f in (".pre-commit-config.yaml", "lefthook.yml", ".husky"):
        pass
    rc, fw = sh("ls -d .pre-commit-config.yaml lefthook.yml lefthook.yaml .husky 2>/dev/null | tr '\\n' ' '", cwd=R)
    out.append(row("pipeline", "Standard pre-commit framework", fw.strip() or "absent",
                   WARN if not fw.strip() else OK,
                   "ls -d .pre-commit-config.yaml lefthook.yml .husky",
                   "Gating is bespoke: hand-written git hooks plus CI. Nothing a new machine "
                   "installs automatically." if not fw.strip() else ""))
    rc, wf = sh("ls .github/workflows/*.yml .github/workflows/*.yaml 2>/dev/null | wc -l", cwd=R)
    out.append(row("pipeline", "CI workflows", wf.strip(), WARN,
                   "ls .github/workflows/*.yml | wc -l",
                   "Includes the merge robot, the admission guard, the green guard, the PR keeper, "
                   "three deploy workflows and four scheduled drills."))
    rc, gates = sh("ls scripts/guard_*.py scripts/*gate*.py scripts/popdd_verify.py "
                   "scripts/claude_guards/*.py 2>/dev/null | wc -l", cwd=R)
    out.append(row("pipeline", "Admission and gate scripts the pipeline invokes", gates.strip(), WARN,
                   "ls scripts/guard_*.py scripts/*gate*.py scripts/claude_guards/*.py | wc -l",
                   "popdd_verify.py signs a proof receipt per lane; guard_main_push refuses a direct "
                   "push to main; load_gate decides whether the machine can produce a trustworthy "
                   "test result at all."))
    out.append(row("pipeline", "Control on main is detective, not preventive", "compensating", CRIT,
                   ".github/workflows/main-admission-guard.yml, job 'admit'",
                   "Branch protection is unavailable on this plan, so the estate substitutes a job "
                   "that reads main after a write and REVERTS. Bad code reaches main first and is "
                   "removed afterwards. An auditor treats that as a compensating control with a "
                   "window, not as a preventive one."))
    return out


def c_pipeline_ci() -> list[dict]:
    """The network half of the pipeline: CI outcomes, open PRs, test floor, deploys."""
    out: list[dict] = []
    R = str(PROSPECTOR)
    rc, runs = sh("gh run list --limit 40 --json workflowName,conclusion 2>/dev/null", cwd=R, timeout=25)
    try:
        rr = json.loads(runs)
        tally: dict[str, dict[str, int]] = {}
        for r in rr:
            w = r.get("workflowName", "?")
            c = r.get("conclusion") or "in_progress"
            tally.setdefault(w, {}).setdefault(c, 0)
            tally[w][c] += 1
        red = {w: t for w, t in tally.items() if t.get("failure", 0) > 0}
        det = "; ".join(f"{w}: {t.get('failure', 0)} failed of {sum(t.values())}"
                        for w, t in sorted(red.items(), key=lambda kv: -kv[1].get("failure", 0)))
        out.append(row("pipeline", "Workflows failing in the last 40 runs", str(len(red)),
                       CRIT if red else OK, "gh run list --limit 40 --json workflowName,conclusion",
                       det or "no failures in the window"))
    except Exception:                                        # noqa: BLE001
        out.append(row("pipeline", "Recent CI outcomes", "UNKNOWN", UNK,
                       "gh run list --limit 40 --json workflowName,conclusion", runs[:90]))
    rc, prs = sh("gh pr list --json number,title,mergeable,statusCheckRollup --limit 20 2>/dev/null",
                 cwd=R, timeout=25)
    try:
        pl = json.loads(prs)
        bad = []
        for p in pl:
            fails = sum(1 for c in (p.get("statusCheckRollup") or [])
                        if (c.get("conclusion") or "").upper() in ("FAILURE", "TIMED_OUT", "CANCELLED"))
            if fails or p.get("mergeable") != "MERGEABLE":
                bad.append(f"#{p['number']} {p.get('mergeable', '?')} {fails} failing")
        out.append(row("pipeline", "Open pull requests not mergeable", f"{len(bad)} of {len(pl)}",
                       CRIT if bad else OK, "gh pr list --json number,mergeable,statusCheckRollup",
                       "; ".join(bad) or "all clear"))
    except Exception:                                        # noqa: BLE001
        out.append(row("pipeline", "Open pull requests", "UNKNOWN", UNK, "gh pr list", prs[:90]))
    rc, tf = sh("rg --files -g 'test_*.py' -g '*_test.py' -g '*.spec.ts' 2>/dev/null | wc -l",
                cwd=R, timeout=25)
    rc2, cov = sh("rg -n 'fail_under|--cov-fail-under' pytest.ini .github/workflows/ 2>/dev/null | head -1", cwd=R)
    out.append(row("pipeline", "Test files, and the coverage floor that gates them",
                   f"{tf.strip()} files / {'threshold set' if cov.strip() else 'NO threshold'}",
                   WARN if not cov.strip() else OK,
                   "rg --files -g 'test_*.py' | wc -l; rg -n 'fail_under' pytest.ini .github/workflows/",
                   "555 test files with no coverage floor means coverage can fall to zero and "
                   "every gate still reports green." if not cov.strip() else cov[:80]))
    rc, dep = sh("rg -l 'flyctl deploy|fly deploy' .github/workflows/ 2>/dev/null | wc -l", cwd=R)
    out.append(row("pipeline", "Workflows that deploy to production", dep.strip(), WARN,
                   "rg -l 'flyctl deploy|fly deploy' .github/workflows/",
                   "deploy-api -> prospector-store-api, deploy-web -> prospector-store-web, "
                   "deploy-engine -> prospector-engine. Each gated test -> deploy, triggered by "
                   "push to main on a path filter, or by hand."))
    return out


CHECKS.append(c_pipeline)
CHECKS.append(c_pipeline_ci)


# The repositories the estate ships from. They are public on purpose, by a founder ruling on
# 2026-08-23: "sort this out nake then public if u have 2". Public is not a preference here, it
# is what makes Actions run at all -- a private repository on this account spends paid minutes,
# the account's payments have failed, and every job in one is refused before its first step.
SHIPS_FROM = ("chidionyema/prospector", "chidionyema/crew",
              "chidionyema/maestro", "chidionyema/claude-guards")

# Every reading is appended here, healed or not. Without it, "claude-guards went private again"
# is one agent's recollection against another's, which is what happened on 2026-08-23: it was
# reported private twice, GitHub's own event stream recorded no private->public transition
# either time, and nobody could say which instrument was lying. A file settles that.
CI_REACH_LOG = STATE / "ci-reach.jsonl"


def c_ci_reach() -> list[dict]:
    """Can GitHub Actions actually start a job in each repo the estate ships from?

    A private repository on this account cannot. The job is refused before it runs, with
    "recent account payments have failed or your spending limit needs to be increased" --
    a message that names billing and really means "private repo, unpaid account". The pull
    request then sits UNSTABLE forever because its gate can never go green.

    This check does three things, and the second two are why it exists rather than just
    reporting. It RECORDS every reading to CI_REACH_LOG, so a repository that changes
    visibility leaves a trace nobody has to remember. It HEALS: a repository in SHIPS_FROM
    found private is put back to public in the same pass, because detecting a thing that
    stops every gate in the estate and waiting for a person to read a dashboard is not a
    fix. And it reports what it did, so a heal is visible rather than silent.

    The healer lives here, on the Mac, and not in a workflow. A workflow inside
    claude-guards cannot repair claude-guards: while the repository is private its jobs are
    exactly what will not start.
    """
    out: list[dict] = []
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for repo in SHIPS_FROM:
        rc, vis = sh(f"gh api repos/{repo} -q .visibility 2>/dev/null", timeout=30)
        vis = vis.strip() or "unknown"
        healed = ""
        if vis == "private":
            # Named repositories only. This never widens to "any repo that looks private".
            rc_h, after = sh(f"gh api --method PATCH repos/{repo} -f visibility=public "
                             "-q .visibility 2>/dev/null", timeout=25)
            after = after.strip()
            healed = "public" if after == "public" else f"failed ({after or 'no response'})"
            if after == "public":
                vis = "public"
        rc, o = sh(f"gh run list --repo {repo} --limit 5 "
                   "--json conclusion -q '[.[].conclusion]|@csv' 2>/dev/null", timeout=25)
        outcomes = [c.strip('"') for c in o.strip().split(",") if c.strip()]
        ran = any(c in ("success", "failure") for c in outcomes)
        try:
            CI_REACH_LOG.parent.mkdir(parents=True, exist_ok=True)
            with CI_REACH_LOG.open("a") as fh:
                fh.write(json.dumps({"at": stamp, "repo": repo, "visibility": vis,
                                     "healed": healed, "last5": outcomes}) + "\n")
        except OSError:
            try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 636))
            except Exception: pass
        if healed.startswith("failed"):
            sev, note = CRIT, (f"found private and could not be put back: {healed}. Every job "
                               "in this repo is refused before it starts until it is public")
        elif healed:
            sev, note = WARN, ("found private and put back to public in this pass; every job "
                               f"was being refused until now. History: {CI_REACH_LOG}")
        elif vis == "private" or (outcomes and all(c == "failure" for c in outcomes)
                                  and vis != "public"):
            sev, note = CRIT, ("private repo on an unpaid account: every job is refused before "
                               "it starts, so this repo's gate can never go green")
        elif not outcomes:
            sev, note = UNK, "no workflow runs in the window"
        elif not ran:
            sev, note = WARN, "runs exist but none completed in the window"
        else:
            sev, note = OK, f"last 5: {', '.join(outcomes)}"
        out.append(row("pipeline", f"Actions can run in {repo}", vis, sev,
                       f"gh api repos/{repo} -q .visibility", note))
    return out


CHECKS.append(c_ci_reach)


def c_envfiles() -> list[dict]:
    rc, envs = sh("for f in $(rg --files -g '.env' -g '.env.production' "
                  f"{PROSPECTOR} {HOME}/.hermes 2>/dev/null); do "
                  "m=$(stat -f %Lp $f); [ \"$m\" = 600 ] || echo \"$(basename $(dirname $f))/$(basename $f):$m\"; done",
                  timeout=20)
    loose = [x for x in envs.splitlines() if x.strip()]
    return [row("secrets", "Real .env files not mode 600", str(len(loose)),
                WARN if loose else OK,
                "stat -f %Lp over every .env found by rg --files", "; ".join(loose[:5]))]


CHECKS.append(c_envfiles)


def c_clock() -> list[dict]:
    """Is the system clock right, and did it jump?

    Written 2026-08-23. On 2026-08-22 this machine lost power at 21:51 with the
    battery at level 3 and 124 mAh, and came back with the clock 19.77 hours in
    the past. It then ran for thirteen hours with the wrong date and not one thing
    on this machine said a word. The founder found it himself, in the morning.

    Why it survived the reboot, which is the part worth knowing: no time daemon
    will STEP a correction that large. An offset of 77841 seconds normally means
    the measurement is wrong, not the clock, so timed measures it cleanly, refuses
    to act, and does that forever. The component whose job is to fix the clock is
    the reason a bad clock persists.

    Two angles, because one instrument here can lie in a way the other cannot.
    sntp says how wrong the clock is NOW. The pmset log says whether it JUMPED,
    which is a fact about the past that survives the clock being corrected since.
    A machine that is right now but jumped an hour ago is not a healthy machine.

    Nothing here sets the clock. A probe that repairs is a probe you cannot trust
    to report, and a 20-hour forward step stampedes every launchd job at once.
    """
    out = []

    rc, sn = sh("/usr/bin/sntp -t 5 time.apple.com 2>&1 | tail -1", timeout=15)
    m = re.match(r"([+-][\d.]+)\s+\+/-\s+([\d.]+)", sn.strip())
    if not m:
        out.append(row("machine", "System clock offset", sn.strip() or "no reply", UNK,
                       "/usr/bin/sntp -t 5 time.apple.com",
                       "No usable reply, so this row carries no verdict. It is not a pass."))
    else:
        off, err = float(m.group(1)), float(m.group(2))
        sev = CRIT if abs(off) > 300 else (WARN if abs(off) > 5 else OK)
        detail = f"Measurement error +/- {err:.3f}s."
        if sev == CRIT:
            detail += (" No time daemon will step a correction this large on its own, so it "
                       "will not fix itself and it survives a reboot. "
                       "Fix: sudo sntp -sS time.apple.com")
        out.append(row("machine", "System clock offset", f"{off:+.3f}s", sev,
                       "/usr/bin/sntp -t 5 time.apple.com", detail))

    rc2, steps = sh("/usr/bin/pmset -g log 2>/dev/null | awk "
                    "'/^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] /"
                    "{t=substr($0,1,19); if (prev!=\"\" && t<prev) print prev\" -> \"t; prev=t}'",
                    timeout=30)
    jumps = [x for x in steps.splitlines() if "->" in x]
    out.append(row("machine", "Backward clock steps in the power log", str(len(jumps)),
                   WARN if jumps else OK,
                   "pmset -g log, scanned for a timestamp older than the one before it",
                   ("Most recent: " + jumps[-1] + ". The log is written by the OS and is "
                    "append-only, so a timestamp older than the one above it is proof the "
                    "clock moved backward, and says when and by how much."
                    ) if jumps else
                   "The power log covers about seven days and its timestamps only go forward."))
    return out


CHECKS.append(c_clock)


def c_backup() -> list[dict]:
    """Is every commit on this Mac also somewhere else, and did anything check today?

    Written 2026-08-23, the morning the founder's board said "39 commits on local Mac
    only, no remote, one disk crash = gone". The estate already had a backup script,
    scripts/backup_agent_estate.py in prospector. It is careful, well commented, and
    covers no .git directory, uploads nowhere, runs on no schedule, and has never
    written a receipt. Nobody noticed for months because nothing read it.

    So this row does not run a backup and it does not trust one either. It reads the
    receipt file the hourly job appends to, which is the only artefact that exists
    because bytes actually moved. No receipt means no backup, whatever any script says.

    It deliberately does NOT walk the repos itself. sh() runs /bin/bash, and /bin/bash
    is denied ~/Documents on this machine, so a walk from here would find zero repos
    under the tree that holds most of them and report that as safety.
    """
    out = []
    rec = pathlib.Path.home() / ".claude/state/estate-bundle-push.jsonl"

    if not rec.exists():
        out.append(row("sched", "Off-machine backup of local-only commits", "NEVER RUN", CRIT,
                       "~/.claude/state/estate-bundle-push.jsonl does not exist",
                       "No bundle has ever been pushed. Every commit that is on this disk and "
                       "on no remote dies with the disk."))
        return out

    lines = [l for l in rec.read_text().splitlines() if l.strip()]
    try:
        recs = [json.loads(l) for l in lines]
    except Exception:                                        # noqa: BLE001
        recs = []
    recs = [r for r in recs if r.get("ts")]
    if not recs:
        out.append(row("sched", "Off-machine backup of local-only commits", "NO RECEIPTS", CRIT,
                       "estate-bundle-push.jsonl exists but holds no parsable receipt",
                       "The file is there and empty of proof, which reads as a working backup "
                       "and is not one."))
        return out

    newest = max(r["ts"] for r in recs)
    age_min = int((time.time() - newest) / 60)
    # The job runs hourly. Two missed runs is a stopped backup, not a slow one.
    sev = CRIT if age_min > 180 else (WARN if age_min > 90 else OK)
    window = [r for r in recs if r["ts"] >= newest - 900]
    covered = len({r.get("slug") for r in window if r.get("slug")})
    kb = sum(int(r.get("bytes", 0)) for r in window) // 1024
    drills = [r.get("restore") for r in window if r.get("restore")]
    clones = sum(1 for d in drills if d == "clone-ok")

    out.append(row("sched", "Age of the last off-machine backup", f"{age_min} min", sev,
                   "newest ts in ~/.claude/state/estate-bundle-push.jsonl",
                   f"Last run covered {covered} repo(s), {kb} KB, of which {clones} were proved "
                   "by cloning the copy downloaded back out of R2. A receipt is written only "
                   "after the bytes come back and match, so this number cannot be produced by "
                   "a job that failed."
                   + ("" if sev == OK else
                      " The job is com.estate.bundlepush, StartInterval 3600. Its log is "
                      "~/.claude/state/logs/estate-bundlepush.out.log.")))

    out.append(row("sched", "Repos covered by the last backup run", str(covered),
                   OK if covered else CRIT,
                   "distinct slug in the last 15 minutes of estate-bundle-push.jsonl",
                   "Covers ~/.claude, ~/.maestro, ~/Documents/code, ~/dev/code and ~/code. "
                   "A repo appears here only when it carries a commit no reachable remote has."))

    refused = [r for r in window if r.get("outcome") == "refused-key-material"]
    if refused:
        out.append(row("secrets", "Repos refused by the backup for key material in history",
                       str(len(refused)), WARN,
                       'outcome == "refused-key-material" in estate-bundle-push.jsonl',
                       "Their history names a .env, a .pem or an ssh key by filename, so the "
                       "bundle was not uploaded. They are therefore NOT backed up."))

    sal = pathlib.Path.home() / ".claude/state/estate-worktree-cleanup.jsonl"
    if sal.exists() and sal.read_text().strip():
        try:
            last = json.loads(sal.read_text().splitlines()[-1])
            d = int((time.time() - last["ts"]) / 86400)
            out.append(row("sched", "Merged worktrees still on disk after the last cleanup",
                           str(last.get("kept", "?")), OK,
                           "last line of ~/.claude/state/estate-worktree-cleanup.jsonl",
                           f"{last.get('salvaged', '?')} merged worktree(s) were salvaged to "
                           f"R2 and retired {d} day(s) ago; the ones counted here are kept "
                           "because their branch is not in origin/main."))
        except Exception:                                    # noqa: BLE001
            pass
    return out


CHECKS.append(c_backup)


# ---------------------------------------------------------------- disaster recovery

# WHY THIS CHECK EXISTS. On 2026-08-23 the founder asked whether the estate could be flipped
# off Fly, and the honest answer took forty minutes to assemble by hand. Worse, part of the
# answer I gave him was wrong: I said the money ledger could not be pulled back out of R2. It
# could, and `backup_store.py --restore-money` had existed the whole time and restored
# 1,650,225 records when finally run.
#
# The scanner is `deploy/stack.sh`, which already exists and is already the two-angle probe
# (the platform is asked AND the service is asked). This is only the wire from it into the
# hourly audit, which estate_watch.py already reads and already sends to Telegram. Nothing
# new is scheduled and nothing new is delivered -- LAW 3, LAW 23, LAW 28 in that order.

PROSPECTOR_CANDIDATES = (
    os.environ.get("PROSPECTOR_REPO", ""),
    # A detached worktree pinned to origin/main, kept for this probe alone. The two working
    # checkouts on this laptop are both months behind main and both are running jobs, so
    # neither can be moved to get a script into place. `git worktree add --detach` costs a
    # tree and no risk. Its `.env` is a symlink into prospector-live, because stack.sh reads
    # the R2 credentials from the repo root and .env is not in git (LAW 21).
    #   git -C ~/dev/code/prospector-main fetch origin main && \
    #   git -C ~/dev/code/prospector-main checkout --detach origin/main
    # is the refresh. This path is listed FIRST of the real checkouts on purpose: main is the
    # only tree that is guaranteed to hold the current probe.
    str(pathlib.Path.home() / "dev/code/prospector-main"),
    str(pathlib.Path.home() / "Documents/code/prospector"),
    str(pathlib.Path.home() / "dev/code/prospector"),
    str(pathlib.Path.home() / "code/prospector"),
)


def _prospector_repo() -> str | None:
    """The first checkout that actually carries deploy/stack.sh.

    Deliberately not "the first directory that exists". The main checkout sat detached at an
    old commit for most of 2026-08-23, so a path test would have found a prospector with no
    stack.sh in it and this check would have graded the estate on a file it never ran.
    """
    # Refresh BEFORE the file test, not after. The first version of this ran the refresh once a
    # candidate had been accepted, which cannot work: the worktree is stale precisely when it is
    # sitting at a commit with no deploy/stack.sh in it, so the loop rejected the path and the
    # refresh that would have fixed it never ran.
    _refresh_pinned_worktree(str(pathlib.Path.home() / "dev/code/prospector-main"))
    for c in PROSPECTOR_CANDIDATES:
        if c and (pathlib.Path(c) / "deploy/stack.sh").is_file():
            return c
    return None


def _refresh_pinned_worktree(repo: str) -> None:
    """Keep the probe's own checkout on main, and only that one.

    ~/dev/code/prospector-main exists for this check and nothing else. Left alone it would sit
    at whatever commit it was created at, and an hourly probe running last month's script is
    the rot that makes a repository worse than no repository. A fetch and a detach cost one
    network call an hour and remove the class.

    Guarded three ways, because a probe that edits a working checkout is a much worse bug than
    a stale probe. It runs only on that exact path, only when git reports the tree clean, and
    it swallows every failure: a probe that cannot refresh still measures, and being unable to
    reach GitHub must never stop the estate being graded.
    """
    if pathlib.Path(repo).name != "prospector-main" or not pathlib.Path(repo, ".git").exists():
        return
    try:
        # --untracked-files=no on purpose. This worktree carries two untracked symlinks by
        # design -- .env and .venv, pointing into the running checkout, because stack.sh reads
        # the R2 credentials from the repo root and needs a python that has boto3. Counting
        # those as "dirty" is what stopped the refresh running at all.
        dirty = subprocess.run(["git", "-C", repo, "status", "--porcelain",
                                "--untracked-files=no"],
                               capture_output=True, text=True, timeout=15)
        if dirty.returncode != 0 or dirty.stdout.strip():
            return
        subprocess.run(["git", "-C", repo, "fetch", "--quiet", "origin", "main"],
                       capture_output=True, timeout=45)
        subprocess.run(["git", "-C", repo, "checkout", "--quiet", "--detach", "origin/main"],
                       capture_output=True, timeout=30)
    except Exception:                                        # noqa: BLE001
        return


def c_disaster_recovery() -> list[dict]:
    out: list[dict] = []
    repo = _prospector_repo()
    if repo is None:
        out.append(row("gap", "Copies of the money data that survive losing Fly", "UNMEASURED",
                       UNK, "no deploy/stack.sh in " + ", ".join(c for c in PROSPECTOR_CANDIDATES if c),
                       "Not graded. The prospector checkout on this machine does not carry "
                       "deploy/stack.sh, so nothing here says whether the backups are good. "
                       "It is not a pass."))
        return out

    # --- what copies exist, and how old ------------------------------------------------
    rc, txt = sh("bash deploy/stack.sh recover --json", timeout=120, cwd=repo)
    try:
        d = json.loads(txt)
    except Exception:                                        # noqa: BLE001
        out.append(row("gap", "Copies of the money data that survive losing Fly", "UNMEASURED",
                       UNK, f"deploy/stack.sh recover --json in {repo} (rc={rc})",
                       "Not graded: the inventory did not return JSON. It is not a pass. "
                       + txt[:300]))
        return out

    # MISSING and UNKNOWN are different facts and this check used to conflate them, so a laptop
    # that merely could not reach R2 was reported to the founder as an estate with no backups.
    # A false red costs the same as a false green in the end: both teach him to stop reading.
    missing = [r["what"] for r in d["rows"] if r["age"] == "MISSING"]
    unknown = [r["what"] for r in d["rows"] if r["age"] == "UNKNOWN"]
    stale = d.get("stale", [])
    # A missing copy is worse than a stale one: stale means the job stopped recently, missing
    # means there is nothing to restore from at all. Unknown is neither, and it is graded
    # unknown rather than critical, which is what the row helper already does for sentinels.
    if missing or stale:
        sev = CRIT
    elif unknown:
        sev = UNK
    else:
        sev = OK
    value = f"{len(d['rows']) - len(missing) - len(unknown)}/{len(d['rows'])}"
    detail = (
        "Every copy the estate has, with the command that restores it, is "
        "`deploy/stack.sh recover`. "
    )
    if missing:
        detail += "NOTHING TO RESTORE FROM for: " + ", ".join(missing) + ". "
    if unknown:
        detail += ("COULD NOT CHECK, so this is not a pass: " + ", ".join(unknown) + ". ")
    if stale:
        detail += (f"Older than {d['stale_after_hours']:.0f}h, so whatever writes them has "
                   "stopped: " + ", ".join(stale) + ". ")
    if not missing and not stale and not unknown:
        ages = ", ".join(f"{r['what']} {r['age']}" for r in d["rows"] if r["where"].startswith("r2:"))
        detail += "All off-machine copies are current: " + ages + "."
    # "backup" is not a declared domain, so this row rendered into no section at all:
    # the one row that says whether the money data survives losing Fly was invisible on
    # the page while still being counted. Its four sibling backup rows all use "sched".
    out.append(row("sched", "Copies of the money data that survive losing Fly", value, sev,
                   f"deploy/stack.sh recover --json in {repo}", detail))

    # --- can each component run anywhere ------------------------------------------------
    rc, txt = sh("bash deploy/stack.sh status --json", timeout=120, cwd=repo)
    try:
        st = json.loads(txt)["rows"]
    except Exception:                                        # noqa: BLE001
        out.append(row("gap", "Components with nowhere left to run", "UNMEASURED", UNK,
                       f"deploy/stack.sh status --json in {repo} (rc={rc})",
                       "Not graded: the probe did not return JSON. It is not a pass. " + txt[:300]))
        return out

    # A component is only in trouble when EVERY platform says it is not there. The engine
    # being down on the laptop while Fly serves is the normal state, not a finding, and a
    # check that reports it hourly is the noise that gets a channel muted.
    by_comp: dict[str, list[str]] = {}
    for r in st:
        by_comp.setdefault(r["component"], []).append(r["state"])
    nowhere = sorted(c for c, states in by_comp.items() if "UP" not in states)
    out.append(row("platform", "Components with nowhere left to run", str(len(nowhere)),
                   CRIT if nowhere else OK,
                   f"deploy/stack.sh status --json in {repo}",
                   ("Not serving on any platform: " + ", ".join(nowhere) + ". "
                    "`deploy/stack.sh status` shows which probe said what; "
                    "`deploy/cutover.sh --from fly --to laptop` moves the engine."
                    if nowhere else
                    "Every component answers on at least one platform: "
                    + ", ".join(sorted(by_comp)) + ".")))
    return out


CHECKS.append(c_disaster_recovery)


def c_founder_actions() -> list[dict]:
    """What is waiting on the founder, on the page he already has open.

    Authorisation used to be retail here. An agent stalled at a step only he could clear,
    spent a reply telling him about that one step, and the next agent did the same for a
    different one. Nothing counted them and nothing showed them side by side, so a single
    visit could never clear more than whatever he happened to be reading about.

    An item closes when a command says the world changed, so nobody has to remember to close
    it. An item no command can settle reads UNKNOWN, never CLEAN, because the honest answer
    to "did he back the key up" is that this machine cannot tell.
    """
    reg = subprocess.run([sys.executable, str(__import__("guards_root").GUARDS_ROOT / "founder_actions.py"),
                          "--json"], capture_output=True, text=True, timeout=45)
    if reg.returncode != 0 and not reg.stdout.strip():
        return [row("access", "Waiting on the founder", "UNKNOWN", UNK,
                    "founder_actions.py --json",
                    "cannot look: %s" % (reg.stderr.strip()[:200] or "no output"))]
    try:
        r = json.loads(reg.stdout)
    except json.JSONDecodeError as exc:
        return [row("access", "Waiting on the founder", "UNKNOWN", UNK,
                    "founder_actions.py --json", "unreadable register: %s" % exc)]

    out = []
    for g in r.get("open", []) + r.get("unverifiable", []):
        openish = g in r.get("open", [])
        detail = "%s Only him because: %s" % (g["what"], g["why_founder"])
        if g.get("unblocks"):
            detail += " It releases: %s" % g["unblocks"]
        detail += " Closes when: %s" % g["proof"]
        out.append(row("access", "Founder must clear: %s" % g["id"],
                       "WAITING" if openish else "CANNOT TELL",
                       WARN if openish else UNK,
                       g.get("source") or "founder_actions.py", detail))
    if not out:
        # An empty register and a deleted one both produce an empty list, and grading them the
        # same way is how this row goes green on a file somebody removed. The register is
        # gitignored runtime state, so losing it is a real path, not a hypothetical one.
        if not r.get("register_exists"):
            out.append(row("access", "Waiting on the founder", "UNKNOWN", UNK,
                           r.get("register", "founder_actions.py"),
                           "The register file is not there, so this is not a pass. Either "
                           "nothing has ever been added to it, or it was deleted and every "
                           "item in it went with it."))
        else:
            out.append(row("access", "Waiting on the founder", "0", OK,
                           "founder_actions.py --json",
                           "Nothing is blocked on an authorisation only he can give."))
    return out


CHECKS.append(c_founder_actions)

# ---------------------------------------------------------------- render

SEV_LABEL = {CRIT: "CRITICAL", WARN: "WARN", UNK: "UNKNOWN", OK: "CLEAN"}

CSS = """
:root{--paper:#F7F8FA;--card:#FFF;--ink:#15181D;--ink2:#3D4553;--ink3:#6B7486;--rule:#DFE3EA;
--rule2:#EDF0F4;--accent:#8C2F39;--soft:#F3E6E8;--crit:#A6202B;--critbg:#FAE9EA;--warn:#8A5B08;
--warnbg:#FBF0DC;--ok:#1F6146;--okbg:#E3F1EA;--unk:#4A5568;--unkbg:#EDF0F4}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--paper:#0E1116;--card:#161B22;
--ink:#E7EBF1;--ink2:#B4BECD;--ink3:#7F8B9C;--rule:#252C36;--rule2:#1C222B;--accent:#E0808C;
--soft:#2A1A1D;--crit:#F1878F;--critbg:#2E1618;--warn:#E0B354;--warnbg:#2B2113;--ok:#68C79B;
--okbg:#12271F;--unk:#9AA5B4;--unkbg:#1C222B}}
:root[data-theme=dark]{--paper:#0E1116;--card:#161B22;--ink:#E7EBF1;--ink2:#B4BECD;--ink3:#7F8B9C;
--rule:#252C36;--rule2:#1C222B;--accent:#E0808C;--soft:#2A1A1D;--crit:#F1878F;--critbg:#2E1618;
--warn:#E0B354;--warnbg:#2B2113;--ok:#68C79B;--okbg:#12271F;--unk:#9AA5B4;--unkbg:#1C222B}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 "Public Sans",-apple-system,
BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1060px;margin:0 auto;padding:44px 22px 90px}
code,.mono{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
header{border-bottom:2px solid var(--ink);padding-bottom:24px}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:700}
h1{font-family:"Newsreader",Georgia,serif;font-weight:600;font-size:clamp(36px,6vw,58px);
line-height:1.02;margin:12px 0 10px;letter-spacing:-.015em;text-wrap:balance}
.sub{color:var(--ink2);font-size:17px;max-width:64ch;margin:0}
.stamp{margin-top:16px;display:flex;flex-wrap:wrap;gap:6px 20px;font-size:12.5px;color:var(--ink3)}
.stamp b{color:var(--ink2);font-weight:500}
.age{margin:22px 0 0;padding:11px 15px;border-radius:3px;font-size:14px;font-weight:500}
.age.fresh{background:var(--okbg);color:var(--ok)}
.age.stale{background:var(--critbg);color:var(--crit)}
.tot{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:26px 0 0}
.tot div{background:var(--card);border:1px solid var(--rule);border-radius:3px;padding:12px 14px;border-left:3px solid var(--rule)}
.tot .n{font-family:"JetBrains Mono",monospace;font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1}
.tot .l{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);font-weight:700}
.tot .critical{border-left-color:var(--crit)}.tot .critical .n{color:var(--crit)}
.tot .warn{border-left-color:var(--warn)}.tot .warn .n{color:var(--warn)}
.tot .unknown{border-left-color:var(--unk)}.tot .unknown .n{color:var(--unk)}
.tot .ok{border-left-color:var(--ok)}.tot .ok .n{color:var(--ok)}
h2{font-family:"Newsreader",Georgia,serif;font-weight:600;font-size:26px;margin:50px 0 3px;
padding-top:20px;border-top:1px solid var(--rule);letter-spacing:-.01em}
h2 .n{color:var(--accent);font-family:"JetBrains Mono",monospace;font-size:13px;font-weight:700;
vertical-align:.45em;margin-right:9px}
.lede{color:var(--ink2);margin:0 0 16px;max-width:72ch;font-size:14.5px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14.5px}
th{text-align:left;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);
font-weight:700;padding:0 11px 7px;border-bottom:1px solid var(--rule)}
td{padding:11px;border-bottom:1px solid var(--rule2);vertical-align:top}
tr td:first-child{border-left:3px solid var(--rule)}
tr.critical td:first-child{border-left-color:var(--crit)}
tr.warn td:first-child{border-left-color:var(--warn)}
tr.ok td:first-child{border-left-color:var(--ok)}
tr.unknown td:first-child{border-left-color:var(--unk)}
td.v{font-family:"JetBrains Mono",monospace;font-variant-numeric:tabular-nums;white-space:nowrap;font-weight:700}
.sev{display:inline-block;font-size:9.5px;font-weight:700;letter-spacing:.09em;padding:2px 7px;
border-radius:2px;margin-right:7px;vertical-align:1px}
.sev.critical{background:var(--critbg);color:var(--crit)}
.sev.warn{background:var(--warnbg);color:var(--warn)}
.sev.ok{background:var(--okbg);color:var(--ok)}
.sev.unknown{background:var(--unkbg);color:var(--unk)}
.d{color:var(--ink2);font-size:13.5px;margin-top:4px}
.proof{display:block;margin-top:6px;font-family:"JetBrains Mono",monospace;font-size:11.5px;
color:var(--ink3);word-break:break-word}
.proof::before{content:"proof  ";color:var(--accent);font-weight:700}
footer{margin-top:60px;padding-top:20px;border-top:2px solid var(--ink);font-size:13px;color:var(--ink3)}
@media(max-width:640px){.wrap{padding:28px 14px 60px}table{font-size:13.5px}}
"""


def render_html(data: dict, stale_s: int = 3600) -> str:
    e = html.escape
    age = time.time() - data["generated_at"]
    stale = age > stale_s
    c = data["counts"]
    parts = ['<title>Estate Audit</title>',
             '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=Newsreader:opsz,wght@6..72,400;6..72,600&family=Public+Sans:wght@400;500;700'
             '&family=JetBrains+Mono:wght@400;700&display=swap">',
             f"<style>{CSS}</style>", '<div class="wrap">', "<header>",
             '<div class="eyebrow">Read-only &middot; nothing was changed to produce this</div>',
             "<h1>Estate Audit</h1>",
             '<p class="sub">Everything running in this estate, however small, with the command '
             'behind every number. No row is recalled from memory and no credential value appears '
             'anywhere on this page.</p>',
             '<div class="stamp">'
             f'<span><b>Generated</b> {e(data["generated_at_iso"])}</span>'
             f'<span><b>Build time</b> {data["duration_s"]}s</span>'
             f'<span><b>Checks</b> {len(data["rows"])} rows across {len(DOMAINS)} domains</span>'
             "</div>",
             f'<div class="age {"stale" if stale else "fresh"}">'
             + (f"STALE &mdash; this page is {int(age / 60)} minutes old and the builder has missed a run. "
                "Do not audit from it."
                if stale else f"Fresh &mdash; measured {int(age / 60)} minutes ago.")
             + "</div>",
             '<div class="tot">'
             + "".join(f'<div class="{s}"><div class="n">{c.get(s, 0)}</div>'
                       f'<div class="l">{SEV_LABEL[s]}</div></div>' for s in (CRIT, WARN, UNK, OK))
             + "</div></header>"]
    for i, (key, title, lede) in enumerate(DOMAINS, 1):
        rows = [r for r in data["rows"] if r["domain"] == key]
        if not rows:
            continue
        parts.append(f'<h2><span class="n">{i:02d}</span>{e(title)}</h2>'
                     f'<p class="lede">{e(lede)}</p><div class="scroll"><table>'
                     "<tr><th>Finding</th><th>Measured</th></tr>")
        for r in rows:
            sev = r["severity"]
            parts.append(
                f'<tr class="{sev}"><td><span class="sev {sev}">{SEV_LABEL[sev]}</span>'
                f'<b>{e(r["title"])}</b>'
                + (f'<div class="d">{e(r["detail"])}</div>' if r["detail"] else "")
                + f'<span class="proof">{e(r["proof"])}</span></td>'
                f'<td class="v">{e(r["value"])}</td></tr>')
        parts.append("</table></div>")
    parts.append(
        '<footer><b>Read-only.</b> Nothing on this machine, in any repository, or on any hosted '
        'service was modified to produce this page. Credential findings carry file, line, prefix '
        'and length only &mdash; no value is reproduced.<br><br>'
        '<b>A row marked UNKNOWN is a real result.</b> It means the check ran and could not get an '
        'answer, or that nothing in the estate measures that thing at all. It is never a guess and '
        f'never an omission.<br><br>Built by <code>estate_audit.py</code> in {data["duration_s"]}s. '
        'Serving is a file read, which is why the page itself is instant.</footer></div>')
    return "\n".join(parts)


def _atomic(path: str | pathlib.Path, text: str) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def selftest() -> int:
    fails = []
    data = collect()
    if not data["rows"]:
        fails.append("collect() returned no rows")

    # A dead command must never reach the page as a grade. This is the exact defect that put
    # "CLEAN -- laws injector selftest: __timeout__ after 12s" in front of the founder.
    for value, detail, claimed in (("__timeout__ after 12s", "", OK),
                                   ("3", "cached under __timeout__ after 12s", OK),
                                   ("__error__ ValueError: boom", "", CRIT)):
        r = row("agent", "probe", value, claimed, "proof", detail)
        if r["severity"] != UNK:
            fails.append(f"an unmeasured check rendered as {r['severity']}, not unknown")
        if any(t in f"{r['value']} {r['detail']}" for t in UNMEASURED):
            fails.append("the timeout sentinel leaked into a rendered field")
    for r in data["rows"]:
        if any(t in f"{r['value']} {r['detail']}" for t in UNMEASURED):
            fails.append(f"sentinel reached a live row: {r['title']!r}")
    if data["duration_s"] > 60:
        # Two very different faults produce one number, so name which one it was. On
        # 2026-08-21 the build took 79.5s at load 431 while a commit gate held 7 of 12
        # cores; the same code takes 21.7s on a quiet machine.
        try:
            load1 = os.getloadavg()[0]
        except OSError:
            load1 = -1.0
        cause = ("the machine is saturated" if load1 > 2 * os.cpu_count()
                 else "the checks themselves are too slow")
        fails.append(f"build took {data['duration_s']}s -- too slow to schedule "
                     f"({cause}; 1-min load {load1:.1f} on {os.cpu_count()} cores)")
    for r in data["rows"]:
        for k in ("domain", "title", "value", "severity", "proof"):
            if not r.get(k):
                fails.append(f"row missing {k}: {r.get('title', r)!r}")
        if r["severity"] not in SEV_LABEL:
            fails.append(f"bad severity {r['severity']!r}")
        if r["domain"] not in {d[0] for d in DOMAINS}:
            fails.append(f"row in unknown domain {r['domain']!r}")
    page = render_html(data)
    for bad in (r"sk_live_[A-Za-z0-9]{12}", r"sk-ant-api[A-Za-z0-9_-]{12}", r"hf_[A-Za-z0-9]{20}"):
        if re.search(bad, page):
            fails.append(f"A CREDENTIAL VALUE LEAKED INTO THE PAGE: /{bad}/")
    if "<title>" not in page or "Estate Audit" not in page:
        fails.append("rendered page has no title")
    old = dict(data)
    old["generated_at"] = time.time() - 7200
    if "STALE" not in render_html(old):
        fails.append("a two-hour-old build did not render as STALE")
    if "STALE" in render_html(data):
        fails.append("a fresh build rendered as STALE")
    # LAW 38: a guard is not finished when it refuses the bad case -- that was never
    # in doubt. It is finished when it has been SHOWN to allow the good one. Both
    # directions, one fixture, so a future edit cannot quietly turn either off.
    fixture = [
        ("com.chidionyema.maestro",      "79018", "-15"),       # alive, exit is history
        ("ai.architect.gateway",         "60261", "1"),         # alive, exit is history
        ("com.prospector.launchd-held",  "-",     "1"),         # declared report job
        ("com.valvesoftware.steamclean", "-",     "78"),        # not ours to fix
        ("com.founder.board",            "-",     "0"),         # exited clean
        ("com.founder.parked",           "-",     "notloaded"),
        ("com.estate.somethingnew",      "-",     "1"),         # UNDECLARED -> crash
        ("com.estate.killed",            "-",     "-9"),        # signal -> crash
        ("com.prospector.launchd-held",  "-",     "-9"),        # declared, wrong code -> crash
    ]
    want = {
        "running":   {"com.chidionyema.maestro", "ai.architect.gateway"},
        "findings":  {"com.prospector.launchd-held"},
        "foreign":   {"com.valvesoftware.steamclean(78)"},
        "notloaded": {"com.founder.parked"},
        "crashed":   {"com.estate.somethingnew(1)", "com.estate.killed(-9)",
                      "com.prospector.launchd-held(-9)"},
    }
    got = grade_launchd(fixture)
    for bucket, expected in want.items():
        if set(got[bucket]) != expected:
            fails.append(f"grade_launchd {bucket}: got {sorted(set(got[bucket]))}, "
                         f"want {sorted(expected)}")

    if fails:
        print("selftest FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"PASS: {len(data['rows'])} rows in {data['duration_s']}s, "
          f"{data['counts'][CRIT]} critical; no credential value in the page; "
          "staleness renders in both directions; a live job is not counted failed "
          "and an undeclared non-zero exit still is.")
    return 0


def c_hook_router() -> list[dict]:
    """crew#326: ~/.estate/guards/hooks/_router is every git hook on this machine. Twice
    (2026-08-23 22:04, 2026-08-26 17:06) it stopped dispatching on the hook name and every
    commit and push in every repo was refused. router-selftest proves it both ways."""
    selftest = pathlib.Path(os.environ.get("ESTATE_HOME", str(HOME / ".estate"))) / "guards" / "bin" / "router-selftest"
    proof = str(selftest)
    if not selftest.exists():
        return [row("agent", "git hook router dispatches per name", "NO SELFTEST", UNK, proof, str(selftest))]
    rc, out = sh(f"'{selftest}'", timeout=60)
    last = out.strip().splitlines()[-1] if out.strip() else "(no output)"
    if any(s in out for s in UNMEASURED):
        return [row("agent", "git hook router dispatches per name", out[:40], UNK, proof, out[-200:])]
    return [row("agent", "git hook router dispatches per name", "OK" if rc == 0 else "BROKEN",
                OK if rc == 0 else CRIT, proof, last[:200])]


CHECKS.append(c_hook_router)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", nargs="?", const=str(DEFAULT_HTML))
    ap.add_argument("--state", nargs="?", const=str(DEFAULT_JSON))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--stale-seconds", type=int, default=3600)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    data = collect()
    if a.json:
        print(json.dumps(data, indent=2))
        return 0
    html_path = a.html or str(DEFAULT_HTML)
    json_path = a.state or str(DEFAULT_JSON)
    _atomic(json_path, json.dumps(data, indent=2))
    _atomic(html_path, render_html(data, a.stale_seconds))
    c = data["counts"]
    print(f"wrote {html_path} and {json_path} in {data['duration_s']}s -- "
          f"{c[CRIT]} critical, {c[WARN]} warn, {c[UNK]} unknown, {c[OK]} clean", file=sys.stderr)
    # Exit 0 whenever a page was produced, criticals included. The criticals are this
    # report's content, not its failure. launchd stores one number per job, so an audit
    # that correctly found three critical things was indistinguishable from an audit that
    # had crashed and found nothing, and both read as LastExitStatus = 256 for weeks. A
    # non-zero exit from here now means one thing: no page.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

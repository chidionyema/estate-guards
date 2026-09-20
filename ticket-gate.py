#!/usr/bin/env python3
"""Every session carries a GitHub issue, or it does not get to change anything.

Founder, 2026-08-23: "i tnothin no ework should ever be done without a ticket and it should be
autonated", "is should be inpossi ble to work without a ticket", "that is a github issue",
"because i an pronting different agent sessions", "so nay things get started and not folloed up".

The failure this closes, measured the same day: he asked a session to get aiden operational, no
issue was ever opened, and by the time he asked again nobody could say which tab held it. Aiden
was in fact running the whole time. The work was fine. The thread was gone.

HOW IT WORKS, and why it never costs him or an agent a single command:

  1. A session's first mutating tool call arrives with no ticket bound.
  2. This hook does NOT ask anyone for one. It reads the founder's own first words out of that
     session's transcript, spawns a detached child that opens the issue, and lets the call
     through. The hook itself returns in milliseconds and never touches the network.
  3. The child writes ~/.claude/state/tickets/<session>.json. Every later call reads that file.
  4. If the child failed, the NEXT mutating call is refused with exit 2. That is the "impossible"
     part, and it lands on the agent, never on him.

Fail-open everywhere except that one case. A guard that wedges fifteen live sessions because
GitHub was slow is a worse defect than the one it prevents.

Modes:
  (no args)      PreToolUse hook. Reads the payload on stdin.
  --roster       Print every live session with its ticket. What aiden puts on his phone.
  --selftest     Prove the decision table without touching the network.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
TICKETS = os.path.join(HOME, ".claude", "state", "tickets")
REPO = "chidionyema/crew"


def _gh() -> str:
    """The absolute path to gh, because launchd does not run with a shell's PATH.

    Measured 2026-08-23 22:30: the first unattended close sweep returned
    `FileNotFoundError: ... 'gh'` and closed nothing. In a terminal the same code closed issue 46.
    A tool named as a bare word works for every agent that tests it by hand and fails for the
    scheduler that is supposed to run it, which is the exact gap between written and operational.
    """
    for path in ("/usr/local/bin/gh", "/opt/homebrew/bin/gh", "/usr/bin/gh"):
        if os.path.exists(path):
            return path
    return "gh"


GH = _gh()

#: What a new ticket is budgeted at until somebody sets a real number. Measured 2026-08-23 across
#: the 6 sessions currently bound to an issue: median $558, mean $492, max $795, $2,954 in total.
#: The default below is deliberately far under that median. A default set at the observed median
#: blesses whatever the estate happens to be spending and can never detect anything, which makes
#: it decoration rather than a budget. Set low, it is wrong often -- and being wrong is visible on
#: close, which is the behaviour wanted.
DEFAULT_BUDGET_USD = 50
DEFAULT_BUDGET_MIN = 120

#: Tools that change the world. A read never needs a ticket: he is not trying to stop an agent
#: from looking at a file, he is trying to stop work from happening off the board.
MUTATING = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

#: A Bash command only needs a ticket if it writes. Grepping for a symbol is reading.
#: The redirect alternative sits outside the \b group on purpose: > is not a word character, so a
#: leading \b can never match in front of it and the whole branch would be dead.
BASH_WRITES = re.compile(
    r"\b(git (commit|push|merge|rebase|reset|checkout -b)|gh (pr|issue|release)"
    r"|fly (deploy|secrets|scale)|rm |mv |cp |tee |chmod |chown |launchctl "
    r"|pip install|npm install)"
    r"|>>?\s*[^\s|&]",
)

#: How long a creation attempt may be in flight before the next call stops waiting for it and
#: refuses. Long enough for a slow `gh`, short enough that a dead child cannot hold the gate open.
INFLIGHT_S = 90


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def _needs_ticket(tool: str, tool_input: dict) -> bool:
    if tool in MUTATING:
        return True
    if tool == "Bash":
        return bool(BASH_WRITES.search(tool_input.get("command", "")))
    return False


# Text the harness puts in a user row that the founder never typed. The compaction summary
# opened crew#323 as a ticket titled "This session is being continued..." (2026-08-26).
NOT_FOUNDER_WORDS = (
    "Caveat:",
    #: liveness probes sent to a session by a monitor; crew#334-#337 were four issues titled with one
    "Answer with one word and nothing else",
    "Stop hook",
    "This session is being continued from a previous conversation",
)


def founder_words(transcript: str) -> str:
    """The first thing the founder actually typed in this session. Hook text arrives with
    role=user too, so it is excluded by shape, not by trusting the role field."""
    try:
        with open(transcript, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"user"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("isSidechain"):
                    continue
                msg = row.get("message") or {}
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content or []
                    if isinstance(b, dict) and b.get("type") == "text")
                text = (text or "").strip()
                if not text or text[0] in "<[" or text.startswith(NOT_FOUNDER_WORDS):
                    continue
                return " ".join(text.split())[:180]
    except OSError:
        pass
    return ""


def bind_path(sid: str) -> str:
    return os.path.join(TICKETS, "%s.json" % re.sub(r"[^A-Za-z0-9_-]", "", sid)[:64])


def read_bind(sid: str) -> dict | None:
    try:
        with open(bind_path(sid), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def write_bind(sid: str, data: dict) -> None:
    os.makedirs(TICKETS, exist_ok=True)
    tmp = bind_path(sid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, bind_path(sid))



#: aiden/tick.py loads this file by path (spec_from_file_location), so this file's own
#: directory is not on sys.path there and `issue_dod` raised ModuleNotFoundError on every
#: ai.aiden.watch tick from 2026-08-27 (crew#527 CP4 landed) until the breaker opened.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from issue_dod import issue_body, lane_for  # noqa: E402  crew#527 CP4


def open_issue(sid: str, words: str, cwd: str) -> int:
    """Runs in the detached child. Never in the hook path."""
    title = words or "Untitled session in %s" % os.path.basename(cwd)
    if len(title) > 90:
        title = title[:87] + "..."
    body = issue_body(words, cwd, sid, DEFAULT_BUDGET_USD, DEFAULT_BUDGET_MIN)
    res = subprocess.run(
        [GH, "issue", "create", "--repo", REPO, "--title", title,
         "--body", body, "--label", "triage", "--label", lane_for(cwd)],
        capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        write_bind(sid, {"issue": 0, "error": (res.stderr or "")[-300:], "at": time.time(),
                         "cwd": cwd, "words": words})
        return 1
    num = 0
    m = re.search(r"/issues/(\d+)", res.stdout or "")
    if m:
        num = int(m.group(1))
    write_bind(sid, {"issue": num, "title": title, "repo": REPO, "at": time.time(),
                     "cwd": cwd, "words": words, "url": (res.stdout or "").strip()})
    return 0


def hook() -> int:
    payload = _read_payload()
    tool = payload.get("tool_name") or ""
    if not _needs_ticket(tool, payload.get("tool_input") or {}):
        return 0
    sid = payload.get("session_id") or ""
    if not sid:
        return 0                                    # cannot identify the session: never block
    cwd = payload.get("cwd") or os.getcwd()

    bound = read_bind(sid)
    if bound and bound.get("issue"):
        return 0                                    # has a ticket, carry on

    if bound and bound.get("error"):
        sys.stderr.write(
            "TICKET GATE: this session has no GitHub issue and opening one failed.\n"
            "  %s\n"
            "No work happens off the board (founder, 2026-08-23: \"it should be impossible to "
            "work without a ticket\").\n"
            "Open one, then retry:  crew-triage  (or gh issue create --repo %s)\n"
            % (bound["error"], REPO))
        return 2

    marker = bind_path(sid) + ".inflight"
    try:
        age = time.time() - os.path.getmtime(marker)
    except OSError:
        age = None

    if age is not None and age < INFLIGHT_S:
        return 0                                    # a child is opening it right now

    os.makedirs(TICKETS, exist_ok=True)
    with open(marker, "w") as fh:
        fh.write(str(time.time()))
    words = founder_words(payload.get("transcript_path") or "")
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--create", sid, cwd, words],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return 0                                        # first call always passes


DASHBOARD = os.path.join(HOME, ".claude", "state", "ops-dashboard.html")


def _observe():
    """Aiden's observe module, one instance per process.

    sys.modules first, and that lookup is the point. Inside a tick, aiden has already walked
    ~/.claude/projects and its rows are still in observe's own `_CACHE`; taking the same instance
    turns the ops page's walk into a dictionary read. `module_from_spec` registers nothing, so a
    second loader silently gets a second copy with a second empty cache and pays the full pass
    again -- measured 2026-08-23 under /usr/bin/python3, 9.8s cold and 0.0s on the cached call.
    That duplicate is what pushed the 20:40 and 20:52 ticks past their 240s deadline.
    """
    mod = sys.modules.get("aiden_observe")
    if mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aiden_observe", os.path.join(HOME, ".claude", "scripts", "aiden", "observe.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["aiden_observe"] = mod
        spec.loader.exec_module(mod)
    return mod


def last_words(path: str) -> str:
    """The final thing the agent said in this session. Its own status line, not an inference."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 400_000))
            tail = fh.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return ""
    for line in reversed(tail):
        if '"assistant"' not in line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("isSidechain"):
            continue
        msg = row.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        text = " ".join(b.get("text", "") for b in msg.get("content") or []
                        if isinstance(b, dict) and b.get("type") == "text").strip()
        if text:
            return " ".join(text.split())
    return ""


MOVEMENT = os.path.join(HOME, ".claude", "state", "ticket-movement.json")


def movement() -> dict:
    """Every ticket and when it last moved. Founder, 2026-08-23: "i should be seeing ticketss
    noving not asking what are you doing".

    A ticket moves when GitHub's own updatedAt changes: a comment, a label, a close, a commit
    that references it. That is the estate's record of work, not an agent's claim about it.

    The last good answer is kept on disk. When `gh` fails the page still renders, and it says
    how old the numbers are rather than showing an empty board, because an empty board reads as
    "no work" and that is the one wrong thing it could say.
    """
    try:
        raw = subprocess.run(
            [GH, "issue", "list", "--repo", REPO, "--state", "all", "--limit", "80",
             "--json", "number,title,state,updatedAt,createdAt,labels"],
            capture_output=True, text=True, timeout=45)
        items = json.loads(raw.stdout) if raw.returncode == 0 else None
    except Exception:
        items = None
    if items is None:
        try:
            with open(MOVEMENT, encoding="utf-8") as fh:
                stale = json.load(fh)
            stale["stale"] = True
            return stale
        except Exception:
            return {"items": [], "at": 0, "stale": True}
    data = {"items": items, "at": time.time(), "stale": False}
    tmp = MOVEMENT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, MOVEMENT)
    return data


def _age_h(stamp: str) -> float:
    """Hours since an ISO8601 GitHub timestamp. Returns a large number when it cannot be read,
    so an unparseable date sorts as stuck rather than as fresh."""
    try:
        t = time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return 9e9
    return (time.time() - calendar.timegm(t)) / 3600.0


def counts(items: list[dict]) -> dict:
    """The three numbers he actually reads: what is open, what moved today, what is stuck."""
    open_ = [i for i in items if i.get("state") == "OPEN"]
    return {
        "open": len(open_),
        "moved_24h": len([i for i in items if _age_h(i.get("updatedAt", "")) < 24]),
        "closed_24h": len([i for i in items if i.get("state") == "CLOSED"
                           and _age_h(i.get("updatedAt", "")) < 24]),
        "stuck": len([i for i in open_ if _age_h(i.get("updatedAt", "")) >= 24]),
    }


#: An acceptance criterion lives in the issue body, under this heading, one per bullet, with the
#: command in backticks. Anything else under the heading is prose and is ignored.
#:
#:     ## Done when
#:     - [ ] `curl -sf http://127.0.0.1:8787/ops`
#:     - [ ] `python3 ~/.claude/scripts/ticket-gate.py --selftest`
DONE_WHEN = re.compile(r"^\s*#{1,4}\s*done when\s*$", re.I)
AC_LINE = re.compile(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?`([^`]+)`\s*$")

#: Commands an acceptance criterion may not contain. A criterion is a check, and a check reads.
#:
#: This is a trust boundary and it is worth naming rather than hiding. The body of a GitHub issue
#: is editable by anyone with write access to the repository, and this function takes text from
#: there and hands it to a shell. `chidionyema/crew` is private and only the founder and his own
#: agents can write to it, so the exposure is small -- but "small" is not "none", and the estate
#: has already been bitten once by an allow-list with a silent miss case. So: a deny-list on the
#: shapes that destroy, publish or move money, a hard timeout on every command, and every command
#: written to a log before it runs. A criterion that trips the list does not merely fail -- the
#: issue is left open and the reason is posted, so a criterion nobody can run is visible rather
#: than quietly treated as unmet.
AC_REFUSED = (
    "rm -r", "rm -f", "mkfs", "dd if=", "shutdown", "reboot", "killall", "sudo ",
    "> /dev/", "curl -X POST", "fly deploy", "git push", "gh issue close", "gh issue edit",
    "gh pr merge", "| sh", "|sh", "| bash", "|bash", "chmod 777", "launchctl unload",
)
AC_TIMEOUT = 20          # per criterion
AC_BUDGET = 60           # per sweep, wall clock, so a slow check never delays his message
AC_LOG = os.path.join(HOME, ".claude", "state", "ticket-close.log")


def acceptance(body: str) -> list:
    """The commands an issue says would prove it finished, in the order it lists them.

    Returns [] when the issue has no `## Done when` block, and an issue with no criteria can
    never close. That is the point rather than a gap: 27 issues were open on 2026-08-23 with 0
    closed in 24 hours, and 21 of them had no comment and no stated finish line. An issue with
    nothing to check stays open and stays visible, which is the pressure that gets a finish line
    written. Closing it on an agent's say-so is the failure this whole file exists to stop.
    """
    out, inside = [], False
    for line in (body or "").splitlines():
        if DONE_WHEN.match(line):
            inside = True
            continue
        if inside:
            if line.strip().startswith("#"):        # the next heading ends the block
                break
            m = AC_LINE.match(line)
            if m:
                out.append(m.group(1).strip())
    return out


#: A budget lives in the issue body under this heading, same shape as the criteria above.
#:
#:     ## Budget
#:     - cost: $25
#:     - time: 90m
#:
#: Founder, 2026-08-23: "budgeting and estinates before starting a piece of work and then figuring
#: out ruthlessig how to optinise the spped ofdelivery and the cost ruthlessly ... soa budget per
#: ticket", and "we need to be rthlessly effcien to prevent driiftig".
#:
#: The second quote is the one that sets the design. A budget here is not an accounting exercise,
#: it is a drift detector: an agent four hops off the named job spends money at the same rate as
#: one on it, and the spend is the only signal that shows up before the founder notices. So the
#: number that matters is not the estimate, it is the estimate compared with what was actually
#: taken -- which is why this is worth building only because the actual is measurable. It is:
#: every turn of every session records its own token usage, and every session is bound to an
#: issue by this same file.
BUDGET_HEAD = re.compile(r"^\s*#{1,4}\s*budget\s*$", re.I)
BUDGET_LINE = re.compile(r"^\s*[-*]\s*(cost|time)\s*:\s*(.+?)\s*$", re.I)

#: Opus 5 list prices, dollars per million tokens. A cache read is a tenth the price of a fresh
#: input token, which is the whole reason resident context is re-billed rather than free -- and
#: the reason a long session is expensive in a way that is invisible turn by turn.
PRICES = {"input_tokens": 15.0, "output_tokens": 75.0,
          "cache_creation_input_tokens": 18.75, "cache_read_input_tokens": 1.50}


def budget(body: str) -> dict:
    """The cost and time an issue was expected to take. {} when it never said.

    An issue with no budget is not refused. It is counted, and the count goes on the page, because
    the estate has 27 open issues and refusing all of them would stop the work rather than measure
    it. Refusal is what LAW 33's `## Done when` block earns; a budget earns visibility first.
    """
    out, inside = {}, False
    for line in (body or "").splitlines():
        if BUDGET_HEAD.match(line):
            inside = True
            continue
        if inside:
            if line.strip().startswith("#"):
                break
            m = BUDGET_LINE.match(line)
            if not m:
                continue
            key, raw = m.group(1).lower(), m.group(2).strip().strip("`")
            if key == "cost":
                num = re.search(r"[\d.]+", raw)
                if num:
                    out["cost"] = float(num.group(0))
            else:
                num = re.search(r"([\d.]+)\s*([hm])", raw, re.I)
                if num:
                    v = float(num.group(1))
                    out["time_min"] = v * 60 if num.group(2).lower() == "h" else v
    return out


def session_cost(sid: str) -> tuple:
    """(dollars, turns) actually spent by one session, read from its own transcript.

    This is the number nobody has been able to see. It is not an estimate and not a sample: every
    assistant turn writes its own `usage` block, so the sum is what the session cost. The scan
    skips any line without the word `usage` before parsing it -- measured necessary, because
    parsing every line of every transcript took over two minutes and timed out.
    """
    tot, turns = {}, 0
    for path in [os.path.join(HOME, ".claude", "projects", d, "%s.jsonl" % sid)
                 for d in os.listdir(os.path.join(HOME, ".claude", "projects"))
                 if os.path.isdir(os.path.join(HOME, ".claude", "projects", d))]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    use = ((row.get("message") or {}).get("usage")) or row.get("usage")
                    if not use:
                        continue
                    turns += 1
                    for k, v in use.items():
                        if isinstance(v, int):
                            tot[k] = tot.get(k, 0) + v
        except OSError:
            continue
    return sum(tot.get(k, 0) * p / 1e6 for k, p in PRICES.items()), turns


def issue_actuals(num: int) -> dict:
    """What an issue actually cost: every session bound to it, summed.

    Returns {} when no session was ever bound, which is the honest answer for an issue a person
    typed by hand rather than one a session opened by working.
    """
    cost, turns, sessions = 0.0, 0, 0
    try:
        names = os.listdir(TICKETS)
    except OSError:
        return {}
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(TICKETS, name), encoding="utf-8") as fh:
                bind = json.load(fh)
        except Exception:
            continue
        if bind.get("issue") != num:
            continue
        c, t = session_cost(name[:-5])
        cost += c
        turns += t
        sessions += 1
    return {"cost": cost, "turns": turns, "sessions": sessions} if sessions else {}


SWEEP_STAMP = os.path.join(HOME, ".claude", "state", "aiden-close-sweep.json")


def spend_summary() -> str:
    """One line for the page: what the ticketed work has cost, and how much of it was budgeted.

    Never raises. This runs inside the dashboard, and a page that fails to render because a cost
    could not be worked out is worse than a page with one line missing.
    """
    try:
        total, sessions = 0.0, 0
        for name in os.listdir(TICKETS):
            if not name.endswith(".json"):
                continue
            cost, turns = session_cost(name[:-5])
            if turns:
                total += cost
                sessions += 1
        line = "$%.0f of token value across %d ticketed session(s)" % (total, sessions)
        try:
            with open(SWEEP_STAMP, encoding="utf-8") as fh:
                sw = json.load(fh)
            unbudgeted = sw.get("no_budget")
            if unbudgeted is not None:
                line += " · %d open ticket(s) with no budget" % unbudgeted
            if sw.get("error"):
                line += " · last sweep failed"
        except Exception:
            line += " · no close sweep has run yet"
        return line + " · list prices, not a bill"
    except Exception as exc:                      # noqa: BLE001 - the page still renders
        return "spend not measured: %s" % type(exc).__name__


def budget_line(num: int, body: str) -> str:
    """One line comparing what an issue was budgeted against what it took. "" when neither is known.

    Both halves have to be present for this to say anything useful, and saying nothing is better
    than printing a spend with no yardstick -- a bare dollar figure reads as either fine or awful
    depending on the reader's mood, which is not a measurement.
    """
    est, act = budget(body), issue_actuals(num)
    if not est and not act:
        return ""
    parts = []
    if act:
        parts.append("spent $%.2f over %d turns in %d session(s)"
                     % (act["cost"], act["turns"], act["sessions"]))
    if est.get("cost"):
        over = act.get("cost", 0) - est["cost"]
        parts.append("budget $%.2f (%s$%.2f)"
                     % (est["cost"], "+" if over >= 0 else "-", abs(over)) if act
                     else "budget $%.2f" % est["cost"])
    elif act:
        parts.append("no budget was set, so there is nothing to compare it against")
    return "  ".join(parts)


def run_ac(cmd: str) -> tuple:
    """Run one acceptance criterion. Returns (ok, one-line receipt).

    Never raises. A criterion that times out, is refused, or blows up is a criterion that did not
    pass, and the issue stays open -- the safe direction. The opposite default would close an
    issue because its check crashed, which is how a board becomes a lie.
    """
    low = cmd.lower()
    for bad in AC_REFUSED:
        if bad in low:
            return False, "refused (contains %r): a criterion is a check, and a check reads" % bad
    try:
        with open(AC_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s RUN %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), cmd))
    except OSError:
        pass
    try:
        p = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=AC_TIMEOUT, cwd=HOME)
    except subprocess.TimeoutExpired:
        return False, "timed out after %ds" % AC_TIMEOUT
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    tail = " ".join((p.stdout or p.stderr or "").split())[:160]
    return p.returncode == 0, "exit %d  %s" % (p.returncode, tail or "(no output)")


def verify_body(body: str) -> tuple:
    """(verdict, receipts) for one issue body. verdict is True only when every criterion passed.

    Split out from the sweep so the controls below can run it on a literal string, with no
    network and no GitHub. A closer that can only be tested against live issues is a closer
    nobody tests.
    """
    acs = acceptance(body)
    if not acs:
        return None, []                     # None means "no finish line", not "failed"
    receipts, ok_all = [], True
    for cmd in acs:
        ok, msg = run_ac(cmd)
        ok_all = ok_all and ok
        receipts.append("%s `%s`\n    %s" % ("PASS" if ok else "FAIL", cmd, msg))
    return ok_all, receipts


def close_sweep(limit: int = 25) -> dict:
    """Close every open issue whose acceptance criteria all pass. Report what it did.

    Founder, 2026-08-23, on being shown 27 open and 0 closed: "thos os the ot that atters". The
    board was only ever opening work. Nothing on this machine closed a crew issue -- the eight
    closes on 08-22 were typed by hand, and `close-guard.py` grades the text of a reply and says
    in its own docstring that it deliberately does not check whether the goal is finished.

    So the rule here is the narrow one that can actually be trusted: an issue closes when the
    commands it named exit 0. Not when an agent believes it is done. That distinction is the
    whole value, because an agent's judgement of its own work is the one measurement this estate
    has already found to be worthless.

    Runs after delivery in the tick and inside a wall-clock budget, so a slow check delays
    nothing he is waiting for.
    """
    started = time.time()
    done = {"checked": 0, "closed": [], "failed": [], "no_criteria": 0, "no_budget": 0,
            "budget_hit": False}
    try:
        raw = subprocess.run(
            [GH, "issue", "list", "--repo", REPO, "--state", "open", "--limit", str(limit),
             "--json", "number,title,body"],
            capture_output=True, text=True, timeout=45)
        if raw.returncode != 0:
            return dict(done, error=(raw.stderr or "gh failed").strip()[:120])
        items = json.loads(raw.stdout)
    except Exception as exc:
        return dict(done, error="%s: %s" % (type(exc).__name__, exc))

    for it in items:
        if time.time() - started > AC_BUDGET:
            done["budget_hit"] = True
            break
        num = it.get("number")
        body = it.get("body") or ""
        if not budget(body):
            done["no_budget"] += 1
        verdict, receipts = verify_body(body)
        if verdict is None:
            done["no_criteria"] += 1
            continue
        done["checked"] += 1
        if not verdict:
            done["failed"].append(num)
            continue
        spend = budget_line(num, body)
        note = ("Closed by the acceptance criteria on this issue, not by an agent saying so.\n\n"
                + "\n".join(receipts)
                + (("\n\nCost: " + spend) if spend else "")
                + "\n\nRan at %s by ticket-gate close_sweep." % time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        try:
            subprocess.run([GH, "issue", "close", str(num), "--repo", REPO, "--comment", note],
                           capture_output=True, text=True, timeout=45)
            done["closed"].append(num)
        except Exception:
            done["failed"].append(num)
    return done


def headline() -> str:
    """One line for Telegram. Ticket movement, not a question about what an agent is doing."""
    data = movement()
    c = counts(data.get("items") or [])
    tail = " (numbers are stale, gh did not answer)" if data.get("stale") else ""
    return ("TICKETS  %d open, %d moved in 24h, %d closed, %d stuck >24h%s\n"
            "         http://127.0.0.1:8787/ops" % (
                c["open"], c["moved_24h"], c["closed_24h"], c["stuck"], tail))


SETTINGS = os.path.join(HOME, ".claude", "settings.json")
HOOK_CMD = "python3 $HOME/Documents/code/claude-guards/ticket-gate.py"
MATCHERS = ("Edit|Write|MultiEdit|NotebookEdit", "Bash")


def switch(on: bool) -> int:
    """The off switch LAW 32 requires: one command, takes effect on the next tool call in every
    session, no restart. Kept here rather than in a README so it cannot drift from the wiring."""
    with open(SETTINGS, encoding="utf-8") as fh:
        cfg = json.load(fh)
    with open(SETTINGS + ".bak-%d" % time.time(), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    pre = cfg.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for m in MATCHERS:
        grp = next((g for g in pre if g.get("matcher") == m), None)
        if on:
            if grp is None:
                grp = {"matcher": m, "hooks": []}
                pre.append(grp)
            if not any(h.get("command") == HOOK_CMD for h in grp["hooks"]):
                grp["hooks"].append({"type": "command", "command": HOOK_CMD})
        elif grp is not None:
            grp["hooks"] = [h for h in grp.get("hooks", [])
                            if h.get("command") != HOOK_CMD]
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, SETTINGS)
    live = [g.get("matcher") for g in pre
            if any(h.get("command") == HOOK_CMD for h in g.get("hooks", []))]
    print("ticket gate is %s. matchers: %s"
          % ("ON" if on else "OFF", ", ".join(live) if live else "none"))
    return 0


def selftest() -> int:
    ok = fail = 0

    def check(name, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print("  FAIL %s: got %r want %r" % (name, got, want))

    check("Edit needs a ticket", _needs_ticket("Edit", {}), True)
    check("Write needs a ticket", _needs_ticket("Write", {}), True)
    check("Read never needs one", _needs_ticket("Read", {}), False)
    check("Grep never needs one", _needs_ticket("Grep", {}), False)
    check("bash grep is reading",
          _needs_ticket("Bash", {"command": "grep -rn foo ."}), False)
    check("bash ls is reading",
          _needs_ticket("Bash", {"command": "ls -la ~/.claude"}), False)
    check("git commit writes",
          _needs_ticket("Bash", {"command": "git commit -m x"}), True)
    check("git push writes",
          _needs_ticket("Bash", {"command": "git push origin main"}), True)
    check("redirect writes",
          _needs_ticket("Bash", {"command": "echo hi > /tmp/x"}), True)
    check("pipe alone is not a write",
          _needs_ticket("Bash", {"command": "cat x | head"}), False)
    check("fly deploy writes",
          _needs_ticket("Bash", {"command": "fly deploy -a app"}), True)
    check("rm writes", _needs_ticket("Bash", {"command": "rm -f /tmp/x"}), True)

    #: The two cases that decide whether this is a gate or a suggestion.
    import tempfile
    global TICKETS
    keep = TICKETS
    TICKETS = tempfile.mkdtemp()
    try:
        write_bind("s1", {"issue": 42})
        check("bound session passes", read_bind("s1").get("issue"), 42)
        write_bind("s2", {"error": "gh: not logged in"})
        check("failed creation is remembered", bool(read_bind("s2").get("error")), True)
        check("unknown session has no bind", read_bind("s3"), None)
    finally:
        TICKETS = keep

    #: One observe per process, asserted rather than remembered. This is the recurring one:
    #: observe.py caches its walk so the tree is read once, and twice now a second loader has
    #: quietly created a second instance with a second empty cache -- first inside tick.py in the
    #: morning, then here when the ops page was added. Both times the symptom was a tick that ran
    #: past its deadline and delivered nothing. `module_from_spec` registers nothing, so nothing
    #: about the second copy is visible at the call site; this check is what makes it visible.
    #: The skip is printed, never counted as a pass. A check that quietly succeeds when it could
    #: not run is the shape that let ten criticals through this estate in 18 hours.
    apath = os.path.join(HOME, ".claude", "scripts", "aiden", "aiden.py")
    if not os.path.exists(apath):
        print("  SKIP one-observe-per-process: %s is not installed" % apath)
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("aiden_check", apath)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        #: _observe(), not live_rows(). The invariant is which object the two files hold, and
        #: asking that question must not cost a walk of the transcript tree -- the first version
        #: of this check called live_rows() and took the selftest past two minutes.
        check("aiden and the gate share one observe", mod.observe is _observe(), True)

    print("selftest: %d/%d passed" % (ok, ok + fail))
    return 1 if fail else 0


def selftest_close() -> int:
    """The closer, proved on literal issue bodies, with no network and no GitHub.

    The case that matters is the negative one. A closer that closes everything passes any test
    written to watch it close something, and this estate has already shipped a guard that graded
    968 replies and passed 98.7% of them because it was checking formatting rather than evidence.
    So each control below is paired: one body that must close, and one that must not, differing
    only in the thing the closer is supposed to be reading.
    """
    global AC_TIMEOUT
    fails, ran = [], []

    def check(name, got, want):
        ran.append(name)
        if got == want:
            print("  ok   %s" % name)
        else:
            print("  FAIL %s: got %r, want %r" % (name, got, want))
            fails.append(name)

    #: No finish line is not the same as a failed finish line. None means the issue can never
    #: close; False means it was checked and is not done. Collapsing the two would close every
    #: issue nobody had bothered to write criteria for -- 21 of the 27 open on 2026-08-23.
    v, _ = verify_body("Some prose about a bug. No criteria anywhere.")
    check("no Done-when block never closes", v, None)

    v, _ = verify_body("## Done when\n- [ ] `true`\n")
    check("one passing criterion closes", v, True)

    #: The budget parser, on the same bodies. The paired control is the one that matters again:
    #: an issue with no budget must read as no budget, not as a budget of zero, because zero is a
    #: number the comparison would happily print as "over by $12" on every close.
    check("no Budget block reads as unset", budget("## Done when\n- [ ] `true`\n"), {})
    check("cost and time are read",
          budget("## Budget\n- cost: $25\n- time: 90m\n"),
          {"cost": 25.0, "time_min": 90.0})
    check("hours become minutes", budget("## Budget\n- time: 2h\n"), {"time_min": 120.0})
    check("a budget block ends at the next heading",
          budget("## Budget\n- cost: $5\n\n## Notes\n- cost: $999\n"), {"cost": 5.0})
    check("prose in the block is skipped, not guessed at",
          budget("## Budget\n- cost: roughly a tenner\n"), {})

    v, _ = verify_body("## Done when\n- [ ] `false`\n")
    check("one failing criterion stays open", v, False)

    #: Every criterion, not any. An issue that half works is not finished.
    v, _ = verify_body("## Done when\n- [ ] `true`\n- [ ] `false`\n")
    check("passing plus failing stays open", v, False)

    v, r = verify_body("## Done when\n- [ ] `rm -rf /tmp/whatever`\n")
    check("a destructive criterion is refused, not run", v, False)
    check("and the refusal says why", "refused" in (r[0] if r else ""), True)

    #: A check that cannot run must not count as a check that passed. This is the direction the
    #: estate keeps getting wrong: a guard that errors and returns success.
    was, AC_TIMEOUT = AC_TIMEOUT, 1
    try:
        v, r = verify_body("## Done when\n- [ ] `sleep 5`\n")
        check("a criterion that times out stays open", v, False)
        check("and the receipt says it timed out", "timed out" in (r[0] if r else ""), True)
    finally:
        AC_TIMEOUT = was

    #: Parsing. Each of these has already been written by an agent in a real issue body.
    check("bare bullet, no checkbox",
          acceptance("## Done when\n- `echo hi`\n"), ["echo hi"])
    check("ticked checkbox still parses",
          acceptance("## Done when\n- [x] `echo hi`\n"), ["echo hi"])
    check("heading depth does not matter",
          acceptance("#### Done when\n- [ ] `echo hi`\n"), ["echo hi"])
    check("case does not matter",
          acceptance("## DONE WHEN\n- [ ] `echo hi`\n"), ["echo hi"])
    check("the next heading ends the block",
          acceptance("## Done when\n- [ ] `echo a`\n## Notes\n- [ ] `echo b`\n"), ["echo a"])
    check("prose under the heading is ignored",
          acceptance("## Done when\nthe page serves and the tick delivers\n- [ ] `echo a`\n"),
          ["echo a"])
    check("criteria above the heading are not criteria",
          acceptance("- [ ] `echo early`\n## Done when\n- [ ] `echo a`\n"), ["echo a"])
    check("order is preserved",
          acceptance("## Done when\n- [ ] `echo a`\n- [ ] `echo b`\n"), ["echo a", "echo b"])

    #: Counted, never hard-coded. The first version of this line said 14 while 16 checks ran,
    #: which is a summary that disagrees with its own body -- the exact shape of receipt this
    #: estate has been burned by.
    print("selftest-close: %d/%d passed" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--create":
        sys.exit(open_issue(sys.argv[2], sys.argv[4] if len(sys.argv) > 4 else "",
                            sys.argv[3]))
    if len(sys.argv) > 1 and sys.argv[1] in ("--off", "--on"):
        sys.exit(switch(sys.argv[1] == "--on"))
    if len(sys.argv) > 1 and sys.argv[1] in ("--roster", "--dashboard"):
        #: Both used to walk 81k transcripts; --dashboard took 4m32s (measured 2026-09-07) to
        #: produce a page a five-minute tick then served as if it were live. session_live reads
        #: the hook ledger instead and answers in 0.08s, so there is one reader and no producer.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import session_live
        print(session_live.text(session_live.sessions(session_live.read_rows())))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest-close":
        sys.exit(selftest_close())
    if len(sys.argv) > 1 and sys.argv[1] == "--close-sweep":
        print(json.dumps(close_sweep(), indent=2))
        sys.exit(0)
    try:
        sys.exit(hook())
    except Exception:
        sys.exit(0)                                 # a broken gate never stops the estate

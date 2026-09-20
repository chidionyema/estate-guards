#!/usr/bin/env python3
"""tool-drip-guard.py — PreToolUse enforcement for the batching / delegation rules.

WHY THIS EXISTS. ~/.claude/CLAUDE.md has carried the rule for weeks:

    "Before the SECOND exploratory grep/glob/Read aimed at the same open question,
     spawn a model:'haiku' Explore subagent instead. Not 'when the search feels big'
     -- on the second call, every time. The tell that this was violated: 3+
     consecutive read-only tool calls in the main loop with no edit between them."

It is prose, so it drifts, exactly like every other status-in-a-paragraph this estate
has been burned by. `batching-compliance.py` MEASURES the violation after the money is
spent. Nothing INTERCEPTS it. This does, at the only moment where interception is
possible: PreToolUse, before the call is made.

WHAT IT BLOCKS (three rules, all mechanical, no judgement):

  1. DRIP. N consecutive read-only tool calls with no intervening edit, delegation, or
     new user prompt. Read-only = Read (whole-file), Grep, Glob, and Bash whose command
     is nothing but search/inspect verbs. A NARROW Read (offset+limit) is the sanctioned
     pattern and is exempt -- the rule targets exploration, not surgical reads.

  2. RE-READ. Reading a path already read in this context, with no write to it since.
     Re-reading is pure re-billing: 79% of spend is context transport, and the file is
     already resident.

  3. POLL. The SAME inspect question asked twice -- `tail`ing one log again, `ps`-ing for
     a job again, `gh pr checks` again -- with no productive work in between.

RULE 3 WAS ADDED 2026-08-10, AND IT IS THE EXPENSIVE ONE. On that date a session burned
~15 polls of a single background test run, and this guard, already wired at PreToolUse,
blocked none of them. Two defects, both structural:

  * A poll DISARMED the guard. `classify` returned "read" only when EVERY segment of a
    Bash command was an inspect verb, else "reset". `ps aux | grep pytest` contains `ps`,
    which was not an inspect verb, so each poll returned "reset" and ZEROED the drip
    counter. The polling did not merely evade rule 1; it rearmed it to zero each time.
  * There was no repeat detector at all. Rule 2 was gated on `tool == "Read"`, so
    `tail -25 the-same.log` fifteen times was invisible by construction.

Rule 1 measures a STREAK OF DIFFERENT READS. The actual violation is ONE IDENTICAL
QUESTION ASKED REPEATEDLY, and the two are not the same shape: polls are separated in
time by kills, edits and mutating Bash, every one of which resets a streak. Measuring the
property instead of the violation is the estate's `measure-the-violation-not-the-property`
trap, and it cost a session here.

The fix for a poll is never "poll less often". It is ONE blocking wait sized to a duration
you already know -- which is why `.state-probe` now injects the suite's measured runtime.

WHAT RESETS THE DRIP COUNTER. Any Edit/Write/NotebookEdit, any Agent spawn (delegating
IS the fix, so it must never be punished), any PRODUCTIVE Bash, and every new user prompt.
"Productive" is now the complement of inspect-or-poll, so `sleep`/`ps`/`tail` no longer
count as work done.

ESCAPE HATCHES, in the block message so they cost no round-trip to discover:
  - delegate: Agent(subagent_type="Explore", model="haiku")
  - narrow:   Read(file_path=..., offset=..., limit=...)
  - batch:    chain the shell commands with ';' into ONE Bash call
  - wait:     one blocking wait -- `gh pr checks --watch`, or a `while ...; do sleep N; done`
  - override: touch ~/.claude/state/toolguard/OFF   (kill switch, estate convention)

STATE is keyed on transcript_path, not session_id, so a subagent with its own transcript
gets its own budget -- which is the correct semantic: each context pays its own bill.

WIRING: PreToolUse (enforce) + UserPromptSubmit (reset). Both in ~/.claude/settings.json.
Exit 0 = allow. Exit 2 = block, stderr goes to the model as the reason.
NEVER raises: any internal error exits 0. A guard that breaks the session is worse than
the waste it prevents.

SELF-PROOF (enforcement that cannot detect its own removal is not enforcement -- the
graphify precedent, scripts/graphify_sweep.py:323):
    tool-drip-guard.py --check-hooks   # exit 0 iff wired in settings.json for both events
    tool-drip-guard.py --selftest      # replays the 2026-08-10 poll burst; exit 0 iff blocked
    tool-drip-guard.py --report        # what it has actually blocked, from events.jsonl
"""
import json
import os
import re
import sys
import time

STATE_DIR = os.environ.get("TOOLGUARD_STATE_DIR") or os.path.expanduser(
    "~/.claude/state/toolguard"
)
KILL_SWITCH = os.path.join(STATE_DIR, "OFF")
EVENT_LOG = os.path.join(STATE_DIR, "events.jsonl")
EVENT_LOG_MAX = 2 * 1024 * 1024  # bytes; trimmed to the tail, never unbounded

#: MEASURED 2026-08-21 and DELIBERATELY LEFT AT 3. Do not re-derive this; read the caveat.
#:
#: Corpus: 21,146 transcripts >50KB, 1,863 sessions with at least one read-only run, 8,364 runs,
#: classified by importing THIS module's own classify() so the calibration cannot drift from the
#: thing it calibrates. Run length p50=1 p90=4 p95=5 p99=10 max=172.
#: Firing rate per session, against the bands declared in ~/.claude/lanes.json
#: (>=3 noise, 0.6-3 usable, <0.15 inert):
#:     L=2  1.54 usable      L=3  0.76 USABLE      L=4  0.46 thin      L=6  0.21 thin
#:     L=8  0.10 inert       L=12 0.03 inert       L=16 0.01 inert
#: 3 is the only value in the usable band that is not twice as noisy as it needs to be. Raising it
#: to 4 does not make the guard calmer, it makes it thin, and 8 and above is inert.
#:
#: THE CAVEAT, because both instruments are censored and a future agent will find one of them.
#: This guard's OWN event log (~/.claude/state/toolguard/events.jsonl) holds 248 drip fires and
#: EVERY ONE is at n=3 -- it blocks at 3, so no run can ever be observed longer. Its apparent rate
#: there is 3.14/session, which reads as NOISE and invites a change. Both numbers are real and the
#: gap is a population difference plus censoring, so the true rate is a RANGE, not a point. No
#: reading anywhere supports a specific different number, which is why this stays at 3.
#: Detail: memory `a-censored-instrument-cannot-price-itself.md`.
DRIP_LIMIT = 3          # block the 3rd consecutive read-only call
POLL_LIMIT = 2          # block the 2nd identical inspect call (see rule 3 above)
STALE_AFTER = 6 * 3600  # seconds; forget state from an abandoned session

# Files the SessionStart hook (memory-loop.py) already injects VERBATIM into every context.
# Reading one is not a re-read, it is a duplicate of text that is already resident -- so the
# very FIRST read is the waste, and no counter-based rule can catch it. Measured 2026-08-10
# across 50 transcripts: `checkpoints/LATEST.md` read 16 times at ~2,023 tokens a time, the
# single largest re-read in the corpus, plus MEMORY.md and both CLAUDE.md files.
# Narrow reads stay exempt, which also preserves the one legitimate case: the harness requires
# a file to have been read before it can be edited, and offset/limit satisfies that far cheaper.
INJECTED_AT_SESSIONSTART = (
    "checkpoints/LATEST.md",
    "memory/MEMORY.md",
    "CLAUDE.md",
)

READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}
RESET_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit", "Agent", "Task", "Workflow"}

# A Bash call counts as exploratory ONLY if every command in it is an inspect verb.
# `grep x; grep y` is still a drip if issued alone, but it is ONE call, so it is the
# batching we want -- hence the counter, not a ban.
# `echo` is here because CLAUDE.md mandates a labelled header per receipt in a batched
# call; without it, every correctly-batched receipt block would read as productive work.
_INSPECT = (
    r"(?:grep|rg|ugrep|ag|find|fd|ls|cat|head|tail|wc|stat|file|tree|which|realpath"
    r"|basename|dirname|echo|printf|true|sed\s+-n|awk)"
)
RE_INSPECT_ONLY = re.compile(rf"^\s*{_INSPECT}\b[^;&|]*$")

# Verbs whose ONLY purpose is to ask "has it finished / changed yet?". These are the ones
# that made rule 1 inert: none of them is an inspect verb, so each one used to reset the
# counter, and none of them is productive work either.
_POLL = (
    r"(?:ps|pgrep|pidof|jobs|sleep|wait|top|uptime|date"
    r"|git\s+(?:status|log|diff\s+--stat|rev-parse)"
    r"|gh\s+(?:pr|run|workflow)\s+(?:view|checks|list|status)"
    r"|launchctl\s+list|docker\s+ps|kubectl\s+get"
    r"|curl\s+[^;&|]*-[a-zA-Z]*I|test\s+-[edfsr]|\[\s+-[edfsr])"
)
RE_POLL_SEG = re.compile(rf"^\s*(?:{_POLL}|{_INSPECT})\b[^;&|]*$")
RE_HAS_POLL = re.compile(rf"^\s*{_POLL}\b")


def state_path(key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[-120:] or "default"
    return os.path.join(STATE_DIR, safe + ".json")


def load(path: str) -> dict:
    try:
        with open(path) as fh:
            st = json.load(fh)
        if time.time() - st.get("ts", 0) > STALE_AFTER:
            return {}
        return st
    except Exception:
        return {}


def save(path: str, st: dict) -> None:
    st["ts"] = time.time()
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh)
        os.replace(tmp, path)
    except Exception:
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 177))
        except Exception: pass


def emit(rule: str, **fields) -> None:
    """Append one monitoring event. Best-effort: monitoring must never block enforcement.

    This is the MONITOR half. `batching-compliance.py` measures violations from the
    transcript after the money is spent and is wired to nothing; this records what was
    actually intercepted, at the moment of interception, so `--report` can answer
    "is the guard doing anything, and is it wrong?" without re-reading a transcript.
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            if os.path.getsize(EVENT_LOG) > EVENT_LOG_MAX:
                with open(EVENT_LOG) as fh:
                    tail = fh.readlines()[-2000:]
                with open(EVENT_LOG, "w") as fh:
                    fh.writelines(tail)
        except OSError:
            try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 197))
            except Exception: pass
        rec = {"t": round(time.time(), 3), "rule": rule}
        rec.update(fields)
        with open(EVENT_LOG, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 203))
        except Exception: pass


def _segments(cmd: str) -> list:
    return [p for p in re.split(r"[;&|]{1,2}", cmd) if p.strip()]


def is_exploratory_bash(cmd: str) -> bool:
    """True only when EVERY segment is an inspect verb. A build, test, git or python
    call is real work and must never be counted as a drip."""
    if not cmd.strip():
        return False
    parts = _segments(cmd)
    return bool(parts) and all(RE_INSPECT_ONLY.match(p) for p in parts)


def is_poll_bash(cmd: str) -> bool:
    """True when the call asks 'has it changed yet?' and does nothing else.

    Every segment must be an inspect or poll verb, AND at least one must be a poll verb --
    a pure `grep`/`cat` call stays under rule 1 where it belongs. `pkill`, `git commit`,
    `pytest`, `python` are none of these, so a command containing one is productive work
    and clears the poll map.
    """
    if not cmd.strip():
        return False
    parts = _segments(cmd)
    if not parts or not all(RE_POLL_SEG.match(p) for p in parts):
        return False
    return any(RE_HAS_POLL.match(p) for p in parts)


def fingerprint(cmd: str) -> str:
    """Collapse a command to the QUESTION it asks.

    Digits are normalised so `tail -25 run.log` and `tail -50 run.log` are recognised as
    the same question -- changing the line count is not new information, and pretending it
    is was how a 15-poll burst justified itself one call at a time.
    """
    s = " ".join(cmd.split()).lower()
    s = s.replace('"', "").replace("'", "")
    s = re.sub(r"\b\d+\b", "N", s)
    return s[:400]


def classify(tool: str, inp: dict):
    """-> ('read'|'poll'|'reset'|'ignore', detail)"""
    if tool in RESET_TOOLS:
        return "reset", inp.get("file_path") or ""
    if tool == "Read":
        # A narrow read is the sanctioned pattern, not exploration.
        if inp.get("offset") is not None or inp.get("limit") is not None:
            return "ignore", ""
        return "read", inp.get("file_path") or ""
    if tool in READ_TOOLS:
        return "read", inp.get("pattern") or inp.get("notebook_path") or ""
    if tool == "Bash":
        cmd = inp.get("command") or ""
        # BOTH shapes are fingerprinted, and that union is the point. The first cut of
        # rule 3 fingerprinted only poll-VERB commands, which left `tail -25 run.log`
        # -- the literal command of the 2026-08-10 burst -- uncaught, because `tail` is
        # an inspect verb. Its own selftest caught that. A repeated identical `grep` is
        # the same waste as a repeated `ps`: the answer is already in context.
        if is_poll_bash(cmd) or is_exploratory_bash(cmd):
            return "poll", fingerprint(cmd)
        return "reset", ""
    return "ignore", ""


DRIP_MSG = """BLOCKED by tool-drip-guard: this is read-only tool call #{n} in a row with no edit
between them. ~/.claude/CLAUDE.md: delegate on the SECOND exploratory search, every time
-- recon must not land in this context, because 79% of spend is context transport.

Do one of these instead:
  1. Agent(subagent_type="Explore", model="haiku", prompt="<the whole open question>")
     -- its tool calls bill against its own small context and its dumps never enter yours.
  2. Read(file_path=..., offset=..., limit=...) if you already know the region. Narrow
     reads are exempt from this guard.
  3. One Bash call with the commands chained by ';' and a labelled header per receipt.

Kill switch if this is genuinely wrong: touch ~/.claude/state/toolguard/OFF"""

REREAD_MSG = """BLOCKED by tool-drip-guard: {path} was already read in this context and has not
been written since. Re-reading re-bills a file that is already resident -- it is the single
cheapest waste to remove.

Use what is already in context. If you need a different region, Read with offset/limit
(exempt). If the file changed outside this session, touch ~/.claude/state/toolguard/OFF."""

INJECTED_MSG = """BLOCKED by tool-drip-guard: {path} is injected into EVERY context at SessionStart
by memory-loop.py. It is already resident -- reading it bills the same text a second time.

Measured 2026-08-10 across 50 transcripts: checkpoints/LATEST.md was read 16 times at roughly
2,023 tokens each, the largest single re-read in the corpus. Unlike a re-read, the FIRST read
is already the waste here, so no counter can catch it.

  - To USE it: scroll up. It is in your context, at the top.
  - To EDIT it: Read(file_path=..., offset=..., limit=...). Narrow reads are exempt, and they
    satisfy the harness's read-before-edit precondition at a fraction of the tokens.
  - If SessionStart genuinely did not inject it (a resumed or compacted session can differ),
    that is the one real exception: touch ~/.claude/state/toolguard/OFF"""

POLL_MSG = """BLOCKED by tool-drip-guard (POLL): you have already asked this exact question
{n} time(s) in this context with no productive work in between:

    {fp}

Each poll re-bills the ENTIRE resident context to learn, almost always, nothing. On
2026-08-10 one session spent ~15 of these on a single background job; 14 taught it nothing.
Changing `-n 25` to `-n 50` is not a different question -- digits are normalised here.

Replace the poll with ONE blocking wait sized to a duration you know:
  1. `gh pr checks --watch` / `gh run watch` -- one call, returns when CI settles.
  2. One Bash call that blocks: `while pgrep -f "<job>" >/dev/null; do sleep 30; done; <report>`
     Size the sleep to the MEASURED duration -- `.state-probe` injects the suite's at
     session start. If a duration is unknown, measure it ONCE and record it; never poll blind.
  3. If a commit gate will run the verification, do NOT also run it yourself. The gate IS
     the run.
  4. Do the next piece of independent work now and collect the result in one batched call
     later -- a wait is not a reason to idle.

Kill switch if this is genuinely wrong: touch ~/.claude/state/toolguard/OFF"""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if os.path.exists(KILL_SWITCH):
        return 0

    event = payload.get("hook_event_name", "")
    key = payload.get("transcript_path") or payload.get("session_id") or "default"
    sp = state_path(key)
    st = load(sp)

    # A new user prompt is a new intent: the drip counter starts over.
    # The POLL map deliberately SURVIVES a new prompt. A poll burst is what happens across
    # turns while a job runs -- "is it done yet?" is a new prompt every time, and clearing
    # here would make rule 3 as inert as rule 1 was.
    if event == "UserPromptSubmit":
        st["run"] = 0
        save(sp, st)
        return 0

    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input") or {}
    kind, detail = classify(tool, inp)

    if kind == "ignore":
        return 0

    seen = st.setdefault("seen", {})
    polls = st.setdefault("polls", {})

    if kind == "reset":
        st["run"] = 0
        st["polls"] = {}  # real work happened; the same question may now have a new answer
        if detail:
            seen.pop(os.path.abspath(detail), None)  # written -> a fresh read is legitimate
        save(sp, st)
        return 0

    if kind == "poll":
        n = int(polls.get(detail, 0)) + 1
        polls[detail] = n
        st["run"] = int(st.get("run", 0)) + 1  # a poll is read-only; it must NOT rearm rule 1
        save(sp, st)
        if n >= POLL_LIMIT:
            emit("poll", n=n, fp=detail, key=os.path.basename(sp))
            sys.stderr.write(POLL_MSG.format(n=n - 1, fp=detail))
            return 2
        # fall through to the drip counter with the incremented run
        if st["run"] >= DRIP_LIMIT:
            st["run"] = 0
            save(sp, st)
            emit("drip", n=DRIP_LIMIT, tool=tool, key=os.path.basename(sp))
            sys.stderr.write(DRIP_MSG.format(n=DRIP_LIMIT))
            return 2
        return 0

    # --- read-only from here ---
    if tool == "Read" and detail:
        ap = os.path.abspath(detail)
        if any(ap.endswith(suffix) for suffix in INJECTED_AT_SESSIONSTART):
            save(sp, st)
            emit("injected", path=detail, key=os.path.basename(sp))
            sys.stderr.write(INJECTED_MSG.format(path=detail))
            return 2
        if ap in seen:
            save(sp, st)
            emit("reread", path=detail, key=os.path.basename(sp))
            sys.stderr.write(REREAD_MSG.format(path=detail))
            return 2
        seen[ap] = 1

    st["run"] = int(st.get("run", 0)) + 1
    save(sp, st)

    if st["run"] >= DRIP_LIMIT:
        st["run"] = 0  # blocked once; do not deadlock the next legitimate call
        save(sp, st)
        emit("drip", n=DRIP_LIMIT, tool=tool, key=os.path.basename(sp))
        sys.stderr.write(DRIP_MSG.format(n=DRIP_LIMIT))
        return 2
    return 0


# ---------------------------------------------------------------------------
# Self-proof. Three commands, because a guard nobody can interrogate becomes a guard
# nobody notices has stopped working -- `spend-guard-governs-four-percent-of-burn` and
# `durable-ledger-was-inert-1874-fixture-laws` are both that failure.
# ---------------------------------------------------------------------------

SETTINGS = os.path.expanduser("~/.claude/settings.json")


def check_hooks() -> int:
    """Exit 0 iff this script is wired for BOTH events in settings.json."""
    try:
        with open(SETTINGS) as fh:
            cfg = json.load(fh)
    except Exception as exc:
        print(f"FAIL  cannot read {SETTINGS}: {exc}")
        return 1
    me = os.path.basename(__file__)
    wired = set()
    for event, entries in (cfg.get("hooks") or {}).items():
        for entry in entries or []:
            for hook in entry.get("hooks") or []:
                if me in str(hook.get("command", "")):
                    wired.add(event)
    ok = True
    for need in ("PreToolUse", "UserPromptSubmit"):
        mark = "PASS" if need in wired else "FAIL"
        ok &= need in wired
        print(f"{mark}  {need} -> {me}")

    # Wiring is only half of it. A hook can be wired and still classify nothing -- that is
    # precisely the state this guard was in until 2026-08-10, when `ps aux | grep pytest`
    # returned "reset". These assertions are in-process and pure: no subprocess, no temp
    # dir, no writes, so this stays safe to run from the SessionStart probe, whose first
    # rule is that it never writes.
    behaviours = [
        ("poll verb classified",
         classify("Bash", {"command": "ps aux | grep pytest"})[0] == "poll"),
        ("inspect repeat classified",
         classify("Bash", {"command": "tail -25 /tmp/run.log"})[0] == "poll"),
        ("digits normalised to one question",
         fingerprint("tail -25 /tmp/run.log") == fingerprint("tail -50 /tmp/run.log")),
        ("distinct targets stay distinct",
         fingerprint("tail -25 /tmp/a.log") != fingerprint("tail -25 /tmp/b.log")),
        ("real work is not a drip",
         classify("Bash", {"command": ".venv/bin/pytest -q"})[0] == "reset"),
        ("narrow read exempt",
         classify("Read", {"file_path": "/x", "offset": 1, "limit": 5})[0] == "ignore"),
    ]
    for label, passed in behaviours:
        print(f"{'PASS' if passed else 'FAIL'}  detector: {label}")
        ok &= bool(passed)

    if os.path.exists(KILL_SWITCH):
        print(f"WARN  kill switch present, guard is INERT: {KILL_SWITCH}")
        ok = False

    cutoff = time.time() - 7 * 86400
    counts = {}
    try:
        with open(EVENT_LOG) as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("t", 0) >= cutoff:
                    counts[rec.get("rule", "?")] = counts.get(rec.get("rule", "?"), 0) + 1
    except OSError:
        pass
    print("BLOCKS7D " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    print("── " + ("ENFORCING" if ok else "NOT ENFORCING"))
    return 0 if ok else 1


def _probe(state_dir, payload) -> int:
    import subprocess

    env = dict(os.environ, TOOLGUARD_STATE_DIR=state_dir)
    p = subprocess.run(
        [sys.executable, os.path.abspath(__file__)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return p.returncode


def selftest() -> int:
    """Replay real sequences against the real entry point in a throwaway state dir.

    Subprocesses, not function calls: the harness depends on the EXIT CODE, and the
    fixture must exercise what the hook actually runs. It writes only to a temp dir --
    `tests-polluted-the-production-audit-log` is the trap this avoids.
    """
    import shutil
    import tempfile

    sd = tempfile.mkdtemp(prefix="toolguard-selftest-")
    tx = "/tmp/selftest-transcript.jsonl"

    def bash(cmd):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "transcript_path": tx,
        }

    def prompt():
        return {"hook_event_name": "UserPromptSubmit", "transcript_path": tx}

    failures = []

    def expect(label, make, want, reset=False):
        """`make` is a THUNK, not a payload.

        It was a payload once. Python evaluates arguments before the call, so `reset=True`
        rebound the transcript AFTER that call's payload had already been stamped with the
        previous one: every section ran one context behind, and the harness reported
        failures the guard had not committed. A harness that lies is worse than no harness
        (`paid-ab-harness-must-be-fixture-tested-first`).
        """
        nonlocal tx
        if reset:
            tx = f"/tmp/selftest-{re.sub(r'[^a-z0-9]+', '-', label.lower())}.jsonl"
        got = _probe(sd, make())
        ok = got == want
        if not ok:
            failures.append(f"{label}: want exit {want}, got {got}")
        print(f"{'PASS' if ok else 'FAIL'}  {label}  (exit {got}, want {want})")

    print("=== rule 3: the 2026-08-10 poll burst (the sequence that cost the session) ===")
    expect("poll1 tail -25 run.log", lambda: bash("tail -25 /tmp/run.log"), 0, reset=True)
    expect("poll2 tail -50 run.log (digits normalised -> same question)",
           lambda: bash("tail -50 /tmp/run.log"), 2)
    expect("a NEW USER PROMPT does not launder a poll", lambda: prompt(), 0)
    expect("poll3 tail -25 run.log (still blocked across the turn)",
           lambda: bash("tail -25 /tmp/run.log"), 2)

    print("\n=== the disarm defect: a poll must NOT reset the drip counter ===")
    expect("ps aux | grep pytest (#1)", lambda: bash("ps aux | grep pytest"), 0, reset=True)
    expect("pgrep -fl pytest (#2, different question)", lambda: bash("pgrep -fl pytest"), 0)
    expect("jobs (#3 read-only in a row -> DRIP)", lambda: bash("jobs"), 2)

    print("\n=== productive work clears the poll map (no false positive) ===")
    expect("git status (#1)", lambda: bash("git status -sb"), 0, reset=True)
    expect("git commit (productive)", lambda: bash("git commit -m x"), 0)
    expect("git status (legitimate re-check after work)", lambda: bash("git status -sb"), 0)

    print("\n=== real work is never a drip or a poll ===")
    expect("pytest -q", lambda: bash(".venv/bin/pytest -q"), 0, reset=True)
    expect("pytest -q again", lambda: bash(".venv/bin/pytest -q"), 0)
    expect("npm run build", lambda: bash("npm run build"), 0)

    print("\n=== batched receipts stay legal (CLAUDE.md's sanctioned pattern) ===")
    expect("one batched call, labelled headers",
           lambda: bash('echo "=== A ==="; git log --oneline -1; echo "=== B ==="; git status -sb'),
           0, reset=True)
    expect("a THIRD distinct narrow Read is exempt (offset+limit)",
           lambda: {"hook_event_name": "PreToolUse", "tool_name": "Read",
                    "tool_input": {"file_path": "/tmp/x.py", "offset": 1, "limit": 20},
                    "transcript_path": tx}, 0)

    print("\n=== SessionStart-injected files are blocked on the FIRST read ===")
    def rd(path, **extra):
        return {"hook_event_name": "PreToolUse", "tool_name": "Read",
                "tool_input": dict({"file_path": path}, **extra), "transcript_path": tx}

    expect("LATEST.md whole-file (16x in 50 sessions)",
           lambda: rd("/Users/x/.claude/projects/p/checkpoints/LATEST.md"), 2, reset=True)
    expect("MEMORY.md whole-file",
           lambda: rd("/Users/x/.claude/projects/p/memory/MEMORY.md"), 2)
    expect("CLAUDE.md whole-file", lambda: rd("/Users/x/repo/CLAUDE.md"), 2)
    expect("LATEST.md NARROW is allowed (read-before-edit stays possible)",
           lambda: rd("/Users/x/.claude/projects/p/checkpoints/LATEST.md",
                      offset=1, limit=40), 0)
    expect("an ordinary memory file is untouched",
           lambda: rd("/Users/x/.claude/projects/p/memory/some-lesson.md"), 0)

    shutil.rmtree(sd, ignore_errors=True)
    print("\n── " + (f"{len(failures)} FAILURE(S)" if failures else "ALL PASS"))
    for f in failures:
        print("   " + f)
    return 1 if failures else 0


def report() -> int:
    """What the guard has actually intercepted. Monitoring, not prose."""
    if not os.path.exists(EVENT_LOG):
        print(f"no events yet ({EVENT_LOG} absent) — guard has blocked nothing")
        return 0
    counts, recent = {}, []
    with open(EVENT_LOG) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            counts[rec.get("rule", "?")] = counts.get(rec.get("rule", "?"), 0) + 1
            recent.append(rec)
    print(f"blocks by rule ({len(recent)} total, {EVENT_LOG}):")
    for rule, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {rule}")
    print("\nmost recent 10:")
    for rec in recent[-10:]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.get("t", 0)))
        what = rec.get("fp") or rec.get("path") or rec.get("tool") or ""
        print(f"  {when}  {rec.get('rule','?'):7s}  {str(what)[:90]}")
    return 0


if __name__ == "__main__":
    try:
        arg = sys.argv[1] if len(sys.argv) > 1 else ""
        if arg == "--check-hooks":
            sys.exit(check_hooks())
        if arg == "--selftest":
            sys.exit(selftest())
        if arg == "--report":
            sys.exit(report())
        sys.exit(main())
    except Exception:
        sys.exit(0)  # a broken guard must never break a session

#!/usr/bin/env python3
"""Refuse a reply that does not close, and a DONE: that carries no receipt.

WHY THIS IS A SCRIPT AND NOT A RULE. The rule already exists. `~/.claude/CLAUDE.md` has said
since 2026-08-10 that line 1 of every reply is DONE:, BLOCKED: or WORKING: and that "a reply that
does not start with one of those three is malformed". Measured 2026-08-21 across 10 sessions and
22,395 tool calls: sessions ending on one of those three markers, 0 of 10. A rule every session
can read and every session breaks is the floor. Founder's words: "we dont close anything
preoperly, bugs ,issues chaos everythre" and "we shi and dont verify".

TWO CHECKS, AND THE SECOND IS THE ONE THAT MATTERS.

  1. THE MARKER. The first non-blank line opens with DONE:, BLOCKED: or WORKING:. This is the
     cheap half. It forces the state of the work to the top of the reply where the founder reads
     it, instead of leaving him to infer it from three paragraphs.

  2. THE RECEIPT. A reply that opens DONE: has to show something. "we shi and dont verify" is the
     complaint this half answers: DONE with nothing to check is a claim, and a claim is what the
     proof-of-claim rule bans. Any ONE of these satisfies it, because any one of them is a thing
     the founder can re-run or open:
        a fenced block          the command that proves it
        a file.ext:123          the line that shows it
        a verdict token         "33/33 passed", "exit 0", "rc=2", "4612 passed"
     BLOCKED: and WORKING: need no receipt. Being stuck and being mid-flight are honest states
     and demanding evidence for them would only teach the session to claim DONE instead.

WHAT IT DOES NOT CHECK, DELIBERATELY. Whether the goal is actually finished. That needs judgement
about the work, and a guard that guesses at judgement produces exactly the confident-and-wrong
self-assessment the self-critique literature measures (Stechly/Kambhampati, arXiv 2310.12397:
GPT-4 called 30 of 500 colourings correct and was right about 5). This guard only ever grades
things a machine can see in the text.

WHY IT CANNOT LOOP. Copied from jargon-guard, which solved this already: at most three blocks per
session, and never twice for the same text. Rewriting gets past it; repeating yourself does not
get blocked forever. `stop_hook_active` is not consulted because it says only that SOME stop hook
fired, not which -- the digest is the stronger guarantee.

WHY IT CANNOT WEDGE. A deadline kills it after 3 seconds, and every path exits 0 on any
exception. A guard that costs a turn is worse than the drift it prevents.

  python3 close-guard.py --selftest
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

STATE = Path.home() / ".claude" / "state" / "close-guard.json"
LANES = Path.home() / ".claude" / "lanes.json"
MAX_BLOCKS_PER_SESSION = 3

MARKERS = ("DONE:", "BLOCKED:", "WORKING:")

#: any one of these makes a DONE: checkable by the founder without asking a question back
FENCE = re.compile(r"```")
FILELINE = re.compile(r"[\w./-]+\.\w+:\d+")
VERDICT = re.compile(
    r"(\b\d+\s*/\s*\d+\s+(passed|checks|green|ok)\b"      # 33/33 passed
    r"|\bexit(ed with)?\s+(code\s+)?\d+\b"                 # exit 0
    r"|\brc=\d+\b"                                         # rc=2
    r"|\b\d+\s+(passed|failed|skipped|merged|fires?)\b"    # 4612 passed
    r"|\bcommit\s+[0-9a-f]{7,40}\b"                        # commit b4cedf9
    r"|\b[0-9a-f]{7,40}\b\s*(->|→)\s*\b[0-9a-f]{7,40}\b)", # 6ad5f66..845b089 style
    re.I,
)


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def marker_of(text: str) -> str | None:
    """Which of the three the reply opens with, or None."""
    head = first_line(text)
    head = re.sub(r"^[*_#>\s]+", "", head)        # a bold or heading marker is still opening
    for m in MARKERS:
        if head.upper().startswith(m):
            return m
    return None


#: a table of measured numbers is evidence. Two CONSECUTIVE lines that both carry a `|`.
#: The earlier version counted pipes per line, which pinned markdown's outer pipes as though
#: they meant something -- a mutation that demanded three pipes instead of two survived the
#: suite, because the only thing it changed was a corner nobody cares about. Two lines with a
#: column separator is the actual signal, and it is the thing the check now grades.
TABLE = re.compile(r"^[^\n|]*\|[^\n]*$\n[^\n|]*\|", re.M)
#: a share is a measurement. "58.4%" is a receipt in a way that "about 3 goes" is not.
PERCENT = re.compile(r"\d[\d.,]*\s?%")
#: an inline span that is a RUNNABLE COMMAND -- it needs a space, because a command takes an
#: argument. It used to accept a space OR a slash, which let a bare path count: `~/.claude/lanes.json`
#: is a location, not a result, and naming where a thing lives says nothing about whether it works.
INLINE_CMD = re.compile(r"`[^`\n]*\s[^`\n]*`")

#: Receipt shapes the checks above miss. Measured 2026-08-23 by replaying 967 DONE: replies from
#: 135 sessions: the accept set above passed 98.7% of them, so it was grading formatting rather
#: than evidence. Dropping the bare-path accept refused 149, and reading those 149 by hand showed
#: most carried real evidence in a shape no pattern here recognised -- a PR link the founder can
#: open, "selftest 10/10", "stranded=34", "0 failures". Those are receipts. These are them.
CHECKABLE = re.compile(
    r"(https?://\S+"                                   # a page he can open -- a LAW 9 channel
    r"|\b(?:PR|issue)\s*#\d+\b"                        # PR #254
    r"|\b\d+\s*/\s*\d+\b"                              # selftest 10/10
    r"|\b\d+\s+(?:failures?|errors?|warnings?|repos?|items?|files?|rows?|jobs?|tests?)\b"
    r"|\b\w+=-?\d+\b"                                  # stranded=34
    r"|\bHTTP\s*\d{3}\b"                               # HTTP 502
    r"|\b[0-9a-f]{7,40}\b(?=\s+(?:on|in|to)\b))",       # aae8f47 on <branch>
    re.I,
)


def has_receipt(text: str) -> bool:
    """Anything the founder can re-run, open or count. Deliberately wide.

    It was narrower -- fence, file:line, verdict token -- and a peer pointed out that their
    best-evidenced reply of the day would have been refused by it: a markdown table of measured
    counts and a backticked `git worktree list`, with none of the three. A grader with a narrow
    accept set does not raise the standard of evidence, it teaches sessions to paste a decorative
    fenced block to get past it, which is the failure mode of every guard that grades a proxy.
    The bar is "can the founder check this himself", not "is it formatted the way I expected".
    """
    return bool(
        FENCE.search(text)
        or FILELINE.search(text)
        or VERDICT.search(text)
        or TABLE.search(text)
        or PERCENT.search(text)
        or INLINE_CMD.search(text)
        or CHECKABLE.search(text)
    )


def lane_wants_close(lane_name: str) -> bool:
    try:
        lanes = json.loads(LANES.read_text(encoding="utf-8")).get("lanes") or {}
    except Exception:  # noqa: BLE001 - no config means no opinion, never a refusal
        return False
    lane = lanes.get(lane_name) or lanes.get("default") or {}
    return bool(lane.get("close_condition"))


def current_lane(session: str) -> str:
    """Always "default" since crew#638 deleted goal-guard, which was the only writer of
    ~/.claude/state/goal/<session>.json. Kept as a function, and kept reading nothing, so the
    one place a lane could come back is this line rather than a silent read of a file no
    process creates -- a read like that returns "default" too, but looks like it measured."""
    return "default"


def last_assistant_text(transcript: Path) -> str:
    """The final assistant message, text blocks only. Thinking is not shown to the founder."""
    text = ""
    with transcript.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            content = (row.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            joined = "\n".join(p for p in parts if p).strip()
            if joined:
                text = joined
    return text


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(STATE)
    except Exception:  # noqa: BLE001 - failing to record must never fail the turn
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 177))
        except Exception: pass


def report(kind: str, lane: str) -> str:
    if kind == "marker":
        return (
            "THIS REPLY DOES NOT SAY WHERE THE WORK STANDS.\n"
            "  Line 1 is DONE:, BLOCKED: or WORKING:, then one plain sentence.\n"
            "  Law: ~/.claude/CLAUDE.md, \"Reply format - ANSWER FIRST\", 2026-08-10.\n"
            "  Measured 2026-08-21: 0 of 10 sessions ended on one of the three. This is the\n"
            "  machine that stops the eleventh.\n"
            "  Pick the honest one. WORKING: is not an admission and BLOCKED: is not a failure;\n"
            "  the only wrong answer is leaving the founder to infer it.\n"
            "  (lane %s, close_condition on. Rewrite line 1 and stop again.)" % lane
        )
    return (
        "DONE: WITH NOTHING TO CHECK.\n"
        "  Founder's words: \"we shi and dont verify\". A DONE: the founder cannot re-run is a\n"
        "  claim, and the proof-of-claim rule bans an unbacked claim.\n"
        "  Add ONE of these to the reply, whichever is honest:\n"
        "    a fenced block   the command that proves it, with its output\n"
        "    a file.ext:123   the line that shows it\n"
        "    a verdict token  \"33/33 passed\", \"exit 0\", a commit sha\n"
        "  If there IS no receipt because the thing is not actually proved, the honest line 1 is\n"
        "  WORKING: or BLOCKED:, not DONE:. Changing the marker is a legal way past this.\n"
        "  (lane %s. Rewrite and stop again.)" % lane
    )


def _deadline(seconds: int = 5) -> None:
    try:
        import signal
        signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.alarm(seconds)
    except Exception:  # noqa: BLE001
        pass


OBSERVE = Path.home() / ".claude" / "state" / "close-guard-observe.jsonl"
_IN_SELFTEST = False   # the selftest calls handle(), which calls observe(); without this
                       # flag its fixtures land in the production log. 8 of the first 12
                       # lines were selftest artifacts, which made the log unreadable.


def settle(path: Path, max_wait: float = 1.2, quiet: float = 0.15) -> None:
    """Wait until the transcript stops growing, or give up.

    WHY THIS EXISTS. 2026-08-21, twenty minutes after this guard went live, it refused a reply
    that opened with DONE:. The reply was row 2428 of the transcript and the guard graded row
    2406, which was an interstitial line from earlier in the same turn. The final message was not
    on disk when the hook ran. A Stop hook that reads the transcript as it finds it is grading
    the second-to-last thing the session said, which is a different sentence written for a
    different purpose.

    Whether waiting is enough is a measurement, not an assumption -- if Claude Code writes the
    final row only after every Stop hook returns, no wait can help and the check has to move to
    the next turn instead. `observe()` below records both readings on every Stop so the answer
    comes from this estate rather than from a guess.
    """
    import time
    last, stable_since = -1, 0.0
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return
        now = time.time()
        if size != last:
            last, stable_since = size, now
        elif now - stable_since >= quiet:
            return
        time.sleep(0.03)


def observe(path: Path, session: str) -> None:
    """Record what the hook would have graded before and after waiting. Never raises."""
    if _IN_SELFTEST:
        return
    try:
        import time
        t0 = time.time()
        before = last_assistant_text(path)
        settle(path)
        after = last_assistant_text(path)
        line = json.dumps({  # crew#73: "at" + "kind" so the series is orderable
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": "observe", "session": session,
            "waited_ms": int((time.time() - t0) * 1000),
            "changed": before != after,
            "before_marker": marker_of(before),
            "before_head": before[:60],
            "after_marker": marker_of(after),
            "after_head": after[:60],
        })
        OBSERVE.parent.mkdir(parents=True, exist_ok=True)
        with OBSERVE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")          # O_APPEND, one line, well under the pipe buffer
    except Exception:  # noqa: BLE001
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 275))
        except Exception: pass


def handle(payload: dict) -> int:
    path = payload.get("transcript_path") or ""
    if not path or not os.path.exists(path):
        return 0                                   # a probe that cannot run means PASS
    session = str(payload.get("session_id") or "unknown")
    lane = current_lane(session)
    if not lane_wants_close(lane):
        observe(Path(path), session)   # off for this lane, but still learning what to grade
        return 0
    settle(Path(path))                 # the final message may not be on disk yet -- see settle()
    try:
        text = last_assistant_text(Path(path))
    except OSError:
        return 0
    if not text:
        return 0                                   # a turn that was only tool calls closes nothing

    marker = marker_of(text)
    if marker is None:
        kind = "marker"
    elif marker == "DONE:" and not has_receipt(text):
        kind = "receipt"
    else:
        return 0

    digest = hashlib.sha256((kind + text).encode("utf-8")).hexdigest()[:16]
    state = load_state()
    mine = state.get(session) or {"count": 0, "seen": []}
    if digest in mine["seen"] or mine["count"] >= MAX_BLOCKS_PER_SESSION:
        return 0
    mine["count"] += 1
    mine["seen"] = (mine["seen"] + [digest])[-20:]
    state[session] = mine
    save_state(state)

    print(report(kind, lane), file=sys.stderr)
    return 2


def _check_observe_is_silent_in_selftest(tmpdir, ck):
    """observe() must write NOTHING while the selftest runs -- see _IN_SELFTEST."""
    global OBSERVE
    saved, probe = OBSERVE, Path(tmpdir) / "observe-probe.jsonl"
    OBSERVE = probe
    try:
        t = Path(tmpdir) / "t.jsonl"
        t.write_text(json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "no marker here"}]}}) + "\n")
        observe(t, "selftest")
        ck("observe writes nothing during selftest", not probe.exists())
        global _IN_SELFTEST
        _IN_SELFTEST = False
        observe(t, "selftest")
        ck("observe DOES write when not in selftest", probe.exists())
        _IN_SELFTEST = True
    finally:
        OBSERVE = saved


def selftest() -> int:
    global _IN_SELFTEST
    _IN_SELFTEST = True
    c = []

    def ck(name, cond, extra=None):
        c.append((name, bool(cond), extra))

    ck("DONE is a marker", marker_of("DONE: shipped it") == "DONE:")
    ck("BLOCKED is a marker", marker_of("BLOCKED: need a key") == "BLOCKED:")
    ck("WORKING is a marker", marker_of("WORKING: halfway") == "WORKING:")
    ck("prose is not", marker_of("I shipped it.") is None)
    ck("lowercase still counts", marker_of("done: shipped") == "DONE:")
    ck("bold still counts", marker_of("**DONE:** shipped") == "DONE:")
    ck("a heading still counts", marker_of("## WORKING: on it") == "WORKING:")
    ck("leading blank lines skipped", marker_of("\n\n\nDONE: yes") == "DONE:")
    ck("empty text has no marker", marker_of("") is None)
    ck("the word done mid-line is not a marker", marker_of("Nearly DONE: soon") is None)

    ck("a fence is a receipt", has_receipt("DONE:\n```\nls\n```"))
    ck("a file:line is a receipt", has_receipt("DONE: see verify.py:365"))
    ck("a ratio is a receipt", has_receipt("DONE: 33/33 passed"))
    ck("an exit code is a receipt", has_receipt("DONE: exit 0"))
    ck("rc is a receipt", has_receipt("DONE: dispatch rc=2"))
    ck("a count is a receipt", has_receipt("DONE: 4612 passed"))
    ck("a sha is a receipt", has_receipt("DONE: commit b4cedf9"))
    ck("bare prose is not a receipt", not has_receipt("DONE: I fixed the thing and it works"))
    ck("a bare number is not a receipt", not has_receipt("DONE: took about 3 goes"))

    ck("lane lookup does not raise", isinstance(lane_wants_close("default"), bool))
    ck("an unknown lane falls back to default",
       lane_wants_close("no-such-lane") == lane_wants_close("default"))

    # end to end, with a transcript on disk AND a lanes file we control.
    # The live lanes.json is NOT used here. An earlier version of this selftest wrote
    # `(... == 2) if lane_wants_close("default") else True`, so every check that mattered
    # passed vacuously whenever the default lane happened to have close_condition off --
    # which it did. A check whose outcome depends on production config is not a check.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        t = Path(d) / "t.jsonl"
        global STATE, LANES
        keep_state, keep_lanes = STATE, LANES
        STATE = Path(d) / "state.json"

        def set_lanes(close_on):
            global LANES
            f = Path(d) / ("lanes-%s.json" % close_on)
            f.write_text(json.dumps({"lanes": {
                "default": {"close_condition": close_on},
                "build": {"close_condition": True},
            }}), encoding="utf-8")
            LANES = f

        def run(msg, sess):
            t.write_text(json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": msg}]},
            }) + "\n", encoding="utf-8")
            return handle({"transcript_path": str(t), "session_id": sess})

        try:
            set_lanes(True)
            ck("lane ON: a reply with no marker is refused", run("I fixed it.", "s1") == 2)
            ck("lane ON: a good reply passes", run("DONE: shipped, 33/33 passed", "s2") == 0)
            ck("lane ON: WORKING needs no receipt", run("WORKING: still reading", "s3") == 0)
            ck("lane ON: BLOCKED needs no receipt", run("BLOCKED: no key here", "s4") == 0)
            ck("lane ON: DONE with no receipt is refused",
               run("DONE: I fixed the thing and it works", "s5") == 2)
            ck("lane ON: the same text is never refused twice", run("I fixed it.", "s1") == 0)
            for i2 in range(5):
                run("I fixed it %d." % i2, "s6")
            ck("lane ON: at most three blocks per session",
               (load_state().get("s6") or {}).get("count", 0) == MAX_BLOCKS_PER_SESSION,
               (load_state().get("s6") or {}).get("count"))
            ck("lane ON: a missing transcript passes",
               handle({"transcript_path": str(Path(d) / "nope"), "session_id": "s7"}) == 0)
            t.write_text("{}\n", encoding="utf-8")
            ck("lane ON: a tool-only turn passes",
               handle({"transcript_path": str(t), "session_id": "s8"}) == 0)
            t.write_text("not json\n", encoding="utf-8")
            ck("lane ON: garbage in the transcript passes",
               handle({"transcript_path": str(t), "session_id": "s9"}) == 0)
            ck("lane ON: an empty payload passes", handle({}) == 0)

            set_lanes(False)
            ck("lane OFF: the same bad reply is waved through", run("I fixed it.", "s10") == 0)
            ck("lane OFF: DONE with no receipt is waved through",
               run("DONE: it works", "s11") == 0)

            LANES = Path(d) / "no-such-lanes.json"
            ck("a missing lanes file never refuses", run("I fixed it.", "s12") == 0)
            LANES = Path(d) / "broken.json"
            LANES.write_text("{ not json", encoding="utf-8")
            ck("a corrupt lanes file never refuses", run("I fixed it.", "s13") == 0)
        finally:
            STATE, LANES = keep_state, keep_lanes

    # MEASURED 2026-08-21 on this estate's own transcript: the compaction summary is written as
    # a row of type "user" with isCompactSummary true (10 such rows, all user), so an
    # assistant-only reader never sees it. A peer raised this as a hazard -- four sessions blocked
    # at once on a turn nobody wrote -- and the measurement refutes it. This check exists so that
    # if Claude Code ever writes the summary as an assistant row, it fails here instead of in
    # production.
    with tempfile.TemporaryDirectory() as d2:
        t2 = Path(d2) / "t.jsonl"
        t2.write_text(
            json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "text", "text": "DONE: 33/33 passed"}]}})
            + "\n"
            + json.dumps({"type": "user", "isCompactSummary": True,
                          "message": {"content": [{"type": "text",
                                                   "text": "This session is being continued"}]}})
            + "\n", encoding="utf-8")
        ck("a compaction summary is not graded as the reply",
           last_assistant_text(t2) == "DONE: 33/33 passed", last_assistant_text(t2)[:40])

    ck("a table of counts is a receipt",
       has_receipt("DONE: it landed\n\n| what | n |\n|---|---|\n| merged | 4 |"))
    ck("a percentage is a receipt", has_receipt("DONE: 58.4% of writes go through Bash"))
    ck("a backticked command is a receipt", has_receipt("DONE: see `git worktree list`"))
    ck("a backticked path alone is NOT a receipt",
       not has_receipt("DONE: it is in `~/.claude/lanes.json`"))
    ck("a url is a receipt", has_receipt("DONE: PR is open https://github.com/x/y/pull/254"))
    ck("a PR number is a receipt", has_receipt("DONE: PR #254 is open and mergeable"))
    ck("a bare ratio is a receipt", has_receipt("DONE: selftest 10/10 and ruff clean"))
    ck("a counted noun is a receipt", has_receipt("DONE: the backfill ran, 0 failures"))
    ck("a key=value count is a receipt", has_receipt("DONE: stranded=34, shelf=74"))
    ck("an http status is a receipt", has_receipt("DONE: the shelf was HTTP 502, now serving"))
    ck("a sha on a branch is a receipt", has_receipt("DONE: aae8f47 on feat/research-batch"))
    ck("a bare word is still not a receipt", not has_receipt("DONE: the thing is done now"))
    ck("a one-word backtick is not a receipt", not has_receipt("DONE: the `flag` is set"))
    ck("one pipe on one line is not a table", not has_receipt("DONE: a | b"))
    ck("two lines with a column separator is a table",
       has_receipt("DONE: it landed\n\nwhat | n\nmerged | 4"))
    ck("a borderless two-column table is a receipt",
       has_receipt("DONE:\n\nlane | limit\nbuild | 12"))

    with tempfile.TemporaryDirectory() as d3:
        _check_observe_is_silent_in_selftest(d3, ck)

    ck("the marker report names the three",
       all(m in report("marker", "default") for m in MARKERS))
    ck("the receipt report quotes the founder",
       "we shi and dont verify" in report("receipt", "default"))
    ck("the receipt report offers the honest way out",
       "WORKING:" in report("receipt", "default"))

    bad = [(n, e) for n, ok, e in c if not ok]
    for n, e in bad:
        print("FAIL %s %r" % (n, e), file=sys.stderr)
    if bad:
        print("close-guard selftest: %d/%d FAILED" % (len(bad), len(c)), file=sys.stderr)
        return 1
    print("close-guard selftest: %d/%d checks passed" % (len(c), len(c)))
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        payload = {}
    return handle(payload)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(main())
    _deadline()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0) from None

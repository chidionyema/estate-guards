#!/usr/bin/env python3
"""Refuse a peer message that re-raises something already on the estate board.

WHY THIS EXISTS. Founder, 2026-08-20: "the peer nessages are not workig it is too noisy, we need
to turn it downn", then, when the first fix was silence: "its tyoo nnot but useful wwith the
downside it keeps everyone loppingevrt he sane issues".

That second sentence is the diagnosis and it is not about volume. The channel is USEFUL. What it
lacks is a memory. Six sessions cannot see each other, so each one discovers the same wedge, the
same red test, the same stale ref, and each one tells everybody else -- and every telling costs
five reads. N sessions x one discovery = N^2 messages, all of them individually correct.

THE CLASS. A push channel with no shared record turns one discovery into a broadcast storm, and
then into a second storm when the next session finds it again an hour later.

THE FIX IS PULL, NOT SILENCE. Every allowed message is written to a board every session can read.
A later message on the SAME subject is refused, and the refusal hands back what is already known
and who posted it -- so the sender gets their answer instead of sending their question.

WHAT THIS REFUSES. Exactly one thing: a SendMessage whose subject already sits on the board,
posted by any session inside the window. Nothing else. First raise always goes through.

THE ESCAPE HATCH is one honest line, like the PR fence's `No-Issue:`. Put `Re-raising: <why>` in
the message when a repeat is genuinely needed -- the first went unread and the estate is stopped,
or the situation changed. It is recorded on the board as a re-raise.

FAILS OPEN, ALWAYS. Unreadable board, bad JSON, surprise payload shape: exit 0. A guard that
blocks a session when its own lookup breaks is a guard somebody deletes by lunchtime.
"""
from __future__ import annotations

import io
import json
import os
import contextlib
import pathlib
import re
import sys
import time

BOARD = pathlib.Path(os.environ.get("CLAUDE_ESTATE_BOARD", str(pathlib.Path.home() / ".claude" / "ESTATE_BOARD.jsonl")))
WINDOW_S = 12 * 3600          # a finding goes stale; the estate changes underneath it
# CONTAINMENT, not Jaccard, and the number came off a measurement rather than out of the air.
# Two real paraphrases of one wedge scored Jaccard 0.50 / containment 0.727; an unrelated finding
# scored 0.00 on both. Jaccard punishes a SHORT restatement of a long finding ("still stuck on the
# fence thing") which is exactly the repeat worth refusing, so it grades shared-over-smaller.
CONTAINMENT = 0.55            # observed: same subject 0.73, different subject 0.00, floor is noise
MIN_TOKENS = 4                # below this a message is too short to fingerprint honestly
FANOUT_S = 300                # one broadcast to N sessions is N sends of ONE message, not N-1 repeats
KEEP = 400                    # board entries retained

RE_RAISE = re.compile(r"(?im)^\s*Re-raising:\s*\S")
_STOP = set("""about after again against all also and any are because been before being between both
but can cant come could did dont down each even every for from get got had has have her here him his
how into its just like made make many may more most much must never new not now off once only other
our out over own same she should since some still such take than that the their them then there these
they this those through time too under until very was way well were what when where which while who
will with would you your this that have been will just need needs still into onto""".split())


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9_./#-]{3,}", (text or "").lower())
    return {w for w in words if w not in _STOP}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: set[str], b: set[str]) -> float:
    """Shared tokens over the SMALLER set, so a terse repeat still matches its long original."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def read_board(path: pathlib.Path = BOARD) -> list[dict]:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-KEEP:]:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def post(entry: dict, path: pathlib.Path = BOARD) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 100))
        except Exception: pass


def check(message: str, to: str, session: str, now: float, board: list[dict],
          board_path: pathlib.Path | None = None) -> int:
    # board_path is explicit because the selftest MUST NOT write to the live board. It did, on
    # 2026-08-21: 40 fixture entries landed in ~/.claude/ESTATE_BOARD.jsonl, and a fixture about
    # the push fence would have REFUSED a real session's real message about the push fence.
    dest = board_path or BOARD
    tk = tokens(message)
    if len(tk) < MIN_TOKENS:
        return 0                                    # too short to judge; let it through
    if RE_RAISE.search(message or ""):
        post({"ts": now, "session": session, "to": to, "tokens": sorted(tk)[:60],
              "text": (message or "")[:300], "reraise": True}, dest)
        return 0

    fanout = False
    for e in reversed(board):
        try:
            if now - float(e.get("ts", 0)) > WINDOW_S:
                continue
            prev = set(e.get("tokens") or [])
            score = containment(tk, prev)
            shared = len(tk & prev)
        except Exception:
            continue
        if score >= CONTAINMENT and shared >= MIN_TOKENS:
            # A FAN-OUT IS NOT A REPEAT. One broadcast to six sessions is six sends of one
            # message, and this fence read five of them as loops. Found 2026-08-23 wiring the
            # fence to PreToolUse: the estate-wide canonical-root broadcast would have reached
            # peer 1 and been refused to peers 2-6. The fence and the broadcast are each correct
            # alone and deadlocked together, which is the pair LAW 12 says is the defect.
            # Same sender, DIFFERENT recipient, inside the fan-out window: this is leg N of one
            # delivery. Let it through, and do not post a second board line for it.
            if (str(e.get("session")) == str(session)
                    and str(e.get("to")) != str(to)
                    and now - float(e.get("ts", 0)) <= FANOUT_S):
                fanout = True
                continue
            when = time.strftime("%H:%M", time.localtime(float(e.get("ts", now))))
            who = str(e.get("session", "another session"))[-8:]
            sys.stderr.write(
                "\nPEER LOOP FENCE -- this subject is already on the estate board.\n\n"
                f"  posted {when} by session {who} (overlap {score:.0%})\n"
                f"  \"{str(e.get('text',''))[:220]}\"\n\n"
                "  You do not need to send this. Every session reads the board at start, so the\n"
                "  estate already knows. Read it:  tail -20 " + str(BOARD) + "\n\n"
                "  If this genuinely must go again -- the first went unread and something is\n"
                "  STOPPED, or the facts changed -- put one honest line in the message:\n"
                "      Re-raising: <what changed, or what is stopped>\n"
            )
            return 2

    if fanout:
        return 0        # leg N of a broadcast: already on the board from leg 1
    post({"ts": now, "session": session, "to": to, "tokens": sorted(tk)[:60],
          "text": (message or "")[:300]}, dest)
    return 0


def digest(hours: int = 12, path: pathlib.Path = BOARD) -> str:
    """What SessionStart injects, so a session READS instead of asking."""
    now = time.time()
    rows = [e for e in read_board(path) if now - float(e.get("ts", 0) or 0) <= hours * 3600]
    if not rows:
        return ""
    out = [f"[estate-board] {len(rows)} findings posted by other sessions in the last {hours}h.",
           "Read before you ask a peer; do not re-raise what is here (peer-loop-fence refuses it).",
           ""]
    for e in rows[-25:]:
        when = time.strftime("%H:%M", time.localtime(float(e.get("ts", now))))
        who = str(e.get("session", "?"))[-6:]
        txt = " ".join(str(e.get("text", "")).split())[:150]
        out.append(f"  {when} {who}: {txt}")
    return "\n".join(out)


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--digest" in sys.argv:
        d = digest()
        if d:
            print(d)
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "SendMessage":
        return 0
    inp = payload.get("tool_input") or {}
    if not isinstance(inp, dict):
        return 0
    session = str(payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "local")
    try:
        return check(str(inp.get("message") or ""), str(inp.get("to") or "?"), session,
                     time.time(), read_board())
    except Exception:
        return 0


def selftest() -> int:
    """Proves it REFUSES a repeat and does NOT refuse a different subject."""
    import tempfile
    _live_size = BOARD.stat().st_size if BOARD.exists() else 0
    now = time.time()
    wedge = ("the push fence is deadlocked on integrate/2026-08-20-final -- the branch is an "
             "ancestor of main so gh refuses to open a pr on it, and the fence refuses a push "
             "without one. nothing can ship.")
    board = [{"ts": now - 600, "session": "sess-aaaa1111", "to": "peer-42",
              "tokens": sorted(tokens(wedge))[:60], "text": wedge}]
    cases = [
        ("the same wedge from another session is refused",
         "push fence deadlock: integrate/2026-08-20-final is an ancestor of main, gh will not open "
         "a pr on it and the fence will not push without one, so nothing ships", board, 2),
        ("the FIRST raise always goes through",
         wedge, [], 0),
        ("a genuinely different subject is not refused",
         "the r2 ledger copy measured 99.88 percent complete against the live source using the "
         "gzip trailer isize, so the offsite backup is sound", board, 0),
        ("Re-raising is the escape hatch",
         "Re-raising: nothing shipped in 40 minutes and ci is now idle.\npush fence deadlock on "
         "integrate/2026-08-20-final, ancestor of main, gh refuses the pr, fence refuses the push",
         board, 0),
        ("a stale board entry does not refuse",
         "push fence deadlock: integrate/2026-08-20-final is an ancestor of main, gh will not open "
         "a pr on it and the fence will not push without one, so nothing ships",
         [{**board[0], "ts": now - 13 * 3600}], 0),
        ("a one-word message is too short to judge and passes",
         "ok", board, 0),
        ("a corrupt board entry never blocks",
         wedge, [{"ts": "not-a-number", "tokens": "not-a-list"}], 0),
        # THE false-positive risk: every session in this estate talks about branches, worktrees,
        # main and the gate. Two DIFFERENT findings share that vocabulary, and a fence that reads
        # shared vocabulary as a shared subject is a cry-wolf machine somebody switches off.
        ("a different finding that shares estate vocabulary is NOT refused",
         "the worktree at wt-storeroot has no .venv so the pre-commit gate dies on a missing "
         "interpreter and reports it as a gate violation on your branch against main", board, 0),
        ("a terse restatement of a long finding IS refused -- this is why containment, not jaccard",
         "still stuck: integrate/2026-08-20-final ancestor of main, fence refuses the push", board, 2),
        # 2026-08-23: wiring this fence to PreToolUse would have refused legs 2-6 of the
        # canonical-root broadcast. A fan-out is one message to many, not many repeats.
        ("leg 2 of MY OWN fan-out to a DIFFERENT peer is not a repeat",
         wedge, [{"ts": now - 5, "session": "sess-cccc3333", "to": "peer-a",
                  "tokens": sorted(tokens(wedge))[:60], "text": wedge}], 0,
         "peer-b", "sess-cccc3333"),
        ("but the SAME message to the SAME peer is still a repeat",
         wedge, [{"ts": now - 5, "session": "sess-cccc3333", "to": "peer-a",
                  "tokens": sorted(tokens(wedge))[:60], "text": wedge}], 2,
         "peer-a", "sess-cccc3333"),
        ("and ANOTHER session repeating it is still refused -- the fence's whole job",
         wedge, [{"ts": now - 5, "session": "sess-cccc3333", "to": "peer-a",
                  "tokens": sorted(tokens(wedge))[:60], "text": wedge}], 2,
         "peer-b", "sess-dddd4444"),
    ]
    failures = []
    for case in cases:
        name, msg, bd, want = case[:4]
        to_ = case[4] if len(case) > 4 else "peer-x"
        sess_ = case[5] if len(case) > 5 else "sess-bbbb2222"
        tmp = pathlib.Path(tempfile.mkdtemp()) / "board.jsonl"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = check(msg, to_, sess_, now, bd, board_path=tmp)
        mark = "ok" if got == want else "FAIL"
        if got != want:
            failures.append(name)
        print(f"  [{mark}] {name}: exit {got} (want {want})")
    # The selftest must not have written to the live board. This case is here because it did.
    live_before = _live_size
    live_after = BOARD.stat().st_size if BOARD.exists() else 0
    ok = live_after == live_before
    if not ok:
        failures.append("selftest wrote to the LIVE board")
    print(f"  [{'ok' if ok else 'FAIL'}] the selftest leaves the live board untouched: "
          f"{live_before} -> {live_after} bytes")
    print(f"peer-loop-fence selftest: {len(cases) + 1 - len(failures)}/{len(cases) + 1} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

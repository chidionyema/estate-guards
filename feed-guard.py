#!/usr/bin/env python3
"""feed-guard: every session writes a six-line handoff to ~/.estate/feed.md every 30 minutes.

Founder, 2026-08-25 (R33): "ok lets adopt this but every 30 minutes not 4 hours ... ensure
everyone is permanently doing this without me asking ... this way even if agent sessions die,
we can recover easily." The feed is the persistent brain across sessions: a session that
dies has left its last state there, and a new session that starts with "Status" reads it
instead of asking him.

Hooks (settings.json):
  Stop               blocks the turn when this session's last entry is older than 30 minutes
                     (or absent) until it appends one. Blocks once, like idle-guard.
  SessionStart       injects the last entries, so "Status" is answered from the feed.
  UserPromptSubmit   injects a one-line reminder when the entry is overdue.

Commands:
  feed-guard.py append --session ID --lane NAME  <<'EOF'   (6 lines max, each starts with
      🔴 🟡 🟢 ⚪ or 📍; refused otherwise)
  feed-guard.py status [--n 5]     print the last n entries
  feed-guard.py selftest           proves the guard both ways in a temp feed

Residual: Stop fires only at turn end, so a single 90-minute turn appends late; the prompt
reminder covers the next turn. A session that never stops cannot be reached by any hook.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# The path this guard tells agents to run is the path it actually lives at. It used to be
# the literal "~/.claude/scripts/<name>" -- a directory that does not exist on this machine,
# so every session was instructed, on every turn, to run a script that was not there
# (measured 2026-09-21, session c759de1f). A message that names a path must DERIVE it.
SELF_CMD = os.path.abspath(__file__)

FEED = Path(os.environ.get("ESTATE_FEED") or os.path.expanduser("~/.estate/feed.md"))
INTERVAL_S = 30 * 60
MARKS = ("🔴", "🟡", "🟢", "⚪", "📍")
HEAD = re.compile(r"^## (\S+) · session (\S+) · lane (.*)$")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def entries(feed: Path) -> list[tuple[dt.datetime, str, str, list[str]]]:
    out = []
    if not feed.is_file():
        return out
    cur = None
    for ln in feed.read_text(encoding="utf-8", errors="replace").splitlines():
        m = HEAD.match(ln)
        if m:
            cur = (dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")), m.group(2), m.group(3), [])
            out.append(cur)
        elif cur and ln.strip():
            cur[3].append(ln)
    return out


def last_for(feed: Path, session: str):
    mine = [e for e in entries(feed) if e[1] == session]
    return mine[-1] if mine else None


def overdue(feed: Path, session: str, at: dt.datetime | None = None) -> int | None:
    """Seconds since this session's last entry, or None when it has one inside the interval."""
    at = at or now()
    e = last_for(feed, session)
    if e is None:
        return -1
    age = int((at - e[0]).total_seconds())
    return age if age >= INTERVAL_S else None


def append(feed: Path, session: str, lane: str, body: str, at: dt.datetime | None = None) -> str | None:
    lines = [l.rstrip() for l in body.strip().splitlines() if l.strip()]
    if not lines or len(lines) > 6:
        return f"handoff must be 1 to 6 lines, got {len(lines)}"
    bad = [l for l in lines if not l.startswith(MARKS)]
    if bad:
        return "every line starts with one of 🔴 🟡 🟢 ⚪ 📍; refused: " + bad[0][:60]
    at = at or now()
    feed.parent.mkdir(parents=True, exist_ok=True)
    with feed.open("a", encoding="utf-8") as fh:
        if feed.stat().st_size == 0:
            fh.write("# Estate feed\n\nOne handoff per session per 30 minutes (R33). Newest at the bottom. "
                     "Written by `python3 " + SELF_CMD + f" append`; read with `status`.\n\n")
        fh.write(f"## {at.strftime('%Y-%m-%dT%H:%M:%SZ')} · session {session} · lane {lane}\n" + "\n".join(lines) + "\n\n")
    return None


def block_text(session: str, lane: str, age: int) -> str:
    why = "has no entry in the feed" if age < 0 else f"last wrote to the feed {age // 60} min ago"
    return (f"FEED GUARD (R33): this session {why}; the limit is 30 minutes. Append the handoff now, "
            f"then end the turn:\n"
            f"python3 " + SELF_CMD + f" append --session {session} --lane {lane} <<'EOF'\n"
            f"🔴 Blocked: <what, who unblocks>\n🟡 Active: <issue numbers>\n🟢 Done: <merged, with sha>\n"
            f"⚪ Pending: <founder pick>\n📍 State: <file or URL with the full picture>\nEOF\n"
            f"Six lines at most, each starting with 🔴 🟡 🟢 ⚪ or 📍. Drop lines that are empty.")


def hook(kind: str) -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    session = (payload.get("session_id") or "unknown")[:8]
    lane = Path(payload.get("cwd") or os.getcwd()).name
    if kind == "SessionStart":
        tail = entries(FEED)[-6:]
        if tail:
            body = "\n".join(f"## {e[0].strftime('%Y-%m-%dT%H:%MZ')} · {e[1]} · {e[2]}\n" + "\n".join(e[3]) for e in tail)
            print(f"[feed] LAST {len(tail)} HANDOFFS FROM {FEED} (R33). When the founder says 'Status', summarise these; "
                  f"do not re-measure what they answer.\n{body}")
        print(f"[feed] You write a 6-line handoff here every 30 minutes: python3 " + SELF_CMD + f" append --session {session} --lane {lane}")
        return 0
    age = overdue(FEED, session)
    if age is None:
        return 0
    if kind == "Stop":
        if payload.get("stop_hook_active"):
            return 0
        print(json.dumps({"decision": "block", "reason": block_text(session, lane, age)}))
        return 0
    print(f"[feed] handoff overdue ({'none yet' if age < 0 else f'{age // 60} min'}); append one before this turn ends: "
          f"python3 " + SELF_CMD + f" append --session {session} --lane {lane}")
    return 0


def selftest() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "feed.md"
        t0 = now()
        # must refuse: no entry, then a 7-line body, then a line without a mark
        ok &= overdue(f, "aaaa", t0) == -1
        ok &= append(f, "aaaa", "idp", "\n".join(["🟢 x"] * 7), t0) is not None
        ok &= append(f, "aaaa", "idp", "Done: x", t0) is not None
        # must permit: a fresh 5-line entry, then not overdue at +29 min, overdue at +31
        ok &= append(f, "aaaa", "idp", "🔴 a\n🟡 b\n🟢 c\n⚪ d\n📍 e", t0) is None
        ok &= overdue(f, "aaaa", t0 + dt.timedelta(minutes=29)) is None
        ok &= overdue(f, "aaaa", t0 + dt.timedelta(minutes=31)) == 31 * 60
        # another session is judged on its own entries
        ok &= overdue(f, "bbbb", t0) == -1
        ok &= len(entries(f)) == 1 and entries(f)[0][3] == ["🔴 a", "🟡 b", "🟢 c", "⚪ d", "📍 e"]
    print(f"{'ok  ' if ok else 'FAIL'}  feed-guard selftest: refuses no-entry/7-lines/no-mark, permits a fresh entry, "
          f"overdue at 31 min and not at 29, per session")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("append"); a.add_argument("--session", required=True); a.add_argument("--lane", required=True)
    s = sub.add_parser("status"); s.add_argument("--n", type=int, default=5)
    sub.add_parser("selftest")
    for k in ("Stop", "SessionStart", "UserPromptSubmit"):
        sub.add_parser(k)
    args = ap.parse_args(argv)
    if args.cmd == "append":
        err = append(FEED, args.session[:8], args.lane, sys.stdin.read())
        if err:
            print(f"FAIL  feed-guard: {err}"); return 1
        print(f"ok    feed-guard: handoff appended to {FEED}"); return 0
    if args.cmd == "status":
        for e in entries(FEED)[-args.n:]:
            print(f"## {e[0].strftime('%Y-%m-%dT%H:%MZ')} · {e[1]} · {e[2]}\n" + "\n".join(e[3]) + "\n")
        return 0
    if args.cmd == "selftest":
        return selftest()
    return hook(args.cmd or "Stop")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

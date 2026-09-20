#!/usr/bin/env python3
"""feed-guard: every session writes a six-line handoff to ~/.estate/feed.md every 15 minutes.

Founder, 2026-08-25 (R33): "every 30 minutes ... even if agent sessions die, we can recover
easily." A dead session has left its last state in the feed; "Status" is answered from it.

Hooks: Stop blocks the turn once when this session's entry is older than 15 minutes or absent;
SessionStart injects the last entries; UserPromptSubmit reminds when overdue.
Commands: append --session ID --lane NAME (shape is policy/feed.rego: 8 lines max, 🔴 🟡 🟢 ⚪ 🔧 🔀 📍
marks, TOUCHES and OVERLAP required; a measured 📍 METER line is added, crew#26); status [--n 5];
selftest (both ways, temp feed). Residual: Stop fires only at turn end, so a 90-minute turn appends
late; a session that never stops is reached by no hook. Without opa the shape check is BLIND.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: The command a session must paste back into its own shell, pointed at wherever this guard
#: actually lives. Resolved from __file__ so it is right on any machine, clone or user -- a
#: hardcoded ~/.claude/scripts died with the .claude rename, and a hardcoded home directory
#: dies on the next machine (AGENTS.md 0.3).
SELF = os.path.join(
    os.environ.get("GUARDS_ROOT")
    or os.path.dirname(os.path.abspath(__file__)),
    "feed-guard.py")

from feed_meter import METER_MARK, meter_line  # crew#26 CP-D: a library, not a guard
from feed_publish import (  # noqa: F401  crew#786: the publisher is a library, not a guard
    IDP_REPO,
    STATE_BRANCH,
    publish,
    redact,
    render_public,
)

FEED = Path(os.environ.get("ESTATE_FEED") or os.path.expanduser("~/.estate/feed.md"))
INTERVAL_S = 15 * 60  # crew#786: founder 2026-09-01, every 15 minutes
HOLD_S = (
    2 * 60 * 60
)  # crew#331: a lane is held by whoever wrote on it inside this window
POLICY = (
    Path(__file__).resolve().parent / "policy"
)  # the shape is policy/feed.rego (crew#259); this file only asks OPA
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
            cur = (
                dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")),
                m.group(2),
                m.group(3),
                [],
            )
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


def holders(
    feed: Path, session: str, lane: str, at: dt.datetime | None = None
) -> list[str]:
    """Other sessions that wrote a handoff on this lane inside HOLD_S (crew#331, LANES rule 2)."""
    at = at or now()
    seen: dict[str, None] = {}
    for when, who, ln, _ in entries(feed):
        if ln == lane and who != session and 0 <= (at - when).total_seconds() < HOLD_S:
            seen[who] = None
    return list(seen)


def _normalize_token(tok: str) -> str:
    """Normalise a TOUCHES token: strip quotes/parentheses/trailing punctuation, expand ~."""
    tok = tok.strip().strip("'\"()").rstrip(",;:.) ")
    if tok.startswith("~"):
        tok = str(Path.home()) + tok[1:]
    return tok


def _tokens_from_line(line: str) -> set[str]:
    """Extract tokens (containing /) from a TOUCHES line, normalised."""
    if not line.startswith("🔧 TOUCHES:"):
        return set()
    after = line[len("🔧 TOUCHES:") :].strip()
    if after.lower() == "none" or not after:
        return set()
    # split by whitespace, comma, semicolon
    parts = re.split(r"[\s,;]+", after)
    return {_normalize_token(p) for p in parts if "/" in p}


def touch_clashes(
    feed: Path, session: str, body: str, at: dt.datetime | None = None
) -> list[tuple[str, str]]:
    """crew#786: Find TOUCHES clashes with other sessions' entries inside HOLD_S.

    Returns (peer_session, token) pairs where the new body touches a token that a
    different session touched inside the last HOLD_S, and the new body's OVERLAP
    does not name that peer session (first 8 chars).
    """
    at = at or now()
    # parse new body's TOUCHES and OVERLAP
    new_lines = [ln.rstrip() for ln in body.strip().splitlines() if ln.strip()]
    new_touches = set()
    new_overlap = ""
    for ln in new_lines:
        if ln.startswith("🔧 TOUCHES:"):
            new_touches = _tokens_from_line(ln)
        elif ln.startswith("🔀 OVERLAP:"):
            new_overlap = ln

    out = []
    # look at entries from other sessions inside HOLD_S
    for when, who, _ln, lines in entries(feed):
        if who == session:
            continue
        if (at - when).total_seconds() >= HOLD_S:
            continue
        # collect tokens from this peer's TOUCHES line
        peer_touches = set()
        for line in lines:
            peer_touches |= _tokens_from_line(line)
        # find intersection with new body's tokens
        for tok in peer_touches & new_touches:
            # check if peer's 8-char id is named in OVERLAP
            peer_prefix = who[:8]
            if peer_prefix not in new_overlap:
                out.append((who, tok))
    return out


def collisions(
    feed: Path, at: dt.datetime | None = None
) -> list[tuple[dt.datetime, str, str, str]]:
    """Report mode for crew#331: every handoff whose lane another session held and whose OVERLAP did not name it."""
    at = at or now()
    out = []
    for when, who, ln, lines in entries(feed):
        held = [
            h
            for h in holders(feed, who, ln, when)
            if not any(line.startswith("🔀") and h in line for line in lines)
        ]
        if held and (at - when).total_seconds() < 24 * 3600:
            out.append((when, who, ln, ",".join(held)))
    return out


def append(
    feed: Path,
    session: str,
    lane: str,
    body: str,
    at: dt.datetime | None = None,
    meter: str | None = None,
) -> str | None:
    lines = [line.rstrip() for line in body.strip().splitlines() if line.strip()]
    if not any(line.startswith(METER_MARK) for line in lines) and len(lines) < 8:
        lines.append(meter if meter is not None else meter_line(timeout=5.0))
    clashes = touch_clashes(feed, session, body, at)
    denied = denials(lines, session, lane, holders(feed, session, lane, at), clashes)
    if denied:
        return "; ".join(denied)
    if not any(line.startswith("📎 FACTS:") for line in lines):
        # crew#629 CP4, report-only: facts are shared through the ticket block, not re-found per session.
        print(
            "note: no 📎 FACTS: line; point at the ticket's Infra facts block (bin/idp-ticket-facts) next time (crew#629 CP4)",
            file=sys.stderr,
        )
    at = at or now()
    feed.parent.mkdir(parents=True, exist_ok=True)
    with feed.open("a", encoding="utf-8") as fh:
        if feed.stat().st_size == 0:
            fh.write(
                "# Estate feed\n\nOne handoff per session per 15 minutes (R33). Newest at the bottom. "
                f"Written by `python3 {SELF} append`; read with `status`.\n\n"
            )
        fh.write(
            f"## {at.strftime('%Y-%m-%dT%H:%M:%SZ')} · session {session} · lane {lane}\n"
            + "\n".join(lines)
            + "\n\n"
        )
    # Publish to IDP state branch (unless disabled)
    if not os.environ.get("FEED_GUARD_NO_PUBLISH"):
        try:
            receipt = publish(feed, at)
        except Exception as exc:  # the handoff is on disk; publishing is best effort
            receipt = f"BLIND feed-publish: {type(exc).__name__}: {exc}"[:200]
        print(receipt)
    return None


def denials(
    lines: list[str],
    session: str = "",
    lane: str = "",
    held_by: list[str] | None = None,
    clashes: list[tuple[str, str]] | None = None,
) -> list[str]:
    opa = shutil.which(
        "opa"
    )  # the shape lives in policy/feed.rego; this decides nothing itself
    if not opa:
        return ["BLIND: opa is not installed, the handoff shape was not checked"]
    out = subprocess.run(  # noqa: S603  argv list, no shell, our own paths
        [
            opa,
            "eval",
            "--format",
            "json",
            "--ignore",
            "fixtures",
            "--ignore",
            "*.json",
            "--data",
            str(POLICY),
            "--stdin-input",
            "data.feed.deny",
        ],
        input=json.dumps(
            {
                "lines": lines,
                "session": session,
                "lane": lane,
                "holders": held_by or [],
                "clashes": [{"session": s, "token": t} for s, t in (clashes or [])],
            }
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if out.returncode != 0:
        return ["BLIND: opa eval failed: " + out.stderr.strip()[:120]]
    return sorted(json.loads(out.stdout)["result"][0]["expressions"][0]["value"])


def block_text(session: str, lane: str, age: int) -> str:
    why = (
        "has no entry in the feed"
        if age < 0
        else f"last wrote to the feed {age // 60} min ago"
    )
    return (
        f"FEED GUARD (R33): this session {why}; the limit is 15 minutes. Append the handoff now, "
        f"then end the turn:\n"
        f"python3 {SELF} append --session {session} --lane {lane} <<'EOF'\n"
        f"🔴 Blocked: <what, who unblocks>\n🟡 Active: <issue numbers>\n🟢 Done: <merged, with sha>\n"
        f"⚪ Pending: <founder pick>\n🔧 TOUCHES: <files, services, ports, secrets you will change in 2h, or none>\n"
        f"🔀 OVERLAP: <issue numbers another session also touches, or none>\n📎 FACTS: <URL of the ticket's Infra facts block, from bin/idp-ticket-facts, or none>\n📍 State: <file or URL>\nEOF\n"
        f"Nine lines at most; TOUCHES and OVERLAP are required (crew#259, policy/feed.rego); FACTS is expected (crew#629 CP4)."
    )


# crew#403 CP6: the founder asked three times on 2026-08-27 what is planned, what is blocking and
# when. The answer is a generated page, never a session's memory. NEXT_PAGE is the hourly render's
# docs/NEXT.md (idp bin/estate-next); NEXT_URL is where it is published.
NEXT_PAGE = Path(
    os.environ.get("ESTATE_NEXT_PAGE")
    or Path.home() / "dev" / "code" / ".idp-state" / "docs" / "NEXT.md"
)
NEXT_URL = (
    os.environ.get("ESTATE_NEXT_URL")
    or f"https://github.com/{IDP_REPO}/blob/{STATE_BRANCH}/docs/NEXT.md"
)
STATUS_RE = re.compile(  # tolerant stems: the founder typed "capablities", "capalilities", "nd when to epect", "whats next"
    r"\b(status|capa\w*|progress|what'?s? next|what (is|are) (planned|outstanding|blocking)|when (to|can i|do you) \w*pect|eta)\b",
    re.I,
)


def next_answer(prompt: str, page: Path = NEXT_PAGE, url: str = NEXT_URL) -> str | None:
    """The text to inject when the founder asks about status, capabilities, progress or when.
    None when the prompt is not that question. A missing page is BLIND and says so (LAW 28)."""
    if not STATUS_RE.search(prompt or ""):
        return None
    if not page.is_file():
        return (
            f"[next] The founder is asking about status/capabilities/progress/when (crew#403 CP6). Answer from the "
            f"generated page, never from memory: {url}. BLIND: no local copy at {page}; quote the URL and say the page is the answer."
        )
    lines = page.read_text(encoding="utf-8", errors="ignore").splitlines()
    bar = [
        line
        for line in lines
        if line.startswith("- Checkpoints:")
        or line.startswith("- When:")
        or line.startswith("- Lanes reporting:")
    ]
    red = [
        line
        for line in lines
        if line.startswith("| BLOCKING |") or line.startswith("| ACTIVE |")
    ][:12]
    return (
        "[next] The founder is asking about status/capabilities/progress/when (crew#403 CP6). Answer from the generated "
        f"page, never from memory. Quote its URL and its bar; the table is the plan, the Expect column is the date:\n{url}\n"
        + "\n".join(bar + red)
    )


# crew#508 CP5, founder 2026-08-27: "when I say science I need to see progress across all lanes
# simultaneously". A science/research/data/ML prompt answers from the science page's Lanes table
# and the research grade's Outward/Inward rows, never from memory. Both are regenerated hourly.
SCIENCE_DIR = Path(
    os.environ.get("ESTATE_SCIENCE_DIR")
    or Path.home() / "dev" / "code" / "crew" / "docs" / "science"
)
SCIENCE_URL = (
    os.environ.get("ESTATE_SCIENCE_URL")
    or "https://github.com/chidionyema/crew/blob/main/docs/science"
)
SCIENCE_RE = re.compile(
    r"\b(scien\w*|rese\w*ch\w*|data (science|lane)|machine learning|ml lane|lanes?|foresight|inward|outward)\b",
    re.I,
)


def science_answer(
    prompt: str, folder: Path = SCIENCE_DIR, url: str = SCIENCE_URL
) -> str | None:
    """The Lanes table and the Outward/Inward grades when the founder asks about science; None otherwise."""
    if not SCIENCE_RE.search(prompt or ""):
        return None
    head = f"[science] The founder is asking about science/research/lanes (crew#508 CP5). Answer from the generated pages, never from memory: {url}/SHOWCASE.md and {url}/RESEARCH-GRADE.md."
    out = [head]
    for name, keep in (
        (
            "SHOWCASE.md",
            lambda ln: (
                ln.startswith("| ") and ("BLIND" in ln or "GAP" in ln or "ELITE" in ln)
            ),
        ),
        (
            "RESEARCH-GRADE.md",
            lambda ln: (
                ln.startswith("| Outward")
                or ln.startswith("| Inward")
                or ln.startswith("| RED")
            ),
        ),
    ):
        page = folder / name
        if not page.is_file():
            out.append(
                f"BLIND: no local copy of {name} at {page}; quote the URL and say the page is the answer."
            )
            continue
        rows = [
            ln
            for ln in page.read_text(encoding="utf-8", errors="ignore").splitlines()
            if keep(ln)
        ]
        out.append(f"{name}:\n" + "\n".join(rows[:14]))
    return "\n".join(out)


def hook(kind: str) -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    session = (payload.get("session_id") or "unknown")[:8]
    lane = Path(payload.get("cwd") or os.getcwd()).name
    if kind == "UserPromptSubmit":
        for ans in (
            next_answer(payload.get("prompt") or ""),
            science_answer(payload.get("prompt") or ""),
        ):
            if ans:
                print(ans)
    if kind == "SessionStart":
        tail = entries(FEED)[-6:]
        if tail:
            body = "\n".join(
                f"## {e[0].strftime('%Y-%m-%dT%H:%MZ')} · {e[1]} · {e[2]}\n"
                + "\n".join(e[3])
                for e in tail
            )
            print(
                f"[feed] LAST {len(tail)} HANDOFFS FROM {FEED} (R33). When the founder says 'Status', summarise these; "
                f"do not re-measure what they answer.\n{body}"
            )
        print(
            f"[feed] You write a 6-line handoff here every 15 minutes: python3 {SELF} append --session {session} --lane {lane}"
        )
        return 0
    age = overdue(FEED, session)
    if age is None or (kind == "Stop" and payload.get("stop_hook_active")):
        return 0
    if kind == "Stop":
        print(
            json.dumps({"decision": "block", "reason": block_text(session, lane, age)})
        )
        return 0
    print(
        f"[feed] handoff overdue ({'none yet' if age < 0 else f'{age // 60} min'}); append one before this turn ends: "
        f"python3 {SELF} append --session {session} --lane {lane}"
    )
    return 0


def selftest() -> int:
    ok = True
    # Disable publishing during selftest
    os.environ["FEED_GUARD_NO_PUBLISH"] = "1"
    globals()["meter_line"] = lambda timeout=None: (
        f"{METER_MARK} selftest"
    )  # the real meter takes ~26 s
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "feed.md"
        t0 = now()
        # shape cases are policy/feed_test.rego; here: no entry refused, the old form refused
        ok &= overdue(f, "aaaa", t0) == -1
        ok &= append(f, "aaaa", "idp", "🔴 a\n🟡 b\n🟢 c\n⚪ d\n📍 e", t0) is not None
        # must permit: a fresh entry with TOUCHES/OVERLAP, then not overdue at +14 min, overdue at +16
        good = "🔴 a\n🟡 b\n🟢 c\n⚪ d\n🔧 TOUCHES: none\n🔀 OVERLAP: none\n📍 e"
        ok &= append(f, "aaaa", "idp", good, t0) is None
        ok &= overdue(f, "aaaa", t0 + dt.timedelta(minutes=14)) is None
        ok &= overdue(f, "aaaa", t0 + dt.timedelta(minutes=16)) == 16 * 60
        # another session is judged on its own entries
        ok &= overdue(f, "bbbb", t0) == -1
        ok &= len(entries(f)) == 1 and entries(f)[0][3] == good.split("\n") + [
            f"{METER_MARK} selftest"
        ]
        # crew#786: TOUCHES clash - peer touched ~/dev/code/idp/bin/x 10 min ago, new body touches same path
        # Use a lane that's NOT held by aaaa to isolate the TOUCHES clash test
        t1 = t0 + dt.timedelta(minutes=10)
        peer_body = "🔴 a\n🟡 b\n🟢 c\n⚪ d\n🔧 TOUCHES: ~/dev/code/idp/bin/x\n🔀 OVERLAP: none\n📍 e"
        ok &= append(f, "eeee", "peer-lane", peer_body, t1) is None
        # New entry from bbbb touching same path in a free lane, OVERLAP does NOT name eeee -> refused
        new_touches = "🔴 a\n🟡 b\n🟢 c\n⚪ d\n🔧 TOUCHES: ~/dev/code/idp/bin/x\n🔀 OVERLAP: none\n📍 e"
        err = append(f, "bbbb", "free-lane", new_touches, t1)
        ok &= err is not None
        ok &= "crew#786" in (err or "")
        # Same but OVERLAP names eeee (first 8 chars) -> allowed
        overlap_names = new_touches.replace("🔀 OVERLAP: none", "🔀 OVERLAP: eeee")
        ok &= append(f, "bbbb", "free-lane", overlap_names, t1) is None
        # crew#331: bbbb may not take lane idp while aaaa holds it, unless OVERLAP names aaaa; after 2h it is free
        ok &= holders(f, "bbbb", "idp", t1) == ["aaaa"]
        ok &= append(f, "bbbb", "idp", good, t1) is not None
        names = good.replace("🔀 OVERLAP: none", "🔀 OVERLAP: aaaa owns the drill")
        ok &= append(f, "bbbb", "idp", names, t1) is None
        ok &= append(f, "cccc", "other-lane", good, t1) is None
        ok &= holders(f, "dddd", "idp", t0 + dt.timedelta(hours=3)) == []
        ok &= append(f, "dddd", "idp", good, t0 + dt.timedelta(hours=3)) is None
        ok &= len(collisions(f, t0 + dt.timedelta(hours=3))) == 0
    print(
        f"{'ok  ' if ok else 'FAIL'}  feed-guard selftest: refuses no-entry and the old form (shape: policy/feed.rego), permits the new form, overdue at 16 min and not at 14, per session; refuses a held lane unless OVERLAP names the holder (crew#331); refuses TOUCHES clash unless OVERLAP names the peer (crew#786)"
    )
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("append")
    a.add_argument("--session", required=True)
    a.add_argument("--lane", required=True)
    s = sub.add_parser("status")
    s.add_argument("--n", type=int, default=5)
    sub.add_parser("selftest")
    sub.add_parser("sweep")
    for k in ("Stop", "SessionStart", "UserPromptSubmit"):
        sub.add_parser(k)
    args = ap.parse_args(argv)
    if args.cmd == "append":
        err = append(FEED, args.session[:8], args.lane, sys.stdin.read())
        print(
            f"FAIL  feed-guard: {err}"
            if err
            else f"ok    feed-guard: handoff appended to {FEED}"
        )
        return 1 if err else 0
    if args.cmd == "status":
        for e in entries(FEED)[-args.n :]:
            print(
                f"## {e[0].strftime('%Y-%m-%dT%H:%MZ')} · {e[1]} · {e[2]}\n"
                + "\n".join(e[3])
                + "\n"
            )
        return 0
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "sweep":
        rows = collisions(FEED)
        for when, who, ln, held in rows:
            print(
                f"{when.strftime('%Y-%m-%dT%H:%MZ')} {who} lane {ln!r} held by {held}, OVERLAP names none of them"
            )
        print(
            f"sweep feed-guard: {len(rows)} handoff(s) in the last 24h a lane-hold rule would have refused (crew#331)"
        )
        return 0
    return hook(args.cmd or "Stop")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

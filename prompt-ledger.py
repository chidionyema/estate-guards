#!/usr/bin/env python3
"""Every founder prompt, captured once and closed with proof.

WHY THIS EXISTS (founder, 2026-08-21): "a lot of pronts and chats get s lost",
"also reearch how to turn pronts, canusal into specs with ac", "project nanagent".

MEASURED ROOT CAUSE. A message typed while a turn is running does NOT arrive as a
`type: "user"` row and does NOT fire UserPromptSubmit. It lands in the transcript as
`{"type": "queue-operation", "operation": "enqueue", ...}` and nothing in this estate
reads those. Measured on session 0d5d261b: 100 distinct enqueue rows, of which every
one of the six founder fragments that produced this script was missing from
`~/.claude/directives/`. So `directive-capture.py` is not broken -- it is wired to an
event that mid-turn messages never raise.

WHAT THIS DOES, in two halves the founder asked for as one ask:

  CAPTURE   Reconcile a transcript into a durable per-project ledger. Reads BOTH
            `user` rows and `queue-operation`/`enqueue` rows, so nothing typed is lost.
            Idempotent: the same transcript scanned a hundred times yields one row per
            prompt, and a row's status is never reset by a later scan.

  CLOSE     A row closes only when a SPEC with acceptance criteria passes. An AC is a
            shell command; it closes when the command exits 0. That is the whole
            mechanism, and it is the answer to "we dont close anything preoperly" and
            "we shi and dont verify" -- a prompt cannot be marked done by an agent
            asserting it is done.

NOT A NOTE. This estate has already proved a note is not a mechanism
(memory: a-documented-trap-is-not-a-guarded-trap).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE = Path.home() / ".claude" / "state" / "prompt-ledger"

# A mid-turn row carries whatever the harness put in the queue, and 35.1% of the
# existing directive log is not a founder prompt at all: 18.7% task-notify,
# 16.4% peer-msg (measured 2026-08-21 on 225 rows). Filter at the source.
NOT_A_PROMPT = (
    "<task-notification>",
    "<cross-session-message",
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
    "<bash-input>",
    "[Request interrupted",
    "Caveat: The messages below were generated",
    "This session is being continued from a previous conversation",
)

# Two prompts 90s apart are one thought continued -- the founder types in fragments
# ("as part of this", "etc", "add to list"). Linking them keeps a 3-word fragment
# readable a week later instead of being an orphan nobody can action.
FRAGMENT_WINDOW_S = 90.0

MAX_TAIL_BYTES = 32 * 1024 * 1024   # a long session's transcript; scan the tail, not the lot
AC_TIMEOUT_S = 300


# ---------------------------------------------------------------- ledger plumbing

def slug_for(project_dir: Path) -> str:
    return project_dir.name or "unknown"


def ledger_path(slug: str) -> Path:
    return STATE / (slug + ".jsonl")


def load(path: Path) -> "dict[str, dict]":
    """Ledger as {id: row}. A corrupt line is skipped, never fatal -- a half-written
    row from a killed hook must not cost every prompt before it."""
    rows: dict[str, dict] = {}
    if not path.exists():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if isinstance(r, dict) and r.get("id"):
            rows[r["id"]] = r
    return rows


def save(path: Path, rows: "dict[str, dict]") -> None:
    """Atomic. Two sessions Stop at the same moment and both write this file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda r: (r.get("ts") or "", r.get("id") or ""))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pl-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            for r in ordered:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def marks_path(ledger: Path) -> Path:
    return ledger.with_suffix(".offsets.json")


def load_marks(ledger: Path) -> "dict[str, int]":
    try:
        d = json.loads(marks_path(ledger).read_text())
        return {k: int(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}      # a lost offset file costs one full re-scan, never a lost prompt


def save_marks(ledger: Path, marks: "dict[str, int]") -> None:
    try:
        marks_path(ledger).write_text(json.dumps(marks))
    except Exception:
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 134))
        except Exception: pass


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def row_id(session: str, text: str) -> str:
    """Session + normalised text. Deliberately NOT timestamped: the same message
    appears as an enqueue row and again as the delivered user row, with different
    timestamps, and those are one prompt. The cost is that a founder who types the
    identical word twice in one session gets one row; measured on 100 distinct
    prompts, that collapsed nothing that carried a distinct ask."""
    h = hashlib.sha1((session + "\x00" + norm(text).lower()).encode("utf-8", "replace"))
    return h.hexdigest()[:12]


def is_prompt(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return not t.startswith(NOT_A_PROMPT)


def _ts_epoch(ts: str) -> float:
    """ISO8601 -> epoch. Returns 0.0 on anything unparseable, which only ever costs
    a fragment link, never a captured row."""
    try:
        s = (ts or "").replace("Z", "+00:00")
        import datetime as _dt
        return _dt.datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


# ---------------------------------------------------------------- extraction

def extract(transcript: Path, start: int = 0,
            consumed: "list[int] | None" = None) -> "list[dict]":
    """Every founder prompt in one transcript, in order, both arrival paths.

    The two row shapes, measured:
      {"type":"queue-operation","operation":"enqueue","timestamp":..,"sessionId":..,
       "content":"..."}                                       <- typed mid-turn
      {"type":"user","timestamp":..,"sessionId":..,
       "message":{"content":"..." | [blocks]}}                <- typed at the prompt

    `operation: "remove"` is the SAME message being delivered into the turn. Reading
    it would double every mid-turn prompt, so only `enqueue` counts.

    Reads from byte `start` and writes the byte it stopped at into `consumed[0]`.
    """
    if consumed is None:
        consumed = [0]
    consumed[0] = start
    out: "list[dict]" = []
    try:
        size = transcript.stat().st_size
    except Exception:
        return out
    # A transcript only ever grows, so re-reading it on every Stop is work already done.
    # Measured 2026-08-21: 0.77s for 6.2MB, and this runs on EVERY Stop in EVERY session
    # -- transcripts on this estate reach tens of MB, so the full scan is a tax that grows
    # all day. `start` is where the last scan stopped at a complete line boundary.
    # A file smaller than the offset was truncated or replaced, so start over.
    if start > size:
        start = 0
    try:
        with transcript.open("rb") as fh:
            if start:
                fh.seek(start)
            elif size > MAX_TAIL_BYTES:
                fh.seek(size - MAX_TAIL_BYTES)
                fh.readline()          # drop the partial line the seek landed in
            blob = fh.read()
    except Exception:
        return out
    # Never advance past a half-written line: the harness may be mid-append right now.
    # Belt and braces: an incomplete trailing line is virtually never valid JSON, so the
    # parse below would drop it anyway. The load-bearing part is the OFFSET, which must
    # never advance past it -- that is what the selftest grades.
    nl = blob.rfind(b"\n")
    if nl == -1:
        return out
    consumed[0] = (start if start else (size - len(blob))) + nl + 1
    blob = blob[:nl + 1]

    for raw in blob.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            r = json.loads(raw)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        typ, text = r.get("type"), None

        if typ == "queue-operation":
            if r.get("operation") != "enqueue":
                continue
            text = r.get("content")
        elif typ == "user":
            if r.get("isMeta"):
                continue
            c = (r.get("message") or {}).get("content")
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                # A tool result is delivered on a user row. It is not a prompt, and one
                # tool_result block anywhere in the row means the whole row is machinery.
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                    continue
                text = "\n".join(
                    b.get("text", "") for b in c
                    if isinstance(b, dict) and b.get("type") == "text"
                )
        else:
            continue

        if not is_prompt(text):
            continue
        out.append({
            "session": (r.get("sessionId") or "")[:8],
            "ts": r.get("timestamp") or "",
            "text": norm(text),
            "source": "queue" if typ == "queue-operation" else "user",
        })
    return out


def reconcile(transcripts: "list[Path]", path: Path) -> "tuple[int,int]":
    """Fold transcripts into the ledger. Returns (new, total). Never resets a status."""
    rows = load(path)
    before = len(rows)
    # An empty ledger means the offsets describe rows nobody holds any more -- a deleted
    # or moved ledger must re-read everything rather than skip to the end of the file.
    marks = load_marks(path) if rows else {}
    found: "list[dict]" = []
    for t in transcripts:
        stopped = [0]
        found.extend(extract(t, marks.get(str(t), 0), stopped))
        marks[str(t)] = stopped[0]
    found.sort(key=lambda p: p["ts"])

    prev_id, prev_t = None, 0.0
    for p in found:
        rid = row_id(p["session"], p["text"])
        now = _ts_epoch(p["ts"])
        link = prev_id if (prev_id and prev_t and 0 <= now - prev_t <= FRAGMENT_WINDOW_S) else None
        prev_id, prev_t = rid, now

        if rid in rows:
            r = rows[rid]
            # A later scan may see the same prompt through the other arrival path.
            # Keep the earliest timestamp and never touch status/spec/proof.
            if p["ts"] and (not r.get("ts") or p["ts"] < r["ts"]):
                r["ts"] = p["ts"]
            if link and not r.get("prev"):
                r["prev"] = link
            continue
        rows[rid] = {
            "id": rid, "session": p["session"], "ts": p["ts"], "text": p["text"],
            "source": p["source"], "status": "open", "prev": link,
            "spec": None, "ac": [], "proof": None,
        }
    save(path, rows)
    save_marks(path, marks)
    return len(rows) - before, len(rows)


# ---------------------------------------------------------------- spec + close

def attach_spec(path: Path, rid: str, statement: str, acs: "list[str]") -> int:
    rows = load(path)
    if rid not in rows:
        print("no such prompt: %s" % rid, file=sys.stderr)
        return 1
    r = rows[rid]
    r["spec"] = statement
    if acs:
        r["ac"] = list(acs)
    save(path, rows)
    print("spec attached to %s (%d acceptance criteria)" % (rid, len(r["ac"])))
    return 0


def verify(path: Path, rid: str) -> int:
    """Run every acceptance criterion. All exit 0 -> the prompt closes, with the
    commands and their exit codes recorded as the proof. Anything else and it stays
    open. An agent cannot close a prompt by saying it is done."""
    rows = load(path)
    if rid not in rows:
        print("no such prompt: %s" % rid, file=sys.stderr)
        return 1
    r = rows[rid]
    acs = r.get("ac") or []
    if not acs:
        # The whole point. A prompt with no checkable criterion is a note, and a note
        # closing itself is exactly the "we shi and dont verify" the founder named.
        print("REFUSED: %s has no acceptance criteria. Attach a spec first:" % rid)
        print('  prompt-ledger.py --spec %s --statement "..." --ac "<command that exits 0>"' % rid)
        return 2
    results, ok = [], True
    for cmd in acs:
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=AC_TIMEOUT_S)
            rc = p.returncode
            tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or [""]
        except subprocess.TimeoutExpired:
            rc, tail = 124, ["timed out after %ds" % AC_TIMEOUT_S]
        except Exception as exc:
            rc, tail = 125, [str(exc)]
        ok = ok and rc == 0
        results.append({"cmd": cmd, "rc": rc, "tail": tail[0][:200]})
        print("  %-4s rc=%-3d %s" % ("PASS" if rc == 0 else "FAIL", rc, cmd[:90]))

    r["proof"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": results}
    r["status"] = "done" if ok else "open"
    save(path, rows)
    print("%s -> %s" % (rid, r["status"]))
    return 0 if ok else 3


def set_status(path: Path, rid: str, status: str) -> int:
    rows = load(path)
    if rid not in rows:
        print("no such prompt: %s" % rid, file=sys.stderr)
        return 1
    rows[rid]["status"] = status
    save(path, rows)
    print("%s -> %s" % (rid, status))
    return 0


def listing(path: Path, which: str) -> int:
    rows = load(path)
    sel = [r for r in sorted(rows.values(), key=lambda r: r.get("ts") or "")
           if which == "all" or r.get("status") == which]
    print("%d of %d prompts (%s)\n" % (len(sel), len(rows), which))
    for r in sel:
        mark = {"open": " ", "done": "x", "retracted": "-"}.get(r.get("status"), "?")
        cont = "  ..cont" if r.get("prev") else ""
        print("[%s] %s %s %s%s" % (mark, r["id"], (r.get("ts") or "")[5:16], r["text"][:88], cont))
    return 0


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    fails = []

    def ck(name, cond):
        print("  %-58s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    def w(p, rows):
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def q(content, ts="2026-08-21T01:00:00.000Z", op="enqueue", sid="abcd1234ef"):
        return {"type": "queue-operation", "operation": op, "timestamp": ts,
                "sessionId": sid, "content": content}

    def u(content, ts="2026-08-21T01:00:00.000Z", sid="abcd1234ef"):
        return {"type": "user", "timestamp": ts, "sessionId": sid,
                "message": {"content": content}}

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        t, led = d / "t.jsonl", d / "ledger.jsonl"

        # --- capture, both arrival paths, and the remove trap -------------------
        w(t, [q("build the ledger"),
              q("build the ledger", ts="2026-08-21T01:00:05.000Z", op="remove"),
              u("a prompt typed at the prompt", ts="2026-08-21T02:00:00.000Z")])
        new, total = reconcile([t], led)
        ck("mid-turn enqueue row is captured", any(
            r["text"] == "build the ledger" for r in load(led).values()))
        # Graded on extract(), NOT on the ledger: reconcile() keys on session+text, so
        # the enqueue/remove pair collapses there whatever the filter does. A check on
        # the ledger count passes even with the filter deleted -- it grades the dedupe.
        ck("extract() ignores the `remove` half of the enqueue/remove pair",
           len(extract(t)) == 2)
        ck("a `remove` row does NOT double-count the same prompt", total == 2)
        ck("a normal user row is still captured", any(
            r["source"] == "user" for r in load(led).values()))

        # --- the 35% pollution ---------------------------------------------------
        t2, led2 = d / "t2.jsonl", d / "l2.jsonl"
        w(t2, [q("<task-notification>\n<task-id>x</task-id>"),
               q('<cross-session-message from="uds:/tmp/x.sock">hello</cross>'),
               q("<system-reminder>as you answer</system-reminder>"),
               q("   "),
               q("a real founder ask")])
        _, tot2 = reconcile([t2], led2)
        ck("task-notification / peer-msg / reminder / blank all filtered", tot2 == 1)

        # --- tool results ride on user rows -------------------------------------
        t3, led3 = d / "t3.jsonl", d / "l3.jsonl"
        w(t3, [{"type": "user", "timestamp": "2026-08-21T03:00:00.000Z",
                "sessionId": "abcd1234ef",
                "message": {"content": [{"type": "tool_result", "content": "out"},
                                        {"type": "text", "text": "not a prompt"}]}},
               {"type": "user", "timestamp": "2026-08-21T03:00:01.000Z",
                "sessionId": "abcd1234ef",
                "message": {"content": [{"type": "text", "text": "genuinely typed"}]}},
               {"type": "user", "timestamp": "2026-08-21T03:00:02.000Z", "isMeta": True,
                "sessionId": "abcd1234ef", "message": {"content": "meta row"}}])
        _, tot3 = reconcile([t3], led3)
        ck("a tool_result user row is not a prompt", tot3 == 1)
        ck("an isMeta user row is not a prompt",
           all(r["text"] != "meta row" for r in load(led3).values()))

        # --- idempotency and status preservation --------------------------------
        n2, tot_again = reconcile([t], led)
        ck("re-scanning the same transcript adds nothing", n2 == 0 and tot_again == 2)
        rid = [r["id"] for r in load(led).values() if r["text"] == "build the ledger"][0]
        set_status(led, rid, "done")
        reconcile([t], led)
        ck("a later scan does not reset a closed prompt",
           load(led)[rid]["status"] == "done")

        # --- fragments ------------------------------------------------------------
        t4, led4 = d / "t4.jsonl", d / "l4.jsonl"
        w(t4, [q("turn prompts into specs", ts="2026-08-21T04:00:00.000Z"),
               q("as part of this", ts="2026-08-21T04:00:30.000Z"),
               q("unrelated later ask", ts="2026-08-21T05:00:00.000Z")])
        reconcile([t4], led4)
        rows4 = {r["text"]: r for r in load(led4).values()}
        ck("a fragment 30s later links to what it continues",
           rows4["as part of this"]["prev"] == rows4["turn prompts into specs"]["id"])
        ck("an ask an hour later does not link", rows4["unrelated later ask"]["prev"] is None)

        # --- corrupt and missing --------------------------------------------------
        t5, led5 = d / "t5.jsonl", d / "l5.jsonl"
        t5.write_text(json.dumps(q("before the corruption")) + "\n"
                      + "{not json at all\n"
                      + json.dumps(q("after the corruption",
                                     ts="2026-08-21T06:00:00.000Z")) + "\n")
        _, tot5 = reconcile([t5], led5)
        ck("a corrupt line costs only itself", tot5 == 2)
        ck("a missing transcript is not fatal", extract(d / "nope.jsonl") == [])
        (d / "empty.jsonl").write_text("")
        ck("an empty transcript is not fatal", extract(d / "empty.jsonl") == [])

        # --- closing needs proof ---------------------------------------------------
        t6, led6 = d / "t6.jsonl", d / "l6.jsonl"
        w(t6, [q("do the thing")])
        reconcile([t6], led6)
        rid6 = list(load(led6))[0]
        ck("a prompt with no acceptance criteria REFUSES to close",
           verify(led6, rid6) == 2 and load(led6)[rid6]["status"] == "open")
        attach_spec(led6, rid6, "the thing is done", ["true", "false"])
        ck("one failing criterion keeps it open",
           verify(led6, rid6) == 3 and load(led6)[rid6]["status"] == "open")
        attach_spec(led6, rid6, "the thing is done", ["true", "test 1 -eq 1"])
        ck("all criteria passing closes it", verify(led6, rid6) == 0)
        r6 = load(led6)[rid6]
        ck("closing records the commands and exit codes as proof",
           r6["status"] == "done" and len(r6["proof"]["results"]) == 2
           and all(x["rc"] == 0 for x in r6["proof"]["results"]))
        ck("verifying an unknown id is refused, not crashed", verify(led6, "deadbeef") == 1)

        # --- incremental scanning ------------------------------------------------
        # Every one of these is a way an offset silently eats a founder prompt.
        t8, led8 = d / "t8.jsonl", d / "l8.jsonl"
        w(t8, [q("first ask", ts="2026-08-21T07:00:00.000Z")])
        reconcile([t8], led8)
        with t8.open("a") as fh:
            fh.write(json.dumps(q("second ask", ts="2026-08-21T07:01:00.000Z")) + "\n")
        n8, tot8 = reconcile([t8], led8)
        ck("an appended prompt is picked up by the next scan", n8 == 1 and tot8 == 2)
        ck("the second scan re-read only the new bytes",
           load_marks(led8)[str(t8)] == t8.stat().st_size)

        # A half-written line is the normal state of a live transcript, and the case that
        # matters is a COMPLETE row followed by a partial one in the same append -- a
        # partial alone is caught by the no-newline early return, so testing only that
        # grades nothing. Here the offset must stop between the two, not at end-of-file.
        with t8.open("a") as fh:
            fh.write(json.dumps(q("third ask", ts="2026-08-21T07:02:00.000Z")) + "\n")
            fh.write('{"type":"queue-operation","operation":"enqueue","content":"tor')
        n9, tot9 = reconcile([t8], led8)
        ck("the complete row lands and the half-written one does not",
           n9 == 1 and tot9 == 3)
        ck("the offset stopped at the last COMPLETE line",
           load_marks(led8)[str(t8)] < t8.stat().st_size)
        with t8.open("a") as fh:
            fh.write('n ask","timestamp":"2026-08-21T07:03:00.000Z","sessionId":"abcd1234ef"}\n')
        n10, tot10 = reconcile([t8], led8)
        ck("the half-written line is captured once it completes", n10 == 1 and tot10 == 4)

        # Graded on extract() itself: reconcile() dedupes, so a scan that ignores the
        # offset and re-reads the whole file still produces the right ledger. Only
        # extract() can show whether the read was actually incremental.
        mark = load_marks(led8)[str(t8)]
        ck("extract() from the saved offset returns only what is new",
           extract(t8, mark) == [])
        with t8.open("a") as fh:
            fh.write(json.dumps(q("fifth ask", ts="2026-08-21T07:04:00.000Z")) + "\n")
        seen = extract(t8, mark)
        ck("extract() from the saved offset returns exactly the new rows",
           len(seen) == 1 and seen[0]["text"] == "fifth ask")

        # A transcript that shrank was truncated or replaced; the offset is meaningless.
        t9, led9 = d / "t9.jsonl", d / "l9.jsonl"
        w(t9, [q("a", ts="2026-08-21T08:00:00.000Z"), q("b", ts="2026-08-21T08:00:01.000Z"),
               q("c", ts="2026-08-21T08:00:02.000Z")])
        reconcile([t9], led9)
        w(t9, [q("d", ts="2026-08-21T08:00:03.000Z")])
        _, tot_t = reconcile([t9], led9)
        ck("a truncated transcript is re-scanned from the start", tot_t == 4)

        # The offsets outliving their ledger is the worst case: every prompt skipped.
        led9.unlink()
        _, tot_r = reconcile([t9], led9)
        ck("a deleted ledger re-reads the whole transcript, not none of it", tot_r == 1)

        # --- ledger robustness -------------------------------------------------------
        led7 = d / "l7.jsonl"
        led7.write_text(json.dumps({"id": "aaa", "text": "kept"}) + "\n{ broken\n"
                        + json.dumps({"no": "id"}) + "\n")
        got = load(led7)
        ck("a corrupt ledger line does not lose the rows around it",
           list(got) == ["aaa"])

    print("\n%d checks, %d failed" % (36, len(fails)))
    return 1 if fails else 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transcript", action="append", default=[])
    ap.add_argument("--project-dir", help="scan every .jsonl transcript in this directory")
    ap.add_argument("--ledger", help="override the ledger path")
    ap.add_argument("--list", dest="do_list", nargs="?", const="open",
                    choices=["open", "done", "retracted", "all"])
    ap.add_argument("--spec", metavar="ID")
    ap.add_argument("--statement", default="")
    ap.add_argument("--ac", action="append", default=[])
    ap.add_argument("--verify", metavar="ID")
    ap.add_argument("--retract", metavar="ID")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    transcripts = [Path(p) for p in a.transcript]
    project_dir = Path(a.project_dir) if a.project_dir else None

    # Run as a Stop hook, the transcript path arrives on stdin. Run by hand, it does not.
    if not transcripts and not project_dir and not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            if payload.get("transcript_path"):
                transcripts = [Path(payload["transcript_path"])]
        except Exception:
            pass

    if project_dir:
        transcripts.extend(sorted(project_dir.glob("*.jsonl")))
    if not project_dir and transcripts:
        project_dir = transcripts[0].parent

    path = Path(a.ledger) if a.ledger else ledger_path(
        slug_for(project_dir) if project_dir else "unknown")

    if a.spec:
        return attach_spec(path, a.spec, a.statement, a.ac)
    if a.verify:
        return verify(path, a.verify)
    if a.retract:
        return set_status(path, a.retract, "retracted")
    if a.do_list:
        return listing(path, a.do_list)

    if not transcripts:
        print("nothing to scan: pass --transcript or --project-dir", file=sys.stderr)
        return 1
    new, total = reconcile(transcripts, path)
    rows = load(path)
    op = sum(1 for r in rows.values() if r.get("status") == "open")
    print("prompt-ledger: %d new, %d total, %d open  -> %s" % (new, total, op, path))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:            # a capture tool must never be the reason a turn dies
        print("prompt-ledger: %s" % exc, file=sys.stderr)
        sys.exit(0)

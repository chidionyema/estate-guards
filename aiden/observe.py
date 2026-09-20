#!/usr/bin/env python3
"""Aiden's observation plane. It costs nothing to run.

Every Claude Code session already writes a complete JSONL transcript to
~/.claude/projects/<slug>/<session-id>.jsonl, and every assistant record in it
carries a `usage` block. So the two things Aiden needs -- what a session is
doing, and what it has spent -- are already on this disk. Asking a model either
question would be paying for an answer we were given for free.

That is the whole design. There is no API call in this file.

Reads are incremental. Each file is remembered by (size, mtime, byte offset), so
a second run reads only the bytes written since the first. Pricing is not
redefined here; it is imported from scripts/token-audit.py, which is the estate's
existing meter and already deduplicates message ids. A transcript repeats a
message id on retry, and counting both overstates spend by about 2x.
"""
import datetime
import json
import os
import sys
import time
import importlib.util

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
STATE = os.path.join(HOME, ".claude", "state", "aiden-observe.json")

#: The meter already exists. Importing it means one set of prices, one dedup
#: rule, and no second ledger to fall out of step with the first.
_spec = importlib.util.spec_from_file_location(
    "token_audit", os.path.join(
        os.environ.get("GUARDS_ROOT")
        or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "token-audit.py"))
_ta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ta)

#: A session whose transcript has not grown in this long is not thinking.
LIVE_SECONDS = 120
#: How far back the board looks. Older sessions are history, not state.
BOARD_HOURS = 24
#: Keep this many message ids per file so a retry seen in an earlier run is
#: still recognised as a duplicate in a later one. Retries land adjacent, so a
#: short memory is enough and an unbounded one would grow without limit.
ID_MEMORY = 400


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def _epoch(ts):
    """Transcript timestamps are ISO 8601 with a Z. Nothing else appears there."""
    if not ts:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def blank():
    return {"offset": 0, "size": 0, "input": 0, "cache_write": 0,
            "cache_read": 0, "output": 0, "usd": 0.0, "requests": 0, "recent": [],
            "ids": [], "text": "", "ts": "", "cwd": "", "branch": "",
            "model": "", "prompt": ""}


def scan_file(path, prev):
    """Fold the bytes written since last time into the running totals."""
    st = os.stat(path)
    rec = dict(blank())
    rec.update(prev or {})
    #: A file that shrank was rotated or replaced, so the offset is meaningless
    #: and everything before it has to be counted again from zero.
    if st.st_size < rec["size"]:
        rec = blank()
    rec["size"] = st.st_size
    if st.st_size == rec["offset"]:
        return rec, st.st_mtime

    seen = set(rec["ids"])
    #: Binary, and readline() rather than `for line in f`. Iterating a text file
    #: turns tell() into an OSError ("telling position disabled by next() call"),
    #: and the byte offset is the whole point of reading incrementally.
    with open(path, "rb") as f:
        f.seek(rec["offset"])
        while True:
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                #: A partial final line means the session is mid-write. Stop
                #: before it and leave the offset short so the next run rereads
                #: the whole line rather than half of one.
                break
            rec["offset"] = f.tell()
            line = raw.decode("utf-8", "replace")
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get("type")
            if r.get("cwd"):
                rec["cwd"] = r["cwd"]
            if r.get("gitBranch"):
                rec["branch"] = r["gitBranch"]
            if t == "user" and not r.get("isSidechain"):
                c = r.get("message", {}).get("content")
                if isinstance(c, str) and c.strip():
                    rec["prompt"] = c.strip()[:300]
                elif isinstance(c, list):
                    for b in c:
                        if b.get("type") == "text" and b.get("text", "").strip():
                            rec["prompt"] = b["text"].strip()[:300]
            if t != "assistant":
                continue
            msg = r.get("message", {})
            mid = msg.get("id")
            if mid in seen:
                continue
            if mid:
                seen.add(mid)
                rec["ids"].append(mid)
            u = msg.get("usage", {}) or {}
            rec["requests"] += 1
            rec["input"] += u.get("input_tokens", 0)
            rec["cache_write"] += u.get("cache_creation_input_tokens", 0)
            rec["cache_read"] += u.get("cache_read_input_tokens", 0)
            rec["output"] += u.get("output_tokens", 0)
            spent = _ta.cost(msg.get("model"), u)
            rec["usd"] += spent
            #: A burn rate has to be spend over a real window, not lifetime spend
            #: divided by whatever number is handy. Keeping the last hour of
            #: (when, how much) is the only honest way to answer "right now",
            #: and it is four hundred floats, not a time series database.
            when = _epoch(r.get("timestamp"))
            if when:
                rec["recent"].append([when, spent])
            rec["model"] = msg.get("model") or rec["model"]
            if r.get("timestamp"):
                rec["ts"] = r["timestamp"]
            #: The session's own words are the status line. It has already said
            #: what it is doing, in the founder's language, at no extra cost.
            if not r.get("isSidechain"):
                for b in msg.get("content", []):
                    if b.get("type") == "text" and b.get("text", "").strip():
                        rec["text"] = b["text"].strip()

    rec["ids"] = rec["ids"][-ID_MEMORY:]
    horizon = time.time() - 3600
    rec["recent"] = [x for x in rec["recent"] if x[0] >= horizon][-2000:]
    return rec, st.st_mtime


#: One walk per process. tick.py asks for the same window twice, once for the
#: board and once for the alerts, and this machine holds 81,377 transcripts
#: across 16,631 project directories with iCloud servicing the same volume. The
#: second walk bought nothing and doubled the wall clock: on 2026-08-23 a single
#: tick sat 46 minutes inside a job scheduled every 5, having burned 26 seconds
#: of CPU, so it was waiting on the disk rather than working. The identical
#: double walk cost founder_board.py 126s of a 385s page and was fixed there the
#: same day; this file has its own copy of the walk, so that fix did not reach it.
_CACHE = {}


def sessions(hours=BOARD_HOURS):
    """Every transcript touched inside the window, folded and costed."""
    if hours in _CACHE:
        return _CACHE[hours]
    state = load_state()
    cutoff = time.time() - hours * 3600
    out, errors = [], []
    #: scandir, not listdir. This machine has 16,631 project directories holding
    #: 81,377 transcripts, and listdir forces a separate stat syscall for every
    #: one of them. scandir carries the type and the stat with the entry, which
    #: took a full pass from 27.9s to a fraction of it under the system Python
    #: that launchd actually uses (3.9.6, older and slower than the shell's).
    try:
        slugs = list(os.scandir(PROJECTS))
    except OSError:
        return [], ["cannot read " + PROJECTS]
    for slug_entry in slugs:
        if not slug_entry.is_dir(follow_symlinks=False):
            continue
        slug = slug_entry.name
        try:
            entries = list(os.scandir(slug_entry.path))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if not name.endswith(".jsonl"):
                continue
            path = entry.path
            try:
                if entry.stat().st_mtime < cutoff:
                    continue
                rec, mtime = scan_file(path, state.get(path))
            except Exception as e:
                #: Narrow enough to name. A bare skip here once hid a real
                #: defect for a whole run and the board still rendered green,
                #: which is the failure mode an instrument must not have.
                errors.append(f"{os.path.basename(path)}: {type(e).__name__}: {e}")
                continue
            state[path] = rec
            if rec["requests"] == 0:
                continue
            out.append({"slug": slug, "session": name[:-6], "path": path,
                        "idle": time.time() - mtime, **rec})
    save_state(state)
    out.sort(key=lambda r: r["idle"])
    _CACHE[hours] = (out, errors)
    return out, errors

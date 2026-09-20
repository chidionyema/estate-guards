#!/usr/bin/env python3
"""Mine the transcripts for the moments the founder stopped an agent, and rank what caused them.

WHY THIS EXISTS
---------------
Asked on 2026-08-17: how do we self-improve from our own transcripts, rather than writing
another memory file nobody executes?

The answer is that the labels already exist and nobody reads them. Measured on that day:

    343 transcripts, 1.2 GB
    478  "Request interrupted by user"          <- you stopped an action mid-flight
    215  "doesn't want to proceed with this"    <- you refused a tool call

Every one of those is a human saying "this exact behaviour, at this exact point, was wrong",
already attached to the trajectory that earned it. That is labelled training data. It has been
sitting on disk unread while the same mistakes were re-litigated in prose.

THE LOOP
--------
1. Find every stop event.
2. Take the tool calls that led into it - the trajectory that earned the stop.
3. Bucket them by signature and rank by how often they recur.
4. The top recurring signature becomes a RULE in rule-guard.py, which refuses it next time.
5. Re-run this. The metric is stops per 100 tool calls. If a rule worked, that number falls.

Step 5 is what makes it recursive rather than a report: the loop grades its own fixes with
the same data it learned from.

WHAT IT DOES NOT DO
-------------------
It does not judge whether a stop was fair. A stop is evidence that the founder wanted
something different, not proof the agent was wrong. Frequency is the signal - one stop is
noise, a signature that recurs 20 times is a behaviour worth refusing.

USAGE
-----
    python3 reflect.py                      # default project, ranked causes
    python3 reflect.py --trend              # stops per 100 tool calls, by month
    python3 reflect.py --show <signature>   # the raw commands behind one bucket
    python3 reflect.py --project <slug>
    python3 reflect.py --complaints         # what he typed, clustered, de-duplicated
    python3 reflect.py --json               # the whole scoreboard, for the Ops Console
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
from collections import Counter, defaultdict, deque
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_SLUG = "-Users-chidionyema-Documents-code-prospector"
#: The console reads this file. It lives in the repo so the Next.js app can reach it without
#: a second service, and it is written atomically because the page polls every 30s.
DEFAULT_SNAPSHOT = (Path.home() / "Documents" / "code" / "prospector"
                    / "store" / "ops" / "method_metrics.json")

#: Substring probes run against the RAW line before any JSON parsing. 1.2 GB of transcript
#: makes `json.loads` on every line the whole cost of this script; a substring test is free.
STOP_MARKERS = (
    "Request interrupted by user",
    "doesn't want to proceed with this tool use",
    "user doesn't want to take this action",
)

#: How many tool calls before a stop count as "the trajectory that earned it". Five is enough
#: to see a drift (a run of reads with no edit) without dragging in the previous task.
LOOKBACK = 5


#: A stop event says an agent was halted. It does not say WHY. The why is in what the founder
#: typed, and 343 transcripts of it were never read — which is how the same complaint got
#: re-raised for months and answered from scratch each time.
COMPLAINT_MARKERS = re.compile(
    r"\b(again|already|still|why (did|are|tf|do)|you (didn'?t|never|keep|always|just)|"
    r"i (told|asked|said)|stop |don'?t |wrong|not what|no,|nope|rubbish|useless|"
    r"sloppy|mess|stupid|wtf|junior|irresponsible|careful|repeat|same (mistake|issue|problem|"
    r"thing|failure)|waste|slow|inefficien|too many|too much|focus|listen)",
    re.I,
)

#: Themes are declared, not inferred, so the buckets are auditable. Anything matching none of
#: them lands in the residue, which is PRINTED — a silent "other" bucket is how a new problem
#: stays invisible for three months.
THEMES: dict[str, tuple[str, ...]] = {
    "proof / unverified claims": (
        "prove", "proof", "verify", "verified", "evidence", "source", "unverified", "assert",
        "receipt", "are you (sure|certain)", "certain", "check(ed)?", "hallucinat", "made up",
        "assumption", "guess",
    ),
    "repeating the same mistake": (
        "again", "same mistake", "same issue", "same problem", "same failure", "repeat",
        "twice", "keep (doing|making)", "never learn", "no improvement", "junior",
    ),
    "efficiency / cost / speed": (
        "slow", "waste", "wasting", "token", "cost", "expensive", "efficien", "inefficien",
        "long(est)? route", "convoluted", "quick", "faster", "burn",
    ),
    "tracking / duplication / other agents": (
        "track", "overlap", "duplicate", "other (session|agent)", "who is", "chaos",
        "coordinat", "ether", "forgotten", "lost",
    ),
    "not following instruction": (
        "i told you", "i asked", "i said", "you didn'?t", "you never", "ignore", "ignoring",
        "listen", "why did you", "not what i", "off track", "distract",
    ),
    "sloppiness / broken output": (
        "sloppy", "mess", "broken", "half.?bak", "rubbish", "useless", "stupid", "doesn'?t work",
        "not working", "fail", "wrong",
    ),
    "rushing / scope / firefighting": (
        "rush", "slow down", "stop", "wait", "too many", "too much", "firefight", "on the fly",
        "no (spec|design|process|plan)", "think", "design",
    ),
    "process / no follow-up": (
        "process", "follow up", "follow.?through", "never (check|prove|measure)", "effective",
        "spec", "trace", "no tracking", "not tracked",
    ),
    "communication / format": (
        "answer", "verbose", "waffle", "unclear", "understand", "explain", "plain",
        "straight", "150 words", "narrat",
    ),
    # The three below were NOT declared up front. They came out of reading the residue: the
    # (unclustered) bucket was the biggest one at 134, and a third of it was the same three
    # complaints. That is the whole reason the residue is printed.
    "cannot tell what you are doing": (
        "wtf (are|do|u|we)", "what (does|are) (this|you)", "even (mean|talking)", "ranbl",
        "rambl", "i (dont|don'?t) (understand|know what|dundern)", "dunno", "confus",
        "hours later", "weird", "a word u said",
    ),
    "is it actually shipped": (
        "still seeing", "still not", "is (it|this|anything|all) (live|shipped|deployed|done|"
        "passing|fixed)", "did (it|these|they|we)", "make it into", "deployed", "in prod",
        "on live", "go live", "ship already", "not live", "whats? left", "shipped",
    ),
    "items raised then dropped": (
        "the rest", "outstanding", "raised", "address(ed)? (all|1|it)", "need to address",
        "left to do", "all (of )?(the )?(items|issues|points|10|ten)", "1,\\s?2", "and why did",
        "what about",
    ),
}


#: The founder routinely pastes an agent's reply back in order to complain about it. Those
#: messages are genuinely his, but most of their WORDS are the agent's, so classifying the
#: whole thing made every long message match every theme — the second run of this scan scored
#: seven themes between 173 and 284, which is the signature of a classifier matching noise.
#: This strips the quoted agent output and keeps only what he typed.
#: Line filtering was not enough: he pastes a reply as ONE long run-on line, so the whole
#: paste survived as a single "line". So split into segments and judge each segment.
_FENCE = re.compile(r"```.*?```", re.S)
_STRUCTURE = re.compile(r"^\s*(#{1,6}\s|\||>|\d+\.\s|[-*]\s|\s*$)")
_SEGMENT = re.compile(r"(?<=[.!?])\s+|\n+")

#: Marks that appear in agent output and effectively never in what he types.
_AGENT_MARK = re.compile(r"[—•→✅❌│┌├`]|\b(?:DONE|WORKING|BLOCKED|HYPOTHESIS|PASS|FAIL)\b:?"
                         r"|\w+\.(?:py|ts|tsx|md|json|yaml|cs):\d+")
#: A finished sentence: starts capital, ends in terminal punctuation. He almost never does both.
_FINISHED = re.compile(r"^[A-Z(\[].{25,}[.!?]$")


#: Sentences an assistant actually emitted, in this estate. Filled by scan_user_messages.
#: This is the decisive filter and the other two are only backstops: when he pastes a reply
#: to complain about it, the paste is a COPY, so the original is on disk one turn earlier.
#: Shape heuristics guessed at authorship; this matches it.
_PASTED: set[int] = set()


def _seg_key(seg: str) -> int:
    return hash(re.sub(r"[^a-z0-9]+", "", seg.lower()))


#: Output tokens per month. OUTPUT only, deliberately: an assistant record's input and
#: cache_read fields describe the whole resident context that turn, so summing them across
#: records counts the same context once per turn and produces a number several times the truth
#: (memory: transcript-totals-double-count-per-record). Output tokens are per-request and add up.
_OUTPUT_TOKENS: Counter = Counter()


def _harvest_usage(rec: dict) -> None:
    usage = (rec.get("message") or {}).get("usage") or {}
    out = usage.get("output_tokens")
    if isinstance(out, int):
        _OUTPUT_TOKENS[str(rec.get("timestamp", ""))[:7]] += out



#: One row per session, keyed by transcript filename. The monthly numbers above answer "is the
#: method improving"; they cannot answer "did THIS change help", because a month averages the
#: before and the after into one bucket. Attribution needs the session as the unit.
#:
#: Every counter here is arithmetic over the transcript. No model call, so this stays free and
#: can run over every session there has ever been, which is what makes a trend trustworthy.
_SESSIONS: dict = {}

#: Read-only by tool NAME only. Bash is deliberately excluded even though most Bash calls in
#: this estate are `cat`/`sed`/`rg`: classifying a shell command needs a parser, and a wrong
#: classification here would silently inflate the streak count. So the streak number is a
#: FLOOR, not a total, and must be read as one.
_READONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "NotebookRead", "WebFetch", "WebSearch"})


def _harvest_session(path, rec: dict) -> None:
    """Fold one assistant record into its session's row."""
    row = _SESSIONS.get(path.stem)
    if row is None:
        row = _SESSIONS[path.stem] = {
            "session": path.stem[:8], "date": None, "requests": 0, "tool_calls": 0,
            "parallel_requests": 0, "readonly_streaks": 0, "peak_resident": 0,
            "output_tokens": 0, "_run": 0, "_seen": set(),
        }
    ts = str(rec.get("timestamp", ""))[:10]
    if ts and row["date"] is None:
        row["date"] = ts

    usage = (rec.get("message") or {}).get("usage") or {}
    out = usage.get("output_tokens")
    if isinstance(out, int):
        row["output_tokens"] += out
    resident = usage.get("cache_read_input_tokens")
    if isinstance(resident, int) and resident > row["peak_resident"]:
        row["peak_resident"] = resident

    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not uses:
        return
    row["tool_calls"] += len(uses)
    # One round trip is one requestId. Claude Code writes ONE tool_use block per assistant
    # RECORD, so counting records counts the file format: the first cut of this did exactly
    # that and reported 100% single-call turns across every session ever recorded.
    rid = rec.get("requestId") or (rec.get("message") or {}).get("id")
    if rid is None:
        row["requests"] += 1
    elif rid not in row["_seen"]:
        row["_seen"].add(rid)
        row["requests"] += 1
    else:
        row["parallel_requests"] += 1

    # A streak is counted once, on the call that reaches three. A longer streak is still one
    # violation of one intent, not one per extra call.
    for use in uses:
        if str(use.get("name")) in _READONLY_TOOLS:
            row["_run"] += 1
            if row["_run"] == 3:
                row["readonly_streaks"] += 1
        else:
            row["_run"] = 0


def _session_rows(limit: int = 40) -> list[dict]:
    """The most recent sessions, newest first, with the ratios worked out."""
    rows = []
    for row in _SESSIONS.values():
        if row["requests"] < 5:
            continue  # too short to say anything about method
        r = {k: v for k, v in row.items() if not k.startswith("_")}
        r["calls_per_request"] = round(row["tool_calls"] / row["requests"], 2)
        rows.append(r)
    rows.sort(key=lambda r: (r["date"] or "", r["session"]), reverse=True)
    return rows[:limit]


def _compliance(rows: list[dict]) -> dict:
    """Grade the sessions against the thresholds CLAUDE.md already states.

    These are the founder's own numbers, not new ones: take the /compact safe point at ~85K
    resident and immediately at ~140K; batch every call into one round trip per intent;
    delegate before the second exploratory search.
    """
    if not rows:
        return {"sessions": 0, "note": "no session long enough to grade"}
    n = len(rows)
    over_soft = sum(1 for r in rows if r["peak_resident"] >= 85_000)
    over_hard = sum(1 for r in rows if r["peak_resident"] >= 140_000)
    return {
        "sessions": n,
        "unit": "last %d sessions of 5+ round trips, newest first" % n,
        "sessions_over_85k_resident": over_soft,
        "sessions_over_140k_resident": over_hard,
        "median_peak_resident": sorted(r["peak_resident"] for r in rows)[n // 2],
        "readonly_streaks": sum(r["readonly_streaks"] for r in rows),
        "median_calls_per_request": round(
            sorted(r["calls_per_request"] for r in rows)[n // 2], 2),
        "notes": {
            "resident": "The thresholds are the founder's own: take the /compact safe point at "
                        "~85K resident, immediately at ~140K. Peak cache_read per request is "
                        "the resident size, so this grades the rule directly.",
            "calls_per_request": "Parallel tool use ONLY. Most batching in this estate happens "
                                 "inside one Bash call by chaining with ; and &&, which is "
                                 "invisible here -- a well batched turn and a lazy one both "
                                 "look like one call. Do not read this as the batching rule.",
            "readonly_streaks": "Runs of 3+ consecutive read-only calls, by TOOL NAME only. "
                                "Bash reads are not classified, so this is a floor.",
        },
    }


def _harvest_assistant(text: str) -> None:
    for seg in _SEGMENT.split(_FENCE.sub(" ", text)):
        seg = seg.strip()
        if len(seg) >= 40:  # short fragments collide with ordinary speech
            _PASTED.add(_seg_key(seg))


def _own_words(text: str) -> str:
    """Return only the part of a message he plausibly typed himself.

    He complains by pasting the reply he is complaining ABOUT, so a naive classifier reads
    the agent's vocabulary and every theme scores the same. Measured before this filter:
    all nine themes between 173 and 284, which is the signature of matching noise.
    """
    body = _FENCE.sub(" ", text)
    kept = []
    for seg in _SEGMENT.split(body):
        seg = seg.strip()
        if not seg or _STRUCTURE.match(seg):
            continue
        if len(seg) >= 40 and _seg_key(seg) in _PASTED:
            continue
        if _AGENT_MARK.search(seg) or _FINISHED.match(seg):
            continue
        kept.append(seg)
    return " ".join(kept).strip()


def _themes_of(text: str) -> list[str]:
    low = _own_words(text).lower()
    hits = [name for name, pats in THEMES.items()
            if any(re.search(p, low) for p in pats)]
    return hits or ["(unclustered)"]


def _text(msg: object) -> str:
    """Message content is either a string or a list of typed blocks; flatten both."""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        out = []
        for b in msg:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(str(b.get("text", "")))
        return "\n".join(out)
    return ""


def _signature(name: str, inp: dict) -> str:
    """Collapse a tool call to the behaviour it represents, not the exact string.

    `git diff --stat A B` and `git diff --shortstat C D` are the same behaviour and must land
    in the same bucket, or every call is unique and nothing ever ranks."""
    if name != "Bash":
        target = str(inp.get("file_path") or inp.get("pattern") or "")
        return f"{name}:{Path(target).suffix or target[:24]}" if target else name
    cmd = str(inp.get("command", "")).strip()
    # A batched command is one intent, so the signature is its first LOAD-BEARING verb.
    # Bucketing on the literal first word put 24% of all stops in a `cd` bucket, which
    # names the shell's boilerplate rather than the behaviour that got stopped.
    noise = {"cd", "set", "echo", "export", "printf", "true", ":", "#"}
    for part in re.split(r"[|;&\n]+", cmd):
        head = part.strip().split()
        while head and (head[0] in {"timeout", "env", "nohup"} or "=" in head[0]):
            head = head[2:] if head[0] in {"timeout"} else head[1:]
        if not head:
            continue
        verb = Path(head[0]).name
        if verb in noise or verb.startswith("-"):
            continue
        if verb in {"python", "python3"} and "-m" in head:
            i = head.index("-m")
            return f"Bash:python -m {head[i + 1] if i + 1 < len(head) else ''}".strip()
        sub = next((w for w in head[1:3] if not w.startswith("-")), "")
        return f"Bash:{verb} {Path(sub).name[:24]}".strip()
    return "Bash:(shell boilerplate only)"


def scan(slug: str) -> tuple[list[dict], int, Counter]:
    """Return (stop events, total tool calls seen, tool calls by month)."""
    events: list[dict] = []
    total_calls = 0
    calls_by_month: Counter = Counter()
    for path in sorted((PROJECTS / slug).glob("*.jsonl")):
        recent: deque = deque(maxlen=LOOKBACK)
        try:
            fh = path.open(errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                is_stop = any(m in line for m in STOP_MARKERS)
                if not is_stop and '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                stamp = str(rec.get("timestamp", ""))[:7]
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            total_calls += 1
                            if stamp:
                                calls_by_month[stamp] += 1
                            recent.append(_signature(str(b.get("name", "?")),
                                                     b.get("input") or {}))
                if is_stop:
                    events.append({
                        "file": path.name,
                        "month": stamp,
                        "trail": list(recent),
                        "last": recent[-1] if recent else "(no preceding tool call)",
                        "note": _text(content)[:200],
                    })
    return events, total_calls, calls_by_month


#: Text that is not the founder typing: hook output, replayed context, tool results, the
#: harness's own interrupt markers. Counting these as complaints would inflate every bucket.
NOT_THE_FOUNDER = (
    "<system-reminder>", "[session-guard]", "[graphify]", "[state-probe]",
    "Caveat:", "<command-name>", "<local-command", "Request interrupted by user",
    "This session is being continued", "tool_use_id", "<function_results>",
    "doesn't want to proceed",
)


def scan_user_messages(slug: str) -> list[dict]:
    """Every message the founder actually typed, across every session, de-duplicated.

    De-duplication is not cosmetic. A resumed or summarised session replays earlier turns
    verbatim, so a complaint made once can appear in a dozen files. Counting raw occurrences
    would rank whichever session got compacted most, not whichever problem recurred most."""
    seen: set[int] = set()
    msgs: list[dict] = []
    for path in sorted((PROJECTS / slug).glob("*.jsonl")):
        try:
            fh = path.open(errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                is_user = '"role":"user"' in line or '"role": "user"' in line
                is_asst = '"role":"assistant"' in line or '"role": "assistant"' in line
                if not (is_user or is_asst):
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if is_asst:
                    _harvest_assistant(_text((rec.get("message") or {}).get("content")))
                    _harvest_usage(rec)
                    _harvest_session(path, rec)
                    continue
                # `role: user` is NOT "the founder typed this". It also covers tool results,
                # task notifications, subagent turns, compaction summaries and replayed
                # context. The first run of this scan counted all of them, reported 716
                # complaints, and the verbatim sample was a task-notification block. Every
                # rejection below is one of the contaminants that run surfaced.
                if rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
                    continue
                if "toolUseResult" in rec:
                    continue
                if rec.get("userType") not in (None, "external"):
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                text = _text(content).strip()
                if text.lstrip().startswith(("<task-notification>", "<user-prompt-submit-hook",
                                             "<bash-", "<command-", "[Request interrupted")):
                    continue
                if not text or len(text) < 12:
                    continue
                if any(m in text for m in NOT_THE_FOUNDER):
                    continue
                key = hash(re.sub(r"\s+", " ", text.lower())[:400])
                if key in seen:
                    continue
                seen.add(key)
                msgs.append({
                    "month": str(rec.get("timestamp", ""))[:7],
                    "file": path.name,
                    "text": text,
                })
    # Classify only after every file is read: a paste in one session quotes a reply that
    # lives in another, so the assistant corpus is not complete until the scan ends.
    for m in msgs:
        own = _own_words(m["text"])
        m["own"] = own
        m["complaint"] = bool(COMPLAINT_MARKERS.search(own))
    return msgs


def complaints(msgs: list[dict], samples: int) -> None:
    gripes = [m for m in msgs if m["complaint"]]
    print(f"FOUNDER MESSAGES: {len(msgs)} unique across all sessions")
    print(f"COMPLAINT-SHAPED: {len(gripes)}  "
          f"({100 * len(gripes) / max(len(msgs), 1):.0f}% of everything he typed)\n")

    per_theme: Counter = Counter()
    by_theme: defaultdict = defaultdict(list)
    theme_months: defaultdict = defaultdict(Counter)
    for m in gripes:
        for t in _themes_of(m["text"]):
            per_theme[t] += 1
            by_theme[t].append(m)
            if m["month"]:
                theme_months[t][m["month"]] += 1

    print("RECURRING COMPLAINTS, RANKED — this is the real backlog")
    print(f"  {'count':>5}  {'months':>6}  theme")
    for theme, n in per_theme.most_common():
        print(f"  {n:>5}  {len(theme_months[theme]):>6}  {theme}")

    print("\nBY MONTH — a theme that spans many months is a root cause never fixed")
    months = sorted({m for c in theme_months.values() for m in c})
    if months:
        print(f"  {'theme':<34} " + " ".join(f"{m[2:]:>6}" for m in months))
        for theme, _ in per_theme.most_common():
            row = " ".join(f"{theme_months[theme].get(m, 0):>6}" for m in months)
            print(f"  {theme:<34} {row}")

    print(f"\nVERBATIM — the {samples} most recent in each theme, in his words")
    for theme, _ in per_theme.most_common():
        print(f"\n### {theme}")
        for m in by_theme[theme][-samples:]:
            line = re.sub(r"\s+", " ", _own_words(m["text"]))[:190]
            print(f"  [{m['month']}] {line}")


def report(events: list[dict], total_calls: int, top: int) -> None:
    print(f"STOP EVENTS: {len(events)}   TOOL CALLS: {total_calls}   "
          f"RATE: {100 * len(events) / max(total_calls, 1):.2f} stops per 100 calls\n")

    last = Counter(e["last"] for e in events)
    print(f"WHAT WAS RUNNING WHEN YOU STOPPED IT  (top {top})")
    print(f"  {'count':>5}  {'share':>6}  signature")
    for sig, n in last.most_common(top):
        print(f"  {n:>5}  {100 * n / len(events):>5.1f}%  {sig}")

    print("\nDRIFT: stops that followed a run of read-only calls with no edit between")
    readonly = {"Read", "Grep", "Glob", "Bash:git log", "Bash:git diff", "Bash:gh run",
                "Bash:rg", "Bash:ls", "Bash:cat", "Bash:git status", "Bash:wc"}
    drifted = [e for e in events
               if len(e["trail"]) >= 3
               and all(any(t.startswith(r) for r in readonly) for t in e["trail"][-3:])]
    print(f"  {len(drifted)} of {len(events)} stops ({100 * len(drifted) / max(len(events), 1):.0f}%) "
          f"came after 3+ consecutive read-only calls.")
    if drifted:
        print("  Those are the ones a delegation rule would have prevented. Sample trail:")
        for t in drifted[-1]["trail"]:
            print(f"    {t}")


def trend(events: list[dict], calls_by_month: Counter) -> None:
    per_month: Counter = Counter(e["month"] for e in events if e["month"])
    print("STOPS PER 100 TOOL CALLS, BY MONTH — this is the number a new rule must move\n")
    print(f"  {'month':<9} {'stops':>6} {'calls':>8} {'rate':>7}")
    for month in sorted(set(per_month) | set(calls_by_month)):
        calls = calls_by_month.get(month, 0)
        stops = per_month.get(month, 0)
        rate = f"{100 * stops / calls:.2f}" if calls else "-"
        print(f"  {month:<9} {stops:>6} {calls:>8} {rate:>7}")


# --------------------------------------------------------------------------- #
# The register: every theme carries the command that reads it and the rule that
# refuses it. A theme with `enforced_by: None` is a complaint nothing can stop.
# --------------------------------------------------------------------------- #
#: Declared here rather than in a doc on purpose. A doc row cannot be executed, so a doc row
#: cannot tell you it has gone stale. Each `check` is a command that prints the number.
REGISTER: dict[str, dict] = {
    "proof / unverified claims": {
        "check": "python3 $HOME/Documents/code/claude-guards/reflect.py --complaints",
        "enforced_by": "stop-hook: every number in a reply must appear in a tool result",
        "script": None,
    },
    "repeating the same mistake": {
        "check": "python3 $HOME/Documents/code/claude-guards/rule-guard.py --selftest",
        "enforced_by": "rule-guard.py PreToolUse (5 rules)",
        "script": "rule-guard.py",
    },
    "efficiency / cost / speed": {
        "check": "python3 $HOME/Documents/code/claude-guards/token-audit.py -Users-chidionyema",
        "enforced_by": "tool-drip-guard.py, context-guard-hook.py",
        "script": "tool-drip-guard.py",
    },
    "tracking / duplication / other agents": {
        "check": "python3 $HOME/Documents/code/claude-guards/reflect.py --json",
        "enforced_by": None,
        "script": None,
    },
    "not following instruction": {"check": None, "enforced_by": None, "script": None},
    "sloppiness / broken output": {
        "check": ".venv/bin/python scripts/popdd_verify.py --staged",
        "enforced_by": "POPDD gate (CI only — the local hook is not installed)",
        "script": None,
    },
    "rushing / scope / firefighting": {
        "check": "python3 $HOME/Documents/code/claude-guards/rule-guard.py --selftest",
        "enforced_by": "rule-guard.py rule_pr_size",
        "script": "rule-guard.py",
    },
    "process / no follow-up": {
        "check": "python3 $HOME/Documents/code/claude-guards/reflect.py --trend",
        "enforced_by": None,
        "script": None,
    },
    "communication / format": {"check": None, "enforced_by": None, "script": None},
    "cannot tell what you are doing": {
        "check": "curl -s localhost:8611/api/ops/read/method",
        "enforced_by": "Ops Console /method page",
        "script": None,
    },
    "is it actually shipped": {
        "check": "python3 scripts/ops_status.py",
        "enforced_by": "Ops Console /method page",
        "script": None,
    },
    "items raised then dropped": {
        "check": "python3 $HOME/Documents/code/claude-guards/reflect.py --json",
        "enforced_by": "this register — an item with no check is printed as untracked",
        "script": None,
    },
}

TARGET_30D = 1.20
TARGET_60D = 0.80


def _live_mechanisms() -> list[dict]:
    """Which scripts in ~/.claude/scripts are actually invoked by something.

    A script nothing calls is not a safeguard, it is a file. On 2026-08-17, 10 of 16 were
    inert. This makes that a probe instead of a one-off count in a handoff.
    """
    def _read(paths) -> str:
        text = ""
        for path in paths:
            try:
                text += path.read_text(errors="replace")
            except OSError:
                pass
        return text

    home = Path.home()
    # INVOKED: something runs it without a human. A hook entry or a launchd agent.
    invoked = _read([home / ".claude" / "settings.json",
                     home / ".claude" / "settings.local.json",
                     *(home / "Library" / "LaunchAgents").glob("*.plist")])
    # DOCUMENTED: a human is told to run it. That is weaker than invoked but it is not orphaned
    # — the first version of this probe called `token-audit.py` inert when CLAUDE.md names it as
    # the command that answers a question. A probe that miscounts a working tool teaches you to
    # ignore the probe.
    documented = _read([home / ".claude" / "CLAUDE.md",
                        home / "Documents" / "code" / "prospector" / "CLAUDE.md",
                        *(home / ".claude" / "projects").glob("*/memory/MEMORY.md"),
                        *(home / "Documents" / "code" / "prospector" / "docs").glob("*.md")])
    out = []
    for script in sorted((home / ".claude" / "scripts").glob("*.py")):
        is_invoked = script.name in invoked
        is_doc = script.name in documented
        out.append({
            "name": script.name,
            "live": is_invoked,
            "documented": is_doc,
            # Nothing runs it and nothing tells anyone to. That is the disposal list.
            "orphaned": not is_invoked and not is_doc,
        })
    return out


#: Predictions live in the repo, next to the metrics they are about.
PREDICTIONS = (Path.home() / "Documents" / "code" / "prospector"
               / "store" / "ops" / "method_predictions.json")


def _grade_predictions(rate: float, per_call: float | None) -> list[dict]:
    """Score every prediction whose due date has passed, against the number it named.

    This is the part that makes the loop a loop. Writing a rule is activity; predicting what
    it will do to a named number, and then being told you were wrong on a date you fixed in
    advance, is the only thing here that produces learning. A prediction added AFTER the fact
    cannot be graded honestly, so `made_on` is recorded and never edited.
    """
    try:
        rows = json.loads(PREDICTIONS.read_text())
    except (OSError, ValueError):
        return []
    now = {"stop_rate_per_100": rate, "output_tokens_per_call": per_call}
    today = _dt.date.today().isoformat()
    out = []
    for row in rows:
        actual = now.get(row.get("metric"))
        due = str(row.get("due", ""))
        row = dict(row)
        row["actual"] = actual
        if actual is None:
            # No probe reads this metric yet. Say so; do not quietly score it as pending.
            row["verdict"] = "unmeasured"
        elif today < due:
            row["verdict"] = "pending"
            row["moved"] = round(actual - float(row.get("baseline", actual)), 2)
        else:
            row["verdict"] = "hit" if actual <= float(row.get("target", 0)) else "missed"
        out.append(row)
    return out


def snapshot(project: str, events: list[dict], total_calls: int,
             calls_by_month: Counter, msgs: list[dict]) -> dict:
    gripes = [m for m in msgs if m["complaint"]]
    per_theme: Counter = Counter()
    theme_months: defaultdict = defaultdict(Counter)
    theme_samples: defaultdict = defaultdict(list)
    for m in gripes:
        for t in _themes_of(m["text"]):
            per_theme[t] += 1
            if m["month"]:
                theme_months[t][m["month"]] += 1
            theme_samples[t].append({"month": m["month"],
                                     "text": re.sub(r"\s+", " ", m["own"])[:220]})

    stops_by_month: Counter = Counter(e["month"] for e in events if e["month"])
    sig_counts = Counter(e["last"] for e in events)
    mechanisms = _live_mechanisms()
    themes = []
    for theme, n in per_theme.most_common():
        reg = REGISTER.get(theme, {})
        script = reg.get("script")
        enforced_live = bool(script) and any(
            m["name"] == script and m["live"] for m in mechanisms)
        themes.append({
            "theme": theme,
            "count": n,
            "months": len(theme_months[theme]),
            "by_month": dict(sorted(theme_months[theme].items())),
            "check": reg.get("check"),
            "enforced_by": reg.get("enforced_by"),
            "enforced_live": enforced_live,
            "tracked": bool(reg.get("check")),
            "samples": theme_samples[theme][-3:],
        })

    rate = 100 * len(events) / max(total_calls, 1)
    # The current month is the one a rule can still move; a lifetime average cannot fall fast
    # enough to grade anything, so efficiency is scored on the latest month with calls in it.
    live_months = [mo for mo in sorted(calls_by_month) if calls_by_month[mo]]
    per_call_now = (round(_OUTPUT_TOKENS.get(live_months[-1], 0) / calls_by_month[live_months[-1]], 1)
                    if live_months else None)
    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "project": project,
        "headline": {
            "stop_rate_per_100": round(rate, 2),
            "target_30d": TARGET_30D,
            "target_60d": TARGET_60D,
            "verdict": ("on target" if rate <= TARGET_30D else "above target"),
            "complaints": len(gripes),
            "messages": len(msgs),
            "untracked_themes": sum(1 for t in themes if not t["tracked"]),
            "unenforced_themes": sum(1 for t in themes if not t["enforced_live"]),
            "inert_mechanisms": sum(1 for m in mechanisms if not m["live"]),
            "orphaned_mechanisms": sum(1 for m in mechanisms if m["orphaned"]),
            "output_tokens_per_call": per_call_now,
        },
        "stops": {
            "events": len(events),
            "calls": total_calls,
            "by_month": [
                {"month": mo,
                 "stops": stops_by_month.get(mo, 0),
                 "calls": calls_by_month.get(mo, 0),
                 "rate": round(100 * stops_by_month.get(mo, 0) / calls_by_month[mo], 2)
                 if calls_by_month.get(mo) else None}
                for mo in sorted(set(stops_by_month) | set(calls_by_month))
            ],
            "top_signatures": [
                {"signature": s, "count": c, "share": round(100 * c / max(len(events), 1), 1)}
                for s, c in sig_counts.most_common(15)
            ],
        },
        # "Get the job done with the fewest tokens, without hurting quality or speed."
        # The ratio is the honest form of that: raw token totals fall when a month is quiet,
        # which would reward doing less rather than doing it in fewer moves.
        "efficiency": {
            "unit": "output tokens per tool call",
            "note": "Output tokens only. Input and cache_read describe the whole resident "
                    "context each turn, so summing them across records counts the same "
                    "context once per turn and inflates the total several times over.",
            "by_month": [
                {"month": mo,
                 "output_tokens": _OUTPUT_TOKENS.get(mo, 0),
                 "tool_calls": calls_by_month.get(mo, 0),
                 "per_call": round(_OUTPUT_TOKENS.get(mo, 0) / calls_by_month[mo], 1)
                 if calls_by_month.get(mo) else None}
                for mo in sorted(set(_OUTPUT_TOKENS) | set(calls_by_month))
            ],
        },
        # Per session, so a change shipped on a date can be graded against the sessions
        # after it. The monthly ratios above cannot do that.
        "compliance": _compliance(_session_rows()),
        "sessions": _session_rows(),
        "predictions": _grade_predictions(rate, per_call_now),
        "themes": themes,
        "mechanisms": mechanisms,
    }


def show(events: list[dict], sig: str) -> None:
    hits = [e for e in events if sig.lower() in e["last"].lower()]
    print(f"{len(hits)} stops whose last tool call matched {sig!r}\n")
    for e in hits[-12:]:
        print(f"— {e['month']}  {e['file'][:20]}")
        print(f"  trail: {' -> '.join(e['trail'])}")
        if e["note"].strip():
            print(f"  you said: {e['note'].strip()[:150]}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default=DEFAULT_SLUG)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--trend", action="store_true")
    ap.add_argument("--complaints", action="store_true",
                    help="what the founder actually typed, clustered, across every session")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--show", metavar="SIGNATURE")
    ap.add_argument("--json", nargs="?", const=str(DEFAULT_SNAPSHOT), metavar="PATH",
                    help="write the whole scoreboard as JSON for the Ops Console")
    args = ap.parse_args()

    if args.json:
        events, total_calls, calls_by_month = scan(args.project)
        msgs = scan_user_messages(args.project)
        snap = snapshot(args.project, events, total_calls, calls_by_month, msgs)
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=2))
        tmp.replace(out)  # atomic: the console may be reading it
        h = snap["headline"]
        print(f"wrote {out}  rate={h['stop_rate_per_100']}/100 "
              f"complaints={h['complaints']} untracked={h['untracked_themes']} "
              f"inert_mechanisms={h['inert_mechanisms']}")
        return 0

    if args.complaints:
        root = PROJECTS / args.project
        if not root.is_dir():
            print(f"no transcripts at {root}")
            return 1
        complaints(scan_user_messages(args.project), args.samples)
        return 0

    root = PROJECTS / args.project
    if not root.is_dir():
        print(f"no transcripts at {root}")
        return 1

    events, total_calls, calls_by_month = scan(args.project)
    if not events:
        print("no stop events found")
        return 0
    if args.show:
        show(events, args.show)
    elif args.trend:
        trend(events, calls_by_month)
    else:
        report(events, total_calls, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

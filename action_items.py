#!/usr/bin/env python3
"""Every action item in every programme doc, extracted and tracked without anyone asking.

The founder, 2026-08-21: "alsi i need a report of acion itens fron this report and tracked and
actions ... should nt be having to do this, should be auto", and "we need to know deliverbles",
and "and track ruthlessly".

Until this ran, the estate's action items lived as markdown table rows in 26 programme docs.
Every one of them was written down and NONE of them was counted: nothing on this machine could
answer "how many things did we say we would do, and how many are done", so the founder answered
it by reading the docs himself, which is the loop he is asking to be taken out of.

WHAT IT READS. `origin/main`, through git, never a working tree -- a checkout can be dirty, or
26 commits behind (memory: the-main-checkout-is-26-behind-main), and grading a stale tree would
report deliverables that shipped days ago as open.

WHAT AN ITEM IS. A table row whose first cell is an id (R12, E-101, A3b, Q4), or a markdown
checkbox. The status is read from the row's own status cell against a fixed vocabulary.

A row whose status matches NOTHING is reported UNKNOWN and counted separately. It is never
counted as done and never silently dropped: an allow-list with a silent miss case dropped 10
criticals in 18 hours on this estate and no test failed (memory: an-allow-list-whose-miss-case-
is-silent).

    python3 action_items.py            # the summary
    python3 action_items.py --json     # for the board
    python3 action_items.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
REPO = os.environ.get("PROSPECTOR_REPO", os.path.join(HOME, "Documents", "code", "prospector"))
REF = os.environ.get("ACTION_ITEMS_REF", "origin/main")
LEDGER = os.path.join(HOME, ".claude", "state", "action_items.json")

# One to three capitals then a number: R12, E-101, A3b, Q4, RB1, E2. Measured 2026-08-21: the
# first version listed the prefixes it had SEEN (R, E, A, Q, D), so `docs/RUNBOOKS.md` shipped
# with seven RB rows and the tracker reported zero of them. An id scheme is not a fixed list.
ID = re.compile(r"^(?:\*\*)?([A-Z]{1,3}-?\d+[a-z]?)(?:\*\*)?$")

# Read as whole words against a cell, so "NOT STARTED" cannot be read as "STARTED" and a row
# saying "not done" cannot be read as "DONE". Order matters: the negatives are tested first,
# which is why "NOT MET" and "NOT LIVE" can sit safely beside "MET" and "LIVE".
OPEN_TOKENS = ("NOT STARTED", "NOT MEASURED", "NOT DONE", "NOT RUN", "NOT MET", "NOT LIVE",
               "NOT BUILT", "NOT WIRED", "TBD", "PARTLY", "PARTIAL", "UNPROVEN",
               "IN PROGRESS", "RUNNING", "QUEUED", "BLOCKED", "OPEN", "TODO", "PENDING",
               "PROPOSED", "PLANNED", "DEFERRED", "AT RISK", "DEGENERATE", "NO RESOLUTION",
               "NEEDS")
DONE_TOKENS = ("DONE", "SHIPPED", "MERGED", "CLOSED", "COMPLETE", "MEASURED", "LANDED",
               "LIVE", "MET", "BUILT", "PROVEN")

#: A status written as a mark instead of a word. Measured on origin/main 2026-08-21: 17 rows
#: carry the cross and nothing read it, so every one of them counted as having no status at
#: all. Word boundaries cannot express a mark -- both sides of an emoji are already non-word
#: characters, so \b never matches -- which is why these are a separate list matched literally.
OPEN_MARKS = ("\u274c", "\u2717", "\U0001f534")
DONE_MARKS = ("\u2705", "\u2714", "\U0001f7e2")

#: A header cell meaning "this table records whether the row is finished". A table with no such
#: column CANNOT record it, and that is a defect in the DOCUMENT -- a different fix from a
#: status word this vocabulary does not know. Reporting both as "unknown" hid which was which:
#: measured on origin/main 2026-08-21, 574 of 689 unreadable rows sat in a table with no status
#: column and 114 sat in one that had it, and the two numbers want opposite work.
_STATUS_HEADERS = ("STATUS", "STATE", "DONE", "VERDICT", "RESULT", "PROGRESS", "SHIPPED",
                   "LANDED", "MET?")

_SEPARATOR = re.compile(r"^\|[\s:|-]*-[\s:|-]*\|$")


def _hit(up: str, words: tuple, marks: tuple) -> bool:
    """True when a cell carries one of these tokens as a WHOLE WORD, or one of the marks.

    Substring matching is the thing this exists to prevent, and it stopped being theoretical
    the moment MET and LIVE were added: MET is inside PARAMETER and SOMETHING, LIVE is inside
    DELIVERABLE. The comment above the lists claimed whole words from the first version while
    the code used `in`, which was true only by luck of the tokens then chosen.

    The optional S/D/ED suffix keeps COMPLETE matching COMPLETED and OPEN matching OPENED,
    which plain `in` gave for free. It is deliberately not `\\w*`: that would put METRICS and
    METERED back in reach of MET.
    """
    for t in words:
        if re.search(r"\b" + re.escape(t) + r"(?:S|D|ED)?\b", up):
            return True
    return any(t in up for t in marks)


def sh(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""


def classify(cells: list[str]) -> tuple[str, str]:
    """(state, the cell that decided it). state is one of open/done/unknown."""
    for cell in cells:
        up = cell.upper()
        if _hit(up, OPEN_TOKENS, OPEN_MARKS):
            return "open", cell.strip()
        if _hit(up, DONE_TOKENS, DONE_MARKS):
            return "done", cell.strip()
    return "unknown", ""


def parse(path: str, text: str) -> list[dict]:
    """Every action item in one document.

    States are open / done / unknown / untracked. The last two are NOT the same thing and the
    split is the point: `unknown` is a row whose table has a status column holding a word this
    vocabulary does not know -- fixable here, in the vocabulary. `untracked` is a row in a
    table with NO status column, so the document has nowhere to record whether it is finished
    -- fixable only in the document. Both used to report as `unknown`, which put 574 items
    needing an edit to a doc in the same number as 114 needing an edit to this file.
    """
    items: list[dict] = []
    lines = text.split("\n")
    tracked = False   # does the table we are currently inside have a status column?
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("- [ ]") or s.startswith("- [x]") or s.startswith("- [X]"):
            items.append({"id": f"{os.path.basename(path)}:{n}", "source": path, "line": n,
                          "state": "done" if s[3].lower() == "x" else "open",
                          "title": s[5:].strip()[:200], "status_cell": "checkbox"})
            continue
        if not s.startswith("|"):
            tracked = False   # prose or a blank line ends the table
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        # A header is the row the |---| separator sits under. Read it, then move on: a header
        # is never an item. Tables that carry no header at all keep tracked=False, which is the
        # honest answer -- no header means no status column.
        if n < len(lines) and _SEPARATOR.match(lines[n].strip()):
            tracked = any(any(h in c.upper() for h in _STATUS_HEADERS) for c in cells)
            continue
        if _SEPARATOR.match(s):
            continue
        if len(cells) < 2:
            continue
        m = ID.match(cells[0])
        if not m:
            continue
        state, cell = classify(cells[1:])
        if state == "unknown" and not tracked:
            state = "untracked"
        items.append({"id": m.group(1), "source": path, "line": n, "state": state,
                      "title": cells[1][:200], "status_cell": cell})
    return items


def collect(repo: str = "", ref: str = "") -> dict:
    repo, ref = repo or REPO, ref or REF
    rc, out = sh(["git", "-C", repo, "ls-tree", "-r", "--name-only", ref, "docs/"])
    if rc != 0 or not out.strip():
        return {"error": f"git ls-tree {ref} docs/ returned nothing (rc={rc})", "items": []}
    items: list[dict] = []
    docs = [p for p in out.split("\n") if p.endswith(".md")]
    for path in docs:
        rc, text = sh(["git", "-C", repo, "show", f"{ref}:{path}"], timeout=30)
        if rc == 0 and text:
            items.extend(parse(path, text))

    # first_seen, so "how long has this been open" is answerable next run. A ledger that only
    # ever holds today's snapshot cannot tell the founder what is STUCK, which is the half of
    # "track ruthlessly" that matters.
    seen: dict = {}
    try:
        with open(LEDGER) as fh:
            seen = json.load(fh).get("first_seen", {})
    except (OSError, ValueError):
        seen = {}
    now = time.time()
    for it in items:
        key = f"{it['source']}#{it['id']}"
        it["first_seen"] = seen.setdefault(key, now)
        it["age_days"] = (now - it["first_seen"]) / 86400.0

    result = {
        "measured_at": now, "ref": REF, "docs": len(docs), "items": items,
        "open": sum(1 for i in items if i["state"] == "open"),
        "done": sum(1 for i in items if i["state"] == "done"),
        "unknown": sum(1 for i in items if i["state"] == "unknown"),
        # Counted apart from `unknown` because the fix is apart: a doc needs a status column.
        "untracked": sum(1 for i in items if i["state"] == "untracked"),
    }
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "w") as fh:
            json.dump({"first_seen": seen, "measured_at": now}, fh)
    except OSError:
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 199))
        except Exception: pass
    return result


def selftest() -> int:
    fails = []
    doc = "\n".join([
        "| # | Requirement | Status | Note |",
        "|---|---|---|---|",
        "| R1 | a thing | **DONE** | note |",
        "| R2 | another | **NOT STARTED** | — |",
        "| R3 | third | it is complicated | — |",     # no vocabulary hit -> unknown
        "| A3b | latency | TBD | 100x |",
        "| E-101 | an experiment | RUNNING | — |",
        "| RB1 | a runbook | **DONE** | proven |",
        "| R4 | met, as a whole word | **met** | — |",
        "| R5 | the negative beats the positive | **not met** | — |",
        "| R6 | a status written as a mark | ❌ | — |",
        "| R7 | a word that only CONTAINS a token | the parameter is a deliverable | — |",
        "| R8 | the suffix plain `in` gave for free | COMPLETED | — |",
        "",
        # A second table, with NO status column. Its rows are `untracked`, never
        # `unknown`: the document has nowhere to record whether they are finished, so
        # widening the vocabulary in this file can never reach them.
        "| # | His words | What it means |",
        "|---|---|---|",
        "| C1 | \"a quote\" | a meaning |",
        "| C2 | \"another\" | another meaning |",
        "",
        "- [ ] an unchecked box",
        "- [x] a checked box",
        "| not-an-id | ignore me | DONE |",
    ])
    got = parse("docs/T.md", doc)
    by = {i["id"]: i["state"] for i in got}
    # RB1 is here because the first version of ID listed prefixes instead of matching the
    # scheme, and a whole new doc went untracked with no failure anywhere.
    want = {"R1": "done", "R2": "open", "R3": "unknown", "A3b": "open", "E-101": "open",
            "RB1": "done",
            # MET is inside PARAMETER and LIVE is inside DELIVERABLE, so R4 and R7 together
            # are what stops this vocabulary going back to substring matching.
            "R4": "done", "R5": "open", "R6": "open", "R7": "unknown", "R8": "done",
            # The whole point of the split: a table with no status column.
            "C1": "untracked", "C2": "untracked"}
    for k, v in want.items():
        if by.get(k) != v:
            fails.append(f"{k}: got {by.get(k)!r}, wanted {v!r}")
    if "not-an-id" in by:
        fails.append("a row with no id was counted as an item")
    # IN ORDER. This was `sorted(boxes)` until 2026-08-21, and sorting made the assertion blind
    # to the only thing it was checking: flip `- [ ]` to done and `- [x]` to open and the sorted
    # list is identical. edge_test.py found it -- the mutant at the checkbox line survived.
    boxes = [i["state"] for i in got if i["status_cell"] == "checkbox"]
    if boxes != ["open", "done"]:
        fails.append(f"checkboxes, in document order: {boxes}, wanted ['open', 'done']")
    # The trap this vocabulary exists for: NOT DONE must never read as DONE.
    if classify(["**NOT DONE**"])[0] != "open":
        fails.append("'NOT DONE' classified as done")
    if classify(["NOT MEASURED"])[0] != "open":
        fails.append("'NOT MEASURED' classified as done by the MEASURED token")
    # sh(): the shell helper every git read goes through. Untested until 2026-08-21.
    if sh(["/bin/echo", "hi"]) != (0, "hi\n"):
        fails.append(f"sh() on a working command: {sh(['/bin/echo', 'hi'])!r}")
    if sh(["/nonexistent-binary-" + "x" * 12])[0] == 0:
        fails.append("sh() reported success for a binary that does not exist")

    # collect(): end to end against a repo built here, so the git plumbing is graded rather
    # than assumed. Both branches: a real ref, and a ref that does not resolve.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "docs"))
        with open(os.path.join(d, "docs", "P.md"), "w") as fh:
            fh.write("| Z9 | a tracked thing | **DONE** | — |\n| Z8 | another | OPEN | — |\n")
        for cmd in (["git", "init", "-q"], ["git", "add", "docs/P.md"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-qm", "seed"]):
            sh(["git", "-C", d] + cmd[1:] if cmd[0] == "git" else cmd)
        got_c = collect(repo=d, ref="HEAD")
        ids = {i["id"]: i["state"] for i in got_c.get("items", [])}
        if ids.get("Z9") != "done" or ids.get("Z8") != "open":
            fails.append(f"collect() against a real repo: {ids!r} err={got_c.get('error')!r}")
        bad = collect(repo=d, ref="refs/heads/no-such-ref")
        if not bad.get("error") or bad.get("items"):
            fails.append("collect() on an unresolvable ref did not report an error")

    if fails:
        print("selftest FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"PASS: {len(want)} statuses, unknown on a miss, untracked when the table has no "
          f"status column, 'NOT DONE' is open, no substring hits, checkboxes in order, "
          f"sh() both ways, collect() end to end.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    r = collect()
    if a.json:
        print(json.dumps(r))
        return 0 if not r.get("error") else 1
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        return 1
    print(f"{len(r['items'])} action items across {r['docs']} docs on {r['ref']}: "
          f"{r['done']} done, {r['open']} open, {r['unknown']} with no readable status")
    old = sorted((i for i in r["items"] if i["state"] == "open"),
                 key=lambda i: -i["age_days"])[:10]
    for i in old:
        print(f"  {i['age_days']:5.1f}d  {i['id']:<8} {i['source']}:{i['line']}  {i['title'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

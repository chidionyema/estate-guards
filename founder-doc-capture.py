#!/usr/bin/env python3
"""A founder-pasted document becomes a file in git the moment it arrives. No session searches for it.

Founder, 2026-08-29: "i post and tell you to save the doc and you fucking dont", "go find it, who is
monitoring transcripts", and the ruling that names the class: "we should not be looking for infra
discussions". The staging-cluster discussion of 2026-08-29 00:44-01:55Z existed only inside
transcript .jsonl files and the directives log (a JSONL row nobody opens), and it cost a session a
search across ~4000 files to answer "what did we discuss yesterday". directive-capture.py had
already captured 65 messages over 1,500 characters in this one project; every one of them is a
document he pasted, and none of them was a document anyone could open.

WHAT THIS DOES (UserPromptSubmit, never blocks, never fails a turn):
  * a prompt that IS a document (>= DOC_MIN_CHARS characters of founder text, not harness or hook
    echo) or that CARRIES a save order ("save this doc", "document this", "note this down", ...)
    is written verbatim to ~/.claude/docs/founder/<UTC stamp>-<slug>.md with a frontmatter header,
    then `git add` + `git commit` in the claude-estate repo, and pushed in the background.
  * the write and the commit are silent (2026-09-14: no more per-turn additionalContext noise --
    "File write and commit still happen; just no more per-turn additionalContext noise"); read the
    path back with --list/--grep, and the board row for the work links it.

Read it back with:  python3 $HOME/Documents/code/claude-guards/founder-doc-capture.py --list [--grep word]
Backfill the directives log once:  python3 $HOME/Documents/code/claude-guards/founder-doc-capture.py --backfill
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ESTATE = Path(os.environ.get("CLAUDE_ESTATE_DIR", Path.home() / ".claude"))
DOC_DIR = ESTATE / "docs" / "founder"
DIRECTIVES = ESTATE / "directives"
DOC_MIN_CHARS = 1500
SAVE_ORDER = re.compile(
    r"\b(save|store|record|document|write ?up|note)\b.{0,40}\b(doc|document|this|it|down|spec|policy|plan)\b"
    r"|\bsave (this|the|that) (doc|document|spec|policy)\b",
    re.I,
)
# A pasted article carries example keys (a tailscale key, an "sk-..." key) and the estate's secret scan
# grades git history, so a placeholder in a document is a red scan forever. Redact the shapes on
# write; the founder's own words are never a place a live secret belongs (R49).
SECRET_SHAPES = re.compile(
    r"(tskey-[a-z]+-[A-Za-z0-9_\-]{4,}|sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abp]-[A-Za-z0-9\-]{10,}|-u [\"']?[^\s\"']+:[^\s\"']*[\"']?)"
)


def scrub(text: str) -> str:
    return SECRET_SHAPES.sub("<redacted>", text)


# Text the harness or a hook put in the prompt box. Not the founder's words; never a document.
MACHINE_PREFIXES = (
    "<",
    "[",
    "Stop hook",
    "IDLE",
    "Called the",
    "This session",
    "Another Claude",
    "Note:",
    "SessionStart",
    "PostToolUse",
    "PreToolUse",
    "/",
)


def is_machine(prompt: str) -> bool:
    head = prompt.lstrip()[:40]
    return head.startswith(MACHINE_PREFIXES) or "<task-notification>" in prompt[:200]


def is_document(prompt: str) -> bool:
    return not is_machine(prompt) and (
        len(prompt) >= DOC_MIN_CHARS or bool(SAVE_ORDER.search(prompt[:400]))
    )


def slugify(text: str) -> str:
    first = next((ln for ln in text.splitlines() if ln.strip()), "document")
    words = re.findall(r"[a-z0-9]+", first.lower())[:8]
    return "-".join(words) or "document"


def doc_path(prompt: str, ts: datetime) -> Path:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    return DOC_DIR / f"{ts.strftime('%Y-%m-%dT%H%MZ')}-{slugify(prompt)}-{digest}.md"


def write_doc(prompt: str, ts: datetime, session: str, cwd: str) -> Path | None:
    """Write the document once (content-addressed); return the path, or None if it already exists."""
    path = doc_path(prompt, ts)
    if path.exists() or any(
        p.name.endswith(path.name[-11:]) for p in DOC_DIR.glob("*.md")
    ):
        return None
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        f"captured: {ts.isoformat(timespec='seconds')}\n"
        f"session: {session}\n"
        f"cwd: {cwd}\n"
        f"chars: {len(prompt)}\n"
        "source: founder prompt, verbatim (founder-doc-capture.py)\n"
        "---\n\n"
        f"{scrub(prompt.rstrip())}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def commit(paths: list[Path], push: bool = True) -> None:
    rel = [str(p.relative_to(ESTATE)) for p in paths]
    subprocess.run(
        ["git", "-C", str(ESTATE), "add", "--", *rel], check=False, capture_output=True
    )
    msg = f"founder-docs: capture {len(rel)} founder document(s) verbatim (LAW 24, LAW 45)"
    subprocess.run(
        ["git", "-C", str(ESTATE), "commit", "-q", "-m", msg, "--", *rel],
        check=False,
        capture_output=True,
    )
    if push:
        subprocess.Popen(
            ["git", "-C", str(ESTATE), "push", "origin", "HEAD"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = (payload.get("prompt") or "").strip()
    if not prompt or not is_document(prompt):
        return 0
    try:
        ts = datetime.now(timezone.utc)
        path = write_doc(
            prompt, ts, payload.get("session_id", ""), payload.get("cwd") or os.getcwd()
        )
        if path is None:
            return 0
        commit([path])
    except Exception:
        return 0
    return 0


def _parse(line: str) -> dict | None:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _stamp(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def backfill(do_commit: bool = True) -> int:
    """One pass over every directives log: every document-shaped founder prompt becomes a file."""
    written: list[Path] = []
    for log in sorted(DIRECTIVES.glob("*.jsonl")):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            row = _parse(line)
            if row is None:
                continue
            prompt = (row.get("prompt") or "").strip()
            if len(prompt) < DOC_MIN_CHARS or is_machine(prompt):
                continue
            ts = _stamp(row.get("ts", ""))
            if ts is None:
                continue
            path = write_doc(prompt, ts, row.get("session", ""), row.get("cwd", ""))
            if path:
                written.append(path)
    if written and do_commit:
        commit(written)
    print(f"backfilled {len(written)} founder document(s) into {DOC_DIR}")
    return 0


def list_docs(grep: str | None) -> int:
    for p in sorted(DOC_DIR.glob("*.md")):
        if grep and not re.search(
            grep, p.read_text(encoding="utf-8", errors="replace"), re.I
        ):
            continue
        print(p)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument(
        "--no-commit", action="store_true", help="backfill: write files only"
    )
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--grep")
    a = ap.parse_args()
    if a.backfill:
        return backfill(do_commit=not a.no_commit)
    if a.list or a.grep:
        return list_docs(a.grep)
    return hook()


if __name__ == "__main__":
    sys.exit(main())

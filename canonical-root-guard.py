#!/usr/bin/env python3
"""Tell a session, at its start, when it is working outside the canonical root.

WHY THIS EXISTS. Founder, 2026-08-23: "we should all be working from this location and have
worktrees and projects littered all over the place ... BROADCAST AND GET IT DONE."

Measured that day, before this guard existed: 67 git checkouts across 6 roots, and only 12 of
them under /Users/chidionyema/dev/code. Live sessions in the previous 24 hours had worked in
four different real places. Six agents cannot see each other, so each one picks up whatever
checkout its cwd happens to land in, and two of them then edit two copies of one repo.

THE CLASS. A convention about WHERE to work, held only in a laws file, is a convention every
new session has to remember. This is the machine remembering instead.

WHAT IT DOES. One notice, at SessionStart, when the cwd is outside the root and not carved out.
It does not block, it does not move anything, and it never fails a session: a guard that stops
work because its own lookup broke is a guard somebody deletes by lunchtime.

THE CARVE-OUTS ARE NOT TIDINESS, THEY ARE LOAD-BEARING, and each was reported by the session
that owns it on 2026-08-23:

  ~/.claude                     Claude Code reads settings, skills and the laws from here. The
                                 path IS the product for that tool.
  ~/Documents/code/claude-guards The guard scripts. Addressed by guards_root.py rather than a
                                 vendor-named home path; the path IS the product for the estate.
  ~/AGENTS.md                    laws-link-guard.py owns this topology and treats "moved, with a
                                 symlink left behind" as damage to repair.
  ~/Documents/code/prospector    com.chidionyema.reflect (StartInterval 14400) hardcodes
                                 .venv/bin/python and store/ops/method_metrics.json under this
                                 tree. Moving it silently kills the estate's only running
                                 self-measurement, and nothing goes red when it stops.
  session scratchpads            /private/tmp/claude-501/**/scratchpad/wt-* are disposable
                                 worktrees a live session made and will remove itself.
"""
from __future__ import annotations

import os
import pathlib
import sys

CANON = pathlib.Path.home() / "dev" / "code"
EXEMPT = (
    pathlib.Path.home() / ".claude",
    pathlib.Path.home() / ".codex",
    pathlib.Path.home() / ".gemini",
    pathlib.Path.home() / "Documents" / "code" / "prospector",
    # 2026-09-20: the guard scripts. 31 launchd jobs, the hook layer and the backups all
    # resolve here; outside the canonical root, load-bearing, so carved out like prospector.
    pathlib.Path.home() / "Documents" / "code" / "claude-guards",
    pathlib.Path("/private/tmp/claude-501"),
    pathlib.Path("/tmp"),
    pathlib.Path("/var/folders"),
    pathlib.Path("/private/var/folders"),
)

NOTICE = """[canonical-root] This session's cwd is OUTSIDE the canonical root.

  cwd    {cwd}
  root   {canon}

Founder ruling 2026-08-23: all work happens in {canon}. Measured that day, 67 git
checkouts were spread across 6 roots with only 12 in the root, and two sessions had edited two
copies of one repo without either knowing.

  - Do not start NEW work here. If this checkout has a twin under the root, use the twin.
  - Do not move, delete or `git worktree remove` anything to fix it. One agent owns the
    consolidation, and several of these paths are named by launchd jobs that a move would break.
  - Push what you are holding. Unpushed commits are the only thing a consolidation cannot
    recover.
"""


def under(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def verdict(cwd: pathlib.Path) -> str:
    """'ok' inside the root, 'exempt' for a load-bearing path, 'outside' otherwise."""
    if under(cwd, CANON):
        return "ok"
    for e in EXEMPT:
        if under(cwd, e):
            return "exempt"
    return "outside"


def selftest() -> int:
    home = pathlib.Path.home()
    cases = [
        ("the canonical root itself is ok", home / "dev/code", "ok"),
        ("a repo under the root is ok", home / "dev/code/crew", "ok"),
        ("~/Documents/code is outside", home / "Documents/code", "outside"),
        ("~/code is outside", home / "code/Website", "outside"),
        ("~/code-backup is outside", home / "code-backup/QAlgo", "outside"),
        ("~/Desktop is outside", home / "Desktop/haworks-platform", "outside"),
        # Each of these was reported by the session that owns it. A guard that nags about them
        # is a guard that gets switched off, and then it protects nothing.
        ("~/.claude is carved out: the path is the product", home / ".claude", "exempt"),
        # 2026-09-20: the guard scripts moved off the vendor-named path into the repo, which
        # sits outside the canonical root. Load-bearing for 31 launchd jobs and the hook layer,
        # exactly like prospector, so it is carved out the same way.
        ("~/Documents/code/claude-guards is carved out: the guard scripts live there",
         home / "Documents/code/claude-guards", "exempt"),
        # crew#13: ~/.hermes is retired; the launchd wrapper lives in ~/.claude/scripts/estate now
        ("~/.hermes/scripts is no longer carved out", home / ".hermes/scripts", "outside"),
        ("prospector is carved out: com.chidionyema.reflect hardcodes it",
         home / "Documents/code/prospector", "exempt"),
        ("a session scratchpad worktree is carved out",
         pathlib.Path("/private/tmp/claude-501/x/scratchpad/wt-main"), "exempt"),
    ]
    bad = []
    for name, path, want in cases:
        got = verdict(path)
        if got != want:
            bad.append(name)
        print(f"  [{'ok' if got == want else 'FAIL'}] {name}: {got} (want {want})")
    print(f"canonical-root-guard selftest: {len(cases) - len(bad)}/{len(cases)} passed")
    return 1 if bad else 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    try:
        cwd = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        if verdict(cwd) == "outside":
            print(NOTICE.format(cwd=cwd, canon=CANON))
    except Exception:
        pass  # never fail a session start
    return 0


if __name__ == "__main__":
    sys.exit(main())

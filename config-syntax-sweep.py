#!/usr/bin/env python3
"""Count every config file on this estate its own consumer cannot parse.

LAW 45 step 4, the word "exhaustively". config-syntax-guard.py is a promise
about the future; this prints how broken the present already is. A guard written
without this step protects the next file and leaves N existing ones broken, and
N is a number you print, not a feeling you have.

Read-only. Exits 1 when anything is broken, 0 when nothing is.

    python3 config-syntax-sweep.py                 # the estate
    python3 config-syntax-sweep.py ~/dev/code/idp  # one tree
    python3 config-syntax-sweep.py --selftest

The parse routing is config_syntax.py, shared with the guard, so the scheduled
sweep and the live refusal can never disagree about what "broken" means.

THE BASELINE
------------
Five files under `~/dev/code/QAlgo/src/api/data/` are JavaScript source and
concatenated JSON documents saved with a .json extension. `nod_attr.json` is
loaded by `src/api/run.py:646` and `src/api/auth2.py:239`, so that application
has been broken at runtime since the repo's last commit on 2023-11-23. Repairing
them means choosing which of the concatenated documents the application should
get, which is a product call in a repo nobody runs.

They sit in `config-syntax-baseline.txt`. A baselined file is still printed on
every run, under KNOWN, and still counted -- suppression would make the count a
lie. What the baseline changes is only the exit code, so that a sweep which is
permanently red does not become an instrument nobody reads (LAW 28). A sixth
broken file turns it red, and removing a line from the baseline is how a repair
gets locked in.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_syntax import (  # noqa: E402
    ParserUnavailable, checker_for, problem, read_text,
)

DEFAULT_ROOTS = [Path.home() / "dev" / "code", Path.home() / ".claude" / "scripts"]
BASELINE = Path(__file__).resolve().parent / "config-syntax-baseline.txt"
BROADCAST = Path(__file__).resolve().parent / "estate-broadcast.py"
#: What the last --broadcast-on-red run reported. A scheduled sweep that posts
#: the same red set every six hours is noise, and noise is an instrument nobody
#: reads (LAW 28), so it posts only when the set changes.
LAST_RED = Path.home() / ".claude" / "state" / "config-syntax-last-red.json"

#: Build output, dependencies, virtualenvs and runtime state. Not this estate's
#: files to fix, and they swamp the count.
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "site-packages",
    "dist", "build", ".next", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "target", "vendor", ".terraform", "coverage", ".tox", "storage", "store",
    ".cache", ".gradle", "Pods", ".angular", "out",
}


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def sweep(roots: list[Path]) -> tuple[int, list[tuple[Path, str]], list[tuple[Path, str]]]:
    checked = 0
    broken: list[tuple[Path, str]] = []
    blind: list[tuple[Path, str]] = []
    for root in roots:
        if not root.exists():
            blind.append((root, "does not exist, so nothing under it was checked"))
            continue
        for path in walk(root):
            if checker_for(path) is None:
                continue
            checked += 1
            try:
                content = read_text(path)
            except (OSError, UnicodeDecodeError) as exc:
                # A guard that cannot read its evidence says so rather than
                # returning a verdict.
                blind.append((path, f"{type(exc).__name__}: {exc}"))
                continue
            try:
                why = problem(path, content)
            except ParserUnavailable as exc:
                # No parser installed for this format. Not a verdict about the
                # file, so it must not be counted as broken.
                blind.append((path, str(exc)))
                continue
            if why:
                broken.append((path, why))
    return checked, broken, blind


def check_files(paths: list[Path]) -> tuple[int, list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Check a named list of files. The git pre-commit hook passes staged paths."""
    checked = 0
    broken: list[tuple[Path, str]] = []
    blind: list[tuple[Path, str]] = []
    for path in paths:
        if checker_for(path) is None:
            continue
        checked += 1
        try:
            content = read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            blind.append((path, f"{type(exc).__name__}: {exc}"))
            continue
        try:
            why = problem(path, content)
        except ParserUnavailable as exc:
            blind.append((path, str(exc)))
            continue
        if why:
            broken.append((path, why))
    return checked, broken, blind


def load_baseline(path: Path) -> set[str]:
    """Paths already broken when the guard was written. `~` is allowed."""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(str(Path(line).expanduser()))
    return out


def broadcast_red(new: list[tuple[Path, str]]) -> str:
    """Post a red sweep to the estate board. Returns what it did, for printing.

    A scheduled job whose only output is a log file nobody opens has not
    reported anything (LAW 28). This puts a red sweep in front of whichever
    session is next to read the board.

    It posts only when the set of broken paths changes. Six-hourly repetition of
    an already-reported file trains every reader to skip the message, which is
    the same failure as not sending it. A repair followed by a regression posts
    again, because the set changed twice.
    """
    paths = sorted(str(p) for p, _ in new)
    try:
        seen = json.loads(LAST_RED.read_text(encoding="utf-8")) if LAST_RED.exists() else []
    except (OSError, ValueError):
        seen = []
    if paths == seen:
        return f"broadcast=skipped (same {len(paths)} path(s) as the last red run)"

    listed = "; ".join(paths[:5]) + ("" if len(paths) <= 5 else f"; +{len(paths) - 5} more")
    message = (
        f"config-syntax-sweep is RED: {len(paths)} config file(s) the service that "
        f"reads them cannot parse. {listed}. A service refuses the whole file at "
        f"startup and anything waiting on its health is never created, which reads "
        f"as 'not started yet' rather than as a fault. "
        f"Run: python3 $HOME/Documents/code/claude-guards/config-syntax-sweep.py"
    )
    try:
        r = subprocess.run(
            [sys.executable, str(BROADCAST), "--from", "config-syntax-sweep",
             "--kind", "alert", "--priority", "high", "--message", message],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # The sweep's verdict does not depend on the broadcast working. It still
        # exits 1; it just says the alert did not leave the machine.
        return f"broadcast=FAILED ({type(exc).__name__}: {exc})"
    if r.returncode != 0:
        return f"broadcast=FAILED (exit {r.returncode}: {r.stderr.strip()[:200]})"
    try:
        LAST_RED.parent.mkdir(parents=True, exist_ok=True)
        LAST_RED.write_text(json.dumps(paths), encoding="utf-8")
    except OSError as exc:
        return f"broadcast=posted, but the state file was not written ({exc})"
    return f"broadcast=posted ({len(paths)} path(s))"


def clear_red_state() -> None:
    """A green run forgets the last red set, so a regression alerts again."""
    try:
        LAST_RED.unlink(missing_ok=True)
    except OSError:
        pass


def selftest() -> int:
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    (tmp / "bad.xml").write_text('<!-- a -- b -->\n<c/>\n')
    (tmp / "good.xml").write_text('<!-- a b -->\n<c/>\n')
    (tmp / "tsconfig.json").write_text('{\n // ok\n "a": 1,\n}\n')
    (tmp / "nd.json").write_text('{"a":1}\n{"a":2}\n')
    (tmp / "bom.json").write_bytes(b'\xef\xbb\xbf{"a":1}\n')
    (tmp / "node_modules").mkdir()
    (tmp / "node_modules" / "junk.json").write_text("not json at all {")

    checked, broken, blind = sweep([tmp])
    ok = (checked == 5 and len(broken) == 1
          and broken[0][0].name == "bad.xml" and not blind)
    print(f"checked={checked} broken={[p.name for p, _ in broken]} blind={len(blind)}")

    # The baseline must change the exit code and nothing else: a baselined file
    # is still found, still printed, still counted.
    bfile = tmp / "baseline.txt"
    bfile.write_text(f"# known\n{tmp / 'bad.xml'}\n")
    base = load_baseline(bfile)
    new = [p for p, _ in broken if str(p) not in base]
    ok_base = base == {str(tmp / "bad.xml")} and new == []
    print(f"baselined={len(base)} new_after_baseline={len(new)}")

    # And it must not swallow a file that was never baselined.
    (tmp / "second.xml").write_text('<!-- x -- y -->\n<c/>\n')
    _, broken2, _ = sweep([tmp])
    new2 = [p for p, _ in broken2 if str(p) not in base]
    ok_new = [p.name for p in new2] == ["second.xml"]
    print(f"new_when_a_sixth_appears={[p.name for p in new2]}")

    passed = ok and ok_base and ok_new
    print("PASS" if passed else "FAIL: expected checked=5, broken=[bad.xml], blind=0, "
                                "baseline hides only bad.xml, second.xml still reported")
    return 0 if passed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("roots", nargs="*", type=Path, help="trees to sweep")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-baseline", action="store_true",
                    help="exit 1 on every broken file, baselined or not")
    ap.add_argument("--files", action="store_true",
                    help="treat the arguments as files, not trees (used by the git hook)")
    ap.add_argument("--broadcast-on-red", action="store_true",
                    help="post a red result to the estate board (used by the scheduled job)")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    base = set() if args.no_baseline else load_baseline(BASELINE)
    if args.files:
        checked, broken, blind = check_files(args.roots)
    else:
        checked, broken, blind = sweep(args.roots or DEFAULT_ROOTS)
    new = [(p, w) for p, w in broken if str(p) not in base]
    known = [(p, w) for p, w in broken if str(p) in base]

    print(f"checked={checked}  broken={len(broken)}  new={len(new)}  "
          f"known={len(known)}  blind={len(blind)}")
    for path, why in new:
        print(f"BROKEN  {path}\n        {why[:200]}")
    for path, why in known:
        print(f"KNOWN   {path}\n        {why[:200]}")
    for path, why in blind:
        print(f"BLIND   {path}\n        {why[:200]}")

    if args.broadcast_on_red:
        print(broadcast_red(new) if new else "broadcast=not needed (green)")
        if not new:
            clear_red_state()
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())

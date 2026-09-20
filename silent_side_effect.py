#!/usr/bin/env python3
"""Find the guards that cannot tell you they are broken.

This is the canonical copy. It lives beside the guards it protects rather than
in the research repo, because hooks/pre-commit imports it and a check whose
implementation lives in another checkout is one `git clone` away from silently
passing everything.

A guard has one job and it ends in a side effect: a line appended to a ledger,
a file written, a message sent. When that side effect sits inside a try whose
except does nothing, the guard reports success on every run for as long as it
has been dead. Nobody finds out, because the absence of a complaint is exactly
what a working guard looks like.

chidionyema-7e found one this way on 2026-08-23 by forcing the failure, not by
reading the code: session-recorder.py rebuilt the founder's recovery file after
every turn and swallowed its own exceptions, so it would have reported success
with the file two days stale. It had been that way since 21 August.

This is the mechanical form of that search. Not "does the file contain
except: pass" -- that fires on every best-effort read of an optional config,
and a check that cries wolf is a check nobody runs twice. The rule is narrower:

  the try body performs a side effect  AND  the handler does nothing but pass.

Read-only. It prints and exits 0 unless --strict.
"""
import ast, os, sys

# Calls whose whole purpose is to change something outside this process. A try
# that contains one of these is not reading, it is acting, and an act that
# fails silently is the failure this file exists to find.
WRITE_ATTRS = {"write", "writelines", "dump", "dumps_to", "flush",
               "commit", "send", "post", "put", "sendall", "publish",
               "replace", "rename", "unlink", "mkdir", "makedirs",
               "run", "check_call", "check_output", "call", "Popen",
               "write_text", "write_bytes", "touch", "symlink_to"}
WRITE_NAMES = {"open"}          # only counts when the mode is a writing one
MODES_WRITE = ("w", "a", "x", "+")


def is_write_open(node):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in WRITE_NAMES):
        return False
    mode = None
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    #: open(p) with no mode is a read. Defaulting the unknown case to "write"
    #: would flag every config read in the estate and the report would be
    #: ignored, which is the same as not writing it.
    return isinstance(mode, str) and any(c in mode for c in MODES_WRITE)


# Removing a temp file is best-effort by nature: the run already succeeded and
# the leftover costs a few bytes. Flagging it alongside a ledger write that
# never landed is how a 32-row report becomes a report nobody opens (LAW 28).
CLEANUP_ONLY = {"unlink()", "rename()", "replace()"}


def side_effects(body):
    """The names of the acts inside this try, in source order."""
    out = []
    for stmt in body:
        for n in ast.walk(stmt):
            if is_write_open(n):
                out.append("open(...,w)")
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in WRITE_ATTRS:
                out.append(n.func.attr + "()")
    return out


def swallows(handler):
    """A handler that does nothing at all. Not one that logs, re-raises,
    sets a flag or returns a default -- those are decisions. This is silence."""
    return len(handler.body) == 1 and isinstance(handler.body[0], (ast.Pass, ast.Continue))


def scan(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
    except (SyntaxError, OSError):
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        acts = side_effects(node.body)
        if not acts:
            continue
        for h in node.handlers:
            if swallows(h):
                caught = ast.unparse(h.type) if h.type else "BARE except"
                found.append((h.lineno, caught, sorted(set(acts))))
    return found


def main():
    strict = "--strict" in sys.argv
    roots = [a for a in sys.argv[1:] if not a.startswith("-")] or \
            [str(__import__("guards_root").GUARDS_ROOT)]
    hits = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           (".git", "__pycache__", "node_modules", ".venv", "venv")]
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    p = os.path.join(dirpath, fn)
                    for lineno, caught, acts in scan(p):
                        hits.append((p, lineno, caught, acts))

    real = [h for h in hits if set(h[3]) - CLEANUP_ONLY]
    minor = [h for h in hits if not (set(h[3]) - CLEANUP_ONLY)]

    print("=" * 74)
    print("SILENT SIDE EFFECTS  (a guard that cannot report its own failure)")
    print("=" * 74)
    if not real:
        print("  none. Every act that matters reports when it fails.")
    for p, lineno, caught, acts in sorted(real):
        rel = p.replace(os.path.expanduser("~"), "~")
        print(f"  {rel}:{lineno}")
        print(f"      swallows : {caught}")
        print(f"      hiding   : {', '.join(acts)}")
    print()
    print(f"  {len(real)} place(s) where a failed act looks exactly like a successful one.")
    print(f"  {len(minor)} more are cleanup only (a leftover temp file), listed with --all.")
    if "--all" in sys.argv:
        for p, lineno, caught, acts in sorted(minor):
            rel = p.replace(os.path.expanduser("~"), "~")
            print(f"    cleanup  {rel}:{lineno}  {', '.join(acts)}")
    print()
    print("  The repair is one line, not a rewrite: append a guard-broken row to")
    print("  ~/.claude/ESTATE_BOARD.jsonl in the handler. A guard that says it")
    print("  died is worth more than one that dies quietly and keeps its tier.")
    return 1 if (strict and real) else 0


if __name__ == "__main__":
    sys.exit(main())

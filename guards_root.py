#!/usr/bin/env python3
"""guards_root.py -- the one place that answers "where do the guard scripts live?"

WHY THIS EXISTS. Founder, 2026-09-20: "nothing should be dependent on .claude folder, we are
model agnostic." The guard scripts used to be reached as ~/.claude/scripts, a path named after
one vendor's tool. It was fragile too: that directory was a submodule of ~/.claude, so a rename,
a fresh clone or a submodule update left dozens of references pointing at an empty directory and
the failure was silent.

THE FIX. Scripts are addressed by where they actually are: this file's parent directory. Any
script that needs a sibling does:

    from guards_root import GUARDS_ROOT          # when this dir is importable
    sys.path.insert(0, str(GUARDS_ROOT))         # put siblings on the path

From a subdirectory (estate/, drills/) use the bootstrap, which finds this file by walking up
from __file__ and needs no path setup of its own:

    from guards_root import guards_root; guards_root.bootstrap()

Set the GUARDS_ROOT environment variable to override the location entirely.
"""
from __future__ import annotations

import os
import pathlib
import sys

_MARKER = "guards_root.py"


def _find_root() -> pathlib.Path:
    env = os.environ.get("GUARDS_ROOT")
    if env:
        return pathlib.Path(env).expanduser().resolve()
    here = pathlib.Path(__file__).resolve().parent
    if (here / _MARKER).exists():
        return here
    for parent in here.parents:
        if (parent / _MARKER).exists():
            return parent
    return here


GUARDS_ROOT: pathlib.Path = _find_root()


def guards_path(*parts: str) -> str:
    """Absolute path to something under the guard scripts root."""
    return str(GUARDS_ROOT.joinpath(*parts))


def add_to_path() -> pathlib.Path:
    """Put GUARDS_ROOT on sys.path (front) and return it. Idempotent."""
    root = str(GUARDS_ROOT)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    return GUARDS_ROOT


def bootstrap() -> pathlib.Path:
    """Make `import guards_root` work from any subdirectory, then add the root to sys.path.

    Safe to call more than once. Scripts in estate/, drills/ etc. call this before importing
    their siblings.
    """
    root = GUARDS_ROOT
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if __name__ != "guards_root":
        sys.modules.setdefault("guards_root", sys.modules[__name__])
    return root


if __name__ == "__main__":
    print(GUARDS_ROOT)

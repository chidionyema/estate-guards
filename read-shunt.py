#!/usr/bin/env python3
"""read-shunt.py — Claude Code PreToolUse shim over the read-shunt core.

Thin adapter only: it owns the Claude Code hook contract (stdin JSON -> stdout
hookSpecificOutput) and delegates every decision to read_shunt/core.py, which is shared
with the pi extension and the MCP/CLI. Model-agnostic and fail-open — see core.py.

Off switch: READ_SHUNT=off.
"""
from __future__ import annotations

import json
import os
import sys

try:
    from read_shunt.config import is_off, load_config
    from read_shunt.core import shunt_file, target_file
    from read_shunt.ledger import report
except ImportError:  # package not on path (e.g. deployed as a bare file) — pass everything through
    def _unavailable():
        return 0

    is_off = lambda _c: True  # noqa: E731
    load_config = lambda: {}  # noqa: E731
    shunt_file = lambda *_a, **_k: {"outcome": "passed-through"}  # noqa: E731
    target_file = lambda _p: None  # noqa: E731
    report = lambda _c: 0  # noqa: E731


def main() -> int:
    if "--report" in sys.argv:
        return report(load_config())
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    cfg = load_config()
    if is_off(cfg):
        return 0
    path = target_file(payload)
    if not path or not os.path.isfile(path):
        return 0
    result = shunt_file(path, cfg, session_id=payload.get("session_id", ""))
    if result.get("outcome") == "shunted":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": result["reason"],
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    # hook-run grades a crashed hook as a refusal (crew#603), so a crash here would refuse
    # every read and every shell call in the estate. The shunt is an optimisation, never a
    # gate: it exits 0 on anything it did not foresee.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"read-shunt: passed through: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)

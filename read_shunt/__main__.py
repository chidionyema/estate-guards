"""read-shunt CLI.

    python -m read_shunt digest <path>   # force a digest of one file
    python -m read_shunt hook            # neutral hook: payload JSON on stdin, result JSON out
    python -m read_shunt --report        # sum the ledger

`hook` is the shared entry point for agent shims (Claude Code, pi, MCP): it takes a
neutral payload and does the full decision in the core, so no adapter carries policy.

Payload in:  {"tool_name": "Read"|"Bash", "tool_input": {...}, "cwd": "...", "session_id": "..."}
Result out:  {"outcome": "shunted", "digest": "...", "reason": "..."}
          or {"outcome": "passed-through"}
"""
from __future__ import annotations

import json
import os
import sys

from .config import is_off, load_config
from .core import digest, shunt_file, target_file
from .ledger import report


def _usage() -> None:
    print("usage: python -m read_shunt digest <path> | hook | --report", file=sys.stderr)


def _hook() -> int:
    cfg = load_config()
    if is_off(cfg):
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    path = target_file(payload)
    if not path or not os.path.isfile(path):
        return 0
    result = shunt_file(path, cfg, session_id=payload.get("session_id", ""))
    if result.get("outcome") == "shunted":
        print(json.dumps({"outcome": "shunted", "digest": result["digest"], "reason": result["reason"]}))
    else:
        print(json.dumps({"outcome": "passed-through"}))
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--report":
        return report(load_config())
    if argv and argv[0] == "hook":
        return _hook()
    if len(argv) >= 2 and argv[0] == "digest":
        cfg = load_config()
        if is_off(cfg):
            print(json.dumps({"error": "read-shunt is off (READ_SHUNT=off or enabled:false)"}), file=sys.stderr)
            return 1
        try:
            d = digest(argv[1], cfg)
        except Exception as e:  # the CLI surfaces the reason; hooks stay fail-open
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
            return 1
        print(d["digest"])
        return 0
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

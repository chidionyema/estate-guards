"""Neutral ledger for read-shunt. One line per shunted (or passed-through) read.

Default location is ~/.read-shunt/ledger.jsonl (agent-neutral), overridable via
READ_SHUNT_LEDGER or the config file. `report` sums the ledger — that is the number,
never a memory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def ledger_path(cfg: dict) -> Path:
    return Path(cfg.get("ledger_path") or "").expanduser()


def ledger_write(row: dict, cfg: dict) -> None:
    try:
        p = ledger_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception as e:  # the ledger may never fail the hook (LAW 38)
        print(f"read-shunt: ledger not written: {e}", file=os.sys.stderr)


def report(cfg: dict) -> int:
    n = tok = saved = 0
    try:
        for line in open(ledger_path(cfg)):
            r = json.loads(line)
            if r.get("outcome") != "shunted":
                continue
            n += 1
            tok += int(r.get("worker_prompt_tokens", 0)) + int(r.get("worker_completion_tokens", 0))
            saved += int(r.get("est_frontier_tokens_avoided", 0))
    except OSError as e:  # no ledger yet is a zero report, not a failure
        print(f"read-shunt: no ledger read: {e}", file=os.sys.stderr)
    print(
        json.dumps(
            {
                "shunted_reads": n,
                "worker_tokens": tok,
                "est_frontier_tokens_avoided": saved,
                "ledger": str(ledger_path(cfg)),
            }
        )
    )
    return 0

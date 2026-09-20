#!/usr/bin/env python3
"""telegram_ledger — a record of what this estate actually sent to the operator.

Written 2026-08-19, because the founder said the Telegram channel was too noisy to
find anything useful in, and NOTHING recorded what had been sent. Both alert paths
already debounce (`estate_alert._debounced`, `estate_watchdog._alert`), so the
obvious suspects were already bounded — and with no ledger there was no way to say
which sender was actually filling the channel. A noise cap chosen without that
measurement is a guess that silences the wrong thing.

Two rules this file follows because they are the reason it exists:

  It records SUPPRESSED sends too. A debounce that is doing its job is invisible,
  and an invisible mechanism gets removed by someone who thinks it does nothing.

  It bounds itself. Adding an unbounded log to fix a storage problem is the joke
  version of this work. `_trim` keeps the newest `_KEEP_LINES` and nothing older.

Never raises. An alert path that dies because its bookkeeping failed is worse than
no bookkeeping.
"""
from __future__ import annotations

# --- guards root bootstrap: find the repo root from anywhere, no ~/.claude path ---
import pathlib as _gr_pl, sys as _gr_sys
_gr_here = _gr_pl.Path(__file__).resolve()
for _gr_p in [_gr_here.parent, *_gr_here.parents]:
    if (_gr_p / "guards_root.py").exists():
        _gr_sys.path.insert(0, str(_gr_p)); break
else:
    raise RuntimeError("guards_root.py not found above " + str(_gr_here))
# --- end bootstrap ---
import json
import os
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
LEDGER = HERMES_HOME / "state" / "telegram_sent.jsonl"

_KEEP_LINES = 20_000          # ~4 MB at these row sizes; months of history at current volume
_TRIM_ABOVE_BYTES = 8 << 20   # only pay for a rewrite when the file has actually grown

# Every outcome an attempted send can have. `suppressed` is the debounce working;
# `muted` is the operator's own switch; `no-creds` is a misconfiguration that would
# otherwise look identical to silence.
# `edited` is deliberately its own outcome and NOT a send: the coordinator's progress
# stream edits one message per task rather than posting a line per step, so counting an
# edit as a send would rank the quietest design as the loudest sender.
OUTCOMES = ("sent", "edited", "suppressed", "rate-capped", "failed", "no-creds", "muted")


def _trim() -> None:
    try:
        if LEDGER.stat().st_size < _TRIM_ABOVE_BYTES:
            return
        lines = LEDGER.read_text(errors="replace").splitlines()[-_KEEP_LINES:]
        LEDGER.write_text("\n".join(lines) + "\n")
    except OSError:
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 50))
        except Exception: pass


def record(source: str, outcome: str, text: str = "", *, key: str = "",
           msg_id: int = 0) -> None:
    """Append one row. `source` is the sender, not the caller — one name per send path.

    `msg_id` is Telegram's own message_id. An HTTP 200 says the API accepted the call;
    a message_id says a message exists in the chat. Only the second is an arrival
    receipt, and a row of "sent" with msg_id 0 is a send nobody has proved landed.
    """
    try:
        row = {
            "ts": round(time.time(), 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": source,
            "outcome": outcome,
            "chars": len(text),
            "key": key,
            "head": " ".join(text.split())[:100],
            "msg_id": msg_id,
        }
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        _trim()
    except Exception:
        # Bookkeeping never breaks the thing it is bookkeeping for.
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 77))
        except Exception: pass


def read(since_s: float = 0.0) -> list[dict]:
    """Every row newer than `since_s` seconds ago. A malformed line is skipped, not fatal."""
    cutoff = time.time() - since_s if since_s > 0 else 0.0
    out: list[dict] = []
    try:
        with LEDGER.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if float(row.get("ts") or 0) >= cutoff:
                    out.append(row)
    except OSError:
        pass
    return out

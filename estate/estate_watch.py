#!/usr/bin/env python3
"""The reader for estate_audit.py. Without this, the audit is a log nobody opens.

Founder, 2026-08-22: "still process has gaps in visibility and application". The gap was
never the scanner. estate_audit.py has run hourly since 2026-08-21, checks 48 things, and
reported 9 critical findings into
/Users/chidionyema/.claude/state/logs/estate-audit.err.log every hour. Nothing read it.
Two of those criticals were leaked credentials.

Every piece needed already existed and none of them were joined:
    estate_audit.py                 the scanner        -- worked, unread
    ~/.hermes/scripts/estate_alert.py  the sender      -- worked, uncalled
    board_serve.py /audit?t=TOKEN   the dashboard      -- worked, token-gated
This file is the wire between the first two. It is deliberately small; the components are
not being rebuilt (LAW 3).

LAW 28: what it emits, who receives it, and what happens when nobody acts.
    emits    one Telegram line per NEWLY critical finding, and one when a finding clears
    receives TELEGRAM_HOME_CHANNEL, via estate_alert.send_operator_alert
    unacted  a finding still critical after ESCALATE_AFTER_H is re-sent once, tagged STILL

Only CHANGES are sent. Re-sending nine unchanged criticals every hour is how a channel
becomes noise, and a noisy channel is an unread channel, which is the defect this fixes.

A scanner that stopped is itself an alert. If the audit on disk is older than STALE_H the
silence is reported, because "no criticals" and "no scan" look identical from here.

    estate_watch.py                 read the audit, send what changed
    estate_watch.py --dry-run       print what would be sent, send nothing
    estate_watch.py --summary       one line, for the board and for /estate
    estate_watch.py --json          machine-readable, for hermes v2
    estate_watch.py --selftest      prove it works
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

HOME = pathlib.Path.home()
AUDIT = HOME / ".claude/state/estate-audit.json"
SEEN = HOME / ".claude/state/estate-watch-seen.json"
# The audit runs every 6 hours (launchagents/com.founder.estateaudit.plist, staggered in #44 for
# load on the 16 GB Mac). 3 h here called the snapshot STALE for half of every day (crew#25,
# 2026-08-26: "scanned 191m ago STALE" while the audit was on schedule). One period plus an hour.
STALE_H = float(os.environ.get("ESTATE_WATCH_STALE_H", "7"))
ESCALATE_AFTER_H = float(os.environ.get("ESTATE_WATCH_ESCALATE_H", "24"))

# estate_alert.py now lives beside this file. ~/.hermes is the discontinued
# estate; keep it as a fallback only so an old checkout still sends.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.append(str(HOME / ".hermes/scripts"))


def _send(text: str, key: str, dry: bool) -> bool:
    """Never let alerting crash the watcher: an unsent alert is bad, a dead watcher is worse."""
    try:
        import estate_alert
    except Exception as exc:                                  # noqa: BLE001
        print(f"[estate_watch] sender unavailable: {exc!r}", file=sys.stderr)
        return False
    return estate_alert.send_operator_alert(text, debounce_key=key, debounce_s=1800.0,
                                            dry_run=dry)


def load_audit():
    """(payload, age_seconds) or (None, None). A missing audit is a finding, not an error."""
    try:
        with open(AUDIT) as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None, None
    return payload, time.time() - float(payload.get("generated_at") or 0)


def criticals(payload) -> list[dict]:
    return [r for r in payload.get("rows", []) if r.get("severity") == "critical"]


def _key(row: dict) -> str:
    return f"{row.get('domain')}::{row.get('title')}"


def load_seen() -> dict:
    try:
        with open(SEEN) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_seen(seen: dict) -> None:
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(seen, fh, indent=1, sort_keys=True)
    os.replace(tmp, SEEN)                                     # atomic: never a half-written state


def summary_line(payload, age_s) -> str:
    if payload is None:
        return f"ESTATE: no audit on disk at {AUDIT}"
    c = payload.get("counts", {})
    stale = " STALE" if age_s is not None and age_s > STALE_H * 3600 else ""
    mins = int((age_s or 0) / 60)
    return (f"ESTATE: {c.get('critical', 0)} critical, {c.get('warn', 0)} warn, "
            f"{c.get('unknown', 0)} unknown, {c.get('ok', 0)} ok "
            f"(scanned {mins}m ago{stale})")


def run(dry: bool = False) -> int:
    payload, age_s = load_audit()
    now = time.time()
    seen = load_seen()

    if payload is None:
        _send(f"🚨 ESTATE SCANNER SILENT\nNo audit at {AUDIT}.\n"
              f"Rebuild: python3 $HOME/Documents/code/claude-guards/estate_audit.py --html --state",
              "estate-watch-missing", dry)
        print("no audit on disk")
        return 1

    if age_s > STALE_H * 3600:
        _send(f"🚨 ESTATE SCANNER STALE\nLast scan {int(age_s / 3600)}h ago, deadline {STALE_H}h.\n"
              f"No scan and no findings look identical from the channel, so this is the alert.\n"
              f"Check: launchctl list | grep estateaudit",
              "estate-watch-stale", dry)

    live = {_key(r): r for r in criticals(payload)}
    prev = set(seen.get("critical", {}))
    new = [k for k in live if k not in prev]
    gone = [k for k in prev if k not in live]

    # LAW 28: a finding is only "seen" once the alert ARRIVED. Recording it on the attempt
    # loses it forever the first time a send is suppressed, rate-capped or fails the network --
    # measured 2026-08-22, the first armed run marked all 8 criticals seen while sending 0.
    delivered = set()
    for k in new:
        r = live[k]
        if _send(f"🔴 ESTATE CRITICAL\n{r.get('title')}  —  {r.get('value')}\n"
                 f"{' '.join(str(r.get('detail', '')).split())[:400]}\n"
                 f"proof: {r.get('proof', '')[:160]}", f"estate-crit-{k}", dry):
            delivered.add(k)
    for k in gone:
        _send(f"🟢 ESTATE CLEARED\n{k.split('::', 1)[-1]}", f"estate-clear-{k}", dry)

    # LAW 28's third question: what happens when nobody acts. It gets said again, once.
    still = []
    for k, r in live.items():
        first = seen.get("critical", {}).get(k, {}).get("first_seen", now)
        if k not in new and now - first > ESCALATE_AFTER_H * 3600 \
                and not seen.get("critical", {}).get(k, {}).get("escalated"):
            still.append(k)
    if still:
        body = "\n".join(f"• {k.split('::', 1)[-1]}" for k in still[:8])
        _send(f"⏳ ESTATE: {len(still)} critical finding(s) unactioned for "
              f"{int(ESCALATE_AFTER_H)}h\n{body}", "estate-watch-still", dry)

    if not dry:
        out = {}
        for k, r in live.items():
            if k in new and k not in delivered:
                continue                  # never delivered -> not seen -> retried next run
            old = seen.get("critical", {}).get(k, {})
            out[k] = {"first_seen": old.get("first_seen", now),
                      "escalated": old.get("escalated") or (k in still),
                      "title": r.get("title")}
        save_seen({"critical": out, "last_run": now})

    print(summary_line(payload, age_s))
    print(f"new={len(new)} delivered={len(delivered)} cleared={len(gone)} "
          f"still={len(still)} dry_run={dry}")
    return 0


def selftest() -> int:
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"  {name}: want {want!r}, got {got!r}")

    payload = {"generated_at": time.time(), "counts": {"critical": 2, "warn": 1,
                                                       "unknown": 0, "ok": 3},
               "rows": [{"domain": "a", "title": "T1", "severity": "critical"},
                        {"domain": "b", "title": "T2", "severity": "critical"},
                        {"domain": "c", "title": "T3", "severity": "warn"}]}
    check("criticals are filtered by severity", len(criticals(payload)), 2)
    check("key is domain-scoped", _key(payload["rows"][0]), "a::T1")
    check("two rows with the same title in different domains are different findings",
          _key({"domain": "z", "title": "T1"}) == _key(payload["rows"][0]), False)
    check("summary names every count",
          all(x in summary_line(payload, 60) for x in ("2 critical", "1 warn", "3 ok")), True)
    check("a stale audit is marked in the summary",
          "STALE" in summary_line(payload, STALE_H * 3600 + 1), True)
    check("a fresh audit is not marked stale", "STALE" in summary_line(payload, 60), False)
    check("crew#25 incident: 191 min on a 6 h audit schedule is not stale",
          "STALE" in summary_line(payload, 191 * 60), False)
    check("one missed 6 h audit slot is stale", "STALE" in summary_line(payload, 7 * 3600 + 1), True)
    check("no audit on disk is a sentence, not a crash",
          summary_line(None, None).startswith("ESTATE: no audit"), True)

    # The whole point: an unchanged estate must send NOTHING. A watcher that re-sends nine
    # criticals every hour trains the founder to mute the channel, which is the defect.
    import tempfile
    global AUDIT, SEEN
    keep_a, keep_s = AUDIT, SEEN
    try:
        d = pathlib.Path(tempfile.mkdtemp())
        AUDIT, SEEN = d / "a.json", d / "s.json"
        AUDIT.write_text(json.dumps(payload))
        sent = []
        globals()["_send"] = lambda t, k, dry: sent.append(k) or True
        run(dry=False)
        check("first run alerts on every critical", sorted(sent), ["estate-crit-a::T1",
                                                                  "estate-crit-b::T2"])
        sent.clear()
        run(dry=False)
        check("an unchanged estate sends nothing", sent, [])
        # An undelivered alert must NOT be marked seen, or the finding is lost forever.
        SEEN.unlink(missing_ok=True)
        globals()["_send"] = lambda t, k, dry: False
        run(dry=False)
        check("a finding whose alert never arrived is not marked seen",
              json.loads(SEEN.read_text())["critical"], {})
        globals()["_send"] = lambda t, k, dry: sent.append(k) or True
        sent.clear()
        run(dry=False)
        check("it is retried on the next run", sorted(sent),
              ["estate-crit-a::T1", "estate-crit-b::T2"])

        payload["rows"] = [payload["rows"][0], payload["rows"][2]]
        payload["counts"]["critical"] = 1
        AUDIT.write_text(json.dumps(payload))
        sent.clear()
        run(dry=False)
        check("a resolved finding is reported once", sent, ["estate-clear-b::T2"])
    finally:
        AUDIT, SEEN = keep_a, keep_s

    if fails:
        print("estate_watch selftest FAILED:")
        print("\n".join(fails))
        return 1
    print("estate_watch selftest: all passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.summary or a.json:
        payload, age_s = load_audit()
        if a.json:
            print(json.dumps({
                "summary": summary_line(payload, age_s),
                "counts": (payload or {}).get("counts", {}),
                "age_s": age_s,
                "stale": age_s is None or age_s > STALE_H * 3600,
                "critical": [{"domain": r.get("domain"), "title": r.get("title"),
                              "value": r.get("value"), "proof": r.get("proof")}
                             for r in criticals(payload or {})],
            }, indent=1))
        else:
            print(summary_line(payload, age_s))
        return 0
    return run(dry=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""One command that runs every guard's own selftest and says PASS or FAIL out loud.

    estate-selftest.py            run everything, print the board
    estate-selftest.py --json     machine output
    estate-selftest.py --quiet    only print if something failed (for launchd)

Exit code is 0 only when nothing failed. Anything red exits 1 so a scheduler,
a CI job or a hook can act on it without reading the text.
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, json, os, pathlib, re, subprocess, sys, time

HOME = pathlib.Path.home()
ROOT = pathlib.Path(os.environ.get("GUARDS_ROOT") or pathlib.Path(__file__).resolve().parent)
STATE = HOME / ".claude/state/estate-selftest.json"
PREV = HOME / ".claude/state/estate-selftest.prev.json"
PY3 = sys.executable or "python3"
TIMEOUT = 150.0   # estate_audit.py alone takes 30-45s; a tight cap makes this cry wolf

G, R, Y, D, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    G = R = Y = D = B = X = ""


def discover() -> list[pathlib.Path]:
    seen, out = set(), []
    for p in sorted(ROOT.glob("*.py")) + sorted(ROOT.glob("*/*.py")):
        if p.name.startswith("test_") or p.resolve() in seen:
            continue
        seen.add(p.resolve())
        out.append(p)
    return out


def wired_hooks() -> set[str]:
    try:
        d = json.loads((HOME / ".claude/settings.json").read_text())
    except Exception:
        return set()
    return {h.get("command", "").split("/")[-1]
            for arr in d.get("hooks", {}).values() for g in arr for h in g.get("hooks", [])}


def run_one(p: pathlib.Path) -> dict:
    rel = p.relative_to(ROOT).as_posix()
    try:
        src = p.read_text(errors="replace")
    except Exception as exc:
        return {"name": rel, "status": "UNREADABLE", "detail": repr(exc), "secs": 0.0}
    hookish = bool(re.search(r"hook_event_name|json\.load\(sys\.stdin\)", src))
    if "--selftest" not in src:
        return {"name": rel, "status": "NO SELFTEST", "detail": "", "secs": 0.0, "hook_shaped": hookish}
    t0 = time.monotonic()
    try:
        r = subprocess.run([PY3, str(p), "--selftest"], capture_output=True,
                           text=True, timeout=TIMEOUT, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return {"name": rel, "status": "TIMEOUT", "detail": f"no exit in {TIMEOUT:.0f}s",
                "secs": time.monotonic() - t0, "hook_shaped": hookish}
    secs = time.monotonic() - t0
    blob = (r.stderr or "") + (r.stdout or "")
    if "unrecognized arguments: --selftest" in blob:
        return {"name": rel, "status": "NO SELFTEST", "detail": "mentions the flag, does not accept it", "secs": secs, "hook_shaped": hookish}
    if "ModuleNotFoundError" in blob:
        mod = re.search(r"No module named '([^']+)'", blob)
        return {"name": rel, "status": "MISSING DEP", "detail": f"needs {mod.group(1)}" if mod else blob.strip()[-100:], "secs": secs, "hook_shaped": hookish}
    if r.returncode == 0:
        tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
        return {"name": rel, "status": "PASS", "detail": tail[-1][:80] if tail else "", "secs": secs, "hook_shaped": hookish}
    err = (r.stderr or r.stdout).strip().splitlines()
    return {"name": rel, "status": "FAIL", "detail": (err[-1][:120] if err else f"rc={r.returncode}"),
            "secs": secs, "hook_shaped": hookish}


def _alert(bad: list[dict], payload: dict) -> None:
    """Say it out loud. An alert that fails to arrive is worse than no alert (LAW 28)."""
    sys.path.insert(0, str(ROOT / "estate"))
    v = payload["verdict"]
    detail = {r["name"]: r["detail"] for r in bad}
    lines = [f"GUARDS {v['severity']}: {v['headline']}"]
    for n in v["broke"][:6]:
        lines.append(f"  BROKE  {n} — {detail.get(n, '')}")
    for n in v["still"][:4]:
        lines.append(f"  still  {n} — {detail.get(n, '')}")
    for n in v["fixed"][:6]:
        lines.append(f"  fixed  {n}")
    for n in v["gone"][:4]:
        lines.append(f"  gone   {n} is no longer on disk")
    if v["idle"]:
        lines.append(f"  {len(v['idle'])} guards pass their own tests and are switched off")
    lines.append(f"  board: http://127.0.0.1:8787")
    try:
        import estate_alert
        ok = estate_alert.send_operator_alert("\n".join(lines),
                                              debounce_key=f"guard-selftest-{payload['verdict']['headline']}",
                                              debounce_s=3600.0)
        print(f"  alert delivered={ok}")
    except Exception as exc:                                       # noqa: BLE001
        print(f"  {R}alert FAILED to send: {exc!r}{X}", file=sys.stderr)


def verdict(rows: list[dict], prev: dict | None) -> dict:
    """Say what CHANGED and what it means, not what the count is.

    A checker that reports the same 31/60 every twelve hours trains its reader to
    stop looking (LAW 28). What is worth a message is a transition: something that
    worked and now does not, something that was broken and now is not, and a guard
    that is switched off while its own tests pass -- which is capability sitting
    unused rather than a fault.
    """
    now = {r["name"]: r["status"] for r in rows}
    was = {r["name"]: r["status"] for r in (prev or {}).get("rows", [])}
    RED = ("FAIL", "TIMEOUT", "UNREADABLE")

    broke = [n for n, st in now.items() if st in RED and was.get(n) not in RED and n in was]
    fixed = [n for n, st in now.items() if st not in RED and was.get(n) in RED]
    still = [n for n, st in now.items() if st in RED and was.get(n) in RED]
    new   = [n for n in now if n not in was] if was else []
    gone  = [n for n in was if n not in now]
    # A guard that passes its own tests and is not wired is capability you paid for
    # and switched off. It is not a fault, so it never goes red, but it is the
    # single most useful thing this tool knows.
    idle  = [r["name"] for r in rows
             if r["status"] == "PASS" and r.get("hook_shaped") and not r["wired"]]

    if broke:
        head, sev = f"{len(broke)} guard(s) just broke", "RED"
    elif still:
        head, sev = f"{len(still)} guard(s) still broken", "RED"
    elif fixed:
        head, sev = f"{len(fixed)} guard(s) recovered, none broken", "GREEN"
    elif gone:
        head, sev = f"{len(gone)} guard(s) disappeared from disk", "AMBER"
    else:
        head, sev = "no change", "GREEN"

    return {"severity": sev, "headline": head, "broke": broke, "fixed": fixed,
            "still": still, "new": new, "gone": gone, "idle": idle,
            # Speak only on a transition. Steady state is the board's job, not a message.
            "worth_saying": bool(broke or fixed or gone or (still and not prev))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="print only on failure")
    ap.add_argument("--alert", action="store_true", help="send a Telegram alert when something fails")
    ap.add_argument("--selftest", action="store_true", help="prove this script itself works")
    a = ap.parse_args()

    if a.selftest:
        ok = discover()
        assert ok, "discover() found no scripts"
        assert all(p.suffix == ".py" for p in ok)
        probe = run_one(ROOT / "close-guard.py")   # crew#638: jargon-guard was deleted
        assert probe["status"] in ("PASS", "FAIL", "NO SELFTEST"), probe
        print(f"selftest OK: {len(ok)} scripts discoverable, runner returns {probe['status']}")
        return 0

    files = discover()
    wired = wired_hooks()
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(run_one, files))
    rows.sort(key=lambda r: ({"FAIL": 0, "TIMEOUT": 0, "UNREADABLE": 0,
                              "MISSING DEP": 1, "NO SELFTEST": 2, "PASS": 3}[r["status"]], r["name"]))
    for r in rows:
        r["wired"] = r["name"].split("/")[-1] in wired

    try:
        prev = json.loads(STATE.read_text()) if STATE.exists() else None
    except Exception:
        prev = None
    v = verdict(rows, prev)
    bad = [r for r in rows if r["status"] in ("FAIL", "TIMEOUT", "UNREADABLE")]
    dep = [r for r in rows if r["status"] == "MISSING DEP"]
    gap = [r for r in rows if r["status"] == "NO SELFTEST"]
    good = [r for r in rows if r["status"] == "PASS"]
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "elapsed_s": round(time.time() - t0, 1),
               "pass": len(good), "fail": len(bad), "no_selftest": len(gap), "missing_dep": len(dep),
               "wired": sum(r["wired"] for r in rows), "verdict": v, "rows": rows}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        PREV.write_text(STATE.read_text())
    STATE.write_text(json.dumps(payload, indent=1) + "\n")

    if a.json:
        print(json.dumps(payload, indent=1))
        return 1 if bad else 0
    if a.quiet and not v["worth_saying"]:
        return 1 if bad else 0
    alerted = False
    if a.quiet and a.alert and v["worth_saying"]:
        _alert(bad, payload); alerted = True

    col = R if v["severity"] == "RED" else (Y if v["severity"] == "AMBER" else G)
    banner = f"{col}{B}  {v['headline'].upper()}  {X}"
    print(f"\n{'=' * 64}\n{banner}   {len(good)} pass · {len(bad)} fail · "
          f"{len(gap)} untested · {payload['wired']} switched on\n{'=' * 64}")
    for r in bad + dep:
        col = R if r in bad else Y
        print(f"  {col}{r['status']:<11}{X} {r['name']:<30} {D}{r['detail']}{X}")
    if bad or dep:
        print()
    for r in good:
        print(f"  {G}pass{X}        {r['name']:<30} {D}{r['secs']:4.1f}s "
              f"{'[ON]' if r['wired'] else '':4} {r['detail']}{X}")
    if v["idle"]:
        print(f"\n  {Y}written and switched off{X} ({len(v['idle'])}): "
              f"{D}{', '.join(v['idle'])}{X}")
    if gap:
        print(f"\n  {Y}no selftest{X} ({len(gap)}): {D}{', '.join(r['name'] for r in gap)}{X}")
    print(f"\n  state: {STATE}   took {payload['elapsed_s']}s\n")
    if a.alert and v["worth_saying"] and not alerted:
        _alert(bad, payload)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

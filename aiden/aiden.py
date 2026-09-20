#!/usr/bin/env python3
"""Aiden. It watches every Claude Code session on this machine and it costs nothing.

The founder's requirement was not "build a watcher". It was that watching must
not be something he pays for. So nothing in this program asks a model anything.
Every fact on the board was already written to this disk by the sessions
themselves, and reading your own logs is free.

  aiden.py board          what every session is doing and what it has cost
  aiden.py cost [days]    where the money actually goes, deduplicated
  aiden.py alerts         only the lines a person would want to be woken for
  aiden.py html <path>    the same board as a page, for a phone

The one number worth understanding is the reuse ratio, `cr/cw` on the board.
A cached prefix costs 1.25x to write and 0.1x to read, so it pays for itself on
the thirteenth read and is a straight loss below that. The estate has already
measured what the bad end of that looks like: a daemon running every call in a
fresh temp directory read its cache 0.72 times on average, and reusing the
directory made an identical call 8.6x cheaper with byte-identical output
(~/.claude/ESTATE_COST_AUDIT_2026-08-06.md). A low ratio on this board is that
same defect, visible before it has run for a week.
"""
import importlib.util
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
#: One instance of observe per process, shared through sys.modules.
#:
#: `module_from_spec` does not register anything, so every loader that reached for this file got a
#: private copy with a private `_CACHE`, and the walk that copy exists to avoid ran again. Inside
#: one tick that was two full passes of ~/.claude/projects: this module's, and the ticket gate's
#: when it rebuilt the ops page. Measured 2026-08-23 under /usr/bin/python3: a cold
#: `observe.sessions(3)` is 9.8s and the second call on the same instance is 0.0s, so the whole
#: cost of the duplicate is the duplication itself.
_OBS_KEY = "aiden_observe"
observe = sys.modules.get(_OBS_KEY)
if observe is None:
    _spec = importlib.util.spec_from_file_location(
        _OBS_KEY, os.path.join(
            os.environ.get("GUARDS_ROOT")
            or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "aiden", "observe.py"))
    observe = importlib.util.module_from_spec(_spec)
    sys.modules[_OBS_KEY] = observe
    _spec.loader.exec_module(observe)

#: A prefix cached at 1.25x and read at 0.1x breaks even at 12.5 reads. Below
#: this the writes are not paying for themselves and something is restarting
#: that should be resuming.
BREAKEVEN = 12.5
#: Spend rate that is worth interrupting a person for. Below it, the board is
#: enough; above it, something is running away.
BURN_ALERT_USD_PER_HOUR = 40.0
#: A session that has said nothing for this long has finished and is waiting.
WAITING_MINUTES = 10


def state_of(r):
    """What this session is, in one word, decided from its own words."""
    first = (r["text"].splitlines() or [""])[0].strip()
    if first.startswith("BLOCKED"):
        return "BLOCKED"
    if r["idle"] < 120:
        return "RUNNING"
    if not r["text"].strip():
        # crew#52: a session that has never said anything is not a stalled agent, it is an
        # empty slot. 7 of 9 alerts on 2026-08-23 were WAITING lines from one such session.
        return "IDLE"
    if first.startswith("DONE"):
        return "DONE"
    if r["idle"] / 60 > WAITING_MINUTES:
        return "WAITING"
    return "IDLE"


def reuse(r):
    return r["cache_read"] / r["cache_write"] if r["cache_write"] else 0.0


def rate_usd_h(r, window=900):
    """Spend inside the last `window` seconds, projected to an hour.

    An earlier version of this divided lifetime spend by seconds-since-last-write
    and reported $91,311/hour, which is what happens when a rate is computed from
    two numbers that were never a rate. The window is real time, measured from
    the timestamps the transcript already carries.
    """
    cut = time.time() - window
    spent = sum(u for t, u in r.get("recent", []) if t >= cut)
    return spent * 3600.0 / window


def board(hours=24):
    rows, errors = observe.sessions(hours)
    if not rows:
        print("no session has written anything in the last %dh" % hours)
        return rows, errors
    total = sum(r["usd"] for r in rows)
    print(f"{'state':<8} {'project':<34} {'idle':>7} {'spend':>9} {'req':>5} "
          f"{'reuse':>6}  says")
    print("-" * 118)
    for r in rows:
        st = state_of(r)
        first = (r["text"].splitlines() or [""])[0][:38]
        ru = reuse(r)
        flag = "!" if 0 < ru < BREAKEVEN else " "
        print(f"{st:<8} {r['slug'][:34]:<34} {r['idle']/60:>6.1f}m "
              f"${r['usd']:>8.2f} {r['requests']:>5} {ru:>5.1f}x{flag} {first}")
    print("-" * 118)
    print(f"{len(rows)} sessions, ${total:,.2f} in the last {hours}h")
    if errors:
        print(f"{len(errors)} transcript(s) could not be read:")
        for e in errors[:5]:
            print("   ", e)
    return rows, errors


def ticket_of(session):
    """The GitHub issue this session is working under, as a short tag.

    Written by ticket-gate.py, which opens the issue itself the first time a session changes
    anything. He prompts several tabs at once, so a status line that does not name the issue
    leaves him holding the thread. Empty string when the session has not changed anything yet;
    that is not a fault, it is a session that has only been reading.
    """
    import json as _json
    path = os.path.join(HOME, ".claude", "state", "tickets", "%s.json" % session)
    try:
        with open(path, encoding="utf-8") as fh:
            b = _json.load(fh)
    except Exception:
        return ""
    if b.get("issue"):
        return "#%s " % b["issue"]
    return "NO-TICKET "


def alerts(hours=24):
    """Only what a person would want to be woken for. Silence means nothing is wrong,
    and the board is what proves the checker itself is alive."""
    rows, errors = observe.sessions(hours)
    out = []
    for r in rows:
        st = state_of(r)
        first = (r["text"].splitlines() or [""])[0][:120]
        tk = ticket_of(r["session"])
        if tk == "NO-TICKET ":
            #: The gate tried to open one and GitHub refused. That is work happening off the
            #: board, which is the exact thing he asked to be impossible.
            out.append(f"NO TICKET {r['slug']}: could not open an issue, work is off the board")
        if st == "BLOCKED":
            out.append(f"BLOCKED  {tk}{r['slug']}: {first}")
        elif st == "WAITING" and r["idle"] / 60 < 180:
            out.append(f"WAITING  {tk}{r['slug']} for {r['idle']/60:.0f} min: {first}")
        ru = reuse(r)
        if r["cache_write"] > 200_000 and ru < BREAKEVEN:
            waste = r["usd"] * (1 - ru / BREAKEVEN) if ru else r["usd"]
            out.append(f"CHURN    {r['slug']}: cache read {ru:.1f}x, breakeven is "
                       f"{BREAKEVEN:.0f}x, about ${waste:.2f} of this ${r['usd']:.2f} "
                       f"bought nothing")
        rt = rate_usd_h(r)
        if rt > BURN_ALERT_USD_PER_HOUR:
            out.append(f"BURN     {r['slug']}: ${rt:.0f}/hour right now")
    for e in errors:
        out.append(f"UNREAD   {e}")
    return out


def cost(days=7):
    """Where the money goes, by day, deduplicated by message id."""
    rows, _ = observe.sessions(days * 24)
    tot = {k: 0 for k in ("input", "cache_write", "cache_read", "output")}
    usd = 0.0
    for r in rows:
        for k in tot:
            tot[k] += r[k]
        usd += r["usd"]
    ta = observe._ta
    parts = [
        ("fresh input", tot["input"] / 1e6 * 5.0),
        ("cache write", tot["cache_write"] / 1e6 * 5.0 * ta.WRITE_1H),
        ("cache read", tot["cache_read"] / 1e6 * 5.0 * ta.READ_MULT),
        ("output", tot["output"] / 1e6 * 25.0),
    ]
    s = sum(p[1] for p in parts) or 1
    print(f"last {days}d, {len(rows)} sessions, priced at the estate's own constants")
    for name, v in parts:
        print(f"  {name:<12} ${v:>10,.2f}   {v/s*100:>5.1f}%")
    print(f"  {'TOTAL':<12} ${usd:>10,.2f}   ${usd/max(days,1):,.2f}/day")
    ru = tot["cache_read"] / tot["cache_write"] if tot["cache_write"] else 0
    print(f"\n  context re-read {ru:.1f}x per write, breakeven {BREAKEVEN:.0f}x")
    print(f"  fresh input is {tot['input']/max(tot['cache_write'],1)*100:.2f}% of "
          f"cache write: almost every token billed is context being rebuilt, "
          f"not new instruction")


def html(path, hours=24):
    rows, errors = observe.sessions(hours)
    total = sum(r["usd"] for r in rows)
    al = alerts(hours)
    def esc(t):
        return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    cards = []
    for r in rows:
        st = state_of(r)
        ru = reuse(r)
        cards.append(f"""<article class="s {st.lower()}">
<header><span class="st">{st}</span><h2>{esc(r['slug'])}</h2></header>
<p class="says">{esc((r['text'].splitlines() or [''])[0][:220])}</p>
<dl><div><dt>idle</dt><dd>{r['idle']/60:.1f}m</dd></div>
<div><dt>spend</dt><dd>${r['usd']:,.2f}</dd></div>
<div><dt>requests</dt><dd>{r['requests']}</dd></div>
<div><dt>reuse</dt><dd class="{'bad' if 0<ru<BREAKEVEN else 'ok'}">{ru:.1f}x</dd></div></dl>
</article>""")
    alist = "".join(f"<li>{esc(a)}</li>" for a in al) or "<li class='quiet'>nothing needs a person</li>"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    open(path, "w").write(f"""<title>Aiden Board</title>
<style>
:root{{--bg:#fbfaf8;--fg:#1a1917;--dim:#6b6862;--line:#e3e0da;--card:#fff;
--run:#2f7d4f;--blk:#b3261e;--wait:#8a6d1f;--ok:#2f7d4f;--bad:#b3261e}}
:root:not([data-theme="light"]) {{}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{
--bg:#141412;--fg:#eceae5;--dim:#9a968e;--line:#2c2a26;--card:#1c1b18;
--run:#6ec48f;--blk:#ef8a82;--wait:#d8b95a;--ok:#6ec48f;--bad:#ef8a82}}}}
:root[data-theme="dark"]{{--bg:#141412;--fg:#eceae5;--dim:#9a968e;--line:#2c2a26;
--card:#1c1b18;--run:#6ec48f;--blk:#ef8a82;--wait:#d8b95a;--ok:#6ec48f;--bad:#ef8a82}}
body{{background:var(--bg);color:var(--fg);font:16px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif;
margin:0;padding:24px 18px 64px;max-width:900px;margin-inline:auto}}
h1{{font-size:1.35rem;margin:0 0 2px}} .sub{{color:var(--dim);font-size:.9rem;margin:0 0 22px}}
.tot{{font-variant-numeric:tabular-nums;font-size:2rem;margin:0}}
.s{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 12px}}
.s header{{display:flex;gap:10px;align-items:baseline}}
.s h2{{font-size:.98rem;margin:0;font-weight:600;overflow-wrap:anywhere}}
.st{{font-size:.68rem;letter-spacing:.09em;font-weight:700;padding:2px 7px;border-radius:99px;
border:1px solid currentColor;white-space:nowrap}}
.running .st{{color:var(--run)}} .blocked .st{{color:var(--blk)}}
.waiting .st{{color:var(--wait)}} .done .st,.idle .st{{color:var(--dim)}}
.says{{color:var(--dim);font-size:.88rem;margin:8px 0 12px;overflow-wrap:anywhere}}
dl{{display:flex;flex-wrap:wrap;gap:18px;margin:0}} dl div{{min-width:64px}}
dt{{font-size:.68rem;letter-spacing:.06em;color:var(--dim);text-transform:uppercase}}
dd{{margin:1px 0 0;font-variant-numeric:tabular-nums;font-size:.95rem}}
.ok{{color:var(--ok)}} .bad{{color:var(--bad);font-weight:600}}
ul{{padding-left:18px}} li{{margin:4px 0;overflow-wrap:anywhere}} .quiet{{color:var(--dim);list-style:none;margin-left:-18px}}
h3{{font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:26px 0 8px}}
</style>
<h1>Aiden</h1>
<p class="sub">every session on this machine, last {hours}h &middot; {stamp} &middot; cost of producing this page: nothing</p>
<p class="tot">${total:,.2f}</p>
<h3>needs a person</h3><ul>{alist}</ul>
<h3>sessions</h3>
{''.join(cards)}
""")
    return path


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "board"
    if cmd == "board":
        board(int(sys.argv[2]) if len(sys.argv) > 2 else 24)
    elif cmd == "cost":
        cost(int(sys.argv[2]) if len(sys.argv) > 2 else 7)
    elif cmd == "alerts":
        a = alerts()
        print("\n".join(a) if a else "nothing needs a person")
    elif cmd == "html":
        print(html(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 24))
    else:
        print(__doc__)
        sys.exit(2)

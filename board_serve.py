#!/usr/bin/env python3
"""Serve the founder's board on a fixed local URL, so reading it needs no agent session.

The founder, 2026-08-21: "nagaing different agent sessionsis ehausintg" and "the sooner wecan
get fouder out of loop the btter". `com.founder.board` already rebuilds the page every hour on
its own. What it could not do was SHOW it: the page only reached him when a session happened to
be alive and published it, so the freshest board on the machine could be an hour of work behind
what he was looking at, and he had to ask.

This serves exactly one file, on 127.0.0.1 only, and it serves the file's OWN age in a banner --
a stale page that says it is stale is honest, and a page that cannot say so is a black box.

    http://127.0.0.1:8787/                 the board
    http://127.0.0.1:8787/ops              every session, its ticket, its last words
    http://127.0.0.1:8787/look             the newest design preview, before it is merged
    http://127.0.0.1:8787/alerts           every estate alert, repeats folded into one row
    http://127.0.0.1:8787/admin            the admin dashboard: freshness, and mint a link
    http://127.0.0.1:8787/audit?t=TOKEN    the full estate audit, behind a minted token
    http://127.0.0.1:8787/health           plain text, for a probe

Two files, never a directory listing: ~/.claude/state holds other sessions' state and none of it
is his to read by accident.

The founder, 2026-08-21: "this audit can never go out of sync", "enforced", and "it needs to be
minted token via admin dashboard". Three things follow from that and all three are here:

  * STALENESS IS REFUSED, NOT ANNOUNCED. A banner saying "this is 4 hours old" is a page you can
    still read and still quote. /audit past its deadline returns 409 and the audit is not served
    at all. A guard you can click past is decoration.
  * A LINK IS MINTED, NOT CONFIGURED. /admin mints a bearer token with an expiry. There is no
    standing URL to leak and no password to forget; a link that has expired says so.
  * THE TOKEN NEVER UNLOCKS THE MINTER. /admin is reachable from this machine only, and no token
    grants access to it. Minting stays a thing you do at the laptop.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOARD = os.path.expanduser("~/.claude/state/founder-board.html")
AUDIT = os.path.expanduser("~/.claude/state/estate-audit.html")
AUDIT_JSON = os.path.expanduser("~/.claude/state/estate-audit.json")
TOKENS = os.path.expanduser("~/.claude/state/audit-tokens.json")
LOOK = os.path.expanduser("~/.claude/state/design-preview.html")
ALERTS = os.path.expanduser(os.environ.get("ESTATE_ALERT_INBOX",
                            "~/.estate/alerts/inbox.jsonl"))
PORT = int(os.environ.get("FOUNDER_BOARD_PORT", "8787"))
STALE_S = 90 * 60          # the builder runs hourly; 90 minutes means a build was MISSED
AUDIT_STALE_S = int(os.environ.get("AUDIT_STALE_S", 2 * 3600))
TOKEN_TTL_S = int(os.environ.get("AUDIT_TOKEN_TTL_S", 24 * 3600))



# ------------------------------------------------------------------- /alerts
# Founder, 2026-08-29: "i cant see ny innportannt nessages", "they should be goig else where",
# "flooding ny view". Two separate faults were behind that, and this is the second one.
#
# estate_alert.send_operator_alert falls back to ~/.estate/alerts/inbox.jsonl whenever
# TELEGRAM_ALERT_CHANNEL is unset -- and it is unset. Measured 2026-08-29: 3,104 alerts had
# accumulated in that file since 08-25, the estate scanner going stale and every spend warning
# among them, and NOTHING RENDERED IT. The sender returns True for those writes, so the whole
# path reads as delivered. An instrument nobody reads is not an instrument (LAW 28).
#
# So the file gets a page, on the board he already has open. The de-duplication here is for
# display only and is not the fix for the volume -- that one is in the sender, where it stops the
# repeats being written at all. This is what makes the ones already written legible: one row per
# distinct sentence, how many times it fired, when it was last seen, loudest first.
ALERT_TAIL = 4000          # rows read from the end of the inbox; the file is append-only


def _alert_rows(path: str = "") -> list[dict]:
    """Distinct alerts, newest first, each carrying how many times its sentence fired."""
    rows = []
    try:
        with open(path or ALERTS, encoding="utf-8", errors="replace") as fh:
            for ln in fh.readlines()[-ALERT_TAIL:]:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    groups: dict[tuple, dict] = {}
    for r in rows:
        text = " ".join(str(r.get("text") or "").split())
        if not text:
            continue
        src = str(r.get("source") or "?")
        k = (src, re.sub(r"\d+", "#", text)[:400])
        ts = r.get("ts") if isinstance(r.get("ts"), (int, float)) else 0
        g = groups.setdefault(k, {"source": src, "text": text, "n": 0, "first": ts, "last": ts})
        g["n"] += 1
        g["text"] = text                      # the most recent wording of the same sentence
        g["first"] = min(g["first"] or ts, ts) if ts else g["first"]
        g["last"] = max(g["last"], ts)
    return sorted(groups.values(), key=lambda g: -g["last"])


def _alerts_page(rows: list[dict]) -> bytes:
    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def when(ts):
        return time.strftime("%a %H:%M", time.localtime(ts)) if ts else "unknown"

    total = sum(r["n"] for r in rows)
    head = (f"<h1>Estate alerts</h1><p class=s>{len(rows)} distinct alerts, "
            f"{total} deliveries. Repeats are folded into one row.</p>")
    if not rows:
        head += ("<p class=s>Nothing in the inbox. If that is a surprise, the sender is writing "
                 "somewhere else: check TELEGRAM_ALERT_CHANNEL.</p>")
    body = []
    for r in rows:
        times = f"<span class=n>&times;{r['n']}</span>" if r["n"] > 1 else ""
        body.append(f"<tr><td class=w>{when(r['last'])}</td><td class=src>{esc(r['source'])}</td>"
                    f"<td>{esc(r['text'][:400])} {times}</td></tr>")
    return (f"""<!doctype html><meta charset=utf-8><title>Estate alerts</title>
<style>body{{font:15px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;margin:2rem auto;max-width:60rem;
padding:0 1rem;color:#111;background:#fff}}h1{{font-size:1.4rem;margin:0 0 .2rem}}
p.s{{color:#666;margin:0 0 1.4rem}}table{{border-collapse:collapse;width:100%}}
td{{border-top:1px solid #e5e5e5;padding:.55rem .5rem;vertical-align:top}}
td.w{{white-space:nowrap;color:#666;width:7rem}}td.src{{color:#666;width:12rem;
font:12px ui-monospace,SFMono-Regular,Menlo,monospace}}
span.n{{background:#eee;border-radius:9px;padding:1px 7px;color:#555;font-size:12px}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}td{{border-color:#333}}
p.s,td.w,td.src{{color:#999}}span.n{{background:#333;color:#bbb}}}}</style>
{head}<table>{''.join(body)}</table>""").encode()


# ---------------------------------------------------------------- minted links

def _load_tokens() -> list[dict]:
    try:
        with open(TOKENS) as fh:
            return json.load(fh).get("tokens", [])
    except (OSError, ValueError):
        return []


def _save_tokens(rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(TOKENS), exist_ok=True)
    tmp = TOKENS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"tokens": rows}, fh, indent=1)
    os.chmod(tmp, 0o600)                  # the hashes are not secrets, the file still is not public
    os.replace(tmp, TOKENS)


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def mint(label: str = "", ttl_s: int = TOKEN_TTL_S) -> tuple[str, dict]:
    """Return (the token, its record). Only the HASH is ever stored."""
    tok = secrets.token_urlsafe(24)
    rec = {"id": secrets.token_hex(4), "hash": _hash(tok), "label": label[:60],
           "minted_at": time.time(), "expires_at": time.time() + ttl_s,
           "uses": 0, "last_used": None, "revoked": False}
    rows = _load_tokens()
    rows.append(rec)
    _save_tokens(rows)
    return tok, rec


def check_token(tok: str) -> tuple[bool, str]:
    """(ok, reason). A bad token never says WHICH way it was bad beyond expiry."""
    if not tok:
        return False, "no token"
    h = _hash(tok)
    rows = _load_tokens()
    for r in rows:
        if secrets.compare_digest(r.get("hash", ""), h):
            if r.get("revoked"):
                return False, "this link was revoked"
            if time.time() > r.get("expires_at", 0):
                return False, "this link has expired"
            r["uses"] = r.get("uses", 0) + 1
            r["last_used"] = time.time()
            _save_tokens(rows)
            return True, "ok"
    return False, "not a valid link"


def _audit_age() -> float | None:
    try:
        return time.time() - os.stat(AUDIT).st_mtime
    except OSError:
        return None


def _audit_counts() -> dict:
    try:
        with open(AUDIT_JSON) as fh:
            return json.load(fh).get("counts", {})
    except (OSError, ValueError):
        return {}


def _banner(age_s: float) -> bytes:
    mins = int(age_s / 60)
    stale = age_s > STALE_S
    bg, fg = ("#7f1d1d", "#fff") if stale else ("#14532d", "#fff")
    msg = (f"board is {mins} minutes old — the hourly builder has missed a run"
           if stale else f"measured {mins} minutes ago")
    return (f'<div style="font:14px/1.5 -apple-system,sans-serif;background:{bg};color:{fg};'
            f'padding:8px 16px">{msg} · rebuild: '
            f'<code>launchctl kickstart -k gui/$(id -u)/com.founder.board</code></div>'
            ).encode()


ADMIN_CSS = """
body{margin:0;background:#0E1116;color:#E7EBF1;font:15px/1.55 -apple-system,BlinkMacSystemFont,
"Segoe UI",sans-serif}
.w{max-width:760px;margin:0 auto;padding:40px 22px 70px}
h1{font-size:30px;margin:0 0 6px;letter-spacing:-.02em}
.sub{color:#7F8B9C;margin:0 0 26px;font-size:14px}
h2{font-size:13px;letter-spacing:.11em;text-transform:uppercase;color:#7F8B9C;margin:34px 0 10px;
border-top:1px solid #252C36;padding-top:18px}
.card{background:#161B22;border:1px solid #252C36;border-radius:4px;padding:16px 18px;margin:0 0 12px;
border-left:3px solid #252C36}
.card.crit{border-left-color:#F1878F}.card.ok{border-left-color:#68C79B}
.n{font-family:ui-monospace,Menlo,monospace;font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
.crit .n{color:#F1878F}.ok .n{color:#68C79B}
.l{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:#7F8B9C;font-weight:700}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
button,input{font:inherit}
input[type=text]{background:#0E1116;border:1px solid #252C36;color:#E7EBF1;padding:9px 11px;
border-radius:3px;width:230px}
button{background:#E0808C;color:#0E1116;border:0;padding:9px 17px;border-radius:3px;font-weight:700;
cursor:pointer}
button.ghost{background:transparent;color:#7F8B9C;border:1px solid #252C36;font-weight:500;padding:5px 11px}
a{color:#E0808C}
code{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;background:#0E1116;padding:2px 6px;
border-radius:2px;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:#7F8B9C;
padding:0 9px 6px;border-bottom:1px solid #252C36}
td{padding:9px;border-bottom:1px solid #1C222B;vertical-align:middle}
.minted{background:#12271F;border:1px solid #1F6146;border-radius:4px;padding:15px 17px;margin:14px 0}
.minted .u{display:block;margin-top:8px}
"""


def _admin_page(minted: str = "", note: str = "") -> bytes:
    e = html.escape
    age = _audit_age()
    counts = _audit_counts()
    if age is None:
        state, cls, msg = "MISSING", "crit", f"no audit on disk at {AUDIT}"
    elif age > AUDIT_STALE_S:
        state, cls, msg = "STALE", "crit", (
            f"{int(age / 60)} minutes old, deadline is {AUDIT_STALE_S // 60}. "
            "/audit is REFUSING to serve it. Rebuild before minting a link.")
    else:
        state, cls, msg = "FRESH", "ok", f"measured {int(age / 60)} minutes ago"
    rows = _load_tokens()
    live = [r for r in rows if not r.get("revoked") and time.time() < r.get("expires_at", 0)]
    body = [f"<style>{ADMIN_CSS}</style><div class='w'>",
            "<h1>Audit admin</h1>",
            "<p class='sub'>Mint a link to the estate audit, see who used it, and revoke it. "
            "This page is reachable from this machine only and no token opens it.</p>"]
    if note:
        body.append(f"<div class='card crit'>{e(note)}</div>")
    if minted:
        body.append(
            "<div class='minted'><b>Link minted. This is the only time it is shown.</b>"
            f"<code class='u'>{e(minted)}</code></div>")
    body.append(f"<h2>The audit itself</h2><div class='card {cls}'>"
                f"<div class='n'>{state}</div><div class='l'>{e(msg)}</div></div>")
    if counts:
        body.append("<div class='grid'>" + "".join(
            f"<div class='card {'crit' if k == 'critical' else ''}'><div class='n'>{v}</div>"
            f"<div class='l'>{e(k)}</div></div>"
            for k, v in counts.items()) + "</div>")
    body.append("<div class='card'><div class='l'>rebuild now</div>"
                "<code>python3 $HOME/Documents/code/claude-guards/estate/estate_audit.py --html --state</code></div>")
    body.append("<h2>Mint a link</h2><form method='POST' action='/admin/mint'>"
                "<input type='text' name='label' placeholder='who is it for?' maxlength='60'> "
                f"<button type='submit'>Mint a {TOKEN_TTL_S // 3600}-hour link</button></form>")
    body.append(f"<h2>Live links &middot; {len(live)}</h2>")
    if live:
        body.append("<table><tr><th>Label</th><th>Expires in</th><th>Uses</th><th></th></tr>")
        for r in sorted(live, key=lambda x: x["expires_at"]):
            left = int((r["expires_at"] - time.time()) / 60)
            left_s = f"{left // 60}h {left % 60}m" if left >= 60 else f"{left}m"
            body.append(
                f"<tr><td>{e(r.get('label') or '(unlabelled)')}</td><td>{left_s}</td>"
                f"<td>{r.get('uses', 0)}</td>"
                f"<td><form method='POST' action='/admin/revoke'>"
                f"<input type='hidden' name='id' value='{e(r['id'])}'>"
                f"<button class='ghost' type='submit'>revoke</button></form></td></tr>")
        body.append("</table>")
    else:
        body.append("<p class='sub'>None. A link that does not exist cannot leak.</p>")
    body.append("</div>")
    return "".join(body).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # one line per request in launchd's log is noise
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local(self) -> bool:
        """The minter is a thing you do AT the laptop. No token substitutes for that."""
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _serve_audit(self, qs: dict) -> None:
        """Enforced, not announced: past the deadline the audit is REFUSED, not banner-ed."""
        ok, why = check_token((qs.get("t") or [""])[0])
        if not ok:
            self._send(403, f"{why}. Mint a fresh link at http://127.0.0.1:{PORT}/admin\n".encode(),
                       "text/plain")
            return
        age = _audit_age()
        if age is None:
            self._send(503, (f"no audit on disk at {AUDIT}. Build it with "
                             "estate_audit.py --html --state\n").encode(), "text/plain")
            return
        if age > AUDIT_STALE_S:
            self._send(409, (f"REFUSED. This audit is {int(age / 60)} minutes old and the deadline "
                             f"is {AUDIT_STALE_S // 60}. A stale audit is not served, because an "
                             f"audit you can read past its deadline is not enforced.\n"
                             f"Rebuild: python3 $HOME/Documents/code/claude-guards/estate/estate_audit.py --html --state\n"
                             ).encode(), "text/plain")
            return
        try:
            with open(AUDIT, "rb") as fh:
                page = fh.read()
        except OSError as e:
            self._send(503, f"audit unreadable: {e}".encode(), "text/plain")
            return
        self._send(200, page, "text/html; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/audit":
            self._serve_audit(qs)
            return
        if path == "/admin":
            if not self._local():
                self._send(403, b"the admin dashboard is local-only\n", "text/plain")
                return
            self._send(200, _admin_page(), "text/html; charset=utf-8")
            return
        if path == "/alerts":
            self._send(200, _alerts_page(_alert_rows()), "text/html; charset=utf-8")
            return
        if path in ("/ops", "/opsdashboard"):
            #: Every agent session and what it is doing right now, computed on this request from
            #: the hook ledger every session already writes (session_live.py). It used to be a
            #: file produced by `ticket-gate.py --dashboard`, which walked 81k transcripts in
            #: 4m32s (measured 2026-09-07) on a five-minute tick; when that tick was deleted the
            #: page froze for eleven days and said so in a banner nobody was reading. A page
            #: computed at read time has no producer to lose (founder 2026-09-07: "you need to
            #: fully audit their transaction in real time as they are working").
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import session_live
            rows = session_live.sessions(session_live.read_rows())
            self._send(200, session_live.render(rows), "text/html; charset=utf-8")
            return
        if path == "/look":
            #: The newest look at a change to a page, written by whichever session built it, so a
            #: design decision reaches him without a vendor page and without a session being alive
            #: (LAW 34: no provider single point of failure; LAW 39: this board already existed).
            try:
                with open(LOOK, "rb") as fh:
                    body = fh.read()
            except OSError as e:
                self._send(503, f"no design preview on disk at {LOOK}: {e}".encode(), "text/plain")
                return
            look_age = time.time() - os.stat(LOOK).st_mtime
            hours = look_age / 3600
            banner = (f'<div style="font:14px/1.5 -apple-system,sans-serif;'
                      f'background:{"#14532d" if hours < 24 else "#9a3412"};'
                      f'color:#fff;padding:8px 16px">this look was drawn '
                      f'{int(look_age // 60)} minutes ago</div>').encode()
            self._send(200, banner + body, "text/html; charset=utf-8")
            return
        try:
            age = time.time() - os.stat(BOARD).st_mtime
        except OSError as e:
            # A missing board is a FINDING, not a 404 with no explanation.
            self._send(503, f"no board on disk at {BOARD}: {e}".encode(), "text/plain")
            return
        if path == "/health":
            aa = _audit_age()
            self._send(200, (f"ok age_s={int(age)} stale={age > STALE_S} "
                             f"audit_age_s={int(aa) if aa is not None else -1} "
                             f"audit_stale={aa is None or aa > AUDIT_STALE_S}\n").encode(),
                       "text/plain")
            return
        if path not in ("/", "/index.html", "/founder-board.html"):
            self._send(404, b"this server serves: / (board), /ops, /look, /alerts, /admin, /audit?t=TOKEN, /health\n",
                       "text/plain")
            return
        try:
            with open(BOARD, "rb") as fh:
                page = fh.read()
        except OSError as e:
            self._send(503, f"board unreadable: {e}".encode(), "text/plain")
            return
        self._send(200, _banner(age) + page, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if not self._local():
            self._send(403, b"the admin dashboard is local-only\n", "text/plain")
            return
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode() if n else "")
        if path == "/admin/mint":
            age = _audit_age()
            if age is None or age > AUDIT_STALE_S:
                self._send(200, _admin_page(note="Refused to mint: the audit is stale or missing. "
                                                 "A link to a stale audit is worse than no link."),
                           "text/html; charset=utf-8")
                return
            tok, _ = mint((form.get("label") or [""])[0])
            url = f"http://127.0.0.1:{PORT}/audit?t={tok}"
            self._send(200, _admin_page(minted=url), "text/html; charset=utf-8")
            return
        if path == "/admin/revoke":
            tid = (form.get("id") or [""])[0]
            rows = _load_tokens()
            hit = False
            for r in rows:
                if r.get("id") == tid:
                    r["revoked"] = True
                    hit = True
            _save_tokens(rows)
            self._send(200, _admin_page(note="" if hit else f"no live link with id {tid}"),
                       "text/html; charset=utf-8")
            return
        self._send(404, b"no such endpoint\n", "text/plain")


def selftest() -> int:
    """Prove the refusals, not the happy path. A guard is only worth what it REFUSES."""
    import tempfile
    import threading
    import urllib.error
    import urllib.request

    g = globals()
    keep = {k: g[k] for k in ("BOARD", "AUDIT", "AUDIT_JSON", "TOKENS", "AUDIT_STALE_S")}
    td = tempfile.mkdtemp(prefix="board-selftest-")
    g["BOARD"] = os.path.join(td, "founder-board.html")
    g["AUDIT"] = os.path.join(td, "estate-audit.html")
    g["AUDIT_JSON"] = os.path.join(td, "estate-audit.json")
    g["TOKENS"] = os.path.join(td, "audit-tokens.json")
    g["AUDIT_STALE_S"] = 3600
    open(g["BOARD"], "w").write("<p>board</p>")
    open(g["AUDIT"], "w").write("<p>THE-AUDIT-BODY</p>")
    open(g["AUDIT_JSON"], "w").write('{"counts":{"critical":3}}')

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    fails = []

    def get(path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")
        except Exception as e:                              # noqa: BLE001
            return -1, f"{type(e).__name__}: {e}"

    def post(path: str, data: str) -> tuple[int, str]:
        try:
            req = urllib.request.Request(base + path, data=data.encode(), method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")
        except Exception as e:                              # noqa: BLE001
            return -1, f"{type(e).__name__}: {e}"

    def want(label: str, got, exp) -> None:
        if got != exp:
            fails.append(f"{label}: got {got!r}, wanted {exp!r}")

    # the board still works, and nothing outside its one file is reachable
    want("board /", get("/")[0], 200)
    want("path escape", get("/../../.env")[0], 404)
    want("health", 200 if "audit_age_s=" in get("/health")[1] else 0, 200)

    # NO TOKEN, BAD TOKEN -- the audit is not served
    want("/audit with no token", get("/audit")[0], 403)
    want("/audit with a junk token", get("/audit?t=nonsense")[0], 403)

    # /admin is reachable locally and mints
    code, page = get("/admin")
    want("/admin renders", code, 200)
    want("/admin shows FRESH", "FRESH" in page, True)
    code, page = post("/admin/mint", "label=auditor")
    want("mint returns the page", code, 200)
    m = re.search(r"/audit\?t=([A-Za-z0-9_-]+)", page)
    if not m:
        fails.append("mint did not show a URL")
        tok = ""
    else:
        tok = m.group(1)

    if tok:
        code, body = get(f"/audit?t={tok}")
        want("minted link serves the audit", code, 200)
        want("and it is the real audit body", "THE-AUDIT-BODY" in body, True)
        # the raw token must NEVER be on disk
        want("token stored as plaintext", tok in open(g["TOKENS"]).read(), False)

        # REVOKE -- the same link must stop working
        rows = _load_tokens()
        post("/admin/revoke", f"id={rows[-1]['id']}")
        want("revoked link is refused", get(f"/audit?t={tok}")[0], 403)

    # EXPIRY -- a token past its expiry is refused
    tok2, rec2 = mint("expiring", ttl_s=1)
    rows = _load_tokens()
    for r in rows:
        if r["id"] == rec2["id"]:
            r["expires_at"] = time.time() - 1
    _save_tokens(rows)
    want("expired link is refused", get(f"/audit?t={tok2}")[0], 403)

    # STALENESS IS REFUSED, NOT ANNOUNCED -- this is the founder's "enforced"
    tok3, _ = mint("staleness")
    old = time.time() - 4 * 3600
    os.utime(g["AUDIT"], (old, old))
    code, body = get(f"/audit?t={tok3}")
    want("stale audit is REFUSED with 409", code, 409)
    want("and the body is not served", "THE-AUDIT-BODY" in body, False)
    want("and it says why", "REFUSED" in body, True)
    code, page = post("/admin/mint", "label=while-stale")
    want("minting is refused while stale", "Refused to mint" in page, True)
    want("/admin says STALE", "STALE" in get("/admin")[1], True)

    srv.shutdown()
    for k, v in keep.items():
        g[k] = v
    if fails:
        print("selftest FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("PASS: board serves; no token / junk / revoked / expired all refused with 403; "
          "a stale audit is refused with 409 and its body never sent; minting refuses while "
          "stale; the raw token is never written to disk.")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"founder board on http://127.0.0.1:{PORT}/ from {BOARD}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

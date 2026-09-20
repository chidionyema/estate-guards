#!/usr/bin/env python3
"""Kimi browser bridge. Drives a signed-in www.kimi.com session over Playwright.

    kimi_bridge.py --login     sign in once, headed, and keep the profile
    kimi_bridge.py --daemon    serve 127.0.0.1:8766
    kimi_bridge.py --health    print the daemon's health and exit
    kimi_bridge.py "question"  ask through a running daemon

Why it is shaped like this.

The browser lives in ONE worker thread and nothing else touches it. Playwright's
sync objects belong to the thread that made them, and a second thread reaching
in is the classic way this kind of bridge dies with an unreadable greenlet
error. Requests arrive on the HTTP thread, go onto a queue, and come back on a
per-request event.

Completion is detected from the network first and the DOM second. The network
answer is the response the page itself received, so it survives a redesign; the
DOM answer only survives until someone renames a class. Both are implemented
because the first one silently stops matching when an endpoint moves, and a
bridge that returns an empty string is worse than one that says it failed.

The profile is a dedicated Chromium user-data-dir. It is not the founder's
Chrome and it never reads his cookies.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import atexit
import signal
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

def _cfg(generic, legacy, default):
    """A config knob with a provider-neutral name and a legacy KIMI_* fallback.

    One engine drives more than one provider. A second instance is this same
    script with BRIDGE_HOME / BRIDGE_PORT / BRIDGE_URL pointed elsewhere -- see
    the ai.estate.deepseek-bridge launchd job. The KIMI_* names still work so
    the original Kimi service needs no change.
    """
    return os.environ.get(generic) or os.environ.get(legacy) or str(default)


ROOT = Path(_cfg("BRIDGE_HOME", "KIMI_BRIDGE_HOME", Path.home() / ".kimi-bridge"))
# One script, two jobs. The reply used to say "kimi-bridge" whichever job sent
# it, so a deepseek answer arrived labelled as a kimi answer. The home
# directory already names the job, so read it from there.
NAME = ROOT.name.lstrip(".") or "bridge"
PROFILE = ROOT / "profile"
DB = ROOT / "session.db"
LOG = ROOT / "logs" / "bridge.jsonl"
PIDFILE = ROOT / "bridge.pid"
HOST, PORT = "127.0.0.1", int(_cfg("BRIDGE_PORT", "KIMI_BRIDGE_PORT", "8766"))
# www.kimi.com is the mainland-China surface. Outside it the site says so
# and refuses the sign-in: "If you're outside mainland China, sign in at
# Kimi.ai with your Google account or a non-+86 phone number". kimi.ai 302s
# to www.kimi.ai, which is where a UK session can actually authenticate.
# Measured 2026-08-22: kimi.ai 302, www.kimi.ai 200, www.kimi.com 200.
TARGET = _cfg("BRIDGE_URL", "KIMI_BRIDGE_URL", "https://www.kimi.ai/")
HEALTH_EVERY = 60
RETRY_BACKOFF = (2, 4, 8)
ANSWER_TIMEOUT = int(_cfg("BRIDGE_TIMEOUT", "KIMI_BRIDGE_TIMEOUT", "300"))
# The browser is the whole cost. A Chromium renderer left open on a chat page
# grows without bound: measured 2026-08-22, one idle tab held 554 MB across four
# processes, the renderer alone 377 MB and climbing. The session lives in the
# profile on disk, not in the running browser, so the browser only has to exist
# while a query is in flight. Park it this many seconds after the last query and
# the idle footprint is zero; the next query wakes it in a few seconds.
IDLE_CLOSE = int(_cfg("BRIDGE_IDLE_SEC", "KIMI_BRIDGE_IDLE_SEC", "180"))
# A daemon holds one conversation open across queries. On DeepSeek the network
# stream for a reused conversation replays older turns, so the capture returns
# the PREVIOUS answer -- measured 2026-08-22: "pineapple" and "helicopter" both
# came back as the prior turn's "DRY...". A fresh chat per query means exactly
# one turn is in flight, so the first (and only) response object is the right
# one. Off for Kimi (unchanged), on for DeepSeek via BRIDGE_FRESH_CHAT=true.
FRESH_CHAT = _cfg("BRIDGE_FRESH_CHAT", "KIMI_BRIDGE_FRESH_CHAT", "false") \
    .strip().lower() in ("1", "true", "yes", "on")
# JS run before every page load (in addition to STEALTH). DeepSeek stores its
# web-search toggle in localStorage as searchEnabled; with it on, DeepSeek
# searched the prompt's PREAMBLE ("lead with the answer", "parse the reply")
# instead of answering the question -- measured 2026-08-22 in the raw stream
# (search_enabled:true, off-topic web summaries). Setting it false here makes
# the toggle default off on every load, reload-proof. Empty for Kimi.
INIT_JS = _cfg("BRIDGE_INIT_JS", "KIMI_BRIDGE_INIT_JS", "")
# Belt-and-suspenders for the same problem: after landing, if a "Smart Search"
# toggle is still shown selected, click it off. The localStorage default above
# should already have done it; this catches a UI that ignores the stored pref.
SEARCH_OFF = _cfg("BRIDGE_SEARCH_OFF", "KIMI_BRIDGE_SEARCH_OFF", "false") \
    .strip().lower() in ("1", "true", "yes", "on")
# Which network responses hold the answer. The default is broad for Kimi; a
# conversation-history GET also matches it, and capturing THAT returned the
# previous turn's answer (measured 2026-08-22: "pineapple" -> the prior "DRY"
# reply). DeepSeek pins this to its streaming endpoint so only the live answer
# is read, which removes the need to reload a fresh chat every query.
CAPTURE_URL = _cfg("BRIDGE_CAPTURE_URL", "KIMI_BRIDGE_CAPTURE_URL",
                   r"(kimi\.ai|deepseek\.com)/api")

# Limits. Every one of these was an unbounded number that an edge case test
# found on 2026-08-22, and an agent fleet is exactly the caller that finds
# them: it retries in a loop, it sends whatever its own bug produced, and it
# does not read the reply before sending the next one.
MAX_BODY = int(_cfg("BRIDGE_MAX_BODY", "KIMI_BRIDGE_MAX_BODY", "1000000"))
MAX_PROMPT = int(_cfg("BRIDGE_MAX_PROMPT", "KIMI_BRIDGE_MAX_PROMPT", "200000"))
MIN_TIMEOUT, MAX_TIMEOUT = 5, int(_cfg("BRIDGE_MAX_TIMEOUT",
                                       "KIMI_BRIDGE_MAX_TIMEOUT", "600"))
# One browser means one job at a time. A caller that never stops asking must
# be told the bridge is busy, not silently queued behind a queue with no end.
QUEUE_MAX = int(_cfg("BRIDGE_QUEUE_MAX", "KIMI_BRIDGE_QUEUE_MAX", "16"))
# How long after the caller's own deadline the handler still waits, so a
# worker finishing at the last moment is not thrown away.
GRACE = 5
# Socket read timeout. Without it a Content-Length larger than the body sent
# blocks rfile.read for ever and leaks one thread per such request.
READ_TIMEOUT = int(_cfg("BRIDGE_READ_TIMEOUT", "KIMI_BRIDGE_READ_TIMEOUT", "10"))


def _markers(generic, legacy, default):
    """Comma list of lowercase phrases, provider-neutral name with KIMI_*
    fallback. An explicit empty string means "no phrases" -- so it is read with
    `is None` rather than `or`, which would swallow the empty override."""
    raw = os.environ.get(generic)
    if raw is None:
        raw = os.environ.get(legacy)
    if raw is None:
        raw = default
    return [m.strip().lower() for m in raw.split(",") if m.strip()]


# Phrases that only the SIGNED-OUT page carries. Kimi's signed-out page keeps a
# composer, so a text veto is the only discriminator. DeepSeek is the opposite:
# its signed-out page redirects to a login form with no composer, and its
# signed-IN page still contains the words "sign in", so the veto must be off
# (BRIDGE_SIGNIN_MARKERS="") and a positive marker used instead.
SIGNIN_MARKERS = _markers("BRIDGE_SIGNIN_MARKERS", "KIMI_BRIDGE_SIGNIN_MARKERS",
                          "log in,sign in,登录")
# Phrases that only the SIGNED-IN page carries (optional). Empty for Kimi.
READY_MARKERS = _markers("BRIDGE_READY_MARKERS", "KIMI_BRIDGE_READY_MARKERS", "")

# Anything that looks like a credential is replaced before a line is written.
# The bridge logs URLs and lengths, never bodies of auth traffic.
_SECRET = re.compile(
    r"(?i)(authorization|cookie|set-cookie|token|api[-_]?key|refresh|bearer)"
    r"\s*[:=]\s*\S+")


def log(event, **fields):
    rec = {"ts": round(time.time(), 3), "event": event}
    for k, v in fields.items():
        rec[k] = _SECRET.sub(r"\1=<redacted>", v) if isinstance(v, str) else v
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


# --------------------------------------------------------------- persistence

def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS state "
                "(key TEXT PRIMARY KEY, value TEXT, updated REAL)")
    con.commit()
    DB.chmod(0o600)
    return con


def put(con, key, value):
    con.execute("INSERT INTO state(key,value,updated) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated=excluded.updated", (key, value, time.time()))
    con.commit()


def get(con, key):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------- stealth

# Chromium started by an automation driver differs from a hand-driven one in a
# handful of readable ways. Each patch below closes one of them. They run before
# any page script, so the page never observes the un-patched value.
STEALTH = r"""
(() => {
  const def = (o, k, v) => {
    try { Object.defineProperty(o, k, { get: () => v, configurable: true }); }
    catch (e) {}
  };

  // The single most-read automation flag.
  def(Navigator.prototype, 'webdriver', undefined);
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}

  // Headless Chromium reports zero plugins and no mime types.
  const mk = (name, filename, desc) => {
    const p = Object.create(Plugin.prototype);
    def(p, 'name', name); def(p, 'filename', filename);
    def(p, 'description', desc); def(p, 'length', 1);
    return p;
  };
  const plugins = [
    mk('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
  ];
  Object.setPrototypeOf(plugins, PluginArray.prototype);
  def(Navigator.prototype, 'plugins', plugins);
  def(Navigator.prototype, 'languages', ['en-GB', 'en']);

  // window.chrome is absent under automation and present in a real browser.
  if (!window.chrome) {
    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {},
                      app: { isInstalled: false } };
  }

  // Notification.permission reads 'denied' headless while the query API says
  // 'prompt'. The disagreement is itself the tell, so make them agree.
  const q = window.navigator.permissions && window.navigator.permissions.query;
  if (q) {
    window.navigator.permissions.query = (p) =>
      p && p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : q.call(window.navigator.permissions, p);
  }

  // Headless returns a software renderer here; a real Mac returns Apple GPU.
  const gp = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Apple Inc.';
    if (p === 37446) return 'Apple M-series GPU';
    return gp.apply(this, arguments);
  };

  def(Navigator.prototype, 'hardwareConcurrency', 10);
  def(Navigator.prototype, 'deviceMemory', 8);
  def(Navigator.prototype, 'maxTouchPoints', 0);

  // toString on a patched native function must still look native.
  const ts = Function.prototype.toString;
  const patched = new WeakSet([
    WebGLRenderingContext.prototype.getParameter,
    window.navigator.permissions ? window.navigator.permissions.query : () => {},
  ]);
  Function.prototype.toString = function () {
    if (patched.has(this)) return 'function () { [native code] }';
    return ts.call(this);
  };
})();
"""

# Watches the page for an assistant reply that has stopped growing. Used only
# when the network capture found nothing, so a UI change degrades the bridge
# rather than breaking it.
SETTLE = r"""
({quietMs, capMs}) => new Promise((resolve) => {
  const started = Date.now();
  let last = '';
  let lastChange = Date.now();
  const read = () => {
    const nodes = document.querySelectorAll(
      '[class*="assistant"],[class*="answer"],[data-role="assistant"],' +
      '[class*="markdown"],[class*="message"]');
    if (!nodes.length) return '';
    return (nodes[nodes.length - 1].innerText || '').trim();
  };
  const tick = () => {
    const now = read();
    if (now !== last) { last = now; lastChange = Date.now(); }
    if (last && Date.now() - lastChange > quietMs) return resolve(last);
    if (Date.now() - started > capMs) return resolve(last);
    setTimeout(tick, 250);
  };
  const mo = new MutationObserver(() => {});
  mo.observe(document.body, { childList: true, subtree: true, characterData: true });
  tick();
})
"""


def sse_text(raw: str) -> str:
    """Pull the assistant's words out of a server-sent-event stream.

    Every provider names the delta field differently and renames it on their own
    schedule, so this walks whatever JSON arrives and takes the string fields
    that carry text rather than matching one shape.
    """
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    if isinstance(v, str) and k in ("text", "content", "delta"):
                        out.append(v)
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)
    return "".join(out).strip()


def connect_frames(raw: bytes):
    """Walk a Connect protocol streaming body: one flag byte, four length bytes
    big-endian, then that many bytes of payload, repeating to the end.

    Kimi's web app speaks Connect, not server-sent events. Measured 2026-08-22
    against a recorded stream in fixtures/kimi-connect-stream.bin: 59 frames,
    the first being b'\x00\x00\x00\x00\x10{"heartbeat":{}}'. A truncated tail
    is ignored rather than raised on, because a stream can be cut off mid-frame
    and the words already read are still worth returning.
    """
    i = 0
    while i + 5 <= len(raw):
        flags = raw[i]
        ln = int.from_bytes(raw[i + 1:i + 5], "big")
        payload = raw[i + 5:i + 5 + ln]
        if len(payload) < ln:
            return
        i += 5 + ln
        yield flags, payload


def connect_stream_text(raw: bytes) -> str:
    """The assistant's words out of a Connect stream, and nothing else.

    Three things in that stream are not the answer and all three have burned
    somebody before:

    block.think.content is the model reasoning aloud. In the recorded stream it
    is 35 of the 59 frames, so a parser that takes every content field returns
    mostly deliberation. The web app renders it in a collapsed "Think" panel.

    A notification frame carries "High demand. Switched to K2.6 Instant for
    speed." It sits inside the answer area on screen, so any reader working off
    the page glues it onto every reply.

    Stage, status and heartbeat frames carry no words at all.

    Only block.text.content is the answer. "set" replaces a block, "append"
    extends it, and blocks are joined in the order they first appear.
    """
    blocks: dict = {}
    order: list = []
    for flags, payload in connect_frames(raw):
        if flags & 0x02:            # end-of-stream trailers, never content
            continue
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        mask = ev.get("mask") or ""
        if not mask.startswith("block.text"):
            continue
        blk = ev.get("block") or {}
        bid = blk.get("id")
        content = (blk.get("text") or {}).get("content") or ""
        if bid is None or not content:
            continue
        if bid not in blocks:
            blocks[bid] = []
            order.append(bid)
        if ev.get("op") == "set":
            blocks[bid] = [content]
        else:
            blocks[bid].append(content)
    return "".join("".join(blocks[b]) for b in order).strip()


def deepseek_stream_text(raw: str) -> str:
    """DeepSeek's web SSE is a patch stream, not the OpenAI choices/delta shape.

    The assistant text seeds in the first response object's RESPONSE fragment
    and then arrives as APPEND ops on response/fragments/*/content; bare
    {"v": "..."} lines continue the last append path. The trailing
    {"content": ...} is the chat title, not the answer -- feeding the generic
    walker that title (and stray date fields) is what returned garbage like
    "Saturday2026-08-22 Saturday" before this existed.
    """
    parts, appending = [], False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        v = obj.get("v")
        if "p" not in obj and isinstance(v, dict) and "response" in v:
            for fr in v["response"].get("fragments", []) or []:
                if fr.get("type") == "RESPONSE" and isinstance(fr.get("content"), str):
                    parts.append(fr["content"])
            appending = True
            continue
        if "p" in obj:
            path = obj["p"]
            if isinstance(path, str) and path.endswith("/content"):
                appending = True
                if isinstance(v, str):
                    parts.append(v)
            else:
                appending = False
            continue
        if set(obj.keys()) == {"v"} and isinstance(v, str):
            if appending:
                parts.append(v)
            continue
    return "".join(parts).strip()


# -------------------------------------------------------------------- worker

class Bridge:
    """Owns the browser. Only the worker thread calls anything on it."""

    def __init__(self, headed=None):
        # KIMI_BRIDGE_HEADLESS=false puts the daemon's own browser on screen, so
        # the founder can watch it work and know the answers are real. Headless
        # stays the default: a window that appears by itself while he is doing
        # something else is a cost paid on every consult, not once.
        if headed is None:
            headed = _cfg("BRIDGE_HEADLESS", "KIMI_BRIDGE_HEADLESS", "true").strip().lower() \
                in ("0", "false", "no", "off")
        self.headed = headed
        self.jobs: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self.con = db()
        self.page = None
        self.ctx = None
        self.pw = None
        self.state = "starting"
        self.detail = ""
        self.last_ok = 0.0
        self.last_used = 0.0
        self.captured: list = []

    # -- browser lifecycle -------------------------------------------------

    def start(self):
        from playwright.sync_api import sync_playwright
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=not self.headed,
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0.0.0 Safari/537.36"),
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check",
                  "--disable-features=IsolateOrigins,site-per-process"],
        )
        self.ctx.add_init_script(STEALTH)
        if INIT_JS:
            try:
                self.ctx.add_init_script(INIT_JS)
            except Exception:
                pass
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.on("response", self._on_response)
        self.page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        if self.headed:
            # Playwright's Chromium opens behind whatever has focus on macOS, so
            # the founder is told a browser is open and cannot see one. Raise it.
            try:
                self.page.bring_to_front()
                # Playwright ships the browser as "Google Chrome for Testing",
                # not "Chromium", so activating by the obvious name is a no-op
                # that fails silently. Measured 2026-08-22 against the visible
                # process list.
                for app in ("Google Chrome for Testing", "Chromium"):
                    r = subprocess.run(
                        ["osascript", "-e", f'tell application "{app}" to activate'],
                        capture_output=True, timeout=10)
                    if r.returncode == 0:
                        break
            except Exception:
                try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 508))
                except Exception: pass
        put(self.con, "last_start", str(time.time()))
        self.refresh_health()
        log("started", state=self.state, detail=self.detail)

    def _on_response(self, resp):
        try:
            url = resp.url
            if not re.search(CAPTURE_URL, url, re.I):
                return
            self.captured.append(resp)
        except Exception:
            pass

    def stop(self):
        for closer in (getattr(self.ctx, "close", None), getattr(self.pw, "stop", None)):
            try:
                closer and closer()
            except Exception:
                pass
        self.ctx = self.page = self.pw = None

    def restart(self):
        log("restart", reason=self.detail)
        self.stop()
        self.start()

    def _ensure(self):
        """Bring the browser up if it has been parked. Cheap when already up."""
        if self.page is None:
            self.start()

    def park(self):
        """Close the browser to free its memory; the session stays on disk.

        Reported as healthy, not down: a query wakes it and the on-disk session
        signs it straight back in. The real proof the session is still good is a
        query, not this idle state, so health here is last-known-good and says
        so. Only park from healthy, or a signed-out browser would be papered
        over as healthy and never re-checked.
        """
        if self.page is None:
            return
        self.stop()
        self.state = "healthy"
        self.detail = "parked to save memory; wakes on the next query"
        log("parked", idle_after=IDLE_CLOSE)

    # -- health ------------------------------------------------------------

    def _hydrated(self, ms: int = 15000) -> bool:
        """Wait until the app has painted, before reading anything off it.

        Judging the page mid-hydration is what produced a green health check
        and a failing ask 20ms later: the empty shell has no "Log in" text yet,
        so the auth test below fell through to the composer and said yes.
        Measured 2026-08-22 in bridge.jsonl, four times in a row.
        """
        try:
            self.page.wait_for_selector(
                "div[contenteditable='true'], textarea, [role='textbox']", timeout=ms)
        except Exception:
            return False
        prev = -1
        for _ in range(20):
            try:
                cur = len(self.page.inner_text("body") or "")
            except Exception:
                return False
            if cur and cur == prev:
                return True
            prev = cur
            self.page.wait_for_timeout(250)
        return prev > 0

    def signed_in(self) -> bool:
        """Signed out is a Log in control on the page, not a missing composer.

        Measured on the signed-out page: the composer is right there, captioned
        "Ask anything, or task an agent...", so its presence says nothing about
        auth. What only the signed-out page carries is "Log in to sync chat
        history" beside a "Log in" button.
        """
        if not self._hydrated():
            return False
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            return False
        if SIGNIN_MARKERS and any(w in body for w in SIGNIN_MARKERS):
            return False
        if not self._composer():
            return False
        if READY_MARKERS and not any(w in body for w in READY_MARKERS):
            return False
        return True

    def refresh_health(self):
        try:
            if self.page is None:
                self.state, self.detail = "down", "no page"
            elif self.signed_in():
                self.state, self.detail = "healthy", "signed in, composer present"
                self.last_ok = time.time()
                put(self.con, "last_healthy", str(self.last_ok))
            else:
                self.state, self.detail = "signed-out", (
                    "the page is showing a Log in control; run --login once")
        except Exception as e:
            self.state, self.detail = "down", f"{type(e).__name__}: {e}"
        return self.state

    # -- asking ------------------------------------------------------------

    def _composer(self):
        for sel in ("div[contenteditable='true']", "textarea",
                    "[role='textbox']", "input[type='text']"):
            try:
                el = self.page.query_selector(sel)
                if el and el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def _disable_search(self):
        """Turn DeepSeek's Smart Search toggle off if it is on. No-op elsewhere."""
        try:
            state = self.page.evaluate("""() => {
                const sp=[...document.querySelectorAll('span')]
                    .find(e=>/smart search/i.test((e.innerText||'').trim()));
                if(!sp) return 'no-toggle';
                let a=sp;
                for(let i=0;i<4 && a;i++){
                    if((a.className||'').toString().includes('ds-toggle-button')) break;
                    a=a.parentElement;
                }
                if(!a) return 'no-btn';
                if((a.className||'').toString().includes('ds-toggle-button--selected')){
                    a.click(); return 'clicked-off';
                }
                return 'already-off';
            }""")
            log("search_toggle", state=state)
        except Exception as e:
            log("search_toggle_err", err=f"{type(e).__name__}: {e}")

    def _ask_once(self, prompt: str, timeout: int, deadline: float) -> str:
        if FRESH_CHAT:
            # Land on an empty compose screen so only this question streams.
            try:
                self.page.goto(TARGET, wait_until="domcontentloaded",
                               timeout=45000)
                self._hydrated()
            except Exception:
                pass
        if SEARCH_OFF:
            self._disable_search()
        box = self._composer()
        if box is None:
            raise RuntimeError("no composer on the page; probably signed out")
        self.captured.clear()
        box.click()
        box.fill("") if box.get_attribute("contenteditable") is None else None
        # A bare "\n" typed into a chat composer is an Enter press, which SUBMITS
        # the message -- so a multi-line prompt sent the first line only and the
        # question (below the preamble) never arrived. Measured 2026-08-22 on
        # DeepSeek: it replied "Awaiting your engineering query." Shift+Enter is
        # the newline that does not submit; the single Enter at the end sends.
        for i, line in enumerate(prompt[:8000].split("\n")):
            if i:
                self.page.keyboard.press("Shift+Enter")
            if line:
                self.page.keyboard.type(line, delay=8)
        self.page.keyboard.press("Enter")

        # Network first. The page's own response outlives any redesign. The
        # network share of the budget is capped by the call's own deadline, so
        # a retry can never push the caller past what it asked for.
        net_deadline = min(time.time() + timeout, deadline)
        seen = set()
        while time.time() < net_deadline:
            for resp in list(self.captured):
                if id(resp) in seen:
                    continue
                seen.add(id(resp))
                try:
                    body = resp.body()
                except Exception as _te:
                    continue
                # Connect framing first: its bodies are not valid UTF-8, so
                # resp.text() raises and the text parsers never get a look.
                text = connect_stream_text(body) if body[:1] in (b"\x00", b"\x01") else ""
                if text:
                    log("answer", source="connect",
                        url=resp.url.split("?")[0], chars=len(text))
                    return text.strip()
                try:
                    raw = body.decode("utf-8")
                except Exception:
                    continue
                if '"fragments"' in raw or '/content","o":"APPEND"' in raw:
                    text = deepseek_stream_text(raw)
                elif "data:" in raw:
                    text = sse_text(raw)
                else:
                    text = ""
                if not text:
                    try:
                        obj = json.loads(raw)
                        text = (obj.get("choices", [{}])[0]
                                .get("message", {}).get("content", "")) or ""
                    except Exception:
                        text = ""
                if text and len(text.strip()) >= 1:
                    log("answer", source="network",
                        url=resp.url.split("?")[0], chars=len(text))
                    return text.strip()
            # wait_for_timeout, NOT time.sleep: sync Playwright only dispatches
            # page.on("response") handlers while a Playwright call is waiting.
            # time.sleep froze the event loop, so a response that arrived after
            # the composer submit never reached self.captured and every query
            # fell through to the slow DOM read. Measured 2026-08-22: the
            # completion URL was seen by the handler but never captured mid-poll.
            self.page.wait_for_timeout(500)

        # DOM second, and say so, because it is the weaker of the two.
        left = max(2.0, deadline - time.time())
        text = self.page.evaluate(SETTLE, {"quietMs": 1500,
                                           "capMs": int(left * 1000)})
        if text and len(text.strip()) >= 1:
            log("answer", source="dom", chars=len(text))
            return text.strip()
        # Say WHAT was seen, not just that nothing parsed. A bare "no answer"
        # is unactionable: it cannot tell a provider that moved its endpoint
        # from one whose stream shape this parser does not know. Log the URLs
        # that matched CAPTURE_URL and the head of each body, so the next
        # failure names the shape that needs a branch.
        try:
            shapes = []
            for resp in list(self.captured)[-20:]:
                try:
                    body = resp.text()[:300]
                except Exception:
                    try:
                        raw_b = resp.body()
                        # Keep the whole framed body on disk. 400 bytes of it
                        # showed the frame header but stopped before the op
                        # that carries the words, which is the one that matters.
                        (ROOT / "logs" / "capture-miss.bin").write_bytes(raw_b)
                        body = "<bytes:%d> " % len(raw_b) + repr(raw_b[:200])
                    except Exception as _e:
                        body = f"<unreadable: {type(_e).__name__}>"
                shapes.append({"url": resp.url.split("?")[0], "head": body})
            page_text = ""
            try:
                page_text = (self.page.evaluate(
                    "() => (document.body.innerText||'').slice(-1200)") or "")
            except Exception:
                pass
            shot = str(ROOT / "logs" / "capture-miss.png")
            try:
                self.page.screenshot(path=shot, full_page=False)
            except Exception:
                shot = ""
            log("capture_miss", n=len(self.captured), url=self.page.url,
                shapes=shapes, shot=shot, page_tail=page_text)
        except Exception as _e:
            log("capture_miss_log_failed", error=f"{type(_e).__name__}: {_e}")
        raise RuntimeError("no answer captured from the network or the DOM")

    def ask(self, prompt: str, timeout: int, deadline: float | None = None) -> str:
        last = None
        self.last_used = time.time()
        # One deadline for the whole call, retries included. The caller's
        # timeout is a promise about when it hears back, not a per-attempt
        # allowance, and the HTTP handler's own wait is built on this number.
        # The handler passes its OWN absolute deadline, set when the request
        # arrived, so time spent queued behind other jobs comes out of the
        # caller's budget instead of extending it.
        if deadline is None:
            deadline = time.time() + timeout
        for attempt, wait in enumerate((0,) + RETRY_BACKOFF):
            if wait:
                time.sleep(wait)
            if time.time() >= deadline:
                log("deadline_reached", attempt=attempt)
                break
            try:
                self._ensure()  # wake the browser if it was parked for idle
                if self.refresh_health() != "healthy":
                    self.restart()
                    if self.refresh_health() != "healthy":
                        raise RuntimeError(self.detail)
                out = self._ask_once(prompt, timeout, deadline)
                self.last_used = time.time()
                return out
            except Exception as e:
                last = e
                log("attempt_failed", attempt=attempt, error=f"{type(e).__name__}: {e}")
                try:
                    self.page.goto(TARGET, wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    self.restart()
        raise RuntimeError(str(last) if last else "deadline reached with no answer")

    # -- thread ------------------------------------------------------------

    def _boot(self):
        """start(), but a failure leaves this thread alive and the state honest.

        The boot start reaches the network, so it can fail for reasons that have
        nothing to do with the bridge. Measured 2026-08-22: the Mac lost its
        route, launchd relaunched the daemon, goto raised
        ERR_INTERNET_DISCONNECTED, and the bare start() killed this thread. The
        process stayed alive and listening, so KeepAlive never fired, /health
        read "starting" for hours, and every query sat on the queue with nobody
        left to take it. A boot failure must never kill the worker.
        """
        try:
            self.start()
            self.last_used = time.time()
        except Exception as e:
            try:
                self.stop()
            except Exception:
                pass
            self.state = "down"
            self.detail = f"start failed: {type(e).__name__}: {e}"
            log("boot_failed", error=self.detail)

    def run(self):
        # One start at boot proves the session on disk is good and logs it, so
        # the founder sees it worked. After IDLE_CLOSE with no query the loop
        # parks the browser and the footprint drops to nothing.
        self._boot()
        self.last_used = time.time()
        last_probe = time.time()
        while True:
            try:
                job = self.jobs.get(timeout=5)
            except queue.Empty:
                # Free the browser once it has sat idle. The session is on disk,
                # so the next query brings it straight back signed in.
                if self.page is not None and time.time() - self.last_used > IDLE_CLOSE:
                    self.park()
                # Probe only while the browser is up. Probing a parked browser
                # would launch one just to check it, which is the cost we are
                # avoiding; parked health is last-known-good until a query tests it.
                if self.page is not None and time.time() - last_probe > HEALTH_EVERY:
                    last_probe = time.time()
                    before = self.state
                    if self.refresh_health() != "healthy" and before == "healthy":
                        log("health_lost", detail=self.detail)
                        try:
                            self.restart()
                        except Exception as e:
                            log("restart_failed", error=str(e))
                # A failed boot leaves the browser down, which is not the same
                # as parked: parked keeps state "healthy" because the session is
                # on disk. Retry a down browser on the health clock so an outage
                # heals itself, instead of waiting for a query to discover it.
                if (self.page is None and self.state == "down"
                        and time.time() - last_probe > HEALTH_EVERY):
                    last_probe = time.time()
                    self._boot()
                continue
            if job is None:
                self.stop()
                return
            prompt, timeout, box = job
            # Do not start work nobody is waiting for. A job that sat in the
            # queue past its caller's deadline is a paid model call whose
            # answer is thrown away, and while it runs it blocks every job
            # behind it. Measured 2026-08-22: an abandoned job ran in full.
            deadline = box.get("deadline")
            if box.get("abandoned") or (deadline and time.time() >= deadline):
                box["error"] = "dropped: the caller's deadline passed while queued"
                box["done"].set()
                log("job_dropped", queued_for=round(
                    time.time() - box.get("queued_at", time.time()), 1))
                continue
            try:
                box["answer"] = self.ask(prompt, timeout, deadline)
            except Exception as e:
                box["error"] = f"{type(e).__name__}: {e}"
            finally:
                box["done"].set()


# ---------------------------------------------------------------------- http

BRIDGE: Bridge | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # socketserver hands this to the socket, so a body that never arrives
    # raises instead of parking a thread for ever.
    timeout = READ_TIMEOUT

    def log_message(self, fmt, *a):  # the jsonl log is the record, not stderr
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def _guard(self, fn):
        """No request may end without a reply.

        Measured 2026-08-22: a prompt that was an int raised inside the
        handler, socketserver closed the connection, and the caller saw
        `RemoteDisconnected` with no status and no message. An agent cannot
        tell that apart from the bridge being dead, so it retries a request
        that will never work. Every unhandled error becomes a 500 with a body.
        """
        try:
            fn()
        except Exception as e:
            log("handler_error", path=self.path, error=f"{type(e).__name__}: {e}")
            try:
                self._send(500, {"success": False,
                                 "error": f"internal error: {type(e).__name__}",
                                 "backend": NAME})
            except Exception:
                pass

    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    def _get(self):
        if self.path.rstrip("/") != "/health":
            return self._send(404, {"error": "not found"})
        b = BRIDGE
        self._send(200, {
            "status": "healthy" if b and b.state == "healthy" else (
                b.state if b else "down"),
            "detail": b.detail if b else "no bridge",
            "last_healthy": b.last_ok if b else 0,
            "target": TARGET,
        })

    def _bad(self, code, msg):
        return self._send(code, {"success": False, "error": msg, "backend": NAME})

    def _post(self):
        if self.path.rstrip("/") != "/query":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._bad(400, "bad content-length")
        if n > MAX_BODY:
            return self._bad(413, f"body over {MAX_BODY} bytes")
        try:
            body = self.rfile.read(n) if n else b"{}"
        except Exception as e:
            return self._bad(408, f"body never arrived: {type(e).__name__}")
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return self._bad(400, "bad json")
        if not isinstance(req, dict):
            return self._bad(400, "body must be a json object")

        # Validate rather than coerce. Every one of these was a 500 or a
        # dropped connection before 2026-08-22.
        prompt = req.get("prompt")
        if not isinstance(prompt, str):
            return self._bad(400, "prompt must be a string")
        prompt = prompt.strip()
        if not prompt:
            return self._bad(400, "empty prompt")
        if len(prompt) > MAX_PROMPT:
            return self._bad(413, f"prompt over {MAX_PROMPT} characters")
        t = req.get("timeout")
        if t is None or t == "":
            timeout = ANSWER_TIMEOUT
        else:
            try:
                timeout = int(float(t))
            except (TypeError, ValueError):
                return self._bad(400, "timeout must be a number of seconds")
        timeout = max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))

        now = time.time()
        box = {"done": threading.Event(), "deadline": now + timeout,
               "queued_at": now}
        try:
            BRIDGE.jobs.put_nowait((prompt, timeout, box))
        except queue.Full:
            # One browser, one job at a time. Saying busy lets a caller back
            # off; queueing without limit lets it fill memory and then time
            # out anyway, which is the same failure with a longer fuse.
            log("queue_full", depth=BRIDGE.jobs.qsize())
            return self._bad(503, "bridge busy: the queue is full, retry shortly")

        if not box["done"].wait(max(1.0, box["deadline"] - time.time()) + GRACE):
            # Tell the worker nobody is listening any more, so it drops the
            # job instead of spending a model call on an answer with no home.
            box["abandoned"] = True
            return self._bad(504, "bridge timed out")
        if "answer" in box:
            return self._send(200, {"success": True, "answer": box["answer"],
                                    "backend": NAME})
        return self._bad(502, box.get("error", "unknown"))


# ---------------------------------------------------------------------- main

def call(path, payload=None, timeout=180):
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def daemon_pid():
    """The running daemon's pid, or None. Port first, pidfile second.

    A stale pidfile outlives the process it named, so the listener is the fact
    and the file is only how we learn which process owns it.
    """
    try:
        call("/health", timeout=5)
    except Exception:
        return None
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return -1  # something holds the port and it is not ours to stop


def reap_stale_driver():
    """Kill whatever the last run of THIS bridge left behind.

    `launchctl kickstart -k` sends SIGKILL, which no handler can catch, so the
    python dies and its playwright node driver is reparented to launchd and
    keeps the profile directory locked. The next run then meets a chromium that
    is already running against the same profile and dies with `write EPIPE`.

    Scoping matters: one script drives two bridges. The pidfile lives under
    ROOT, so it names only this bridge's last process. The python is its own
    process group leader (measured 2026-08-22: pid 49334, pgid 49334) and the
    node driver joins that group, so killing the group takes the driver with
    it. Refuse to touch our own group, and treat "no such process" as the
    normal case, because a clean shutdown removes the pidfile.
    """
    try:
        old = int(PIDFILE.read_text().strip())
    except Exception:
        return
    if old <= 1 or old == os.getpid() or old == os.getpgrp():
        return
    try:
        os.killpg(old, signal.SIGKILL)
    except Exception:
        return
    log("reaped_stale_driver", pgid=old)


def stop_daemon(pid):
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        try:
            call("/health", timeout=2)
        except Exception:
            return True
        time.sleep(0.2)
    return False


def start_daemon():
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--daemon"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    # Wait past "starting". The port answers before the browser has loaded the
    # page, so returning on the first 200 reports a state that is about to
    # change and reads as a fault when it is not.
    last = None
    for _ in range(300):
        try:
            last = call("/health", timeout=2)
            if last.get("status") != "starting":
                return last
        except Exception:
            pass
        time.sleep(0.2)
    return last


def main():
    global BRIDGE
    ap = argparse.ArgumentParser(prog="kimi_bridge.py")
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--health", action="store_true")
    ns = ap.parse_args()

    if ns.health:
        try:
            print(json.dumps(call("/health", timeout=10), indent=2))
            return 0
        except Exception as e:
            print(f"bridge is not answering on {HOST}:{PORT}: {e}", file=sys.stderr)
            return 1

    if ns.login:
        # Headed and blocking. The founder signs in by hand once; the profile
        # keeps the session, so the daemon never sees a credential.
        #
        # The daemon is stopped first and restarted after. Two browsers on one
        # profile directory do not collide at launch, which is the trap: they
        # both open, each keeps its cookies in its own memory, and the last one
        # to close wins the file. The founder would sign in, see success, and
        # the daemon would go on reporting signed-out from the jar it loaded
        # before he started. Restarting is what makes the sign-in take.
        was_running = daemon_pid()
        if was_running == -1:
            print(f"something is already listening on {HOST}:{PORT} and it is not "
                  f"this bridge. Stop it first.", file=sys.stderr)
            return 1
        if was_running:
            print(f"stopping the daemon (pid {was_running}) so it does not hold the profile")
            if not stop_daemon(was_running):
                print("the daemon did not stop; not opening a second browser",
                      file=sys.stderr)
                return 1
        b = Bridge(headed=True)
        b.start()
        # flush, because python buffers stdout when it is not a terminal and
        # the founder then watches a silent window with no idea what it wants.
        print(f"A browser is open at {TARGET}. Sign in there; this waits.", flush=True)
        print("Nothing to press. It closes itself once the page is signed in.", flush=True)
        # Waiting on Enter was the wrong signal twice over: a run with no
        # terminal on stdin got EOF and shut the window in under a second, and
        # a run with one asked the founder to confirm a thing the page can
        # simply be asked. Polling the page is the measurement.
        state = "signed-out"
        deadline = time.time() + 900
        while time.time() < deadline:
            state = b.refresh_health()
            if state == "healthy":
                break
            time.sleep(3)
        else:
            print("15 minutes with no sign-in; closing the browser.", file=sys.stderr)
        print(f"state: {state} ({b.detail})")
        b.stop()
        if was_running:
            h = start_daemon()
            if h is None:
                print("the daemon did not come back up; start it by hand with --daemon",
                      file=sys.stderr)
                return 1
            print(f"daemon back up: {h.get('status')} ({h.get('detail')})")
            state = h.get("status", state)
        return 0 if state == "healthy" else 1

    if ns.daemon:
        reap_stale_driver()
        BRIDGE = Bridge()
        t = threading.Thread(target=BRIDGE.run, daemon=True, name=NAME)
        t.start()
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
        PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(str(os.getpid()))
        atexit.register(lambda: PIDFILE.unlink(missing_ok=True))
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        log("listening", host=HOST, port=PORT)
        print(f"kimi bridge on http://{HOST}:{PORT}  (target {TARGET})")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            BRIDGE.jobs.put(None)
        return 0

    if ns.prompt:
        try:
            out = call("/query", {"prompt": ns.prompt})
        except Exception as e:
            print(f"bridge not reachable: {e}", file=sys.stderr)
            return 1
        if out.get("success"):
            print(out["answer"])
            return 0
        print(f"bridge failed: {out.get('error')}", file=sys.stderr)
        return 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

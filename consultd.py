#!/usr/bin/env python3
"""A second mind on tap, over HTTP, for every agent on this machine.

An agent that is stuck has two bad options: guess, or wake the founder. This
gives it a third. It asks a different model, gets an answer, and carries on.

  consultd.py serve                 run the daemon on 127.0.0.1:8765
  consultd.py ask "question"        one question, no daemon, straight to stdout
  consultd.py health                is it up, which backend, is that backend ready
  consultd.py backends              what is available on this machine and why not

WHY NOT A BROWSER BRIDGE. The first design drove a logged-in Kimi tab over
Chrome's debug port and scraped the reply. That works until the page changes a
class name. It also needs Chrome started with --remote-debugging-port, which
hands every cookie in the profile to any process on the box. `kimi -p` is the
same model, the same subscription, no browser, no selectors, no scraping, and
it survives a laptop reboot. The CDP path stays a documented fallback and is
not built, because building a backend nobody can prove works is worse than
saying it is not there.

BACKENDS CASCADE. Tried in this order. The first one that is ready gets the
question; if it errors the next one gets it, and the reply says which one
answered.

  kimi-bridge  127.0.0.1:8766              browser bridge to the kimi.ai web app
  deepseek     127.0.0.1:8767              browser bridge to chat.deepseek.com
  ollama       127.0.0.1:11434             local, offline, weakest, always there
  none         always ready, always 503    so the caller's fallback stays honest

NO KIMI CLI, removed 2026-08-22 on the founder's instruction. `kimi -p` calls
api.kimi.com/coding, which is Kimi For Coding. This account does not hold that
subscription and every readiness check returned 402. The web app subscription
is a different product and is reached through the browser bridge above.

NO GEMINI, on the founder's instruction, 2026-08-22. It had earned removal
anyway: the free tier was spent, and its CLI retries a 429 internally instead
of returning one, so it burned its whole time cap on every consult and then
failed. A backend that cannot fail fast is worse than no backend. Do not add
it back without a measurement showing it answers.

SECURITY. Binds loopback only. Every request needs the bearer token in
~/.claude/consult-token, created 0600 on first run. Local callers read that
file themselves, so the token costs them nothing; a caller reaching this
through a tunnel sets CONSULT_TOKEN and the token is the whole gate. The
subprocess backends run in an empty directory with no repository in it and
without --yolo or --auto, so a consult cannot edit anything.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.3.0"
HOME = Path.home()
CLAUDE = HOME / ".claude"
TOKEN_FILE = Path(os.environ.get("CONSULT_TOKEN_FILE", CLAUDE / "consult-token"))
LOG_FILE = Path(os.environ.get("CONSULT_LOG", CLAUDE / "logs" / "consult.jsonl"))
WORKDIR = Path(os.environ.get("CONSULT_WORKDIR", CLAUDE / "consult-work"))
DEFAULT_PORT = int(os.environ.get("CONSULT_PORT", "8765"))
DEFAULT_TIMEOUT = int(os.environ.get("CONSULT_TIMEOUT", "600"))
MAX_CONCURRENT = int(os.environ.get("CONSULT_CONCURRENCY", "2"))
LOG_CONTENT = os.environ.get("CONSULT_LOG_CONTENT", "1") != "0"
READY_TTL = 30
STRIKES = int(os.environ.get("CONSULT_STRIKES", "3"))
BENCH_SECONDS = int(os.environ.get("CONSULT_BENCH_SECONDS", "600"))

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(s):
    return ANSI.sub("", s)


def ensure_token():
    """Create the shared secret once, 0600, and hand back its value."""
    if TOKEN_FILE.exists():
        v = TOKEN_FILE.read_text().strip()
        if v:
            return v
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    v = secrets.token_urlsafe(32)
    fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(v + "\n")
    return v


def log(row):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not LOG_FILE.exists():
            os.close(os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT, 0o600))
        with open(LOG_FILE, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        try:
            (
                __import__("sys").path.append(
                    str(__import__("guards_root").GUARDS_ROOT)
                ),
                __import__("guard_report").broken(__file__, 104),
            )
        except Exception:
            pass


def workdir():
    """An empty directory with no repository in it, so a consult cannot edit."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    g = WORKDIR / ".gitignore"
    if not g.exists():
        g.write_text("*\n")
    return str(WORKDIR)


class Backend:
    name = "base"
    free = True
    # Its share of one consult's wall clock. A backend that hangs must not
    # spend the whole budget before the next one is even tried: the backend
    # removed on 2026-08-22 burned 180s failing to reach an answer that ollama
    # then produced in 46s. Keep this cap on anything added here.
    cap = 150

    def ready(self):
        return False, "not implemented"

    def ask(self, prompt, timeout):
        raise NotImplementedError


KIMI_NOTE = HOME / ".claude" / "logs" / "kimi-login-last.txt"


class KimiBackend(Backend):
    """Moonshot's Kimi Code CLI in one-shot prompt mode."""

    name = "kimi"
    BIN = Path(os.environ.get("KIMI_BIN", HOME / ".kimi-code" / "bin" / "kimi"))

    def ready(self):
        if not self.BIN.exists():
            return False, f"{self.BIN} is not on this machine"
        try:
            p = subprocess.run(
                [str(self.BIN), "provider", "list"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            return False, "`kimi provider list` did not answer in 20s"
        out = strip_ansi(p.stdout + p.stderr)
        if "No providers configured" in out or p.returncode != 0:
            # Telling someone to run `kimi login` when they already have, and
            # it failed for a reason another code will not fix, is how two
            # device codes expired unapproved on 2026-08-22. Whatever the last
            # login measured is more use than the standing advice, so the note
            # wins over the generic line. `consultd.py login` writes it, and it
            # writes a status code rather than a vendor sentence.
            note = ""
            try:
                note = KIMI_NOTE.read_text().strip()
            except OSError:
                pass
            if note:
                return False, f"login was tried and did not take: {note}"
            return False, "no provider configured -- run `consultd.py login` once"
        return True, (out.strip().splitlines() or ["configured"])[0]

    def ask(self, prompt, timeout):
        cmd = [str(self.BIN), "-p", prompt]
        model = os.environ.get("CONSULT_KIMI_MODEL")
        if model:
            cmd[1:1] = ["-m", model]
        p = subprocess.run(
            cmd, cwd=workdir(), capture_output=True, text=True, timeout=timeout
        )
        out = strip_ansi(p.stdout).strip()
        err = strip_ansi(p.stderr).strip()
        if p.returncode != 0 and not out:
            raise RuntimeError(err or f"kimi exited {p.returncode} with no output")
        return out or err


class OllamaBackend(Backend):
    """A model on this machine. Weakest of the three and the only offline one."""

    name = "ollama"
    URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

    def model(self):
        want = os.environ.get("CONSULT_OLLAMA_MODEL")
        if want:
            return want
        for m in self._tags():
            if "coder" in m or "qwen" in m:
                return m
        return self._tags()[0] if self._tags() else ""

    def _tags(self):
        try:
            with urllib.request.urlopen(f"{self.URL}/api/tags", timeout=5) as r:
                names = [m["name"] for m in json.loads(r.read())["models"]]
        except Exception:
            return []
        return [n for n in names if "embed" not in n]

    def ready(self):
        tags = self._tags()
        if not tags:
            return False, f"nothing answering at {self.URL} -- is `ollama serve` up?"
        return True, f"{self.model()} (local, offline, no quota to run out)"

    def ask(self, prompt, timeout):
        body = json.dumps(
            {"model": self.model(), "prompt": prompt, "stream": False}
        ).encode()
        req = urllib.request.Request(
            f"{self.URL}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read()).get("response", "").strip()
        if not out:
            raise RuntimeError("ollama returned an empty response")
        return out


class NoneBackend(Backend):
    """Always available, always refuses. Keeps the caller's fallback honest."""

    name = "none"

    def ready(self):
        return True, "no real backend configured; every consult returns 503"

    def ask(self, prompt, timeout):
        raise RuntimeError("no consult backend on this machine")


# Keyed router lanes first: one endpoint, the estate router, its lanes as the
# cascade (direct_api_backends.DIRECT, crew#568 Phase 2). Then the local ollama
# floor, then the honest 503. The kimi and deepseek browser bridges were retired
# 2026-08-29: a scrape dies at a login wall at 03:00 and stays dead until a
# person signs in (LAW 27); a router key dies when a number someone watches
# hits zero. Absent or unimportable costs the cascade its keyed lanes, never
# the daemon.
try:
    from direct_api_backends import DIRECT as _DIRECT
except Exception:  # noqa: BLE001
    _DIRECT = []

BACKENDS = _DIRECT + [OllamaBackend(), NoneBackend()]
REAL = [b for b in BACKENDS if b.name != "none"]


def by_name(name):
    for b in BACKENDS:
        if b.name == name:
            return b
    raise SystemExit(f"no backend named {name}")


def wanted():
    w = os.environ.get("CONSULT_BACKEND")
    return None if not w or w == "auto" else w


def build_prompt(question, context, agent):
    """Say who is asking and what they need, so the answer is usable as data."""
    parts = [
        f"You are answering a question from an autonomous engineering agent "
        f"called {agent or 'unknown'}. It will parse your reply, not read it "
        f"for pleasure. Answer directly. Lead with the answer. If you are not "
        f"sure, say which part you are not sure about and what command would "
        f"settle it. Do not ask follow-up questions -- there is nobody to "
        f"answer them.",
        "",
    ]
    if context:
        parts += ["CONTEXT", context.strip(), ""]
    parts += ["QUESTION", question.strip()]
    return "\n".join(parts)


class Service:
    """Holds the cascade and re-checks it, because logins happen after boot.

    `kimi login` happens after this daemon starts, not before, and a founder
    who has to restart a service to finish logging in will not finish logging
    in. Readiness is therefore re-read every 30 seconds rather than once.
    """

    def __init__(self, pin=None):
        self.pin = pin
        self.chain = [by_name(pin)] if pin else list(BACKENDS)
        self.sem = threading.Semaphore(MAX_CONCURRENT)
        self.inflight = 0
        self.lock = threading.Lock()
        self.last = None
        self.counts = {"ok": 0, "failed": 0}
        self._ready = {}
        self._checked_at = 0.0
        self.strikes = {}  # name -> consecutive failures
        self.benched = {}  # name -> unix time it may be tried again

    def _bench(self, name):
        """Three failures in a row and a backend sits out for ten minutes.

        Without this every caller pays that backend's cap on every consult,
        for as long as it stays broken. Ten minutes is short enough that a
        quota reset or a restarted server comes back on its own.
        """
        n = self.strikes.get(name, 0) + 1
        self.strikes[name] = n
        if n >= STRIKES:
            self.benched[name] = time.time() + BENCH_SECONDS

    def _clear(self, name):
        self.strikes.pop(name, None)
        self.benched.pop(name, None)

    def readiness(self):
        now = time.time()
        if self._ready and now - self._checked_at < READY_TTL:
            return self._ready
        self._ready = {b.name: b.ready() for b in self.chain}
        self._checked_at = now
        return self._ready

    def live(self):
        """Every ready, un-benched backend in cascade order.

        Falls back to `none` only when nothing real is left, so the caller
        gets an honest 503 instead of a plausible answer from nowhere.
        """
        r = self.readiness()
        now = time.time()
        real = [
            b
            for b in self.chain
            if b.name != "none" and r[b.name][0] and self.benched.get(b.name, 0) < now
        ]
        return real or [b for b in self.chain if b.name == "none"] or self.chain[:1]

    def consult(
        self, question, context="", agent="unknown", cid=None, timeout=DEFAULT_TIMEOUT
    ):
        cid = cid or f"consult-{int(time.time() * 1000)}"
        prompt = build_prompt(question, context, agent)
        started = time.time()
        tried, answer, used, err = [], None, None, None
        with self.sem:
            with self.lock:
                self.inflight += 1
            try:
                deadline = started + timeout
                for b in self.live():
                    budget = int(min(b.cap, deadline - time.time()))
                    if budget < 5:
                        tried.append(f"{b.name}: skipped, consult budget spent")
                        continue
                    try:
                        answer = b.ask(prompt, budget)
                        used = b.name
                        err = None
                        self._clear(b.name)
                        break
                    except subprocess.TimeoutExpired:
                        err = f"{b.name}: timed out after {budget}s"
                    except Exception as e:
                        err = f"{b.name}: {e}"
                    self._bench(b.name)
                    tried.append(err)
            finally:
                with self.lock:
                    self.inflight -= 1
        status = "success" if answer else "failed"
        elapsed = round(time.time() - started, 2)
        self.counts["ok" if status == "success" else "failed"] += 1
        self.last = {
            "id": cid,
            "agent": agent,
            "status": status,
            "backend": used,
            "elapsed_s": elapsed,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        row = dict(self.last)
        row["tried"] = tried
        row["question_sha256"] = hashlib.sha256(question.encode()).hexdigest()[:16]
        if LOG_CONTENT:
            row["question"] = question
            row["answer"] = answer
        log(row)
        return {
            "consult_id": cid,
            "status": status,
            "answer": answer,
            "error": None if answer else (err or "no backend answered"),
            "backend": used,
            "tried": tried,
            "elapsed_s": elapsed,
            "timestamp": self.last["at"],
        }

    def health(self):
        r = self.readiness()
        live = [b.name for b in self.live() if b.name != "none"]
        return {
            "status": "ok" if live else "degraded",
            "cascade": [b.name for b in self.chain],
            "live": live,
            "backends": {
                n: {
                    "ready": ok,
                    "detail": d,
                    "strikes": self.strikes.get(n, 0),
                    "benched_for_s": max(
                        0, round(self.benched.get(n, 0) - time.time())
                    ),
                }
                for n, (ok, d) in r.items()
            },
            "inflight": self.inflight,
            "counts": self.counts,
            "last_consult": self.last,
            "version": VERSION,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = f"consultd/{VERSION}"
    service = None
    token = None

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

    def _send(self, code, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        got = self.headers.get("Authorization", "")
        if got.startswith("Bearer "):
            got = got[7:]
        if secrets.compare_digest(got.strip(), self.token):
            return True
        self._send(401, {"status": "failed", "error": "bad or missing bearer token"})
        return False

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            if not self._authed():
                return
            self._send(200, self.service.health())
        else:
            self._send(404, {"error": "GET /health or POST /consult"})

    def do_POST(self):
        if self.path.rstrip("/") != "/consult":
            self._send(404, {"error": "GET /health or POST /consult"})
            return
        if not self._authed():
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"status": "failed", "error": f"bad json: {e}"})
            return
        q = (data.get("question") or "").strip()
        if not q:
            self._send(400, {"status": "failed", "error": "question is required"})
            return
        res = self.service.consult(
            q,
            data.get("context", ""),
            data.get("agent", "unknown"),
            data.get("id"),
            int(data.get("timeout") or DEFAULT_TIMEOUT),
        )
        self._send(200 if res["status"] == "success" else 503, res)


def cmd_serve(argv):
    port = DEFAULT_PORT
    pin = wanted()
    for i, a in enumerate(argv):
        if a == "--port":
            port = int(argv[i + 1])
        if a == "--backend" and argv[i + 1] != "auto":
            pin = argv[i + 1]
    svc = Service(pin)
    Handler.service = svc
    Handler.token = ensure_token()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    h = svc.health()
    print(f"consultd {VERSION} on http://127.0.0.1:{port}")
    print(f"  cascade  {' -> '.join(h['cascade'])}")
    for n, s in h["backends"].items():
        print(f"    {'READY  ' if s['ready'] else 'not ok '} {n:7} {s['detail']}")
    print(f"  token    {TOKEN_FILE} (0600)")
    print(f"  log      {LOG_FILE}")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_ask(argv):
    ctx, agent = "", "cli"
    for flag, setter in (("--context", "ctx"), ("--agent", "agent")):
        if flag in argv:
            i = argv.index(flag)
            v = argv[i + 1] if i + 1 < len(argv) else ""
            if setter == "ctx":
                ctx = v
            else:
                agent = v
            del argv[i : i + 2]
    q = " ".join(a for a in argv if not a.startswith("--"))
    res = Service(wanted()).consult(q, ctx, agent)
    if res["status"] == "success":
        print(res["answer"])
        return 0
    print(f"consult failed: {res['error']}", file=sys.stderr)
    for t in res["tried"]:
        print(f"  tried {t}", file=sys.stderr)
    return 1


KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_CREDS = HOME / ".kimi-code" / "credentials" / "kimi-code.json"
KIMI_AUTH_HOST = os.environ.get("KIMI_OAUTH_HOST", "https://auth.kimi.com")
KIMI_API = os.environ.get("KIMI_CODE_BASE_URL", "https://api.kimi.com/coding/v1")


def kimi_diagnose():
    """Split a kimi failure into the token and the plan. Returns (kind, detail).

    Written because an agent read "unable to verify your membership benefits"
    out of a log and reported it as the cause while the founder was signed in
    to Kimi at that moment. The sentence is the vendor's; the status code is
    the measurement, and the two say different things. On 2026-08-22 the same
    token returned 200 on /me and 402 on /models: authentication perfect,
    coding plan absent. Only one of those is fixed by another device tap.

    Kinds: ok, no-credentials, auth-dead, no-plan, unknown.
    """
    import json as _json, time as _time, urllib.error, urllib.parse, urllib.request

    try:
        creds = _json.loads(KIMI_CREDS.read_text())
        refresh = creds["refresh_token"]
    except (OSError, ValueError, KeyError):
        return "no-credentials", f"nothing stored at {KIMI_CREDS}"

    body = urllib.parse.urlencode(
        {
            "client_id": KIMI_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }
    ).encode()
    req = urllib.request.Request(
        f"{KIMI_AUTH_HOST}/api/oauth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            fresh = _json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return "auth-dead", f"refresh returned {e.code}; the stored grant is spent"
    except OSError as e:
        return "unknown", f"cannot reach {KIMI_AUTH_HOST}: {e}"

    # The server rotates the refresh token on every exchange, so the new one is
    # written before anything else can fail. Dropping it costs a device tap.
    try:
        creds.update(fresh)
        creds["expires_at"] = int(_time.time()) + int(fresh.get("expires_in", 900))
        KIMI_CREDS.write_text(_json.dumps(creds))
        KIMI_CREDS.chmod(0o600)
    except OSError as e:
        return "unknown", f"refreshed but could not save the rotated grant: {e}"

    probe = urllib.request.Request(
        f"{KIMI_API}/models",
        headers={"Authorization": "Bearer " + fresh["access_token"]},
    )
    try:
        with urllib.request.urlopen(probe, timeout=30) as r:
            return "ok", f"{KIMI_API}/models returned {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (402, 403):
            return "no-plan", (
                f"the token is good and {KIMI_API}/models returned "
                f"{e.code}: this account is not on Kimi For Coding, "
                f"which is a separate subscription from the web app"
            )
        return "unknown", f"{KIMI_API}/models returned {e.code}"
    except OSError as e:
        return "unknown", f"cannot reach {KIMI_API}: {e}"


def cmd_login(argv):
    """Log kimi in, and do nothing at all if it is already logged in.

    `kimi login` always mints a device code, even when the machine already
    holds a working provider. A code nobody needs still expires, and an expired
    code in the log reads exactly like a failed login. This checks first, and
    only mints one when there is something to fix.
    """
    k = KimiBackend()
    ok, detail = k.ready()
    if ok:
        print(f"kimi is already logged in: {detail}")
        return 0
    if not k.BIN.exists():
        print(f"kimi is not installed at {k.BIN}", file=sys.stderr)
        return 2

    kind, why = kimi_diagnose()
    if kind == "no-plan":
        # A device code cannot buy a subscription. Minting one here is how the
        # founder ends up tapping approvals that were never going to work.
        KIMI_NOTE.parent.mkdir(parents=True, exist_ok=True)
        KIMI_NOTE.write_text(why)
        print(f"not minting a device code: {why}", file=sys.stderr)
        print(
            "a tap cannot fix this. The estate keeps running on ollama.",
            file=sys.stderr,
        )
        return 1
    if kind == "ok":
        print(
            f"the credential is live ({why}) but the CLI wrote no provider. "
            f"Minting one device code to let it write one."
        )
    else:
        print(f"kimi is not logged in ({kind}: {why}). Minting one device code.")
    try:
        p = subprocess.run(
            [str(k.BIN), "login"], capture_output=True, text=True, timeout=1800
        )
    except subprocess.TimeoutExpired:
        KIMI_NOTE.parent.mkdir(parents=True, exist_ok=True)
        KIMI_NOTE.write_text("the device code expired before it was approved")
        print("the device code expired before anyone approved it", file=sys.stderr)
        return 1
    out = strip_ansi(p.stdout + p.stderr).strip()
    print(out)

    ok, detail = k.ready()
    if ok:
        KIMI_NOTE.unlink(missing_ok=True)
        print(f"\nlogged in: {detail}")
        return 0
    reason = next(
        (
            l.strip()
            for l in out.splitlines()
            if "failed" in l.lower() or "unable" in l.lower()
        ),
        "login finished but no provider was configured",
    )
    KIMI_NOTE.parent.mkdir(parents=True, exist_ok=True)
    KIMI_NOTE.write_text(reason)
    print(f"\nnot logged in: {reason}", file=sys.stderr)
    return 1


def cmd_backends(argv):
    svc = Service(wanted())
    for name, s in svc.health()["backends"].items():
        print(f"{'READY  ' if s['ready'] else 'not ok '} {name:7} {s['detail']}")
    print(f"\ncascade in use: {' -> '.join(b.name for b in svc.live())}")
    return 0


def cmd_health(argv):
    print(json.dumps(Service(wanted()).health(), indent=2))
    return 0


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "serve":
        return cmd_serve(rest) or 0
    if cmd == "ask":
        return cmd_ask(rest)
    if cmd == "health":
        return cmd_health(rest)
    if cmd == "backends":
        return cmd_backends(rest)
    if cmd == "login":
        return cmd_login(rest)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

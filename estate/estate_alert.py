#!/usr/bin/env python3
"""estate_alert — gateway-INDEPENDENT operator alerting.

The estate's normal alert path goes through the gateway queue. But the failures we
most need to hear about are exactly when the gateway is DOWN (crash-loop, preflight
failure). So this sends straight to Telegram via urllib, reading the bot token and
operator channel from ~/.hermes/.env — no gateway, no heavy deps, stdlib only.

Built 2026-06-20 after a syntax-broken commit crash-looped the gateway silently.
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
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import pathlib
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telegram_ledger                                        # noqa: E402  (path set above)

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
# Credentials moved out of the Hermes tree on 2026-08-22. Two reasons, both real:
# ~/.hermes symlinks into ~/Documents, which macOS TCC hides from a bootstrapped
# LaunchAgent (com.founder.estatepush failed on schedule and passed by hand for
# exactly this); and Hermes is discontinued, so this tree retires under crew #13.
# The old path stays as a fallback until it does.
_ENV = pathlib.Path.home() / ".config" / "estate" / "estate.env"
if not _ENV.is_file():
    _ENV = HERMES_HOME / ".env"
_DEBOUNCE = HERMES_HOME / "logs" / ".alert-debounce.json"


def _env(key: str) -> str | None:
    """Read one KEY from ~/.hermes/.env (env var wins), minimal parser, no deps."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in _ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _debounced(key: str, window_s: float, record: bool = True) -> bool:
    """True if an alert under `key` fired within window_s (i.e. suppress this one).

    `record=False` asks the question without answering it in the file. A dry run used to arm
    the debounce: measured 2026-08-22, `estate_watch.py --dry-run` marked all nine estate
    criticals, and the real run five minutes later was suppressed in full. A dry run that
    changes state is not a dry run, and this one silenced the alerts it was meant to preview.
    """
    if not key:
        return False
    now = time.time()
    try:
        data = json.loads(_DEBOUNCE.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    last = data.get(key, 0)
    if now - last < window_s:
        return True
    if not record:
        return False
    data[key] = now
    try:
        _DEBOUNCE.parent.mkdir(parents=True, exist_ok=True)
        _DEBOUNCE.write_text(json.dumps(data))
    except OSError:
        try: (__import__("sys").path.insert(0,str(__import__("guards_root").GUARDS_ROOT)), __import__("guard_report").broken(__file__, 78))
        except Exception: pass
    return False


# Founder, 2026-08-29: "nosie eveyrwhere", "i cant see ny innportannt nessages", "they should be
# goig else where", "flooding ny view". Measured that morning from ~/.estate/alerts/inbox.jsonl:
# 3,104 alerts, of which 2,609 were NINE distinct sentences repeated, one of them 1,730 times.
#
# The debounce above was already here and did nothing about it, because it was opt-in: the send
# read `if debounce_key and _debounced(...)`, so a caller that named no key repeated forever. A
# noise control that defaults to off is not a noise control (LAW 44), and this is the second time
# he has said it -- the comment further down records "still noisy telegram" and "all important
# links need to be pinned and the noisy stuff moved elsewhere" from 2026-08-25. That fix moved the
# flood off his DM and into the inbox his board reads. It did not make it smaller.
#
# So an alert that names no key derives one from its sender and its own words, with digits
# normalised out: "BLOCKED for 41m" and "BLOCKED for 42m" are one sentence said twice, not two
# alerts. The window is an hour rather than the keyed default of five minutes, because a caller
# that names no key is by definition not managing its own cadence. A caller that must page faster
# still passes its own key and keeps the 300s default. Nothing is lost either way: every
# suppressed alert is recorded in the ledger, which is what the ledger is for.
_UNKEYED_WINDOW_S = 3600.0


def _derived_key(source: str, text: str) -> str:
    """A stable key for an alert whose caller named none: same sender, same sentence, same key."""
    body = re.sub(r"\d+", "<n>", " ".join(str(text).split()))[:400]
    return "auto:" + hashlib.sha256(f"{source}\n{body}".encode()).hexdigest()[:16]


# Telegram rejects a sendMessage over 4096 characters OUTRIGHT — the whole message, not the
# tail. An alerting path that loses the entire page because one fault line grew is the worst
# way to fail: it is silent, and it is silent exactly when there is a lot to say. Measured
# 2026-08-19: the self-check's estate section alone builds 2767 characters from 11 faults.
#
# This lives in the SENDER, not in any one caller, because every caller has the same ceiling
# and a rule kept private gets reimplemented three times and wrong twice.
TELEGRAM_MAX_CHARS = 4096


def _fit(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    """Trim to Telegram's ceiling on a line boundary, saying how much was dropped."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        marker = f"\n… {len(lines) - i} more line(s) trimmed; run the command above for all of it"
        if used + len(line) + 1 + len(marker) > limit:
            return "\n".join(kept) + marker
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


# ── The hourly ceiling ────────────────────────────────────────────────────────────────────
#
# Founder, 2026-08-19: the Telegram channel is "too noisy, hard to see anything useful".
# Per-alert debouncing already existed and was doing its job — what it cannot see is the
# TOTAL. Twenty different faults, each firing once and each correctly un-debounced, still
# buries the one message worth reading.
#
# So there is a ceiling on how many alerts an hour may carry. Past it, alerts stop reaching
# the channel and ONE line goes instead, naming the count and the command that shows them
# all. No information is lost: every capped alert is in the ledger, which is the point of
# having a ledger. A cap that silently drops is unacceptable; a cap that says how much it
# dropped is just a summary.
ALERT_HOURLY_CAP = int(os.environ.get("HERMES_ALERT_HOURLY_CAP", "12"))
_CAP_NOTICE_KEY = "__hourly_cap_notice__"


def _alerts_sent_last_hour() -> int:
    """Alerts this sender put in the channel in the last hour. The cap notice counts too:
    it occupies a message slot exactly like an alert does."""
    return sum(1 for r in telegram_ledger.read(3600.0)
               if r.get("outcome") == "sent" and r.get("source") != "test")


def _cap_notice_due() -> bool:
    """One notice per hour, not one per capped alert — otherwise the cap is the noise."""
    return not any(r.get("key") == _CAP_NOTICE_KEY for r in telegram_ledger.read(3600.0))


# crew#407 (2026-08-27): a session sent the router Admin UI password to the founder's Telegram.
# A delivery channel is not a secret store. Every sender in this tree passes through here; text
# shaped like a credential is refused before the API call, and the caller sees REFUSED on stderr.
# The shapes are the value, never the word: "password rotated" passes, "password: hunter2" does not.
_CREDENTIAL_SHAPES = [
    # "<name>: <value>" where the value looks like a value: not a placeholder (${X}, {{x}}, <x>,
    # os.environ/X, ***), not an ALL_CAPS env name (vault-seed writes KEY=ENVKEY mappings).
    re.compile(r"(?i)(?:password|passwd|passphrase|client[_ -]?secret|master[_ -]?key|api[_ -]?key|"
               r"private[_ -]?key|(?<![a-z])(?:secret|token))\s*[:=]\s*[`'\"]?"
               r"(?!\$|\{|<|\*|os\.environ|os\.getenv|process\.env|get[A-Za-z]*\(|"
               r"(?-i:[A-Z][A-Z0-9_]{5,})(?:[^A-Za-z0-9_]|$))[A-Za-z0-9!#$%&*+\[\]{}<>?_./=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),                 # OpenAI / LiteLLM keys
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                    # AWS access key id
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),          # Slack
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"),           # Telegram bot token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # "user: x / <value>" pairs where the value is not an ALL_CAPS name
    re.compile(r"(?i)\b(?:user(?:name)?|login)\s*[:=]\s*\S+\s*[/,\n]\s*(?:pass(?:word)?\s*[:=]\s*)?"
               r"(?!(?-i:[A-Z][A-Z0-9_]{5,})\b)[A-Za-z0-9!#$%&*()+_./=-]{10,}\s*$"),
]


def credential_shape(text: str) -> str | None:
    """Name the first credential shape in text, or None. Pure, so both senders and the test share it."""
    for rx in _CREDENTIAL_SHAPES:
        m = rx.search(text)
        if m:
            return m.group(0)[:12] + "…"
    return None


class CredentialRefused(ValueError):
    """Raised by every Telegram sender when the text carries a credential shape (crew#407)."""


def _post(token: str, chat: str, text: str) -> int:
    """The raw send. Kept separate so the cap notice can go out without being capped.

    Returns Telegram's message_id, which is the arrival receipt — an HTTP 200 only says
    the API took the call. Returns 0 on a 200 with no id. An int is truthy, so callers
    written against the old bool return are unaffected.
    """
    hit = credential_shape(text)
    if hit:
        print(f"REFUSED: credential shape ({hit}) in Telegram text; never sent (crew#407)", file=sys.stderr)
        raise CredentialRefused(hit)
    payload = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=10) as r:
        if r.status != 200:
            return 0
        try:
            return int(json.load(r).get("result", {}).get("message_id") or 0)
        except (ValueError, TypeError, AttributeError):
            return 0


def send_operator_alert(text: str, *, debounce_key: str | None = None,
                        debounce_s: float = 300.0, dry_run: bool = False) -> bool:
    """Send `text` to the operator's Telegram channel. Returns True if sent.

    Returns False (without raising) on missing creds, debounce suppression, or
    network error — alerting must never crash the caller.
    """
    source = os.path.basename(sys.argv[0] or "")
    if not source or source == "-":          # stdin script, or an embedded caller
        source = "estate_alert"
    key, window = debounce_key, debounce_s
    if not key:
        key, window = _derived_key(source, text), _UNKEYED_WINDOW_S
    if _debounced(key, window, record=not dry_run):
        # A suppressed alert is recorded too. The debounce is the noise control that
        # already exists; if it never appears in the ledger it reads as doing nothing.
        telegram_ledger.record(source, "suppressed", text, key=key)
        return False
    token = _env("TELEGRAM_BOT_TOKEN")
    # Founder, 2026-08-25, three times in one day: "still noisy telegram", "all important
    # links need to be pinned and the noisy stuff moved elsewhere". The home channel is his
    # private DM. Automated alerts go to TELEGRAM_ALERT_CHANNEL if set (a group he adds the
    # bot to), else to the on-disk inbox the founder board reads. The DM is for conversation.
    # HERMES_ALERT_DM_FALLBACK=1 restores the old behaviour.
    chat = _env("TELEGRAM_ALERT_CHANNEL")
    if not chat and os.environ.get("HERMES_ALERT_DM_FALLBACK", "0") != "1":
        inbox = Path(os.environ.get("ESTATE_ALERT_INBOX",
                                    str(Path.home() / ".estate" / "alerts" / "inbox.jsonl")))
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "source": source, "key": key,
                                 "text": _fit(text)}) + "\n")
        telegram_ledger.record(source, "inboxed", text, key=key)
        if dry_run:
            print(f"[estate_alert] inboxed to {inbox} (no TELEGRAM_ALERT_CHANNEL)")
        return True
    if not chat:
        chat = _env("TELEGRAM_HOME_CHANNEL")
    if not token or not chat:
        if dry_run:
            print(f"[estate_alert] MISSING creds (token={bool(token)} chat={bool(chat)})")
        telegram_ledger.record(source, "no-creds", text, key=key)
        return False
    text = _fit(text)
    if dry_run:
        print(f"[estate_alert] would send to chat {chat[:4]}…: {text[:120]}")
        return True

    if ALERT_HOURLY_CAP > 0 and _alerts_sent_last_hour() >= ALERT_HOURLY_CAP:
        telegram_ledger.record(source, "rate-capped", text, key=key)
        if not _cap_notice_due():
            return False
        notice = (f"🔇 Alert ceiling reached: {ALERT_HOURLY_CAP} in the last hour, so further "
                  f"alerts are being held.\nThey are all recorded. See them with:\n"
                  f"  python3 ~/.hermes/scripts/telegram_noise.py --since 2h")
        try:
            ok = _post(token, chat, notice)
        except Exception as exc:
            print(f"[estate_alert] cap notice failed: {exc!r}", file=sys.stderr)
            return False
        telegram_ledger.record(source, "sent" if ok else "failed", notice,
                               key=_CAP_NOTICE_KEY, msg_id=int(ok or 0))
        return False

    try:
        ok = _post(token, chat, text)
        telegram_ledger.record(source, "sent" if ok else "failed", text,
                               key=key, msg_id=int(ok or 0))
        return ok
    except Exception as exc:
        # Never raise — but never swallow the reason either. A page that vanishes with no
        # trace cannot be diagnosed, and this is the path that reports everything else.
        print(f"[estate_alert] send failed: {exc!r}", file=sys.stderr)
        telegram_ledger.record(source, "failed", text, key=key)
        return False


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) or "🛰️ estate_alert self-test"
    dry = os.environ.get("ESTATE_ALERT_DRYRUN") == "1"
    ok = send_operator_alert(msg, dry_run=dry)
    print(f"sent={ok} dry_run={dry}")

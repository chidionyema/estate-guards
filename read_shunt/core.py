"""read-shunt core: the offload itself. stdlib only, no Claude/pi imports.

The frontier model never sees the bulk read: the file goes to a cheap worker model through
the estate's LiteLLM router, and a structured digest comes back instead. Everything here is
fail-open (LAW 38): any failure passes the read through and is recorded, never refuses work.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import estate_roster
from .ledger import ledger_write

HOME = Path.home()


class EmptyDigest(RuntimeError):
    """The worker answered 200 with no content; graded like a 5xx, never shown to the frontier."""


CAT_RE = re.compile(r"^\s*cat\s+(?:-[A-Za-z]+\s+)?(['\"]?)([^\s'\"|;&<>]+)\1\s*$")

PROMPT = """You are a code-reading worker for a senior engineer who will NOT see this file. Produce a digest they can act on without reading it. Be exact and dense; no preamble. HARD BUDGET: the whole digest must be under 2500 characters. Prefer line ranges over prose.

FILE: {path} ({lines} lines)

Return, in this order, as plain text with these headings:
PURPOSE: one or two sentences.
STRUCTURE: the top-level units (functions, classes, sections, resources) as `L<start>-L<end> <name>: <=8 words`. Fold imports, constants and trivia into one line each. Cover the whole file span.
KEY VALUES: constants, env vars, paths, ports, hosts, versions, flags, external calls (line numbers).
NOTABLE: bugs, TODOs, dead code, security or correctness smells, things that contradict their own comments (line numbers). Say "none found" if none.
READ EXACTLY: the 1-3 line ranges most worth reading verbatim for the likeliest tasks (edit, debug, extend), and why.

FILE CONTENT:
{body}
"""


def _key() -> str:
    k = os.environ.get("LITELLM_API_KEY", "")
    if not k:
        p = HOME / ".config" / "prospector" / "secrets.d" / "LITELLM_API_KEY"
        try:
            k = p.read_text().strip()
        except OSError:
            k = ""
    return k


def _base(cfg: dict) -> str:
    # LAW 46: the zone is never a literal here. LITELLM_BASE_URL or llm.<ESTATE_ZONE>;
    # with neither, the shunt has no worker and passes the read through.
    base = (cfg.get("router_base_url") or os.environ.get("LITELLM_BASE_URL", "")).rstrip("/")
    if not base and os.environ.get("ESTATE_ZONE"):
        base = f"https://llm.{os.environ['ESTATE_ZONE']}/v1"
    if not base.startswith("https://"):
        raise ValueError(f"no https LiteLLM base: router_base_url={base!r}")
    return base


def _ask(model: str, path: str, lines: int, body: str, cfg: dict) -> tuple[str, dict]:
    req = urllib.request.Request(  # noqa: S310 scheme pinned to https in _base()
        _base(cfg) + "/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "user", "content": PROMPT.format(path=path, lines=lines, body=body)}
                ],
                "max_tokens": cfg["max_tokens"],
                "temperature": 0,
                # MiniMax-M3 spends the whole budget on reasoning_content without this.
                "reasoning_effort": "none",
            }
        ).encode(),
        headers={"Authorization": f"Bearer {_key()}", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=cfg["timeout_seconds"]) as r:  # noqa: S310
        d = json.load(r)
    digest = (d["choices"][0]["message"].get("content") or "").strip()
    if not digest:
        raise EmptyDigest(f"{model} answered 200 with an empty digest")
    return digest, d.get("usage") or {}


def _fallbacks(cfg: dict) -> list[str]:
    roster = estate_roster() or ["fast", "minimax_m27", "gemini", "minimax"]
    model = cfg["worker_model"] or roster[0]
    explicit = [m.strip() for m in cfg["worker_fallbacks"].split(",") if m.strip()]
    rest = explicit if explicit else roster
    return model, [m for m in rest if m != model]


def _worker(path: str, lines: int, body: str, cfg: dict) -> tuple[str, dict, str]:
    model, fallbacks = _fallbacks(cfg)
    last: Exception | None = None
    for m in [model, *fallbacks]:
        try:
            digest, usage = _ask(m, path, lines, body, cfg)
            return digest, usage, m
        except (urllib.error.HTTPError, EmptyDigest) as e:
            last = e
            if getattr(e, "code", None) == 401:
                raise  # the key is wrong for every alias; no point asking the next one
            # 403 falls through: LiteLLM answers 403 when this key may not use that alias.
            print(f"read-shunt: {m} answered {getattr(e, 'code', 'empty')}; trying the next worker", file=os.sys.stderr)
    raise last if last else RuntimeError("no worker model configured")


def target_file(payload: dict) -> str | None:
    """The file a PreToolUse payload should shunt, or None. Shared by every agent shim."""
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    if tool == "Read":
        if ti.get("offset") or ti.get("limit") or ti.get("pages"):
            return None
        # Claude Code uses `file_path`; pi uses `path`. Same shunt, both agent shims.
        return ti.get("file_path") or ti.get("path")
    if tool == "Bash":
        m = CAT_RE.match(ti.get("command", "") or "")
        if not m:
            return None
        path = os.path.expanduser(os.path.expandvars(m.group(2)))
        if not os.path.isabs(path):
            path = os.path.join(payload.get("cwd") or os.getcwd(), path)
        return path
    return None


def _read_body(path: str, cfg: dict) -> tuple[str, int, bool, int] | None:
    """Read the file for shunting. Returns (body, lines, truncated, raw_len) or None to pass."""
    try:
        with open(path, "rb") as f:
            raw = f.read(cfg["max_chars"] + 1)
    except OSError:
        return None
    if b"\0" in raw[:8000]:
        return None  # binary file: never digest it
    body = raw[: cfg["max_chars"]].decode("utf-8", "replace")
    truncated = len(raw) > cfg["max_chars"]
    lines = body.count("\n") + (0 if body.endswith("\n") else 1)
    return body, lines, truncated, len(raw)


def should_shunt(path: str, offset=None, limit=None, cfg: dict | None = None) -> bool:
    """True when a read of `path` should be offloaded. Ranged reads always pass."""
    if offset or limit:
        return False
    if not path or not os.path.isfile(path):
        return False
    cfg = cfg or {}
    got = _read_body(path, cfg)
    if got is None:
        return False
    _body, lines, truncated, _raw_len = got
    threshold = int(cfg.get("threshold_lines") or 350)
    return lines > threshold or truncated


def digest(path: str, cfg: dict | None = None) -> dict:
    """Force a digest of `path` regardless of threshold. Returns full metadata."""
    cfg = cfg or {}
    got = _read_body(path, cfg)
    if got is None:
        raise ValueError(f"unreadable or binary file: {path}")
    body, lines, truncated, raw_len = got
    if truncated:
        body += f"\n[... truncated at {cfg['max_chars']} chars; the rest was not sent ...]"
    text, usage, model = _worker(path, lines, body, cfg)
    return {
        "digest": text,
        "usage": usage,
        "model": model,
        "lines": lines,
        "file_chars": min(raw_len, int(cfg.get("max_chars") or 400000)),
        "digest_chars": len(text),
    }


def shunt_file(path: str, cfg: dict, session_id: str = "") -> dict:
    """Full hook flow for one file: threshold check -> worker -> ledger. Fail-open."""
    t0 = time.time()
    row = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "file": path,
        "session": session_id,
        "model": cfg["worker_model"],
    }
    try:
        if not should_shunt(path, cfg=cfg):
            return {"outcome": "passed-through", "under_threshold": True}
        d = digest(path, cfg)
        row.update(
            outcome="shunted",
            model=d["model"],
            lines=d["lines"],
            file_chars=d["file_chars"],
            digest_chars=d["digest_chars"],
            worker_prompt_tokens=d["usage"].get("prompt_tokens", 0),
            worker_completion_tokens=d["usage"].get("completion_tokens", 0),
            est_frontier_tokens_avoided=max(0, (d["file_chars"] - d["digest_chars"]) // 4),
            ms=int((time.time() - t0) * 1000),
        )
        ledger_write(row, cfg)
        reason = (
            f"read-shunt: {path} is {d['lines']} lines (over {cfg['threshold_lines']}); the full read "
            f"went to the worker model `{d['model']}` and this digest came back instead. For exact text "
            f"call Read with offset and limit (a ranged read is never shunted); the READ EXACTLY section "
            f"names the ranges worth it.\n\n{d['digest']}"
        )
        return {"outcome": "shunted", "digest": d["digest"], "reason": reason, "model": d["model"]}
    except Exception as e:  # fail open, record why
        row.update(
            outcome="passed-through",
            error=f"{type(e).__name__}: {e}"[:200],
            ms=int((time.time() - t0) * 1000),
        )
        ledger_write(row, cfg)
        return {"outcome": "passed-through", "error": str(e)[:200]}

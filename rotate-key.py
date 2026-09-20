#!/usr/bin/env python3
"""Replace one API key everywhere it is stored, without it ever touching a log.

Written 2026-08-23, after three live keys were found in ~/.zsh_history and
~/.claude/history.jsonl. secret-scrub.py removed the copies. It cannot make an
exposed key invalid, so each one still has to be reissued at its provider. This is
the second half of that: the paste.

The reason it exists rather than an instruction to edit the file. Every obvious way
of installing a new key writes it somewhere permanent:

    export K=sk-...            -> ~/.zsh_history
    sed -i s/old/new/ file     -> ~/.zsh_history, and the old value too
    rotate-key K sk-...        -> argv, visible in ps, and history again

So the value is never an argument. It is read from a terminal prompt with echo off,
held in memory, checked against the provider, and written straight to the file that
already defines it.

    rotate-key.py                      list the keys it knows, and whether each is live
    rotate-key.py OPENROUTER_API_KEY   prompt for the new value, verify it, install it

It refuses to install a key the provider will not accept, because a rotation that
breaks every tool gets rolled back, and a rolled-back rotation leaves the leaked key live.
"""
from __future__ import annotations

import getpass
import os
import pathlib
import re
import subprocess
import sys
import tempfile

HOME = pathlib.Path.home()
STORES = [HOME / ".config/llm/secrets.sh", HOME / ".config/wave/secrets.sh"]

# name -> (url, header template). A key the provider accepts returns 200.
PROBES: dict[str, tuple[str, str]] = {
    "ANTHROPIC_API_KEY":  ("https://api.anthropic.com/v1/models",
                           "x-api-key: {v}\nanthropic-version: 2023-06-01"),
    "OPENROUTER_API_KEY": ("https://openrouter.ai/api/v1/auth/key", "Authorization: Bearer {v}"),
    "CEREBRAS_API_KEY":   ("https://api.cerebras.ai/v1/models", "Authorization: Bearer {v}"),
    "DEEPSEEK_API_KEY":   ("https://api.deepseek.com/models", "Authorization: Bearer {v}"),
    "MISTRAL_API_KEY":    ("https://api.mistral.ai/v1/models", "Authorization: Bearer {v}"),
    "HF_TOKEN":           ("https://huggingface.co/api/whoami-v2", "Authorization: Bearer {v}"),
}

CONSOLES = {
    "ANTHROPIC_API_KEY":  "https://console.anthropic.com/settings/keys",
    "OPENROUTER_API_KEY": "https://openrouter.ai/settings/keys",
    "CEREBRAS_API_KEY":   "https://cloud.cerebras.ai/platform/apikeys",
    "DEEPSEEK_API_KEY":   "https://platform.deepseek.com/api_keys",
    "MISTRAL_API_KEY":    "https://console.mistral.ai/api-keys",
    "GEMINI_API_KEY":     "https://aistudio.google.com/apikey",
    "MINIMAX_API_KEY":    "https://www.minimax.io/platform",
    "EXA_API_KEY":        "https://dashboard.exa.ai/api-keys",
    "HF_TOKEN":           "https://huggingface.co/settings/tokens",
}


def line_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"^(\s*(?:export\s+)?{re.escape(name)}=)(.*)$", re.M)


def stores_defining(name: str) -> list[pathlib.Path]:
    rx = line_re(name)
    return [p for p in STORES if p.exists() and rx.search(p.read_text())]


def probe(name: str, value: str) -> tuple[bool, str]:
    """Ask the provider. Returns (accepted, detail). The value is passed on stdin, not argv."""
    spec = PROBES.get(name)
    if not spec:
        return True, "no probe for this provider, installing unverified"
    url, hdr = spec
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "15", url]
    for h in hdr.format(v=value).split("\n"):
        cmd += ["-H", h]
    try:
        code = subprocess.run(cmd, capture_output=True, text=True, timeout=25).stdout.strip()
    except Exception as exc:                                        # noqa: BLE001
        return False, f"probe failed: {type(exc).__name__}"
    return code == "200", f"HTTP {code}"


def distinct_current(name: str, paths: list[pathlib.Path]) -> int:
    """Two stores can hold two DIFFERENT keys under one name. Collapsing them silently
    would revoke one provider account's key by overwriting it with another's."""
    rx = line_re(name)
    vals = set()
    for p in paths:
        m = rx.search(p.read_text())
        if m:
            vals.add(m.group(2).strip().strip('"').strip("'"))
    return len(vals)


def install(name: str, value: str, paths: list[pathlib.Path]) -> None:
    rx = line_re(name)
    for p in paths:
        text = p.read_text()
        new = rx.sub(lambda m: m.group(1) + value, text)
        if new == text:
            print(f"  no change in {p}")
            continue
        mode = p.stat().st_mode & 0o777
        fd, tmp = tempfile.mkstemp(dir=str(p.parent))
        os.close(fd)
        pathlib.Path(tmp).write_text(new)
        os.chmod(tmp, mode)
        os.replace(tmp, p)
        print(f"  installed into {str(p).replace(str(HOME), '~')} (mode {oct(mode)})")


def list_keys() -> int:
    names: dict[str, list[pathlib.Path]] = {}
    for p in STORES:
        if not p.exists():
            continue
        for m in re.finditer(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*_(?:API_)?KEY|HF_TOKEN)=", p.read_text(), re.M):
            names.setdefault(m.group(1), []).append(p)
    print(f"{'KEY':<22} {'LIVE':<12} WHERE IT IS STORED")
    for name, paths in sorted(names.items()):
        vals = []
        for p in paths:
            m = line_re(name).search(p.read_text())
            if m:
                vals.append(m.group(2).strip().strip('"').strip("'"))
        live = "-"
        if name in PROBES and vals:
            ok, detail = probe(name, vals[0])
            live = "yes" if ok else detail
        where = ", ".join(str(p).replace(str(HOME), "~") for p in paths)
        print(f"{name:<22} {live:<12} {where}")
    print()
    print("rotate-key.py <KEY_NAME>   to replace one. The value is never typed as an argument.")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        return list_keys()
    name = sys.argv[1]
    paths = stores_defining(name)
    if not paths:
        print(f"rotate-key: nothing defines {name} in {[str(p) for p in STORES]}", file=sys.stderr)
        return 2

    console = CONSOLES.get(name)
    print(f"Rotating {name}")
    print(f"  stored in: {', '.join(str(p).replace(str(HOME), '~') for p in paths)}")
    if console:
        print(f"  1. Revoke the old key and create a new one:  {console}")
    print("  2. Paste the new value below. It will not be shown, and it never")
    print("     reaches your shell history, argv, or any log.")
    print()

    try:
        value = getpass.getpass("new value: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nnothing changed")
        return 1
    if not value:
        print("empty, nothing changed")
        return 1

    if distinct_current(name, paths) > 1:
        print(f"  WARNING: the stores hold {distinct_current(name, paths)} DIFFERENT values for")
        print(f"           {name}. Installing one value replaces both.")
        if input("           type yes to continue: ").strip() != "yes":
            print("  nothing changed")
            return 1

    ok, detail = probe(name, value)
    print(f"  provider says: {detail}")
    if not ok:
        print("  refusing to install a key the provider does not accept. Nothing changed.")
        return 1

    install(name, value, paths)
    del value

    scrub = pathlib.Path(os.environ.get("GUARDS_ROOT") or pathlib.Path(__file__).resolve().parent) / "secret-scrub.py"
    if scrub.exists():
        subprocess.run([sys.executable, str(scrub)], check=False)
    print()
    print(f"{name} rotated. Open a new shell, or run:  source {paths[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Neutral configuration for read-shunt.

Layering (later wins): built-in defaults < ~/.config/read-shunt/config.yaml < env vars.
The worker roster is NOT owned here: it is read from idp/llm/config.yaml, the estate's one
shared router config (LAW 43: onboard the existing router, do not keep a private roster
that drifts from it). Env vars override per LAW 46.

Config file format: flat `key: value` YAML (comments allowed). No nesting needed.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

HOME = Path.home()

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("READ_SHUNT_CONFIG") or HOME / ".config" / "read-shunt" / "config.yaml"
)
DEFAULT_LEDGER_PATH = Path(
    os.environ.get("READ_SHUNT_LEDGER") or HOME / ".read-shunt" / "ledger.jsonl"
)

DEFAULTS: dict = {
    "enabled": True,
    "threshold_lines": 350,
    "max_chars": 400000,
    "timeout_seconds": 60.0,
    "max_tokens": 1500,
    "worker_model": "",
    "worker_fallbacks": "",
    "router_base_url": "",
    "ledger_path": str(DEFAULT_LEDGER_PATH),
}

# config key -> env var that overrides it
_ENV_KEYS = {
    "threshold_lines": "READ_SHUNT_LINES",
    "max_chars": "READ_SHUNT_MAX_CHARS",
    "timeout_seconds": "READ_SHUNT_TIMEOUT",
    "max_tokens": "READ_SHUNT_MAX_TOKENS",
    "worker_model": "READ_SHUNT_MODEL",
    "worker_fallbacks": "READ_SHUNT_FALLBACK",
    "router_base_url": "LITELLM_BASE_URL",
    "ledger_path": "READ_SHUNT_LEDGER",
}


def _idp_dir() -> str | None:
    """The idp checkout on this machine (LAW 46: env first, never only a literal)."""
    cands = [os.environ["IDP_DIR"]] if os.environ.get("IDP_DIR") else []
    cands += [str(HOME / "dev" / "code" / "idp"), str(HOME / "Documents" / "code" / "idp")]
    for c in cands:
        if os.path.exists(os.path.join(c, "llm", "config.yaml")):
            return c
    return None


def estate_roster() -> list[str]:
    """Worker aliases in the estate router's own file order."""
    idp = _idp_dir()
    if not idp:
        return []
    try:
        text = open(os.path.join(idp, "llm", "config.yaml"), encoding="utf-8").read()
    except OSError:
        return []
    return re.findall(r"^\s*-\s*model_name:\s*(\S+)\s*$", text, re.MULTILINE)


def _parse_flat_yaml(path: Path) -> dict:
    """Parse the flat `key: value` subset of YAML this config uses. No dependency, no nesting."""
    out: dict = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("---", "- ")):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce(cfg: dict) -> dict:
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULTS and v not in (None, "")})
    if "enabled" in merged:
        merged["enabled"] = _as_bool(merged["enabled"])
    for k in ("threshold_lines", "max_chars", "max_tokens"):
        v = _as_int(merged.get(k))
        if v is not None:
            merged[k] = v
    v = _as_float(merged.get("timeout_seconds"))
    if v is not None:
        merged["timeout_seconds"] = v
    for k in ("worker_model", "worker_fallbacks", "router_base_url", "ledger_path"):
        merged[k] = str(merged.get(k) or "")
    return merged


def load_config() -> dict:
    """Defaults <- config.yaml <- env vars. Returns a fully resolved config dict."""
    cfg = _parse_flat_yaml(DEFAULT_CONFIG_PATH)
    for key, env in _ENV_KEYS.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return _coerce(cfg)


def is_off(cfg: dict) -> bool:
    """Respect both the config `enabled` flag and the READ_SHUNT off switch."""
    if not cfg.get("enabled"):
        return True
    return os.environ.get("READ_SHUNT", "on").strip().lower() in ("off", "0", "false")

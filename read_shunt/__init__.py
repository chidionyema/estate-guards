"""read-shunt core: offload bulk file reads to a cheap worker model via the estate's LiteLLM router.

Model-agnostic by construction: the worker alias and fallback roster are read from
idp/llm/config.yaml (the estate's single router config), so adding a provider is a
LiteLLM change, never a read-shunt change. Provider credentials live in LiteLLM, not here.

Agent adapters (Claude Code PreToolUse hook, pi extension, MCP tool) are thin shims over
this package and share the same config and ledger.
"""
from .config import DEFAULT_CONFIG_PATH, DEFAULT_LEDGER_PATH, load_config
from .core import CAT_RE, EmptyDigest, digest, should_shunt, shunt_file, target_file
from .ledger import ledger_write, report

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_LEDGER_PATH",
    "load_config",
    "should_shunt",
    "digest",
    "target_file",
    "shunt_file",
    "ledger_write",
    "report",
    "CAT_RE",
    "EmptyDigest",
]

"""Provider adapters for `workforce hire` (AGENT_ADOPTION D13).

Four providers, one seat shape: :class:`~workforce.adapters.base.SeatContext`
in, a non-interactive command + auth probe out. No adapter emits a
permission-bypass flag (``ProviderAdapter.command`` raises if one appears).
"""

from __future__ import annotations

from typing import Dict

from .base import AdapterError, ProviderAdapter, SeatContext, DEFAULT_ALLOWED_TOOLS
from .claude import ADAPTER as CLAUDE_ADAPTER
from .cursor import ADAPTER as CURSOR_ADAPTER
from .grok import ADAPTER as GROK_ADAPTER
from .codex import ADAPTER as CODEX_ADAPTER

ADAPTERS: Dict[str, ProviderAdapter] = {
    "claude": CLAUDE_ADAPTER,
    "cursor": CURSOR_ADAPTER,
    "grok": GROK_ADAPTER,
    "codex": CODEX_ADAPTER,
}

PROVIDERS = tuple(ADAPTERS.keys())


def get_adapter(provider: str) -> ProviderAdapter:
    key = (provider or "").strip().lower()
    try:
        return ADAPTERS[key]
    except KeyError:
        raise AdapterError(
            "unknown provider %r — choose one of %s" % (provider, ", ".join(PROVIDERS))
        )


__all__ = [
    "ADAPTERS", "PROVIDERS", "get_adapter",
    "AdapterError", "ProviderAdapter", "SeatContext", "DEFAULT_ALLOWED_TOOLS",
]

"""Shared shape for provider adapters (AGENT_ADOPTION D13).

Each adapter turns a :class:`SeatContext` into the provider's non-interactive
command and auth probe. No adapter may emit a permission-bypass flag —
``ProviderAdapter.command`` fails closed if one slips in, so a future edit to
an adapter that adds ``--dangerously-skip-permissions`` (or an equivalent)
breaks loudly instead of shipping.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Callable, FrozenSet, List, Sequence

# Exclusive WorkLane hand feed — the same five tools every generated seat's
# papers point at (STAFFING §2). Adapters may extend this per seat.
DEFAULT_ALLOWED_TOOLS: Sequence[str] = (
    "wl_show", "wl_ready", "wl_claim", "wl_comment", "wl_park",
)


class AdapterError(Exception):
    """Raised when an adapter would emit a forbidden flag or is misconfigured."""


@dataclass
class SeatContext:
    """Everything an adapter needs to build one seat's command + MCP wiring."""

    slug: str
    identity: str
    model: str
    repository: str
    remote: str
    project: str
    mcp_config_path: str
    worklane_python: str
    worklane_runtime_dir: str
    max_turns: int = 60
    allowed_tools: Sequence[str] = field(default_factory=lambda: tuple(DEFAULT_ALLOWED_TOOLS))

    @property
    def mcp_tool_names(self) -> List[str]:
        return list(self.allowed_tools)


@dataclass
class ProviderAdapter:
    """One provider's non-interactive shape: binary, command, auth probe."""

    provider: str
    binary_names: Sequence[str]
    bypass_flags: FrozenSet[str]
    build_command: Callable[[SeatContext, str], List[str]]
    build_auth_check: Callable[[str], List[str]]
    build_allow_list: Callable[[SeatContext], List[str]]
    doc: str

    def resolve_binary(self) -> str:
        for name in self.binary_names:
            found = shutil.which(name)
            if found:
                return found
        # Not installed on this host — still returns a name so runner.json /
        # tests can be generated; dispatch will fail its own auth_check first.
        return self.binary_names[0]

    def command(self, ctx: SeatContext) -> List[str]:
        binary = self.resolve_binary()
        cmd = self.build_command(ctx, binary)
        bad = self.bypass_flags & set(cmd)
        if bad:
            raise AdapterError(
                "%s adapter would emit forbidden bypass flag(s): %s — "
                "hire never generates a permission-bypass command"
                % (self.provider, ", ".join(sorted(bad)))
            )
        return cmd

    def auth_check(self) -> List[str]:
        return self.build_auth_check(self.resolve_binary())

    def allow_list(self, ctx: SeatContext) -> List[str]:
        """Tool names this seat may call — never empty, never a wildcard."""
        return self.build_allow_list(ctx)

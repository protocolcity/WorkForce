"""Grok Build CLI adapter (AGENT_ADOPTION D13)."""

from __future__ import annotations

from typing import List

from .base import ProviderAdapter, SeatContext

_BASE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"


def _build_command(ctx: SeatContext, binary: str) -> List[str]:
    allowed = _BASE_TOOLS + "," + ",".join(
        "mcp__worklane__%s" % name for name in ctx.mcp_tool_names
    )
    # --trust only dismisses the interactive "trust this workspace?" prompt
    # (required headless, same ruling as cursor) — grok will not load the
    # seat's project-scoped .grok/config.toml MCP server from an untrusted
    # folder otherwise.
    cmd = [
        binary, "--trust", "--single", "{prompt}",
        "--permission-mode", "dontAsk",
        "--tools", _BASE_TOOLS,
        "--allowedTools", allowed,
    ]
    if ctx.model:
        cmd += ["--model", ctx.model]
    cmd += ["--max-turns", str(ctx.max_turns), "--output-format", "json"]
    return cmd


def _auth_check(binary: str) -> List[str]:
    # `grok doctor` checks the runtime without starting a session — the
    # closest non-destructive probe grok ships (no `auth status` command).
    return [binary, "doctor"]


def _allow_list(ctx: SeatContext) -> List[str]:
    return list(ctx.mcp_tool_names)


ADAPTER = ProviderAdapter(
    provider="grok",
    binary_names=("grok",),
    # --always-approve and --permission-mode bypassPermissions both skip the
    # allow list the same way claude's --dangerously-skip-permissions does.
    bypass_flags=frozenset({"--always-approve", "bypassPermissions"}),
    build_command=_build_command,
    build_auth_check=_auth_check,
    build_allow_list=_allow_list,
    doc=(
        "Single-turn (--single) with --permission-mode dontAsk and an "
        "explicit --tools/--allowedTools allow list (--allow is the alias "
        "grok documents for --allowedTools); --always-approve is never "
        "emitted. --trust only skips the workspace-trust prompt. The "
        "worklane MCP server is not wired through a CLI flag — the "
        "generated seat plants a project-scoped .grok/config.toml the "
        "grok CLI reads from its working directory."
    ),
    default_model="grok-4.6",
)

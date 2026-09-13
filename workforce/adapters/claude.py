"""Claude CLI adapter (AGENT_ADOPTION D13)."""

from __future__ import annotations

from typing import List

from .base import ProviderAdapter, SeatContext

_BASE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"


def _build_command(ctx: SeatContext, binary: str) -> List[str]:
    allowed = _BASE_TOOLS + "," + ",".join(
        "mcp__worklane__%s" % name for name in ctx.mcp_tool_names
    )
    cmd = [
        binary, "-p",
        "--setting-sources", "",
        "--settings", '{"disableAllHooks":true}',
        "--strict-mcp-config",
        "--mcp-config", ctx.mcp_config_path,
        "--permission-mode", "dontAsk",
        "--tools", _BASE_TOOLS,
        "--allowedTools", allowed,
    ]
    if ctx.model:
        cmd += ["--model", ctx.model]
    cmd += ["--max-turns", str(ctx.max_turns), "--output-format", "json", "{prompt}"]
    return cmd


def _auth_check(binary: str) -> List[str]:
    return [binary, "auth", "status"]


def _allow_list(ctx: SeatContext) -> List[str]:
    return list(ctx.mcp_tool_names)


ADAPTER = ProviderAdapter(
    provider="claude",
    binary_names=("claude",),
    # bypassPermissions is an alias value for --permission-mode that skips the
    # allow list the same way --dangerously-skip-permissions does.
    bypass_flags=frozenset({"--dangerously-skip-permissions", "bypassPermissions"}),
    build_command=_build_command,
    build_auth_check=_auth_check,
    build_allow_list=_allow_list,
    doc=(
        "Non-interactive (-p) with --permission-mode dontAsk and an explicit "
        "--tools/--allowedTools allow list scoped to the five WorkLane hand "
        "tools plus the project's file/bash tools; --strict-mcp-config pins "
        "the seat to its own mcp.json only."
    ),
)

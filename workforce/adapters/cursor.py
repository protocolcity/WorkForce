"""Cursor CLI (cursor-agent) adapter (AGENT_ADOPTION D13)."""

from __future__ import annotations

from typing import List

from .base import ProviderAdapter, SeatContext


def _build_command(ctx: SeatContext, binary: str) -> List[str]:
    # --trust only dismisses the interactive "trust this workspace?" prompt
    # (required headless) — it is not a permission bypass. --sandbox enabled
    # is the safety boundary; --approve-mcps auto-approves this seat's own
    # mcp.json, not arbitrary MCP servers. --force/--yolo (Run Everything)
    # are never emitted here.
    cmd = [
        binary, "--trust", "--sandbox", "enabled",
        "--print", "--auto-review", "--approve-mcps",
    ]
    if ctx.model:
        cmd += ["--model", ctx.model]
    cmd += ["--add-dir", "{git_common_dir}", "--output-format", "json", "{prompt}"]
    return cmd


def _auth_check(binary: str) -> List[str]:
    return [binary, "status"]


def _allow_list(ctx: SeatContext) -> List[str]:
    # cursor-agent has no --allowedTools flag; the allow list is enforced by
    # which MCP server the seat's own mcp.json registers (worklane, wl_* only)
    # plus --sandbox enabled for the built-in file/shell tools.
    return list(ctx.mcp_tool_names)


ADAPTER = ProviderAdapter(
    provider="cursor",
    binary_names=("cursor-agent",),
    bypass_flags=frozenset({"--force", "--yolo"}),
    build_command=_build_command,
    build_auth_check=_auth_check,
    build_allow_list=_allow_list,
    doc=(
        "Non-interactive (--print) with --sandbox enabled as the safety "
        "boundary; --trust only skips the workspace-trust prompt and "
        "--approve-mcps only auto-approves the seat's own mcp.json (whose "
        "worklane server exposes only the five wl_* hand tools) — neither is "
        "a permission bypass. --force/--yolo (Run Everything) are never "
        "emitted."
    ),
    default_model="composer-2.5",
)

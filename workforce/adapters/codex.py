"""Codex CLI adapter (AGENT_ADOPTION D13)."""

from __future__ import annotations

from typing import List

from .base import ProviderAdapter, SeatContext


def _build_command(ctx: SeatContext, binary: str) -> List[str]:
    cmd = [
        binary, "exec",
        "--ignore-user-config", "--json",
        "--sandbox", "workspace-write",
    ]
    if ctx.model:
        cmd += ["-c", 'model="%s"' % ctx.model]
    cmd += [
        "-c", "sandbox_workspace_write.network_access=true",
        "--add-dir", "{git_common_dir}",
        "-c", 'mcp_servers.worklane.command="%s"' % ctx.worklane_python,
        "-c", (
            'mcp_servers.worklane.args=["-m","worklane.mcp","--author","%s"]'
            % ctx.identity
        ),
        "-c", (
            'mcp_servers.worklane.env={WL_AGENT_ID="%s",TP_AGENT_ID="%s",'
            'WORKLANE_COMMENT_TRANSITIONS="0",WORKLANE_RUNTIME_DIR="%s"}'
            % (ctx.identity, ctx.identity, ctx.worklane_runtime_dir)
        ),
        "-c", (
            "mcp_servers.worklane.enabled_tools=[%s]"
            % ",".join('"%s"' % name for name in ctx.mcp_tool_names)
        ),
    ]
    for name in ctx.mcp_tool_names:
        cmd += [
            "-c",
            'mcp_servers.worklane.tools.%s.approval_mode="approve"' % name,
        ]
    cmd += ["-o", "{result}", "{prompt}"]
    return cmd


def _auth_check(binary: str) -> List[str]:
    return [binary, "login", "status"]


def _allow_list(ctx: SeatContext) -> List[str]:
    return list(ctx.mcp_tool_names)


ADAPTER = ProviderAdapter(
    provider="codex",
    binary_names=("codex",),
    bypass_flags=frozenset({"--dangerously-bypass-approvals-and-sandbox"}),
    build_command=_build_command,
    build_auth_check=_auth_check,
    build_allow_list=_allow_list,
    doc=(
        "Non-interactive (`codex exec`) with --sandbox workspace-write as "
        "the safety boundary and an explicit "
        "mcp_servers.worklane.enabled_tools allow list scoped to the five "
        "WorkLane hand tools; --dangerously-bypass-approvals-and-sandbox is "
        "never emitted."
    ),
)

"""Grok Build CLI adapter (AGENT_ADOPTION D13)."""

from __future__ import annotations

from typing import List

from .base import ProviderAdapter, SeatContext

_BASE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"

# grok's own built-in "Dangerous Commands" bucket (rm, chmod, chown, chgrp,
# chattr, pkill, kill, killall, git push) always requires either an explicit
# matching --allow rule or interactive confirmation, on top of ordinary tool
# grants — see grok's permissions reference. Under --permission-mode dontAsk
# a command needing that confirmation is denied instantly instead of
# prompted, and observed evidence (wf-262: grok-stop-diagnostics.json) shows
# that instant denial tears down the *whole* turn (stopReason=cancelled) even
# for ordinary, harmless commands that were never in that bucket — dontAsk's
# fail-closed default apparently also denies any run_terminal_command whose
# segments cannot all be matched by our bare tool-name grant, not just the
# genuinely dangerous ones. --permission-mode auto instead lets the turn
# continue and reports a blocked call back to the model as text (grok's own
# documented behaviour for non-interactive sessions), so a seat that hits a
# denial can still finish its work. These --deny rules keep the dangerous
# bucket hard-blocked regardless of mode — deny always wins over allow, the
# classifier, and always-approve — so relaxing dontAsk->auto does not loosen
# the actual safety boundary, only the false-positive cancellation of
# harmless commands.
_DENY_RULES = (
    "Bash(rm *)", "Bash(chmod *)", "Bash(chown *)", "Bash(chgrp *)",
    "Bash(chattr *)", "Bash(pkill *)", "Bash(kill *)", "Bash(killall *)",
    "Bash(git push*)", "Bash(sudo *)",
)


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
        "--permission-mode", "auto",
        "--tools", _BASE_TOOLS,
        "--allowedTools", allowed,
    ]
    for rule in _DENY_RULES:
        cmd += ["--deny", rule]
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
        "Single-turn (--single) with --permission-mode auto (wf-262: dontAsk "
        "denies the whole turn — stopReason=cancelled — on any "
        "run_terminal_command whose segments a bare tool-name grant can't "
        "match, not only genuinely dangerous ones; auto instead reports a "
        "blocked call back to the model so the turn can still finish) and "
        "an explicit --tools/--allowedTools allow list (--allow is the "
        "alias grok documents for --allowedTools); --always-approve is "
        "never emitted. Explicit --deny rules hard-block grok's own "
        "built-in dangerous-command bucket (rm, chmod/chown/chgrp/chattr, "
        "pkill/kill/killall, git push, sudo) regardless of mode, since deny "
        "always wins over allow/classifier/always-approve — this keeps the "
        "deny boundary intact even though auto's classifier alone would "
        "otherwise let them through unconfirmed in a headless session. "
        "--trust only skips the workspace-trust prompt. The worklane MCP "
        "server is not wired through a CLI flag — the generated seat "
        "plants a project-scoped .grok/config.toml the grok CLI reads from "
        "its working directory."
    ),
    default_model="grok-4.6",
)

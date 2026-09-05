"""Route an MCP tool name onto WFHandlers."""
from __future__ import annotations

from typing import Any, Dict

from .errors import ToolError
from .wf import WFHandlers


def dispatch_tool(handlers: WFHandlers, name: str, arguments: Dict[str, Any]) -> Any:
    args = arguments or {}
    if name == "wf_status":
        return handlers.status(args)
    if name == "wf_roster":
        return handlers.roster(args)
    if name == "wf_show":
        return handlers.show(args)
    if name == "wf_hire":
        return handlers.hire(args)
    if name == "wf_dispatch":
        return handlers.dispatch(args)
    raise ToolError("unknown tool: %s" % name)


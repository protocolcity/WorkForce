"""Tool handlers for WorkForce MCP.

Minimal surface: roster · show · hire · dispatch · status.
No silent destructive hires — hire requires explicit workdir + name.

Package peel of the former ``workforce/mcp/handlers.py`` monolith.
Existing imports stay stable: ``from workforce.mcp.handlers import
WFHandlers, ToolError, build_tool_definitions, dispatch_tool``.
"""

from __future__ import annotations

import json  # noqa: F401
import os  # noqa: F401
import types
import urllib.error  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401
import urllib  # noqa: F401
from typing import Any, Dict, List, Optional  # noqa: F401

from workforce import hire as hire_mod  # noqa: F401
from workforce import roster as roster_mod  # noqa: F401
from workforce._utils import engine_api_url  # noqa: F401
from workforce.ledger import Ledger  # noqa: F401

from .dispatch import dispatch_tool
from .errors import ToolError
from .http import _http_ok
from .paths import _load_roster_lenient, resolve_paths
from .tools import build_tool_definitions
from .wf import WFHandlers


def _adopt(fn):
    """Rebind *fn* so name lookups use this package (monkeypatch-stable)."""
    if not isinstance(fn, types.FunctionType):
        return fn
    adopted = types.FunctionType(
        fn.__code__,
        globals(),
        name=fn.__name__,
        argdefs=fn.__defaults__,
        closure=fn.__closure__,
    )
    adopted.__kwdefaults__ = fn.__kwdefaults__
    adopted.__doc__ = fn.__doc__
    adopted.__annotations__ = getattr(fn, "__annotations__", {})
    adopted.__module__ = __name__
    adopted.__qualname__ = fn.__qualname__
    return adopted


for _name in (
    "resolve_paths",
    "_load_roster_lenient",
    "build_tool_definitions",
    "dispatch_tool",
    "_http_ok",
):
    globals()[_name] = _adopt(globals()[_name])

for _name, _val in list(vars(WFHandlers).items()):
    if isinstance(_val, types.FunctionType):
        setattr(WFHandlers, _name, _adopt(_val))
WFHandlers.__module__ = __name__

del _name, _val, _adopt, types

__all__ = [
    "ToolError",
    "WFHandlers",
    "build_tool_definitions",
    "dispatch_tool",
    "resolve_paths",
    "_http_ok",
    "_load_roster_lenient",
]

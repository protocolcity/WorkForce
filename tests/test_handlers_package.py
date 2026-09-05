"""Lock the mcp/handlers.py → workforce.mcp.handlers package peel."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce.mcp import handlers as handlers_pkg
from workforce.mcp.handlers import (
    ToolError,
    WFHandlers,
    build_tool_definitions,
    dispatch_tool,
    errors,
    paths,
    tools,
    wf,
)


class HandlersPackagePeelTest(unittest.TestCase):
    def test_stable_import_surface(self) -> None:
        self.assertIs(handlers_pkg.ToolError, errors.ToolError)
        self.assertIs(handlers_pkg.WFHandlers, wf.WFHandlers)
        self.assertEqual(handlers_pkg.WFHandlers.__module__, "workforce.mcp.handlers")
        self.assertTrue(callable(handlers_pkg.build_tool_definitions))
        self.assertTrue(callable(handlers_pkg.dispatch_tool))
        self.assertTrue(callable(handlers_pkg.resolve_paths))

    def test_five_tool_catalog(self) -> None:
        names = {t["name"] for t in build_tool_definitions()}
        self.assertEqual(
            names,
            {"wf_status", "wf_roster", "wf_show", "wf_hire", "wf_dispatch"},
        )

    def test_boundaries(self) -> None:
        self.assertTrue(hasattr(wf, "WFHandlers"))
        self.assertTrue(hasattr(tools, "build_tool_definitions"))
        self.assertTrue(hasattr(paths, "resolve_paths"))
        self.assertFalse(hasattr(errors, "WFHandlers"))
        self.assertFalse(hasattr(paths, "dispatch_tool"))

    def test_monkeypatch_surface_is_package(self) -> None:
        self.assertIs(
            handlers_pkg.WFHandlers.status.__globals__.get("_http_ok"),
            handlers_pkg._http_ok,
        )
        self.assertIn("urllib", handlers_pkg.WFHandlers.dispatch.__globals__)

    def test_dispatch_unknown_tool(self) -> None:
        with self.assertRaises(ToolError):
            dispatch_tool(WFHandlers(), "wf_nope", {})


if __name__ == "__main__":
    unittest.main()

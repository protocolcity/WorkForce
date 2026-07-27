"""wf-86: WorkForce MCP handlers smoke."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workforce.mcp.handlers import (
    WFHandlers,
    build_tool_definitions,
    dispatch_tool,
    resolve_paths,
)
from workforce.mcp.server import MCPServer


class McpHandlersTests(unittest.TestCase):
    def test_tool_names(self) -> None:
        names = {t["name"] for t in build_tool_definitions()}
        self.assertEqual(
            names,
            {"wf_status", "wf_roster", "wf_show", "wf_hire", "wf_dispatch"},
        )

    def test_resolve_paths_from_roster(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local = root / "local"
            local.mkdir()
            roster = local / "roster.json"
            roster.write_text(
                json.dumps({"workers": {}}), encoding="utf-8"
            )
            p = resolve_paths(str(roster), None)
            self.assertEqual(
                Path(p["roster_path"]).resolve(), roster.resolve()
            )
            self.assertTrue(Path(p["local_root"]).name == "local")

    def test_status_empty_roster(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local = root / "local"
            local.mkdir()
            roster = local / "roster.json"
            roster.write_text(
                json.dumps({"workers": {}}), encoding="utf-8"
            )
            h = WFHandlers(author="test")
            out = dispatch_tool(
                h, "wf_status", {"roster": str(roster), "data_dir": str(root)}
            )
            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("workers"), 0)

    def test_initialize_roundtrip(self) -> None:
        import io

        h = WFHandlers(author="test")
        stdin = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {},
                }
            )
            + "\n"
        )
        stdout = io.StringIO()
        MCPServer(h, stdin=stdin, stdout=stdout).run()
        lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
        self.assertTrue(lines)
        msg = json.loads(lines[0])
        self.assertEqual(msg["id"], 1)
        self.assertIn("serverInfo", msg["result"])


if __name__ == "__main__":
    unittest.main()

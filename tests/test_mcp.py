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

    def test_wf_hire_workdir_description_says_project_folder(self) -> None:
        """wf-224 / pc-1380: MCP hire workdir help is project folder, not neighborhood."""
        tools = {t["name"]: t for t in build_tool_definitions()}
        desc = tools["wf_hire"]["inputSchema"]["properties"]["workdir"][
            "description"
        ].lower()
        self.assertIn("project folder", desc)
        self.assertNotIn("neighborhood", desc)
        self.assertNotIn("cabinet", desc)

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

    def test_status_engine_api_url_honors_workforce_port(self) -> None:
        """wf-221 — MCP must not hard-code :8797 when WORKFORCE_PORT is set."""
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local = root / "local"
            local.mkdir()
            roster = local / "roster.json"
            roster.write_text(json.dumps({"workers": {}}), encoding="utf-8")
            pinged = []

            def fake_ok(url):
                pinged.append(url)
                return False

            with patch.dict(os.environ, {"WORKFORCE_PORT": "9111"}):
                with patch(
                    "workforce.mcp.handlers._http_ok", side_effect=fake_ok
                ):
                    h = WFHandlers(author="test")
                    out = dispatch_tool(
                        h,
                        "wf_status",
                        {"roster": str(roster), "data_dir": str(root)},
                    )
            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("engine_api_url"), "http://127.0.0.1:9111/")
            self.assertEqual(pinged, ["http://127.0.0.1:9111/"])

    def test_dispatch_http_uses_engine_api_url(self) -> None:
        """wf-221 — via_http dispatch posts to WORKFORCE_PORT, not :8797."""
        import os
        from unittest.mock import patch

        class _Resp:
            def read(self):
                return b'{"ok": true, "msg": "dispatched"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            status = 200

        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.full_url)
            return _Resp()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local = root / "local"
            local.mkdir()
            roster = local / "roster.json"
            roster.write_text(json.dumps({"workers": {}}), encoding="utf-8")
            with patch.dict(os.environ, {"WORKFORCE_PORT": "9111"}):
                with patch(
                    "workforce.mcp.handlers.urllib.request.urlopen",
                    side_effect=fake_urlopen,
                ):
                    h = WFHandlers(author="test")
                    out = dispatch_tool(
                        h,
                        "wf_dispatch",
                        {
                            "name": "salem",
                            "roster": str(roster),
                            "data_dir": str(root),
                            "dry_run": False,
                            "via_http": True,
                        },
                    )
            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("via"), "http")
            self.assertEqual(
                captured, ["http://127.0.0.1:9111/api/dispatch/salem"]
            )

    def test_handlers_source_has_no_hardcoded_engine_port(self) -> None:
        import workforce.mcp.handlers as handlers_mod

        pkg = Path(handlers_mod.__file__).resolve().parent
        src = "\n".join(p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py")))
        self.assertNotIn("127.0.0.1:8797", src)

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

"""wf-86: WorkForce MCP handlers smoke."""

from __future__ import annotations

import json
import os
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
    def setUp(self) -> None:
        # Host environments (e.g. this very workspace) may export
        # WORKFORCE_DATA_DIR/WORKFORCE_ROSTER; isolate path-resolution
        # tests from that ambient state.
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in ("WORKFORCE_DATA_DIR", "WORKFORCE_ROSTER")
        }

    def tearDown(self) -> None:
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

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

    def test_resolve_paths_explicit_data_dir_wins_over_roster_location(
        self,
    ) -> None:
        """wf-248: explicit data_dir must own local_root even when the
        roster lives in an independent location."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            roster_root = root / "roster_store" / "local"
            roster_root.mkdir(parents=True)
            roster = roster_root / "roster.json"
            roster.write_text(json.dumps({"workers": {}}), encoding="utf-8")

            runtime_root = root / "runtime"
            runtime_root.mkdir()

            p = resolve_paths(str(roster), str(runtime_root))
            self.assertEqual(
                Path(p["roster_path"]).resolve(), roster.resolve()
            )
            self.assertEqual(
                Path(p["local_root"]).resolve(),
                (runtime_root / "local").resolve(),
            )

    def test_resolve_paths_env_data_dir_wins_over_roster_location(
        self,
    ) -> None:
        """wf-248: WORKFORCE_DATA_DIR takes the same precedence as the
        explicit data_dir argument."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            roster_root = root / "roster_store" / "local"
            roster_root.mkdir(parents=True)
            roster = roster_root / "roster.json"
            roster.write_text(json.dumps({"workers": {}}), encoding="utf-8")

            runtime_root = root / "runtime"
            runtime_root.mkdir()

            os.environ["WORKFORCE_DATA_DIR"] = str(runtime_root)
            p = resolve_paths(str(roster), None)

            self.assertEqual(
                Path(p["roster_path"]).resolve(), roster.resolve()
            )
            self.assertEqual(
                Path(p["local_root"]).resolve(),
                (runtime_root / "local").resolve(),
            )

    def test_resolve_paths_roster_only_still_uses_roster_local_root(
        self,
    ) -> None:
        """wf-248: legacy roster-only resolution is unaffected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local = root / "local"
            local.mkdir()
            roster = local / "roster.json"
            roster.write_text(json.dumps({"workers": {}}), encoding="utf-8")

            p = resolve_paths(str(roster), None)
            self.assertEqual(
                Path(p["local_root"]).resolve(), local.resolve()
            )
            self.assertEqual(
                Path(p["data_dir"]).resolve(), root.resolve()
            )

    def test_wf_show_reads_runtime_ledger_not_roster_ledger(self) -> None:
        """wf-248: wf_show against split runtime/roster must read the
        runtime ledger, not a historical ledger next to the roster."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            contract = root / "contract.md"
            contract.write_text("contract", encoding="utf-8")
            prompt = root / "prompt.md"
            prompt.write_text("prompt", encoding="utf-8")

            roster_root = root / "roster_store" / "local"
            roster_root.mkdir(parents=True)
            roster = roster_root / "roster.json"
            roster.write_text(
                json.dumps(
                    {
                        "workers": {
                            "alice": {
                                "kind": "lane",
                                "workdir": str(root),
                                "contract": str(contract),
                                "prompt": str(prompt),
                                "identity": "alice",
                                "command": ["true"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            stale_ledger_dir = roster_root / "ledger"
            stale_ledger_dir.mkdir()
            (stale_ledger_dir / "alice.log").write_text(
                "2020-01-01T00:00:00Z START order=stale\n", encoding="utf-8"
            )

            runtime_root = root / "runtime"
            fresh_ledger_dir = runtime_root / "local" / "ledger"
            fresh_ledger_dir.mkdir(parents=True)
            (fresh_ledger_dir / "alice.log").write_text(
                "2026-01-01T00:00:00Z START order=fresh\n", encoding="utf-8"
            )

            h = WFHandlers(author="test")
            out = dispatch_tool(
                h,
                "wf_show",
                {
                    "name": "alice",
                    "roster": str(roster),
                    "data_dir": str(runtime_root),
                },
            )
            self.assertTrue(out.get("ok"))
            tail_lines = [e.get("raw", "") for e in out.get("ledger_tail", [])]
            self.assertTrue(any("order=fresh" in line for line in tail_lines))
            self.assertFalse(any("order=stale" in line for line in tail_lines))

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

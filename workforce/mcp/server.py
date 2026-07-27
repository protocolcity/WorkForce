"""Stdio MCP server for WorkForce.

JSON-RPC 2.0 over stdin/stdout, protocol 2024-11-05. No external MCP SDK.

Run:
  WORKFORCE_ROSTER=~/city/.protocolcity/workforce/local/roster.json \\
    python -m workforce.mcp --author owner-terminal

  # or
  WORKFORCE_DATA_DIR=~/city/.protocolcity/workforce python -m workforce.mcp
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any, Dict, Optional, TextIO

from workforce.mcp.handlers import (
    ToolError,
    WFHandlers,
    build_tool_definitions,
    dispatch_tool,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "workforce"
SERVER_VERSION = "0.1.4"


class MCPServer:
    def __init__(
        self,
        handlers: WFHandlers,
        stdin: Optional[TextIO] = None,
        stdout: Optional[TextIO] = None,
    ) -> None:
        self.handlers = handlers
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._initialized = False
        self._tools = build_tool_definitions()

    def _write(self, message: Dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        self.stdout.write(line + "\n")
        self.stdout.flush()

    def _reply(self, req_id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: Any, code: int, message: str, data: Any = None) -> None:
        err: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._write({"jsonrpc": "2.0", "id": req_id, "error": err})

    def handle_message(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            self._initialized = True
            self._reply(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                    "instructions": (
                        "WorkForce MCP. Signed as author=%r. "
                        "Tools: wf_status, wf_roster, wf_show, wf_hire, wf_dispatch. "
                        "Set WORKFORCE_ROSTER or WORKFORCE_DATA_DIR to the city "
                        ".protocolcity/workforce path. Prefer dry_run on hire/dispatch "
                        "before real runs."
                        % (self.handlers.author,)
                    ),
                },
            )
            return

        if method == "notifications/initialized":
            return

        if method == "tools/list":
            self._reply(req_id, {"tools": self._tools})
            return

        if method == "tools/call":
            name = (params.get("name") or "").strip()
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = dispatch_tool(self.handlers, name, arguments)
                text = json.dumps(result, indent=2, default=str)
                self._reply(
                    req_id,
                    {
                        "content": [{"type": "text", "text": text}],
                        "isError": bool(
                            isinstance(result, dict) and result.get("ok") is False
                        ),
                    },
                )
            except ToolError as e:
                self._reply(
                    req_id,
                    {
                        "content": [{"type": "text", "text": "Error: %s" % e}],
                        "isError": True,
                    },
                )
            except Exception as e:
                self._reply(
                    req_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": "Error: %s\n%s" % (e, traceback.format_exc()),
                            }
                        ],
                        "isError": True,
                    },
                )
            return

        if method == "ping":
            self._reply(req_id, {})
            return

        if req_id is not None:
            self._error(req_id, -32601, "Method not found: %s" % method)

    def run(self) -> int:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self.handle_message(msg)
        return 0


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="workforce.mcp")
    p.add_argument(
        "--author",
        default=os.environ.get("WF_AGENT_ID")
        or os.environ.get("TP_AGENT_ID")
        or "mcp",
        help="identity for audit notes (default: WF_AGENT_ID / TP_AGENT_ID / mcp)",
    )
    args = p.parse_args(argv)
    handlers = WFHandlers(author=str(args.author))
    return MCPServer(handlers).run()


if __name__ == "__main__":
    raise SystemExit(main())

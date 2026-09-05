"""MCP tool catalog for WorkForce."""
from __future__ import annotations

from typing import Any, Dict, List


def build_tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": "wf_status",
            "description": (
                "WorkForce health: roster path, worker count, daemon.json if present, "
                "board URL reachability."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "roster": {"type": "string", "description": "path to roster.json"},
                    "data_dir": {
                        "type": "string",
                        "description": "WorkForce data dir (contains local/)",
                    },
                },
            },
        },
        {
            "name": "wf_roster",
            "description": (
                "List employed agents/jobs on the WorkForce roster "
                "(name, kind, workdir, schedule, model)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "roster": {"type": "string"},
                    "data_dir": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "description": "optional filter: lane or job",
                    },
                },
            },
        },
        {
            "name": "wf_show",
            "description": "Show one roster worker + recent ledger tail.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "worker slug"},
                    "roster": {"type": "string"},
                    "data_dir": {"type": "string"},
                    "ledger_n": {"type": "integer", "default": 8},
                },
                "required": ["name"],
            },
        },
        {
            "name": "wf_hire",
            "description": (
                "Employ an agent: plant CONTRACT/prompt papers + roster row. "
                "Requires name + workdir. Use dry_run to preview. "
                "kind=lane claims work orders; kind=job is scheduled duty."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "workdir": {
                        "type": "string",
                        "description": "absolute project folder path",
                    },
                    "role": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["lane", "job"],
                        "default": "lane",
                    },
                    "schedule": {"type": "string", "default": "*/30 * * * *"},
                    "model": {"type": "string"},
                    "project": {
                        "type": "string",
                        "description": "WorkLane store slug for ready queue",
                    },
                    "roster": {"type": "string"},
                    "data_dir": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": False},
                    "force_papers": {"type": "boolean", "default": False},
                },
                "required": ["name", "workdir"],
            },
        },
        {
            "name": "wf_dispatch",
            "description": (
                "Run one shift now (manual fire). Prefer dry_run=true first. "
                "Real dispatch spawns the agent CLI — confirm with human when unsure."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "worker slug"},
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "engine dry-run (no vendor CLI spawn)",
                    },
                    "roster": {"type": "string"},
                    "data_dir": {"type": "string"},
                    "via_http": {
                        "type": "boolean",
                        "default": True,
                        "description": "POST daemon board /api/dispatch when up",
                    },
                },
                "required": ["name"],
            },
        },
    ]


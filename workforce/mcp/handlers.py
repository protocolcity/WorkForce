"""Tool handlers for WorkForce MCP.

Minimal surface: roster · show · hire · dispatch · status.
No silent destructive hires — hire requires explicit workdir + name.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from workforce import hire as hire_mod
from workforce import roster as roster_mod
from workforce.ledger import Ledger


class ToolError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code


def resolve_paths(
    roster_path: Optional[str] = None,
    data_dir: Optional[str] = None,
) -> Dict[str, str]:
    """Resolve WORKFORCE_ROSTER / WORKFORCE_DATA_DIR / cwd defaults."""
    env_roster = (os.environ.get("WORKFORCE_ROSTER") or "").strip()
    env_data = (os.environ.get("WORKFORCE_DATA_DIR") or "").strip()
    data = (data_dir or env_data or "").strip()
    roster = (roster_path or env_roster or "").strip()

    if roster and not data:
        # roster.json lives in …/local/roster.json
        parent = os.path.dirname(os.path.realpath(roster))
        data = os.path.dirname(parent) if os.path.basename(parent) == "local" else parent
    if data and not roster:
        roster = os.path.join(data, "local", "roster.json")
    if not data:
        data = os.getcwd()
    if not roster:
        for cand in (
            os.path.join(data, "local", "roster.json"),
            os.path.join(data, "roster.json"),
            os.path.join(os.getcwd(), "local", "roster.json"),
        ):
            if os.path.isfile(cand):
                roster = cand
                break
        if not roster:
            roster = os.path.join(data, "local", "roster.json")
    local_root = os.path.dirname(os.path.realpath(roster))
    if os.path.basename(local_root) != "local":
        local_root = os.path.join(os.path.realpath(data), "local")
    return {
        "data_dir": os.path.realpath(data),
        "roster_path": os.path.realpath(roster),
        "local_root": os.path.realpath(local_root),
    }


def _load_roster_lenient(paths: Dict[str, str]):
    """Load roster; empty workers dict → empty Roster-like object."""
    path = paths["roster_path"]
    if not os.path.isfile(path):
        return None
    try:
        return roster_mod.load(path, base=paths["data_dir"])
    except roster_mod.RosterError as e:
        msg = str(e).lower()
        if "no workers" in msg:
            # Empty employment file is a valid first-user state
            class _Empty:
                workers: Dict[str, Any] = {}

            return _Empty()
        raise


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
                        "description": "absolute project/neighborhood path",
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


class WFHandlers:
    def __init__(self, *, author: str = "mcp"):
        self.author = author or "mcp"

    def status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        paths = resolve_paths(args.get("roster"), args.get("data_dir"))
        workers_n = 0
        kinds: Dict[str, int] = {}
        try:
            r = _load_roster_lenient(paths)
            workers_n = len(r.workers) if r else 0
            if r:
                for w in r.workers.values():
                    k = getattr(w, "kind", "lane") or "lane"
                    kinds[k] = kinds.get(k, 0) + 1
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "paths": paths,
                "author": self.author,
            }
        daemon_path = os.path.join(paths["local_root"], "daemon.json")
        daemon = None
        if os.path.isfile(daemon_path):
            try:
                with open(daemon_path, encoding="utf-8") as fh:
                    daemon = json.load(fh)
            except Exception:
                daemon = {"error": "unreadable"}
        board_up = _http_ok("http://127.0.0.1:8797/")
        return {
            "ok": True,
            "author": self.author,
            "paths": paths,
            "workers": workers_n,
            "kinds": kinds,
            "daemon": daemon,
            "engine_api_http": "up" if board_up else "down",
            "engine_api_url": "http://127.0.0.1:8797/",
        }

    def roster(self, args: Dict[str, Any]) -> Dict[str, Any]:
        paths = resolve_paths(args.get("roster"), args.get("data_dir"))
        try:
            r = _load_roster_lenient(paths)
        except Exception as e:
            raise ToolError("roster unreadable: %s" % e) from e
        if r is None:
            return {
                "ok": True,
                "count": 0,
                "roster_path": paths["roster_path"],
                "workers": [],
                "note": "empty or missing roster",
            }
        kind_f = (args.get("kind") or "").strip().lower()
        rows = []
        for name, w in sorted(r.workers.items()):
            k = getattr(w, "kind", "lane") or "lane"
            if kind_f and k != kind_f:
                continue
            rows.append(
                {
                    "name": name,
                    "kind": k,
                    "display": getattr(w, "display", "") or name,
                    "workdir": getattr(w, "workdir", ""),
                    "schedule": getattr(w, "schedule", ""),
                    "model": getattr(w, "model", ""),
                    "identity": getattr(w, "identity", "") or name,
                }
            )
        return {
            "ok": True,
            "count": len(rows),
            "roster_path": paths["roster_path"],
            "workers": rows,
        }

    def show(self, args: Dict[str, Any]) -> Dict[str, Any]:
        name = (args.get("name") or "").strip()
        if not name:
            raise ToolError("name required")
        paths = resolve_paths(args.get("roster"), args.get("data_dir"))
        try:
            r = _load_roster_lenient(paths)
            if r is None or name not in r.workers:
                raise ToolError("no such worker: %s" % name)
            w = r.worker(name) if hasattr(r, "worker") else r.workers[name]
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(str(e)) from e
        n = int(args.get("ledger_n") or 8)
        events = []
        try:
            led = Ledger(os.path.join(paths["local_root"], "ledger"), name)
            raw_tail = led.tail(max(n * 4, 40))
            # Ledger.tail returns a string of lines
            lines = str(raw_tail).splitlines() if raw_tail else []
            for line in lines[-n:]:
                events.append({"raw": line})
        except Exception as e:
            events = [{"error": str(e)}]
        return {
            "ok": True,
            "worker": {
                "name": w.name,
                "kind": w.kind,
                "display": w.display,
                "workdir": w.workdir,
                "schedule": w.schedule,
                "model": w.model,
                "identity": w.identity,
                "contract": w.contract,
                "prompt": w.prompt,
                "command": list(w.command or []),
            },
            "ledger_tail": events,
            "paths": paths,
        }

    def hire(self, args: Dict[str, Any]) -> Dict[str, Any]:
        name = (args.get("name") or "").strip()
        workdir = (args.get("workdir") or "").strip()
        if not name or not workdir:
            raise ToolError("name and workdir are required")
        if not os.path.isdir(workdir):
            raise ToolError("workdir does not exist: %s" % workdir)
        paths = resolve_paths(args.get("roster"), args.get("data_dir"))
        os.makedirs(os.path.dirname(paths["roster_path"]), exist_ok=True)
        dry = bool(args.get("dry_run"))
        staff_raw = args.get("staff", None)
        staff_arg = None if staff_raw is None else bool(staff_raw)
        try:
            result = hire_mod.hire(
                name=name,
                workdir=os.path.abspath(workdir),
                role=str(args.get("role") or ""),
                kind=str(args.get("kind") or "lane"),
                schedule=str(args.get("schedule") or "*/30 * * * *"),
                model=str(args.get("model") or ""),
                project=str(args.get("project") or ""),
                roster_path=paths["roster_path"],
                plant=True,
                force_papers=bool(args.get("force_papers")),
                dry_run=dry,
                base=paths["data_dir"],
                staff=staff_arg,
            )
        except hire_mod.RosterError as e:
            raise ToolError(str(e)) from e
        except Exception as e:
            raise ToolError("hire failed: %s" % e) from e
        result = dict(result or {})
        result["paths"] = paths
        result["author"] = self.author
        if dry:
            result.setdefault(
                "msg",
                "dry_run — roster not written; re-call with dry_run=false to arm",
            )
        return result

    def dispatch(self, args: Dict[str, Any]) -> Dict[str, Any]:
        name = (args.get("name") or "").strip()
        if not name:
            raise ToolError("name required")
        paths = resolve_paths(args.get("roster"), args.get("data_dir"))
        dry = bool(args.get("dry_run"))
        via_http = args.get("via_http", True)
        if via_http and not dry:
            # Prefer live daemon (same path as suite Dispatch button)
            url = "http://127.0.0.1:8797/api/dispatch/%s" % urllib.parse.quote(name)
            try:
                req = urllib.request.Request(url, data=b"", method="POST")
                req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, timeout=12) as r:
                    raw = r.read().decode("utf-8")
                try:
                    payload = json.loads(raw) if raw else {"ok": True, "msg": "dispatched"}
                except json.JSONDecodeError:
                    payload = {"ok": True, "msg": raw or "dispatched"}
                if not isinstance(payload, dict):
                    payload = {"ok": True, "msg": str(payload)}
                payload.setdefault("ok", True)
                payload["name"] = name
                payload["via"] = "http"
                payload["paths"] = paths
                return payload
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8")
                    payload = json.loads(body) if body else {"ok": False, "msg": e.reason}
                except Exception:
                    payload = {"ok": False, "msg": e.reason or str(e.code)}
                if isinstance(payload, dict):
                    payload["ok"] = False
                    payload["name"] = name
                    payload["via"] = "http"
                    return payload
            except Exception:
                # Fall through to in-process engine
                pass

        # In-process dispatch (daemon may be down)
        try:
            r = roster_mod.load(paths["roster_path"], base=paths["data_dir"])
            w = r.worker(name)
        except Exception as e:
            raise ToolError(str(e)) from e
        from workforce import engine

        try:
            rc = engine.dispatch(w, paths["local_root"], dry_run=dry)
        except Exception as e:
            raise ToolError("dispatch failed: %s" % e) from e
        return {
            "ok": rc == 0,
            "name": name,
            "rc": rc,
            "dry_run": dry,
            "via": "engine",
            "msg": "dispatched" if rc == 0 else "dispatch rc=%s" % rc,
            "paths": paths,
        }


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return 200 <= int(r.status) < 500
    except urllib.error.HTTPError as e:
        return 100 <= int(getattr(e, "code", 0) or 0) < 600
    except Exception:
        return False


# late import for quote in dispatch
import urllib.parse  # noqa: E402


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

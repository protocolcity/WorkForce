"""WFHandlers — roster · show · hire · dispatch · status."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from workforce import hire as hire_mod
from workforce import roster as roster_mod
from workforce._utils import engine_api_url
from workforce.ledger import Ledger

from .errors import ToolError
from .http import _http_ok
from .paths import _load_roster_lenient, resolve_paths


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
        api = engine_api_url().rstrip("/")
        board_up = _http_ok(api + "/")
        return {
            "ok": True,
            "author": self.author,
            "paths": paths,
            "workers": workers_n,
            "kinds": kinds,
            "daemon": daemon,
            "engine_api_http": "up" if board_up else "down",
            "engine_api_url": api + "/",
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
            url = "%s/api/dispatch/%s" % (
                engine_api_url().rstrip("/"),
                urllib.parse.quote(name),
            )
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


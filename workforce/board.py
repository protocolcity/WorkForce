"""The board — the workforce office, served on its own port.

Renders the JOIN the desk can't see alone: roster + shift ledgers on one
side, desk activity on the other. Three data sources, all seams:

  1. The roster + ledgers (this product's own state).
  2. ``launchctl list`` — TRANSITIONAL adapter for the legacy hand-rolled
     lanes; each row disappears as its lane migrates onto the daemon.
  3. The desk's published dev feed (activity + summary) — the desk half of
     the join, consumed over HTTP, never imported.

Own port (default 8797), own theme. Law lens: /law/<worker>/<contract|prompt>
renders the exact file the next shift will read — a lens, never a copy.
"""

import html
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from .daemon import heartbeat_status, read_heartbeat
from .ledger import Ledger, parse_shifts
from .roster import RosterError
from .schedule import maybe_cron

from .api.roster import (
    DEFAULT_PORT, DESK, CITYHALL, _BRAND_TITLE,
    generation_token, scene_model, scene_tape, report_model, worker_model,
    _load_roster, _worker_queue, _worker_health, _utcnow, _launchctl_rota,
    _cli_label, _kind_label, _worker_identity_aliases, _worker_holdings,
    _worker_ready_teaser, _worker_flags, _desk_json, _law_stack,
    _contract_rules, _git_law_log, _workplaces, _ago, _fmt_fire,
    _platforms, _display_names, _sector_for_worker, _city_folder_name,
    _legacy_plist, _service_config, _last_ledger_line, _queue_human_link,
    _desk_owner_of, OUTCOME_CLS, _IN_CITY, _KIND_LABELS, LAUNCH_AGENTS,
    _REPORT_WINDOW_DAYS, _REPORT_QUIET_HOURS, _REPORT_WINDOWS,
    _FAULT_OUTCOMES, RULE_HEADINGS, _WEDGE_SHIFTS,
)
from .surfaces.roster import (
    CSS, FIRE_JS, SCENE_CSS, SCENE_JS, REPORT_CSS, REPORT_JS,
    render_board, render_scene, render_settings, render_report,
    render_shifts, render_worker, render_out, render_legacy_log, render_law,
    _tail_page,
)

# ONE DOOR: this process is the WorkForce API (roster/scene/dispatch).
# Citizen UI lives on suite :8801/roster. Opt out for host debug:
# WORKFORCE_API_ONLY=0.
_API_ONLY_RAW = (os.environ.get("WORKFORCE_API_ONLY") or "1").strip().lower()
API_ONLY = _API_ONLY_RAW not in ("0", "false", "no", "off")
SUITE_URL = (os.environ.get("SUITE_URL") or "http://127.0.0.1:8801").rstrip("/")


def _out_path(local_root: str, name: str) -> str:
    return os.path.join(local_root, "run", "%s.out" % name)



def _safe_worker_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""))


def _days_param(path: str) -> Optional[int]:
    """?days= from a request path; None on absence or junk (model defaults)."""
    query = urllib.parse.urlsplit(path).query
    raw = urllib.parse.parse_qs(query).get("days", [""])[0]
    try:
        return int(raw)
    except ValueError:
        return None


class _Handler(BaseHTTPRequestHandler):
    local_root = "local"
    daemon = None  # set when the daemon serves the board in-process

    def _read_json_body(self) -> Dict[str, object]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _json_response(self, payload: Dict[str, object], code: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/dispatch/"):
            name = self.path.strip("/").split("/")[-1]
            if self.daemon is None:
                payload = {"ok": False, "msg": "board is read-only (no daemon in this process)"}
                code = 409
            else:
                ok, msg = self.daemon.fire_now(name)
                payload = {"ok": ok, "msg": msg}
                code = 200 if ok else 409
            self._json_response(payload, code)
            return
        if self.path == "/api/hire" or self.path.startswith("/api/hire?"):
            # STAFFING §2 — employment write path (papers + roster). Localhost
            # bind is the gate; daemon not required (roster reload is next tick).
            body = self._read_json_body()
            try:
                from . import hire as hire_mod
                # Board local_root is …/local; hire base is the package cwd parent.
                base = os.path.dirname(os.path.abspath(self.local_root)) or os.getcwd()
                result = hire_mod.hire(
                    name=str(body.get("name") or ""),
                    workdir=str(body.get("workdir") or ""),
                    display=str(body.get("display") or ""),
                    role=str(body.get("role") or ""),
                    kind=str(body.get("kind") or "lane"),
                    identity=str(body.get("identity") or ""),
                    schedule=str(body.get("schedule") or "*/30 * * * *"),
                    model=str(body.get("model") or ""),
                    queue_url=str(body.get("queue_url") or ""),
                    queue_count_key=str(body.get("queue_count_key") or "count"),
                    budget_secs=int(body.get("budget_secs") or 1500),
                    keychain_service=str(body.get("keychain_service")
                                         or "claude-cli-oauth"),
                    keychain_env=str(body.get("keychain_env")
                                     or "CLAUDE_CODE_OAUTH_TOKEN"),
                    env=body.get("env") if isinstance(body.get("env"), dict) else None,
                    plant=bool(body.get("plant_papers", True)),
                    force_papers=bool(body.get("force_papers", False)),
                    project=str(body.get("project") or ""),
                    base=base,
                    roster_path=os.path.join(self.local_root, "roster.json")
                    if os.path.isdir(self.local_root) else None,
                    dry_run=bool(body.get("dry_run", False)),
                )
                self._json_response(result, 200)
            except RosterError as exc:
                self._json_response({"ok": False, "msg": str(exc)}, 409)
            except (TypeError, ValueError) as exc:
                self._json_response({"ok": False, "msg": str(exc)}, 400)
            return
        self._json_response({"ok": False, "msg": "not found"}, 404)

    def do_GET(self) -> None:  # noqa: N802
        path_only = (self.path or "").split("?", 1)[0]
        # API-only: keep /api/*; retire HTML product pages → suite :8801.
        if API_ONLY and not path_only.startswith("/api/"):
            suite = SUITE_URL + "/roster"
            accept = (self.headers.get("Accept") or "").lower()
            if "text/html" in accept or "*/*" in accept or not accept:
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>WorkForce API</title>"
                    "<p>WorkForce HTML is retired — open the suite: "
                    "<a href='%s'>%s</a></p>"
                    "<p class='dim'>This port serves <code>/api/*</code> only "
                    "(API_ONLY). Set <code>WORKFORCE_API_ONLY=0</code> for "
                    "legacy board HTML.</p>"
                ) % (html.escape(suite), html.escape(suite))
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                payload = {
                    "ok": False,
                    "error": "workforce HTML retired (WORKFORCE_API_ONLY); "
                             "open suite at %s" % suite,
                    "api": "/api/scene",
                    "suite": suite,
                }
                self._json_response(payload, 404)
            return
        if self.path == "/" or self.path.startswith("/?"):
            # oc-20 root-merge: the living scene IS the room.
            body, code = render_scene(self.local_root,
                                      can_dispatch=self.daemon is not None), 200
        elif self.path == "/board" or self.path.startswith("/board?"):
            body, code = render_board(self.local_root, can_dispatch=self.daemon is not None), 200
        elif self.path == "/report" or self.path.startswith("/report?"):
            # oc-22: the floor's strategic view — the footer's Overview slot
            body, code = render_report(self.local_root,
                                       days=_days_param(self.path)), 200
        elif self.path == "/settings" or self.path.startswith("/settings?"):
            # D1 Settings bay — daemon / root / record doors (suite perimeter)
            body, code = render_settings(
                self.local_root, can_dispatch=self.daemon is not None), 200
        elif self.path == "/api/report" or self.path.startswith("/api/report?"):
            # one seam, three consumers: the /report page, the oc-15 daily
            # brief (future), and anything above this board
            data = json.dumps(report_model(
                self.local_root, days=_days_param(self.path))).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path == "/dispatch" or self.path.startswith("/dispatch?"):
            # legacy address from the pre-merge layout — the room moved to /
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        elif self.path.split("?")[0] == "/api/scene":
            # pc-346: ?light=1 skips ledger tails / launchctl / runtime detect
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            light = (q.get("light") or ["0"])[0].lower() in ("1", "true", "yes")
            try:
                from .api.roster import scene_model as _sm
                payload = _sm(self.local_root, light=light)
            except Exception:
                payload = scene_model(self.local_root)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if light:
                self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path == "/api/generation" or self.path == "/api/pulse":
            # LIVE-B2: tokens only for suite pulse bus
            data = json.dumps({"ok": True, **generation_token(self.local_root)}).encode(
                "utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return
        elif (self.path.startswith("/api/out/")
              and ("/stream" in self.path.split("?")[0])):
            # LIVE-C: SSE tail of shift .out while worker in_flight
            path_only = self.path.split("?")[0]
            # /api/out/<name>/stream
            parts = path_only.strip("/").split("/")
            name = urllib.parse.unquote(parts[2]) if len(parts) >= 4 else ""
            if not _safe_worker_name(name):
                self._json_response({"ok": False, "msg": "bad worker name"}, 400)
                return
            hb0 = read_heartbeat(self.local_root) or {}
            inflight = hb0.get("in_flight") or []
            if not isinstance(inflight, list):
                inflight = []
            on_shift = name in inflight
            out_path = _out_path(self.local_root, name)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def _sse(event: str, payload: Dict[str, object]) -> None:
                chunk = "event: %s\ndata: %s\n\n" % (
                    event, json.dumps(payload, ensure_ascii=False))
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()

            if not on_shift:
                _sse("idle", {
                    "ok": True, "worker": name, "in_flight": False,
                    "msg": "not on shift",
                })
                return
            if not os.path.isfile(out_path):
                _sse("waiting", {
                    "ok": True, "worker": name, "path": out_path,
                    "msg": "out file not yet created",
                })
            # Tail growing file; re-check in_flight each loop
            import time as _time
            pos = 0
            if os.path.isfile(out_path):
                try:
                    # Start near end (last 8 KiB) so reconnect isn't a full replay
                    size = os.path.getsize(out_path)
                    pos = max(0, size - 8192)
                except OSError:
                    pos = 0
            idle_ticks = 0
            try:
                while idle_ticks < 600:  # ~10 min max stream
                    hb = read_heartbeat(self.local_root) or {}
                    infl = hb.get("in_flight") or []
                    if name not in (infl if isinstance(infl, list) else []):
                        _sse("end", {
                            "ok": True, "worker": name, "reason": "shift ended",
                        })
                        break
                    try:
                        with open(out_path, "r", encoding="utf-8",
                                  errors="replace") as fh:
                            fh.seek(pos)
                            chunk = fh.read()
                            pos = fh.tell()
                    except OSError:
                        chunk = ""
                    if chunk:
                        _sse("chunk", {
                            "ok": True, "worker": name, "text": chunk,
                        })
                        idle_ticks = 0
                    else:
                        idle_ticks += 1
                        _sse("ping", {"ok": True, "worker": name})
                    _time.sleep(1.0)
                else:
                    _sse("end", {
                        "ok": True, "worker": name, "reason": "stream timeout",
                    })
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        elif self.path == "/api/scene-tape":
            # oc-19: the traffic tape's own endpoint — the scene polls it on a
            # separate cadence so scene_model stays network-free. Degrades on
            # its own (desk_ok:false) when the desk is unreachable.
            data = json.dumps(scene_tape(self.local_root)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/worker/"):
            # oc-21: the personnel-file read model behind the in-scene drawer
            parts = self.path.strip("/").split("/")
            model = (worker_model(self.local_root, urllib.parse.unquote(parts[2]))
                     if len(parts) == 3 else None)
            data = json.dumps(model if model is not None
                              else {"error": "no such worker"}).encode("utf-8")
            self.send_response(200 if model is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/law/"):
            parts = self.path.strip("/").split("/")
            body = render_law(self.local_root, parts[1], parts[2]) if len(parts) == 3 else None
            code = 200 if body else 404
            body = body or "<p>no such law</p>"
        elif self.path.startswith("/worker/"):
            parts = self.path.strip("/").split("/")
            body = render_worker(self.local_root, parts[1]) if len(parts) == 2 else None
            code = 200 if body else 404
            body = body or "<p>no such worker</p>"
        elif self.path.startswith("/shifts/"):
            parts = self.path.strip("/").split("/")
            body = render_shifts(self.local_root, parts[1]) if len(parts) == 2 else None
            code = 200 if body else 404
            body = body or "<p>no such worker</p>"
        elif self.path.startswith("/out/"):
            parts = self.path.strip("/").split("/")
            body = render_out(self.local_root, parts[1]) if len(parts) == 2 else None
            code = 200 if body else 404
            body = body or "<p>no such worker</p>"
        elif self.path.startswith("/legacylog/"):
            parts = self.path.strip("/").split("/")
            body = (render_legacy_log(_launchctl_rota(self.local_root), parts[1])
                    if len(parts) == 2 else None)
            code = 200 if body else 404
            body = body or "<p>no such legacy log</p>"
        elif self.path == "/api/workers":
            # the §8 machine-readable seam: roster × health × next fire,
            # for anything above this board (e.g. a city lens)
            roster = _load_roster(self.local_root)
            status = heartbeat_status(self.local_root)
            workers = []
            if roster:
                for name in sorted(roster.workers):
                    w = roster.workers[name]
                    q = _worker_queue(w)
                    cron = maybe_cron(w.schedule)
                    nf = cron.next_fire(_utcnow()) if cron else None
                    shifts = parse_shifts(
                        Ledger(os.path.join(self.local_root, "ledger"), name).tail(60), limit=3)
                    last = next((s for s in shifts if not s["dry_run"]), None)
                    workers.append({
                        "name": name, "kind": w.kind, "workdir": os.path.abspath(w.workdir),
                        "display": w.display or "", "succeeds": w.succeeds or "",
                        "identity": w.identity,
                        "cli": _cli_label(w), "model": w.model,
                        "schedule": w.schedule, "owned": bool(cron),
                        "owner": w.owner or "",
                        "skill": w.skill or "",
                        "next_fire": nf.strftime("%Y-%m-%dT%H:%M:%SZ") if nf else "",
                        "queue": q, "queue_url": w.queue_url or "",
                        "health": _worker_health(self.local_root, w, q)["cls"],
                        "last_shift": ({"ts": last["ts"], "outcome": last["outcome"],
                                        "passes": last["passes"], "reason": last["reason"]}
                                       if last else None),
                    })
            data = json.dumps({"daemon": status, "workers": workers}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path == "/api/health":
            payload = {"ok": True, "port": DEFAULT_PORT}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return
        else:
            body, code = "<p>not found</p>", 404
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # quiet; the ledger is the record that matters


def make_server(port: Optional[int] = None, local_root: str = "local",
                daemon: Optional[object] = None) -> ThreadingHTTPServer:
    if port is None:
        port = DEFAULT_PORT  # resolved at call time so tests/config can repoint
    _Handler.local_root = local_root
    _Handler.daemon = daemon  # None = read-only board (standalone)
    # ThreadingHTTPServer so LIVE-C SSE tails do not block /api/scene.
    return ThreadingHTTPServer(("127.0.0.1", port), _Handler)


def serve(port: Optional[int] = None, local_root: str = "local") -> None:
    httpd = make_server(port, local_root)
    print("%s: http://127.0.0.1:%d" % (_BRAND_TITLE, httpd.server_address[1]))
    httpd.serve_forever()

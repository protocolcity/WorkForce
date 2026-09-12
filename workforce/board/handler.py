"""HTTP handler for the WorkForce JSON API (roster/scene/dispatch)."""

import concurrent.futures
import html
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Dict

from ..daemon import adaptive_backoff_secs, heartbeat_status, read_heartbeat
from ..engine import empty_run_streak
from ..ledger import Ledger, parse_shifts
from ..roster import RosterError
from ..schedule import maybe_cron, next_fire_utc
from .._utils import _parse_iso_z, _utc_iso_z, _utcnow
from ..api.roster import (
    _cli_label,
    _load_roster,
    _worker_health,
    _worker_queue,
    generation_token,
    report_model,
    scene_model,
    scene_tape,
    worker_model,
)
from .paths import (
    _days_param,
    _map_roster_url,
    _out_path,
    _safe_worker_name,
)


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

    def _client_gone_write(self, data: bytes) -> None:
        """Write response body; swallow client disconnect.

        When /api/scene (or any slow GET) exceeds the client's patience, the
        peer closes mid-write → BrokenPipeError / ConnectionResetError. That is
        not a server fault and must not dump a full traceback into daemon.log
        (hundreds of lines during a desk-spin window). One-line context only.
        """
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError) as exc:
            path = (self.path or "").split("?", 1)[0]
            try:
                n = len(data)
            except Exception:
                n = -1
            # stdout joins daemon.log when the board is in-process
            print("board client-gone path=%s bytes=%s (%s)" % (
                path, n, type(exc).__name__), flush=True)

    def _json_response(self, payload: Dict[str, object], code: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self._client_gone_write(data)
        except (BrokenPipeError, ConnectionResetError) as exc:
            path = (self.path or "").split("?", 1)[0]
            print("board client-gone path=%s phase=headers (%s)" % (
                path, type(exc).__name__), flush=True)

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
        if self.path.startswith("/api/skip/"):
            # wf-94: skip next scheduled fire only (Map Approaching abandon)
            name = self.path.strip("/").split("/")[-1]
            if self.daemon is None:
                payload = {
                    "ok": False,
                    "msg": "board is read-only (no daemon in this process)",
                }
                code = 409
            else:
                ok, msg, skipped, nxt = self.daemon.skip_now(name)
                payload = {
                    "ok": ok,
                    "msg": msg,
                    "skipped_fire": skipped,
                    "next_fire": nxt,
                }
                code = 200 if ok else 409
            self._json_response(payload, code)
            return
        if self.path == "/api/wake" or self.path.startswith("/api/wake?"):
            # wf-149 wake-on-route: WorkLane nudges {"worker": <hand>} on route
            # events so a freshly seated ticket fires within seconds instead of
            # waiting for the lane's next clock. Fire-and-forget contract: the
            # caller never blocks a ticket write on this.
            body = self._read_json_body()
            name = str(body.get("worker") or "")
            if not _safe_worker_name(name):
                self._json_response({"ok": False, "msg": "bad worker name"}, 400)
                return
            if self.daemon is None:
                self._json_response(
                    {"ok": False,
                     "msg": "board is read-only (no daemon in this process)"}, 409)
                return
            ok, msg = self.daemon.wake_now(name)
            self._json_response({"ok": ok, "msg": msg}, 200 if ok else 409)
            return
        if self.path == "/api/hire" or self.path.startswith("/api/hire?"):
            # STAFFING §2 — employment write path (papers + roster). Localhost
            # bind is the gate; daemon not required (roster reload is next tick).
            body = self._read_json_body()
            try:
                from .. import hire as hire_mod
                # Board local_root is …/local; hire base is the package cwd parent.
                base = os.path.dirname(os.path.abspath(self.local_root)) or os.getcwd()
                # staff: omit → auto (city-ops workdir); explicit bool overrides
                staff_body = body.get("staff", None)
                staff_arg = None if staff_body is None else bool(staff_body)
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
                    staff=staff_arg,
                    worker_type=str(body.get("type") or ""),
                )
                self._json_response(result, 200)
            except RosterError as exc:
                self._json_response({"ok": False, "msg": str(exc)}, 409)
            except (TypeError, ValueError) as exc:
                self._json_response({"ok": False, "msg": str(exc)}, 400)
            return
        self._json_response({"ok": False, "msg": "not found"}, 404)

    def _reply_html_retired(self) -> None:
        """Non-/api/* is gone — one line pointing at Map."""
        suite = _map_roster_url()
        msg = "WorkForce HTML is retired — open the suite Map at %s" % suite
        accept = (self.headers.get("Accept") or "").lower()
        if "application/json" in accept and "text/html" not in accept:
            self._json_response({
                "ok": False,
                "error": msg,
                "api": "/api/scene",
                "suite": suite,
            }, 410)
            return
        body = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>WorkForce API</title>"
            "<p>%s — <a href='%s'>%s</a></p>"
        ) % (html.escape(msg), html.escape(suite), html.escape(suite))
        data = body.encode("utf-8")
        try:
            self.send_response(410)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._client_gone_write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        path_only = (self.path or "").split("?", 1)[0]
        if not path_only.startswith("/api/"):
            self._reply_html_retired()
            return
        if self.path == "/api/report" or self.path.startswith("/api/report?"):
            # one seam: suite Map, the oc-15 daily brief (future), and
            # anything above this board
            data = json.dumps(report_model(
                self.local_root, days=_days_param(self.path))).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.end_headers()
                self._client_gone_write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        elif self.path.split("?")[0] == "/api/scene":
            # pc-346: ?light=1 skips ledger tails / launchctl / runtime detect
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            light = (q.get("light") or ["0"])[0].lower() in ("1", "true", "yes")
            try:
                from ..api.roster import scene_model as _sm
                payload = _sm(self.local_root, light=light)
            except Exception:
                payload = scene_model(self.local_root)
            data = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if light:
                    self.send_header("Cache-Control", "no-store, max-age=0")
                self.end_headers()
                self._client_gone_write(data)
            except (BrokenPipeError, ConnectionResetError) as exc:
                print("board client-gone path=/api/scene phase=headers (%s)" % (
                    type(exc).__name__,), flush=True)
            return
        elif self.path == "/api/generation" or self.path == "/api/pulse":
            # LIVE-B2: tokens only for suite pulse bus
            data = json.dumps({"ok": True, **generation_token(self.local_root)}).encode(
                "utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.end_headers()
                self._client_gone_write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
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
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self._client_gone_write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        elif self.path.startswith("/api/worker/"):
            # oc-21: the personnel-file read model behind the in-scene drawer
            parts = self.path.strip("/").split("/")
            model = (worker_model(self.local_root, urllib.parse.unquote(parts[2]))
                     if len(parts) == 3 else None)
            data = json.dumps(model if model is not None
                              else {"error": "no such worker"}).encode("utf-8")
            try:
                self.send_response(200 if model is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self._client_gone_write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        elif self.path.split("?")[0] == "/api/workers":
            # the §8 machine-readable seam: roster × health × next fire,
            # for anything above this board (e.g. a city lens).
            # ?light=1: skip probes/health/shifts — for callers with <1s budgets
            # (e.g. WorkLane hired-hands lookup).  Non-light: parallel fan-out.
            _q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            light = (_q.get("light") or ["0"])[0].lower() in ("1", "true", "yes")
            roster = _load_roster(self.local_root)
            status = heartbeat_status(self.local_root)
            # wf-149: wake stamps floor the streak so a woken lane reads as
            # back at base cadence, matching the daemon's own gate.
            _hb = read_heartbeat(self.local_root) or {}
            wakes = _hb.get("wakes") if isinstance(_hb.get("wakes"), dict) else {}
            workers = []
            if roster:
                names = sorted(roster.workers)
                queue_by: Dict[str, str] = {}
                if not light:
                    wlist = [roster.workers[n] for n in names]
                    with concurrent.futures.ThreadPoolExecutor(
                            max_workers=min(len(wlist) or 1, 16)) as pool:
                        queue_by = dict(zip(names, pool.map(_worker_queue, wlist)))
                for name in names:
                    w = roster.workers[name]
                    q = "—" if light else queue_by[name]
                    cron = maybe_cron(w.schedule)
                    nf = next_fire_utc(cron, _utcnow()) if cron else None
                    if light:
                        last = None
                        health_cls = "ok"
                        e_streak = 0
                        backoff_secs = 0
                        resting = False
                    else:
                        shifts = parse_shifts(
                            Ledger(os.path.join(self.local_root, "ledger"), name).tail(60), limit=3)
                        last = next((s for s in shifts if not s["dry_run"]), None)
                        health_cls = _worker_health(self.local_root, w, q)["cls"]
                        e_streak, e_last = empty_run_streak(
                            self.local_root, name, since_ts=wakes.get(name) or None)
                        # wf-149: effective idle gate for honest Map "resting"
                        backoff_secs = (int(w.empty_run_backoff or 0)
                                        or adaptive_backoff_secs(w, e_streak))
                        threshold = max(1, int(w.empty_run_threshold or 3))
                        if w.empty_run_pause and w.queue_url and e_streak >= threshold:
                            resting = True  # wf-125 probe gate holds while empty
                        elif backoff_secs > 0 and e_last:
                            _lastdt = _parse_iso_z(e_last)
                            resting = bool(_lastdt) and (_utcnow() - _lastdt).total_seconds() < backoff_secs
                        else:
                            resting = False
                    workers.append({
                        "name": name, "kind": w.kind,
                        "staff": bool(w.staff), "type": w.worker_type,
                        "workdir": os.path.abspath(w.workdir),
                        "display": w.display or "", "succeeds": w.succeeds or "",
                        "identity": w.identity,
                        "cli": _cli_label(w), "model": w.model,
                        "schedule": w.schedule, "owned": bool(cron),
                        "owner": w.owner or "",
                        "skill": w.skill or "",
                        "next_fire": _utc_iso_z(nf) if nf else "",
                        "queue": q, "queue_url": w.queue_url or "",
                        "health": health_cls,
                        "empty_streak": e_streak,
                        "backoff_secs": backoff_secs,
                        "resting": resting,
                        "last_shift": ({"ts": last["ts"], "outcome": last["outcome"],
                                        "passes": last["passes"], "reason": last["reason"]}
                                       if last else None),
                    })
            data = json.dumps({"daemon": status, "workers": workers,
                               "engine": {"pid": os.getpid(),
                                          "local_root": os.path.realpath(self.local_root)}}).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self._client_gone_write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        elif self.path == "/api/health":
            payload = {"ok": True, "port": int(self.server.server_address[1])}
            data = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self._client_gone_write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._json_response({"ok": False, "msg": "not found"}, 404)

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # quiet; the ledger is the record that matters

    def log_error(self, fmt: str, *args: object) -> None:
        # BaseHTTPRequestHandler dumps full tracebacks for client disconnects
        # via socketserver.handle_error → log_error. Suppress the noisy ones;
        # _client_gone_write already records a one-line breadcrumb.
        msg = fmt % args if args else str(fmt)
        if "BrokenPipeError" in msg or "ConnectionResetError" in msg:
            return
        # fall through for real faults (keep default shape, no super spam)
        print("board error: " + msg.splitlines()[0][:200], flush=True)

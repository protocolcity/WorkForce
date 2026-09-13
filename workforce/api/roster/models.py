"""JSON read models: scene, tape, report, and one-worker personnel file."""

import concurrent.futures
import datetime
import os
import urllib.parse
from typing import Dict, List, Optional

from ...daemon import heartbeat_status, read_heartbeat
from ..._utils import _parse_iso_z, _utc_iso_z, _utcnow
from ...ledger import Ledger, open_candidates, parse_shifts
from ...roster import Worker
from ...schedule import maybe_cron, next_fire_utc
from ... import runtimes as runtimes_mod
from .constants import (
    CITYHALL,
    _FAULT_OUTCOMES,
    _REPORT_QUIET_HOURS,
    _REPORT_WINDOW_DAYS,
    _desk,
    _kind_label,
)
from .desk import (
    _desk_json,
    _worker_flags,
    _worker_holdings,
    _worker_queue,
    _worker_ready_teaser,
)
from .helpers import _cli_label, _display_names, _load_roster, _sector_for_worker
from .law import _law_stack, _worker_health
from .pulse import _launchctl_rota


def _ledger_candidates(
    local_root: str, worker_name: str,
) -> List[Dict[str, object]]:
    """Open CANDIDATE rows (and legacy CLAIM dispatch-input) → scene shape.

    Local disk only — safe on the light path. Empty when no shift is open or
    the ready probe was count-only (no task ids recorded). Not ownership.
    """
    path = os.path.join(local_root, "ledger", "%s.log" % worker_name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    out: List[Dict[str, object]] = []
    for c in open_candidates(text)[:3]:
        tid = str(c.get("ticket") or c.get("id") or "").strip()
        if not tid:
            continue
        product = str(c.get("product") or "")
        href = "%s/admin/desk?open=%s" % (
            _desk().rstrip("/"), urllib.parse.quote(tid))
        pri: object = c.get("priority")
        if pri is not None and str(pri) != "":
            try:
                pri = int(str(pri))
            except (TypeError, ValueError):
                pass
        else:
            pri = None
        row: Dict[str, object] = {
            "id": tid,
            "title": str(c.get("title") or ""),
            "status": "candidate",
            "priority": pri,
            "product": product,
            "updated_at": str(c.get("ts") or ""),
            "href": href,
            "source": "ledger",
        }
        if c.get("legacy_claim") == "1":
            row["legacy_claim"] = True
        out.append(row)
    return out


def _worker_full_data(
    local_root: str, w: Worker, in_flight_set: set
) -> Dict[str, object]:
    """Per-worker full-path data in one thread: queue probe, health, last shift, holdings.

    Called from scene_model's ThreadPoolExecutor fan-out so all workers run
    concurrently instead of serially.  _worker_health and _worker_holdings are
    defined later in this module — Python resolves at call time, not definition
    time, so forward refs here are fine.

    wf-147: when the worker is in_flight, queue probe and holdings run in
    parallel inside this worker slot so the two desk round-trips do not stack
    (serial path was ~timeout_q + timeout_hold under a slow desk).

    wf-250: holdings come from desk probes (signed WorkLane claims). Ledger
    CANDIDATE rows are dispatch-input context only.
    """
    holding: List[Dict[str, object]] = []
    candidates: List[Dict[str, object]] = []
    if w.name in in_flight_set:
        candidates = _ledger_candidates(local_root, w.name)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as _inner:
            fq = _inner.submit(_worker_queue, w)
            # scene bay = live claims only (in_progress); skip in_review round-trip
            fh = _inner.submit(
                _worker_holdings, w,  # type: ignore[name-defined]
                ("in_progress",),
            )
            q = fq.result()
            try:
                holding = (fh.result() or [])[:3]
            except Exception:
                holding = []
    else:
        q = _worker_queue(w)
    health = _worker_health(local_root, w, q)  # type: ignore[name-defined]
    shifts = parse_shifts(
        Ledger(os.path.join(local_root, "ledger"), w.name).tail(60),
        limit=3,
    )
    last = next((s for s in shifts if not s["dry_run"]), None)
    return {
        "q": q, "health": health, "last": last,
        "holding": holding, "candidates": candidates,
    }


def scene_model(local_root: str, light: bool = False) -> Dict[str, object]:
    """The dispatch scene's facts, computed from THIS engine's own state
   : the production room reads its own
    roster/heartbeat/ledger directly — never the city lens's /api/city.

    Pure: raw facts only, no per-second derivation. The scene JS computes
    on-shift / T-minus / progress client-side each tick, so viewer-truth
    tracks the wall clock without a re-fetch. Hot path stays network-free
    except a bounded desk probe for workers currently in_flight (live claim
    teaser on the bay) — idle floors still hit zero desk URLs.

    light=True: skip launchctl services, runtime detect,
    queue probes, and desk holdings — for suite Map people bootstrap.
    Stripped fields are fixed to sentinel values so suite consumers need no
    null-guards:
        cli=""  queue="—"  health="ok"  why="light"
        last_shift=null  services=[]  runtimes={detected:[],pool:[]}
    candidates (not holding) are filled from engine-owned CANDIDATE ledger
    rows when the worker is in_flight — local disk only, no desk round-trip.
    Stable fields present in both modes: name, kind, display, model,
    schedule, owned, owner, skill, next_fire, daemon, in_flight, last_tick.
    """
    roster = _load_roster(local_root)
    status = heartbeat_status(local_root)
    hb = read_heartbeat(local_root) or {}
    names = _display_names(local_root)
    in_flight_raw = hb.get("in_flight") or []
    if not isinstance(in_flight_raw, list):
        in_flight_raw = []
    in_flight_set = {str(x) for x in in_flight_raw}

    sectors: Dict[str, Dict[str, object]] = {}
    services: List[Dict[str, str]] = []
    _pool: object = None  # set in fan-out (not light) or light sentinel below
    if roster:
        _sorted_names = sorted(roster.workers)
        _wdata_by: Dict[str, Dict[str, object]] = {}
        if not light:
            _wlist = [roster.workers[n] for n in _sorted_names]
            _n = max(len(_wlist), 1)

            def _rt_task() -> object:
                _d = runtimes_mod.detect()
                return runtimes_mod.staffing_pool(_d, roster, local_root=local_root)

            # Fan out: per-worker I/O (queue probe + health + ledger + holdings),
            # launchctl subprocess, and runtime detect all run concurrently.
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(_n + 2, 18)) as _tp:
                _wfuts = [
                    _tp.submit(_worker_full_data, local_root, w, in_flight_set)
                    for w in _wlist
                ]
                _lctl_fut = _tp.submit(_launchctl_rota, local_root)
                _rt_fut = _tp.submit(_rt_task)
            # All futures complete when the `with` block exits.
            _wdata_by = dict(zip(_sorted_names, [f.result() for f in _wfuts]))
            try:
                for row in _lctl_fut.result():
                    if row.get("kind") == "service":
                        services.append({
                            "label": row.get("label") or "",
                            "kind": "service",
                            "pid": row.get("pid") or "",
                            "next_fire": row.get("next_fire") or "",
                            "state": row.get("state") or "",
                        })
            except Exception:
                services = []
            try:
                _pool = _rt_fut.result()
            except Exception:
                pass
        for name in _sorted_names:
            w = roster.workers[name]
            # light=True must stay network-free: each queue_url probe is up to
            # 3s, and a hung WorkLane (17 workers) freezes Map bootstrap for
            # ~50s+ (founder 2026-07-25 outage). Full scene probes via fan-out.
            if light:
                q = "—"
                health = {"cls": "ok", "why": "light"}
                last = None
                # wf-250: local CANDIDATE ledger only — never desk on light path
                if name in in_flight_set:
                    candidates = _ledger_candidates(local_root, name)
                else:
                    candidates = []
                holding = []
            else:
                _wd = _wdata_by[name]
                q = _wd["q"]
                health = _wd["health"]
                last = _wd["last"]
                holding = _wd["holding"]
                candidates = _wd["candidates"]
            cron = maybe_cron(w.schedule)
            nf = next_fire_utc(cron, _utcnow()) if cron else None
            gkey, workplace, role = _sector_for_worker(name, w, names)
            workdir = os.path.abspath(w.workdir) if w.workdir else ""
            sec = sectors.setdefault(gkey, {
                "workplace": workplace,
                "role": role,
                "workdir": workdir if role != "civic" else workdir,
                "workers": []})
            # Prefer ProtocolCity workdir for Office staff papers when present
            if role == "staff" and "ProtocolCity" in workdir:
                sec["workdir"] = workdir
            sec["workers"].append({  # type: ignore[union-attr]
                "name": name, "kind": _kind_label(w.kind),
                "display": w.display or "",
                "cli": _cli_label(w) if not light else "",
                "model": w.model or "default", "schedule": w.schedule or "",
                "owned": bool(cron),
                "owner": w.owner or "",
                "skill": w.skill or "",
                "next_fire": _utc_iso_z(nf) if nf else "",
                "queue": q, "health": health["cls"], "why": health["why"],
                "holding": holding,
                "candidates": candidates,
                "last_shift": ({"ts": last["ts"], "outcome": last["outcome"],
                                "passes": last["passes"], "reason": last["reason"]}
                               if last else None),
            })

    # You — citizen presence on the Roster (not a clock-in job)
    you_sec = {
        "workplace": "You",
        "role": "you",
        "workdir": "",
        "workers": [{
            "name": "you",
            "kind": "citizen",
            "display": "You · this Office",
            "cli": "",
            "model": "",
            "schedule": "",
            "owned": False,
            "next_fire": "",
            "queue": "—",
            "health": "ok",
            "why": "citizen",
            "holding": [],
            "candidates": [],
            "last_shift": None,
            "no_clock_in": True,
            "href": CITYHALL,
        }],
    }

    role_rank = {"you": 0, "staff": 1, "engine": 2, "business": 3}

    def _sector_sort(s: Dict[str, object]) -> tuple:
        return (role_rank.get(str(s.get("role") or "business"), 9),
                str(s.get("workplace") or "").lower())

    daemon = ("draining" if (hb.get("state") == "draining" and status != "stopped")
              else status)
    sector_list = [you_sec] + sorted(sectors.values(), key=_sector_sort)
    if light:
        _pool = {"detected": [], "pool": []}
    elif _pool is None:
        # roster was None (no roster.json) — runtimes fan-out never ran
        try:
            _detected = runtimes_mod.detect()
            _pool = runtimes_mod.staffing_pool(_detected, roster, local_root=local_root)
        except Exception:
            _pool = []
    return {
        "generated_at": _utc_iso_z(),
        "daemon": daemon,
        "in_flight": list(in_flight_raw),
        "last_tick": hb.get("last_tick", ""),
        "sectors": sector_list,
        "services": services,
        "runtimes": _pool,
        "light": bool(light),
    }


def scene_tape(local_root: str) -> Dict[str, object]:
    """Desk closures proxy for /api/scene-tape.

    Kept as a read API for benches; Roster D0 no longer mounts the tape —
    Desk owns closure traffic (suite perimeter).

    Deliberately NOT folded into scene_model: that read model is network-free
    by design, and the desk feed lives on the desk, not this engine's
    state. Keeping the tape a separate endpoint means the hot path never
    blocks on the desk and the tape degrades on its own when the desk is down.

    The desk stays a config seam (DESK env, host-neutral): we reuse the same
    ``_desk_json("/api/dev/activity")`` proxy the JSON tape already uses. The
    feed mixes comments and status changes; a CLOSED item is a status_change
    to a terminal state (done | canceled). The desk already bounds status
    changes to the last 24h server-side, so no window filter is needed here.
    """
    generated = _utc_iso_z()
    feed = _desk_json("/api/dev/activity?limit=50")
    if feed is None:
        return {"generated_at": generated, "desk": _desk(),
                "desk_ok": False, "closed": []}
    closed: List[Dict[str, str]] = []
    for e in feed.get("entries", []):
        if e.get("entry_type") != "status_change":
            continue
        status = e.get("new_status") or ""
        if status not in ("done", "canceled"):
            continue
        closed.append({
            "task_id": str(e.get("task_id", "")),
            "title": (e.get("task_title") or "").strip(),
            "status": status,
            "ts": e.get("created_at") or "",
        })
    return {"generated_at": generated, "desk": _desk(),
            "desk_ok": True, "closed": closed[:12]}


def report_model(local_root: str, days: Optional[int] = None) -> Dict[str, object]:
    """The report's facts in one call: per-worker verdicts + shift tallies
    from THIS engine's ledger, schedule/daemon state, the quiet list, and
    the desk's filed/closed tallies joined by signing identity. The desk
    join degrades on its own (desk.ok=false) — the board never recomputes
    comment-derived numbers."""
    days = max(1, min(int(days or _REPORT_WINDOW_DAYS), 90))
    now = _utcnow()
    since = now - datetime.timedelta(days=days)
    quiet_cut = now - datetime.timedelta(hours=_REPORT_QUIET_HOURS)
    roster = _load_roster(local_root)
    status = heartbeat_status(local_root)
    hb = read_heartbeat(local_root) or {}
    names = _display_names(local_root)

    def _shift_secs(s: Dict[str, object]) -> int:
        """Busy seconds for one shift: telemetry when present,
        start→end wall clock as the pre-telemetry fallback."""
        usage = s.get("usage") or {}
        if usage.get("secs"):
            return int(usage["secs"])  # type: ignore[index]
        a, b = _parse_iso_z(str(s.get("ts", ""))), _parse_iso_z(str(s.get("end_ts", "")))
        return int((b - a).total_seconds()) if a and b else 0

    workers: List[Dict[str, object]] = []
    quiet: List[Dict[str, object]] = []
    fires: List[Dict[str, str]] = []
    ident_by: Dict[str, str] = {}
    vendors: Dict[str, Dict[str, object]] = {}
    if roster:
        for name in sorted(roster.workers):
            w = roster.workers[name]
            if w.identity:
                ident_by[w.identity] = name
            q = _worker_queue(w)
            health = _worker_health(local_root, w, q)
            cron = maybe_cron(w.schedule)
            nf = next_fire_utc(cron, now) if cron else None
            shifts = [s for s in parse_shifts(
                Ledger(os.path.join(local_root, "ledger"), name).tail(2000),
                limit=400) if not s["dry_run"]]
            last = shifts[0] if shifts else None
            in_window = [s for s in shifts
                         if (_parse_iso_z(s["ts"]) or since) >= since]
            n_ok = sum(1 for s in in_window if s["outcome"] == "ok")
            n_fault = sum(1 for s in in_window if s["outcome"] in _FAULT_OUTCOMES)
            n_total = len(in_window)
            running = bool(last and last["outcome"] == "running")
            if health["cls"] == "wedged":
                verdict = "wedged"
            elif health["cls"] == "err":
                verdict = "faulting"
            elif running:
                verdict = "on shift"
            elif n_fault:
                verdict = "rough"
            elif n_ok:
                verdict = "steady"
            elif n_total:
                verdict = "starved"   # window activity, zero ok: skips/warns only
            elif cron:
                verdict = "quiet"
            else:
                verdict = "off rota"
            # capacity: what this worker burned in the window, by
            # the vendor CLI it runs on — the board's answer to "which
            # engine is the city actually spending"
            vendor = os.path.basename(w.command[0]) if w.command else "?"
            busy_secs = sum(_shift_secs(s) for s in in_window)
            tokens = int(sum(float((s.get("usage") or {}).get(k, 0) or 0)
                             for s in in_window for k in ("tok_in", "tok_out")))
            cost = round(sum(float((s.get("usage") or {}).get("cost_usd", 0) or 0)
                             for s in in_window), 4)
            vrow = vendors.setdefault(vendor, {
                "vendor": vendor, "workers": 0, "shifts": 0,
                "busy_secs": 0, "tokens": 0, "cost_usd": 0.0})
            vrow["workers"] = int(vrow["workers"]) + 1          # type: ignore[arg-type]
            vrow["shifts"] = int(vrow["shifts"]) + n_total      # type: ignore[arg-type]
            vrow["busy_secs"] = int(vrow["busy_secs"]) + busy_secs  # type: ignore[arg-type]
            vrow["tokens"] = int(vrow["tokens"]) + tokens       # type: ignore[arg-type]
            vrow["cost_usd"] = round(float(vrow["cost_usd"]) + cost, 4)  # type: ignore[arg-type]
            workers.append({
                "name": name,
                "sector": names.get(os.path.basename(os.path.abspath(w.workdir)),
                                    os.path.basename(os.path.abspath(w.workdir))),
                "identity": w.identity, "model": w.model or "default",
                "vendor": vendor,
                "owned": bool(cron), "schedule": w.schedule or "",
                "next_fire": _utc_iso_z(nf) if nf else "",
                "queue": q, "health": health["cls"], "why": health["why"],
                "verdict": verdict, "ok": n_ok, "fault": n_fault,
                "total": n_total,
                "busy_secs": busy_secs, "tokens": tokens, "cost_usd": cost,
                "last_ts": last["ts"] if last else "",
                "last_outcome": last["outcome"] if last else "",
            })
            last_dt = _parse_iso_z(last["ts"]) if last else None
            if not running and (last_dt is None or last_dt < quiet_cut):
                quiet.append({"name": name, "owned": bool(cron),
                              "hours": (int((now - last_dt).total_seconds() // 3600)
                                        if last_dt else None)})
            if nf:
                fires.append({"name": name,
                              "at": _utc_iso_z(nf)})
    fires.sort(key=lambda f: f["at"])
    quiet.sort(key=lambda e: (e["hours"] is not None, -(e["hours"] or 0)))

    alloc = _desk_json("/api/dev/allocation?window_days=%d" % days)
    if alloc and alloc.get("ok"):
        desk: Dict[str, object] = {
            "ok": True, "url": _desk(),
            "authors": [{"author": a.get("author", ""),
                         "filed": a.get("filed", 0), "closed": a.get("closed", 0),
                         "worker": ident_by.get(a.get("author", ""), "")}
                        for a in alloc.get("authors", [])],
            "lanes": alloc.get("lanes", []),
        }
    else:
        desk = {"ok": False, "url": _desk(), "authors": [], "lanes": []}

    daemon = ("draining" if (hb.get("state") == "draining" and status != "stopped")
              else status)
    return {
        "generated_at": _utc_iso_z(now),
        "window_days": days, "quiet_hours": _REPORT_QUIET_HOURS,
        "daemon": {"status": daemon, "last_tick": hb.get("last_tick", ""),
                   "in_flight": hb.get("in_flight", [])},
        "workers": workers,
        "next_fires": fires[:5],
        "quiet": quiet,
        "desk": desk,
        "capacity": sorted(vendors.values(),
                           key=lambda v: -int(v["busy_secs"])),  # type: ignore[arg-type]
    }


def worker_model(local_root: str, name: str) -> Optional[Dict[str, object]]:
    """One worker's personnel file as a read model — the facts the
    in-scene drawer renders: identity plate, schedule/queue/health, the
    resolved law stack (with /law hrefs — paths never leave the server),
    and the recent shift record. Pure and board-local, like scene_model."""
    roster = _load_roster(local_root)
    if not roster or name not in roster.workers:
        return None
    w = roster.workers[name]
    q = _worker_queue(w)
    health = _worker_health(local_root, w, q)
    cron = maybe_cron(w.schedule)
    nf = next_fire_utc(cron, _utcnow()) if cron else None
    law = []
    for i, entry in enumerate(_law_stack(w)):
        law.append({
            "level": entry["level"], "label": entry["label"],
            "file": os.path.basename(entry["path"]),
            "sha": entry["sha"][:8] if entry["sha"] else "",
            "mtime": entry["mtime"],
            "href": ("/law/%s/stack%d" % (name, i)) if entry["sha"] else "",
        })
    shifts = parse_shifts(
        Ledger(os.path.join(local_root, "ledger"), name).tail(400), limit=10)
    # Holding = Owner: claims; ready = top of queue; flags = governance layer.
    holding = _worker_holdings(w)
    ready = _worker_ready_teaser(w) if w.queue_url else []
    flags = _worker_flags(w) if w.queue_url else []
    return {
        "name": name, "kind": _kind_label(w.kind),
        "display": w.display or "",
        "succeeds": w.succeeds or "",
        "cli": _cli_label(w),
        "model": w.model or "default", "identity": w.identity,
        "workdir": os.path.abspath(w.workdir),
        "schedule": w.schedule or "", "owned": bool(cron),
        "next_fire": _utc_iso_z(nf) if nf else "",
        "budget_secs": w.budget_secs, "max_passes": w.max_passes,
        "queue": q, "queue_url": w.queue_url or "",
        "health": health["cls"], "why": health["why"],
        "holding": holding,
        "holding_count": len(holding),
        "ready": ready,
        "flags": flags,
        "law": law,
        "shifts": [{"ts": s["ts"], "outcome": s["outcome"],
                    "passes": s["passes"], "queue": s["queue"],
                    "reason": s["reason"], "dry_run": s["dry_run"]}
                   for s in shifts],
    }

"""Data models and API helpers.

Pure-Python side: constants, helpers, JSON data models. No HTML.
HTML surfaces live in workforce.surfaces.roster.
"""

import datetime
import hashlib
import json
import os
import plistlib
import re
import subprocess
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from ..daemon import heartbeat_status, read_heartbeat
from ..engine import _dig
from ..ledger import Ledger, parse_shifts
from ..roster import Roster, RosterError, Worker
from ..schedule import calendar_intervals_to_cron, maybe_cron
from .. import roster as roster_mod
from .. import runtimes as runtimes_mod

DEFAULT_PORT = int(os.environ.get("WORKFORCE_PORT") or "8797")

# pc-23: "lane" is retired vocabulary on rendered surfaces; roster data still
# says kind=lane until the schema migration lands.
_KIND_LABELS = {"lane": "worker"}


def generation_token(local_root: str) -> Dict[str, object]:
    """Cheap freshness token for suite pulse bus.

    Token changes on any of:
    - hire/fire: roster.json mtime/size changes
    - in_flight change: daemon.json mtime changes AND in_flight list hashed
      explicitly (belt-and-suspenders so the suite Map detects dispatch
      start/stop without waiting for the next ledger write)
    - ledger write, run artifact, or lock acquire/release

    Returns {"token": str, "ts": ISO8601Z, "in_flight": list, "daemon": str}.
    Tokens only — no worker bodies; stays cheap enough for sub-second polling.
    """
    parts: List[str] = []
    for name in ("roster.json", "daemon.json", "platforms.json"):
        p = os.path.join(local_root, name)
        try:
            st = os.stat(p)
            parts.append("%s:%d:%d" % (name, int(st.st_mtime), int(st.st_size)))
        except OSError:
            parts.append("%s:0" % name)
    for sub in ("ledger", "run", "locks"):
        d = os.path.join(local_root, sub)
        max_m = 0
        count = 0
        try:
            for fn in os.listdir(d):
                try:
                    m = int(os.path.getmtime(os.path.join(d, fn)))
                    if m > max_m:
                        max_m = m
                    count += 1
                except OSError:
                    pass
        except OSError:
            pass
        parts.append("%s:%d:%d" % (sub, max_m, count))
    daemon_state = heartbeat_status(local_root) or "stopped"
    hb = read_heartbeat(local_root) or {}
    inflight = hb.get("in_flight") or []
    if not isinstance(inflight, list):
        inflight = []
    parts.append("if:" + ",".join(sorted(str(x) for x in inflight)))
    parts.append("daemon:" + str(daemon_state))
    raw = "|".join(parts)
    token = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return {
        "token": token,
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "in_flight": list(inflight),
        "daemon": daemon_state,
    }


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")
DESK = os.environ.get("WORKFORCE_DESK", os.environ.get("WORKFORCE_DESK", "http://127.0.0.1:8799"))
CITYHALL = os.environ.get("WORKFORCE_CITYHALL", os.environ.get("WORKFORCE_CITYHALL", "http://127.0.0.1:8796"))

# ── Dashboard branding ──────
# In a founded city the room name leads: "ProtocolCity — Roster · Workers".
# A standalone WorkForce install fronts the engine brand and shows no doors
# to uninstalled rooms. Mirrors TP's TP_BRAND seam;
# this internal checkout IS the city instance, so "city" is the default and
# the public export must default to "standalone".
_BRAND_MODE = os.environ.get("WORKFORCE_BRAND", "city")
_IN_CITY = _BRAND_MODE == "city"
_BRAND_TITLE = ("ProtocolCity — Roster · Workers" if _IN_CITY
                else "WorkForce — Workers")


def _city_folder_name() -> str:
    """Basename of the city root for `[Folder] Roster` mast parity with Office."""
    root = (os.environ.get("CITY_ROOT") or os.environ.get("TP_CITY_ROOT") or "").strip()
    if not root:
        hood = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        top = ""
        cur = hood
        while True:
            if os.path.isfile(os.path.join(cur, "AGENTS.md")):
                top = cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        # Standalone: topmost AGENTS.md is this neighborhood — not a city.
        if top and os.path.abspath(top) != hood:
            root = top
    if not root or not os.path.isdir(root):
        return "City"
    return os.path.basename(root.rstrip(os.sep)) or "City"


def _service_config(local_root: str) -> Dict[str, tuple]:
    """launchd label prefixes/labels to render, from platforms.json — the
    board names no host in code (the tenth-runner law applies to the UI too).
    Absent config = no host-services section, which is correct on a fresh
    install."""
    path = os.path.join(local_root, "platforms.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {"prefixes": tuple(raw.get("service_prefixes", [])),
                "services": tuple(raw.get("service_labels", []))}
    except (OSError, ValueError):
        return {"prefixes": (), "services": ()}


# ── Time helpers ──────────────────────────────────────────────────────────

def _ago(ts: str) -> str:
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return ts
    secs = int((_utcnow() - dt).total_seconds())
    if secs < 90:
        return "%ds ago" % secs
    if secs < 5400:
        return "%dm ago" % (secs // 60)
    if secs < 172800:
        return "%dh ago" % (secs // 3600)
    return "%dd ago" % (secs // 86400)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _fmt_fire(dt: Optional[datetime.datetime]) -> str:
    if not dt:
        return ""
    mins = int((dt - _utcnow()).total_seconds() // 60)
    if mins >= 2880:
        return "%s (in %dd)" % (dt.strftime("%b %d %H:%M"), mins // 1440)
    return "%s (in %dm)" % (dt.strftime("%H:%M"), max(mins, 0))


# ── Legacy plist / launchd ────────────────────────────────────────────────

def _legacy_plist(label: str) -> Dict[str, str]:
    """Next fire + log path from the legacy plist itself — read-only lens.

    Dies with the rota: once a lane migrates, its plist (and this parse)
    is gone. Until then it's the truthful source for the legacy cadence.
    """
    path = os.path.join(LAUNCH_AGENTS, "%s.plist" % label)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        # legacy plists carry '--flags' inside XML comments — legal to Apple's
        # parser, fatal to expat; comments carry no plist data, drop them
        raw = re.sub(rb"<!--.*?-->", b"", raw, flags=re.S)
        data = plistlib.loads(raw)
    except Exception:
        return {"next_fire": "", "log": ""}
    next_fire = ""
    cal = data.get("StartCalendarInterval")
    if cal is not None:
        cron = calendar_intervals_to_cron(cal)
        next_fire = _fmt_fire(cron.next_fire(_utcnow())) if cron else "calendar"
    elif "StartInterval" in data:
        next_fire = "every %ss" % data["StartInterval"]
    elif data.get("KeepAlive"):
        next_fire = "keepalive"
    return {"next_fire": next_fire, "log": data.get("StandardOutPath", "") or ""}


def _launchctl_rota(local_root: str) -> List[Dict[str, str]]:
    cfg = _service_config(local_root)
    if not cfg["prefixes"]:
        return []
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[2].startswith(cfg["prefixes"]):
            continue
        pid, status, label = parts
        row = {
            "label": label,
            "pid": pid if pid != "-" else "",
            "last_exit": status,
            "kind": "service" if label in cfg["services"] else "legacy worker/job",
        }
        row.update(_legacy_plist(label))
        rows.append(row)
    return sorted(rows, key=lambda r: r["label"])


# ── Desk JSON proxy ───────────────────────────────────────────────────────

def _desk_json(path: str) -> Optional[dict]:
    try:
        with urllib.request.urlopen(DESK + path, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# ── Roster / worker helpers ───────────────────────────────────────────────

def _last_ledger_line(local_root: str, worker: str) -> str:
    tail = Ledger(os.path.join(local_root, "ledger"), worker).tail(1).strip()
    return tail


def _queue_human_link(w: Worker) -> str:
    """The way back: the queue probe is an API URL; the same desk serves the
    human view at /admin/tickets/<product>?label=... (a desk convention —
    conventions are the API, same as the law-stack walk)."""
    if not w.queue_url:
        return ""
    from urllib.parse import parse_qs, quote, urlsplit
    parts = urlsplit(w.queue_url)
    qs = parse_qs(parts.query)
    product = (qs.get("product") or qs.get("project") or [""])[0]
    if not product:
        return ""
    url = "%s://%s/admin/tickets/%s" % (parts.scheme, parts.netloc, product)
    label = (qs.get("label") or [""])[0]
    if label:
        url += "?label=" + quote(label)
    return url


def _worker_queue(w: Worker) -> str:
    """Ready count for this worker's lane — '?' when unprobed/unreachable."""
    if not w.queue_url:
        return "—"
    try:
        with urllib.request.urlopen(w.queue_url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(int(_dig(data, w.queue_count_key)))
    except Exception:
        return "?"


def _platforms(local_root: str) -> List[Dict[str, str]]:
    path = os.path.join(local_root, "platforms.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            entries = json.load(fh).get("platforms", [])
    except (OSError, ValueError):
        return []
    out = []
    for e in entries:
        ok = False
        try:
            with urllib.request.urlopen(e.get("health", e["url"]), timeout=5) as resp:
                ok = 200 <= resp.status < 400
        except Exception:
            ok = False
        out.append({"name": e.get("name", "?"), "url": e["url"], "ok": "ok" if ok else "err"})
    return out


def _load_roster(local_root: str) -> Optional[Roster]:
    try:
        return roster_mod.load(base=os.path.dirname(local_root) or os.getcwd())
    except RosterError:
        return None


def _display_names(local_root: str) -> Dict[str, str]:
    """Optional workdir-basename -> public name map (surface naming law:
    rendered surfaces wear PUBLIC names; internal codenames stay in paths).
    Lives in platforms.json as 'workplace_names'; absent = basename."""
    path = os.path.join(local_root, "platforms.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return dict(json.load(fh).get("workplace_names", {}))
    except (OSError, ValueError):
        return {}


def _sector_for_worker(
    wname: str, w: object, names: Dict[str, str]
) -> Tuple[str, str, str]:
    """Return (group_key, workplace_label, role) for floor grouping.

    role: you | staff | engine | business — drives the roof title.
    Workers with staff=True share one Office staff bay; product patrols
    stay with their cabinet (hired), not mixed into Office staff.
    """
    workdir = os.path.abspath(getattr(w, "workdir", "") or "")
    base = os.path.basename(workdir) if workdir else ""
    public = names.get(base, names.get(base.lower(), base or "unknown"))

    if getattr(w, "staff", False):
        return ("__office_staff__", "Office staff", "staff")

    if base.lower() in ("workforce", "workforce") or public == "WorkForce":
        return (workdir or "__engine__", "WorkForce", "engine")

    # Neighborhood cabinets (WorkLane, your products, ProtocolCity desk, …)
    return (workdir or public, public, "business")


def _workplaces(roster: Roster, local_root: str,
                health_by: Dict[str, Dict[str, str]],
                queue_by: Dict[str, str]) -> List[Dict[str, object]]:
    """The many-workplaces dimension, derived from roster data alone:
    group workers by workdir, roll up queue + health, derive the desk URL
    from the queue probes. Zero hand-maintained registry."""
    names = _display_names(local_root)
    groups: Dict[str, Dict[str, object]] = {}
    for wname in sorted(roster.workers):
        w = roster.workers[wname]
        key = os.path.abspath(w.workdir)
        g = groups.setdefault(key, {"workdir": key,
                                    "label": names.get(os.path.basename(key),
                                                       os.path.basename(key)),
                                    "workers": [], "queue": 0, "queue_known": False,
                                    "desks": set(), "worst": "ok"})
        g["workers"].append(wname)
        q = queue_by.get(wname, "?")
        if q.isdigit():
            g["queue"] += int(q)
            g["queue_known"] = True
        if w.queue_url:
            from urllib.parse import urlsplit
            parts = urlsplit(w.queue_url)
            g["desks"].add("%s://%s" % (parts.scheme, parts.netloc))
        rank = {"ok": 0, "dim": 0, "amber": 1, "err": 2}
        cls = health_by.get(wname, {}).get("cls", "dim")
        if rank.get(cls, 0) > rank.get(str(g["worst"]), 0):
            g["worst"] = cls
    return sorted(groups.values(), key=lambda g: str(g["label"]).lower())


def _cli_label(worker: Worker) -> str:
    """Basename of command[0] for bay payroll subtitle (claude/grok/codex/…)."""
    cmd = worker.command or []
    if not cmd:
        return ""
    return os.path.basename(str(cmd[0]))


# ── Data models ───────────────────────────────────────────────────────────

def scene_model(local_root: str, light: bool = False) -> Dict[str, object]:
    """The dispatch scene's facts, computed from THIS engine's own state
   : the production room reads its own
    roster/heartbeat/ledger directly — never the city lens's /api/city.

    Pure: raw facts only, no per-second derivation. The scene JS computes
    on-shift / T-minus / progress client-side each tick, so viewer-truth
    tracks the wall clock without a re-fetch. Hot path stays network-free
    except a bounded desk probe for workers currently in_flight (live claim
    teaser on the bay) — idle floors still hit zero desk URLs.

    light=True: skip ledger tails, launchctl services,
    runtime detect, queue probes, and in_flight holdings — for suite Map
    people bootstrap. Stripped fields are fixed to sentinel values so suite
    consumers need no null-guards:
        cli=""  queue="—"  health="ok"  why="light"  holding=[]
        last_shift=null  services=[]  runtimes={detected:[],pool:[]}
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
    if roster:
        for name in sorted(roster.workers):
            w = roster.workers[name]
            # light=True must stay network-free: each queue_url probe is up to
            # 3s, and a hung WorkLane (17 workers) freezes Map bootstrap for
            # ~50s+ (founder 2026-07-25 outage). Full scene still probes.
            if light:
                q = "—"
                health = {"cls": "ok", "why": "light"}
            else:
                q = _worker_queue(w)
                health = _worker_health(local_root, w, q)
            cron = maybe_cron(w.schedule)
            nf = cron.next_fire(_utcnow()) if cron else None
            last = None
            if not light:
                shifts = parse_shifts(
                    Ledger(os.path.join(local_root, "ledger"), name).tail(60),
                    limit=3,
                )
                last = next((s for s in shifts if not s["dry_run"]), None)
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
            # Live claim teaser only while the daemon has them in flight —
            # keeps the idle scene network-free (desk can be slow/down).
            holding: List[Dict[str, object]] = []
            if (not light) and name in in_flight_set:
                try:
                    holding = _worker_holdings(w)[:3]
                except Exception:
                    holding = []
            sec["workers"].append({  # type: ignore[union-attr]
                "name": name, "kind": _kind_label(w.kind),
                "display": w.display or "",
                "cli": _cli_label(w) if not light else "",
                "model": w.model or "default", "schedule": w.schedule or "",
                "owned": bool(cron),
                "owner": w.owner or "",
                "skill": w.skill or "",
                "next_fire": nf.strftime("%Y-%m-%dT%H:%M:%SZ") if nf else "",
                "queue": q, "health": health["cls"], "why": health["why"],
                "holding": holding,
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
    services: List[Dict[str, str]] = []
    if not light:
        try:
            for row in _launchctl_rota(local_root):
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
    sector_list = [you_sec] + sorted(sectors.values(), key=_sector_sort)
    if light:
        _pool = {"detected": [], "pool": []}
    else:
        _detected = runtimes_mod.detect()
        _pool = runtimes_mod.staffing_pool(_detected, roster)
    return {
        "generated_at": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    ``_desk_json("/api/dev/activity")`` proxy render_board already uses. The
    feed mixes comments and status changes; a CLOSED item is a status_change
    to a terminal state (done | canceled). The desk already bounds status
    changes to the last 24h server-side, so no window filter is needed here.
    """
    generated = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    feed = _desk_json("/api/dev/activity?limit=50")
    if feed is None:
        return {"generated_at": generated, "desk": DESK,
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
    return {"generated_at": generated, "desk": DESK,
            "desk_ok": True, "closed": closed[:12]}


# ── The report: the floor's strategic view ───────────────────────
# Sibling of tp-156's desk report, same reporting doctrine: engines
# compute facts, dashboards render. /api/report is one seam feeding this
# page and, later, the oc-15 daily founder brief. The throughput panel is
# the desk Overview's retired allocation panel rehired here — the
# DESK tallies filed/closed from its signed comments, the
# BOARD joins them to roster identities and shift data. Founder rulings
# (2026-07-14, live): the footer's "classic board" slot becomes this
# Overview; the scene footer adopts the desk footer format.

_REPORT_WINDOW_DAYS = int(os.environ.get("WORKFORCE_REPORT_WINDOW_DAYS", "7"))
_REPORT_QUIET_HOURS = int(os.environ.get("WORKFORCE_REPORT_QUIET_HOURS", "72"))
_REPORT_WINDOWS = (7, 14, 30)

_FAULT_OUTCOMES = ("error", "crashed")


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

    def _ts(iso: str) -> Optional[datetime.datetime]:
        try:
            return datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        except (ValueError, TypeError):
            return None

    def _shift_secs(s: Dict[str, object]) -> int:
        """Busy seconds for one shift: telemetry when present,
        start→end wall clock as the pre-telemetry fallback."""
        usage = s.get("usage") or {}
        if usage.get("secs"):
            return int(usage["secs"])  # type: ignore[index]
        a, b = _ts(str(s.get("ts", ""))), _ts(str(s.get("end_ts", "")))
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
            nf = cron.next_fire(now) if cron else None
            shifts = [s for s in parse_shifts(
                Ledger(os.path.join(local_root, "ledger"), name).tail(2000),
                limit=400) if not s["dry_run"]]
            last = shifts[0] if shifts else None
            in_window = [s for s in shifts
                         if (_ts(s["ts"]) or since) >= since]
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
                "next_fire": nf.strftime("%Y-%m-%dT%H:%M:%SZ") if nf else "",
                "queue": q, "health": health["cls"], "why": health["why"],
                "verdict": verdict, "ok": n_ok, "fault": n_fault,
                "total": n_total,
                "busy_secs": busy_secs, "tokens": tokens, "cost_usd": cost,
                "last_ts": last["ts"] if last else "",
                "last_outcome": last["outcome"] if last else "",
            })
            last_dt = _ts(last["ts"]) if last else None
            if not running and (last_dt is None or last_dt < quiet_cut):
                quiet.append({"name": name, "owned": bool(cron),
                              "hours": (int((now - last_dt).total_seconds() // 3600)
                                        if last_dt else None)})
            if nf:
                fires.append({"name": name,
                              "at": nf.strftime("%Y-%m-%dT%H:%M:%SZ")})
    fires.sort(key=lambda f: f["at"])
    quiet.sort(key=lambda e: (e["hours"] is not None, -(e["hours"] or 0)))

    alloc = _desk_json("/api/dev/allocation?window_days=%d" % days)
    if alloc and alloc.get("ok"):
        desk: Dict[str, object] = {
            "ok": True, "url": DESK,
            "authors": [{"author": a.get("author", ""),
                         "filed": a.get("filed", 0), "closed": a.get("closed", 0),
                         "worker": ident_by.get(a.get("author", ""), "")}
                        for a in alloc.get("authors", [])],
            "lanes": alloc.get("lanes", []),
        }
    else:
        desk = {"ok": False, "url": DESK, "authors": [], "lanes": []}

    daemon = ("draining" if (hb.get("state") == "draining" and status != "stopped")
              else status)
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
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


# ── Worker detail helpers ─────────────────────────────────────────────────

def _desk_owner_of(task_id: str) -> str:
    """Latest Owner: marker on a ticket (PROCESS.md §5)."""
    d = _desk_json("/api/admin/tasks/" + urllib.parse.quote(str(task_id)))
    task = (d or {}).get("task") if isinstance(d, dict) else None
    if not isinstance(task, dict):
        task = d if isinstance(d, dict) and d.get("id") else None
    for c in reversed((task or {}).get("comments") or []):
        body = str(c.get("body") or "")
        if body.startswith("Owner: "):
            line = body.split("\n", 1)[0][len("Owner: "):].strip()
            return line.split()[0].rstrip(".,;:") if line else ""
    return ""


def _worker_identity_aliases(w: Worker) -> List[str]:
    out: List[str] = []
    for raw in (w.identity, w.name, w.succeeds or ""):
        parts = (raw or "").strip().split()
        if not parts:
            continue
        tok = parts[0].rstrip(".,;:").lower()
        if tok and tok not in out:
            out.append(tok)
    return out


def _worker_holdings(w: Worker) -> List[Dict[str, object]]:
    """Tickets this worker currently holds (in_progress / in_review + Owner:)."""
    product = ""
    if w.queue_url:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(w.queue_url).query)
            product = (qs.get("product") or qs.get("project") or [""])[0]
        except Exception:
            product = ""
    if not product or product == "all":
        product = os.path.basename(os.path.abspath(w.workdir or "")).lower()
    if not product:
        return []
    aliases = set(_worker_identity_aliases(w))
    held: List[Dict[str, object]] = []
    seen: set = set()
    for status in ("in_progress", "in_review"):
        d = _desk_json(
            "/api/admin/tasks?product=%s&status=%s&limit=40"
            % (urllib.parse.quote(product), status))
        for t in (d or {}).get("tasks") or []:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            if not tid or tid in seen:
                continue
            owner = _desk_owner_of(tid)
            tok = owner.strip().split()[0].rstrip(".,;:").lower() if owner else ""
            if tok not in aliases:
                continue
            seen.add(tid)
            held.append({
                "id": tid,
                "title": str(t.get("title") or ""),
                "status": str(t.get("status") or status),
                "priority": t.get("priority"),
                "product": product,
                "owner": owner,
                "updated_at": str(t.get("updated_at") or ""),
                "href": "%s/admin/desk?open=%s" % (
                    DESK.rstrip("/"), urllib.parse.quote(tid)),
            })
    return held


def _worker_ready_teaser(w: Worker, *, limit: int = 10) -> List[Dict[str, object]]:
    """Top of this worker's ready queue (for emptying queues)."""
    product = ""
    label = ""
    if w.queue_url:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(w.queue_url).query)
            product = (qs.get("product") or qs.get("project") or [""])[0]
            label = (qs.get("label") or [""])[0]
        except Exception:
            product = ""
    if not product or product == "all":
        product = os.path.basename(os.path.abspath(w.workdir or "")).lower()
    if not product:
        return []
    q = {"product": product}
    if label:
        q["label"] = label
    d = _desk_json("/api/admin/tasks/ready?" + urllib.parse.urlencode(q))
    tasks = (d or {}).get("tasks") if isinstance(d, dict) else None
    if not isinstance(tasks, list):
        tasks = []
    out: List[Dict[str, object]] = []
    for t in tasks[:limit]:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        if not tid:
            continue
        out.append({
            "id": tid,
            "title": str(t.get("title") or ""),
            "status": str(t.get("status") or "backlog"),
            "priority": t.get("priority"),
            "product": product,
            "href": "%s/admin/desk?open=%s" % (
                DESK.rstrip("/"), urllib.parse.quote(tid)),
        })
    return out


def _worker_flags(w: Worker) -> List[Dict[str, object]]:
    """Open tickets in this worker's lane not yet in active hands (backlog + parked).

    Governance layer: surfaces blocked or waiting work so the health badge
    isn't the only signal on the personnel card.  Empty list when queue_url
    is absent or DESK unreachable — always safe to skip rendering.
    """
    product = ""
    label = ""
    if w.queue_url:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(w.queue_url).query)
            product = (qs.get("product") or qs.get("project") or [""])[0]
            label = (qs.get("label") or [""])[0]
        except Exception:
            product = ""
    if not product or product == "all":
        product = os.path.basename(os.path.abspath(w.workdir or "")).lower()
    if not product or not label:
        return []
    flags: List[Dict[str, object]] = []
    seen: set = set()
    for status in ("backlog", "parked"):
        d = _desk_json(
            "/api/admin/tasks?product=%s&label=%s&status=%s&limit=30"
            % (urllib.parse.quote(product), urllib.parse.quote(label), status))
        for t in (d or {}).get("tasks") or []:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            task_labels = [str(lbl) for lbl in (t.get("labels") or [])]
            founder_gated = any("needs:founder" in lbl for lbl in task_labels)
            flags.append({
                "id": tid,
                "title": str(t.get("title") or ""),
                "status": status,
                "priority": t.get("priority"),
                "labels": task_labels,
                "founder_gated": founder_gated,
                "href": "%s/admin/desk?open=%s" % (
                    DESK.rstrip("/"), urllib.parse.quote(tid)),
            })
    return flags


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
    nf = cron.next_fire(_utcnow()) if cron else None
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
        "next_fire": nf.strftime("%Y-%m-%dT%H:%M:%SZ") if nf else "",
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


# ── Law stack ─────────────────────────────────────────────────────────────

def _law_stack(worker: Worker) -> List[Dict[str, str]]:
    """The resolved law stack, by Charter convention — zero config.

    Standard filenames/placements ARE the API: the
    neighborhood's AGENTS.md sits in the workdir, city-level AGENTS.md files
    sit in ancestor directories. Contract and prompt come from the roster.
    Levels are labeled top-down: L0 city law ... L3 shift brief.
    """
    ancestors: List[str] = []
    d = os.path.dirname(os.path.abspath(worker.workdir))
    while True:
        cand = os.path.join(d, "AGENTS.md")
        if os.path.isfile(cand):
            ancestors.append(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    stack: List[Dict[str, str]] = []
    for path in reversed(ancestors):          # topmost first = city rules
        stack.append({"label": "city rules", "path": path})
    stack.append({"label": "neighborhood rules",
                  "path": os.path.join(os.path.abspath(worker.workdir), "AGENTS.md")})
    stack.append({"label": "contract", "path": worker.contract})
    stack.append({"label": "prompt", "path": worker.prompt})
    for i, entry in enumerate(stack):
        entry["level"] = "L%d" % min(i, 3)
        try:
            st = os.stat(entry["path"])
            with open(entry["path"], "rb") as fh:
                entry["sha"] = hashlib.sha256(fh.read()).hexdigest()[:16]
            entry["mtime"] = datetime.datetime.fromtimestamp(
                st.st_mtime, datetime.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        except OSError:
            entry["sha"], entry["mtime"] = "", "missing"
    return stack


RULE_HEADINGS = re.compile(r"lane|never|stop|scope|gate|may not|boundar", re.I)


def _contract_rules(path: str) -> List[Dict[str, str]]:
    """May/may-not summary from the contract's standardized headings."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    sections: List[Dict[str, str]] = []
    keep = False
    for line in lines:
        m = re.match(r"^#{2,4}\s+(.*)", line)
        if m:
            keep = bool(RULE_HEADINGS.search(m.group(1)))
            if keep:
                sections.append({"title": m.group(1).strip(), "body": ""})
            continue
        if keep and sections and line.strip():
            if len(sections[-1]["body"].splitlines()) < 8:
                sections[-1]["body"] += line.rstrip() + "\n"
    return sections


_WEDGE_SHIFTS = 3  # consecutive no-progress shifts on a nonempty queue = wedged


def _worker_health(local_root: str, w: Worker, queue: str) -> Dict[str, str]:
    """One dot per worker: ok | amber (starving) | err (last shift failed)
    | wedged (fires but never claims — the no-sitting law, oc-34)."""
    shifts = parse_shifts(Ledger(os.path.join(local_root, "ledger"), w.name).tail(60), limit=6)
    real = [s for s in shifts if not s["dry_run"]]
    if not real:
        return {"cls": "dim", "why": "no shifts yet"}
    last = real[0]
    if last["outcome"] == "vendor_limit":
        return {"cls": "amber", "why": last["reason"] or "vendor limit"}
    if last["outcome"] in ("error", "crashed"):
        return {"cls": "err", "why": "last desk run %s: %s"
                                     % (last["outcome"].upper(), last["reason"] or "?")}
    try:
        q_n = int(queue)
    except ValueError:
        q_n = 0
    recent = real[:_WEDGE_SHIFTS]
    if (q_n > 0 and len(recent) == _WEDGE_SHIFTS
            and all(s["outcome"] == "ok" and s["reason"].startswith("no progress")
                    for s in recent)):
        # True sit only: reason is "no progress (N -> N)" (flat count).
        # "restocked (N -> M)" is productive-but-full (close + file follow-ups)
        # and must not ship-cut the default-lane coordinator.
        return {"cls": "wedged",
                "why": "queue %s but last %d shifts ended no-progress — "
                       "fires without claiming" % (queue, _WEDGE_SHIFTS)}
    try:
        starving = int(queue) > 0 and all(s["outcome"] == "skip" for s in real[:2]) and len(real) >= 2
    except ValueError:
        starving = False
    if starving:
        return {"cls": "amber", "why": "queue %s but last %d fires skipped (%s)"
                                        % (queue, len(real[:2]), real[0]["reason"])}
    return {"cls": "ok", "why": "last desk run %s" % last["outcome"]}


def _git_law_log(path: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", os.path.dirname(path), "log", "-3", "--format=%h %ad %s",
             "--date=short", "--", os.path.basename(path)],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


OUTCOME_CLS = {"ok": "ok", "error": "err", "skip": "dim", "warn": "amber",
               "running": "amber", "crashed": "err", "vendor_limit": "amber"}

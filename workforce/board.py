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

import datetime
import hashlib
import html
import json
import os
import plistlib
import re
import subprocess
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

from .daemon import heartbeat_status, read_heartbeat
from .engine import _dig
from .ledger import Ledger, parse_shifts
from .roster import Roster, RosterError, Worker
from .schedule import calendar_intervals_to_cron, maybe_cron
from . import roster as roster_mod
from . import runtimes as runtimes_mod

DEFAULT_PORT = int(os.environ.get("WORKFORCE_PORT") or "8797")

# pc-23: "lane" is retired vocabulary on rendered surfaces; roster data still
# says kind=lane until the schema migration lands.
_KIND_LABELS = {"lane": "worker"}


def generation_token(local_root: str) -> Dict[str, object]:
    """Cheap freshness token for suite pulse bus.

    Covers roster, daemon heartbeat (in_flight), ledger and run-file mtimes.
    Tokens only — no worker bodies.
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


def _out_path(local_root: str, name: str) -> str:
    return os.path.join(local_root, "run", "%s.out" % name)


def _safe_worker_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""))


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
        hood = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
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

CSS = """
:root { --bg:#0b0d10; --panel:#12151a; --line:#232830; --ink:#d7dce3;
        --dim:#7d8590; --amber:#f5a623; --ok:#4cc38a; --err:#e5534b; }
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--ink); font:14px/1.5 "SF Mono",
       ui-monospace, Menlo, monospace; padding:28px; max-width:1180px;
       margin:0 auto; }
header { display:flex; align-items:baseline; gap:14px; border-bottom:2px solid
         var(--amber); padding-bottom:14px; margin-bottom:24px; }
h1 { font-size:17px; letter-spacing:.22em; color:var(--amber); }
h1 small { color:var(--dim); letter-spacing:.08em; font-weight:normal; }
h2 { font-size:12px; letter-spacing:.18em; color:var(--dim); margin:30px 0 10px;
     text-transform:uppercase; }
table { width:100%; border-collapse:collapse; background:var(--panel); }
th { text-align:left; color:var(--dim); font-weight:normal; font-size:11px;
     letter-spacing:.1em; text-transform:uppercase; }
th, td { padding:9px 12px; border-bottom:1px solid var(--line); }
tr:hover td { background:#161a21; }
a { color:var(--amber); text-decoration:none; } a:hover { text-decoration:underline; }
.ok { color:var(--ok); } .err { color:var(--err); } .dim { color:var(--dim); }
.wedged { color:var(--err); }
.tag { border:1px solid var(--line); border-radius:3px; padding:1px 7px;
       font-size:11px; color:var(--dim); }
.amber { color:var(--amber); }
pre { background:var(--panel); border:1px solid var(--line); padding:18px;
      overflow-x:auto; white-space:pre-wrap; }
footer { margin-top:30px; color:var(--dim); font-size:11px; }
.tiles { display:flex; gap:14px; margin:18px 0 6px; flex-wrap:wrap; }
.tile { background:var(--panel); border:1px solid var(--line); border-radius:4px;
        padding:12px 18px; flex:1; min-width:140px; }
.tile .n { font-size:24px; line-height:1.2; color:var(--ink); }
.tile .n.amber { color:var(--amber); } .tile .n.ok { color:var(--ok); }
.tile .n.err { color:var(--err); }
.tile .l { font-size:10px; letter-spacing:.14em; text-transform:uppercase;
           color:var(--dim); margin-top:2px; }
button.fire { background:none; border:1px solid var(--amber); color:var(--amber);
              border-radius:3px; font:inherit; font-size:11px; padding:2px 9px;
              cursor:pointer; }
button.fire:hover { background:var(--amber); color:var(--bg); }
button.fire:disabled { border-color:var(--line); color:var(--dim); cursor:default; }
.sub { color:var(--dim); font-size:12px; }
"""

FIRE_JS = """
<script>
function fire(name){
  if(!confirm('Clock '+name+' in now? This runs a real desk run.')) return;
  fetch('/api/dispatch/'+name, {method:'POST'})
    .then(r=>r.json()).then(d=>{
      alert(name+': '+(d.msg||JSON.stringify(d)));
      if(d.ok) setTimeout(()=>location.reload(), 800);
    }).catch(e=>alert('dispatch failed: '+e));
}
</script>
"""


# --- The dispatch scene: the living room as a
# daylight building cutaway with state-legible rooms. CITY_DNA sec.5:
# room base is the plat's parchment world + live sky; phosphor survives ONLY
# on the CRT consoles the workers sit at. oc-26 brings the plat's liveness
# indoors — floor strip + four embodied bay states (on / imminent / waiting
# / fault) + in-flight glow. Served at /, polling /api/scene.
SCENE_CSS = """
:root, [data-theme="light"] {
  /* CITY_DNA §5 + pc-162 — one daylight sheet; suite accent = verd */
  --parch-front:#e2d9c2; --parch-top:#efe8d5; --parch-side:#c9bd9f;
  --plaza:#cfc6ab; --walk:#bdb294; --gold:#e9c46a;
  --page:#faf6ec; --bg:#faf6ec; --card:#fffdf8; --line:#c4b8a4;
  --ink:#2a241c; --dim:#6b6154; --ink-deep:#2a241c; --verd:#3d7a6a;
  --ok:#2e7d4f; --warn:#a8681e; --fire:#a33327;
  --chrome-end:#ebe4d4;
  /* phosphor object — the machine only (not room chrome) */
  --crt-bg:#0a120a; --crt-panel:#050805; --amber:#ffb000; --amber2:#a07000;
  --glow:#ffcc33; --crt-ok:#33ff66; --crt-fire:#ff4422; --crt-dim:#445544;
  color-scheme:light;
}
/* Suite night sheet — shared with Office / Desk via protocolcity-theme */
[data-theme="dark"] {
  --parch-front:#2a261e; --parch-top:#322c24; --parch-side:#3a342c;
  --plaza:#242018; --walk:#2e2a22; --gold:#d4b05a;
  --page:#1a1814; --bg:#1a1814; --card:#252018; --line:#4a4338;
  --ink:#f0eade; --dim:#a89f8e; --ink-deep:#f0eade; --verd:#5a9a88;
  --ok:#4caf7d; --warn:#d9a441; --fire:#d4543f;
  --chrome-end:#221e18;
  --crt-bg:#0a120a; --crt-panel:#050805; --amber:#ffb000; --amber2:#a07000;
  --glow:#ffcc33; --crt-ok:#33ff66; --crt-fire:#ff4422; --crt-dim:#445544;
  color-scheme:dark;
}
* { box-sizing:border-box; margin:0; padding:0; }
html,body { height:100%; background:var(--page); color:var(--ink);
  font:15px/1.45 "Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  overflow:hidden; }
body { background:var(--page); }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.2} }
@keyframes breathe { 50%{opacity:.55} }
@keyframes bob { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-3px) rotate(-2deg)} }
@keyframes marquee { to { transform:translateX(-50%); } }
@keyframes hammer { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-2px)} }
/* Perimeter lock (suite D0): chrome + permanent seats reserved; only
   card stacks inside hired columns scroll. minmax(0,1fr) is required —
   bare 1fr keeps min-content and clips under the footer.
   Rows: masthead · above-floor (alarm+exceptions) · permanent · floor · footer. */
.room { position:relative; height:100%; min-height:0; display:grid;
        grid-template-rows:auto auto auto minmax(0,1fr) auto; z-index:1;
        overflow:hidden; }
.above-floor { flex:none; display:flex; flex-direction:column; min-height:0; }
/* Suite mast: mast | centered search cell | ops+doors
   Roster has no search yet — empty center cell keeps the same grid. */
.masthead { display:grid;
  grid-template-columns:minmax(0,1fr) minmax(200px,300px) minmax(0,1fr);
  align-items:center; gap:14px;
  padding:10px 16px; border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,var(--page),var(--chrome-end)); flex:none; }
.masthead h1 { font:700 clamp(16px,2.2vw,22px)/1.15 Georgia,"Times New Roman",serif;
  letter-spacing:.06em; text-transform:none; color:var(--ink); margin:0; }
.masthead h1 span.fn { color:var(--verd); font-weight:600; letter-spacing:.12em;
  text-transform:uppercase; font-size:0.72em; margin-left:0.35em; vertical-align:0.06em; }
.mast-sub { font-size:12px; color:var(--dim); letter-spacing:.02em;
  text-transform:none; margin-top:3px; }
/* oc-23/oc-24 city DNA: plat antenna landmark, re-inked for parchment */
.chrome-mast { display:flex; align-items:center; gap:12px; min-width:0; justify-self:start; }
.chrome-search { justify-self:center; width:100%; min-height:1px; }
.chrome-right { display:flex; align-items:center; gap:14px; justify-self:end; min-width:0; }
.mast-antenna { height:36px; width:auto; overflow:visible; flex:none; }
.mast-antenna .wire { stroke:var(--ink); fill:none; }
.mast-antenna .shed { stroke:var(--ink); fill:var(--parch-side); }
#mastBeacon { fill:var(--ok); }
#mastBeacon.down { fill:var(--fire); }
.cursor { display:none; }
.chrome-ops { flex:none; }
.sys-meta { font-size:11px; color:var(--dim); text-align:right; line-height:1.35;
  font-variant-numeric:tabular-nums; max-width:220px; }
.sys-meta .lamp { color:var(--dim); }
.sys-meta .lamp.on { color:var(--ok); animation:blink 1.2s step-end infinite; }
.you-attn { display:none; margin-left:10px; padding:3px 9px; border-radius:999px;
  font:700 11px/1.1 ui-monospace,Menlo,monospace; text-decoration:none;
  color:#5c3a08; background:linear-gradient(180deg,#ffe7a8,#f5c84a);
  border:1px solid #a8681e; animation:blink 2.4s step-end infinite; }
.you-attn.bump { animation:claimLand .9s ease-out; }
.sys-meta .lamp.err { color:var(--fire); animation:blink 1.2s step-end infinite; }
.sys-meta .ops { color:var(--dim); }
.suite-doors { display:flex; align-items:center; gap:8px; flex:none; }
.suite-doors:empty { display:none; }
.suite-doors a {
  color:var(--verd); font-size:12px; font-weight:700; text-decoration:none;
  border:1px solid var(--line); padding:5px 10px; border-radius:8px;
  background:var(--card); letter-spacing:.02em; white-space:nowrap;
}
.suite-doors a:hover { border-color:var(--verd); }
/* Theme + settings — D1 furniture entry from D0 chrome (not a suite peer). */
.settings-gear,
.theme-toggle {
  flex:none; width:34px; height:34px; display:inline-flex;
  align-items:center; justify-content:center;
  border:1px solid var(--line); border-radius:8px; background:var(--card);
  color:var(--dim); text-decoration:none; font-size:16px; line-height:1;
  cursor:pointer; padding:0; font-family:inherit;
}
.settings-gear:hover,
.theme-toggle:hover { border-color:var(--verd); color:var(--verd); }
#wallTime { color:var(--ink); font-variant-numeric:tabular-nums; }
@media (max-width:720px) {
  .masthead { grid-template-columns:1fr auto; }
  .chrome-mast { grid-column:1; }
  .chrome-right { grid-column:2; }
  .chrome-search { display:none; }
}
.alarm { min-height:26px; padding:5px 16px; background:var(--parch-top);
  border-bottom:1px solid var(--parch-side); font-size:.72rem; color:var(--dim);
  overflow:hidden; white-space:nowrap; display:flex; align-items:center; gap:20px;
  flex:none; }
.alarm .alert { color:var(--fire); font-weight:600; animation:blink .8s step-end infinite; }
.alarm .quiet { color:var(--dim); }
.ticker { display:inline-block; animation:marquee 30s linear infinite; }
/* Permanent seats sit above the hired floor — frozen, centered, independent.
   flex-start (not stretch): You must not inherit Office staff height. */
.permanent-row { flex:none; display:flex; flex-wrap:wrap; justify-content:center;
  align-items:flex-start; gap:12px; padding:10px 14px 8px;
  background:linear-gradient(180deg,var(--plaza),#c8bfa4);
  border-bottom:1px solid var(--parch-side); min-height:0; }
.permanent-row:empty { display:none; padding:0; border:0; }
.permanent-strip { border:1px solid var(--line); background:var(--card);
  border-radius:8px; box-shadow:0 1px 3px #0001; scroll-margin-top:12px;
  border-left:3px solid var(--verd); max-width:100%;
  height:fit-content; align-self:flex-start; }
.permanent-strip[data-role="you"] { border-left-color:var(--ink-deep); }
.permanent-strip .sector-roof .lock {
  font-size:.58rem; letter-spacing:.12em; text-transform:uppercase;
  color:var(--dim); font-weight:700; border:1px solid var(--line);
  padding:2px 7px; border-radius:4px; flex:none; }
.permanent-strip .sector-rooms {
  display:flex; flex-wrap:wrap; gap:8px; padding:10px 12px 12px;
  justify-content:center; background:var(--card); }
.permanent-strip .bay { width:220px; max-width:100%; flex:0 0 auto; }
/* Hired floor: even-height cabinet columns; card stacks scroll inside.
   Cabinet filter rail sits above the columns inside .floor. */
.floor { position:relative; min-height:0; overflow:hidden; padding:8px 14px 12px;
  background:linear-gradient(180deg,#c8bfa4,var(--walk));
  display:flex; flex-direction:column; align-items:stretch; gap:8px; }
.cabinet-rail { flex:none; display:flex; flex-wrap:wrap; align-items:center;
  gap:6px; padding:0 2px 2px; min-height:0; }
.cabinet-rail:empty { display:none; }
button.cab-chip {
  background:var(--card); border:1px solid var(--line); color:var(--ink);
  font:700 .62rem/1 Georgia,"Times New Roman",serif; letter-spacing:.06em;
  text-transform:none; cursor:pointer; border-radius:6px;
  padding:6px 10px; white-space:nowrap; }
button.cab-chip:hover { border-color:var(--verd); color:var(--verd); }
button.cab-chip.on { background:#e8f2ec; border-color:var(--verd); color:var(--ink-deep); }
button.cab-chip .eye { display:block; font-size:.5rem; letter-spacing:.12em;
  text-transform:uppercase; color:var(--dim); font-weight:700; margin-bottom:2px; }
.hired-floor { display:flex; flex-direction:row; flex-wrap:nowrap; gap:12px;
  align-items:stretch; align-content:stretch; overflow-x:auto; overflow-y:hidden;
  padding-bottom:4px; min-height:0; flex:1; width:100%; height:100%; }
.hired-floor .sector-bldg {
  flex:0 0 min(280px, 78vw); width:min(280px, 78vw); max-width:280px;
  height:100%; max-height:100%; display:flex; flex-direction:column;
  min-height:0; align-self:stretch; overflow:hidden; }
.hired-floor .sector-roof { flex:0 0 auto; }
.hired-floor .sector-rooms {
  display:flex; flex-direction:column; gap:8px; padding:10px 12px 12px;
  background:var(--card); overflow-x:hidden; overflow-y:auto; min-height:0;
  flex:1 1 auto; /* fill column; scroll when cards overflow */ }
/* Cards keep natural height so overflow scrolls — never squash to fit. */
.hired-floor .sector-rooms .bay { flex:0 0 auto; min-height:auto; }
/* Roster group — permanent strip OR cabinet/engine column */
.sector-bldg { border:1px solid var(--line); background:var(--card);
  border-radius:8px; box-shadow:0 1px 3px #0001; scroll-margin-top:12px; }
.sector-bldg:target { outline:2px solid var(--verd); outline-offset:2px; }
/* wf-53: stacked roof — name fully visible; Hire/meta on second row */
.sector-roof { display:flex; flex-direction:column; align-items:stretch;
  gap:6px; padding:8px 12px 8px; background:linear-gradient(180deg,var(--parch-top),var(--parch-front));
  border-bottom:1px solid var(--line); color:var(--ink); }
.sector-roof .shape { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 8px;
  min-width:0; width:100%; }
.sector-roof .eyebrow { font-size:.58rem; letter-spacing:.14em; text-transform:uppercase;
  color:var(--dim); font-weight:700; flex:none; }
.sector-roof .title { font:700 .95rem/1.25 Georgia,"Times New Roman",serif;
  color:var(--ink-deep); letter-spacing:.02em; white-space:normal;
  overflow:visible; text-overflow:unset; text-transform:none; min-width:0; flex:1 1 auto; }
.sector-roof .q { color:var(--dim); letter-spacing:.02em; flex:none; font-size:.68rem; }
.sector-roof .roof-actions { display:flex; align-items:center; gap:8px; flex:none;
  width:100%; justify-content:space-between; }
button.hire-btn {
  background:var(--card); border:1px solid var(--verd); color:var(--verd);
  font:700 .62rem/1 Georgia,"Times New Roman",serif; letter-spacing:.08em;
  text-transform:uppercase; cursor:pointer; border-radius:6px;
  padding:5px 10px; white-space:nowrap; }
button.hire-btn:hover { background:#e8f2ec; color:var(--ink); }
.sector-rooms { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
  gap:8px; padding:10px 12px 12px; justify-content:start; background:var(--card); }
/* Hire drawer — reuses personnel-file chrome */
.hire-form { display:flex; flex-direction:column; gap:10px; margin-top:8px; }
.hire-form label { display:flex; flex-direction:column; gap:3px;
  font-size:.62rem; letter-spacing:.1em; text-transform:uppercase; color:var(--dim); }
.hire-form input, .hire-form select {
  font:14px/1.35 Georgia,"Times New Roman",serif; color:var(--ink-deep);
  border:1px solid var(--line); border-radius:6px; padding:7px 9px;
  background:var(--card); letter-spacing:0; text-transform:none; }
.hire-form .row2 { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.hire-form .hint { font-size:.66rem; color:var(--dim); letter-spacing:0;
  text-transform:none; line-height:1.35; }
.hire-form .err { color:var(--fire); font-size:.7rem; letter-spacing:0;
  text-transform:none; min-height:1.2em; }
button.hire-submit {
  margin-top:6px; padding:9px 0; width:100%;
  background:var(--verd); border:1px solid var(--verd); color:#fff;
  font:700 .72rem/1 Georgia,"Times New Roman",serif; letter-spacing:.08em;
  text-transform:uppercase; cursor:pointer; border-radius:6px; }
button.hire-submit:hover { background:#2f5f53; }
button.hire-submit:disabled { opacity:.55; cursor:default; }
.hire-next { margin-top:12px; font-size:.7rem; color:var(--ink); line-height:1.45; }
.hire-next li { margin:4px 0 0 1.1em; }
/* Roster card — employment line, not a CRT toy.
   States: on | imminent | waiting | fault (.err). */
.bay { position:relative; background:var(--parch-top); border:1px solid var(--line);
  border-radius:6px; box-shadow:0 1px 2px #0001; overflow:hidden;
  transition:border-color .2s, box-shadow .2s; display:flex; flex-direction:column;
  min-height:0; }
.bay.on { border-color:var(--ok); box-shadow:0 0 0 1px #2e7d4f33; }
.bay.err { border-color:var(--fire); box-shadow:0 0 0 1px #a3332733; }
.bay.imminent { border-color:var(--warn); }
.bay.waiting { border-color:var(--line); }
.bay.dim { opacity:.82; }
.bay.inflight { border-color:var(--ok); background:#f3f8f4; }
/* On-shift / in-flight: figure bobs (CSS only — tick() stays countdown/chip). */
.bay.on .figure-slot,
.bay.inflight .figure-slot {
  overflow:visible;
  animation:bob .55s ease-in-out infinite; }
.bay.err .figure-slot {
  overflow:visible;
  animation:bob .4s steps(2) infinite; }
.bay-top { display:flex; align-items:center; gap:8px; padding:8px 10px 6px;
  background:transparent; border:0; }
/* Persona icon — bare sprite, no enclosing circle (larger than the old ring inset). */
.figure-slot { width:36px; height:36px; display:flex; align-items:center;
  justify-content:center; border:none; border-radius:0; background:transparent;
  position:relative; overflow:visible; flex:none; }
.figure-slot .figure { width:34px; height:34px; display:block; }
.figure-slot .floor-dot { display:none; }
.bay-id { flex:1; min-width:0; display:flex; flex-direction:column; gap:2px; }
.bay-name { font-size:.84rem; letter-spacing:.01em; color:var(--ink-deep);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:700; }
.bay-name a { color:var(--ink-deep); text-decoration:none; }
.bay-name a:hover { color:var(--verd); }
.bay-sub { font-size:.62rem; color:var(--dim);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.bay-pay { font-size:.56rem; color:var(--dim); letter-spacing:.02em;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  font-variant-numeric:tabular-nums; opacity:.88; }
.status-chip { flex:none; font-size:.58rem; font-weight:700; letter-spacing:.08em;
  text-transform:uppercase; padding:3px 7px; border-radius:999px;
  border:1px solid var(--line); color:var(--dim); background:var(--card); }
.status-chip.on { color:var(--ok); border-color:#2e7d4f66; background:#e8f2ec; }
.status-chip.fault { color:var(--fire); border-color:#a3332766; background:#f8ecea; }
.status-chip.imminent { color:var(--warn); border-color:#a8681e66; background:#f7f0e4; }
.status-chip.idle { color:var(--dim); }
/* Facts — ink on paper; answers working-on + next fire */
.bay-body { padding:0 10px 8px; font:12px/1.4 Georgia,"Times New Roman",serif;
  color:var(--ink); background:transparent; box-shadow:none; flex:1; }
.bay-body .work-line { font-size:.72rem; color:var(--ink); margin:0 0 6px;
  text-decoration:none; display:block; }
a.bay-body .work-line, .bay-body a.work-line { color:var(--verd); }
.bay-body a.work-line:hover { text-decoration:underline; }
.bay-body .work-line {
  min-height:2.1em; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
  overflow:hidden; }
.bay-body .work-line .muted { color:var(--dim); }
.bay-body .work-line.bad { color:var(--fire); }
.bay-body .work-line.hot { color:var(--ok); font-weight:600; }
.bay.claim-land {
  box-shadow:0 0 0 2px #3d7a6a88, 0 4px 12px #2a241c22;
  animation:claimLand .95s ease-out;
}
.bay.claim-land .work-line.hot {
  animation:claimLandText .95s ease-out;
}
@keyframes claimLand {
  0% { box-shadow:0 0 0 4px #3d7a6acc, 0 0 0 0 #3d7a6a00; transform:translateY(-2px); }
  100% { box-shadow:0 0 0 0 #3d7a6a00; transform:none; }
}
@keyframes claimLandText {
  0% { transform:scale(1.04); }
  100% { transform:none; }
}
.meta-row { display:flex; justify-content:space-between; align-items:baseline;
  gap:8px; font-size:.68rem; color:var(--dim); font-variant-numeric:tabular-nums; }
.meta-row .k { letter-spacing:.04em; text-transform:uppercase; font-size:.58rem; }
.meta-row .v { color:var(--ink-deep); font-weight:600; }
.meta-row .v.hot { color:var(--warn); }
.meta-row .v.bad { color:var(--fire); }
.cd { font-size:.78rem; letter-spacing:.04em; color:var(--ink-deep);
  font-variant-numeric:tabular-nums; font-weight:700; }
.cd.imminent { color:var(--warn); }
.cd.firing { color:var(--ok); }
.cd.hold { color:var(--dim); font-weight:600; }
.progress { margin-top:4px; height:2px; background:var(--paper); border-radius:1px;
  overflow:hidden; }
.progress > i { display:block; height:100%; width:0; background:var(--verd);
  transition:width .8s linear; }
.bay.on .progress > i { background:var(--ok); width:100%!important; }
.log { margin-top:4px; font-size:.66rem; color:var(--dim); line-height:1.3; }
.log a { color:var(--verd); font-weight:700; text-decoration:none; }
/* compat rows if drawer reuses .r */
.r { display:flex; justify-content:space-between; gap:6px; padding:1px 0; font-size:.72rem; }
.r .k { color:var(--dim); } .r .v { color:var(--ink-deep); text-align:right;
  font-variant-numeric:tabular-nums; }
.r .v.hot { color:var(--warn); } .r .v.bad { color:var(--fire); } .r .v.good { color:var(--ok); }
.empty { color:var(--dim); padding:40px; text-align:center; letter-spacing:.1em; }
/* Footer = room verbs + census. Desk traffic stays on Desk (suite boundary). */
footer.bar { flex:none; border-top:1px solid var(--line); padding:8px 16px; display:flex;
  justify-content:space-between; align-items:center; gap:16px; font-size:11px;
  color:var(--dim); background:#ebe4d4; letter-spacing:.02em; min-height:36px; }
footer.bar .foot-verbs { display:flex; align-items:center; gap:0; flex-wrap:wrap; min-width:0; }
footer.bar a { color:var(--verd); text-decoration:none; margin-left:14px; font-weight:700; }
footer.bar a:hover { color:var(--ink); }
footer.bar a.first { margin-left:0; }
footer.bar a.quiet-link { color:var(--dim); font-weight:600; }
footer.bar a.quiet-link:hover { color:var(--verd); }
footer.bar .foot-sum { color:var(--dim); font-variant-numeric:tabular-nums;
  white-space:nowrap; flex:none; }
/* Exception vitals — who/when health signals only; burn & steady live on /report */
.overview-bay { flex:none; border-bottom:1px solid var(--parch-side);
  background:linear-gradient(180deg, var(--parch-top) 0%, var(--parch-front) 100%);
  padding:4px 14px 6px; }
.overview-bay.clear { display:none; } /* safe: parent .above-floor keeps grid tracks */
.overview-bay .ov-head { display:flex; align-items:baseline; justify-content:space-between;
  gap:12px; margin-bottom:3px; }
.overview-bay .ov-title { font-size:.58rem; letter-spacing:.18em; text-transform:uppercase;
  color:var(--dim); font-weight:700; }
.overview-bay .ov-more { font-size:.62rem; color:var(--verd); text-decoration:none; font-weight:700; }
.overview-bay .ov-more:hover { color:var(--ink); }
.overview-bay .ov-tiles { display:flex; flex-wrap:wrap; gap:6px; }
.overview-bay .ov-tile { flex:1 1 100px; min-width:90px; max-width:180px;
  border:1px solid var(--parch-side); background:var(--parch-top);
  border-radius:3px; padding:4px 7px 5px; cursor:pointer; text-align:left;
  font:inherit; color:inherit; }
.overview-bay .ov-tile:hover { border-color:var(--verd); }
.overview-bay .ov-tile.on { border-color:var(--verd); box-shadow:0 0 0 1px var(--verd); }
.bay.focus-dim { opacity:.34; filter:grayscale(.15); }
.bay.focus-hit { outline:1.5px solid var(--verd); outline-offset:2px; }
.q-hit { background:none; border:0; padding:0; margin:0; font:inherit; color:var(--verd);
  cursor:pointer; text-decoration:underline; text-underline-offset:2px; }
a.q-hit { color:var(--verd); }
.q-hit.stuck, button.q-hit.stuck { color:var(--fire); }
#focusBanner { display:none; padding:6px 14px; font:600 .72rem/1.4 Georgia,serif;
  letter-spacing:.04em; background:#f5efe3; border-bottom:1px solid var(--line,#c4b8a4);
  color:#2a241c; }
#focusBanner.on { display:block; }
#focusBanner a { color:var(--verd,#3d7a6a); }
.overview-bay .ov-tile .k { display:block; font-size:.52rem; letter-spacing:.12em;
  text-transform:uppercase; color:var(--dim); margin-bottom:1px; }
.overview-bay .ov-tile .v { display:block; font-size:.78rem; font-weight:700;
  color:var(--ink-deep); letter-spacing:.03em; }
.overview-bay .ov-tile .s { display:block; font-size:.56rem; color:var(--dim);
  margin-top:1px; line-height:1.25; }
.overview-bay .ov-tile.warn .v { color:var(--warn); }
.overview-bay .ov-tile.err .v { color:var(--fire); }
.overview-bay .ov-tile.ok .v { color:var(--ok); }
.overview-bay .ov-quiet { font-size:.66rem; color:var(--dim); font-style:italic; }
/* Dispatch lives on the bay */
button.callin { display:block; width:100%; margin-top:7px; padding:6px 0;
  background:var(--card); border:1px solid var(--verd); color:var(--verd);
  font:700 .68rem/1 Georgia,"Times New Roman",serif; letter-spacing:.06em;
  text-transform:uppercase; cursor:pointer; border-radius:6px; }
button.callin:hover { background:#e8f2ec; color:var(--ink); }
button.callin:disabled { color:var(--dim); border-color:var(--line); cursor:default;
  background:transparent; }
/* personnel-file drawer restyled to parchment */
#pfscrim { position:fixed; inset:0; background:#2b262066; opacity:0;
  pointer-events:none; transition:opacity .18s; z-index:200; }
#pfscrim.open { opacity:1; pointer-events:auto; }
#pf { position:fixed; top:0; right:0; bottom:0; width:min(540px,94vw);
  background:var(--parch-top); border-left:1.5px solid var(--ink);
  box-shadow:-8px 0 24px #0003, inset 0 1px 0 #fff8;
  transform:translateX(103%); transition:transform .22s ease-out; z-index:201;
  display:flex; flex-direction:column; color:var(--ink); }
#pf.open { transform:translateX(0); }
.pf-head { padding:14px 16px 10px; border-bottom:1px solid var(--parch-side);
  position:relative; background:var(--parch-front); }
.pf-head .file-no { font-size:.6rem; letter-spacing:.24em; color:var(--dim);
  text-transform:uppercase; }
.pf-head .nm { font-size:1.05rem; color:var(--ink-deep); letter-spacing:.08em;
  margin-top:2px; font-weight:600; }
.pf-head .sub { font-size:.68rem; color:var(--dim); margin-top:2px; }
.pf-close { position:absolute; top:10px; right:10px; background:none;
  border:1px solid var(--ink); color:var(--ink); width:24px; height:24px;
  cursor:pointer; font:inherit; line-height:1; }
.pf-close:hover { border-color:var(--ink-deep); color:var(--ink-deep); }
.pf-body { flex:1; overflow-y:auto; padding:12px 16px 16px; font-size:.74rem; }
.pf-sec { margin:14px 0 6px; font-size:.62rem; letter-spacing:.22em;
  color:var(--dim); text-transform:uppercase;
  border-bottom:1px solid var(--parch-side); padding-bottom:3px; }
.pf-law { width:100%; border-collapse:collapse; font-size:.68rem; }
.pf-law td { padding:3px 4px; border-bottom:1px dotted var(--parch-side);
  vertical-align:top; }
.pf-law td.lv { color:var(--ink-deep); white-space:nowrap; font-weight:600; }
.pf-law a { color:var(--ok); text-decoration:none; }
.pf-law a:hover { text-decoration:underline; }
.pf-law .sha { color:var(--dim); font-size:.62rem; }
.pf-shift { padding:4px 0; border-bottom:1px dotted var(--parch-side);
  font-size:.68rem; }
.pf-shift .ok { color:var(--ok); } .pf-shift .err { color:var(--fire); }
.pf-shift .amber { color:var(--warn); } .pf-shift .dim { color:var(--dim); }
.pf-foot { padding:8px 16px; border-top:1px solid var(--parch-side);
  font-size:.66rem; color:var(--dim); background:var(--parch-front); }
.pf-foot a { color:var(--ink-deep); text-decoration:none; margin-right:12px; }
.pf-foot a:hover { color:var(--ok); }
/* drawer reuses .r rows — ink on parchment, not phosphor */
#pf .r .k { color:var(--dim); } #pf .r .v { color:var(--ink-deep); }
#pf .r .v.bad { color:var(--fire); } #pf .r .v.good { color:var(--ok); }
#pf .r .v.hot { color:var(--warn); text-shadow:none; }
/* Runtimes strip — installed staffing pool at a glance */
.runtimes-strip { display:flex; flex-wrap:wrap; align-items:center; gap:6px;
  padding:5px 14px; border-top:1px solid var(--line); }
.runtimes-strip:empty { display:none; }
.rt-label { font-size:.58rem; color:var(--dim); text-transform:uppercase;
  letter-spacing:.07em; flex:none; margin-right:2px; }
.rt-chip { font-size:.6rem; padding:1px 7px; border-radius:3px;
  border:1px solid var(--line); color:var(--dim); cursor:default; }
.rt-chip.available { border-color:var(--ok); color:var(--ok); }
.rt-chip.absent { opacity:.35; }
/* wf-77: live shift-output tail in personnel-file drawer */
.pf-tail { font:11px/1.4 ui-monospace,Menlo,monospace;
  background:var(--crt-bg,#0a120a); color:var(--crt-ok,#33ff66);
  border:1px solid var(--line); border-radius:4px; padding:10px;
  max-height:200px; overflow-y:auto; white-space:pre-wrap;
  word-break:break-all; margin-top:4px; }
.pf-tail .pf-tail-note { color:var(--dim); font-style:italic; }
"""

# setInterval only — requestAnimationFrame suspends in background panes (the
# constraint the founder proved live in the prototype; verdict 59).
SCENE_JS = """
<script>
/* Suite dark/light — shared with Office / Desk (protocolcity-theme). */
(function(){
  var KEY='protocolcity-theme';
  function theme(){
    try{ var t=localStorage.getItem(KEY)||'light'; return t==='dark'?'dark':'light'; }
    catch(e){ return 'light'; }
  }
  function apply(t){
    t=(t==='dark')?'dark':'light';
    document.documentElement.setAttribute('data-theme', t);
    try{ localStorage.setItem(KEY,t); localStorage.setItem('tp-theme',t); }catch(e){}
    var btn=document.getElementById('theme-toggle');
    if(btn){
      btn.textContent = t==='dark' ? '\\u2600' : '\\u263D';
      btn.title = t==='dark' ? 'Switch to light theme' : 'Switch to dark theme';
      btn.setAttribute('aria-label', btn.title);
    }
  }
  function toggle(){ apply(theme()==='dark'?'light':'dark'); }
  document.addEventListener('DOMContentLoaded', function(){
    apply(theme());
    var btn=document.getElementById('theme-toggle');
    if(btn) btn.addEventListener('click', toggle);
  });
  /* Also apply immediately if body already present. */
  apply(theme());
  var btn0=document.getElementById('theme-toggle');
  if(btn0) btn0.addEventListener('click', toggle);
})();

"use strict";
var STATE=null, RAW_SCENE=null, TICK=0;
/* Claim-land flash: first time we see a holding id for an in-flight worker. */
var HOLDING_SEEN={}; var HOLDING_PRIMED=false;
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function $(id){return document.getElementById(id);}
function parseTs(iso){if(!iso)return null;var t=Date.parse(iso);return isNaN(t)?null:t;}
function p2(n){return (n<10?"0":"")+n;}
function fmtZ(d){return p2(d.getUTCHours())+":"+p2(d.getUTCMinutes())+":"+p2(d.getUTCSeconds())+"Z";}
function fmtL(d){return p2(d.getHours())+":"+p2(d.getMinutes())+":"+p2(d.getSeconds());}
function rel(iso){var t=parseTs(iso);if(t==null)return "—";
  var s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<60)return s+"s ago"; if(s<3600)return (s/60|0)+"m ago";
  if(s<86400)return (s/3600|0)+"h ago"; return (s/86400|0)+"d ago";}
function onShift(w){
  if(w.last_shift && w.last_shift.outcome==="running")return true;
  var t=parseTs(w.last_shift && w.last_shift.ts);
  if(t!=null && (Date.now()-t)<5*60000)return true;
  var nf=parseTs(w.next_fire);
  if(nf!=null && Date.now()>=nf && (Date.now()-nf)<5*60000)return true;
  return false;}
/* oc-26: four embodied bay states re-derived each setInterval tick from
   polled facts (in_flight, next_fire, last_shift, health). Never rAF. */
function inFlight(w){
  return !!(STATE && STATE.in_flight && w &&
    STATE.in_flight.indexOf(w.name)>=0);}
function isPaused(w){
  /* Daemon-owned iff schedule is five-field cron (schedule.maybe_cron).
     "paused (...)" and other prose → not owned — surface as PAUSED, not IDLE. */
  if(!w)return false;
  if(w.owned===false||w.owned===0){
    var sch=String(w.schedule||"").trim().toLowerCase();
    if(!sch||sch==="manual")return false; /* citizen / manual seats */
    return sch.split(/\s+/).length!==5;
  }
  return false;}
function workerState(w){
  if(!w)return "waiting";
  if(w.health==="err"||w.health==="wedged")return "fault";
  if(onShift(w)||inFlight(w))return "on";
  if(isPaused(w))return "paused";
  var nf=parseTs(w.next_fire);
  if(nf!=null){var s=Math.floor((nf-Date.now())/1000);
    if(s>0 && s<=120)return "imminent";}  /* ~2 min indoor commute */
  return "waiting";}
function bayClass(w){
  var st=workerState(w), cls="bay";
  if(st==="fault")cls+=" err";
  else if(st==="on")cls+=" on";
  else if(st==="imminent")cls+=" imminent";
  else if(st==="paused")cls+=" waiting dim";
  else {cls+=" waiting"; if(w.health==="dim")cls+=" dim";}
  if(inFlight(w))cls+=" inflight";
  return cls;}
/* commute progress 0→1 over the 2-min imminent window (plat-style walk) */
function commuteP(w){
  var nf=parseTs(w.next_fire); if(nf==null)return 0;
  var rem=nf-Date.now(); if(rem<=0)return 1;
  if(rem>=120000)return 0;
  return 1-rem/120000;}
function countdown(iso){
  var t=parseTs(iso);
  if(t==null)return {text:"standing by",cls:"cd hold"};
  var s=Math.floor((t-Date.now())/1000);
  if(s<=0 && s>-300)return {text:"\\u25B6 DISPATCHING",cls:"cd firing"};
  if(s<=0)return {text:"OVERDUE",cls:"cd imminent"};
  var h=s/3600|0, m=(s%3600)/60|0, sec=s%60;
  return {text:(h>0?p2(h)+":":"")+p2(m)+":"+p2(sec),
          cls:s<120?"cd imminent":"cd"};}
function period(sch){
  if(!sch)return 3600; var f=String(sch).trim().split(/\\s+/), min=f[0];
  if(min==="*")return 60;
  if(min.indexOf("*/")===0){var st=parseInt(min.slice(2),10); if(st>0)return st*60;}
  if(min.indexOf(",")>=0){var xs=min.split(",").map(function(x){return parseInt(x,10);})
    .filter(function(x){return !isNaN(x);}).sort(function(a,b){return a-b;});
    if(xs.length>=2){var g=[]; for(var i=1;i<xs.length;i++)g.push(xs[i]-xs[i-1]);
      g.push(60-xs[xs.length-1]+xs[0]); var mn=Math.min.apply(null,g); if(mn>0)return mn*60;}}
  if(/^\\d+$/.test(min))return (f[1] && f[1]!=="*")?86400:3600;
  return 3600;}
/* oc-27: cron speaks plain — five-field roster subset only; unparseable → RAW.
   pin samples (marker test): "45 * * * *"→"hourly at :45" · "10,40 * * * *"→
   "hourly at :10 and :40" · "0,30 * * * *"→"every 30 min" · "0 8 * * 1"→
   "Mondays 08:00" · "0 9 * * *"→"daily 09:00" · "manual"→"manual". */
var _CRON_SPEECH_PIN="hourly at :45";
function cronSpeech(expr){
  if(expr==null||String(expr).trim()===""||String(expr).trim().toLowerCase()==="manual")
    return "manual";
  var raw=String(expr).trim(), f=raw.split(/\\s+/);
  if(f.length!==5)return raw;
  var min=f[0], hour=f[1], dom=f[2], mon=f[3], dow=f[4];
  if(dom!=="*"||mon!=="*")return raw;
  function p2(n){n=parseInt(n,10); return (n<10?"0":"")+n;}
  function minBits(m){
    if(/^\\d+$/.test(m)){var v=parseInt(m,10); if(v<0||v>59)return null; return {one:":"+p2(v)};}
    if(!/^\\d+(,\\d+)+$/.test(m))return null;
    var xs=m.split(",").map(function(x){return parseInt(x,10);});
    for(var i=0;i<xs.length;i++){
      if(isNaN(xs[i])||xs[i]<0||xs[i]>59)return null;
      if(i>0&&xs[i]<=xs[i-1])return null;}
    // even spacing covering the hour from :00 → "every N min" (e.g. 0,30)
    if(xs[0]===0&&xs.length>=2){
      var gap=xs[1]-xs[0], even=true;
      for(var j=1;j<xs.length;j++)if(xs[j]-xs[j-1]!==gap)even=false;
      if(even&&gap>0&&(60%gap)===0&&xs.length===(60/gap))return {every:gap};}
    var parts=xs.map(function(x){return ":"+p2(x);});
    if(parts.length===2)return {list:parts[0]+" and "+parts[1]};
    var last=parts.pop(); return {list:parts.join(", ")+" and "+last};}
  var DOW=["Sundays","Mondays","Tuesdays","Wednesdays","Thursdays","Fridays","Saturdays"];
  if(hour==="*"&&dow==="*"){
    var mb=minBits(min); if(!mb)return raw;
    if(mb.one)return "hourly at "+mb.one;
    if(mb.every)return "every "+mb.every+" min";
    if(mb.list)return "hourly at "+mb.list;
    return raw;}
  if(/^\\d+$/.test(min)&&/^\\d+$/.test(hour)){
    var hh=parseInt(hour,10), mm=parseInt(min,10);
    if(hh<0||hh>23||mm<0||mm>59)return raw;
    var t=p2(hh)+":"+p2(mm);
    if(dow==="*")return "daily "+t;
    if(/^\\d+$/.test(dow)){var d=parseInt(dow,10); if(d>=0&&d<=6)return DOW[d]+" "+t;}
    return raw;}
  return raw;}
function pct(w){
  if(onShift(w))return 100; var nf=parseTs(w.next_fire); if(nf==null)return 0;
  var rem=nf-Date.now(); if(rem<=0)return 100;
  var per=period(w.schedule)*1000, el=per-rem; if(el<0)el=0;
  return Math.max(0,Math.min(100,Math.round(el/per*100)));}
/* City DNA: identity registry shared with the plat —
   same hash, same palette, same little person on every surface. */
var DNA_PALETTE=["#3d7a6a","#a8842c","#4a6fa5","#7d5185","#a35b3a","#5f7d3a"];
function dnaHash(s){var h=0,i;for(i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return Math.abs(h);}
function dnaColor(name){return DNA_PALETTE[dnaHash(String(name||""))%DNA_PALETTE.length];}
function figure(w){
  /* Portrait chip only — CITY_DNA identity color, no laptop/CRT vignette. */
  var h=w.health||"ok", ink=h==="err"||h==="wedged"?"#a33327":"#4a3f2c";
  var tor=dnaColor(w.name);
  return '<svg class="figure" width="28" height="28" viewBox="-8 -24 16 28" aria-hidden="true">'+
    '<line x1="-2" y1="-4" x2="-2" y2="0" stroke="'+ink+'" stroke-width="2"/>'+
    '<line x1="2" y1="-4" x2="2" y2="0" stroke="'+ink+'" stroke-width="2"/>'+
    '<rect x="-4" y="-14" width="8" height="11" rx="2.5" fill="'+tor+'"'+
      (h==="err"||h==="wedged"?' stroke="#a33327" stroke-width=".8"':'')+'/>'+
    '<circle cx="0" cy="-18" r="4.2" fill="#d9b98c" stroke="#4a3f2c" stroke-width=".7"/>'+
    '</svg>';}
function statusLabel(w){
  var st=workerState(w);
  if(st==="fault") return (w.health==="wedged")?"WEDGED":"FAULT";
  if(st==="on") return "ON SHIFT";
  if(st==="imminent") return "DUE SOON";
  if(st==="paused") return "PAUSED";
  if(w.health==="dim") return "QUIET";
  return "IDLE";}
function statusChipClass(w){
  var st=workerState(w);
  if(st==="fault") return "fault";
  if(st==="on") return "on";
  if(st==="imminent") return "imminent";
  if(st==="paused") return "idle";
  return "idle";}
function kindWord(w){
  var k=String(w.kind||"").toLowerCase();
  if(k==="citizen") return "you";
  if(k==="job"||k==="agent") return "agent";
  if(k==="service") return "service";
  if(k==="worker"||k==="lane") return "worker";
  return k||"unit";}
function splitPersona(w){
  var raw=(w.display&&String(w.display).trim())?String(w.display).trim():w.name;
  var i=raw.indexOf(" \\u00b7 ");
  if(i<0) i=raw.indexOf(" · ");
  if(i>0) return {who:raw.slice(0,i), role:raw.slice(i+3)};
  return {who:raw, role:kindWord(w)};}
function deskBaseUrl(){
  return String(window.DESK_URL||"http://127.0.0.1:8799").replace(/\\/$/,"");
}
function deskFilterHref(cab, status){
  var q=[];
  if(cab) q.push("cabinet="+encodeURIComponent(cab));
  if(status) q.push("status="+encodeURIComponent(status));
  return deskBaseUrl()+"/admin/desk"+(q.length?("?"+q.join("&")):"");
}
function workerCabSlug(w){
  var wd=String((w&&w.workdir)||"").replace(/\\\\/g,"/");
  return (wd.split("/").filter(Boolean).pop()||"").toLowerCase();
}
function payrollLine(w){
  /* Quiet subtitle: CLI · model pin (payroll metadata, not the persona). */
  if(!w||String(w.kind||"")==="citizen"||w.no_clock_in)return "";
  var cli=String(w.cli||"").trim();
  var model=String(w.model||"").trim();
  if(model==="default") model="";
  if(cli&&model) return cli+" \\u00b7 "+model;
  return cli||model||"";
}
function workingOn(w){
  /* Plain English: what they're doing / what's waiting / why stuck. */
  if(inFlight(w)||onShift(w)){
    /* Prefer live Desk claim when scene carried a holding teaser (in_flight only). */
    var held=w.holding||[];
    if(held.length){
      var t=held[0]||{};
      var st=String(t.status||"");
      var verb=st==="in_review"?"review":"doing";
      var more=held.length>1?(" +"+String(held.length-1)+" more"):"";
      var title=t.title?(" \\u00b7 "+String(t.title).slice(0,48)):"";
      return {text:verb+" "+(t.id||"?")+title+more, cls:"hot",
              claimHref:t.href||"", claimId:t.id||""};
    }
    var ls=w.last_shift||{};
    var r=ls.reason?String(ls.reason):"";
    return {text: r?("on shift \\u00b7 "+r):"on shift now", cls:"hot"};
  }
  if(isPaused(w)){
    var why=String(w.schedule||"").trim();
    /* Prefer the parenthetical reason after "paused" when present. */
    var m=why.match(/^paused\\s*(?:\\((.*)\\))?/i);
    var detail=(m&&m[1])?m[1].trim():(why&&why.toLowerCase().indexOf("paused")===0?why:"schedule not armed");
    return {text:"paused \\u00b7 "+detail, cls:"muted"};
  }
  if(w.health==="wedged"||w.health==="err"){
    return {text:w.why||((w.last_shift&&w.last_shift.reason)||"needs attention"), cls:"bad"};
  }
  var q=parseInt(w.queue,10);
  if(!isNaN(q)&&q>0) return {text:q+" ready", cls:"", deskReady:q};
  if(w.last_shift&&w.last_shift.outcome){
    var o=w.last_shift.outcome, reason=w.last_shift.reason?(" \\u00b7 "+w.last_shift.reason):"";
    return {text:"last "+o+reason, cls:"muted"};
  }
  return {text:"standing by", cls:"muted"};}
function bay(w){
  /* Roster card: who · status · working on · next fire · dispatch. */
  var st=workerState(w), on=st==="on", cd=countdown(w.next_fire);
  var cls=bayClass(w);
  var persona=splitPersona(w);
  var isCitizen=String(w.kind||"")==="citizen"||w.no_clock_in;
  var link=isCitizen
    ?(w.href?('<a href="'+esc(w.href)+'">'+esc(persona.who)+'</a>'):esc(persona.who))
    :('<a href="/worker/'+esc(w.name)+'">'+esc(persona.who)+'</a>');
  var callin=(window.CAN_DISPATCH&&!isCitizen&&st!=="on")
    ?('<button class="callin" data-name="'+esc(w.name)+'">Dispatch</button>'):'';
  var work=workingOn(w);
  var workCls="work-line"+(work.cls?(" "+work.cls):"");
  var cab=workerCabSlug(w);
  var workHtml;
  if(work.claimHref){
    workHtml='<a class="'+workCls+'" href="'+esc(work.claimHref)+
      '" target="_blank" rel="noopener" title="Open claim on Desk">'+esc(work.text)+'</a>';
  }else if(work.deskReady){
    workHtml='<a class="'+workCls+'" href="'+esc(deskFilterHref(cab,"backlog"))+
      '" title="Open Desk ready pile">'+esc(work.text)+'</a>';
  }else{
    workHtml='<div class="'+workCls+'" title="'+esc(w.why||work.text)+'">'+esc(work.text)+'</div>';
  }
  var pay=payrollLine(w);
  var nextLabel=on?"Now":(st==="imminent"?"Due":"Next");
  var stuck=(w.health==="wedged"||w.health==="err")?"1":"0";
  var ready=work.deskReady?"1":"0";
  var body=isCitizen
    ?('<div class="log">You own this Office — open <a href="'+esc(w.href||"http://127.0.0.1:8796/")+'">Office</a></div>')
    :(
      workHtml+
      '<div class="meta-row"><span class="k">'+nextLabel+'</span>'+
        '<span class="'+cd.cls+'" data-nf="'+esc(w.next_fire||"")+'">'+esc(cd.text)+'</span></div>'+
      '<div class="progress"><i style="width:'+pct(w)+'%"></i></div>'+
      callin
    );
  return '<article class="'+cls+'" data-worker="'+esc(w.name)+
    '" data-stuck="'+stuck+'" data-ready="'+ready+'" data-cab="'+esc(cab)+'">'+
    '<div class="bay-top">'+
      '<div class="figure-slot">'+figure(w)+'</div>'+
      '<div class="bay-id"><div class="bay-name">'+link+'</div>'+
        '<div class="bay-sub">'+esc(persona.role)+'</div>'+
        (pay?'<div class="bay-pay" title="payroll runtime">'+esc(pay)+'</div>':'')+
        '</div>'+
      '<span class="status-chip '+statusChipClass(w)+'">'+esc(statusLabel(w))+'</span>'+
    '</div>'+
    '<div class="bay-body">'+body+'</div></article>';}
document.addEventListener("click",function(e){
  var b=e.target&&e.target.closest?e.target.closest("button.callin"):null; if(!b)return;
  var name=b.getAttribute("data-name")||"";
  if(!confirm("Dispatch "+name+" now? This runs a real desk run."))return;
  b.disabled=true; b.textContent="Dispatching\\u2026";
  fetch("/api/dispatch/"+encodeURIComponent(name),{method:"POST"})
    .then(function(r){return r.json();}).then(function(d){
      if(!d.ok)alert(name+": "+(d.msg||"dispatch refused"));
      b.textContent=d.ok?"Dispatched":"Dispatch";
      setTimeout(function(){b.disabled=false;b.textContent="Dispatch";poll();},1500);
    }).catch(function(err){b.disabled=false;b.textContent="Dispatch";
      alert("dispatch failed: "+err);});});
/* wf-47: wall clock only — plat sky/sun band retired with the cabinet pivot. */
/* Walk-in seam (CITY_DNA sec.5): sector-<slug> where slug matches the city
   lens neighborhood slug = workdir basename lowercased (not the display
   workplace name — a folder displays under its public name). */
function sectorSlug(s){
  var path=String((s&&s.workdir)||(s&&s.workplace)||"");
  var base=path.replace(/\\\\/g,"/").split("/").filter(Boolean).pop()||"";
  return base.toLowerCase();}
function sectorEyebrow(s){
  var role=String((s&&s.role)||"business");
  return {you:"You",staff:"Office staff",civic:"Office staff",
          engine:"Engine",business:"Cabinet"}[role]||"Cabinet";}
function sectorTitle(s){
  /* Cabinet name leads. Role lives in the eyebrow (You / Office staff /
     Cabinet / Engine) — not as a title prefix. */
  var role=String((s&&s.role)||"business");
  var name=String((s&&s.workplace)||"?");
  if(role==="you") return "You";
  if(role==="staff"||role==="civic") return "Office staff";
  if(role==="engine") return name||"WorkForce";
  return name; /* WorkLane, your products… */
}
/* wf-53: All + cabinet chips → ?cabinet= (same applyScope as Office deep links) */
function hiredCabinetList(d){
  var out=[], seen={};
  ((d&&d.sectors)||[]).forEach(function(s){
    var role=String(s.role||"business");
    if(role==="you"||role==="staff"||role==="civic")return;
    if(!(s.workers||[]).length && role!=="business" && role!=="engine")return;
    var slug=sectorSlug(s)||String(s.workplace||"").toLowerCase();
    if(!slug||seen[slug])return;
    seen[slug]=1;
    out.push({slug:slug, title:sectorTitle(s), eye:sectorEyebrow(s),
              workplace:String(s.workplace||"")});
  });
  return out;
}
function cabinetChipActive(cab, chip){
  if(!cab)return false;
  var c=String(cab).toLowerCase();
  return c===String(chip.slug||"").toLowerCase()
    || c===String(chip.workplace||"").toLowerCase()
    || c===String(chip.title||"").toLowerCase();
}
function syncCabinetQuery(cab){
  try{
    var u=new URL(location.href);
    if(cab) u.searchParams.set("cabinet",cab); else u.searchParams.delete("cabinet");
    u.searchParams.delete("workplace");
    var qs=u.searchParams.toString();
    history.replaceState({},"",u.pathname+(qs?("?"+qs):"")+u.hash);
  }catch(err){}
}
function applyCabinetFilter(cab){
  syncCabinetQuery(cab||"");
  if(!RAW_SCENE){ poll(); return; }
  renderCabinetRail(RAW_SCENE);
  var scoped=applyScope(RAW_SCENE);
  renderAll(scoped);
  paintScopeBanner(scoped);
}
function renderCabinetRail(d){
  var rail=$("cabinetRail"); if(!rail)return;
  var chips=hiredCabinetList(d);
  if(!chips.length){ rail.innerHTML=""; return; }
  var cab=scopeParams().cabinet;
  var any=!!(cab&&chips.some(function(c){return cabinetChipActive(cab,c);}));
  if(cab&&!any) cab=""; /* unknown → highlight All */
  var html='<button type="button" class="cab-chip'+(cab?"":" on")+
    '" data-cabinet="" title="Show every hired cabinet">All</button>';
  chips.forEach(function(c){
    var on=cabinetChipActive(cab,c);
    html+='<button type="button" class="cab-chip'+(on?" on":"")+
      '" data-cabinet="'+esc(c.slug)+'" title="'+esc(c.eye+": "+c.title)+'">'+
      '<span class="eye">'+esc(c.eye)+'</span>'+esc(c.title)+'</button>';
  });
  rail.innerHTML=html;
}
document.addEventListener("click",function(e){
  var chip=e.target&&e.target.closest?e.target.closest("button.cab-chip"):null;
  if(!chip)return;
  e.preventDefault();
  applyCabinetFilter(chip.getAttribute("data-cabinet")||"");
});
function sectorMeta(ws){
  var qsum=0, wedged=0;
  (ws||[]).forEach(function(w){
    var q=parseInt(w.queue,10); if(!isNaN(q))qsum+=q;
    if(w.health==="err"||w.health==="wedged")wedged++;
  });
  return (ws||[]).length+" on roster"+(qsum?(" \\u00b7 Q="+qsum):"")+
    (wedged?(" \\u00b7 "+wedged+" stuck"):"");
}
function sectorMetaHtml(ws, slug){
  /* Counts that represent a set are hits.
     Q= ready tickets live on Desk; stuck highlights the floor. */
  var qsum=0, wedged=0;
  (ws||[]).forEach(function(w){
    var q=parseInt(w.queue,10); if(!isNaN(q))qsum+=q;
    if(w.health==="err"||w.health==="wedged")wedged++;
  });
  var bits=[esc((ws||[]).length+" on roster")];
  if(qsum) bits.push('<a class="q-hit" href="'+esc(deskFilterHref(slug,"backlog"))+
    '" title="Open Desk ready pile for this cabinet">Q='+esc(String(qsum))+'</a>');
  if(wedged) bits.push('<button type="button" class="q-hit stuck" data-focus="stuck"'+
    ' data-sector="'+esc(slug)+'" title="Highlight stuck workers">'+esc(String(wedged))+
    ' stuck</button>');
  return bits.join(" \\u00b7 ");
}
function countBayStats(ws, acc){
  (ws||[]).forEach(function(w){
    acc.total++; if(workerState(w)==="on")acc.active++;
    if(w.health==="err"||w.health==="wedged")acc.faults++;
  });
}
function renderHiredSector(s){
  var ws=s.workers||[];
  var role=String(s.role||"business");
  var slug=sectorSlug(s);
  var eye=sectorEyebrow(s), title=sectorTitle(s);
  var head='<span class="eyebrow">'+esc(eye)+'</span><span class="title">'+esc(title)+'</span>';
  var hire='<button type="button" class="hire-btn" data-workdir="'+esc(s.workdir||"")+
    '" data-workplace="'+esc(s.workplace||title)+'" data-project="'+esc(slug)+'">Hire</button>';
  return '<section class="sector-bldg" id="sector-'+esc(slug)+'" data-role="'+esc(role)+
    '" data-workdir="'+esc(s.workdir||"")+'">'+
    '<div class="sector-roof"><span class="shape">'+head+'</span>'+
    '<span class="roof-actions"><span class="q">'+sectorMetaHtml(ws, slug)+'</span>'+hire+
    '</span></div><div class="sector-rooms">'+
    ws.map(bay).join("")+'</div></section>';
}
function renderPermanentStrip(role, title, ws){
  if(!(ws||[]).length) return "";
  var n=ws.length;
  return '<section class="permanent-strip sector-bldg permanent" id="sector-'+esc(role)+
    '" data-role="'+esc(role)+'">'+
    '<div class="sector-roof"><span class="shape">'+
    '<span class="eyebrow">permanent</span><span class="title">'+esc(title)+'</span></span>'+
    '<span class="roof-actions"><span class="lock" title="Pre-installed — not hired">locked</span>'+
    '<span class="q">'+esc(n+(n===1?" seat":" seats"))+'</span></span></div>'+
    '<div class="sector-rooms">'+ws.map(bay).join("")+'</div></section>';
}
function renderBays(d){
  var g=$("grid"), perm=$("permanent"), secs=(d&&d.sectors)||[];
  if(!secs.length){
    if(perm) perm.innerHTML="";
    g.innerHTML='<div class="empty">NO ONE ON THE ROSTER</div>'; return;}
  var you=null, staff=[], hired=[], stats={total:0,active:0,faults:0};
  secs.forEach(function(s){
    var role=String(s.role||"business");
    var ws=s.workers||[];
    if(role==="you"){ you=s; return; }
    if(role==="staff"||role==="civic"){ staff=staff.concat(ws); return; }
    if(!ws.length && role!=="business" && role!=="engine") return;
    hired.push(s);
  });
  /* You and Office staff are independent permanent panes — not hireable. */
  var permHtml="";
  if(you && (you.workers||[]).length){
    countBayStats(you.workers, stats);
    permHtml+=renderPermanentStrip("you","You",you.workers);
  }
  if(staff.length){
    countBayStats(staff, stats);
    permHtml+=renderPermanentStrip("staff","Office staff",staff);
  }
  if(perm) perm.innerHTML=permHtml;
  var out="";
  hired.forEach(function(s){
    countBayStats(s.workers||[], stats);
    out+=renderHiredSector(s);
  });
  g.innerHTML=out||(permHtml?'':'<div class="empty">NO ONE ON THE ROSTER</div>');
  var sum=$("sum"); if(sum) sum.textContent=stats.total+" ON ROSTER \\u00b7 "+stats.active+" ON SHIFT \\u00b7 "+stats.faults+" STUCK";
  applyFloorFocus();
  scrollToHash();
  maybeOpenHireFromUrl();
  maybeOpenPfFromQuery();}
function renderAlarm(d){
  var faults=[]; ((d&&d.sectors)||[]).forEach(function(s){(s.workers||[]).forEach(function(w){
    if(w.health==="err")faults.push((s.workplace||"")+"/"+(w.name||"?"));});});
  var el=$("alarm");
  if(!faults.length){el.innerHTML='<span class="quiet">\\u25C6 ALL CHANNELS QUIET \\u00b7 SCHEDULE NOMINAL \\u00b7 '+esc(fmtL(new Date()))+'</span>'; return;}
  var line=faults.map(function(f){return '<span class="alert">FAULT '+esc(f)+'</span>';}).join("  \\u00b7  ");
  el.innerHTML='<div class="ticker">'+line+'  \\u00b7  '+line+'</div>';}

function renderRuntimes(d){
  var el=$("runtimesStrip"); if(!el)return;
  var pool=(d&&d.runtimes)||[];
  if(!pool.length){el.innerHTML="";return;}
  var html='<span class="rt-label">Runtimes</span>';
  pool.forEach(function(r){
    var cls=r.path?(r.workers&&r.workers.length?"employed":"available"):"absent";
    var tip=cls+(r.path?" \\u00b7 "+r.path:"")+(r.workers&&r.workers.length?" \\u00b7 "+r.workers.join(", "):"");
    html+='<span class="rt-chip '+cls+'" title="'+esc(tip)+'">'+esc(r.cli)+'</span>';
  });
  el.innerHTML=html;}

function flashClaimLands(d){
  /* When a worker newly holds a Desk claim while in_flight, bay flashes once. */
  if(!d) return;
  var inflight={};
  (d.in_flight||[]).forEach(function(n){ inflight[n]=1; });
  var nowSeen={};
  (d.sectors||[]).forEach(function(s){
    (s.workers||[]).forEach(function(w){
      if(!w||!w.name) return;
      var ids=[];
      (w.holding||[]).forEach(function(h){ if(h&&h.id) ids.push(String(h.id)); });
      ids.sort();
      var key=ids.join("|");
      nowSeen[w.name]=key;
      if(!HOLDING_PRIMED) return;
      if(!inflight[w.name] || !ids.length) return;
      var prev=HOLDING_SEEN[w.name]||"";
      /* New claim id appeared (empty→id or id set grew/changed). */
      if(key && key!==prev){
        var bay=document.querySelector('.bay[data-worker="'+CSS.escape(w.name)+'"]');
        if(bay){
          bay.classList.add("claim-land");
          setTimeout(function(){ bay.classList.remove("claim-land"); }, 950);
        }
      }
    });
  });
  HOLDING_SEEN=nowSeen;
  HOLDING_PRIMED=true;
}
function renderAll(d){
  if(!d)return; STATE=d; flashClaimLands(d); renderBays(d); renderAlarm(d); renderRuntimes(RAW_SCENE);
  var dm=String(d.daemon||"—").toUpperCase();
  var flight=(d.in_flight&&d.in_flight.length)?" \\u00b7 "+d.in_flight.length+" IN FLIGHT":"";
  var ops=$("opsLine");
  if(ops) ops.textContent="DAEMON "+dm+flight;
  var mb=document.getElementById("mastBeacon");
  if(mb)mb.setAttribute("class",d.daemon==="running"?"":"down");
  $("carrierLamp").className="lamp on"; $("carrierLamp").textContent="\\u25CF";
  $("carrier").textContent="CARRIER \\u00b7 "+(d.generated_at?rel(d.generated_at):"LIVE");}
/* Exception vitals only — stuck / quiet / up-next. Burn & steady → /report. */
function paintOverview(r){
  var bay=$("overview"), tiles=$("ovTiles"); if(!tiles||!bay)return;
  if(!r){ bay.classList.remove("clear");
    tiles.innerHTML='<div class="ov-quiet">overview offline</div>'; return; }
  var ws=r.workers||[];
  var by={}; ws.forEach(function(w){ var v=w.verdict||"?"; by[v]=(by[v]||0)+1; });
  var wedged=(by.wedged||0)+(by.faulting||0)+(by.starved||0);
  var quietList=r.quiet||[];
  var quiet=quietList.length;
  var fires=r.next_fires||[];
  var next=fires.slice(0,3).map(function(f){ return (f.name||"?"); }).join(", ")||"—";
  var nextWhen=fires[0]&&fires[0].at?rel(fires[0].at):"";
  window._QUIET_SET={}; quietList.forEach(function(w){
    var n=(w&&w.name)||w; if(n) window._QUIET_SET[n]=1;
  });
  window._NEXT_SET={}; fires.forEach(function(f){ if(f&&f.name) window._NEXT_SET[f.name]=1; });
  var focus=focusParams().focus;
  function tile(cls,k,v,s,focusKey){
    var on=focus&&focus===focusKey;
    return '<button type="button" class="ov-tile '+(cls||'')+(on?' on':'')+'"'+
      (focusKey?(' data-focus="'+esc(focusKey)+'"'):'')+
      ' title="'+(focusKey?'Highlight this set on the floor':'')+'">'+
      '<span class="k">'+esc(k)+'</span><span class="v">'+esc(String(v))+'</span>'+
      (s?'<span class="s">'+esc(s)+'</span>':'')+'</button>';
  }
  if(!wedged && !quiet){
    bay.classList.add("clear");
    tiles.innerHTML="";
    return;
  }
  bay.classList.remove("clear");
  var html="";
  if(wedged) html+=tile("err","workers stuck",String(wedged),"wedged / faulting / starved","stuck");
  if(quiet) html+=tile("warn","quiet jobs",String(quiet),"not firing recently","quiet");
  if(fires.length) html+=tile("","up next",next,nextWhen?("first in "+nextWhen):"schedule","next");
  tiles.innerHTML=html;
  if(focusParams().focus) applyFloorFocus();
}
function pollOverview(){
  fetch("/api/report?days=7",{cache:"no-store"}).then(function(r){
    if(!r.ok)throw 0; return r.json();
  }).then(paintOverview).catch(function(){ paintOverview(null); });}
/* Office deep-link scope (from ProtocolCity office doors):
   ?scope=floor|agents|l0  → agents (kind=job) + host services/cron only
   ?cabinet=Example        → that cabinet's workers (kind=worker/lane) only
   ?focus=stuck|quiet|ready|next → highlight that set
   ?pf=<name>              → open personnel-file summary drawer
   Workers never jump cabinets; agents are floor-level (may appear elsewhere). */
function scopeParams(){
  try{
    var q=new URLSearchParams(location.search||"");
    return {scope:String(q.get("scope")||"").toLowerCase(),
            cabinet:String(q.get("cabinet")||q.get("workplace")||"").trim()};
  }catch(e){return {scope:"",cabinet:""};}
}
function focusParams(){
  try{
    var q=new URLSearchParams(location.search||"");
    return {focus:String(q.get("focus")||"").toLowerCase(),
            sector:String(q.get("sector")||"").toLowerCase()};
  }catch(e){return {focus:"",sector:""};}
}
function syncFocusQuery(focus, sector){
  try{
    var u=new URL(location.href);
    if(focus) u.searchParams.set("focus",focus); else u.searchParams.delete("focus");
    if(sector) u.searchParams.set("sector",sector); else u.searchParams.delete("sector");
    var qs=u.searchParams.toString();
    history.replaceState({},"",u.pathname+(qs?("?"+qs):"")+u.hash);
  }catch(err){}
}
function setFloorFocus(focus, sector){
  var cur=focusParams();
  if(cur.focus===focus && (!sector||cur.sector===sector)){
    syncFocusQuery("",""); applyFloorFocus(); return;
  }
  syncFocusQuery(focus||"", sector||"");
  applyFloorFocus();
}
function bayMatchesFocus(bay, focus, sector){
  if(!focus) return true;
  if(sector){
    var cab=String(bay.getAttribute("data-cab")||"").toLowerCase();
    var sec=bay.closest(".sector-bldg");
    var sid=sec&&sec.id?sec.id.replace(/^sector-/,""):"";
    if(cab!==sector && sid!==sector) return false;
  }
  if(focus==="stuck") return bay.getAttribute("data-stuck")==="1";
  if(focus==="ready") return bay.getAttribute("data-ready")==="1";
  if(focus==="quiet"){
    /* Quiet jobs come from /api/report — mark via STATE when available. */
    var name=bay.getAttribute("data-worker")||"";
    var quiet=(window._QUIET_SET||{})[name];
    return !!quiet;
  }
  if(focus==="next"){
    var name2=bay.getAttribute("data-worker")||"";
    return !!(window._NEXT_SET||{})[name2];
  }
  return true;
}
function applyFloorFocus(){
  var p=focusParams();
  var focus=p.focus, sector=p.sector;
  var banner=$("focusBanner");
  if(!banner){
    banner=document.createElement("div");
    banner.id="focusBanner";
    var host=document.querySelector(".above-floor")||document.querySelector(".room")||document.body;
    host.insertBefore(banner, host.firstChild);
  }
  if(!focus){
    banner.className=""; banner.innerHTML="";
    document.querySelectorAll(".bay.focus-dim, .bay.focus-hit").forEach(function(b){
      b.classList.remove("focus-dim"); b.classList.remove("focus-hit");
    });
    document.querySelectorAll(".ov-tile.on").forEach(function(t){ t.classList.remove("on"); });
    return;
  }
  var labels={stuck:"stuck workers",quiet:"quiet jobs",ready:"ready on Desk",next:"up next"};
  banner.className="on";
  banner.innerHTML='Highlighting <strong>'+esc(labels[focus]||focus)+'</strong>'+
    (sector?(' in '+esc(sector)):'')+
    ' · <a href="#" id="clearFocus">show full floor</a>';
  var first=null, hits=0;
  document.querySelectorAll(".bay[data-worker]").forEach(function(b){
    var hit=bayMatchesFocus(b, focus, sector);
    b.classList.toggle("focus-dim", !hit);
    b.classList.toggle("focus-hit", hit);
    if(hit){ hits++; if(!first) first=b; }
  });
  document.querySelectorAll(".ov-tile[data-focus]").forEach(function(t){
    t.classList.toggle("on", t.getAttribute("data-focus")===focus);
  });
  if(first) first.scrollIntoView({behavior:"smooth", block:"center"});
  var clr=$("clearFocus");
  if(clr) clr.onclick=function(e){ e.preventDefault(); setFloorFocus("",""); };
}
function maybeOpenPfFromQuery(){
  try{
    var q=new URLSearchParams(location.search||"");
    var pf=String(q.get("pf")||"").trim();
    if(!pf) return;
    openPF(pf);
    /* HISTORY LAW: clear sticky pf so refresh does not reopen the overlay. */
    q.delete("pf");
    var qs=q.toString();
    history.replaceState({},"",location.pathname+(qs?("?"+qs):"")+location.hash);
  }catch(err){}
}
function isAgentKind(k){
  k=String(k||"").toLowerCase();
  return k==="job"||k==="agent";
}
function isWorkerKind(k){
  k=String(k||"").toLowerCase();
  return k==="worker"||k==="lane"||k==="";
}
function sectorMatchesCabinet(s, cab){
  if(!cab)return false;
  var c=cab.toLowerCase();
  var wp=String((s&&s.workplace)||"").toLowerCase();
  var wd=String((s&&s.workdir)||"").replace(/\\\\/g,"/").toLowerCase();
  var base=wd.split("/").filter(Boolean).pop()||"";
  return wp===c||base===c||wd.endsWith("/"+c);
}
function applyScope(d){
  if(!d)return d;
  var p=scopeParams();
  var floor=p.scope==="floor"||p.scope==="agents"||p.scope==="l0"||p.scope==="home";
  var cab=p.cabinet;
  if(!floor&&!cab)return d;
  var out={generated_at:d.generated_at,daemon:d.daemon,in_flight:d.in_flight,
           last_tick:d.last_tick,services:d.services||[],sectors:[],_scope:p};
  if(floor){
    /* Keep You + Office staff sectors; fold other jobs into Office staff if any;
       host services stay engine. */
    var you=null, staff=[], otherJobs=[];
    (d.sectors||[]).forEach(function(s){
      var role=String(s.role||"");
      if(role==="you"){ you=s; return; }
      if(role==="staff"||role==="civic"){ staff=staff.concat(s.workers||[]); return; }
      (s.workers||[]).forEach(function(w){
        if(isAgentKind(w.kind)) otherJobs.push(Object.assign({},w));
      });
    });
    if(you) out.sectors.push(you);
    var staffWs=staff.concat(otherJobs);
    if(staffWs.length){
      out.sectors.push({workplace:"Office staff",role:"staff",workdir:"",
        workers:staffWs});
    }
    var svcs=d.services||[];
    if(svcs.length){
      out.sectors.push({workplace:"Host services · cron",role:"engine",workdir:"",
        workers:svcs.map(function(r){
          return {name:r.label||"service",kind:"service",display:r.label||"",
            model:"launchd",schedule:r.next_fire||"",owned:true,
            next_fire:r.next_fire||"",queue:"—",health:"ok",
            why:r.pid?"pid "+r.pid:(r.state||"service"),last_shift:null};
        })});
    }
    out._scopeLabel="L0 · You + Office staff + services";
    return out;
  }
  if(cab){
    /* Permanent seats always stay; filter only hired cabinets. */
    var hired=0;
    (d.sectors||[]).forEach(function(s){
      var role=String(s.role||"");
      if(role==="you"||role==="staff"||role==="civic"){
        out.sectors.push(s); return;
      }
      if(!sectorMatchesCabinet(s,cab))return;
      /* Engine bays keep agents/jobs; business cabinets keep workers/lanes. */
      var ws=(s.workers||[]);
      if(role!=="engine"){
        ws=ws.filter(function(w){return isWorkerKind(w.kind);});
      }
      if(!ws.length)return;
      hired++;
      out.sectors.push(Object.assign({},s,{workers:ws}));
    });
    if(!hired) return d; /* unknown cabinet → full floor (All) */
    out._scopeLabel="cabinet · "+cab;
    return out;
  }
  return d;
}
function paintScopeBanner(d){
  var el=$("scopeBanner");
  if(!el){
    el=document.createElement("div");
    el.id="scopeBanner";
    el.style.cssText="padding:6px 14px;font:600 .72rem/1.4 Georgia,serif;"+
      "letter-spacing:.04em;background:#e8f0ec;border-bottom:1px solid var(--line,#c4b8a4);color:#2a241c";
    /* Live inside .above-floor so the 4-row perimeter grid stays intact. */
    var host=document.querySelector(".above-floor")||document.querySelector(".room")||document.body;
    host.insertBefore(el, host.firstChild);
  }
  var p=scopeParams();
  /* Cabinet filter has its own chip rail — no duplicate banner. */
  if(p.cabinet && !p.scope){
    el.style.display="none"; el.innerHTML=""; return;
  }
  if(d&&d._scopeLabel){
    el.style.display="block";
    el.innerHTML=esc(d._scopeLabel)+
      ' · hired cabinets hidden · <a href="/" style="color:var(--verd,#3d7a6a)">show full floor</a>';
  } else if(p.scope){
    el.style.display="block";
    el.innerHTML='scoped view · <a href="/" style="color:var(--verd,#3d7a6a)">show full floor</a>';
  } else {
    el.style.display="none";
  }
}

var YOU_ATTN_LAST=null;
var YOU_NOTIFY_KEY="pc_you_notify";
function youNotifyPref(){ try{return localStorage.getItem(YOU_NOTIFY_KEY)||"";}catch(e){return "";} }
function setYouNotifyPref(v){ try{localStorage.setItem(YOU_NOTIFY_KEY,v);}catch(e){} }
function maybeNotifyYouAttn(n, prev, d){
  if(prev==null||n<=prev) return;
  if(typeof Notification==="undefined"||Notification.permission!=="granted") return;
  if(youNotifyPref()==="off") return;
  var items=(d&&d.items)||[];
  var top=items[0];
  var body=top?((top.id||"")+" · "+String(top.title||top.note||"").slice(0,90)):(n+" waiting on You");
  try{
    var note=new Notification(n+" for You · Protocol City",{body:body,tag:"pc-you-attention",renotify:true});
    note.onclick=function(){ try{window.focus();}catch(e){}
      window.open("http://127.0.0.1:8799/admin/attention","_blank","noopener"); note.close(); };
  }catch(e){}
}
function paintYouAttnRoster(d){
  var el=document.getElementById("youAttn");
  if(!el){
    var host=document.querySelector(".sys-meta")||document.getElementById("opsLine")||document.getElementById("carrier");
    if(!host||!host.parentNode) return;
    el=document.createElement("a");
    el.id="youAttn";
    el.className="you-attn";
    el.href="http://127.0.0.1:8799/admin/attention";
    el.target="_blank";
    el.rel="noopener";
    host.parentNode.insertBefore(el, host.nextSibling);
    el.addEventListener("dblclick", function(ev){
      ev.preventDefault(); ev.stopPropagation();
      if(typeof Notification==="undefined"){ alert("No Notification API here."); return; }
      var req=Notification.permission==="default"?Notification.requestPermission():Promise.resolve(Notification.permission);
      Promise.resolve(req).then(function(p){
        if(p==="granted"){ setYouNotifyPref("on");
          try{ new Notification("You · alerts on",{body:"Roster will notify when more items wait on You.",tag:"pc-you-attention-on"});}catch(e){}
        } else setYouNotifyPref("off");
      });
    });
  }
  var n=(d&&d.ok!==false)?(d.count|0):(d|0);
  if(typeof d==="number") n=d|0;
  var prev=YOU_ATTN_LAST;
  YOU_ATTN_LAST=n;
  if(n<=0){ el.style.display="none"; el.textContent=""; return; }
  el.style.display="inline-flex";
  el.textContent=n+" for You";
  el.title=n+" waiting on You · click Desk attention · dbl-click enable browser notify";
  if(prev!=null && n>prev){
    el.classList.add("bump");
    setTimeout(function(){ el.classList.remove("bump"); }, 900);
    if(typeof d==="object" && d) maybeNotifyYouAttn(n, prev, d);
  }
}
function pollYouAttnRoster(){
  fetch("http://127.0.0.1:8799/api/dev/attention",{cache:"no-store"})
    .then(function(r){return r.ok?r.json():null;})
    .then(function(d){ if(d) paintYouAttnRoster(d); })
    .catch(function(){});
}
function poll(){
  pollYouAttnRoster();
  fetch("/api/scene",{cache:"no-store"}).then(function(r){
    if(!r.ok)throw 0; return r.json();
  }).then(function(d){
    RAW_SCENE=d;
    renderCabinetRail(d);
    d=applyScope(d);
    renderAll(d);
    paintScopeBanner(d);
  }).catch(function(){
    if(STATE){$("carrierLamp").className="lamp err"; $("carrier").textContent="CARRIER DROP \\u00b7 HOLDING";}
    else {$("carrierLamp").className="lamp"; $("carrier").textContent="NO CARRIER";}
  });
  pollOverview();}
function tick(){
  var now=new Date(); var wt=$("wallTime");
  if(wt) wt.textContent=fmtZ(now)+"  "+fmtL(now)+"L";
  document.querySelectorAll(".cd[data-nf]").forEach(function(el){
    var cd=countdown(el.getAttribute("data-nf")); el.textContent=cd.text; el.className=cd.cls;});
  if(!STATE)return;
  var by={}; (STATE.sectors||[]).forEach(function(s){(s.workers||[]).forEach(function(w){by[w.name]=w;});});
  // re-derive status each second — countdown + chip + progress
  // (figure bob is CSS on .bay.on / .inflight — not JS motion)
  document.querySelectorAll(".bay[data-worker]").forEach(function(b){
    var w=by[b.getAttribute("data-worker")]; if(!w)return;
    var i=b.querySelector(".progress > i");
    if(i)i.style.width=pct(w)+"%";
    /* Preserve Click-ladder focus marks — bayClass alone would wipe them. */
    var keepDim=b.classList.contains("focus-dim");
    var keepHit=b.classList.contains("focus-hit");
    b.className=bayClass(w);
    if(keepDim) b.classList.add("focus-dim");
    if(keepHit) b.classList.add("focus-hit");
    var chip=b.querySelector(".status-chip");
    if(chip){ chip.className="status-chip "+statusChipClass(w);
      chip.textContent=statusLabel(w); }
    var work=b.querySelector(".work-line");
    if(work){
      var wo=workingOn(w);
      work.className="work-line"+(wo.cls?(" "+wo.cls):"");
      work.textContent=wo.text;
      work.title=w.why||wo.text;
      if(work.tagName==="A" && wo.claimHref) work.setAttribute("href", wo.claimHref);
    }
    var payEl=b.querySelector(".bay-pay");
    if(payEl){ var pl=payrollLine(w); if(pl) payEl.textContent=pl; }
  });
  TICK++;
  if(STATE && TICK%5===0)$("carrier").textContent="CARRIER \\u00b7 "+(STATE.generated_at?rel(STATE.generated_at):"LIVE");}
/* walk-in seam: scroll to location.hash on first render and on hashchange.
   #hire opens the hire drawer (optionally with ?cabinet=&workdir=). */
var HASH_DONE=false;
var HIRE_OPENED_FROM_URL=false;
function scrollToHash(force){
  var h=(location.hash||"").replace(/^#/,"");
  if(!h || h==="hire")return;
  if(!force && HASH_DONE)return;
  var el=document.getElementById(h);
  if(el){ el.scrollIntoView({behavior:force?"smooth":"auto", block:"start"}); HASH_DONE=true; }}
function maybeOpenHireFromUrl(){
  if(HIRE_OPENED_FROM_URL)return;
  var h=(location.hash||"").replace(/^#/,"");
  var q;
  try{ q=new URLSearchParams(location.search||""); }catch(e){ return; }
  var want=h==="hire"||q.get("hire")==="1"||q.get("hire")==="true";
  if(!want)return;
  HIRE_OPENED_FROM_URL=true;
  var cab=String(q.get("cabinet")||q.get("workplace")||"").trim();
  var wd=String(q.get("workdir")||"").trim();
  if(!wd && cab && STATE){
    (STATE.sectors||[]).forEach(function(s){
      if(sectorMatchesCabinet(s, cab)) wd=String(s.workdir||"");
    });
  }
  openHire({workdir:wd, workplace:cab||"", project:cab.toLowerCase()});
  if(cab){
    var el=document.getElementById("sector-"+cab.toLowerCase());
    if(el) el.scrollIntoView({behavior:"smooth", block:"start"});
  }
}
window.addEventListener("hashchange",function(){
  HASH_DONE=false;
  if((location.hash||"").replace(/^#/,"")==="hire"){
    HIRE_OPENED_FROM_URL=false; maybeOpenHireFromUrl();
  } else scrollToHash(true);
});
tick(); poll();
setInterval(tick,1000); setInterval(poll,20000);

/* ── Hire drawer (STAFFING §2) — cabinet/engine only ── */
var HIRE_CTX=null;
function openHire(ctx){
  HIRE_CTX=ctx||{};
  closePF();
  $("pfscrim").classList.add("open"); $("pf").classList.add("open");
  var wp=HIRE_CTX.workplace||HIRE_CTX.project||"cabinet";
  var wd=HIRE_CTX.workdir||"";
  $("pfHead").innerHTML='<div class="file-no">HIRE</div>'+
    '<div class="nm">New worker</div>'+
    '<div class="sub">'+esc(wp)+(wd?(" \\u00b7 "+esc(wd)):"")+'</div>'+pfCloseBtn();
  $("pfBody").innerHTML=
    '<div class="hint" style="margin-bottom:8px">You and Office staff are permanent. '+
    'Hires land only in a cabinet (papers + roster row).</div>'+
    '<form class="hire-form" id="hireForm">'+
    '<label>Persona name<input name="name" required placeholder="Neo" autocomplete="off"/></label>'+
    '<label>Role title<input name="role" required placeholder="Market Analyst" autocomplete="off"/></label>'+
    '<div class="row2">'+
    '<label>Kind<select name="kind"><option value="lane" selected>worker (lane)</option>'+
    '<option value="job">agent (job)</option></select></label>'+
    '<label>Schedule<input name="schedule" value="*/30 * * * *" /></label></div>'+
    '<label>Workdir<input name="workdir" required value="'+esc(wd)+'" '+(wd?"readonly":"")+
    ' placeholder="/path/to/cabinet"/></label>'+
    '<label>Desk project (queue)<input name="project" value="'+esc(HIRE_CTX.project||"")+
    '" placeholder="gridfinity"/></label>'+
    '<div class="err" id="hireErr"></div>'+
    '<button type="submit" class="hire-submit">Hire &amp; arm</button>'+
    '</form>';
  $("pfFoot").innerHTML='<span>Produces CONTRACT.md + prompt.md + local/roster.json row</span>';
  var form=$("hireForm");
  if(form) form.addEventListener("submit", submitHire);
}
function submitHire(ev){
  ev.preventDefault();
  var form=ev.target;
  var err=$("hireErr");
  var btn=form.querySelector("button.hire-submit");
  var fd=new FormData(form);
  var body={
    name:String(fd.get("name")||"").trim(),
    role:String(fd.get("role")||"").trim(),
    kind:String(fd.get("kind")||"lane"),
    schedule:String(fd.get("schedule")||"").trim(),
    workdir:String(fd.get("workdir")||"").trim(),
    project:String(fd.get("project")||"").trim(),
    plant_papers:true
  };
  if(!body.name||!body.role||!body.workdir){
    if(err) err.textContent="Persona, role, and workdir are required.";
    return;
  }
  if(btn){ btn.disabled=true; btn.textContent="Hiring\\u2026"; }
  if(err) err.textContent="";
  fetch("/api/hire",{method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)})
  .then(function(r){ return r.json().then(function(d){ return {status:r.status,d:d}; }); })
  .then(function(x){
    if(!x.d||!x.d.ok){
      if(err) err.textContent=(x.d&&x.d.msg)||"hire refused";
      if(btn){ btn.disabled=false; btn.textContent="Hire & arm"; }
      return;
    }
    var steps=(x.d.next_steps||[]).map(function(s){return "<li>"+esc(s)+"</li>";}).join("");
    $("pfBody").innerHTML='<div class="hire-next"><b>Hired '+esc((x.d.worker&&x.d.worker.name)||body.name)+
      '</b><p>'+esc(x.d.msg||"armed")+'</p><ol>'+steps+"</ol></div>";
    $("pfFoot").innerHTML='<a href="#" onclick="closePF();return false">close</a>';
    setTimeout(function(){ poll(); }, 400);
  })
  .catch(function(e){
    if(err) err.textContent="hire failed: "+e;
    if(btn){ btn.disabled=false; btn.textContent="Hire & arm"; }
  });
}
document.addEventListener("click",function(e){
  var h=e.target&&e.target.closest?e.target.closest("button.hire-btn"):null;
  if(!h)return;
  openHire({
    workdir:h.getAttribute("data-workdir")||"",
    workplace:h.getAttribute("data-workplace")||"",
    project:h.getAttribute("data-project")||""
  });
});

/* ── the personnel-file drawer: workers open ON the floor ── */
var PF_NAME=null;
/* wf-77: live SSE tail while worker in_flight */
var PF_SSE=null;
function stopPFTail(){if(PF_SSE){PF_SSE.close();PF_SSE=null;}}
function startPFTail(name){
  stopPFTail();
  var el=$("pfTail"); if(!el)return;
  PF_SSE=new EventSource("/api/out/"+encodeURIComponent(name)+"/stream");
  PF_SSE.addEventListener("idle",function(){
    if($("pfTail"))$("pfTail").innerHTML='<span class="pf-tail-note">not on shift</span>';
    stopPFTail();});
  PF_SSE.addEventListener("waiting",function(){
    if($("pfTail"))$("pfTail").innerHTML='<span class="pf-tail-note">waiting for output\\u2026</span>';});
  PF_SSE.addEventListener("chunk",function(e){
    var box=$("pfTail"); if(!box)return;
    try{var d=JSON.parse(e.data);
      var note=box.querySelector(".pf-tail-note"); if(note)note.remove();
      box.appendChild(document.createTextNode(String(d.text||"")));
      box.scrollTop=box.scrollHeight;}catch(err){}});
  PF_SSE.addEventListener("ping",function(){});
  PF_SSE.addEventListener("end",function(){
    var box=$("pfTail"); if(box){
      var s=document.createElement("span"); s.className="pf-tail-note";
      s.textContent="\\u2014 shift ended \\u2014"; box.appendChild(s);
      box.scrollTop=box.scrollHeight;}
    stopPFTail();});
  PF_SSE.onerror=function(){
    var box=$("pfTail"); if(box){
      var s=document.createElement("span"); s.className="pf-tail-note";
      s.textContent="\\u2014 stream error \\u2014"; box.appendChild(s);}
    stopPFTail();};}
function pfCloseBtn(){return '<button class="pf-close" onclick="closePF()" title="close (esc)">\\u00d7</button>';}
function closePF(){
  stopPFTail();
  PF_NAME=null;HIRE_CTX=null;$("pf").classList.remove("open");$("pfscrim").classList.remove("open");
  /* HISTORY LAW: Esc closes overlay; browser Back leaves the room.
     PF is app-owned — no sticky query to clear. Full file = /worker/<name>. */
}
function openPF(name){
  HIRE_CTX=null;
  PF_NAME=name; $("pfscrim").classList.add("open"); $("pf").classList.add("open");
  $("pfHead").innerHTML='<div class="file-no">PERSONNEL FILE</div>'+
    '<div class="nm">'+esc(name)+'</div><div class="sub">pulling the file\\u2026</div>'+pfCloseBtn();
  $("pfBody").innerHTML='<div class="pf-sec">RETRIEVING\\u2026</div>';
  $("pfFoot").innerHTML="";
  fetch("/api/worker/"+encodeURIComponent(name),{cache:"no-store"})
  .then(function(r){if(!r.ok)throw 0; return r.json();})
  .then(function(w){if(PF_NAME===name){renderPF(w);if(inFlight(w))startPFTail(name);}})
  .catch(function(){if(PF_NAME===name)
    $("pfBody").innerHTML='<div class="pf-sec">NO FILE ON RECORD</div>';});}
function renderPF(w){
  var title=(w.display&&String(w.display).trim())?w.display:w.name;
  $("pfHead").innerHTML='<div class="file-no">PERSONNEL FILE</div>'+
    '<div class="nm">'+esc(title)+'</div>'+
    '<div class="sub">'+esc(w.kind||"unit")+' \\u00b7 '+esc(w.model||"default")+
    ' \\u00b7 identity '+esc(w.identity||"\\u2014")+(w.owned?' \\u00b7 OWNED':'')+'</div>'+pfCloseBtn();
  var cd=countdown(w.next_fire);
  var payBits=[];
  if(w.cli) payBits.push(String(w.cli));
  if(w.model&&w.model!=="default") payBits.push(String(w.model));
  var h='<div class="r"><span class="k">CRON</span><span class="v" title="'+esc(w.schedule||"manual")+'">'+
      esc(cronSpeech(w.schedule||"manual"))+'</span></div>'+
    '<div class="r"><span class="k">EMPLOYMENT</span><span class="v">'+
      (w.owned?'ARMED \\u00b7 daemon-owned':(String(w.schedule||"").toLowerCase().indexOf("paused")===0
        ?'PAUSED \\u00b7 not firing':'NOT OWNED \\u00b7 manual / unarmed'))+
      '</span></div>'+
    (payBits.length?'<div class="r"><span class="k">PAYROLL</span><span class="v">'+
      esc(payBits.join(" \\u00b7 "))+'</span></div>':'')+
    '<div class="r"><span class="k">NEXT FIRE</span><span class="v">'+
      (w.next_fire?esc(cd.text):"\\u2014")+'</span></div>'+
    '<div class="r"><span class="k">QUEUE</span><span class="v">'+esc(w.queue!=null?w.queue:"\\u2014")+'</span></div>'+
    '<div class="r"><span class="k">HOLDING</span><span class="v">'+
      esc(String((w.holding_count!=null?w.holding_count:(w.holding||[]).length)||0))+
      ' claim'+(Number(w.holding_count||(w.holding||[]).length)===1?"":"s")+'</span></div>'+
    '<div class="r"><span class="k">HEALTH</span><span class="v '+
      (w.health==="err"?"bad":(w.health==="ok"?"good":""))+'">'+
      esc(String(w.health||"\\u2014").toUpperCase())+(w.why?" \\u00b7 "+esc(w.why):"")+'</span></div>'+
    '<div class="r"><span class="k">BUDGET</span><span class="v">'+
      esc(w.budget_secs)+'s \\u00b7 \\u2264'+esc(w.max_passes)+'p</span></div>'+
    '<div class="r"><span class="k">WORKDIR</span><span class="v">'+esc(w.workdir||"")+'</span></div>'+
    (w.succeeds?'<div class="r"><span class="k">SUCCESSION</span><span class="v">succeeds '+
      esc(w.succeeds)+'</span></div>':'');
  /* FLAGS — governance layer: open labeled tickets not in active HOLDINGS. */
  var wf=w.flags||[];
  if(wf.length){
    h+='<div class="pf-sec">FLAGS \\u2014 open tickets in lane ('+wf.length+')</div>';
    wf.forEach(function(t){
      var gated=t.founder_gated;
      var cls=gated?"amber":"dim";
      var lbl=gated?"founder\\u2011gated":String(t.status||"");
      var href=t.href||(deskBaseUrl()+"/admin/desk?open="+encodeURIComponent(t.id||""));
      h+='<div class="pf-shift"><a href="'+esc(href)+'" target="_blank" rel="noopener">'+
        esc(t.id||"?")+'</a> \\u00b7 <span class="'+cls+'">'+esc(lbl)+'</span>'+
        (t.title?' \\u00b7 '+esc(String(t.title).slice(0,72)):"")+'</div>';
    });
  }
  /* Holding + ready — what they have and what is next (emptying queues). */
  var held=w.holding||[];
  h+='<div class="pf-sec">HOLDING NOW \\u2014 Owner: claims on Desk</div>';
  if(!held.length){
    h+='<div class="pf-shift dim">none \\u2014 no in_progress / in_review with their Owner marker</div>';
  }else{
    held.forEach(function(t){
      var st=String(t.status||"");
      var label=st==="in_progress"?"doing":(st==="in_review"?"review":st);
      var href=t.href||(deskBaseUrl()+"/admin/desk?open="+encodeURIComponent(t.id||""));
      h+='<div class="pf-shift"><a href="'+esc(href)+'" target="_blank" rel="noopener">'+
        esc(t.id||"?")+'</a> \\u00b7 <span class="amber">'+esc(label)+'</span>'+
        (t.title?' \\u00b7 '+esc(String(t.title).slice(0,72)):"")+'</div>';
    });
  }
  var ready=w.ready||[];
  h+='<div class="pf-sec">READY QUEUE \\u2014 top of their pile'
    +(w.queue!=null && w.queue!=="" && w.queue!=="\\u2014"?" \\u00b7 "+esc(String(w.queue))+" ready":"")
    +'</div>';
  if(!ready.length){
    h+='<div class="pf-shift dim">'+(Number(w.queue)>0
      ?"queue count is "+esc(String(w.queue))+" but ready list empty (gated / desk down)"
      :"queue empty \\u2014 nothing ready to claim")+'</div>';
  }else{
    ready.forEach(function(t){
      var href=t.href||(deskBaseUrl()+"/admin/desk?open="+encodeURIComponent(t.id||""));
      var pri=t.priority!=null?"P"+t.priority:"";
      h+='<div class="pf-shift"><a href="'+esc(href)+'" target="_blank" rel="noopener">'+
        esc(t.id||"?")+'</a>'+(pri?' \\u00b7 '+esc(pri):"")+
        (t.title?' \\u00b7 '+esc(String(t.title).slice(0,72)):"")+'</div>';
    });
  }
  h+='<div class="pf-sec">RULE STACK \\u2014 what the next shift reads</div><table class="pf-law">';
  (w.law||[]).forEach(function(e){
    var f=e.href?'<a href="'+esc(e.href)+'">'+esc(e.file)+'</a>'
               :'<span class="sha">'+esc(e.file)+' (missing)</span>';
    h+='<tr><td class="lv">'+esc(e.level)+'</td><td>'+esc(e.label)+'</td><td>'+f+
       '</td><td class="sha">'+esc(e.sha||"\\u2014")+'<br>'+esc(e.mtime||"")+'</td></tr>';});
  h+='</table>';
  h+='<div class="pf-sec">SHIFT RECORD \\u2014 last '+((w.shifts||[]).length)+'</div>';
  if(!(w.shifts||[]).length)h+='<div class="pf-shift dim">never dispatched</div>';
  (w.shifts||[]).forEach(function(s){
    var oc=s.outcome==="ok"?"ok":(s.outcome==="running"?"amber":
           (s.outcome==="error"||s.outcome==="crashed"?"err":"dim"));
    h+='<div class="pf-shift"><span class="dim">'+esc(rel(s.ts)||s.ts)+'</span> \\u00b7 '+
      '<span class="'+oc+'">'+esc(s.outcome)+(s.dry_run?" (dry)":"")+'</span>'+
      (s.passes?' \\u00b7 '+esc(s.passes)+'p':'')+
      (s.reason?' \\u00b7 <span class="dim">'+esc(s.reason)+'</span>':'')+'</div>';});
  h+='<div class="pf-sec">SHIFT OUTPUT</div>';
  if(inFlight(w)){
    h+='<pre id="pfTail" class="pf-tail"><span class="pf-tail-note">connecting\\u2026</span></pre>';
  }else{
    h+='<div class="pf-shift dim">not on shift \\u2014 tail streams here while in_flight</div>';
  }
  $("pfBody").innerHTML=h;
  $("pfFoot").innerHTML='<a href="/worker/'+esc(w.name)+'">full personnel file \\u2197</a>'+
    '<a href="/shifts/'+esc(w.name)+'">raw ledger</a>'+
    '<a href="/out/'+esc(w.name)+'">last output</a>';}
document.addEventListener("click",function(e){
  var focusBtn=e.target&&e.target.closest?e.target.closest("[data-focus]"):null;
  if(focusBtn&&(focusBtn.classList.contains("ov-tile")||focusBtn.classList.contains("q-hit"))){
    e.preventDefault();
    var f=focusBtn.getAttribute("data-focus")||"";
    var sec=focusBtn.getAttribute("data-sector")||"";
    setFloorFocus(f, sec);
    return;
  }
  if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;
  var a=e.target&&e.target.closest?e.target.closest('a[href^="/worker/"]'):null;
  if(!a)return;
  if(a.closest("#pf"))return; /* the full-file link is the escape hatch */
  var name=a.getAttribute("href").slice("/worker/".length).split("?")[0];
  if(!name)return;
  e.preventDefault(); openPF(decodeURIComponent(name));});
document.addEventListener("keydown",function(e){if(e.key==="Escape")closePF();});
</script>
"""


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


def _desk_json(path: str) -> Optional[dict]:
    try:
        with urllib.request.urlopen(DESK + path, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


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


def render_board(local_root: str, can_dispatch: bool = False) -> str:
    roster = _load_roster(local_root)
    rota = _launchctl_rota(local_root)
    summary = _desk_json("/api/dev/board-summary") or {}
    activity = (_desk_json("/api/dev/activity") or {}).get("entries", [])[:8]

    out = ["<!doctype html><meta charset='utf-8'><title>Classic board (legacy) · %s</title>"
           % _BRAND_TITLE,
           "<meta http-equiv='refresh' content='30'>",
           "<style>%s</style>" % CSS, FIRE_JS,
           # oc-39/oc-40: living floor is the room; these tables are a demoted bench
           "<div style='background:#3a2c10;color:#ffb000;padding:10px 16px;"
           "font:13px/1.45 ui-monospace,Menlo,Consolas,monospace;"
           "border-bottom:1px solid #a07000'>"
           "<strong>Legacy tables.</strong> Day-to-day management lives on the "
           "<a href='/' style='color:#ffcc33'>Roster</a> "
           "(overview + bays). This page keeps the dense tables for power use. "
           "· <a href='/#overview' style='color:#ffcc33'>overview</a> · "
           "<a href='/report' style='color:#ffcc33'>full overview sheet</a>"
           "</div>",
           "<header><h1>%s <small>— classic board (legacy) · powered by WorkForce · "
           "who's employed, who's on shift, under what rules · :%d</small></h1></header>"
           % (_BRAND_TITLE.upper(), DEFAULT_PORT)]
    status = heartbeat_status(local_root)
    hb = read_heartbeat(local_root) or {}

    # ---- the glance row: is the workforce healthy, what happens next ----
    rows = []
    soonest: Optional[datetime.datetime] = None
    soonest_name = ""
    working, red, amber_n = [], 0, 0
    health_by: Dict[str, Dict[str, str]] = {}
    queue_by: Dict[str, str] = {}
    if roster:
        for name in sorted(roster.workers):
            w = roster.workers[name]
            q = _worker_queue(w)
            health = _worker_health(local_root, w, q)
            health_by[name], queue_by[name] = health, q
            shifts = parse_shifts(Ledger(os.path.join(local_root, "ledger"), name).tail(120), limit=5)
            last_real = next((s for s in shifts if not s["dry_run"]), None)
            cron = maybe_cron(w.schedule)
            nf_dt = cron.next_fire(_utcnow()) if cron else None
            if nf_dt and (soonest is None or nf_dt < soonest):
                soonest, soonest_name = nf_dt, name
            if last_real and last_real["outcome"] == "running":
                working.append(name)
            red += health["cls"] == "err"
            amber_n += health["cls"] == "amber"
            rows.append((name, w, q, health, last_real, cron, nf_dt))
        # the roster is a LIVE thing: on-shift first, then soonest fire,
        # unscheduled (manual) at the bottom
        far = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        rows.sort(key=lambda r: (
            0 if (r[4] and r[4]["outcome"] == "running") else 1,
            r[6] or far, r[0]))

    if hb.get("state") == "draining" and status != "stopped":
        n_inflight = len(hb.get("in_flight", []))
        daemon_tile = ("amber", "DRAINING · %d shift%s finishing"
                               % (n_inflight, "" if n_inflight == 1 else "s"))
    else:
        daemon_tile = {"running": ("ok", "running"), "stale": ("err", "STALE"),
                       "stopped": ("err", "down")}[status]
    ready = summary.get("ready_count", "?")
    stalled = summary.get("stalled_count", 0)
    out.append("<div class='tiles'>")
    out.append("<div class='tile'><div class='n %s'>●</div><div class='l'>daemon %s · tick %s</div></div>"
               % (daemon_tile[0], daemon_tile[1], html.escape(_ago(hb.get("last_tick", "")) if hb else "—")))
    out.append("<div class='tile'><div class='n'>%d</div><div class='l'>workers employed</div></div>"
               % len(rows))
    working_label = ", ".join(working[:2]) + ("…" if len(working) > 2 else "") if working else "idle"
    out.append("<div class='tile'><div class='n %s'>%d</div><div class='l'>on shift now · %s</div></div>"
               % ("amber" if working else "dim", len(working), html.escape(working_label)))
    nf_label = "%s · %s" % (soonest_name, _fmt_fire(soonest)) if soonest else "nothing scheduled"
    out.append("<div class='tile'><div class='n ok'>%s</div><div class='l'>next fire · %s</div></div>"
               % (soonest.strftime("%H:%M") if soonest else "—", html.escape(nf_label)))
    att_bits = []
    if red:
        att_bits.append("%d red" % red)
    if amber_n:
        att_bits.append("%d amber" % amber_n)
    if stalled:
        att_bits.append("%s stalled tickets" % stalled)
    out.append("<div class='tile'><div class='n %s'>%s</div><div class='l'>attention · %s</div></div>"
               % ("err" if (red or stalled) else ("amber" if amber_n else "ok"),
                  (red + amber_n + (1 if stalled else 0)) or "✓",
                  html.escape(" / ".join(att_bits) or "all clear")))
    out.append("<div class='tile'><div class='n amber'>%s</div><div class='l'>worklane ready · %s stalled</div></div>"
               % (ready, stalled))
    out.append("</div>")

    platforms = _platforms(local_root)
    by_pname = {p["name"].lower(): p for p in platforms}

    out.append("<h2>Workplaces — one workforce, many neighborhoods</h2>")
    if roster:
        out.append("<table><tr><th></th><th>workplace</th><th>workers</th>"
                   "<th>queue</th><th>surfaces</th></tr>")
        for g in _workplaces(roster, local_root, health_by, queue_by):
            plat = by_pname.get(str(g["label"]).lower())
            label = html.escape(str(g["label"]))
            if plat:
                dot = "<span class='%s'>●</span> " % plat["ok"]
                label = "%s<a href='%s'>%s</a>" % (dot, html.escape(plat["url"]), label)
            chips = " ".join(
                "<span class='%s'>●</span> <a href='/worker/%s'>%s</a>"
                % (health_by.get(n, {}).get("cls", "dim"), html.escape(n), html.escape(n))
                for n in g["workers"])
            surfaces = []
            if plat:
                surfaces.append("<a href='%s'>board</a>" % html.escape(plat["url"]))
            surfaces.extend("<a href='%s'>desk api</a>" % html.escape(d)
                            for d in sorted(g["desks"]))  # type: ignore[arg-type]
            out.append("<tr><td></td><td>%s<br><span class='dim sub'>%s</span></td>"
                       "<td>%s</td><td>%s</td><td>%s</td></tr>"
                       % (label, html.escape(str(g["workdir"])), chips,
                          ("<span class='amber'>%d</span>" % g["queue"])
                          if g["queue_known"] and g["queue"] else
                          ("0" if g["queue_known"] else "<span class='dim'>—</span>"),
                          " · ".join(surfaces) or "<span class='dim'>—</span>"))
        out.append("</table>")
    leftover = [p for p in platforms
                if not roster or p["name"].lower() not in
                {str(g["label"]).lower() for g in _workplaces(roster, local_root, health_by, queue_by)}]
    if leftover:
        out.append("<p class='sub'>" + " · ".join(
            "<span class='%s'>●</span> <a href='%s'>%s</a>"
            % (p["ok"], html.escape(p["url"]), html.escape(p["name"]))
            for p in leftover) + "</p>")

    out.append("<h2>Roster — employed here</h2>")
    if rows:
        out.append("<table><tr><th></th><th>worker</th><th>model</th>"
                   "<th>schedule → next fire</th><th>queue</th>"
                   "<th>last shift</th><th></th></tr>")
        for name, w, q, health, last_real, cron, nf_dt in rows:
            q_html = ("<span class='amber'>%s</span>" % q) if q not in ("0", "—", "?") else q
            qlink = _queue_human_link(w)
            if qlink and q not in ("—",):
                q_html = "<a href='%s' title='open these tickets in WorkLane'>%s</a>" % (
                    html.escape(qlink), q_html)
            if cron and status == "running":
                nf = "<span class='ok'>%s</span>" % html.escape(_fmt_fire(nf_dt))
            elif cron:
                nf = "<span class='err'>daemon down</span>"
            else:
                nf = "<span class='dim'>manual</span>"
            sched = ("<span class='amber'>%s</span> → %s" % (html.escape(w.schedule), nf)
                     if cron else nf)
            if last_real is None:
                last_html = "<span class='dim'>never dispatched</span>"
            else:
                cls = OUTCOME_CLS.get(last_real["outcome"], "dim")
                bits = [last_real["outcome"]]
                if last_real["passes"] > 1:
                    bits.append("%d passes" % last_real["passes"])
                detail = last_real["reason"]
                last_html = ("<span class='%s'>%s</span> <span class='dim'>· %s%s</span>"
                             % (cls, " · ".join(bits), _ago(last_real["ts"]),
                                (" · " + html.escape(detail[:48])) if detail else ""))
            fire_btn = ("<button class='fire' onclick=\"fire('%s')\">&#9654; dispatch</button>"
                        % html.escape(name)) if can_dispatch else ""
            out.append(
                "<tr><td class='%s' title='%s'>●</td>"
                "<td><a href='/worker/%s'>%s</a> <span class='tag'>%s</span></td>"
                "<td class='dim'>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (health["cls"], html.escape(health["why"]),
                   html.escape(name), html.escape(name), _kind_label(w.kind),
                   html.escape(w.model or "default"), sched,
                   q_html, last_html, fire_btn))
        out.append("</table>")
    else:
        out.append("<p class='dim'>No roster at local/roster.json — see roster.example.json.</p>")

    services = [r for r in rota if r["kind"] == "service"]
    legacy = [r for r in rota if r["kind"] != "service"]

    def rota_table(rows: List[Dict[str, str]]) -> None:
        out.append("<table><tr><th>launchd label</th><th>kind</th><th>pid</th>"
                   "<th>next fire</th><th>last exit</th><th>log</th></tr>")
        for r in rows:
            exit_cls = "ok" if r["last_exit"] == "0" else "err"
            log_cell = ("<a href='/legacylog/%s'>tail</a>" % html.escape(r["label"])
                        if r.get("log") else "<span class='dim'>—</span>")
            out.append("<tr><td>%s</td><td><span class='tag'>%s</span></td><td>%s</td>"
                       "<td class='dim'>%s</td><td class='%s'>%s</td><td>%s</td></tr>"
                       % (html.escape(r["label"]), r["kind"], r["pid"] or "—",
                          html.escape(r.get("next_fire") or "—"),
                          exit_cls, r["last_exit"], log_cell))
        out.append("</table>")

    if legacy:
        out.append("<h2>Legacy rota — migrating onto the daemon, one worker at a time</h2>")
        rota_table(legacy)
    out.append("<h2>Host services — the launchd that remains (servers, not workers)</h2>")
    if services:
        rota_table(services)
    else:
        out.append("<p class='dim'>launchctl unavailable or no service entries.</p>")

    out.append("<h2>WorkLane — throughput data</h2>")
    if summary:
        out.append("<p><span class='amber'>%s</span> ready · %s in flight · "
                   "<span class='%s'>%s stalled</span></p>"
                   % (summary.get("ready_count", "?"), summary.get("in_flight_count", "?"),
                      "err" if summary.get("stalled_count") else "dim",
                      summary.get("stalled_count", "?")))
    if activity:
        out.append("<table><tr><th>ticket</th><th>last signed activity</th></tr>")
        for e in activity:
            body = (e.get("body") or "").strip().splitlines()
            first = body[0] if body else ""
            tid = str(e.get("task_id", "?"))
            out.append("<tr><td><a class='amber' href='%s/admin/desk?open=%s'>#%s</a></td>"
                       "<td>%s</td></tr>"
                       % (html.escape(DESK), html.escape(tid), html.escape(tid),
                          html.escape(first[:140])))
        out.append("</table>")
    elif summary == {}:
        out.append("<p class='err'>WorkLane unreachable at %s.</p>" % html.escape(DESK))

    out.append("<footer><a href='/'>▶ back to the Roster</a> · "
               "<a href='/#overview'>overview</a> · "
               "<a href='/report'>full overview sheet</a> — "
               "this classic board keeps the dense tables. "
               "Workers feed WorkLane · the runner feeds the ledger. "
               "Rules render from disk — a lens, never a copy.</footer>")
    return "".join(out)


def _cli_label(worker: Worker) -> str:
    """Basename of command[0] for bay payroll subtitle (claude/grok/codex/…)."""
    cmd = worker.command or []
    if not cmd:
        return ""
    return os.path.basename(str(cmd[0]))


def scene_model(local_root: str) -> Dict[str, object]:
    """The dispatch scene's facts, computed from THIS engine's own state
   : the production room reads its own
    roster/heartbeat/ledger directly — never the city lens's /api/city.

    Pure: raw facts only, no per-second derivation. The scene JS computes
    on-shift / T-minus / progress client-side each tick, so viewer-truth
    tracks the wall clock without a re-fetch. Hot path stays network-free
    except a bounded desk probe for workers currently in_flight (live claim
    teaser on the bay) — idle floors still hit zero desk URLs.
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
            q = _worker_queue(w)
            health = _worker_health(local_root, w, q)
            cron = maybe_cron(w.schedule)
            nf = cron.next_fire(_utcnow()) if cron else None
            shifts = parse_shifts(
                Ledger(os.path.join(local_root, "ledger"), name).tail(60), limit=3)
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
            if name in in_flight_set:
                try:
                    holding = _worker_holdings(w)[:3]
                except Exception:
                    holding = []
            sec["workers"].append({  # type: ignore[union-attr]
                "name": name, "kind": _kind_label(w.kind),
                "display": w.display or "",
                "cli": _cli_label(w),
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
    # Host services / infrastructure cron (launchd) — floor-level only (L0).
    services: List[Dict[str, str]] = []
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


def render_scene(local_root: str, can_dispatch: bool = False) -> str:
    """The dispatch room as a live model: a daylight
    building cutaway floor — parchment rooms under the live sky, one phosphor
    CRT console per worker. Sectors wrap their bay grids as neighborhood
    interiors (``id="sector-<slug>"`` for the plat walk-in seam). oc-26 adds
    the floor establishing strip and four embodied bay states (on / imminent
    / waiting / fault) plus in-flight glow. The scene JS polls /api/scene
    every 20s and re-derives state / T-minus / progress / sky every second
    from the wall clock (setInterval only). Server ships skeleton + facts.

    Since the oc-20 root-merge this IS the room (served at /); the classic
    board survives at /board. ``can_dispatch`` (daemon in-process) arms the
    in-bay Dispatch verb — the only room with action buttons."""
    # Sixth naming amendment: city D0 mast is "[Folder] Roster" (Office / Desk
    # parity). Tab <title> keeps ProtocolCity — Roster · Workers.
    if _IN_CITY:
        folder = html.escape(_city_folder_name())
        h1 = ("%s <span class='fn'>Roster</span>" % folder)
        sub = "ProtocolCity · powered by WorkForce"
        # Suite doors = room names only (no "— Function"; Desk /
        # Roster already name the place; Roster holds staff + hired).
        doors = ("<a href='%s'>Office</a>"
                 "<a href='%s'>Desk</a>"
                 % (html.escape(CITYHALL), html.escape(DESK)))
    else:
        h1 = "WORKFORCE <span class='fn'>— WORKERS</span>"
        sub = "the roster"
        doors = ""  # standalone: one room, no doors
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>%s</title>"
        "<style>%s</style>"
        # Suite theme before paint — shared key with Office / Desk.
        "<script>(function(){var K='protocolcity-theme';try{"
        "var leg=localStorage.getItem('tp-theme');"
        "if(leg&&!localStorage.getItem(K))"
        "localStorage.setItem(K,leg==='dark'?'dark':'light');"
        "var t=localStorage.getItem(K)||'light';"
        "if(t!=='dark'&&t!=='light')t='light';"
        "document.documentElement.setAttribute('data-theme',t);"
        "}catch(e){document.documentElement.setAttribute('data-theme','light');}"
        "})();</script>"
        "</head><body class='crt'><div class='room'>"
        "<header class='masthead'>"
        # Suite mast: mast | center search cell | ops+doors.
        # oc-23/oc-24: antenna is the room sign; beacon = daemon lamp.
        "<div class='chrome-mast'>"
        "<svg class='mast-antenna' viewBox='-20 -118 42 122' aria-hidden='true'>"
        "<rect class='shed' x='-16' y='-22' width='32' height='22'/>"
        "<line class='wire' x1='0' y1='-22' x2='0' y2='-96' stroke-width='2.5'/>"
        "<line class='wire' x1='-14' y1='-40' x2='14' y2='-40' stroke-width='1.5'/>"
        "<line class='wire' x1='-10' y1='-60' x2='10' y2='-60' stroke-width='1.5'/>"
        "<line class='wire' x1='-6' y1='-79' x2='6' y2='-79' stroke-width='1.5'/>"
        "<circle id='mastBeacon' cx='0' cy='-101' r='4'>"
        "<animate attributeName='opacity' values='1;.3;1' dur='2s' repeatCount='indefinite'/></circle>"
        "<path class='wire' d='M-11 -107 a15 15 0 0 1 22 0' stroke-width='1.5'>"
        "<animate attributeName='opacity' values='.8;0;.8' dur='2.2s' repeatCount='indefinite'/></path>"
        "<path class='wire' d='M-17 -113 a23 23 0 0 1 34 0' stroke-width='1.2'>"
        "<animate attributeName='opacity' values='0;.6;0' dur='2.2s' repeatCount='indefinite'/></path>"
        "</svg>"
        "<div class='chrome-title'><h1>%s<span class='cursor'></span></h1>"
        "<div class='mast-sub'>%s</div></div></div>"
        "<div class='chrome-search' aria-hidden='true'></div>"
        "<div class='chrome-right'>"
        "<div class='chrome-ops'><div class='sys-meta'>"
        "<div><span class='lamp' id='carrierLamp'>●</span> "
        "<span id='carrier'>NO CARRIER</span></div>"
        "<div class='ops'><span id='opsLine'>DAEMON —</span>"
        " · <span id='wallTime'>--:--:--Z</span></div></div></div>"
        "<button type='button' class='theme-toggle' id='theme-toggle' "
        "title='Switch to dark theme' aria-label='Toggle dark or light theme'>&#9789;</button>"
        "<a class='settings-gear' href='/settings' title='Settings — daemon, root, Overview' "
        "aria-label='Settings'>&#9881;</a>"
        "<div class='suite-doors'>%s</div></div></header>"
        "<div class='above-floor'>"
        "<div class='alarm' id='alarm'>"
        "<span class='quiet'>AWAITING WORKFORCE TELEMETRY…</span></div>"
        # Exception vitals only (stuck/quiet/up-next). Hide when clear.
        "<section class='overview-bay clear' id='overview' aria-label='floor exceptions'>"
        "<div class='ov-head'>"
        "<span class='ov-title'>Exceptions</span>"
        "<a class='ov-more' href='/report'>Overview \u2192</a></div>"
        "<div class='ov-tiles' id='ovTiles'></div></section>"
        "</div>"
        # Permanent seats (You · Office staff) freeze above the hired floor.
        # Sector roofs own workplace nav — no duplicate floor strip.
        # Desk traffic tape removed from D0 (Desk owns closures).
        "<div class='permanent-row' id='permanent' aria-label='You and Office staff'></div>"
        "<div class='floor'>"
        "<div class='cabinet-rail' id='cabinetRail' aria-label='Filter by cabinet'></div>"
        "<div class='hired-floor' id='grid'>"
        "<div class='empty'>AWAITING WORKFORCE TELEMETRY…</div></div>"
        "<div id='runtimesStrip' class='runtimes-strip'></div></div>"
        # Footer: room verbs left, bay census right. No poll / desk traffic.
        "<footer class='bar'>"
        "<div class='foot-verbs'>"
        "<a class='first' href='/report'>Overview</a>"
        "<a class='quiet-link' href='/board'>tables</a>"
        "<a class='quiet-link' href='/settings'>Settings</a></div>"
        "<div id='sum' class='foot-sum'>0 ON ROSTER · 0 ON SHIFT · 0 STUCK</div></footer>"
        "</div>"
        "<div id='pfscrim' onclick='closePF()'></div>"
        "<aside id='pf' aria-label='personnel file or hire'>"
        "<div class='pf-head' id='pfHead'></div>"
        "<div class='pf-body' id='pfBody'></div>"
        "<div class='pf-foot' id='pfFoot'></div></aside>"
        "<script>var CAN_DISPATCH=%s;var DESK_URL=%s;</script>%s</body></html>"
        % (_BRAND_TITLE, SCENE_CSS, h1, sub, doors,
           "true" if can_dispatch else "false",
           json.dumps(DESK.rstrip("/")), SCENE_JS))


# Overview + Settings D1 furniture: same suite daylight DNA as Roster Home /
# Desk Overview / Office. Phosphor CRT stays on the *floor machine*
# only — not room chrome for strategic sheets (SUITE_PERIMETER).
REPORT_CSS = """
:root, [data-theme="light"] {
  /* CITY_DNA §5 + pc-162 — one daylight sheet; suite accent = verd */
  --page:#faf6ec; --bg:#faf6ec; --paper:#fffdf8; --paper-top:#efe8d5;
  --line:#c4b8a4; --ink:#2a241c; --dim:#6b6154;
  --verd:#3d7a6a; --ok:#2e7d4f; --warn:#a8681e; --fire:#a33327;
  --rule:#c9c2b0; --meter:#8a9a7a; --meter-g:#a8d4b8;
  color-scheme:light;
}
[data-theme="dark"] {
  --page:#1a1814; --bg:#1a1814; --paper:#252018; --paper-top:#322c24;
  --line:#4a4338; --ink:#f0eade; --dim:#a89f8e;
  --verd:#5a9a88; --ok:#4caf7d; --warn:#d9a441; --fire:#d4543f;
  --rule:#4a4338; --meter:#6a7a5a; --meter-g:#3a5a48;
  color-scheme:dark;
}
* { box-sizing:border-box; margin:0; padding:0; }
html,body { height:100%; }
body { background:var(--page); color:var(--ink);
  font:15px/1.45 "Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  display:flex; flex-direction:column; overflow:hidden; }
a { color:var(--verd); text-decoration:none; }
a:hover { color:var(--ink); text-decoration:underline; }
.dim { color:var(--dim); } .ok { color:var(--ok); } .warn { color:var(--warn); }
.masthead { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
  padding:12px 22px 11px; border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,var(--paper-top),var(--paper));
  box-shadow:0 2px 8px #2a241c12; }
.masthead h1 { font-size:18px; font-weight:700; letter-spacing:.06em;
  color:var(--ink); text-transform:none; }
.masthead h1 span.fn { color:var(--verd); letter-spacing:.12em; }
.mast-sub { font-size:12.5px; color:var(--dim); letter-spacing:.02em;
  margin-top:3px; text-transform:none; }
.room-back { font:600 12px/1 "IBM Plex Sans",system-ui,sans-serif;
  color:var(--ink); border:1px solid var(--line); border-radius:4px;
  padding:4px 11px; white-space:nowrap; background:var(--paper); }
.room-back:hover { border-color:var(--verd); color:var(--verd); text-decoration:none; }
.sys-meta { margin-left:auto; font:12px/1.5 "IBM Plex Sans",system-ui,sans-serif;
  color:var(--dim); text-align:right; }
.sys-meta .lamp { color:var(--dim); }
.sys-meta .lamp.on { color:var(--ok); }
.sys-meta .lamp.err { color:var(--fire); }
#wallTime { color:var(--ink); font-variant-numeric:tabular-nums; }
main.sheet { flex:1; min-height:0; overflow:auto; padding:18px 22px 28px;
  max-width:1180px; width:100%; margin:0 auto; }
h2 { font:700 10px/1 "IBM Plex Sans",system-ui,sans-serif; letter-spacing:.18em;
  color:var(--dim); text-transform:uppercase; margin:20px 0 9px;
  border-bottom:1px solid var(--line); padding-bottom:5px; }
h2:first-child { margin-top:0; }
.card { background:var(--paper); border:1px solid var(--line); border-radius:4px;
  box-shadow:0 1px 4px #2a241c0a; padding:14px 16px; }
.verdicts { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
  gap:10px; }
.verdict { background:var(--paper); border:1px solid var(--line); border-radius:4px;
  box-shadow:0 1px 3px #2a241c0a; padding:10px 14px;
  position:relative; overflow:hidden; }
.verdict::before { content:""; position:absolute; left:0; top:0; bottom:0;
  width:4px; background:var(--rule); }
.verdict.v-ok::before { background:var(--ok); }
.verdict.v-warn::before { background:var(--warn); }
.verdict.v-err::before { background:var(--fire); }
.verdict .nm { font-size:13px; font-weight:700; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.verdict .nm a { color:var(--ink); }
.verdict .nm a:hover { color:var(--verd); }
.verdict .vw { font:700 15px/1.2 "IBM Plex Sans",system-ui,sans-serif;
  letter-spacing:.04em; text-transform:uppercase; margin-top:3px; }
.verdict.v-ok .vw { color:var(--ok); }
.verdict.v-warn .vw { color:var(--warn); }
.verdict.v-err .vw { color:var(--fire); }
.verdict.v-dim .vw { color:var(--dim); }
.verdict .meta { font-size:11.5px; color:var(--dim); margin-top:3px; }
.rows { display:grid; grid-template-columns:150px minmax(0,1fr) 90px;
  gap:6px 12px; align-items:center; font-size:13px;
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
.rows .lbl { color:var(--dim); white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; }
.rows .lbl a { color:var(--verd); }
.rows .num { color:var(--dim); text-align:right;
  font-variant-numeric:tabular-nums; }
.rows.cap { grid-template-columns:110px minmax(0,1fr) minmax(200px,auto); }
/* chart bars are .meter, never .bar — footer.bar wears that class */
.meter { display:block; height:9px; border-radius:2px; background:var(--meter); }
.meter.g { background:var(--meter-g); }
.meter + .meter { margin-top:2px; }
.split { display:flex; height:22px; border-radius:3px; overflow:hidden;
  border:1px solid var(--line); background:var(--page); }
.split i { display:block; height:100%; }
.split .s-ok { background:var(--ok); opacity:.85; }
.split .s-warn { background:var(--warn); opacity:.9; }
.split .s-err { background:var(--fire); opacity:.9; }
.legend { display:flex; gap:16px; font-size:12.5px; margin-top:8px;
  flex-wrap:wrap; color:var(--dim);
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
.li { display:flex; justify-content:space-between; gap:10px; padding:7px 2px;
  border-bottom:1px dotted var(--line); font-size:13px; }
.li:last-child { border-bottom:0; }
.li .age { color:var(--warn); white-space:nowrap;
  font-variant-numeric:tabular-nums;
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
.tag { border:1px solid var(--line); border-radius:3px; padding:0 6px;
  font-size:11px; color:var(--dim); background:var(--page);
  font-family:"IBM Plex Sans",system-ui,sans-serif; }
.note { font-size:12px; color:var(--dim); margin-top:8px; }
.stamp { display:inline-block; border:2.5px solid var(--warn); color:var(--warn);
  border-radius:4px; padding:6px 14px; font:800 14px/1 "IBM Plex Sans",system-ui,sans-serif;
  letter-spacing:.08em; text-transform:uppercase; }
.stamp.hot { border-color:var(--fire); color:var(--fire); }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:18px;
  align-items:start; }
@media (max-width:900px){ .grid2 { grid-template-columns:1fr; } }
.winsel { display:flex; gap:8px; margin:0 0 10px;
  font:12px/1 "IBM Plex Sans",system-ui,sans-serif; }
.winsel a { border:1px solid var(--line); border-radius:3px; padding:3px 10px;
  color:var(--dim); }
.winsel a.on { color:var(--verd); border-color:var(--verd);
  background:#3d7a6a12; font-weight:700; }
footer.bar { flex:none; border-top:1px solid var(--line); padding:9px 22px;
  display:flex; justify-content:space-between; font-size:12px;
  color:var(--dim); background:var(--paper);
  font-family:"IBM Plex Sans",system-ui,sans-serif;
  flex-wrap:wrap; gap:6px; }
footer.bar a { color:var(--verd); margin-left:14px; }
footer.bar a:first-child { margin-left:0; }
"""

# setInterval only — never requestAnimationFrame (the proven constraint).
REPORT_JS = """
<script>
"use strict";
var R=null;
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function $(id){return document.getElementById(id);}
function p2(n){return (n<10?"0":"")+n;}
function parseTs(iso){if(!iso)return null;var t=Date.parse(iso);return isNaN(t)?null:t;}
function rel(iso){var t=parseTs(iso);if(t==null)return "\\u2014";
  var s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<60)return s+"s ago"; if(s<3600)return (s/60|0)+"m ago";
  if(s<86400)return (s/3600|0)+"h ago"; return (s/86400|0)+"d ago";}
function tminus(iso){var t=parseTs(iso);if(t==null)return "\\u2014";
  var s=Math.floor((t-Date.now())/1000); if(s<=0)return "\\u25B6 NOW";
  var h=s/3600|0,m=(s%3600)/60|0;
  return "T\\u2212"+(h>0?p2(h)+":":"")+p2(m)+":"+p2(s%60);}
function pct(n,d){return d>0?Math.max(n>0?2:0,Math.round(n/d*100)):0;}
function vCls(v){
  if(v==="faulting"||v==="wedged")return "v-err";
  if(v==="rough"||v==="starved")return "v-warn";
  if(v==="steady"||v==="on shift")return "v-ok";
  return "v-dim";}
function busyFmt(s){if(!s)return "0m";
  if(s<3600)return Math.round(s/60)+"m";
  return (s/3600).toFixed(1)+"h";}
function tokFmt(n){if(!n)return "";
  if(n>=1e6)return (n/1e6).toFixed(1)+"M tok";
  if(n>=1e3)return (n/1e3).toFixed(0)+"k tok";
  return n+" tok";}
function render(){
  var d=R; if(!d)return;
  var vz="";
  (d.workers||[]).forEach(function(w){
    var meta=w.total?(w.ok+" ok \\u00b7 "+w.fault+" fault"):"no shifts in window";
    if(w.queue&&w.queue!=="0"&&w.queue!=="\\u2014")meta+=" \\u00b7 Q="+esc(w.queue);
    vz+='<div class="verdict '+vCls(w.verdict)+'">'+
      '<div class="nm"><a href="/worker/'+esc(w.name)+'">'+esc(w.name)+'</a></div>'+
      '<div class="vw">'+esc(w.verdict)+'</div>'+
      '<div class="meta">'+meta+' \\u00b7 '+esc(w.sector)+'</div></div>';});
  $("verdictStrip").innerHTML=vz||'<div class="note">no workers on roster</div>';

  var maxS=1; (d.workers||[]).forEach(function(w){maxS=Math.max(maxS,w.total,w.ok);});
  var fz="";
  (d.workers||[]).forEach(function(w){
    fz+='<span class="lbl"><a href="/worker/'+esc(w.name)+'">'+esc(w.name)+'</a></span>'+
      '<span><span class="meter" style="width:'+pct(w.total,maxS)+'%"></span>'+
      '<span class="meter g" style="width:'+pct(w.ok,maxS)+'%"></span></span>'+
      '<span class="num">'+esc(w.total)+' / '+esc(w.ok)+'</span>';});
  $("flowRows").innerHTML=fz||'<span class="note">no workers</span>';

  var n_ok=0,n_warn=0,n_err=0,n_dim=0;
  (d.workers||[]).forEach(function(w){var c=vCls(w.verdict);
    if(c==="v-ok")n_ok++; else if(c==="v-warn")n_warn++;
    else if(c==="v-err")n_err++; else n_dim++;});
  var tot=Math.max(1,(d.workers||[]).length);
  $("splitBar").innerHTML=
    '<i class="s-ok" style="width:'+pct(n_ok,tot)+'%"></i>'+
    '<i class="s-warn" style="width:'+pct(n_warn,tot)+'%"></i>'+
    '<i class="s-err" style="width:'+pct(n_err,tot)+'%"></i>'+
    '<i style="flex:1"></i>';
  $("splitLegend").innerHTML=
    '<span><b class="ok">'+n_ok+' steady / on shift</b></span>'+
    '<span><b class="warn">'+n_warn+' rough / starved</b></span>'+
    '<span><b style="color:var(--fire)">'+n_err+' faulting / wedged</b></span>'+
    '<span>'+n_dim+' quiet / off rota</span>';

  var cap=(d.capacity||[]);
  var maxB=1; cap.forEach(function(v){maxB=Math.max(maxB,v.busy_secs);});
  var cz="";
  cap.forEach(function(v){
    var det=busyFmt(v.busy_secs)+" \\u00b7 "+v.shifts+" shifts \\u00b7 "+v.workers+" seats";
    var tk=tokFmt(v.tokens); if(tk)det+=" \\u00b7 "+tk;
    if(v.cost_usd)det+=" \\u00b7 $"+v.cost_usd.toFixed(2);
    cz+='<span class="lbl">'+esc(v.vendor)+'</span>'+
      '<span><span class="meter" style="width:'+pct(v.busy_secs,maxB)+'%"></span></span>'+
      '<span class="num">'+esc(det)+'</span>';});
  if($("capRows"))$("capRows").innerHTML=cz||'<span class="note">no shifts in window</span>';

  var dm=d.daemon||{};
  var fzz='<div class="li"><span>DAEMON</span><span class="age">'+
    esc(String(dm.status||"\\u2014").toUpperCase())+
    (dm.in_flight&&dm.in_flight.length?" \\u00b7 "+dm.in_flight.length+" IN FLIGHT":"")+
    (dm.last_tick?" \\u00b7 tick "+esc(rel(dm.last_tick)):"")+'</span></div>';
  (d.next_fires||[]).forEach(function(f){
    fzz+='<div class="li"><span><a href="/worker/'+esc(f.name)+'">'+esc(f.name)+
      '</a></span><span class="age" data-at="'+esc(f.at)+'">'+esc(tminus(f.at))+'</span></div>';});
  if(!(d.next_fires||[]).length)
    fzz+='<div class="note">nothing on the schedule \\u2014 all manual</div>';
  $("fireList").innerHTML=fzz;

  var dk=d.desk||{};
  if(!dk.ok){
    $("thruRows").innerHTML="";
    $("thruNote").innerHTML='WorkLane feed offline \\u2014 throughput numbers unavailable; '+
      'the panel holds until it returns';
  } else {
    var maxT=1; (dk.authors||[]).forEach(function(a){maxT=Math.max(maxT,a.filed,a.closed);});
    var rostered=(dk.authors||[]).filter(function(a){return a.worker;});
    var others=(dk.authors||[]).filter(function(a){return !a.worker;});
    var tz="";
    rostered.concat(others).forEach(function(a){
      var nm=a.worker
        ? '<a href="/worker/'+esc(a.worker)+'">'+esc(a.worker)+'</a>'
        : '<span class="dim">'+esc(a.author)+'</span>';
      tz+='<span class="lbl">'+nm+
        (a.worker&&a.worker!==a.author?' <span class="tag">'+esc(a.author)+'</span>':'')+'</span>'+
        '<span><span class="meter" style="width:'+pct(a.filed,maxT)+'%"></span>'+
        '<span class="meter g" style="width:'+pct(a.closed,maxT)+'%"></span></span>'+
        '<span class="num">'+esc(a.filed)+' / '+esc(a.closed)+'</span>';});
    $("thruRows").innerHTML=tz||'<span class="note">no signed activity in window</span>';
    $("thruNote").innerHTML='amber = filed \\u00b7 green = closed \\u00b7 signed on WorkLane, '+
      'joined to the roster by identity \\u00b7 unmatched signers dim';
  }

  var qz=(d.quiet||[]);
  $("quietStamp").textContent=qz.length+" QUIET";
  $("quietStamp").className="stamp"+(qz.length?"":" ")+(qz.filter(function(e){
    return e.owned;}).length?" hot":"");
  $("quietNote").innerHTML=qz.length
    ? "past "+esc(Math.round(d.quiet_hours/24*10)/10)+"d without a shift: "+
      qz.map(function(e){return '<a href="/worker/'+esc(e.name)+'">'+esc(e.name)+'</a>'+
        (e.hours==null?" (never)":" ("+(e.hours/24|0)+"d)");}).join(" \\u00b7 ")+
      " \\u2014 owned workers here mean the schedule is lying"
    : "every bay has fired inside the window";}
function poll(){
  fetch("/api/report?days="+WINDOW_DAYS,{cache:"no-store"}).then(function(r){
    if(!r.ok)throw 0; return r.json();
  }).then(function(d){R=d; render();
    $("carrierLamp").className="lamp on"; $("carrierLamp").textContent="\\u25CF";
    $("carrier").textContent="CARRIER LOCK \\u00b7 "+(d.generated_at?rel(d.generated_at):"LIVE");
  }).catch(function(){
    $("carrierLamp").className="lamp"+(R?" err":"");
    $("carrier").textContent=R?"CARRIER DROP \\u00b7 HOLDING":"NO CARRIER";});}
function tick(){
  var n=new Date();
  $("wallTime").textContent=p2(n.getUTCHours())+":"+p2(n.getUTCMinutes())+":"+
    p2(n.getUTCSeconds())+"Z  "+p2(n.getHours())+":"+p2(n.getMinutes())+":"+
    p2(n.getSeconds())+"L";
  document.querySelectorAll("[data-at]").forEach(function(el){
    el.textContent=tminus(el.getAttribute("data-at"));});}
tick(); poll();
setInterval(tick,1000); setInterval(poll,30000);
</script>
"""


def render_settings(local_root: str, can_dispatch: bool = False) -> str:
    """D1 Settings bay for Roster — daemon, WorkForce root, record doors.
    Room furniture (SUITE_PERIMETER), not a suite peer. Hire stays on the
    floor; vendor limits stay on vendor pages."""
    status = heartbeat_status(local_root)
    hb = read_heartbeat(local_root) or {}
    last_tick = _ago(hb.get("last_tick", "")) if hb else "—"
    daemon_line = "%s · tick %s" % (status or "—", last_tick)
    dispatch_line = ("in-process — Dispatch armed"
                     if can_dispatch else "board-only — start the daemon to arm Dispatch")
    root_esc = html.escape(local_root or "—")
    if _IN_CITY:
        h1 = "SETTINGS <span class='fn'>· ROSTER</span>"
        sub = "ProtocolCity · room furniture"
    else:
        h1 = "WORKFORCE <span class='fn'>— SETTINGS</span>"
        sub = "room furniture"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Settings · %s</title>"
        "<style>%s</style>"
        "<script>(function(){var K='protocolcity-theme';try{"
        "var t=localStorage.getItem(K)||localStorage.getItem('tp-theme')||'light';"
        "if(t!=='dark'&&t!=='light')t='light';"
        "document.documentElement.setAttribute('data-theme',t);"
        "}catch(e){document.documentElement.setAttribute('data-theme','light');}"
        "})();</script>"
        "</head><body>"
        "<header class='masthead'>"
        "<a class='room-back' href='/'>&larr; Roster</a>"
        "<div><h1>%s</h1><div class='mast-sub'>%s</div></div>"
        "<div class='sys-meta'>"
        "<div><span class='lamp%s'>●</span> daemon</div>"
        "<div id='wallTime'>--:--:--Z</div></div></header>"
        "<main class='sheet'>"
        "<h2>WorkForce root</h2>"
        "<div class='card'><code style='word-break:break-all'>%s</code>"
        "<div class='note'>Local employment store — platforms.json, ledger, "
        "heartbeats. Not the city root.</div></div>"
        "<h2>Daemon</h2>"
        "<div class='card'><span class='stamp'>%s</span>"
        "<div class='note'>%s</div></div>"
        "<h2>Poll</h2>"
        "<div class='card'><div class='note'>Scene census every 20s · wall "
        "clock every 1s · Overview on demand</div></div>"
        "<h2>Overview &amp; benches</h2>"
        "<div class='card'>"
        "<div class='li'><a href='/report'>Overview</a>"
        "<span class='age dim'>strategic view</span></div>"
        "<div class='li'><a href='/board'>tables</a>"
        "<span class='age dim'>classic board</span></div>"
        "<div class='li'><a href='/api/scene'>/api/scene</a>"
        "<span class='age dim'>floor facts</span></div>"
        "<div class='note'>Hire and Dispatch live on the floor · vendor "
        "limits stay on vendor settings</div></div>"
        "</main>"
        "<footer class='bar'>"
        "<div>A bay of the roster floor — "
        "<a href='/'>back to the floor</a>"
        "<a href='/report'>Overview</a></div>"
        "<div><span class='dim'>records: "
        "<a href='/api/scene'>/api/scene</a></span></div>"
        "</footer>"
        "<script>"
        "function tick(){var el=document.getElementById('wallTime');"
        "if(!el)return;el.textContent=new Date().toISOString().slice(11,19)+'Z';}"
        "tick();setInterval(tick,1000);</script></body></html>"
        % (_BRAND_TITLE, REPORT_CSS, h1, sub,
           " on" if status == "running" else (" err" if status in ("stale", "stopped") else ""),
           root_esc, html.escape(daemon_line), html.escape(dispatch_line))
    )


def render_report(local_root: str, days: Optional[int] = None) -> str:
    """Roster D1 Overview — strategic facts sheet (suite daylight DNA).
    Same cream/Georgia/verd sheet as Roster Home and Desk Overview; CRT
    phosphor stays on the floor machine only."""
    days = max(1, min(int(days or _REPORT_WINDOW_DAYS), 90))
    if _IN_CITY:
        h1 = "OVERVIEW <span class='fn'>· Roster</span>"
        sub = "ProtocolCity · powered by WorkForce · strategic facts"
    else:
        h1 = "OVERVIEW <span class='fn'>· WorkForce</span>"
        sub = "the strategic view of your floor"
    winsel = "".join(
        "<a href='/report?days=%d'%s>%dd</a>"
        % (d, " class='on'" if d == days else "", d)
        for d in _REPORT_WINDOWS)
    # Non-f-string script so empty braces don't break formatting.
    room_back_js = (
        "<script>(function(){try{"
        "var p=new URLSearchParams(location.search||'');"
        "var b=document.getElementById('roomBack');if(!b)return;"
        "var ret=p.get('return')||'';"
        "var from=(p.get('from')||'').toLowerCase();"
        "var ok=/^https?:\\/\\/127\\.0\\.0\\.1:8796\\/?$/.test(ret)"
        "||/^https?:\\/\\/localhost:8796\\/?$/.test(ret);"
        "if(from==='office'&&ret&&ok){b.href=ret;b.textContent='\\u2190 Office';}"
        "else if(document.referrer&&/:8796(?:\\/|$)/.test(document.referrer))"
        "{b.href='http://127.0.0.1:8796/';b.textContent='\\u2190 Office';}"
        "}catch(e){}})();</script>"
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Overview · %s</title>"
        "<style>%s</style>"
        "<script>(function(){var K='protocolcity-theme';try{"
        "var t=localStorage.getItem(K)||localStorage.getItem('tp-theme')||'light';"
        "if(t!=='dark'&&t!=='light')t='light';"
        "document.documentElement.setAttribute('data-theme',t);"
        "}catch(e){document.documentElement.setAttribute('data-theme','light');}"
        "})();</script>"
        "</head><body>"
        "<header class='masthead'>"
        "<a class='room-back' id='roomBack' href='/'>&larr; Roster</a>"
        "<div><h1>%s</h1><div class='mast-sub'>%s</div></div>"
        "<div class='sys-meta'>"
        "<div><span class='lamp' id='carrierLamp'>●</span> "
        "<span id='carrier'>NO CARRIER</span></div>"
        "<div id='wallTime'>--:--:--Z</div></div></header>"
        "%s"
        "<main class='sheet'>"
        "<h2>Verdicts · one word per worker</h2>"
        "<div class='verdicts' id='verdictStrip'>"
        "<div class='note'>pulling the shift records…</div></div>"
        "<div class='grid2' style='margin-top:6px'>"
        "<div>"
        "<h2>Shift flow · run vs clean, last %dd</h2>"
        "<div class='card'><div class='rows' id='flowRows'></div>"
        "<div class='note'>top bar = shifts run · green = clean exits · "
        "the gap is faults and skips</div></div>"
        "<h2>Capacity · burn by vendor, last %dd</h2>"
        "<div class='card'><div class='rows cap' id='capRows'></div>"
        "<div class='note'>busy time from the ledger · tokens/cost where the "
        "vendor CLI reports them · vendor limits stay on vendor "
        "settings pages — this is the city's relative burn</div></div>"
        "<h2>Worker state · where the workers stand</h2>"
        "<div class='card'><div class='split' id='splitBar'></div>"
        "<div class='legend' id='splitLegend'></div></div>"
        "<h2>The schedule's word · daemon + next fires</h2>"
        "<div class='card' id='fireList'></div>"
        "</div>"
        "<div>"
        "<h2>Throughput · filed vs closed per identity, last %dd</h2>"
        "<div class='winsel'>%s</div>"
        "<div class='card'><div class='rows' id='thruRows'></div>"
        "<div class='note' id='thruNote'>connecting…</div></div>"
        "<h2>Quiet list · employed but not firing</h2>"
        "<div class='card'><span class='stamp' id='quietStamp'>—</span>"
        "<div class='note' id='quietNote'></div></div>"
        "</div></div>"
        "</main>"
        "<footer class='bar'>"
        "<div>Roster Overview — "
        "<a href='/'>← back to Roster</a>"
        "<a href='/settings'>Settings</a>"
        "<a href='/board' style='opacity:.75'>tables (legacy)</a></div>"
        "<div><span class='dim'>facts: "
        "<a href='/api/report?days=%d'>/api/report</a></span></div>"
        "</footer>"
        "<script>var WINDOW_DAYS=%d;</script>%s</body></html>"
        % (_BRAND_TITLE, REPORT_CSS, h1, sub, room_back_js,
           days, days, days, winsel, days, days, REPORT_JS))


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


def _tail_page(title: str, subtitle: str, text: str) -> str:
    return ("<!doctype html><meta charset='utf-8'><title>%s</title><style>%s</style>"
            "<header><h1>%s <small>— %s</small></h1></header><pre>%s</pre>"
            "<p><a href='/'>&larr; board</a></p>"
            % (html.escape(title), CSS, html.escape(title), html.escape(subtitle),
               html.escape(text or "(empty)")))


def render_shifts(local_root: str, worker_name: str) -> Optional[str]:
    roster = _load_roster(local_root)
    if not roster or worker_name not in roster.workers:
        return None
    tail = Ledger(os.path.join(local_root, "ledger"), worker_name).tail(40)
    return _tail_page("SHIFT LEDGER", "%s · last 40 events" % worker_name, tail)


OUTCOME_CLS = {"ok": "ok", "error": "err", "skip": "dim", "warn": "amber",
               "running": "amber", "crashed": "err", "vendor_limit": "amber"}


def render_worker(local_root: str, name: str) -> Optional[str]:
    """The worker page — identity, resolved law stack, rules, shift history.

    The ratified law-lens design: the board never stores instructions;
    every law renders from disk at request time, so viewer-truth ==
    runtime-truth. The stack below is exactly what the next shift reads.
    """
    roster = _load_roster(local_root)
    if not roster or name not in roster.workers:
        return None
    w = roster.workers[name]
    cron = maybe_cron(w.schedule)
    status = heartbeat_status(local_root)
    next_fire = (_fmt_fire(cron.next_fire(_utcnow()))
                 if cron and status == "running" else
                 ("daemon down" if cron else "manual"))
    q = _worker_queue(w)

    out = ["<!doctype html><meta charset='utf-8'><title>%s · ProtocolCity — Workers</title>"
           % html.escape(name),
           "<style>%s</style>" % CSS,
           "<header><h1>%s <small>— %s · identity %s</small></h1></header>"
           % (html.escape(name.upper()), _kind_label(w.kind), html.escape(w.identity))]

    out.append("<p><span class='tag'>%s</span> · model %s · budget %ds · passes ≤%d · "
               "schedule <span class='amber'>%s</span> · next fire %s · queue %s</p>"
               % (_kind_label(w.kind), html.escape(w.model or "default"), w.budget_secs,
                  w.max_passes, html.escape(w.schedule or "manual"),
                  html.escape(next_fire), q))

    flags = _worker_flags(w)
    if flags:
        out.append("<h2>Flags — open tickets in lane (%d)</h2>" % len(flags))
        out.append("<table><tr><th>ticket</th><th>status</th><th>title</th></tr>")
        for f in flags:
            cls = "amber" if f.get("founder_gated") else "dim"
            lbl = "founder-gated" if f.get("founder_gated") else str(f.get("status") or "")
            href = html.escape(str(f.get("href") or ""))
            tid = html.escape(str(f.get("id") or ""))
            title = html.escape(str(f.get("title") or ""))
            link = "<a href='%s'>%s</a>" % (href, tid) if href else tid
            out.append("<tr><td>%s</td><td class='%s'>%s</td><td>%s</td></tr>"
                       % (link, cls, html.escape(lbl), title))
        out.append("</table>")

    out.append("<h2>Instructions — the stack the next shift reads (rendered from disk)</h2>")
    out.append("<table><tr><th>level</th><th>instruction</th><th>file</th><th>sha256</th>"
               "<th>last modified</th><th>recent changes (git)</th></tr>")
    for i, entry in enumerate(_law_stack(w)):
        gl = _git_law_log(entry["path"]) if entry["sha"] else ""
        link = ("<a href='/law/%s/stack%d'>%s</a>" % (html.escape(name), i,
                html.escape(os.path.basename(entry["path"])))
                if entry["sha"] else "<span class='err'>%s (missing)</span>"
                % html.escape(os.path.basename(entry["path"])))
        out.append("<tr><td class='amber'>%s</td><td>%s</td><td>%s<br>"
                   "<span class='dim'>%s</span></td><td class='dim'>%s</td>"
                   "<td class='dim'>%s</td><td class='dim'>%s</td></tr>"
                   % (entry["level"], entry["label"], link,
                      html.escape(entry["path"]), entry["sha"][:8] if entry["sha"] else "—",
                      entry["mtime"], html.escape(gl).replace("\n", "<br>")))
    out.append("</table>")

    rules = _contract_rules(w.contract)
    if rules:
        out.append("<h2>May / may not — from the contract's own headings</h2>")
        for s in rules:
            out.append("<p><span class='amber'>%s</span></p><pre>%s</pre>"
                       % (html.escape(s["title"]), html.escape(s["body"].strip())))

    shifts = parse_shifts(Ledger(os.path.join(local_root, "ledger"), name).tail(400), limit=20)
    out.append("<h2>Shift history — the ledger, read as shifts</h2>")
    if shifts:
        out.append("<table><tr><th>fired</th><th>outcome</th><th>passes</th>"
                   "<th>queue at start</th><th>reason</th></tr>")
        for s in shifts:
            cls = OUTCOME_CLS.get(s["outcome"], "dim")
            label = s["outcome"] + (" (dry)" if s["dry_run"] else "")
            out.append("<tr><td>%s</td><td class='%s'>%s</td><td>%s</td><td>%s</td>"
                       "<td class='dim'>%s</td></tr>"
                       % (html.escape(s["ts"]), cls, html.escape(label),
                          s["passes"] or "—", html.escape(str(s["queue"] or "—")),
                          html.escape(s["reason"])))
        out.append("</table>")
    else:
        out.append("<p class='dim'>never dispatched</p>")

    out.append("<p><a href='/shifts/%s'>raw ledger</a> · <a href='/out/%s'>worker output</a>"
               " · <a href='/'>&larr; board</a></p>"
               % (html.escape(name), html.escape(name)))
    return "".join(out)


def render_out(local_root: str, worker_name: str) -> Optional[str]:
    roster = _load_roster(local_root)
    if not roster or worker_name not in roster.workers:
        return None
    path = os.path.join(local_root, "run", "%s.out" % worker_name)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-80:]
    except OSError:
        lines = ["(no output captured yet — written from the next shift on)"]
    # a telemetry-armed CLI emits its final message as one JSON blob;
    # unfold it so the room stays readable — the raw file remains the record
    shown: List[str] = []
    for raw in lines:
        s = raw.strip()
        if s.startswith("{") and s.endswith("}") and len(s) > 200:
            try:
                doc = json.loads(s)
            except ValueError:
                doc = None
            if isinstance(doc, dict) and "result" in doc:
                shown.append(str(doc.get("result", "")).rstrip() + "\n")
                meta = []
                usage = doc.get("usage") or {}
                if isinstance(usage, dict):
                    if usage.get("input_tokens") is not None:
                        meta.append("tok in %s / out %s" % (usage.get("input_tokens"),
                                                            usage.get("output_tokens")))
                if doc.get("total_cost_usd") is not None:
                    meta.append("cost $%s" % doc["total_cost_usd"])
                if meta:
                    shown.append("[usage: %s]\n" % " · ".join(meta))
                continue
        shown.append(raw)
    text = "".join(shown)
    return _tail_page("WORKER OUTPUT", "%s · last shift's stdout+stderr" % worker_name, text)


def render_legacy_log(rota: List[Dict[str, str]], label: str) -> Optional[str]:
    """Tail a legacy runner's own log — path taken from its plist, never the URL."""
    match = [r for r in rota if r["label"] == label and r.get("log")]
    if not match:
        return None
    path = match[0]["log"]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = "".join(fh.readlines()[-60:])
    except OSError:
        text = "(log unreadable at %s)" % path
    return _tail_page("LEGACY LOG", "%s · %s" % (label, path), text)


def render_law(local_root: str, worker_name: str, which: str) -> Optional[str]:
    roster = _load_roster(local_root)
    if not roster or worker_name not in roster.workers:
        return None
    w: Worker = roster.workers[worker_name]
    stack = _law_stack(w)
    path = None
    level = ""
    label = ""
    if which.startswith("stack"):
        # path comes from the stack walk, never from the URL
        try:
            entry = stack[int(which[len("stack"):])]
            path = entry["path"]
            level = entry["level"]
            label = entry["label"]
        except (ValueError, IndexError):
            return None
    elif which in ("contract", "prompt"):
        path = w.contract if which == "contract" else w.prompt
        for entry in stack:
            if entry["label"] == which:
                level = entry["level"]
                label = entry["label"]
                break
    else:
        return None
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    badge = ("%s · %s" % (level, label)) if level else which
    fname = os.path.basename(path)
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>%s — %s · %s</title>"
        "<style>%s</style>"
        "<header><h1><span class='amber'>%s</span>"
        " <small>— %s · %s</small></h1></header>"
        "<p class='dim'>Rendered from disk at request time — this is byte-for-byte "
        "what the next shift reads.</p>"
        "<pre>%s</pre>"
        "<p><a href='/worker/%s'>← %s</a>"
        " · <a href='/'>Roster</a></p>"
        % (
            html.escape(worker_name), html.escape(badge), html.escape(fname),
            CSS,
            html.escape(badge),
            html.escape(worker_name), html.escape(fname),
            html.escape(text),
            html.escape(worker_name), html.escape(worker_name),
        )
    )


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

"""Freshness tokens and the transitional launchctl rota lens."""

import datetime
import hashlib
import json
import os
import plistlib
import re
import subprocess
from typing import Dict, List

from ...daemon import heartbeat_status, read_heartbeat
from ..._utils import _fmt_fire, _utc_iso_z, _utcnow
from ...ledger import parse_shifts
from ...schedule import calendar_intervals_to_cron, next_fire_utc
from .constants import (
    LAUNCH_AGENTS,
    _FAILURE_OUTCOMES,
    _RECENT_FAILURE_WINDOW_SECS,
)


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
        "ts": _utc_iso_z(),
        "in_flight": list(inflight),
        "daemon": daemon_state,
        "recent_failures": recent_failures(local_root),
    }


# Outcomes that count as "failure" for wf-118 pulse visibility.
_FAILURE_OUTCOMES = frozenset(("vendor_limit", "error", "crashed"))
_RECENT_FAILURE_WINDOW_SECS = 1800  # 30 minutes — matches daemon cadence


def recent_failures(local_root: str,
                    window_secs: int = _RECENT_FAILURE_WINDOW_SECS
                    ) -> List[Dict[str, str]]:
    """Workers with a failed shift in the last ``window_secs``.

    Surfaces vendor_limit / error / crashed outcomes for fast-fail shifts
    (e.g. 1-second vendor-limit hits) that finish before the suite Map polls.
    Only reads ledger files whose mtime falls within the window; bounded for
    sub-second callers on the pulse bus.
    """
    ledger_dir = os.path.join(local_root, "ledger")
    cutoff = (_utcnow() - datetime.timedelta(seconds=window_secs)).timestamp()
    out: List[Dict[str, str]] = []
    try:
        entries = os.listdir(ledger_dir)
    except OSError:
        return out
    for fn in sorted(entries):
        if not fn.endswith(".log"):
            continue
        path = os.path.join(ledger_dir, fn)
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            size = os.path.getsize(path)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(max(0, size - 4096))
                tail = fh.read()
        except OSError:
            continue
        shifts = parse_shifts(tail, limit=1)
        if not shifts:
            continue
        last = shifts[0]
        outcome = last.get("outcome") or ""
        reason = last.get("reason") or ""
        # Reclassify when the START line fell outside the 4 KB tail window
        if outcome == "error" and reason.startswith("vendor limit:"):
            outcome = "vendor_limit"
        if outcome not in _FAILURE_OUTCOMES:
            continue
        out.append({
            "worker": fn[:-4],
            "outcome": outcome,
            "reason": reason,
            "ts": last.get("end_ts") or last.get("ts") or "",
        })
    return out


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
        next_fire = (
            _fmt_fire(next_fire_utc(cron, _utcnow())) if cron else "calendar"
        )
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

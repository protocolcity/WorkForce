"""Roster loaders, workplace grouping, and identity helpers."""

import json
import os
import urllib.request
from typing import Dict, List, Optional, Tuple

from ...roster import Roster, RosterError, Worker
from ... import roster as roster_mod


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

    if getattr(w, "staff", False) in (True, 1, "1", "true", "True"):
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

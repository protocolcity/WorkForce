"""Capacity pool alerts — vendor_limit thrash → scarce For You signal.

Read-model over ledgers + optional desk drop. Host-neutral: no hard-coded
desk/workplace; desk URL comes from env (WL_DESK_URL / TP_DESK_URL) when
dropping. Never mutates roster/daemon; report write is opt-in via CLI.
"""

from __future__ import annotations

import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from .ledger import parse_shifts
from .runtimes import KNOWN_RUNTIMES

# Defaults for "pool blocked" thresholds.
DEFAULT_CONSECUTIVE = 3
DEFAULT_SEATS_SAME_HOUR = 2
# How many ledger lines to read per worker (best-effort tail).
_LEDGER_TAIL_LINES = 200
_SHIFT_LIMIT = 40

DEFAULT_DESK = os.environ.get("WL_DESK_URL") or os.environ.get(
    "TP_DESK_URL", "http://127.0.0.1:8799"
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _day_str(when: Optional[datetime.datetime] = None) -> str:
    return (when or _utcnow()).strftime("%Y-%m-%d")


def pool_for_command(command: Optional[List[str]]) -> str:
    """Map a worker command argv to a runtime pool name (CLI basename).

    Unknown basenames return the basename itself so alerts still group;
    empty command → \"\".
    """
    if not command:
        return ""
    base = os.path.basename(command[0] or "")
    return base


def is_capacity_outcome(outcome: str, reason: str = "") -> bool:
    """True when a shift counts as a capacity/vendor-limit failure."""
    if outcome == "vendor_limit":
        return True
    if outcome == "error" and (reason or "").startswith("vendor limit:"):
        return True
    return False


def consecutive_capacity_streak(shifts_newest_first: List[dict]) -> int:
    """Count consecutive capacity fails from the newest finished shift.

    A successful (ok) or non-capacity error breaks the streak. Running /
    skip / empty outcomes are skipped so mid-flight shifts do not mask a
    thrash pattern.
    """
    streak = 0
    for s in shifts_newest_first:
        outcome = s.get("outcome") or ""
        if outcome in ("running", "skip", "warn", "scope_deny"):
            continue
        if is_capacity_outcome(outcome, s.get("reason") or ""):
            streak += 1
            continue
        break
    return streak


def _parse_ts(ts: str) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        return datetime.datetime.strptime(
            ts, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def seats_capacity_same_hour(
    worker_shifts: Dict[str, List[dict]],
    when: Optional[datetime.datetime] = None,
) -> List[str]:
    """Workers whose newest capacity fail falls in the same UTC hour as ``when``."""
    when = when or _utcnow()
    hour_start = when.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + datetime.timedelta(hours=1)
    hit: List[str] = []
    for name, shifts in worker_shifts.items():
        for s in shifts:
            if not is_capacity_outcome(s.get("outcome") or "", s.get("reason") or ""):
                continue
            ts = _parse_ts(s.get("ts") or "")
            if ts is None:
                continue
            if hour_start <= ts < hour_end:
                hit.append(name)
            break  # only newest capacity-ish shift per worker
    return sorted(hit)


def _read_worker_shifts(local_root: str, worker_name: str) -> List[dict]:
    """Best-effort ledger tail → shifts (newest first). OSError → []."""
    path = os.path.join(local_root, "ledger", "%s.log" % worker_name)
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            text = "".join(fh.readlines()[-_LEDGER_TAIL_LINES:])
        return parse_shifts(text, limit=_SHIFT_LIMIT)
    except OSError:
        return []


def group_workers_by_pool(roster) -> Dict[str, List[str]]:
    """{pool_cli: [worker_name, ...]} for employed workers with a command."""
    pools: Dict[str, List[str]] = {}
    workers = getattr(roster, "workers", None) or {}
    for name, w in workers.items():
        pool = pool_for_command(getattr(w, "command", None) or [])
        if not pool:
            continue
        pools.setdefault(pool, []).append(name)
    for p in pools:
        pools[p].sort()
    return pools


def inbox_key_for_pool(pool: str, day: Optional[str] = None) -> str:
    """Idempotent report key: capacity-<pool> (date applied by drop label)."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (pool or "unknown"))
    return "capacity-%s" % (safe or "unknown")


def inbox_label(project: str, pool: str, day: Optional[str] = None) -> str:
    day = day or _day_str()
    return "inbox-report:%s:%s:%s" % (project, inbox_key_for_pool(pool), day)


def detect_capacity_alerts(
    roster,
    local_root: str,
    consecutive: int = DEFAULT_CONSECUTIVE,
    seats_same_hour: int = DEFAULT_SEATS_SAME_HOUR,
    when: Optional[datetime.datetime] = None,
    project: str = "workforce",
) -> List[dict]:
    """Return alert dicts for pools that look blocked.

    Each alert:
      pool, reason, workers, streak, seats_hour, inbox_key, inbox_label,
      glance, day

    Clear condition: newest finished shift is non-capacity (streak == 0) and
    fewer than K seats hit capacity this hour → no alert for that pool.
    """
    when = when or _utcnow()
    day = _day_str(when)
    consecutive = max(1, int(consecutive))
    seats_same_hour = max(1, int(seats_same_hour))
    alerts: List[dict] = []

    for pool, names in group_workers_by_pool(roster).items():
        by_worker: Dict[str, List[dict]] = {
            n: _read_worker_shifts(local_root, n) for n in names
        }
        streaks = {
            n: consecutive_capacity_streak(shifts)
            for n, shifts in by_worker.items()
        }
        max_streak = max(streaks.values()) if streaks else 0
        thrash_workers = sorted(n for n, s in streaks.items() if s >= consecutive)
        hour_hits = seats_capacity_same_hour(by_worker, when=when)

        reasons: List[str] = []
        if thrash_workers:
            reasons.append(
                "%d+ consecutive capacity fails on: %s"
                % (consecutive, ", ".join(thrash_workers))
            )
        if len(hour_hits) >= seats_same_hour:
            reasons.append(
                "%d seats capacity-failed this UTC hour: %s"
                % (len(hour_hits), ", ".join(hour_hits))
            )
        if not reasons:
            continue

        glance = (
            "Provider pool **%s** looks blocked — %s. "
            "Re-pin seats or wait for vendor reset; snooze this card when handled."
            % (pool, "; ".join(reasons))
        )
        alerts.append({
            "pool": pool,
            "reason": "; ".join(reasons),
            "workers": names,
            "thrash_workers": thrash_workers,
            "hour_workers": hour_hits,
            "streak": max_streak,
            "seats_hour": len(hour_hits),
            "inbox_key": inbox_key_for_pool(pool),
            "inbox_label": inbox_label(project, pool, day),
            "glance": glance,
            "day": day,
            "project": project,
        })

    # Prefer known runtime order, then alpha.
    order = {name: i for i, name in enumerate(KNOWN_RUNTIMES)}
    alerts.sort(key=lambda a: (order.get(a["pool"], 999), a["pool"]))
    return alerts


def format_capacity_report(alerts: List[dict], day: Optional[str] = None) -> str:
    """Markdown report body for disk / For You glance source."""
    day = day or _day_str()
    lines = [
        "# Capacity alerts · %s" % day,
        "",
        "High-risk provider pool blocks (vendor_limit / usage-limit streak).",
        "Source: WorkForce ledgers · detector `workforce capacity`.",
        "",
    ]
    if not alerts:
        lines.extend(["## Alerts", "", "_none_", ""])
        return "\n".join(lines)
    lines.append("## Alerts")
    lines.append("")
    for a in alerts:
        lines.append("### pool `%s`" % a["pool"])
        lines.append("")
        lines.append("- **Why:** %s" % a["reason"])
        lines.append("- **Max streak:** %d" % a["streak"])
        lines.append("- **Seats this hour:** %d" % a["seats_hour"])
        lines.append("- **Workers:** %s" % ", ".join(a["workers"]))
        lines.append("- **Inbox key:** `%s`" % a["inbox_key"])
        lines.append("")
        lines.append(a["glance"])
        lines.append("")
    lines.extend([
        "## Clear when",
        "",
        "- Next successful (non-capacity) shift on the pool, or",
        "- You **Mark read** / snooze the For You inbox card",
        "",
    ])
    return "\n".join(lines)


def write_capacity_report(
    local_root: str,
    alerts: List[dict],
    day: Optional[str] = None,
) -> str:
    """Write report under local/reports/capacity/; return absolute path."""
    day = day or _day_str()
    rel_dir = os.path.join(local_root, "reports", "capacity")
    os.makedirs(rel_dir, exist_ok=True)
    path = os.path.join(rel_dir, "%s.md" % day)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(format_capacity_report(alerts, day=day))
    return path


def _req(
    method: str,
    url: str,
    body: Optional[dict] = None,
    timeout: float = 20.0,
) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(err)
        except Exception:
            return {"ok": False, "error": err or str(e)}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "error": str(e)}


def find_open_by_label(desk: str, project: str, label: str) -> Optional[dict]:
    q = urllib.parse.urlencode(
        {"product": project, "label": label, "limit": 20}
    )
    data = _req("GET", "%s/api/admin/tasks?%s" % (desk.rstrip("/"), q))
    tasks = data.get("tasks") or data.get("items") or []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        st = str(t.get("status") or "").lower()
        if st in ("canceled", "done", "cancelled"):
            continue
        labs = [str(x) for x in (t.get("labels") or [])]
        if label in labs:
            return t
    return None


def desk_writes_allowed() -> bool:
    """Whether live desk POSTs/PATCHes are permitted.

    Order:
    1. ``WORKFORCE_ALLOW_DESK=1`` — explicit opt-in (rare integration tests).
    2. ``WORKFORCE_NO_DESK=1`` — suite kill-switch (conftest autouse).
    3. ``PYTEST_CURRENT_TEST`` set — refuse live drop under pytest by default.
    4. Otherwise allow (daemon / CLI ``--live`` path).

    Host-neutral: no hard-coded desk URL here; only whether writes may run.
    """
    allow = (os.environ.get("WORKFORCE_ALLOW_DESK") or "").strip().lower()
    if allow in ("1", "true", "yes"):
        return True
    no_desk = (os.environ.get("WORKFORCE_NO_DESK") or "").strip().lower()
    if no_desk in ("1", "true", "yes"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


def drop_capacity_for_you(
    alert: dict,
    report_path: str,
    desk: str = "",
    author: str = "you",
    dry_run: bool = True,
    city_rel_path: str = "",
) -> dict:
    """Create/refresh one human-gated inbox card for a capacity alert.

    Default ``dry_run=True`` — never gold You without an explicit drop.
    Idempotent on ``inbox_label`` for the day/pool.

    Under pytest (or ``WORKFORCE_NO_DESK=1``) live drops are refused even when
    callers pass ``dry_run=False`` — hermetic guard so daemon.tick tests cannot
    mint real For You cards. Opt in with
    ``WORKFORCE_ALLOW_DESK=1``.
    """
    desk = (desk or DEFAULT_DESK).rstrip("/")
    project = alert.get("project") or "workforce"
    key = alert["inbox_key"]
    day = alert.get("day") or _day_str()
    label = alert.get("inbox_label") or inbox_label(project, alert["pool"], day)
    rel = city_rel_path or report_path
    title = "Inbox · Capacity · pool %s blocked · %s" % (alert["pool"], day)
    description = (
        "**Report:** `%s`\n"
        "**Date:** %s · **Key:** `%s`\n\n"
        "## Glance\n\n"
        "%s\n\n"
        "## Where\n\n"
        "WorkForce ledgers · provider pool `%s`\n\n"
        "## Done when\n\n"
        "- Re-pin or wait out vendor limit, then Mark read / snooze\n"
        "- Next successful shift clears re-drop until thrash returns\n"
        % (rel, day, key, alert["glance"], alert["pool"])
    )
    labels = [
        "worker:you",
        "you:todo",
        "inbox-report",
        label,
        "product:%s" % project,
        "capacity",
        "for-you",
    ]
    receipt = {
        "ok": True,
        "project": project,
        "key": key,
        "day": day,
        "label": label,
        "pool": alert["pool"],
        "action": "none",
        "path": rel,
    }
    # Hermetic: refuse live desk even if caller asked for dry_run=False.
    hermetic_block = (not dry_run) and (not desk_writes_allowed())
    if hermetic_block:
        dry_run = True
        receipt["hermetic"] = True
    # dry_run never touches the desk — pure local receipt (token-free verify).
    if dry_run:
        receipt["action"] = "would_create"
        return receipt

    existing = find_open_by_label(desk, project, label)
    if existing:
        tid = str(existing.get("id") or "")
        body = {
            "title": title[:200],
            "description": description,
            "gate_type": "human",
            "gate_note": "Capacity pool blocked — act-now",
            "priority": 1,
        }
        out = _req(
            "PATCH",
            "%s/api/admin/tasks/%s?product=%s"
            % (desk, urllib.parse.quote(tid), project),
            body,
        )
        receipt["action"] = "updated"
        receipt["task_id"] = (out.get("task") or existing).get("id")
        receipt["api"] = out
        return receipt

    create_body = {
        "title": title[:200],
        "description": description,
        "author": author,
        "labels": labels,
        "priority": 1,
        "intake": "workforce-capacity",
        "project": project,
    }
    out = _req(
        "POST",
        "%s/api/admin/tasks?product=%s" % (desk, project),
        create_body,
    )
    task = out.get("task") or {}
    tid = str(task.get("id") or "")
    if not tid:
        receipt["ok"] = False
        receipt["error"] = out.get("error") or out
        receipt["action"] = "create_failed"
        return receipt

    # Apply human gate (act-now gold)
    gout = _req(
        "PATCH",
        "%s/api/admin/tasks/%s?product=%s"
        % (desk, urllib.parse.quote(tid), project),
        {
            "gate_type": "human",
            "gate_note": "Capacity pool blocked — act-now",
            "priority": 1,
        },
    )
    receipt["action"] = "created"
    receipt["task_id"] = tid
    receipt["api"] = {"create": out, "gate": gout}
    return receipt


def city_rel_report_path(report_abs: str, workspace: str = "") -> str:
    """Prefer city-root-relative path for For You dig-in."""
    if workspace:
        try:
            return os.path.relpath(report_abs, os.path.expanduser(workspace))
        except ValueError:
            pass
    # Fall back to product-relative local/… form
    marker = "%slocal%s" % (os.sep, os.sep)
    if marker in report_abs or report_abs.startswith("local" + os.sep):
        idx = report_abs.find("local" + os.sep)
        if idx != -1:
            return "workforce/" + report_abs[idx:].replace("\\", "/")
    return report_abs

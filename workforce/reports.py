"""Daily cost-rollup reports — persisted date-keyed snapshots.

Writes local/reports/cost/YYYY-MM-DD.md, one file per day, idempotent.
Read-model over ledger files; stdlib only; Python 3.9 floor.

Also exposes read-only supervisor pass evidence under
``local/reports/supervisor/`` for the board API (wf-254).
"""

import datetime
import json
import os
from typing import Dict, List, Optional, Tuple

from ._utils import _parse_iso_z, _utcnow
from .ledger import parse_shifts


_DEFAULT_THRESHOLD = 5.0
_DEFAULT_REPORT_WINDOW_DAYS = int(os.environ.get("WORKFORCE_REPORT_WINDOW_DAYS", "7"))


def _date_str(d: Optional[datetime.date] = None) -> str:
    d = d or datetime.datetime.now(datetime.timezone.utc).date()
    return d.strftime("%Y-%m-%d")


def collect_daily_cost(
    local_root: str,
    date: datetime.date,
    workers: Dict[str, object],
) -> Dict[str, dict]:
    """Aggregate per-vendor cost/token sums for all shifts on ``date`` (UTC).

    Returns ``{vendor_cli: {tok_in, tok_out, cost_usd, shifts}}``.
    """
    date_str = _date_str(date)
    ledger_root = os.path.join(local_root, "ledger")
    vendor_totals: Dict[str, dict] = {}

    for name, worker in workers.items():
        cmd = getattr(worker, "command", None) or []
        vendor = os.path.basename(cmd[0]) if cmd else "unknown"

        log_path = os.path.join(ledger_root, "%s.log" % name)
        if not os.path.exists(log_path):
            continue
        with open(log_path, "r", encoding="utf-8") as fh:
            text = fh.read()

        for shift in parse_shifts(text, limit=9999):
            ts = shift.get("ts", "")
            if not ts or ts[:10] != date_str:
                continue
            usage = shift.get("usage") or {}
            if vendor not in vendor_totals:
                vendor_totals[vendor] = {
                    "tok_in": 0.0, "tok_out": 0.0, "cost_usd": 0.0, "shifts": 0,
                }
            vt = vendor_totals[vendor]
            vt["tok_in"] += usage.get("tok_in", 0.0)
            vt["tok_out"] += usage.get("tok_out", 0.0)
            vt["cost_usd"] += usage.get("cost_usd", 0.0)
            vt["shifts"] += 1

    return vendor_totals


def format_daily_cost_report(
    vendor_totals: Dict[str, dict],
    date: datetime.date,
    cost_threshold: float = _DEFAULT_THRESHOLD,
) -> str:
    date_str = _date_str(date)
    grand_cost = sum(v["cost_usd"] for v in vendor_totals.values())
    grand_in = sum(v["tok_in"] for v in vendor_totals.values())
    grand_out = sum(v["tok_out"] for v in vendor_totals.values())
    grand_shifts = sum(v["shifts"] for v in vendor_totals.values())
    anomaly = grand_cost > cost_threshold

    lines: List[str] = [
        "# Daily Cost Report — %s" % date_str,
        "",
    ]
    if anomaly:
        lines += [
            "⚠️ **Threshold exceeded** — $%.4f > $%.2f" % (grand_cost, cost_threshold),
            "",
        ]
    lines += [
        "**Grand total** $%.4f | tok_in=%d tok_out=%d shifts=%d" % (
            grand_cost, int(grand_in), int(grand_out), grand_shifts,
        ),
        "",
        "## By vendor",
        "",
    ]
    if not vendor_totals:
        lines.append("_No shifts recorded._")
    else:
        for vendor in sorted(vendor_totals, key=lambda v: -vendor_totals[v]["cost_usd"]):
            vt = vendor_totals[vendor]
            lines.append(
                "- **%s** $%.4f | tok_in=%d tok_out=%d shifts=%d" % (
                    vendor, vt["cost_usd"], int(vt["tok_in"]), int(vt["tok_out"]), vt["shifts"],
                )
            )
    lines.append("")
    return "\n".join(lines)


def write_daily_cost_report(
    local_root: str,
    date: datetime.date,
    workers: Dict[str, object],
    cost_threshold: float = _DEFAULT_THRESHOLD,
) -> str:
    """Write local/reports/cost/YYYY-MM-DD.md; return absolute path.

    Idempotent: returns existing path without rewriting if the file already
    exists for this date (re-run on the same day is safe).
    """
    date_str = _date_str(date)
    rel_dir = os.path.join(local_root, "reports", "cost")
    os.makedirs(rel_dir, exist_ok=True)
    path = os.path.join(rel_dir, "%s.md" % date_str)
    if os.path.exists(path):
        return path
    vendor_totals = collect_daily_cost(local_root, date, workers)
    content = format_daily_cost_report(vendor_totals, date, cost_threshold=cost_threshold)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


_DEFAULT_SUPERVISOR_LIMIT = 20
_MAX_SUPERVISOR_LIMIT = 100


def _supervisor_dir(local_root: str) -> str:
    return os.path.join(local_root, "reports", "supervisor")


def _positive_int_field(data: dict, key: str) -> Optional[int]:
    val = data.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        return None
    return val


def _supervisor_row_from_report(data: object, evidence_file: str) -> Optional[Dict[str, object]]:
    """Parse one on-disk supervisor evidence file into an API row.

    Returns ``None`` when the file is malformed or cannot be trusted in full.
    """
    if not isinstance(data, dict):
        return None
    generated_at = data.get("generated_at")
    mode = data.get("mode")
    provider_ok = data.get("provider_ok")
    proposals = data.get("proposals")
    dispatched = data.get("dispatched")
    if (not isinstance(generated_at, str) or not generated_at
            or _parse_iso_z(generated_at) is None
            or not isinstance(mode, str)
            or not isinstance(provider_ok, bool)
            or not isinstance(proposals, list)
            or not isinstance(dispatched, list)):
        return None
    dispatch_attempted = _positive_int_field(data, "dispatch_attempted")
    dispatch_started = _positive_int_field(data, "dispatch_started")
    dispatch_completed = _positive_int_field(data, "dispatch_completed")
    dispatch_failed = _positive_int_field(data, "dispatch_failed")
    if None in (dispatch_attempted, dispatch_started, dispatch_completed, dispatch_failed):
        return None
    provider_error = data.get("provider_error")
    if provider_error is not None and not isinstance(provider_error, str):
        return None
    pass_outcome = data.get("pass_outcome") if "pass_outcome" in data else None
    if pass_outcome is not None and not isinstance(pass_outcome, str):
        return None
    dispatched_rows: List[Dict[str, str]] = []
    for item in dispatched:
        if not isinstance(item, dict):
            return None
        worker = item.get("worker")
        project = item.get("project")
        outcome = item.get("outcome")
        if (not isinstance(worker, str) or not worker
                or not isinstance(project, str) or not project
                or not isinstance(outcome, str)):
            return None
        dispatched_rows.append({
            "worker": worker,
            "project": project,
            "outcome": outcome,
        })
    for proposal in proposals:
        if not isinstance(proposal, dict) or not isinstance(proposal.get("valid"), bool):
            return None
    proposals_total = len(proposals)
    proposals_valid = sum(1 for p in proposals if p["valid"] is True)
    return {
        "generated_at": generated_at,
        "mode": mode,
        "pass_outcome": pass_outcome,
        "provider_ok": provider_ok,
        "provider_error": provider_error,
        "proposals_total": proposals_total,
        "proposals_valid": proposals_valid,
        "dispatch_attempted": dispatch_attempted,
        "dispatch_started": dispatch_started,
        "dispatch_completed": dispatch_completed,
        "dispatch_failed": dispatch_failed,
        "dispatched": dispatched_rows,
        "evidence_file": evidence_file,
    }


def _read_supervisor_row(path: str, basename: str) -> Optional[Dict[str, object]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _supervisor_row_from_report(raw, basename)


def scan_supervisor_passes(local_root: str) -> Tuple[List[Dict[str, object]], int]:
    """Read every supervisor evidence JSON file; newest ``generated_at`` first."""
    dir_path = _supervisor_dir(local_root)
    if not os.path.isdir(dir_path):
        return [], 0
    passes: List[Dict[str, object]] = []
    unreadable = 0
    for name in sorted(os.listdir(dir_path)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(dir_path, name)
        if not os.path.isfile(path):
            continue
        row = _read_supervisor_row(path, name)
        if row is None:
            unreadable += 1
        else:
            passes.append(row)
    passes.sort(
        key=lambda r: _parse_iso_z(str(r["generated_at"])),  # type: ignore[arg-type, return-value]
        reverse=True,
    )
    return passes, unreadable


def supervisor_api_model(
    local_root: str,
    limit: int = _DEFAULT_SUPERVISOR_LIMIT,
) -> Dict[str, object]:
    """Payload for GET /api/supervisor — past-pass evidence only."""
    bounded = max(1, min(int(limit or _DEFAULT_SUPERVISOR_LIMIT), _MAX_SUPERVISOR_LIMIT))
    passes, unreadable = scan_supervisor_passes(local_root)
    return {"ok": True, "passes": passes[:bounded], "unreadable": unreadable}


def supervisor_report_section(
    local_root: str,
    days: Optional[int] = None,
) -> Dict[str, object]:
    """Supervisor summary for /api/report using the report window."""
    window_days = max(1, min(int(days if days is not None else _DEFAULT_REPORT_WINDOW_DAYS), 90))
    since = _utcnow() - datetime.timedelta(days=window_days)
    passes, unreadable = scan_supervisor_passes(local_root)
    in_window: List[Dict[str, object]] = []
    for row in passes:
        ts = _parse_iso_z(str(row.get("generated_at", "")))
        if ts is not None and ts >= since:
            in_window.append(row)
    return {
        "passes_in_window": len(in_window),
        "last_pass": in_window[0] if in_window else None,
        "unreadable": unreadable,
    }

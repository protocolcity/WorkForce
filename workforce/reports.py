"""Daily cost-rollup reports — persisted date-keyed snapshots.

Writes local/reports/cost/YYYY-MM-DD.md, one file per day, idempotent.
Read-model over ledger files; stdlib only; Python 3.9 floor.
"""

import datetime
import os
from typing import Dict, List, Optional

from .ledger import parse_shifts


_DEFAULT_THRESHOLD = 5.0


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

"""Early-idle process-decay detection.

A shift that ends while the seat still has ungated ready work **without** a
lawful exit (empty / gated / budget / fault) is process decay — the drain-loop
smell from ALWAYS_WORK §4 / §9.

This module is pure read-model + classifiers. The engine may append a ledger
``WARN reason=early-idle`` breadcrumb at soft-ceiling stop; doctor and
workspace-efficiency roll up findings into **one** report line (never per-hand
Map gold).
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from .ledger import parse_shifts

# Soft-ceiling STOP reasons that leave chew on the feed when ready > 0.
# Engine multipass uses these when max_passes is hit before empty/budget/fault.
_SOFT_CEILING_RE = re.compile(
    r"^(single-pass complete|max passes \(\d+\))$"
)

# ALWAYS_WORK §4 exit classes that lawfully end a shift with (or without) ready.
# Matched as substrings / prefixes on STOP.reason (case-sensitive ledger text).
_LAWFUL_EMPTY = (
    "queue empty",
)
_LAWFUL_BUDGET = (
    "budget floor",
    "killed at budget",
    "drain hard cap",
)
# Progress / infra brakes: not "I closed one and left" — engine multipass stops.
_LAWFUL_PROGRESS = (
    "no progress",
    "restocked",
    "queue unprobed",
)
# Other orderly terminals that are not early-idle smells.
_LAWFUL_OTHER = (
    "dry-run",
    "fallback complete",
    "shift ended",  # external board skip path
)


def is_soft_ceiling_reason(reason: str) -> bool:
    """True when STOP reason is a multipass soft ceiling (single-pass / max N)."""
    return bool(_SOFT_CEILING_RE.match((reason or "").strip()))


def is_lawful_exit_reason(reason: str, outcome: str = "ok") -> bool:
    """True when the terminal reason is a lawful ALWAYS_WORK §4 exit (or brake).

    Outcomes other than ok / vendor_limit are treated as fault-side (lawful
    early end). Soft ceiling alone is **not** lawful when ready remains — that
    is the early-idle smell.
    """
    oc = (outcome or "").lower()
    if oc in ("error", "crashed", "scope_deny", "host_mutation_deny"):
        return True
    if oc == "vendor_limit":
        return True
    r = (reason or "").strip()
    if not r:
        return False
    for prefix in _LAWFUL_EMPTY + _LAWFUL_BUDGET + _LAWFUL_PROGRESS + _LAWFUL_OTHER:
        if r == prefix or r.startswith(prefix):
            return True
    if r.startswith("vendor limit:"):
        return True
    return False


def is_early_idle_stop(reason: str, outcome: str = "ok") -> bool:
    """True when a successful STOP smells like early-idle (soft ceiling).

    Callers must also confirm ready count > 0 (at stop or at patrol time).
    """
    if (outcome or "").lower() not in ("ok", "running"):
        return False
    if is_lawful_exit_reason(reason, outcome):
        return False
    return is_soft_ceiling_reason(reason)


def scan_early_idle(
    local_root: str,
    workers: Dict[str, object],
    *,
    shift_limit: int = 8,
) -> List[dict]:
    """Scan per-worker ledgers for recent early-idle breadcrumbs / smells.

    Returns a list of finding dicts (newest-first per worker, then by name)::

        {
          "worker": str,
          "ts": str,
          "reason": str,       # STOP reason or WARN stop=
          "ready": int|None,   # leftover ready when known
          "source": "warn"|"stop",
          "passes": int,
        }

    Prefer ``WARN reason=early-idle`` (engine breadcrumb). Fall back to STOP
    soft-ceiling reasons when no WARN was recorded (pre-wf-176 ledgers).
    Jobs without a ready feed are skipped. Does **not** re-probe the desk —
    leftover ready is taken from the WARN when present.
    """
    ledger_root = os.path.join(local_root, "ledger")
    findings: List[dict] = []

    for name in sorted(workers.keys()):
        worker = workers[name]
        kind = getattr(worker, "kind", "") or ""
        # Lanes with a queue are the drain seats; jobs are usually single-pass
        # by design and are not process-decay early-idle targets.
        if kind and kind != "lane":
            continue
        queue_url = (getattr(worker, "queue_url", "") or "").strip()
        if kind == "lane" and not queue_url:
            continue

        log_path = os.path.join(ledger_root, "%s.log" % name)
        if not os.path.isfile(log_path):
            continue
        try:
            with open(log_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue

        # Prefer explicit WARN early-idle lines in the raw file (newest last).
        warn_hits = _parse_early_idle_warns(text)
        if warn_hits:
            hit = warn_hits[-1]
            findings.append({
                "worker": name,
                "ts": hit.get("ts", ""),
                "reason": hit.get("stop") or hit.get("reason", "early-idle"),
                "ready": _int_or_none(hit.get("ready")),
                "source": "warn",
                "passes": _int_or_none(hit.get("on_pass")) or 0,
            })
            continue

        # Fallback: latest ok soft-ceiling STOP without a WARN breadcrumb.
        for shift in parse_shifts(text, limit=shift_limit):
            if shift.get("outcome") != "ok":
                continue
            reason = shift.get("reason") or ""
            if not is_early_idle_stop(reason, "ok"):
                continue
            # Heuristic: START queue was >1, or unknown queue with ≥1 pass —
            # single-pass on a one-ticket feed is not decay.
            q = shift.get("queue")
            qn = _int_or_none(q)
            passes = int(shift.get("passes") or 0)
            if qn is not None and qn <= 1 and passes <= 1:
                continue
            if qn is None and passes < 1:
                continue
            findings.append({
                "worker": name,
                "ts": shift.get("end_ts") or shift.get("ts") or "",
                "reason": reason,
                "ready": None if qn is None else max(0, qn - passes),
                "source": "stop",
                "passes": passes,
            })
            break

    return findings


def format_early_idle_report(findings: List[dict]) -> str:
    """One rollup line for doctor / efficiency (no per-hand gold spam)."""
    if not findings:
        return "Early-idle: none"
    seats = sorted({f["worker"] for f in findings})
    detail = []
    for f in findings[:12]:
        ready = f.get("ready")
        ready_s = "?" if ready is None else str(ready)
        detail.append("%s ready=%s via %s" % (f["worker"], ready_s, f.get("source")))
    return (
        "Early-idle: %d seat(s) stopped with ready open (ALWAYS_WORK §9 / wf-176): %s"
        % (len(seats), "; ".join(detail))
    )


def _parse_early_idle_warns(text: str) -> List[dict]:
    """Extract WARN reason=early-idle keyvals from raw ledger lines."""
    hits: List[dict] = []
    for line in text.splitlines():
        parts = line.strip().split(" ")
        if len(parts) < 2 or parts[1] != "WARN":
            continue
        rest = " ".join(parts[2:])
        kv: Dict[str, str] = {"ts": parts[0]}
        for m in re.finditer(r'(\w+)=("(?:[^"]*)"|\S+)', rest):
            kv[m.group(1)] = m.group(2).strip('"')
        if kv.get("reason") != "early-idle":
            continue
        hits.append(kv)
    return hits


def _int_or_none(v: Optional[object]) -> Optional[int]:
    if v is None or v == "" or v == "?":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

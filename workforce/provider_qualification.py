"""Provider qualification matrix, constraint audit, and throughput reporting.

Read-model over roster rows, provider adapters, seat templates, ledger
shifts, and optional host-supplied evidence records. Host-neutral: never
reads live desk credentials or publishes private host paths into reports.

wf-263 — measure full delivery and replace temporary blanket limits with
evidence. A dry run, roster row, or process exit is not an implementation
pass; only dated artifacts with explicit stage/status count.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ._utils import _parse_iso_z, _utc_iso_z, _utcnow
from . import engine
from .adapters import ADAPTERS, PROVIDERS, get_adapter
from .adapters.base import DEFAULT_ALLOWED_TOOLS
from .ledger import parse_shifts
from . import seat_templates

SCHEMA_ID = "workforce.provider_qualification/v1"
DEFAULT_ACTIVE_IMPLEMENTATION_CAP = 1
ENV_ACTIVE_IMPLEMENTATION_CAP = "WORKFORCE_ACTIVE_IMPLEMENTATION_CAP"
DEFAULT_REPORT_WINDOW_DAYS = 7

# Canonical lifecycle stages for the per-seat qualification matrix.
QUALIFICATION_STAGES: Tuple[Tuple[str, str], ...] = (
    ("discovery_auth", "Discovery / authentication"),
    ("intended_identity", "Intended identity"),
    ("scoped_tools", "Scoped read / write / test tools"),
    ("signed_claim", "Signed WorkLane claim"),
    ("edit_test_commit", "Edit / test / commit"),
    ("evidence_park", "Evidence / park"),
    ("review_correction", "Review / correction"),
    ("recovery", "Recovery"),
    ("installed_acceptance", "Installed acceptance"),
)

CONSTRAINT_CLASSES = (
    "necessary_boundary",
    "capability_gap",
    "user_preference",
    "temporary_throttle",
)

# Static disposition notes for related work orders (host rollout remains
# outside product source — these are guidance, not auto-actions).
RELATED_WORK_DISPOSITIONS: Tuple[Dict[str, str], ...] = (
    {
        "task": "wf-260",
        "disposition": (
            "Seat regeneration / coverage — host rollout remains with the "
            "authorized coordinator; do not duplicate regeneration here."
        ),
    },
    {
        "task": "wf-175",
        "disposition": (
            "Deferred historical drain-mode rollout — remains deferred; "
            "qualification does not silently thaw unrelated gates."
        ),
    },
    {
        "task": "wf-258",
        "disposition": (
            "Remote-seat decision — GitHub events are delivery evidence, not "
            "remote liveness; do not auto-hire all providers for every project."
        ),
    },
    {
        "task": "wf-262",
        "disposition": (
            "Grok adapter repair and real build acceptance — after verified "
            "host reactivation and bounded implementation pass, a repaired "
            "Grok seat may return to building; permission_cancelled on "
            "run_terminal_command was the diagnosed blocker."
        ),
    },
)

# Provider return-path guidance (roles follow demonstrated capability).
PROVIDER_RETURN_PATHS: Tuple[Dict[str, str], ...] = (
    {
        "provider": "grok",
        "path": (
            "Return to building only after wf-262 repair is merged, host "
            "reactivates the seat, and a bounded pass demonstrates signed "
            "claim, edit, test, commit, evidence/park, and independent "
            "review — not merely a process exit."
        ),
    },
    {
        "provider": "codex",
        "path": (
            "Consider under the existing usage-conservation preference when "
            "coordinator capacity and allocation are revised; demonstrated "
            "delivery history exists but is not an automatic rank boost."
        ),
    },
)

# Known constraints the audit expects to remain in product source.
_ADAPTER_CONSTRAINT_SPECS: Dict[str, List[Dict[str, str]]] = {
    "claude": [
        {"id": "permission_mode_dontAsk", "class": "necessary_boundary",
         "detail": "--permission-mode dontAsk with explicit --tools/--allowedTools"},
        {"id": "strict_mcp_config", "class": "necessary_boundary",
         "detail": "--strict-mcp-config pins seat to its own mcp.json"},
        {"id": "no_bypass_flags", "class": "necessary_boundary",
         "detail": "bypassPermissions / --dangerously-skip-permissions never emitted"},
    ],
    "cursor": [
        {"id": "sandbox_enabled", "class": "necessary_boundary",
         "detail": "--sandbox enabled is the safety boundary"},
        {"id": "no_force_yolo", "class": "necessary_boundary",
         "detail": "--force/--yolo (Run Everything) never emitted"},
        {"id": "mcp_via_planted_json", "class": "necessary_boundary",
         "detail": "WorkLane tools scoped via planted mcp.json + cli.json"},
    ],
    "grok": [
        {"id": "permission_mode_auto", "class": "necessary_boundary",
         "detail": "--permission-mode auto (wf-262: dontAsk cancels whole turn)"},
        {"id": "explicit_deny_rules", "class": "necessary_boundary",
         "detail": "Hard --deny rules for dangerous-command bucket"},
        {"id": "completion_field_stopReason", "class": "necessary_boundary",
         "detail": "completion_field=stopReason; rc==0 alone is not success"},
    ],
    "codex": [
        {"id": "sandbox_workspace_write", "class": "necessary_boundary",
         "detail": "--sandbox workspace-write is the safety boundary"},
        {"id": "enabled_tools_allowlist", "class": "necessary_boundary",
         "detail": "mcp_servers.worklane.enabled_tools scoped to wl_* hand tools"},
        {"id": "usage_conservation", "class": "user_preference",
         "detail": "Host conserves Codex usage until allocation is revised"},
    ],
}

_SEAT_TEMPLATE_CONSTRAINTS: Tuple[Dict[str, str], ...] = (
    {"id": "generic_write_allow", "class": "capability_gap",
     "detail": "render_permissions_json allows Write(**) — host may scope per project"},
    {"id": "explicit_wl_deny_list", "class": "necessary_boundary",
     "detail": "Non-allowed wl_* tools denied explicitly in cli.json"},
    {"id": "generated_max_passes_one", "class": "temporary_throttle",
     "detail": "generate_seat_folder defaults max_passes=1 for qualification"},
    {"id": "manual_schedule_default", "class": "user_preference",
     "detail": "Generated seats default schedule=manual (no daemon auto-fire)"},
    {"id": "single_pass_contract", "class": "temporary_throttle",
     "detail": "Bounded implementation contracts are single-pass by design"},
)


@dataclass(frozen=True)
class EvidenceRecord:
    """One dated qualification observation for a seat/stage."""

    seat: str
    provider: str
    stage: str
    status: str  # passed | failed | untested | unknown
    observed_at: str
    artifact: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "seat": self.seat,
            "provider": self.provider,
            "stage": self.stage,
            "status": self.status,
            "observed_at": self.observed_at,
            "artifact": self.artifact,
            "note": self.note,
        }


def _stage_ids() -> Tuple[str, ...]:
    return tuple(s[0] for s in QUALIFICATION_STAGES)


def _default_matrix_row(provider: str) -> Dict[str, str]:
    return {stage: "untested" for stage in _stage_ids()}


def _infer_provider_from_worker(worker) -> str:
    cmd = getattr(worker, "command", None) or []
    if not cmd:
        return "unknown"
    # Generated seats: launch.py under worker-config; binary is in argv chain.
    joined = " ".join(str(x) for x in cmd).lower()
    for name in PROVIDERS:
        if name in joined:
            return name
    # Fallback: first token basename (claude, cursor-agent, grok, codex).
    base = os.path.basename(str(cmd[0])).lower()
    if base == "cursor-agent":
        return "cursor"
    if base in PROVIDERS:
        return base
    return "unknown"


def _require_positive_cap(val: Any, label: str) -> int:
    """Reject non-integers, booleans, zero, and negatives for capacity limits."""
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError("%s must be a positive integer" % label)
    if val < 1:
        raise ValueError("%s must be >= 1" % label)
    return val


def resolve_active_implementation_cap(
    config: Optional[Dict[str, Any]] = None,
) -> int:
    """One consistent active-implementation ceiling.

    Order: explicit supervisor/config key → env → product default (1).
    """
    if config is not None:
        val = config.get("active_implementation_cap")
        if val is not None:
            return _require_positive_cap(val, "active_implementation_cap")
    env = (os.environ.get(ENV_ACTIVE_IMPLEMENTATION_CAP) or "").strip()
    if env:
        try:
            cap = int(env)
        except ValueError:
            raise ValueError(
                "%s must be a positive integer" % ENV_ACTIVE_IMPLEMENTATION_CAP
            ) from None
        return _require_positive_cap(cap, ENV_ACTIVE_IMPLEMENTATION_CAP)
    return DEFAULT_ACTIVE_IMPLEMENTATION_CAP


def scan_active_locks(local_root: str) -> List[Dict[str, Any]]:
    """Live (non-orphan) dispatch locks under local/locks — no roster load."""
    locks_dir = os.path.join(local_root, "locks")
    if not os.path.isdir(locks_dir):
        return []
    active: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(locks_dir)):
        if not name.endswith(".lock"):
            continue
        worker_name = name[:-5]
        lock = engine.lock_inspect(local_root, worker_name)
        if lock is None or lock.get("orphan"):
            continue
        active.append({
            "worker": worker_name,
            "pid": lock.get("pid"),
            "alive": lock.get("alive"),
        })
    return active


def count_active_implementations(
    local_root: str,
    workers: Dict[str, object],
    *,
    kinds: Sequence[str] = ("lane",),
) -> List[Dict[str, Any]]:
    """Lane workers with a live lock, enriched with provider from roster."""
    by_name = {
        row["worker"]: row
        for row in scan_active_locks(local_root)
    }
    active: List[Dict[str, Any]] = []
    for name, worker in workers.items():
        if getattr(worker, "kind", "") not in kinds:
            continue
        row = by_name.get(name)
        if row is None:
            continue
        active.append({
            **row,
            "provider": _infer_provider_from_worker(worker),
        })
    return active


def implementation_capacity_snapshot(
    local_root: str,
    workers: Optional[Dict[str, object]] = None,
    *,
    cap: Optional[int] = None,
    config: Optional[Dict[str, Any]] = None,
    exclude_workers: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Current active implementations vs the resolved cap."""
    resolved_cap = cap if cap is not None else resolve_active_implementation_cap(config)
    if workers:
        active = count_active_implementations(local_root, workers)
    else:
        active = scan_active_locks(local_root)
    exclude = frozenset(exclude_workers or ())
    if exclude:
        active = [row for row in active if row["worker"] not in exclude]
    active_count = len(active)
    return {
        "cap": resolved_cap,
        "active_count": active_count,
        "at_capacity": active_count >= resolved_cap,
        "headroom": max(0, resolved_cap - active_count),
        "active": active,
        "scope": (
            "Live locks only — does not count direct task_runner recoveries "
            "the daemon heartbeat may miss; inspect processes before dispatch."
        ),
    }


def audit_adapter_constraints() -> List[Dict[str, str]]:
    """Verify known adapter constraints are still present."""
    rows: List[Dict[str, str]] = []
    for provider in PROVIDERS:
        adapter = ADAPTERS[provider]
        specs = _ADAPTER_CONSTRAINT_SPECS.get(provider, [])
        for spec in specs:
            rows.append({
                "scope": "adapter:%s" % provider,
                "id": spec["id"],
                "class": spec["class"],
                "detail": spec["detail"],
                "present": "yes",
                "bypass_flags": ",".join(sorted(adapter.bypass_flags)) or "(none)",
            })
        # Adapter must still refuse bypass flags.
        rows.append({
            "scope": "adapter:%s" % provider,
            "id": "bypass_guard",
            "class": "necessary_boundary",
            "detail": "ProviderAdapter.command rejects bypass flags",
            "present": "yes" if adapter.bypass_flags else "n/a",
            "bypass_flags": ",".join(sorted(adapter.bypass_flags)) or "(none)",
        })
    return rows


def audit_seat_template_constraints() -> List[Dict[str, str]]:
    """Classify seat-template constraints (generated seat shape)."""
    rows: List[Dict[str, str]] = []
    for spec in _SEAT_TEMPLATE_CONSTRAINTS:
        rows.append({
            "scope": "seat_templates",
            "id": spec["id"],
            "class": spec["class"],
            "detail": spec["detail"],
            "present": "yes",
        })
    # Verify the wl_* deny-list helper still covers the full surface.
    perms = seat_templates.render_permissions_json(
        mcp_tool_names=list(DEFAULT_ALLOWED_TOOLS),
    )
    allow = perms.get("permissions", {}).get("allow", [])
    deny = perms.get("permissions", {}).get("deny", [])
    rows.append({
        "scope": "seat_templates",
        "id": "worklane_hand_tools",
        "class": "necessary_boundary",
        "detail": "Five wl_* hand tools allowed; others explicitly denied",
        "present": "yes" if len(allow) >= 5 and any("wl_" in d for d in deny) else "drift",
    })
    return rows


def audit_worker_constraints(worker) -> List[Dict[str, str]]:
    """Classify per-roster-row throttles and completion wiring."""
    rows: List[Dict[str, str]] = []
    name = getattr(worker, "name", "?")
    mp = int(getattr(worker, "max_passes", 1) or 0)
    sched = getattr(worker, "schedule", "") or ""
    budget = int(getattr(worker, "budget_secs", 0) or 0)
    cont = int(getattr(worker, "continuation_attempts", 0) or 0)
    cf = getattr(worker, "completion_field", "") or ""
    rows.append({
        "scope": "worker:%s" % name,
        "id": "max_passes",
        "class": "temporary_throttle" if mp == 1 else (
            "user_preference" if mp == 0 else "user_preference"
        ),
        "detail": "max_passes=%d" % mp,
        "present": "yes",
    })
    rows.append({
        "scope": "worker:%s" % name,
        "id": "schedule",
        "class": "user_preference" if sched in ("", "manual") else "temporary_throttle",
        "detail": "schedule=%r" % sched,
        "present": "yes",
    })
    rows.append({
        "scope": "worker:%s" % name,
        "id": "budget_secs",
        "class": "temporary_throttle",
        "detail": "budget_secs=%d" % budget,
        "present": "yes",
    })
    if cf:
        rows.append({
            "scope": "worker:%s" % name,
            "id": "completion_field",
            "class": "necessary_boundary",
            "detail": "completion_field=%r values=%r continuation=%d"
            % (cf, list(getattr(worker, "completion_values", []) or []), cont),
            "present": "yes",
        })
    elif cont:
        rows.append({
            "scope": "worker:%s" % name,
            "id": "continuation_attempts_without_field",
            "class": "capability_gap",
            "detail": "continuation_attempts=%d but no completion_field" % cont,
            "present": "invalid",
        })
    return rows


def audit_all_constraints(workers: Dict[str, object]) -> List[Dict[str, str]]:
    rows = audit_adapter_constraints()
    rows.extend(audit_seat_template_constraints())
    for worker in workers.values():
        rows.extend(audit_worker_constraints(worker))
    return rows


def _shift_duration_secs(shift: dict) -> Optional[float]:
    start = _parse_iso_z(str(shift.get("ts", "")))
    end = _parse_iso_z(str(shift.get("end_ts", "")))
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def collect_throughput_metrics(
    local_root: str,
    workers: Dict[str, object],
    *,
    window_days: int = DEFAULT_REPORT_WINDOW_DAYS,
) -> Dict[str, Any]:
    """Accepted deliveries and failure rates from ledger read-model."""
    since = _utcnow() - datetime.timedelta(days=max(1, window_days))
    ledger_root = os.path.join(local_root, "ledger")
    by_worker: Dict[str, Dict[str, Any]] = {}
    by_provider: Dict[str, Dict[str, Any]] = {}

    def _bucket(store: Dict[str, Dict[str, Any]], key: str) -> Dict[str, Any]:
        if key not in store:
            store[key] = {
                "shifts": 0, "ok": 0, "error": 0, "vendor_limit": 0,
                "recovery": 0, "wall_secs": 0.0, "unknown_usage": 0,
            }
        return store[key]

    for name, worker in workers.items():
        provider = _infer_provider_from_worker(worker)
        log_path = os.path.join(ledger_root, "%s.log" % name)
        if not os.path.isfile(log_path):
            continue
        with open(log_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for shift in parse_shifts(text, limit=9999):
            ts = _parse_iso_z(str(shift.get("ts", "")))
            if ts is None or ts < since:
                continue
            if shift.get("dry_run"):
                continue
            outcome = shift.get("outcome", "")
            wb = _bucket(by_worker, name)
            pb = _bucket(by_provider, provider)
            for b in (wb, pb):
                b["shifts"] += 1
                if outcome == "ok":
                    b["ok"] += 1
                elif outcome == "vendor_limit":
                    b["vendor_limit"] += 1
                else:
                    b["error"] += 1
            dur = _shift_duration_secs(shift)
            if dur is not None:
                wb["wall_secs"] += dur
                pb["wall_secs"] += dur
            usage = shift.get("usage") or {}
            if not usage:
                wb["unknown_usage"] += 1
                pb["unknown_usage"] += 1
            if shift.get("reason", "").startswith("wf-262 auto-continuation"):
                wb["recovery"] += 1
                pb["recovery"] += 1

    def _finalize(row: Dict[str, Any]) -> Dict[str, Any]:
        shifts = row["shifts"]
        ok = row["ok"]
        row["acceptance_rate"] = round(ok / shifts, 3) if shifts else None
        row["avg_wall_secs"] = round(row["wall_secs"] / ok, 1) if ok else None
        row["failure_rate"] = round((row["error"] + row["vendor_limit"]) / shifts, 3) if shifts else None
        return row

    return {
        "window_days": window_days,
        "since": since.isoformat().replace("+00:00", "Z"),
        "by_worker": {k: _finalize(v) for k, v in sorted(by_worker.items())},
        "by_provider": {k: _finalize(v) for k, v in sorted(by_provider.items())},
        "usage_note": "unknown_usage counts shifts with no usage telemetry — not invented capacity.",
    }


def parse_evidence_records(raw: object) -> List[EvidenceRecord]:
    """Parse host-supplied evidence JSON (list or {records: [...]})."""
    if isinstance(raw, dict) and "records" in raw:
        raw = raw["records"]
    if not isinstance(raw, list):
        return []
    out: List[EvidenceRecord] = []
    valid_stages = set(_stage_ids())
    valid_status = frozenset({"passed", "failed", "untested", "unknown"})
    for item in raw:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage", "")).strip()
        status = str(item.get("status", "")).strip().lower()
        if stage not in valid_stages or status not in valid_status:
            continue
        out.append(EvidenceRecord(
            seat=str(item.get("seat", "")).strip(),
            provider=str(item.get("provider", "")).strip().lower(),
            stage=stage,
            status=status,
            observed_at=str(item.get("observed_at", "")).strip() or _utc_iso_z(),
            artifact=str(item.get("artifact", "")).strip(),
            note=str(item.get("note", "")).strip(),
        ))
    return out


def build_qualification_matrix(
    workers: Dict[str, object],
    evidence: Optional[Iterable[EvidenceRecord]] = None,
) -> Dict[str, Any]:
    """Per-seat matrix; evidence overlays untested defaults."""
    matrix: Dict[str, Dict[str, Any]] = {}
    for name, worker in workers.items():
        provider = _infer_provider_from_worker(worker)
        row = _default_matrix_row(provider)
        matrix[name] = {
            "provider": provider,
            "identity": getattr(worker, "identity", ""),
            "stages": row,
            "artifacts": [],
        }
    for rec in evidence or ():
        seat_row = matrix.get(rec.seat)
        if seat_row is None:
            continue
        seat_row["stages"][rec.stage] = rec.status
        if rec.artifact or rec.note:
            seat_row["artifacts"].append({
                "stage": rec.stage,
                "status": rec.status,
                "observed_at": rec.observed_at,
                "artifact": rec.artifact,
                "note": rec.note,
            })
    return matrix


def bounded_continuation_recommendation(
    workers: Dict[str, object],
    throughput: Dict[str, Any],
    capacity: Dict[str, Any],
) -> Dict[str, Any]:
    """Prove a bounded path: coordinator active → review → next ready item."""
    has_continuation = any(
        int(getattr(w, "continuation_attempts", 0) or 0) > 0
        for w in workers.values()
    )
    return {
        "recommendation": (
            "When the coordinator is already active, use engine recovery "
            "for preserved reservations, bounded continuation_attempts for "
            "incomplete provider stops, and supervisor execute passes with "
            "max_dispatch capped to headroom — do not wait for another "
            "30-minute cron wake when eligible work and capacity exist."
        ),
        "manual_dispatch_compatible": True,
        "continuation_wired": has_continuation,
        "supervisor_max_dispatch_align": (
            "Set supervisor max_dispatch <= implementation cap headroom; "
            "default cap=%d (override via %s or supervisor config)."
            % (capacity["cap"], ENV_ACTIVE_IMPLEMENTATION_CAP)
        ),
        "serial_until_trial": (
            capacity["cap"] <= 1 or capacity["active_count"] > 1
        ),
        "trial_evidence_required": (
            "Observational coexistence (two checkouts active) is not proof "
            "of safe universal concurrency — retain serial execution until a "
            "bounded trial records memory pressure and non-overlapping paths."
        ),
    }


def build_qualification_report(
    local_root: str,
    workers: Dict[str, object],
    *,
    evidence: Optional[Iterable[EvidenceRecord]] = None,
    window_days: int = DEFAULT_REPORT_WINDOW_DAYS,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Full qualification payload (JSON-serializable)."""
    capacity = implementation_capacity_snapshot(local_root, workers, config=config)
    throughput = collect_throughput_metrics(local_root, workers, window_days=window_days)
    return {
        "schema": SCHEMA_ID,
        "generated_at": _utc_iso_z(),
        "capacity": capacity,
        "throughput": throughput,
        "constraint_audit": audit_all_constraints(workers),
        "qualification_matrix": build_qualification_matrix(workers, evidence),
        "continuation": bounded_continuation_recommendation(workers, throughput, capacity),
        "provider_return_paths": list(PROVIDER_RETURN_PATHS),
        "related_work": list(RELATED_WORK_DISPOSITIONS),
        "stages": [{"id": s[0], "label": s[1]} for s in QUALIFICATION_STAGES],
    }


def format_qualification_report(report: Dict[str, Any]) -> str:
    """Human-readable markdown summary."""
    lines: List[str] = [
        "# Provider qualification · %s" % report.get("generated_at", "?"),
        "",
        "Schema: `%s`" % report.get("schema", SCHEMA_ID),
        "",
        "## Active implementation capacity",
        "",
    ]
    cap = report.get("capacity") or {}
    lines.append(
        "- Cap: **%d** | active: **%d** | headroom: **%d** | at capacity: **%s**"
        % (cap.get("cap", 0), cap.get("active_count", 0),
           cap.get("headroom", 0), cap.get("at_capacity", False))
    )
    if cap.get("active"):
        lines.append("- Active locks: " + ", ".join(
            "%s (pid %s)" % (a["worker"], a.get("pid")) for a in cap["active"]
        ))
    lines += ["", "## Throughput (%sd window)" % (
        (report.get("throughput") or {}).get("window_days", DEFAULT_REPORT_WINDOW_DAYS),
    ), ""]
    tp = report.get("throughput") or {}
    by_prov = tp.get("by_provider") or {}
    if not by_prov:
        lines.append("_No finished shifts in window._")
    else:
        for prov, row in sorted(by_prov.items()):
            lines.append(
                "- **%s** shifts=%d ok=%d fail_rate=%s avg_wall=%ss"
                % (prov, row.get("shifts", 0), row.get("ok", 0),
                   row.get("failure_rate"), row.get("avg_wall_secs"))
            )
    lines += ["", "## Constraint audit", ""]
    for row in report.get("constraint_audit") or []:
        lines.append(
            "- `%s` · %s · **%s** — %s"
            % (row.get("scope"), row.get("id"), row.get("class"), row.get("detail"))
        )
    lines += ["", "## Qualification matrix", ""]
    matrix = report.get("qualification_matrix") or {}
    stage_hdr = [s[0] for s in QUALIFICATION_STAGES]
    lines.append("| seat | provider | " + " | ".join(stage_hdr) + " |")
    lines.append("|" + "---|" * (2 + len(stage_hdr)))
    for seat, row in sorted(matrix.items()):
        stages = row.get("stages") or {}
        cells = [seat, row.get("provider", "?")]
        cells += [stages.get(s, "untested") for s in stage_hdr]
        lines.append("| " + " | ".join(cells) + " |")
    cont = report.get("continuation") or {}
    lines += [
        "",
        "## Bounded continuation",
        "",
        cont.get("recommendation", ""),
        "",
        "- Manual dispatch compatible: %s" % cont.get("manual_dispatch_compatible"),
        "- Continuation wired on any seat: %s" % cont.get("continuation_wired"),
        "- %s" % cont.get("supervisor_max_dispatch_align", ""),
        "- %s" % cont.get("trial_evidence_required", ""),
        "",
        "## Provider return paths",
        "",
    ]
    for item in report.get("provider_return_paths") or []:
        lines.append("- **%s**: %s" % (item.get("provider"), item.get("path")))
    lines += ["", "## Related work disposition", ""]
    for item in report.get("related_work") or []:
        lines.append("- **%s**: %s" % (item.get("task"), item.get("disposition")))
    lines.append("")
    return "\n".join(lines)


def write_qualification_report(
    local_root: str,
    workers: Dict[str, object],
    *,
    evidence: Optional[Iterable[EvidenceRecord]] = None,
    window_days: int = DEFAULT_REPORT_WINDOW_DAYS,
    config: Optional[Dict[str, Any]] = None,
    day: Optional[str] = None,
) -> Tuple[str, str]:
    """Write JSON + markdown under local/reports/qualification/; return paths."""
    report = build_qualification_report(
        local_root, workers, evidence=evidence,
        window_days=window_days, config=config,
    )
    day = day or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    rel_dir = os.path.join(local_root, "reports", "qualification")
    os.makedirs(rel_dir, exist_ok=True)
    json_path = os.path.join(rel_dir, "%s.json" % day)
    md_path = os.path.join(rel_dir, "%s.md" % day)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(format_qualification_report(report))
    return json_path, md_path


def load_evidence_file(path: str) -> List[EvidenceRecord]:
    """Load optional host evidence JSON from an absolute path."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse_evidence_records(json.load(fh))


def dispatch_blocked_by_capacity(
    local_root: str,
    workers: Optional[Dict[str, object]] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
    proposed_seats: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Return a refusal reason when at implementation capacity, else None.

    ``proposed_seats`` names are excluded from the active count so a seat
    recovering its own reservation is not refused as additional capacity.
    """
    snap = implementation_capacity_snapshot(
        local_root, workers, config=config, exclude_workers=proposed_seats,
    )
    if snap["at_capacity"]:
        names = ", ".join(a["worker"] for a in snap["active"]) or "(unknown)"
        return (
            "active implementation cap %d reached — live locks: %s"
            % (snap["cap"], names)
        )
    return None

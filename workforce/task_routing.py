"""Explain task-fit decisions for registered execution seats.

Backlog assignment is distinct from a live claim. Implementation qualification
checks the assigned seat; recommendations never mutate WorkLane ownership.
The supervisor and task runner enforce configuration binding at launch.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import capability_qualification as cq
from .routing_policy import SeatCapability
from ._utils import _parse_iso_z, _utc_iso_z, _utcnow

SCHEMA_ID = "workforce.task_routing/v2"

WORKER_LABEL_PREFIX = "worker:"
WORK_KIND_LABEL_PREFIX = "work-kind:"
RISK_LABEL_PREFIX = "risk:"

# Versioned work-kind -> capability_qualification evaluation category
# (blueprint/docs/specs/PROVIDER_ROUTING.md "Work kinds" / "Default routing
# table"). A work kind absent from both this table and FIXED_WORK_KINDS has
# no defined interpretation and refuses automated routing.
WORK_KIND_CATEGORY: Dict[str, str] = {
    "design": "architecture",
    "implement": "bounded_edit",
    "review": "review",
    "docs": "docs_consistency",
    "recovery": "recovery",
}

# Existing implementation assignments require explicit ownership transfer before
# another seat executes. Supervision/reporting use separate configured runners.
_EXISTING_SEAT_WORK_KINDS = frozenset({"implement"})
FIXED_WORK_KINDS = frozenset({"supervise", "report"})

DEFAULT_RISK = "low"

# Statuses that make a task permanently ineligible for automated routing --
# closed work is never a routing candidate regardless of its labels.
_TERMINAL_STATUSES = frozenset({"done", "canceled"})

# A live signed claim per PROCESS §5: routing never reassigns work already
# active or parked under an owner, even though it still carries the
# worker:<name> label that a plain backlog assignment also carries.
_LIVE_CLAIM_STATUSES = frozenset({"in_progress", "in_review"})

# Gate classes that make a task ineligible for automated routing even when
# it otherwise looks ready (mirrors supervisor.py's _BLOCKED_GATE_TYPES).
_BLOCKED_GATE_TYPES = frozenset({"human", "deferred", "tracking"})

_DECISIONS = (
    "existing_claim_preserved",
    "fixed_work_kind",
    "confirmed_existing_seat",
    "recommended",
    "refused",
)


@dataclass(frozen=True)
class RoutingReceipt:
    """One explained routing outcome for one task — never a dispatch order."""

    task_id: str
    generated_at: str
    decision: str
    reason: str
    work_kind: Optional[str] = None
    category: Optional[str] = None
    risk: str = DEFAULT_RISK
    existing_worker: Optional[str] = None
    project: str = ""
    recommendation: Optional[Dict[str, Any]] = None
    refusals: Tuple[Dict[str, str], ...] = ()
    excluded: Tuple[Dict[str, str], ...] = ()
    schema: str = SCHEMA_ID

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "generated_at": self.generated_at,
            "decision": self.decision,
            "reason": self.reason,
            "work_kind": self.work_kind,
            "category": self.category,
            "risk": self.risk,
            "existing_worker": self.existing_worker,
            "project": self.project,
            "recommendation": self.recommendation,
            "refusals": list(self.refusals),
            "excluded": list(self.excluded),
        }

    @property
    def accepted(self) -> bool:
        """Only a qualified recommendation permits a new dispatch.

        Preserved claims and separate job kinds never authorize a new writer.
        """
        return self.decision in (
            "confirmed_existing_seat", "recommended",
        )


def _refuse(task_id: str, now: datetime.datetime, reason: str, **over: Any) -> RoutingReceipt:
    fields: Dict[str, Any] = {
        "task_id": task_id,
        "generated_at": _utc_iso_z(now),
        "decision": "refused",
        "reason": reason,
    }
    fields.update(over)
    return RoutingReceipt(**fields)


def _labeled_values(labels: Sequence[str], prefix: str) -> List[str]:
    seen: List[str] = []
    for label in labels:
        if not label.startswith(prefix):
            continue
        value = label[len(prefix):].strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def _task_labels(task: Dict[str, Any]) -> List[str]:
    raw = task.get("labels")
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _task_id(task: Dict[str, Any]) -> str:
    return str(task.get("id") or task.get("task_id") or "").strip()


def _scope_seats(
    task: Dict[str, Any],
    seats: Sequence[SeatCapability],
    project: str,
    host: str,
) -> Tuple[List[SeatCapability], List[Dict[str, str]]]:
    """Narrow *seats* to this task's project/host/tool scope.

    Scope is never inferred from provider preference — a seat registered
    for a different project or host, or missing a tool the task requires,
    is excluded before capability qualification runs at all, with its own
    reason distinct from a capacity/evidence refusal. *project* and *host*
    are always non-empty by the time this is called (the caller refuses
    before scoping when either is missing) so a candidate can never be
    admitted merely because a constraint went unstated.
    """
    required_raw = task.get("required_tools")
    required_tools: Tuple[str, ...] = ()
    if isinstance(required_raw, list):
        required_tools = tuple(
            str(t).strip() for t in required_raw if str(t).strip()
        )

    scoped: List[SeatCapability] = []
    excluded: List[Dict[str, str]] = []
    for seat in seats:
        cand = seat.candidate
        if cand.project != project:
            excluded.append({
                "candidate": cand.candidate_id(),
                "reason": "%s (worker=%s) is registered for project %r, not %r"
                          % (cand.candidate_id(), seat.worker, cand.project, project),
            })
            continue
        if cand.host != host:
            excluded.append({
                "candidate": cand.candidate_id(),
                "reason": "%s (worker=%s) is registered for host %r, not %r"
                          % (cand.candidate_id(), seat.worker, cand.host, host),
            })
            continue
        missing = [t for t in required_tools if t not in cand.tools]
        if missing:
            excluded.append({
                "candidate": cand.candidate_id(),
                "reason": "%s (worker=%s) is missing required tools: %s"
                          % (cand.candidate_id(), seat.worker, ", ".join(missing)),
            })
            continue
        scoped.append(seat)
    return scoped, excluded


def route_task(
    task: Dict[str, Any],
    seats: Sequence[SeatCapability],
    results_by_candidate: Dict[str, List[cq.EvaluationResult]],
    *,
    host: str,
    threshold: float = cq.DEFAULT_ACCEPTANCE_THRESHOLD,
    capacity_refusal: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    eval_max_age: datetime.timedelta = cq.DEFAULT_EVALUATION_MAX_AGE,
) -> RoutingReceipt:
    """Route one task, or explain why automated routing must refuse.

    *seats* must already be roster-validated
    (:func:`routing_policy.validate_against_roster`); this module treats
    them as trusted seat authority, never re-derives it. *host* is the
    operating host this routing pass runs for and is always required — a
    task carrying no ``host`` field is scoped against *host*, never left
    unscoped, and a task whose own ``host`` field disagrees with it is a
    hard mismatch, not a silent override.

    Order of checks (each is a hard stop, never a soft preference):

    1. Terminal status (``done``/``canceled``) — closed work never routes.
    2. Ambiguous worker labels, or a live signed claim
       (``status in {"in_progress", "in_review"}``) — preserved untouched.
       A plain backlog assignment (``status == "backlog"`` with a
       ``worker:<name>`` label) is not yet a live claim and proceeds.
    3. Caller-supplied ``capacity_refusal`` (e.g. active-implementation cap
       reached, from :func:`provider_qualification.dispatch_blocked_by_capacity`)
       — a budget refusal blocks new automated selection outright.
    4. A blocking gate (``human``/``deferred``/``tracking``), or an
       unexpired ``timer`` gate — gated/embargoed work is not eligible.
    5. Work-kind / risk label parsing — missing, ambiguous, or a label this
       module has never named refuses rather than guessing.
    6. Required project (refuses if the task carries none — never scopes
       against an unscoped/empty project) / host / tool scope.
    7. ``implement``: confirm the task's own assigned seat still qualifies;
       never substitute a different one. ``design``/``review``: a real
       preference-ordered choice via
       :func:`capability_qualification.recommend_candidate`.
    """
    now = now or _utcnow()
    task_id = _task_id(task)
    labels = _task_labels(task)

    status = str(task.get("status") or "").strip()
    if status not in ("backlog", "in_progress", "in_review") or status in _TERMINAL_STATUSES:
        return _refuse(
            task_id, now,
            "%s has terminal status=%r; not eligible for routing" % (task_id, status),
        )

    worker_labels = sorted(set(_labeled_values(labels, WORKER_LABEL_PREFIX)))
    if len(worker_labels) > 1:
        return _refuse(
            task_id, now,
            "%s carries ambiguous worker labels: %s"
            % (task_id, ", ".join(worker_labels)),
        )
    assigned_worker = worker_labels[0] if worker_labels else None

    if status in _LIVE_CLAIM_STATUSES:
        if assigned_worker is None:
            return _refuse(
                task_id, now,
                "%s has status=%r but no worker:<name> label to preserve"
                % (task_id, status),
            )
        return RoutingReceipt(
            task_id=task_id,
            generated_at=_utc_iso_z(now),
            decision="existing_claim_preserved",
            reason="%s has status=%r under worker:%s — routing never reassigns "
                   "a live signed claim" % (task_id, status, assigned_worker),
            existing_worker=assigned_worker,
        )

    if capacity_refusal:
        return _refuse(task_id, now, capacity_refusal)

    gate_type = str(task.get("gate_type") or "").strip()
    if gate_type not in ("", "timer") or gate_type in _BLOCKED_GATE_TYPES:
        return _refuse(
            task_id, now,
            "%s carries gate_type=%r; not eligible for automated routing"
            % (task_id, gate_type),
        )
    if gate_type == "timer":
        gate_until = _parse_iso_z(str(task.get("gate_until") or ""))
        if gate_until is None:
            return _refuse(
                task_id, now,
                "%s carries an unparseable/absent timer gate_until; refusing "
                "rather than guessing whether it has thawed" % task_id,
            )
        if gate_until > now:
            return _refuse(
                task_id, now,
                "%s is timer-gated until %s; not yet eligible" % (task_id, task.get("gate_until")),
            )

    work_kind_labels = sorted(set(_labeled_values(labels, WORK_KIND_LABEL_PREFIX)))
    if len(work_kind_labels) > 1:
        return _refuse(
            task_id, now,
            "%s carries ambiguous work-kind labels: %s"
            % (task_id, ", ".join(work_kind_labels)),
        )
    if not work_kind_labels:
        return _refuse(task_id, now, "%s carries no work-kind:<kind> label" % task_id)
    work_kind = work_kind_labels[0]

    if work_kind in FIXED_WORK_KINDS:
        return RoutingReceipt(
            task_id=task_id,
            generated_at=_utc_iso_z(now),
            decision="fixed_work_kind",
            reason="work-kind=%r is fixed by design, not a routed choice"
                   % work_kind,
            work_kind=work_kind,
        )
    category = WORK_KIND_CATEGORY.get(work_kind)
    if category is None:
        return _refuse(
            task_id, now,
            "work-kind=%r has no defined routing category (schema %s)"
            % (work_kind, SCHEMA_ID),
            work_kind=work_kind,
        )

    risk_labels = sorted(set(_labeled_values(labels, RISK_LABEL_PREFIX)))
    if len(risk_labels) > 1:
        return _refuse(
            task_id, now,
            "%s carries ambiguous risk labels: %s"
            % (task_id, ", ".join(risk_labels)),
            work_kind=work_kind, category=category,
        )
    if not risk_labels:
        return _refuse(task_id, now, "explicit risk label is required", work_kind=work_kind, category=category)
    risk = risk_labels[0]
    if risk not in cq.RISK_LEVELS:
        return _refuse(
            task_id, now,
            "%s risk label %r is not a recognized risk level %s"
            % (task_id, risk, cq.RISK_LEVELS),
            work_kind=work_kind, category=category,
        )

    project = str(task.get("project") or task.get("product") or "").strip()
    if not project:
        return _refuse(
            task_id, now,
            "%s carries no project/product; routing never scopes candidates "
            "against an unstated project" % task_id,
            work_kind=work_kind, category=category, risk=risk,
        )
    task_host = str(task.get("host") or "").strip()
    if not host:
        return _refuse(
            task_id, now,
            "no operating host was supplied for this routing pass; routing "
            "never scopes candidates against an unstated host",
            work_kind=work_kind, category=category, risk=risk, project=project,
        )
    if task_host and task_host != host:
        return _refuse(
            task_id, now,
            "%s declares host=%r but this routing pass runs for host=%r"
            % (task_id, task_host, host),
            work_kind=work_kind, category=category, risk=risk, project=project,
        )

    if "required_tools" in task and (not isinstance(task["required_tools"], list) or any(not isinstance(x, str) or not x for x in task["required_tools"])):
        return _refuse(task_id, now, "required_tools must be a list of non-empty strings")
    scoped, excluded = _scope_seats(task, seats, project, host)

    if work_kind in _EXISTING_SEAT_WORK_KINDS:
        if assigned_worker is None:
            return _refuse(
                task_id, now,
                "work-kind=%r keeps the project's existing registered seat "
                "unchanged; %s has no worker:<name> assignment to confirm "
                "and routing never invents one" % (work_kind, task_id),
                work_kind=work_kind, category=category, risk=risk, project=project,
                excluded=tuple(excluded),
            )
        seat_scoped = [s for s in scoped if s.worker == assigned_worker]
        if not seat_scoped:
            return _refuse(
                task_id, now,
                "no in-scope registered capability evidence for the assigned "
                "worker:%s on %s; routing does not reassign implement work "
                "to a different seat" % (assigned_worker, task_id),
                work_kind=work_kind, category=category, risk=risk, project=project,
                existing_worker=assigned_worker, excluded=tuple(excluded),
            )
        try:
            rec = cq.recommend_candidate(
                [s.candidate for s in seat_scoped], results_by_candidate,
                category=category, risk=risk, threshold=threshold,
                now=now, eval_max_age=eval_max_age,
            )
        except ValueError as exc:
            return _refuse(
                task_id, now, str(exc),
                work_kind=work_kind, category=category, risk=risk, project=project,
                existing_worker=assigned_worker,
            )
        refusals = tuple(cq.qualification_refusals([s.candidate for s in seat_scoped], now=now))
        if rec is None:
            return RoutingReceipt(
                task_id=task_id,
                generated_at=_utc_iso_z(now),
                decision="refused",
                reason="assigned worker:%s is not currently qualified for %s "
                       "at risk=%s; retained, not reassigned — resolve the "
                       "seat's evidence before automated dispatch"
                       % (assigned_worker, task_id, risk),
                work_kind=work_kind, category=category, risk=risk, project=project,
                existing_worker=assigned_worker, refusals=refusals, excluded=tuple(excluded),
            )
        return RoutingReceipt(
            task_id=task_id,
            generated_at=_utc_iso_z(now),
            decision="confirmed_existing_seat",
            reason="assigned worker:%s remains the project's existing "
                   "registered seat and is currently qualified: %s"
                   % (assigned_worker, rec.reason),
            work_kind=work_kind, category=category, risk=risk, project=project,
            existing_worker=assigned_worker, recommendation=rec.to_dict(),
            refusals=refusals, excluded=tuple(excluded),
        )

    if not scoped:
        return _refuse(
            task_id, now,
            "no registered candidate is in scope for %s (project=%r, host=%r)"
            % (task_id, project, host),
            work_kind=work_kind, category=category, risk=risk,
            project=project, excluded=tuple(excluded),
        )

    try:
        rec = cq.recommend_candidate(
            [s.candidate for s in scoped], results_by_candidate,
            category=category, risk=risk, threshold=threshold,
            now=now, eval_max_age=eval_max_age,
        )
    except ValueError as exc:
        return _refuse(
            task_id, now, str(exc),
            work_kind=work_kind, category=category, risk=risk, project=project,
        )

    refusals = tuple(cq.qualification_refusals([s.candidate for s in scoped], now=now))
    if rec is None:
        return RoutingReceipt(
            task_id=task_id,
            generated_at=_utc_iso_z(now),
            decision="refused",
            reason="no qualified candidate meets %s>=%.2f acceptance for "
                   "%s at risk=%s" % (category, threshold, task_id, risk),
            work_kind=work_kind, category=category, risk=risk, project=project,
            refusals=refusals, excluded=tuple(excluded),
        )

    return RoutingReceipt(
        task_id=task_id,
        generated_at=_utc_iso_z(now),
        decision="recommended",
        reason=rec.reason,
        work_kind=work_kind, category=category, risk=risk, project=project,
        recommendation=rec.to_dict(),
        refusals=refusals, excluded=tuple(excluded),
    )


def route_ready_queue(
    tasks: Sequence[Dict[str, Any]],
    seats: Sequence[SeatCapability],
    results_by_candidate: Dict[str, List[cq.EvaluationResult]],
    *,
    host: str,
    threshold: float = cq.DEFAULT_ACCEPTANCE_THRESHOLD,
    capacity_refusal: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    eval_max_age: datetime.timedelta = cq.DEFAULT_EVALUATION_MAX_AGE,
) -> List[RoutingReceipt]:
    """Route every task the shift can currently select (stable input order).

    Empty *tasks* returns an empty list — an empty or fully gated ready
    queue stops cleanly with no receipts, never a manufactured one.
    """
    now = now or _utcnow()
    return [
        route_task(
            task, seats, results_by_candidate,
            host=host, threshold=threshold, capacity_refusal=capacity_refusal,
            now=now, eval_max_age=eval_max_age,
        )
        for task in tasks
    ]


def first_accepted_task(
    tasks: Sequence[Dict[str, Any]],
    seats: Sequence[SeatCapability],
    results_by_candidate: Dict[str, List[cq.EvaluationResult]],
    *,
    host: str,
    threshold: float = cq.DEFAULT_ACCEPTANCE_THRESHOLD,
    capacity_refusal: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    eval_max_age: datetime.timedelta = cq.DEFAULT_EVALUATION_MAX_AGE,
) -> Tuple[Optional[Dict[str, Any]], List[RoutingReceipt]]:
    """First task (preserving *tasks* order) whose receipt is accepted.

    Returns ``(task, receipts)`` — *receipts* covers every task inspected
    up to and including the accepted one (or all of them, if none is
    accepted), so a caller can log every refusal reason, not just the
    winner. Never picks a task out of order: an earlier refusal never
    causes a later, "easier" task to jump the queue silently — the
    receipts trail makes every skip explicit.
    """
    now = now or _utcnow()
    receipts: List[RoutingReceipt] = []
    for task in tasks:
        receipt = route_task(
            task, seats, results_by_candidate,
            host=host, threshold=threshold, capacity_refusal=capacity_refusal,
            now=now, eval_max_age=eval_max_age,
        )
        receipts.append(receipt)
        if receipt.accepted:
            return task, receipts
    return None, receipts


def format_receipt(receipt: RoutingReceipt) -> str:
    """One-line human-readable summary (evidence comments, CLI output)."""
    head = "%s: %s" % (receipt.task_id or "?", receipt.decision)
    if receipt.decision in ("recommended", "confirmed_existing_seat") and receipt.recommendation:
        head += " -> %s" % receipt.recommendation.get("candidate_id", "?")
    return "%s — %s" % (head, receipt.reason)

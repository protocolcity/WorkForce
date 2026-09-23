"""Versioned host policy mapping registered roster seats to capability evidence (wf-279).

A :mod:`workforce.task_routing` decision is only as trustworthy as the
candidate evidence it qualifies against. This module is the one place a
host declares that mapping explicitly, as a dated JSON document an operator
authors and refreshes -- never inferred from installed binaries, never
accepted as bare caller input at the supervisor/task_runner seam. Every row
names a real roster seat by name; :func:`validate_against_roster` then
proves that seat is currently a claiming lane on the *loaded* roster and
that the roster itself, not this document, actually scopes it to the
project the row claims -- a policy document cannot grant a seat authority
the roster never gave it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from . import capability_qualification as cq

SCHEMA_ID = "workforce.routing_policy/v1"
SUPPORTED_SCHEMA_VERSIONS = (1,)


class RoutingPolicyError(ValueError):
    """The policy document itself is missing, malformed, or unversioned."""


@dataclass(frozen=True)
class SeatCapability:
    """One registered roster seat's own dated candidate evidence row."""

    worker: str
    candidate: cq.CandidateRecord

    def to_dict(self) -> Dict[str, Any]:
        d = self.candidate.to_dict()
        d["worker"] = self.worker
        return d


@dataclass(frozen=True)
class RoutingPolicy:
    schema: str
    seats: Tuple[SeatCapability, ...]
    results_by_candidate: Dict[str, List[cq.EvaluationResult]]


def _parse_evaluation_results(raw: object) -> Dict[str, List[cq.EvaluationResult]]:
    results_by_candidate: Dict[str, List[cq.EvaluationResult]] = {}
    if not isinstance(raw, list):
        return results_by_candidate
    for item in raw:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id", "")).strip()
        task_id = str(item.get("task_id", "")).strip()
        accepted = item.get("accepted")
        regressions = item.get("regressions", 0)
        retries = item.get("retries", 0)
        if (not candidate_id or not task_id or type(accepted) is not bool
                or type(regressions) is not int or type(retries) is not int
                or regressions < 0 or retries < 0):
            continue
        observed_at = str(item.get("observed_at", "")).strip()
        if not observed_at or cq._parse_iso_z(observed_at) is None:
            continue
        result = cq.EvaluationResult(
            task_id=task_id, candidate_id=candidate_id, accepted=accepted,
            regressions=regressions, retries=retries,
            observed_at=observed_at,
        )
        results_by_candidate.setdefault(candidate_id, []).append(result)
    return results_by_candidate


def parse_routing_policy(raw: object) -> RoutingPolicy:
    """Parse a host-authored routing policy document.

    A malformed or incomplete seat row is dropped, not guessed into
    validity -- same discipline as
    :func:`capability_qualification.parse_candidate_records`, which this
    reuses for every field except the seat ``worker`` name itself. An
    unversioned or wrong-shaped document raises rather than silently
    yielding an empty policy, so a broken deploy is never mistaken for "no
    routing policy configured".
    """
    if not isinstance(raw, dict):
        raise RoutingPolicyError("routing policy must be a JSON object")
    version = raw.get("version")
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise RoutingPolicyError("routing policy version %r is unsupported" % (version,))
    seats_raw = raw.get("seats")
    if not isinstance(seats_raw, list):
        raise RoutingPolicyError("routing policy must carry a 'seats' list")
    seats: List[SeatCapability] = []
    for row in seats_raw:
        if not isinstance(row, dict):
            continue
        worker = str(row.get("worker", "")).strip()
        if not worker:
            continue
        candidates = cq.parse_candidate_records([row])
        if not candidates:
            continue
        seats.append(SeatCapability(worker=worker, candidate=candidates[0]))
    return RoutingPolicy(
        schema=str(raw.get("schema") or SCHEMA_ID),
        seats=tuple(seats),
        results_by_candidate=_parse_evaluation_results(raw.get("evaluation_results")),
    )


def load_routing_policy(path: str) -> RoutingPolicy:
    """Read and parse a routing policy document from an absolute path."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return parse_routing_policy(raw)


def validate_against_roster(policy: RoutingPolicy, roster) -> Tuple[Tuple[SeatCapability, ...], List[Dict[str, str]]]:
    """Keep only seat rows the *loaded* roster itself actually backs.

    A row naming a worker absent from the roster, not a claiming
    ``kind == "lane"``, or claiming a project the roster's own
    ``queue_url`` does not scope it to, is never trusted candidate
    evidence for that worker -- the roster is the source of truth for
    seat authority, this document only supplies capability evidence for
    seats the roster already grants.
    """
    from . import engine  # local import: keep this module host-neutral at import time

    valid: List[SeatCapability] = []
    refusals: List[Dict[str, str]] = []
    for seat in policy.seats:
        worker = roster.workers.get(seat.worker)
        if worker is None:
            refusals.append({
                "worker": seat.worker,
                "reason": "%s is not a registered lane on the loaded roster" % seat.worker,
            })
            continue
        if worker.kind != "lane":
            refusals.append({
                "worker": seat.worker,
                "reason": "%s is kind=%r, not a claiming lane" % (seat.worker, worker.kind),
            })
            continue
        roster_project = engine.product_from_queue_url(worker.queue_url or "")
        if roster_project != seat.candidate.project:
            refusals.append({
                "worker": seat.worker,
                "reason": "%s is registered on the roster for project %r, not policy-claimed %r"
                          % (seat.worker, roster_project, seat.candidate.project),
            })
            continue
        valid.append(seat)
    return tuple(valid), refusals

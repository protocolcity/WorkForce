"""Task routing — bind a real ready-queue task to registered seat capability (wf-279)."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import capability_qualification as cq  # noqa: E402
from workforce import task_routing as tr  # noqa: E402
from workforce.routing_policy import SeatCapability  # noqa: E402

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
HOST = "bp"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _quota(**over):
    spec = dict(
        pool="subscription", pool_id="acct-1", units="requests",
        observed_at=_iso(NOW - timedelta(minutes=5)), remaining=5.0,
    )
    spec.update(over)
    return cq.QuotaObservation(**spec)


def _candidate(**over):
    spec = dict(
        provider="claude",
        model="sonnet-5",
        reasoning_effort="medium",
        host=HOST,
        tools=("wl_show", "wl_claim"),
        project="widgets",
        account_state="authenticated",
        observed_at=_iso(NOW - timedelta(hours=1)),
        expires_at=_iso(NOW + timedelta(hours=1)),
        supported_efforts=("low", "medium", "high"),
        quota=_quota(),
    )
    spec.update(over)
    return cq.CandidateRecord(**spec)


def _seat(worker="tester", **over):
    return SeatCapability(worker=worker, candidate=_candidate(**over))


def _synthetic_runner(accepted=True, regressions=0, retries=0, observed_at=None):
    def runner(task, candidate):
        return cq.EvaluationResult(
            task_id=task.id,
            candidate_id=candidate.candidate_id(),
            accepted=accepted,
            regressions=regressions,
            retries=retries,
            observed_at=observed_at or _iso(NOW),
        )
    return runner


def _results_for(seat, **runner_kw):
    cand = seat.candidate
    return {cand.candidate_id(): cq.run_evaluation_suite(cand, _synthetic_runner(**runner_kw))}


def _task(**over):
    spec = dict(
        id="wt-1",
        project="widgets",
        host=HOST,
        status="backlog",
        labels=["work-kind:implement", "risk:low", "worker:tester"],
    )
    spec.update(over)
    return spec


def _route(task, seats, results, **over):
    over.setdefault("host", HOST)
    over.setdefault("now", NOW)
    return tr.route_task(task, seats, results, **over)


# --- terminal status -----------------------------------------------------------

def test_done_task_refuses_routing():
    receipt = _route(_task(status="done"), [], {})
    assert receipt.decision == "refused"
    assert "terminal status" in receipt.reason


def test_canceled_task_refuses_routing():
    receipt = _route(_task(status="canceled"), [], {})
    assert receipt.decision == "refused"


# --- live claim vs. plain backlog assignment -----------------------------------

def test_live_claim_status_is_preserved_not_reassigned():
    task = _task(status="in_progress")
    receipt = _route(task, [], {})
    assert receipt.decision == "existing_claim_preserved"
    assert receipt.existing_worker == "tester"
    assert receipt.recommendation is None


def test_in_review_status_is_also_a_live_claim():
    task = _task(status="in_review")
    receipt = _route(task, [], {})
    assert receipt.decision == "existing_claim_preserved"


def test_live_claim_status_without_worker_label_refuses():
    task = _task(status="in_progress", labels=["work-kind:implement", "risk:low"])
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "no worker" in receipt.reason


def test_backlog_assignment_with_worker_label_is_qualified_not_preserved():
    """A worker:<name> label on a *backlog* task is assignment, not a live
    claim — this must still be gated, unlike wf-279's first pass."""
    seat = _seat()
    task = _task(status="backlog")
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "confirmed_existing_seat"


def test_ambiguous_worker_labels_refuse_regardless_of_status():
    task = _task(labels=["worker:a", "worker:b"], status="in_progress")
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "ambiguous worker labels" in receipt.reason


# --- capacity / budget --------------------------------------------------------

def test_capacity_refusal_blocks_new_selection():
    seat = _seat()
    task = _task()
    receipt = _route(
        task, [seat], _results_for(seat),
        capacity_refusal="active implementation cap 1 reached — live locks: wt-x",
    )
    assert receipt.decision == "refused"
    assert "cap 1 reached" in receipt.reason


# --- gated / timer / empty queues ----------------------------------------------

def test_human_gated_task_refuses_automated_routing():
    task = _task(gate_type="human")
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "gate_type='human'" in receipt.reason


def test_deferred_gated_task_refuses_automated_routing():
    task = _task(gate_type="deferred")
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"


def test_unexpired_timer_gate_refuses():
    task = _task(gate_type="timer", gate_until=_iso(NOW + timedelta(hours=1)))
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "timer-gated" in receipt.reason


def test_timer_gate_missing_gate_until_refuses():
    task = _task(gate_type="timer")
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "unparseable/absent timer gate_until" in receipt.reason


def test_thawed_timer_gate_proceeds_to_routing():
    seat = _seat()
    task = _task(gate_type="timer", gate_until=_iso(NOW - timedelta(hours=1)))
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "confirmed_existing_seat"


def test_route_ready_queue_empty_stops_cleanly():
    assert tr.route_ready_queue([], [], {}, host=HOST, now=NOW) == []


def test_route_ready_queue_routes_each_task_in_order():
    seat = _seat()
    tasks = [_task(id="wt-1"), _task(id="wt-2", labels=["work-kind:review", "risk:low"])]
    results = _results_for(seat)
    receipts = tr.route_ready_queue(tasks, [seat], results, host=HOST, now=NOW)
    assert [r.task_id for r in receipts] == ["wt-1", "wt-2"]


# --- work-kind / risk label vocabulary ---------------------------------------

def test_missing_work_kind_label_refuses():
    task = _task(labels=["worker:tester"])
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "no work-kind" in receipt.reason


def test_ambiguous_work_kind_labels_refuse():
    task = _task(labels=["work-kind:implement", "work-kind:review", "worker:tester"])
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "ambiguous work-kind" in receipt.reason


def test_unknown_work_kind_refuses_without_guessing_a_category():
    task = _task(labels=["work-kind:refactor", "worker:tester"])
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "no defined routing category" in receipt.reason
    assert receipt.work_kind == "refactor"


def test_fixed_work_kind_is_not_routed():
    task = _task(labels=["work-kind:supervise"])
    receipt = _route(task, [], {})
    assert receipt.decision == "fixed_work_kind"
    assert receipt.recommendation is None


def test_invalid_risk_label_refuses():
    task = _task(labels=["work-kind:implement", "risk:extreme", "worker:tester"])
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "not a recognized risk level" in receipt.reason


def test_ambiguous_risk_labels_refuse():
    task = _task(labels=["work-kind:implement", "risk:low", "risk:high", "worker:tester"])
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "ambiguous risk" in receipt.reason


def test_missing_risk_label_refuses_without_assuming_low_risk():
    seat = _seat()
    task = _task(labels=["work-kind:implement", "worker:tester"])
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "refused"
    assert "explicit risk" in receipt.reason


# --- required project / host ---------------------------------------------------

def test_missing_project_on_task_refuses_rather_than_skipping_scope():
    task = _task(project="")
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "no project/product" in receipt.reason


def test_missing_operating_host_refuses_rather_than_skipping_scope():
    task = _task()
    receipt = _route(task, [], {}, host="")
    assert receipt.decision == "refused"
    assert "no operating host" in receipt.reason


def test_task_declared_host_mismatch_with_operating_host_refuses():
    task = _task(host="other-host")
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "runs for host=" in receipt.reason


# --- scope: project / host / tools -------------------------------------------

def test_scope_mismatch_by_project_refuses():
    seat = _seat(project="other-project")
    task = _task(project="widgets")
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "refused"
    assert len(receipt.excluded) == 1
    assert "registered for project" in receipt.excluded[0]["reason"]


def test_scope_mismatch_by_host_refuses():
    seat = _seat(host="other-host")
    task = _task(host=HOST)
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "refused"
    assert any("registered for host" in e["reason"] for e in receipt.excluded)


def test_unsupported_tool_refuses():
    seat = _seat(tools=("wl_show",))
    task = _task(required_tools=["wl_show", "wl_claim"])
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "refused"
    assert any("missing required tools" in e["reason"] for e in receipt.excluded)


def test_all_required_tools_present_confirms_existing_seat():
    seat = _seat(tools=("wl_show", "wl_claim", "wl_comment"))
    task = _task(required_tools=["wl_show", "wl_claim"])
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "confirmed_existing_seat"


# --- implement: existing seat is confirmed, never reassigned ------------------

def test_implement_with_no_worker_assignment_refuses():
    task = _task(labels=["work-kind:implement", "risk:low"])
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"
    assert "no worker:<name> assignment" in receipt.reason


def test_implement_assigned_worker_with_no_registered_evidence_refuses():
    other_seat = _seat(worker="someone-else")
    task = _task()
    receipt = _route(task, [other_seat], _results_for(other_seat))
    assert receipt.decision == "refused"
    assert "does not reassign implement work" in receipt.reason
    assert receipt.existing_worker == "tester"


def test_implement_assigned_worker_unqualified_refuses_without_reassigning():
    seat = _seat(account_state="unauthenticated")
    task = _task()
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "refused"
    assert "retained, not reassigned" in receipt.reason
    assert receipt.existing_worker == "tester"


def test_implement_assigned_worker_qualified_confirms_existing_seat():
    seat = _seat()
    task = _task()
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "confirmed_existing_seat"
    assert receipt.category == "bounded_edit"
    assert receipt.recommendation["candidate_id"] == seat.candidate.candidate_id()


def test_implement_ignores_a_different_seats_evidence_even_if_qualified():
    """Another registered seat qualifying for bounded_edit must never cause
    reassignment away from the task's own assigned worker."""
    assigned_unqualified = _seat(worker="tester", account_state="unauthenticated")
    other_qualified = _seat(worker="someone-else")
    task = _task()
    results = {}
    results.update(_results_for(assigned_unqualified))
    results.update(_results_for(other_qualified))
    receipt = _route(task, [assigned_unqualified, other_qualified], results)
    assert receipt.decision == "refused"
    assert receipt.existing_worker == "tester"


def test_high_risk_implement_task_requires_stronger_reasoning():
    weak = _seat(reasoning_effort="low")
    task = _task(labels=["work-kind:implement", "risk:high", "worker:tester"])
    receipt = _route(task, [weak], _results_for(weak))
    assert receipt.decision == "refused"
    assert receipt.risk == "high"


# --- design / review: a real preference-ordered routing choice ----------------

def test_review_work_kind_is_routed_among_all_in_scope_seats():
    seat = _seat()
    task = _task(labels=["work-kind:review", "risk:low"])
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "recommended"
    assert receipt.category == "review"


def test_design_work_kind_with_no_seats_refuses_not_raises():
    task = _task(labels=["work-kind:design", "risk:low"])
    receipt = _route(task, [], {})
    assert receipt.decision == "refused"


def test_review_scoped_pool_is_not_limited_to_the_assigned_worker():
    """Unlike implement, a review task with a stale worker: label can still
    route to any in-scope qualified seat -- review is a real routing choice."""
    seat = _seat(worker="reviewer-seat")
    task = _task(labels=["work-kind:review", "risk:low", "worker:someone-else"])
    receipt = _route(task, [seat], _results_for(seat))
    assert receipt.decision == "recommended"
    assert receipt.recommendation["candidate_id"] == seat.candidate.candidate_id()


# --- capability qualification pass-through (stale / unknown quota / held) ----

def test_stale_evidence_refuses_with_durable_reason():
    seat = _seat(expires_at=_iso(NOW - timedelta(hours=1)))
    receipt = _route(_task(), [seat], _results_for(seat))
    assert receipt.decision == "refused"
    assert receipt.refusals
    assert "not fresh" in receipt.refusals[0]["reason"]


def test_unknown_quota_refuses():
    seat = _seat(quota=_quota(remaining=None))
    receipt = _route(_task(), [seat], _results_for(seat))
    assert receipt.decision == "refused"
    assert any("unknown" in r["reason"] for r in receipt.refusals)


def test_held_unauthenticated_account_refuses():
    seat = _seat(account_state="unauthenticated")
    receipt = _route(_task(), [seat], _results_for(seat))
    assert receipt.decision == "refused"
    assert any("not authenticated" in r["reason"] for r in receipt.refusals)


# --- first_accepted_task -------------------------------------------------------

def test_first_accepted_task_skips_refused_tasks_in_order():
    seat = _seat()
    refused_task = _task(id="wt-1", gate_type="human")
    accepted_task = _task(id="wt-2")
    task, receipts = tr.first_accepted_task(
        [refused_task, accepted_task], [seat], _results_for(seat), host=HOST, now=NOW,
    )
    assert task["id"] == "wt-2"
    assert [r.task_id for r in receipts] == ["wt-1", "wt-2"]
    assert receipts[0].decision == "refused"
    assert receipts[1].accepted


def test_first_accepted_task_returns_none_when_all_refuse():
    task, receipts = tr.first_accepted_task(
        [_task(id="wt-1", gate_type="human")], [], {}, host=HOST, now=NOW,
    )
    assert task is None
    assert len(receipts) == 1


# --- receipt formatting ---------------------------------------------------------

def test_format_receipt_includes_task_and_decision():
    seat = _seat()
    receipt = _route(_task(), [seat], _results_for(seat))
    text = tr.format_receipt(receipt)
    assert "wt-1: confirmed_existing_seat" in text
    assert seat.candidate.candidate_id() in text

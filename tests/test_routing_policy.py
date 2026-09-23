"""Routing policy — host-authored seat capability document, roster-validated (wf-279)."""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import routing_policy as rp  # noqa: E402
from workforce.roster import Roster, Worker  # noqa: E402

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seat_row(**over):
    spec = dict(
        worker="tester",
        provider="claude",
        model="sonnet-5",
        reasoning_effort="medium",
        host="bp",
        tools=["wl_show", "wl_claim"],
        project="widgets",
        account_state="authenticated",
        observed_at=_iso(NOW - timedelta(hours=1)),
        expires_at=_iso(NOW + timedelta(hours=1)),
        supported_efforts=["low", "medium", "high"],
        version=1,
    )
    spec.update(over)
    return spec


def _policy_doc(seats, evaluation_results=None):
    return {"version": 1, "seats": seats, "evaluation_results": evaluation_results or []}


def make_worker(**over):
    spec = dict(
        name="tester", workdir="/tmp/hood", contract="/tmp/CONTRACT.md",
        prompt="/tmp/prompt.md", identity="tester-id",
        command=["/bin/sh", "-c", "exit 0"], kind="lane",
        queue_url="http://desk.test/api/admin/tasks/ready?product=widgets&label=worker:tester",
    )
    spec.update(over)
    return Worker(**spec)


def make_roster(*workers):
    return Roster(workers={w.name: w for w in workers}, path="/tmp/roster.json")


# --- parsing ---------------------------------------------------------------

def test_parse_routing_policy_requires_supported_version():
    with pytest.raises(rp.RoutingPolicyError):
        rp.parse_routing_policy({"version": 99, "seats": []})


def test_parse_routing_policy_requires_object():
    with pytest.raises(rp.RoutingPolicyError):
        rp.parse_routing_policy([])


def test_parse_routing_policy_requires_seats_list():
    with pytest.raises(rp.RoutingPolicyError):
        rp.parse_routing_policy({"version": 1})


def test_parse_routing_policy_drops_row_without_worker_name():
    doc = _policy_doc([{k: v for k, v in _seat_row().items() if k != "worker"}])
    policy = rp.parse_routing_policy(doc)
    assert policy.seats == ()


def test_parse_routing_policy_drops_malformed_candidate_row():
    row = _seat_row()
    del row["tools"]
    policy = rp.parse_routing_policy(_policy_doc([row]))
    assert policy.seats == ()


def test_parse_routing_policy_parses_valid_seat():
    policy = rp.parse_routing_policy(_policy_doc([_seat_row()]))
    assert len(policy.seats) == 1
    assert policy.seats[0].worker == "tester"
    assert policy.seats[0].candidate.project == "widgets"


def test_parse_routing_policy_parses_evaluation_results():
    seat_row = _seat_row()
    candidate_id = "claude/sonnet-5@medium#bp::widgets"
    results = [{
        "candidate_id": candidate_id, "task_id": "edit-01", "accepted": True,
        "regressions": 0, "retries": 0, "observed_at": _iso(NOW),
    }]
    policy = rp.parse_routing_policy(_policy_doc([seat_row], results))
    assert candidate_id in policy.results_by_candidate
    assert policy.results_by_candidate[candidate_id][0].task_id == "edit-01"


def test_parse_routing_policy_drops_malformed_evaluation_result():
    results = [{"candidate_id": "x", "task_id": "edit-01", "accepted": "yes"}]
    policy = rp.parse_routing_policy(_policy_doc([_seat_row()], results))
    assert policy.results_by_candidate == {}


def test_load_routing_policy_reads_file(tmp_path):
    import json
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_policy_doc([_seat_row()])))
    policy = rp.load_routing_policy(str(path))
    assert len(policy.seats) == 1


# --- roster validation -------------------------------------------------------

def test_validate_against_roster_keeps_matching_seat():
    policy = rp.parse_routing_policy(_policy_doc([_seat_row()]))
    roster = make_roster(make_worker())
    valid, refusals = rp.validate_against_roster(policy, roster)
    assert len(valid) == 1
    assert refusals == []


def test_validate_against_roster_refuses_unknown_worker():
    policy = rp.parse_routing_policy(_policy_doc([_seat_row(worker="ghost")]))
    roster = make_roster(make_worker())
    valid, refusals = rp.validate_against_roster(policy, roster)
    assert valid == ()
    assert "not a registered lane" in refusals[0]["reason"]


def test_validate_against_roster_refuses_non_lane_worker():
    policy = rp.parse_routing_policy(_policy_doc([_seat_row()]))
    roster = make_roster(make_worker(kind="job", queue_url=""))
    valid, refusals = rp.validate_against_roster(policy, roster)
    assert valid == ()
    assert "not a claiming lane" in refusals[0]["reason"]


def test_validate_against_roster_refuses_project_the_roster_never_scoped():
    policy = rp.parse_routing_policy(_policy_doc([_seat_row(project="other-project")]))
    roster = make_roster(make_worker())  # roster worker is scoped to "widgets"
    valid, refusals = rp.validate_against_roster(policy, roster)
    assert valid == ()
    assert "not policy-claimed" in refusals[0]["reason"]


def test_validate_against_roster_keeps_only_matching_rows_from_a_mixed_policy():
    policy = rp.parse_routing_policy(_policy_doc([_seat_row(), _seat_row(worker="ghost")]))
    roster = make_roster(make_worker())
    valid, refusals = rp.validate_against_roster(policy, roster)
    assert [s.worker for s in valid] == ["tester"]
    assert len(refusals) == 1

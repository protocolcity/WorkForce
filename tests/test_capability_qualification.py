"""Capability qualification — measured task fit over provider stereotypes (wf-282)."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import capability_qualification as cq  # noqa: E402

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


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
        host="bp",
        tools=("wl_show", "wl_claim"),
        project="workforce",
        account_state="authenticated",
        observed_at=_iso(NOW - timedelta(hours=1)),
        expires_at=_iso(NOW + timedelta(hours=1)),
        supported_efforts=("low", "medium", "high"),
        quota=_quota(),
    )
    spec.update(over)
    return cq.CandidateRecord(**spec)


def _synthetic_runner(accepted=True, regressions=0, retries=0, observed_at=None):
    def runner(task, candidate):
        return cq.EvaluationResult(
            task_id=task.id,
            candidate_id=candidate.candidate_id(),
            accepted=accepted,
            regressions=regressions,
            retries=retries,
            elapsed_secs=12.5,
            usage={"tokens": 100},
            observed_at=observed_at or _iso(NOW),
        )
    return runner


# --- CandidateRecord / parsing -------------------------------------------------

def test_candidate_id_encodes_provider_model_effort_host_and_project():
    cand = _candidate()
    assert cand.candidate_id() == "claude/sonnet-5@medium#bp::workforce"


def test_candidate_id_distinguishes_projects_so_scores_cannot_collide():
    a = _candidate(project="workforce")
    b = _candidate(project="worklane")
    assert a.candidate_id() != b.candidate_id()


def _raw_candidate(**over):
    spec = dict(
        provider="grok", model="grok-4.6", reasoning_effort="low",
        host="bp", project="workforce", account_state="authenticated",
        observed_at=_iso(NOW), expires_at=_iso(NOW + timedelta(hours=1)),
        version=1, tools=["wl_show"], supported_efforts=["low", "medium"],
    )
    spec.update(over)
    return spec


def test_parse_candidate_records_drops_rows_missing_required_fields():
    raw = [
        {"provider": "claude", "model": "sonnet-5"},  # missing everything else
        _raw_candidate(),
    ]
    recs = cq.parse_candidate_records(raw)
    assert len(recs) == 1
    assert recs[0].provider == "grok"


def test_parse_candidate_records_drops_unknown_account_state():
    raw = [_raw_candidate(account_state="definitely-logged-in")]
    assert cq.parse_candidate_records(raw) == []


def test_parse_candidate_records_drops_missing_host_project_or_tools():
    assert cq.parse_candidate_records([_raw_candidate(host="")]) == []
    assert cq.parse_candidate_records([_raw_candidate(project="")]) == []
    assert cq.parse_candidate_records([_raw_candidate(tools=[])]) == []


def test_parse_candidate_records_drops_unsupported_schema_version():
    assert cq.parse_candidate_records([_raw_candidate(version=99)]) == []
    assert cq.parse_candidate_records([_raw_candidate(version="not-a-number")]) == []


def test_parse_candidate_records_drops_effort_not_in_own_supported_efforts():
    raw = _raw_candidate(reasoning_effort="high", supported_efforts=["low", "medium"])
    assert cq.parse_candidate_records([raw]) == []


def test_parse_candidate_records_accepts_records_wrapper_and_quota():
    raw = {"records": [_raw_candidate(quota={
        "pool": "subscription", "pool_id": "acct-1", "units": "requests",
        "observed_at": _iso(NOW), "remaining": 5,
    })]}
    recs = cq.parse_candidate_records(raw)
    assert len(recs) == 1
    assert recs[0].quota.pool == "subscription"
    assert recs[0].quota.pool_id == "acct-1"
    assert recs[0].quota.units == "requests"
    assert recs[0].quota.state == "known"


def test_parse_candidate_records_drops_cost_missing_units():
    raw = _raw_candidate(cost_amount=0.5)  # cost_units omitted
    recs = cq.parse_candidate_records([raw])
    assert len(recs) == 1
    assert recs[0].cost_amount is None
    assert recs[0].cost_known() is False


def test_parse_candidate_records_accepts_explicit_cost():
    raw = _raw_candidate(cost_amount=0.5, cost_units="usd_per_1k_tokens")
    recs = cq.parse_candidate_records([raw])
    assert recs[0].cost_amount == 0.5
    assert recs[0].cost_units == "usd_per_1k_tokens"
    assert recs[0].cost_known() is True


# --- QuotaObservation parsing ---------------------------------------------------

def test_quota_observation_stays_unknown_without_remaining():
    obs = cq.QuotaObservation(
        pool="api_budget", pool_id="p1", units="tokens", observed_at=_iso(NOW),
    )
    assert obs.state == "unknown"
    assert obs.remaining is None
    assert obs.reset_at is None


def test_parse_quota_drops_missing_pool_id_or_units():
    base = {"pool": "shared", "observed_at": _iso(NOW), "remaining": 1}
    assert cq._parse_quota(dict(base, pool_id="", units="tokens")) is None
    assert cq._parse_quota(dict(base, pool_id="p1", units="")) is None


def test_parse_quota_never_defaults_missing_observed_at_to_now():
    raw = {"pool": "shared", "pool_id": "p1", "units": "tokens", "remaining": 1}
    assert cq._parse_quota(raw) is None


def test_parse_quota_rejects_bool_nan_and_negative_remaining():
    base = {"pool": "shared", "pool_id": "p1", "units": "tokens", "observed_at": _iso(NOW)}
    assert cq._parse_quota(dict(base, remaining=True)) is None
    assert cq._parse_quota(dict(base, remaining=float("nan"))) is None
    assert cq._parse_quota(dict(base, remaining=-1)) is None
    assert cq._parse_quota(dict(base, remaining=0)) is not None


# --- Staleness / refusal --------------------------------------------------------

def test_refusal_reason_none_for_fresh_authenticated_candidate_with_quota():
    cand = _candidate()
    assert cq.refusal_reason(cand, now=NOW) is None


def test_refusal_reason_for_unauthenticated_candidate():
    cand = _candidate(account_state="unauthenticated")
    reason = cq.refusal_reason(cand, now=NOW)
    assert reason is not None
    assert "not authenticated" in reason


def test_refusal_reason_for_expired_evidence():
    cand = _candidate(expires_at=_iso(NOW - timedelta(minutes=1)))
    reason = cq.refusal_reason(cand, now=NOW)
    assert reason is not None
    assert "not fresh" in reason


def test_refusal_reason_for_future_observed_at():
    cand = _candidate(observed_at=_iso(NOW + timedelta(minutes=1)))
    reason = cq.refusal_reason(cand, now=NOW)
    assert reason is not None
    assert "not fresh" in reason


def test_refusal_reason_for_missing_expires_at_never_guesses_freshness():
    cand = _candidate(expires_at="")
    assert cq.is_stale(cand, now=NOW) is True
    assert cq.refusal_reason(cand, now=NOW) is not None


def test_refusal_reason_for_missing_host_project_or_tools():
    assert cq.refusal_reason(_candidate(host=""), now=NOW) is not None
    assert cq.refusal_reason(_candidate(project=""), now=NOW) is not None
    assert cq.refusal_reason(_candidate(tools=()), now=NOW) is not None


def test_refusal_reason_for_unsupported_schema_version():
    cand = _candidate(version=2)
    reason = cq.refusal_reason(cand, now=NOW)
    assert reason is not None
    assert "schema version" in reason


def test_refusal_reason_for_effort_not_in_own_supported_efforts():
    cand = _candidate(reasoning_effort="high", supported_efforts=("low", "medium"))
    reason = cq.refusal_reason(cand, now=NOW)
    assert reason is not None
    assert "supported_efforts" in reason


def test_refusal_reason_missing_quota_refuses():
    cand = _candidate(quota=None)
    reason = cq.refusal_reason(cand, now=NOW)
    assert reason is not None
    assert "quota evidence" in reason


def test_refusal_reason_unknown_quota_remaining_refuses():
    cand = _candidate(quota=_quota(remaining=None))
    reason = cq.refusal_reason(cand, now=NOW)
    assert reason is not None
    assert "unknown" in reason


def test_refusal_reason_exhausted_quota_refuses():
    cand = _candidate(quota=_quota(remaining=0))
    reason = cq.refusal_reason(cand, now=NOW)
    assert reason is not None
    assert "exhausted" in reason


def test_refusal_reason_future_quota_observation_refuses():
    cand = _candidate(quota=_quota(observed_at=_iso(NOW + timedelta(minutes=1))))
    reason = cq.refusal_reason(cand, now=NOW)
    assert reason is not None
    assert "future" in reason


def test_qualification_refusals_lists_only_refused_candidates():
    fresh = _candidate()
    stale = _candidate(host="other", expires_at=_iso(NOW - timedelta(hours=1)))
    refusals = cq.qualification_refusals([fresh, stale], now=NOW)
    assert len(refusals) == 1
    assert refusals[0]["candidate"] == stale.candidate_id()


# --- Evaluation suite / scoring --------------------------------------------------

def test_run_evaluation_suite_skips_tasks_above_candidate_effort():
    low_effort = _candidate(reasoning_effort="low")
    results = cq.run_evaluation_suite(low_effort, _synthetic_runner())
    categories = {r.task_id for r in results}
    # arch-01/review-01/recovery-01 require medium; only edit-01/docs-01 remain.
    assert categories == {"edit-01", "docs-01"}


def test_run_evaluation_suite_runs_all_tasks_for_high_effort_candidate():
    cand = _candidate(reasoning_effort="high")
    results = cq.run_evaluation_suite(cand, _synthetic_runner())
    assert len(results) == len(cq.EVALUATION_TASKS)


def test_run_evaluation_suite_skips_tasks_the_candidate_never_declares():
    # Declares only "low" and a custom "max" label, never "medium" — so
    # tasks requiring "medium" cannot be judged for this candidate even
    # though "max" might colloquially be stronger.
    cand = _candidate(reasoning_effort="max", supported_efforts=("low", "max"))
    results = cq.run_evaluation_suite(cand, _synthetic_runner())
    categories = {r.task_id for r in results}
    assert categories == {"edit-01", "docs-01"}


def test_evaluation_result_score_penalizes_regressions_and_retries():
    perfect = cq.EvaluationResult(task_id="edit-01", candidate_id="x", accepted=True)
    assert perfect.score() == 1.0
    flawed = cq.EvaluationResult(
        task_id="edit-01", candidate_id="x", accepted=True, regressions=2, retries=1,
    )
    assert round(flawed.score(), 2) == 0.75
    rejected = cq.EvaluationResult(task_id="edit-01", candidate_id="x", accepted=False)
    assert rejected.score() == 0.0


def test_category_scores_aggregates_mean_per_category():
    cand = _candidate(reasoning_effort="high")
    results = cq.run_evaluation_suite(cand, _synthetic_runner())
    scores = cq.category_scores(results)
    assert set(scores) == set(cq.EVALUATION_CATEGORIES)
    assert all(v == 1.0 for v in scores.values())


# --- Recommendation --------------------------------------------------------------

def test_recommend_candidate_picks_least_costly_by_explicit_cost():
    cheap = _candidate(
        provider="cursor", model="composer-2.5", reasoning_effort="low", host="bp",
        cost_amount=0.1, cost_units="usd_per_task",
    )
    pricey = _candidate(
        provider="claude", model="sonnet-5", reasoning_effort="high", host="bp",
        cost_amount=1.0, cost_units="usd_per_task",
    )
    results_by_candidate = {
        cheap.candidate_id(): cq.run_evaluation_suite(cheap, _synthetic_runner()),
        pricey.candidate_id(): cq.run_evaluation_suite(pricey, _synthetic_runner()),
    }
    rec = cq.recommend_candidate(
        [cheap, pricey], results_by_candidate, category="docs_consistency", now=NOW,
    )
    assert rec is not None
    assert rec.candidate.candidate_id() == cheap.candidate_id()
    assert "measured/configured cost" in rec.reason


def test_recommend_candidate_does_not_claim_least_costly_from_effort_alone():
    # Both candidates have unknown cost; a low-effort candidate must not be
    # preferred purely because low effort "sounds cheaper".
    low_effort = _candidate(
        provider="cursor", model="composer-2.5", reasoning_effort="low", host="bp",
    )
    high_effort = _candidate(
        provider="claude", model="sonnet-5", reasoning_effort="high", host="bp",
    )
    results_by_candidate = {
        low_effort.candidate_id(): cq.run_evaluation_suite(low_effort, _synthetic_runner()),
        high_effort.candidate_id(): cq.run_evaluation_suite(high_effort, _synthetic_runner()),
    }
    rec = cq.recommend_candidate(
        [low_effort, high_effort], results_by_candidate, category="docs_consistency", now=NOW,
    )
    assert rec is not None
    assert "cost unknown" in rec.reason
    assert "not a cost claim" in rec.reason


def test_recommend_candidate_refuses_when_no_candidate_qualifies():
    cand = _candidate(reasoning_effort="low", supported_efforts=("low", "medium", "high"))
    results_by_candidate = {
        cand.candidate_id(): cq.run_evaluation_suite(cand, _synthetic_runner(accepted=False)),
    }
    rec = cq.recommend_candidate(
        [cand], results_by_candidate, category="bounded_edit", now=NOW,
    )
    assert rec is None


def test_recommend_candidate_refuses_stale_evidence_even_if_scores_pass():
    stale = _candidate(expires_at=_iso(NOW - timedelta(hours=1)))
    results_by_candidate = {
        stale.candidate_id(): cq.run_evaluation_suite(stale, _synthetic_runner()),
    }
    rec = cq.recommend_candidate(
        [stale], results_by_candidate, category="docs_consistency", now=NOW,
    )
    assert rec is None


def test_recommend_candidate_refuses_without_fresh_quota_even_if_scores_pass():
    unknown_quota = _candidate(quota=_quota(remaining=None))
    results_by_candidate = {
        unknown_quota.candidate_id(): cq.run_evaluation_suite(unknown_quota, _synthetic_runner()),
    }
    rec = cq.recommend_candidate(
        [unknown_quota], results_by_candidate, category="docs_consistency", now=NOW,
    )
    assert rec is None


def test_recommend_candidate_high_risk_requires_stronger_reasoning():
    low_effort = _candidate(reasoning_effort="low")
    results_by_candidate = {
        low_effort.candidate_id(): cq.run_evaluation_suite(low_effort, _synthetic_runner()),
    }
    rec = cq.recommend_candidate(
        [low_effort], results_by_candidate, category="bounded_edit", risk="high", now=NOW,
    )
    assert rec is None


def test_recommend_candidate_high_risk_excludes_candidate_that_never_declares_min_effort():
    # Even if its configured effort label is "max", it never declared
    # "medium" so it cannot be credited with meeting the high-risk floor.
    cand = _candidate(reasoning_effort="max", supported_efforts=("low", "max"))
    results_by_candidate = {
        cand.candidate_id(): cq.run_evaluation_suite(cand, _synthetic_runner()),
    }
    rec = cq.recommend_candidate(
        [cand], results_by_candidate, category="bounded_edit", risk="high", now=NOW,
    )
    assert rec is None


def test_recommend_candidate_high_risk_accepts_qualifying_medium_effort():
    cand = _candidate(reasoning_effort="medium")
    results_by_candidate = {
        cand.candidate_id(): cq.run_evaluation_suite(cand, _synthetic_runner()),
    }
    rec = cq.recommend_candidate(
        [cand], results_by_candidate, category="bounded_edit", risk="high", now=NOW,
    )
    assert rec is not None
    assert rec.candidate.candidate_id() == cand.candidate_id()


def test_recommend_candidate_rejects_unknown_risk():
    cand = _candidate()
    try:
        cq.recommend_candidate([cand], {}, category="bounded_edit", risk="extreme", now=NOW)
    except ValueError as exc:
        assert "risk" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown risk")


def test_recommend_candidate_rejects_unknown_category():
    cand = _candidate()
    try:
        cq.recommend_candidate([cand], {}, category="not-a-real-category", now=NOW)
    except ValueError as exc:
        assert "category" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown category")


def test_recommend_candidate_ignores_results_recorded_for_another_candidate():
    cand = _candidate()
    other = _candidate(host="other-host")
    # Results keyed under cand's id but actually stamped with another
    # candidate_id must not count as cand's evidence.
    mislabeled = [
        cq.EvaluationResult(
            task_id="docs-01", candidate_id=other.candidate_id(), accepted=True,
            observed_at=_iso(NOW),
        )
    ]
    rec = cq.recommend_candidate(
        [cand], {cand.candidate_id(): mislabeled}, category="docs_consistency", now=NOW,
    )
    assert rec is None


def test_recommend_candidate_rejects_future_evaluation_results():
    cand = _candidate()
    results = cq.run_evaluation_suite(
        cand, _synthetic_runner(observed_at=_iso(NOW + timedelta(hours=1))),
    )
    rec = cq.recommend_candidate(
        [cand], {cand.candidate_id(): results}, category="docs_consistency", now=NOW,
    )
    assert rec is None


def test_recommend_candidate_rejects_stale_evaluation_results():
    cand = _candidate()
    results = cq.run_evaluation_suite(
        cand, _synthetic_runner(observed_at=_iso(NOW - timedelta(days=60))),
    )
    rec = cq.recommend_candidate(
        [cand], {cand.candidate_id(): results}, category="docs_consistency", now=NOW,
    )
    assert rec is None


def test_recommend_candidate_rejects_candidate_with_unresolved_regression():
    cand = _candidate()
    results = cq.run_evaluation_suite(cand, _synthetic_runner(regressions=1))
    rec = cq.recommend_candidate(
        [cand], {cand.candidate_id(): results}, category="docs_consistency", now=NOW,
    )
    assert rec is None


# --- Report -----------------------------------------------------------------------

def test_build_capability_report_no_paid_calls_uses_synthetic_runner():
    cand = _candidate(reasoning_effort="high")
    results_by_candidate = {
        cand.candidate_id(): cq.run_evaluation_suite(cand, _synthetic_runner()),
    }
    report = cq.build_capability_report(
        [cand], results_by_candidate, now=NOW,
    )
    assert report["schema"] == cq.SCHEMA_ID
    assert report["refusals"] == []
    assert set(report["recommendations"]) == set(cq.EVALUATION_CATEGORIES)
    for category in cq.EVALUATION_CATEGORIES:
        assert report["recommendations"][category]["candidate_id"] == cand.candidate_id()


def test_build_capability_report_records_refusals_without_recommending():
    unauth = _candidate(account_state="unauthenticated")
    report = cq.build_capability_report([unauth], {}, now=NOW)
    assert len(report["refusals"]) == 1
    assert all(v is None for v in report["recommendations"].values())


def test_stale_quota_refuses_even_when_authentication_is_fresh():
    candidate = _candidate(quota=_quota(observed_at=_iso(NOW - timedelta(hours=1))))
    assert "stale" in cq.refusal_reason(candidate, now=NOW)


def test_boolean_version_and_nonfinite_direct_quota_refused():
    assert cq.parse_candidate_records([_raw_candidate(version=True)]) == []
    for value in [True, float("nan"), float("inf"), -1]:
        assert cq.refusal_reason(_candidate(quota=_quota(remaining=value)), now=NOW)


def test_incomparable_cost_units_do_not_make_a_cheapest_claim():
    candidates = [_candidate(provider="alpha", cost_amount=100, cost_units="tokens"),
                  _candidate(provider="beta", cost_amount=1, cost_units="usd")]
    results = {c.candidate_id(): cq.run_evaluation_suite(c, _synthetic_runner()) for c in candidates}
    chosen = cq.recommend_candidate(candidates, results, category="review", now=NOW)
    assert chosen.candidate.provider == "alpha"
    assert "incomparable" in chosen.reason

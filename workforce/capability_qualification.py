"""Provider/model capability qualification.

Model selection comes from measured task fit, supported tools, permissions
and fresh capacity evidence, never from provider stereotypes or which
binaries happen to be installed locally. Candidate records are versioned
and dated; missing or stale evidence refuses automatic routing rather than
guessing quota, reset time, or authentication state. Evaluations here are
synthetic (a caller-supplied scoring runner, no live model calls) so the
default test suite never spends paid usage.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ._utils import _parse_iso_z, _utc_iso_z, _utcnow

SCHEMA_ID = "workforce.capability_qualification/v1"
SUPPORTED_SCHEMA_VERSIONS = (1,)

# Quota pools have distinct exhaustion/reset semantics — never conflated.
QUOTA_POOL_SUBSCRIPTION = "subscription"
QUOTA_POOL_API_BUDGET = "api_budget"
QUOTA_POOL_SHARED = "shared"
QUOTA_POOLS = (QUOTA_POOL_SUBSCRIPTION, QUOTA_POOL_API_BUDGET, QUOTA_POOL_SHARED)

ACCOUNT_STATES = ("authenticated", "unauthenticated", "unknown")

# Canonical vocabulary evaluation tasks use to state a minimum requirement.
# This is naming only, not a claim that every candidate ranks these the
# same way — a candidate's own strength ordering is its declared
# ``supported_efforts``, never assumed from this list.
CANONICAL_EFFORTS = ("low", "medium", "high")

RISK_LEVELS = ("low", "medium", "high")

# Representative evaluation categories a candidate can be scored against.
EVALUATION_CATEGORIES = (
    "architecture",
    "bounded_edit",
    "docs_consistency",
    "review",
    "recovery",
)

DEFAULT_ACCEPTANCE_THRESHOLD = 0.8
# Risk tiers that require at least this canonical reasoning effort to be
# eligible — only applied when the candidate itself declares this label
# among its own ``supported_efforts``; a candidate that never declares it
# cannot be credited with meeting it.
HIGH_RISK_MIN_EFFORT = "medium"
# How old a synthetic evaluation result may be and still count as evidence.
DEFAULT_EVALUATION_MAX_AGE = datetime.timedelta(days=30)
DEFAULT_QUOTA_MAX_AGE = datetime.timedelta(minutes=15)


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


@dataclass(frozen=True)
class QuotaObservation:
    """One dated read of a candidate's quota pool.

    ``remaining`` is ``None`` when the provider does not report it —
    absence stays unknown rather than being guessed from a plan name or
    elapsed time. ``pool_id`` distinguishes multiple pools of the same
    ``pool`` kind (e.g. two shared org budgets); ``units`` names what
    ``remaining`` counts (requests, tokens, usd, ...) so pools are never
    compared across incompatible units.
    """

    pool: str
    pool_id: str
    units: str
    observed_at: str
    remaining: Optional[float] = None
    reset_at: Optional[str] = None
    note: str = ""

    @property
    def state(self) -> str:
        return "known" if self.remaining is not None else "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pool": self.pool,
            "pool_id": self.pool_id,
            "units": self.units,
            "observed_at": self.observed_at,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "note": self.note,
            "state": self.state,
        }


@dataclass(frozen=True)
class CandidateRecord:
    """One versioned provider/model/host/project inventory row.

    ``account_state`` records what was last observed, not an authentication
    guarantee — evidence goes stale and must be refreshed by a live probe.
    ``supported_efforts`` is the candidate's own explicitly declared
    reasoning-effort strength ordering (weakest first); it is never
    inferred from a global cross-provider table. ``cost_amount``/
    ``cost_units`` is an explicit measured or configured price; when
    unknown, cost stays ``None`` and no "least costly" claim is made from
    reasoning effort alone.
    """

    provider: str
    model: str
    reasoning_effort: str
    host: str
    tools: Tuple[str, ...]
    project: str
    account_state: str
    observed_at: str
    expires_at: str
    supported_efforts: Tuple[str, ...] = ()
    version: int = 1
    quota: Optional[QuotaObservation] = None
    cost_amount: Optional[float] = None
    cost_units: str = ""

    def candidate_id(self) -> str:
        return "%s/%s@%s#%s::%s" % (
            self.provider, self.model, self.reasoning_effort, self.host, self.project,
        )

    def effort_rank(self, effort: str) -> int:
        """Index of *effort* within this candidate's own declared strength
        ordering, or ``-1`` if the candidate never declared it."""
        try:
            return self.supported_efforts.index(effort)
        except ValueError:
            return -1

    def cost_known(self) -> bool:
        return self.cost_amount is not None and bool(self.cost_units)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id(),
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "host": self.host,
            "tools": list(self.tools),
            "project": self.project,
            "account_state": self.account_state,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "supported_efforts": list(self.supported_efforts),
            "version": self.version,
            "quota": self.quota.to_dict() if self.quota else None,
            "cost_amount": self.cost_amount,
            "cost_units": self.cost_units,
            "cost_known": self.cost_known(),
        }


def _parse_quota(raw: object) -> Optional[QuotaObservation]:
    """Parse a host-supplied quota row. Any malformed field drops the
    whole observation — a quota record with a guessed or invalid field is
    worse than an absent one (absent stays unknown; invalid must not be
    silently coerced into looking valid)."""
    if not isinstance(raw, dict):
        return None
    pool = str(raw.get("pool", "")).strip()
    if pool not in QUOTA_POOLS:
        return None
    pool_id = str(raw.get("pool_id", "")).strip()
    if not pool_id:
        return None
    units = str(raw.get("units", "")).strip()
    if not units:
        return None
    observed_at = str(raw.get("observed_at", "")).strip()
    if not observed_at or _parse_iso_z(observed_at) is None:
        # Never default a missing/invalid observation time to "now" —
        # that would manufacture freshness that was never measured.
        return None
    remaining = raw.get("remaining")
    if remaining is not None:
        if not _is_finite_number(remaining) or remaining < 0:
            return None
        remaining = float(remaining)
    reset_at = raw.get("reset_at")
    if reset_at is not None:
        reset_at = str(reset_at).strip() or None
    return QuotaObservation(
        pool=pool,
        pool_id=pool_id,
        units=units,
        observed_at=observed_at,
        remaining=remaining,
        reset_at=reset_at,
        note=str(raw.get("note", "")).strip(),
    )


def parse_candidate_records(raw: object) -> List[CandidateRecord]:
    """Parse host-supplied candidate JSON (list or {records: [...]}).

    Malformed rows (missing provider/model/host/project/tools/
    observed_at/expires_at, an unrecognized account_state, an unsupported
    schema version, or a reasoning_effort the row's own
    ``supported_efforts`` does not declare) are dropped rather than
    defaulted — a candidate with guessed fields is worse than an absent
    one.
    """
    if isinstance(raw, dict) and "records" in raw:
        raw = raw["records"]
    if not isinstance(raw, list):
        return []
    out: List[CandidateRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        required_strings = ("provider", "model", "host", "project", "observed_at", "expires_at", "reasoning_effort")
        if any(not isinstance(item.get(k), str) or not item[k].strip() for k in required_strings):
            continue
        provider = item["provider"].strip().lower()
        model = str(item.get("model", "")).strip()
        host = str(item.get("host", "")).strip()
        project = str(item.get("project", "")).strip()
        observed_at = str(item.get("observed_at", "")).strip()
        expires_at = str(item.get("expires_at", "")).strip()
        account_state = str(item.get("account_state", "unknown")).strip().lower()
        if not provider or not model or not host or not project:
            continue
        if not observed_at or not expires_at:
            continue
        if account_state not in ACCOUNT_STATES:
            continue
        version = item.get("version")
        if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
            continue
        tools_raw = item.get("tools") or []
        if not isinstance(tools_raw, list) or any(not isinstance(t, str) or not t.strip() for t in tools_raw):
            continue
        tools = tuple(str(t) for t in tools_raw if str(t).strip())
        if not tools:
            continue
        supported_raw = item.get("supported_efforts") or []
        if (not isinstance(supported_raw, list) or
                any(not isinstance(e, str) or not e.strip() for e in supported_raw) or
                len(set(supported_raw)) != len(supported_raw)):
            continue
        supported_efforts = tuple(str(e).strip().lower() for e in supported_raw if str(e).strip())
        reasoning_effort = str(item.get("reasoning_effort", "")).strip().lower()
        if not reasoning_effort or reasoning_effort not in supported_efforts:
            continue
        cost_amount = item.get("cost_amount")
        if cost_amount is not None and (not _is_finite_number(cost_amount) or cost_amount < 0):
            cost_amount = None
        cost_units = str(item.get("cost_units", "")).strip() if cost_amount is not None else ""
        if cost_amount is not None and not cost_units:
            cost_amount = None
        out.append(CandidateRecord(
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            host=host,
            tools=tools,
            project=project,
            account_state=account_state,
            observed_at=observed_at,
            expires_at=expires_at,
            supported_efforts=supported_efforts,
            version=version,
            quota=_parse_quota(item.get("quota")),
            cost_amount=float(cost_amount) if cost_amount is not None else None,
            cost_units=cost_units,
        ))
    return out


def is_stale(record: CandidateRecord, *, now: Optional[datetime.datetime] = None) -> bool:
    """True when the evidence window (``observed_at`` <= *now* <
    ``expires_at``) is unparseable, not yet started, or already passed."""
    now = now or _utcnow()
    observed = _parse_iso_z(record.observed_at)
    expires = _parse_iso_z(record.expires_at)
    if observed is None or expires is None:
        return True
    return not (observed <= now < expires)


def _quota_refusal(record: CandidateRecord, *, now: datetime.datetime) -> Optional[str]:
    """Capacity evidence required for automatic routing.

    Unknown quota (absent, unparseable remaining, or a future/unparseable
    observation time) must not silently pass automatic policy — it stays
    unknown and refuses, same as missing capability evidence.
    """
    quota = record.quota
    if quota is None:
        return "%s has no quota evidence" % record.candidate_id()
    if quota.pool not in QUOTA_POOLS or not quota.pool_id or not quota.units:
        return "%s quota identity is invalid" % record.candidate_id()
    if quota.remaining is None:
        return "%s quota remaining is unknown" % record.candidate_id()
    if not _is_finite_number(quota.remaining) or quota.remaining < 0:
        return "%s quota remaining is invalid" % record.candidate_id()
    observed = _parse_iso_z(quota.observed_at)
    if observed is None:
        return "%s quota observed_at is unparseable" % record.candidate_id()
    if observed > now:
        return "%s quota observed_at is in the future" % record.candidate_id()
    if now - observed > DEFAULT_QUOTA_MAX_AGE:
        return "%s quota evidence is stale" % record.candidate_id()
    if quota.remaining <= 0:
        return "%s quota pool %s is exhausted" % (record.candidate_id(), quota.pool_id)
    return None


def refusal_reason(
    record: CandidateRecord, *, now: Optional[datetime.datetime] = None,
) -> Optional[str]:
    """Reason automatic routing must refuse this candidate, else ``None``."""
    now = now or _utcnow()
    if type(record.version) is not int or record.version not in SUPPORTED_SCHEMA_VERSIONS:
        return "%s schema version %r is unsupported" % (record.candidate_id(), record.version)
    if not record.host or not record.project or not record.tools:
        return "%s is missing host, project or tools" % record.candidate_id()
    if record.account_state != "authenticated":
        return "%s account_state=%r is not authenticated" % (
            record.candidate_id(), record.account_state,
        )
    if is_stale(record, now=now):
        return "%s evidence is not fresh (observed_at<=now<expires_at failed)" % record.candidate_id()
    if record.effort_rank(record.reasoning_effort) < 0:
        return "%s reasoning_effort %r is not in its own supported_efforts" % (
            record.candidate_id(), record.reasoning_effort,
        )
    return _quota_refusal(record, now=now)


def qualification_refusals(
    candidates: Sequence[CandidateRecord], *, now: Optional[datetime.datetime] = None,
) -> List[Dict[str, str]]:
    """Per-candidate refusal reasons — missing/stale evidence stays visible."""
    out: List[Dict[str, str]] = []
    for cand in candidates:
        reason = refusal_reason(cand, now=now)
        if reason:
            out.append({"candidate": cand.candidate_id(), "reason": reason})
    return out


@dataclass(frozen=True)
class EvaluationTask:
    """One small, representative synthetic task definition."""

    id: str
    category: str
    description: str
    min_reasoning_effort: str = "low"


EVALUATION_TASKS: Tuple[EvaluationTask, ...] = (
    EvaluationTask(
        "arch-01", "architecture",
        "Propose a module boundary for a bounded feature without over-abstracting.",
        "medium",
    ),
    EvaluationTask(
        "edit-01", "bounded_edit",
        "Apply a scoped edit that satisfies acceptance criteria without touching unrelated files.",
        "low",
    ),
    EvaluationTask(
        "docs-01", "docs_consistency",
        "Update generated docs to match a source change without drift.",
        "low",
    ),
    EvaluationTask(
        "review-01", "review",
        "Flag a regression a naive diff review would miss.",
        "medium",
    ),
    EvaluationTask(
        "recovery-01", "recovery",
        "Resume a bounded pass after an interrupted shift without duplicating work.",
        "medium",
    ),
)

_TASKS_BY_ID: Dict[str, EvaluationTask] = {t.id: t for t in EVALUATION_TASKS}


def _meets_task_effort(candidate: CandidateRecord, task: EvaluationTask) -> bool:
    """Whether *candidate* meets *task*'s minimum, judged only against the
    candidate's own declared ``supported_efforts`` ordering — never a
    global cross-provider rank."""
    cand_rank = candidate.effort_rank(candidate.reasoning_effort)
    min_rank = candidate.effort_rank(task.min_reasoning_effort)
    if cand_rank < 0 or min_rank < 0:
        return False
    return cand_rank >= min_rank


@dataclass(frozen=True)
class EvaluationResult:
    """One scored run of an evaluation task against a candidate."""

    task_id: str
    candidate_id: str
    accepted: bool
    regressions: int = 0
    retries: int = 0
    elapsed_secs: Optional[float] = None
    usage: Optional[Dict[str, Any]] = None
    observed_at: str = field(default_factory=_utc_iso_z)

    def score(self) -> float:
        """0..1 acceptance score, penalized by regressions and retries."""
        if not self.accepted:
            return 0.0
        penalty = 0.1 * max(0, self.regressions) + 0.05 * max(0, self.retries)
        return max(0.0, 1.0 - penalty)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "accepted": self.accepted,
            "regressions": self.regressions,
            "retries": self.retries,
            "elapsed_secs": self.elapsed_secs,
            "usage": self.usage,
            "observed_at": self.observed_at,
            "score": self.score(),
        }


# A runner scores one (task, candidate) pair. Tests pass a synthetic
# (rule-based) runner — no paid model calls in the default suite. Live
# qualification wires an explicit bounded-run runner separately.
EvaluationRunner = Callable[[EvaluationTask, CandidateRecord], EvaluationResult]


def run_evaluation_suite(
    candidate: CandidateRecord,
    runner: EvaluationRunner,
    *,
    tasks: Sequence[EvaluationTask] = EVALUATION_TASKS,
) -> List[EvaluationResult]:
    """Run each task the candidate's own declared efforts qualify it for."""
    out: List[EvaluationResult] = []
    for task in tasks:
        if not _meets_task_effort(candidate, task):
            continue
        out.append(runner(task, candidate))
    return out


def _usable_results(
    candidate: CandidateRecord,
    results: Iterable[EvaluationResult],
    *,
    now: datetime.datetime,
    max_age: datetime.timedelta,
) -> List[EvaluationResult]:
    """Results that actually belong to *candidate* and are neither stale
    nor from the future — a result for another candidate_id, or one whose
    timestamp cannot be trusted, is not evidence for this candidate."""
    cid = candidate.candidate_id()
    out: List[EvaluationResult] = []
    for r in results:
        if r.candidate_id != cid:
            continue
        if (type(r.accepted) is not bool or type(r.regressions) is not int
                or type(r.retries) is not int or r.regressions < 0 or r.retries < 0):
            continue
        observed = _parse_iso_z(r.observed_at)
        if observed is None or observed > now or (now - observed) > max_age:
            continue
        out.append(r)
    return out


def category_scores(
    results: Iterable[EvaluationResult],
    *,
    tasks_by_id: Optional[Dict[str, EvaluationTask]] = None,
) -> Dict[str, float]:
    """Mean score per evaluation category (only categories with results)."""
    tasks_by_id = tasks_by_id or _TASKS_BY_ID
    buckets: Dict[str, List[float]] = {}
    for r in results:
        task = tasks_by_id.get(r.task_id)
        if task is None:
            continue
        buckets.setdefault(task.category, []).append(r.score())
    return {cat: round(sum(v) / len(v), 3) for cat, v in buckets.items()}


@dataclass(frozen=True)
class Recommendation:
    """Candidate that met acceptance for one category, chosen by explicit
    measured/configured cost when known — reasoning effort alone is never
    treated as a cross-provider price."""

    candidate: CandidateRecord
    category: str
    category_scores: Dict[str, float]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        d = self.candidate.to_dict()
        d["category"] = self.category
        d["category_scores"] = self.category_scores
        d["reason"] = self.reason
        return d


def _cost_sort_key(record: CandidateRecord) -> Tuple[int, float, str]:
    if record.cost_known():
        return (0, record.cost_amount, record.candidate_id())
    return (1, 0.0, record.candidate_id())


def recommend_candidate(
    candidates: Sequence[CandidateRecord],
    results_by_candidate: Dict[str, List[EvaluationResult]],
    *,
    category: str,
    risk: str = "low",
    threshold: float = DEFAULT_ACCEPTANCE_THRESHOLD,
    now: Optional[datetime.datetime] = None,
    eval_max_age: datetime.timedelta = DEFAULT_EVALUATION_MAX_AGE,
) -> Optional[Recommendation]:
    """Qualified candidate meeting acceptance for ``category``.

    Refuses (returns ``None``) rather than guessing when no candidate has
    fresh, authenticated evidence, applicable fresh quota, and a passing
    score with no unresolved regression — this is the seam a later router
    consumes; it never silently substitutes a stale record.

    Raises ``ValueError`` for an unknown *category* or *risk* — those are
    caller programming errors, not evidence gaps to refuse quietly.
    High-risk categories require the candidate's own declared
    :data:`HIGH_RISK_MIN_EFFORT` strength (a candidate that never declares
    that label cannot be credited with meeting it), so complex/high-risk
    changes get stronger reasoning and separate review capacity.
    """
    if risk not in RISK_LEVELS:
        raise ValueError("unknown risk level: %r" % (risk,))
    if not _is_finite_number(threshold) or not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    category_tasks = [t for t in EVALUATION_TASKS if t.category == category]
    if not category_tasks:
        raise ValueError("unknown evaluation category: %r" % (category,))
    now = now or _utcnow()

    qualified: List[Tuple[CandidateRecord, Dict[str, float]]] = []
    for cand in candidates:
        if refusal_reason(cand, now=now) is not None:
            continue
        if risk == "high":
            high_rank = cand.effort_rank(HIGH_RISK_MIN_EFFORT)
            cand_rank = cand.effort_rank(cand.reasoning_effort)
            if high_rank < 0 or cand_rank < high_rank:
                continue
        if not any(_meets_task_effort(cand, t) for t in category_tasks):
            continue
        results = _usable_results(
            cand, results_by_candidate.get(cand.candidate_id(), []),
            now=now, max_age=eval_max_age,
        )
        if any(r.regressions > 0 for r in results):
            continue
        scores = category_scores(results)
        score = scores.get(category)
        if score is None or score < threshold:
            continue
        qualified.append((cand, scores))
    if not qualified:
        return None
    comparable_costs = (all(c.cost_known() for c, _ in qualified)
                        and len({c.cost_units for c, _ in qualified}) == 1)
    qualified.sort(key=lambda pair: _cost_sort_key(pair[0]) if comparable_costs
                   else (0, 0.0, pair[0].candidate_id()))
    cand, scores = qualified[0]
    if comparable_costs:
        reason = "least costly qualified candidate (measured/configured cost=%s %s) meeting %s>=%.2f acceptance" % (
            cand.cost_amount, cand.cost_units, category, threshold,
        )
    else:
        reason = (
            "cost unknown or incomparable across qualified candidates meeting %s>=%.2f acceptance; "
            "selected by candidate_id, not a cost claim" % (category, threshold)
        )
    return Recommendation(candidate=cand, category=category, category_scores=scores, reason=reason)


def build_capability_report(
    candidates: Sequence[CandidateRecord],
    results_by_candidate: Dict[str, List[EvaluationResult]],
    *,
    categories: Sequence[str] = EVALUATION_CATEGORIES,
    risk: str = "low",
    threshold: float = DEFAULT_ACCEPTANCE_THRESHOLD,
    now: Optional[datetime.datetime] = None,
    eval_max_age: datetime.timedelta = DEFAULT_EVALUATION_MAX_AGE,
) -> Dict[str, Any]:
    """Full capability-qualification payload (JSON-serializable)."""
    now = now or _utcnow()
    recommendations: Dict[str, Optional[Dict[str, Any]]] = {}
    for category in categories:
        rec = recommend_candidate(
            candidates, results_by_candidate,
            category=category, risk=risk, threshold=threshold, now=now,
            eval_max_age=eval_max_age,
        )
        recommendations[category] = rec.to_dict() if rec else None
    return {
        "schema": SCHEMA_ID,
        "generated_at": _utc_iso_z(now),
        "risk": risk,
        "threshold": threshold,
        "candidates": [c.to_dict() for c in candidates],
        "refusals": qualification_refusals(candidates, now=now),
        "recommendations": recommendations,
    }

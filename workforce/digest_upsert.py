"""Chief-of-staff daily digest upsert — one desk ticket per local day.

Stops CoS freehand re-file spam (evidence: wf-140 / wf-144 / wf-148 same-day
triplicate). Complements engine ``max_fires_per_day``: that gate
throttles schedule fires; this helper enforces **one work order per calendar
day** when a shift does run.

Host-neutral: desk base URL from env / arg (never hard-coded product path
in the engine core). Pure planning is free of I/O; HTTP goes through
injectable ``_req`` + ``desk_writes_allowed`` (capacity hermetic guard).

Default is dry-run receipt — never mint a live digest without explicit
``dry_run=False`` and desk writes allowed.
"""

from __future__ import annotations

import datetime
import json
import os
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from .capacity import _req, desk_writes_allowed

DEFAULT_DESK = os.environ.get("WL_DESK_URL") or os.environ.get(
    "TP_DESK_URL", "http://127.0.0.1:8799"
)

# Canonical title form (historical digests wf-140/144/148 used this shape).
TITLE_PREFIX = "Chief-of-staff daily digest"

# Stable routing labels on every CoS digest (intake + re-run).
OPS_DIGEST_LABEL = "ops:digest"
# Legacy typo observed on wf-144 — still match for same-day find.
OPS_DIGEST_LEGACY = "ops-digest"

_OPEN_STATUSES = frozenset({"backlog", "in_progress", "in_review"})
_REUSE_STATUSES = frozenset({"backlog", "in_progress", "in_review", "done"})


def local_day_str(when: Optional[datetime.datetime] = None) -> str:
    """Host-local calendar day as YYYY-MM-DD (digest day boundary).

    Uses the host wall clock (no per-seat TZ). Matches schedule.host_wall
    intent: laptop local day is the product.
    """
    if when is None:
        when = datetime.datetime.now().astimezone()
    elif when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc).astimezone()
    else:
        when = when.astimezone()
    return when.strftime("%Y-%m-%d")


def digest_title(day: str) -> str:
    """Canonical ticket title for a calendar day."""
    return "%s · %s" % (TITLE_PREFIX, day)


def digest_day_label(day: str) -> str:
    """Idempotent day key label: ops:digest:YYYY-MM-DD."""
    return "%s:%s" % (OPS_DIGEST_LABEL, day)


def digest_labels(project: str, day: str) -> List[str]:
    """Labels for create — worker:you + you:note + ops:digest + day key."""
    project = (project or "workforce").strip() or "workforce"
    return [
        "worker:you",
        "you:note",
        OPS_DIGEST_LABEL,
        digest_day_label(day),
        "product:%s" % project,
    ]


def task_matches_day(task: dict, day: str) -> bool:
    """True when a desk task is the CoS digest for ``day``.

    Match order (any one is enough):
    1. day label ``ops:digest:YYYY-MM-DD``
    2. exact canonical title
    3. ``ops:digest`` / ``ops-digest`` plus day substring in title
    """
    if not isinstance(task, dict):
        return False
    title = str(task.get("title") or "")
    labs = [str(x) for x in (task.get("labels") or [])]
    day_lab = digest_day_label(day)
    if day_lab in labs:
        return True
    want = digest_title(day)
    if title == want or title.startswith(want + " "):
        return True
    if day not in title:
        return False
    if OPS_DIGEST_LABEL in labs or OPS_DIGEST_LEGACY in labs:
        return True
    # Historical: title-only match without ops:digest
    if TITLE_PREFIX.lower() in title.lower() and day in title:
        return True
    return False


def _status_rank(status: str) -> int:
    """Prefer open over done when multiple same-day rows exist."""
    st = (status or "").lower()
    if st in _OPEN_STATUSES:
        return 0
    if st == "done":
        return 1
    return 9


def find_same_day_digest(
    tasks: List[dict],
    day: str,
) -> Optional[dict]:
    """Pick the best same-day digest ticket from a task list (pure).

    Reuses open or **done** (ticket law: update-in-place, never re-file).
    Skips canceled. Prefer open > done; then oldest created_at.
    """
    matches: List[dict] = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        st = str(t.get("status") or "").lower()
        if st in ("canceled", "cancelled"):
            continue
        if st not in _REUSE_STATUSES:
            continue
        if task_matches_day(t, day):
            matches.append(t)
    if not matches:
        return None

    def _key(t: dict) -> Tuple[int, str, str]:
        return (
            _status_rank(str(t.get("status") or "")),
            str(t.get("created_at") or ""),
            str(t.get("id") or ""),
        )

    matches.sort(key=_key)
    return matches[0]


def plan_digest_upsert(
    existing: Optional[dict],
    day: str,
    body: str,
    project: str = "workforce",
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Pure plan: would_create / would_update / create / update fields.

    No I/O. Callers attach HTTP results later. ``action`` uses ``would_*``
    when dry_run so receipts never claim a write that did not run.
    """
    project = (project or "workforce").strip() or "workforce"
    title = digest_title(day)
    labels = digest_labels(project, day)
    receipt: Dict[str, Any] = {
        "ok": True,
        "project": project,
        "day": day,
        "title": title,
        "labels": labels,
        "day_label": digest_day_label(day),
        "action": "none",
        "dry_run": bool(dry_run),
    }
    if existing:
        tid = str(existing.get("id") or "")
        receipt["task_id"] = tid
        receipt["prior_status"] = existing.get("status")
        receipt["action"] = "would_update" if dry_run else "update"
        receipt["patch"] = {
            "title": title[:200],
            "description": body,
        }
        return receipt

    receipt["action"] = "would_create" if dry_run else "create"
    receipt["create"] = {
        "title": title[:200],
        "description": body,
        "labels": labels,
        "priority": 3,
        "intake": "workforce-cos-digest",
        "project": project,
    }
    return receipt


def list_digest_candidates(
    desk: str,
    project: str,
    day: str,
    *,
    limit: int = 50,
) -> List[dict]:
    """List desk tasks that might be today's digest (label + title probes).

    Two GETs: by day label (new shape) and by ops:digest (historical).
    Merges by id. Host-neutral desk base required.
    """
    desk = desk.rstrip("/")
    project = (project or "workforce").strip() or "workforce"
    seen: Dict[str, dict] = {}
    probes = [digest_day_label(day), OPS_DIGEST_LABEL, OPS_DIGEST_LEGACY]
    for label in probes:
        q = urllib.parse.urlencode(
            {"product": project, "label": label, "limit": int(limit)}
        )
        data = _req("GET", "%s/api/admin/tasks?%s" % (desk, q))
        tasks = data.get("tasks") or data.get("items") or []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            if tid and tid not in seen:
                seen[tid] = t
    # Title search fallback: list recent worker:you notes and filter pure-side
    q2 = urllib.parse.urlencode(
        {
            "product": project,
            "label": "you:note",
            "limit": int(limit),
        }
    )
    data2 = _req("GET", "%s/api/admin/tasks?%s" % (desk, q2))
    for t in data2.get("tasks") or data2.get("items") or []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        if tid and tid not in seen and task_matches_day(t, day):
            seen[tid] = t
    return list(seen.values())


def upsert_cos_digest(
    body: str,
    *,
    day: Optional[str] = None,
    project: str = "workforce",
    desk: str = "",
    author: str = "chief-of-staff",
    dry_run: bool = True,
    existing: Optional[dict] = None,
    candidates: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Create or update the CoS daily digest ticket for ``day``.

    Default ``dry_run=True`` — token-free verify path. Under pytest /
    ``WORKFORCE_NO_DESK=1`` live writes flip to dry-run (hermetic).

    Pure inject points for tests:
    - pass ``existing`` to skip list
    - pass ``candidates`` to skip HTTP list (still pure find + plan)
    - monkeypatch ``_req`` for full HTTP path tests
    """
    day = day or local_day_str()
    desk = (desk or DEFAULT_DESK).rstrip("/")
    project = (project or "workforce").strip() or "workforce"
    body = body if body is not None else ""

    # Hermetic: refuse live desk even if caller asked for dry_run=False.
    hermetic_block = (not dry_run) and (not desk_writes_allowed())
    effective_dry = bool(dry_run or hermetic_block)

    if existing is None:
        if candidates is not None:
            existing = find_same_day_digest(candidates, day)
        elif effective_dry:
            # dry_run with no inject: plan as create (no desk probe)
            existing = None
        else:
            existing = find_same_day_digest(
                list_digest_candidates(desk, project, day), day
            )

    receipt = plan_digest_upsert(
        existing, day, body, project=project, dry_run=effective_dry,
    )
    if hermetic_block:
        receipt["hermetic"] = True
    receipt["desk"] = desk

    if effective_dry:
        return receipt

    # Live path
    if existing:
        tid = str(existing.get("id") or "")
        if not tid:
            receipt["ok"] = False
            receipt["error"] = "existing task missing id"
            receipt["action"] = "update_failed"
            return receipt
        patch = receipt.get("patch") or {
            "title": digest_title(day)[:200],
            "description": body,
        }
        out = _req(
            "PATCH",
            "%s/api/admin/tasks/%s?product=%s"
            % (desk, urllib.parse.quote(tid, safe=""), project),
            patch,
        )
        if out.get("ok") is False and out.get("error"):
            receipt["ok"] = False
            receipt["error"] = out.get("error")
            receipt["action"] = "update_failed"
            receipt["api"] = out
            return receipt
        receipt["action"] = "updated"
        receipt["task_id"] = tid
        receipt["api"] = out
        # Legacy rows matched by title keep their labels; new creates stamp
        # ops:digest + ops:digest:YYYY-MM-DD. Label repair is out of scope.
        return receipt

    create_body = dict(receipt.get("create") or {})
    create_body["author"] = author
    out = _req(
        "POST",
        "%s/api/admin/tasks?product=%s" % (desk, project),
        create_body,
    )
    task = out.get("task") or {}
    tid = str(task.get("id") or "")
    if not tid:
        receipt["ok"] = False
        receipt["error"] = out.get("error") or out
        receipt["action"] = "create_failed"
        receipt["api"] = out
        return receipt
    receipt["action"] = "created"
    receipt["task_id"] = tid
    receipt["api"] = out
    return receipt


def format_receipt(receipt: Dict[str, Any]) -> str:
    """Human-readable one-block receipt for CLI."""
    lines = [
        "cos-digest: action=%s day=%s project=%s"
        % (receipt.get("action"), receipt.get("day"), receipt.get("project")),
        "  title: %s" % (receipt.get("title") or ""),
        "  day_label: %s" % (receipt.get("day_label") or ""),
    ]
    if receipt.get("task_id"):
        lines.append("  task_id: %s" % receipt["task_id"])
    if receipt.get("prior_status"):
        lines.append("  prior_status: %s" % receipt["prior_status"])
    if receipt.get("hermetic"):
        lines.append("  hermetic: live write refused (desk_writes_allowed=False)")
    if receipt.get("dry_run"):
        lines.append("  dry_run: true")
    if receipt.get("ok") is False:
        lines.append("  error: %s" % (receipt.get("error") or "?"))
    labs = receipt.get("labels") or []
    if labs:
        lines.append("  labels: %s" % ", ".join(labs))
    return "\n".join(lines)

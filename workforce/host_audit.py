"""Host-mutation ghost-audit — mid-shift tier-2 residual.

Read-model over desk in_progress (and in_review) claims: scan title,
description, and comment bodies for the same tier-2 patterns used by
dispatch argv deny (engine.tier2_mutation_hit). Default is dry-run
receipt; ``--live`` posts Blocked: when desk_writes_allowed().

Host-neutral: desk URL from env / flag; product from CLI. Never writes
local/ roster, never invokes launchctl. OS seatbelt (option B) is out of
scope — see workers/salem/designs/wf-160.md.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from .capacity import desk_writes_allowed
from .engine import tier2_mutation_hit, _http_json

DEFAULT_DESK = os.environ.get("WL_DESK_URL") or os.environ.get(
    "TP_DESK_URL", "http://127.0.0.1:8799"
)

# Policy §B / design v1: open human gate whose title carries this phrase.
_FOUNDER_HOST_RE = re.compile(r"FOUNDER\s*·\s*host", re.IGNORECASE)

# Idempotent re-run: auditor's own Blocked body quotes patterns — skip noise.
_ALREADY_BLOCKED_RE = re.compile(
    r"Blocked:\s*Host-mutation ghost-audit",
    re.IGNORECASE,
)

_COMMENT_CAP = 40
_HTTP_TIMEOUT = 12.0

# Surfaces scanned in priority order (design §2).
_CLAIM_STATUSES = ("in_progress", "in_review")


def has_founder_host_gate(tasks: List[dict]) -> bool:
    """True when any open human-gated ticket is a FOUNDER · host act-now.

    v1 (strict, cheap): title starts with or contains ``FOUNDER · host``.
    Per-label match tightening is a later slice if false clears appear.
    """
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "")
        if _FOUNDER_HOST_RE.search(title):
            return True
        # Some desk payloads put the phrase only in description.
        desc = str(t.get("description") or "")
        if _FOUNDER_HOST_RE.search(desc[:500]):
            return True
    return False


def already_host_mutation_blocked(comments: List[dict]) -> bool:
    """True when a prior host-audit Blocked: already sits on the claim."""
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        body = str(c.get("body") or "")
        if _ALREADY_BLOCKED_RE.search(body):
            return True
    return False


def scan_claim_surfaces(
    title: str,
    description: str,
    comments: Optional[List[dict]] = None,
    *,
    comment_cap: int = _COMMENT_CAP,
) -> Optional[Tuple[str, str]]:
    """Return (pattern, surface) for first tier-2 hit, or None if clear.

    *surface* is ``title``, ``description``, or ``comment:<id|n>``.
    Skips comments that already carry the host-audit Blocked: marker so
    re-runs stay idempotent.
    """
    hit = tier2_mutation_hit(title or "")
    if hit:
        return hit, "title"
    hit = tier2_mutation_hit(description or "")
    if hit:
        return hit, "description"

    # Newest-first: desk usually returns oldest-first; reverse then cap.
    raw = [c for c in (comments or []) if isinstance(c, dict)]
    ordered = list(reversed(raw))[:comment_cap]

    for idx, c in enumerate(ordered):
        body = str(c.get("body") or "")
        if _ALREADY_BLOCKED_RE.search(body):
            continue
        hit = tier2_mutation_hit(body)
        if hit:
            cid = c.get("id")
            surface = "comment:%s" % (cid if cid is not None else idx)
            return hit, surface
    return None


def build_blocked_body(pattern: str, surface: str, task_id: str) -> str:
    """PROCESS §5 Blocked: + Next step: body for ungated ghost-audit hit."""
    reason = (
        "Host-mutation ghost-audit — tier-2 pattern "
        "%s found in %s on claim %s without an open FOUNDER · host gate."
        % (pattern, surface, task_id)
    )
    next_step = (
        "stage only; file FOUNDER · host: … (gate_type=human) with exact "
        "commands + rollback; do not re-execute live service lifecycle "
        "autonomously."
    )
    return "Blocked: %s\nNext step: %s" % (reason, next_step)


def _list_tasks(
    desk: str,
    product: str,
    *,
    status: str = "",
    label: str = "",
    gate_type: str = "",
    limit: int = 100,
) -> List[dict]:
    q: Dict[str, Any] = {"product": product, "limit": limit}
    if status:
        q["status"] = status
    if label:
        q["label"] = label
    if gate_type:
        q["gate_type"] = gate_type
    data = _http_json(
        "GET",
        "%s/api/admin/tasks?%s" % (desk.rstrip("/"), urllib.parse.urlencode(q)),
        timeout=_HTTP_TIMEOUT,
    )
    tasks = data.get("tasks") or data.get("items") or []
    out: List[dict] = []
    for t in tasks:
        if isinstance(t, dict):
            out.append(t)
    return out


def _fetch_task(desk: str, product: str, task_id: str) -> Optional[dict]:
    q = urllib.parse.urlencode({"product": product})
    data = _http_json(
        "GET",
        "%s/api/admin/tasks/%s?%s"
        % (desk.rstrip("/"), urllib.parse.quote(str(task_id), safe=""), q),
        timeout=_HTTP_TIMEOUT,
    )
    task = data.get("task") if isinstance(data, dict) else None
    if isinstance(task, dict):
        return task
    if isinstance(data, dict) and data.get("id"):
        return data
    return None


def _post_blocked(
    desk: str,
    product: str,
    task_id: str,
    body: str,
    author: str,
) -> dict:
    q = urllib.parse.urlencode({"product": product})
    url = "%s/api/admin/tasks/%s/comments?%s" % (
        desk.rstrip("/"),
        urllib.parse.quote(str(task_id), safe=""),
        q,
    )
    return _http_json(
        "POST",
        url,
        {"body": body, "author": author},
        timeout=_HTTP_TIMEOUT,
    )


def list_open_founder_host_gates(desk: str, product: str) -> List[dict]:
    """Open human-gated tickets that look like FOUNDER · host (act-now).

    Design v1: any open ``gate_type=human`` ticket whose title/body carries
    the FOUNDER · host phrase. Falls back to status scan when the desk
    ignores gate_type filters.
    """
    candidates = _list_tasks(desk, product, gate_type="human", limit=100)
    if not candidates:
        for st in ("backlog", "in_progress", "in_review"):
            candidates.extend(_list_tasks(desk, product, status=st, limit=50))
    out: List[dict] = []
    seen = set()
    for t in candidates:
        tid = str(t.get("id") or "")
        if not tid or tid in seen:
            continue
        st = str(t.get("status") or "").lower()
        if st in ("done", "canceled", "cancelled"):
            continue
        gt = str(t.get("gate_type") or "").lower()
        # When gate_type filter worked, rows are human. On fallback status
        # scan, require human (or empty gate with phrase-only FOUNDER title).
        title = str(t.get("title") or "")
        desc = str(t.get("description") or "")
        phrase = bool(
            _FOUNDER_HOST_RE.search(title)
            or _FOUNDER_HOST_RE.search(desc[:500])
        )
        if not phrase:
            continue
        if gt and gt != "human":
            continue
        seen.add(tid)
        out.append(t)
    return out


def list_claim_tasks(
    desk: str,
    product: str,
    *,
    worker: str = "",
) -> List[dict]:
    """in_progress + in_review claims for *product*, optional worker: label."""
    label = ("worker:%s" % worker) if worker else ""
    out: List[dict] = []
    seen = set()
    for st in _CLAIM_STATUSES:
        for t in _list_tasks(desk, product, status=st, label=label, limit=100):
            tid = str(t.get("id") or "")
            if not tid or tid in seen:
                continue
            if label:
                labs = [str(x) for x in (t.get("labels") or [])]
                if label not in labs:
                    continue
            seen.add(tid)
            out.append(t)
    return out


def evaluate_claim(
    task: dict,
    *,
    gated: bool,
    comments: Optional[List[dict]] = None,
) -> dict:
    """Pure evaluation of one claim dict (+ optional full comments).

    Returns a receipt dict with keys: task_id, action, pattern?, surface?,
    gated?, body?, status?, title?.
    """
    tid = str(task.get("id") or "")
    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    comments = comments if comments is not None else (task.get("comments") or [])
    if not isinstance(comments, list):
        comments = []

    receipt: dict = {
        "task_id": tid,
        "status": str(task.get("status") or ""),
        "title": title[:120],
        "action": "clear",
        "ok": True,
    }

    if already_host_mutation_blocked(comments):
        receipt["action"] = "already_blocked"
        return receipt

    hit = scan_claim_surfaces(title, description, comments)
    if hit is None:
        return receipt

    pattern, surface = hit
    receipt["pattern"] = pattern
    receipt["surface"] = surface
    if gated:
        receipt["action"] = "gated_report"
        receipt["gated"] = True
        return receipt

    body = build_blocked_body(pattern, surface, tid)
    receipt["action"] = "would_block"
    receipt["body"] = body
    receipt["gated"] = False
    return receipt


def audit_product(
    product: str,
    *,
    desk: str = "",
    worker: str = "",
    author: str = "workforce",
    dry_run: bool = True,
    ledger_append=None,
) -> dict:
    """Scan open claims on *product*; dry-run or live Blocked: on ungated hits.

    *ledger_append* optional callable(event, **kw) for HOST_MUTATION_DENY
    when live-blocking (caller may pass a Ledger.append bound method).

    Exit semantics for CLI: receipt[\"ungated_hits\"] > 0 → non-zero.
    """
    desk = (desk or DEFAULT_DESK).rstrip("/")
    hermetic_block = (not dry_run) and (not desk_writes_allowed())
    if hermetic_block:
        dry_run = True

    summary: dict = {
        "ok": True,
        "product": product,
        "desk": desk,
        "dry_run": dry_run,
        "hermetic": bool(hermetic_block),
        "worker": worker or None,
        "claims_scanned": 0,
        "clear": 0,
        "gated_reports": 0,
        "would_block": 0,
        "blocked": 0,
        "already_blocked": 0,
        "ungated_hits": 0,
        "errors": 0,
        "results": [],
    }

    try:
        gates = list_open_founder_host_gates(desk, product)
        gated = has_founder_host_gate(gates)
        claims = list_claim_tasks(desk, product, worker=worker)
    except Exception as exc:
        summary["ok"] = False
        summary["error"] = "desk list failed: %.200s" % exc
        summary["errors"] = 1
        return summary

    summary["founder_host_gate"] = gated
    summary["claims_scanned"] = len(claims)

    for t in claims:
        tid = str(t.get("id") or "")
        # Prefer full task (comments) when list payload is summary-only.
        comments = t.get("comments")
        if not isinstance(comments, list) or not comments:
            try:
                full = _fetch_task(desk, product, tid)
            except Exception as exc:
                summary["errors"] += 1
                summary["results"].append({
                    "task_id": tid,
                    "action": "error",
                    "ok": False,
                    "error": "fetch failed: %.120s" % exc,
                })
                continue
            if full:
                t = full
                comments = t.get("comments") or []
            else:
                comments = []

        if not isinstance(comments, list):
            comments = []

        ev = evaluate_claim(t, gated=gated, comments=comments)
        action = ev.get("action") or "clear"

        if action == "clear":
            summary["clear"] += 1
        elif action == "already_blocked":
            summary["already_blocked"] += 1
        elif action == "gated_report":
            summary["gated_reports"] += 1
            summary["ungated_hits"] += 0  # not an ungated hit
        elif action == "would_block":
            summary["ungated_hits"] += 1
            if dry_run:
                summary["would_block"] += 1
            else:
                body = ev.get("body") or ""
                try:
                    out = _post_blocked(desk, product, tid, body, author)
                except Exception as exc:
                    ev["ok"] = False
                    ev["action"] = "comment_failed"
                    ev["error"] = str(exc)
                    summary["errors"] += 1
                    summary["results"].append(ev)
                    continue
                if out.get("ok") is False or out.get("error"):
                    ev["ok"] = False
                    ev["action"] = "comment_failed"
                    ev["error"] = out.get("error") or out
                    ev["api"] = out
                    summary["errors"] += 1
                else:
                    ev["action"] = "blocked"
                    ev["api"] = out
                    summary["blocked"] += 1
                    if ledger_append is not None:
                        try:
                            ledger_append(
                                "HOST_MUTATION_DENY",
                                source="ghost-audit",
                                task_id=tid,
                                pattern=ev.get("pattern") or "",
                                surface=ev.get("surface") or "",
                            )
                        except Exception:
                            pass
        summary["results"].append(ev)

    if summary["errors"] and not summary["results"]:
        summary["ok"] = False
    return summary


def format_receipt(summary: dict) -> str:
    """Human-readable multi-line receipt for CLI stdout."""
    lines = [
        "host-audit: product=%s dry_run=%s scanned=%d ungated_hits=%d%s"
        % (
            summary.get("product"),
            int(bool(summary.get("dry_run"))),
            summary.get("claims_scanned") or 0,
            summary.get("ungated_hits") or 0,
            " hermetic=1" if summary.get("hermetic") else "",
        ),
    ]
    if summary.get("error"):
        lines.append("  error: %s" % summary["error"])
    if summary.get("founder_host_gate"):
        lines.append("  founder_host_gate: open (hits report only, no Blocked)")
    for r in summary.get("results") or []:
        action = r.get("action") or "?"
        tid = r.get("task_id") or "?"
        extra = ""
        if r.get("pattern"):
            extra = " pattern=%s surface=%s" % (
                r.get("pattern"),
                r.get("surface"),
            )
        if r.get("error"):
            extra += " err=%s" % r["error"]
        lines.append("  %s %s%s" % (action, tid, extra))
    if (summary.get("ungated_hits") or 0) > 0 and summary.get("dry_run"):
        lines.append("  (pass --live to post Blocked: when desk writes allowed)")
    return "\n".join(lines)

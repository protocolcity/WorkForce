"""Stale needs:routing sweep on terminal desk tickets.

Closed work (done / canceled) must not keep a routing stamp — Map/triage
noise and false "needs hand" signals from bulk-label artefacts.

Host-neutral: desk base URL from env / arg; product list from desk
discovery API (or an explicit product list). Pure filters have no I/O.

Default is **report-only** (planned strip set). Live label removes require
``repair=True`` **and** ``desk_writes_allowed()`` (pytest / WORKFORCE_NO_DESK
stay hermetic). Never strips labels other than ``needs:routing``. Never
touches backlog / in_progress / in_review.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence

from .capacity import desk_writes_allowed
from .engine import _http_json

DEFAULT_DESK = os.environ.get("WL_DESK_URL") or os.environ.get(
    "TP_DESK_URL", "http://127.0.0.1:8799"
)

STALE_LABEL = "needs:routing"
TERMINAL_STATUSES = frozenset({"done", "canceled", "cancelled"})
# Desk API uses American spelling for the filter value.
_LIST_STATUSES = ("done", "canceled")

_HTTP_TIMEOUT = 15.0
_LIST_LIMIT = 200

# Injectable transport for tests (method, url, body?) -> dict
HttpFn = Callable[..., dict]


def is_terminal_status(status: str) -> bool:
    return (status or "").strip().lower() in TERMINAL_STATUSES


def has_needs_routing(labels: Optional[Sequence[Any]]) -> bool:
    for raw in labels or []:
        if str(raw).strip() == STALE_LABEL:
            return True
    return False


def is_stale_routing_candidate(task: dict) -> bool:
    """True when a task is done/canceled and still carries needs:routing.

    Pure. Open statuses never qualify — active backlog with needs:routing
    must stay for chief-of-staff / human routing.
    """
    if not isinstance(task, dict):
        return False
    if not is_terminal_status(str(task.get("status") or "")):
        return False
    return has_needs_routing(task.get("labels"))


def plan_strip(tasks: Sequence[dict]) -> List[dict]:
    """Return lightweight planned-strip rows (pure, stable order).

    Each row: task_id, product (if known), status, title (truncated).
    """
    out: List[dict] = []
    seen = set()
    for t in tasks or []:
        if not is_stale_routing_candidate(t):
            continue
        tid = str(t.get("id") or "").strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        title = str(t.get("title") or "")
        product = (
            str(t.get("product") or t.get("project") or "").strip() or None
        )
        out.append({
            "task_id": tid,
            "product": product,
            "status": str(t.get("status") or "").lower(),
            "title": title[:100],
        })
    out.sort(key=lambda r: (r.get("product") or "", r["task_id"]))
    return out


def _list_tasks(
    desk: str,
    product: str,
    *,
    status: str = "",
    label: str = "",
    limit: int = _LIST_LIMIT,
    http: Optional[HttpFn] = None,
) -> List[dict]:
    http = http or _http_json
    q: Dict[str, Any] = {"product": product, "limit": int(limit)}
    if status:
        q["status"] = status
    if label:
        q["label"] = label
    data = http(
        "GET",
        "%s/api/admin/tasks?%s" % (desk.rstrip("/"), urllib.parse.urlencode(q)),
        timeout=_HTTP_TIMEOUT,
    )
    if not isinstance(data, dict):
        return []
    tasks = data.get("tasks") or data.get("items") or []
    out: List[dict] = []
    for t in tasks:
        if isinstance(t, dict):
            # Stamp product for multi-store receipts when the desk omits it.
            if product and not t.get("product") and not t.get("project"):
                t = dict(t)
                t["product"] = product
            out.append(t)
    return out


def list_products(
    desk: str,
    *,
    http: Optional[HttpFn] = None,
) -> List[str]:
    """Discover product slugs from the desk (GET /api/admin/products).

    Host-neutral — no hard-coded city product list. Empty on failure.
    """
    http = http or _http_json
    data = http(
        "GET",
        "%s/api/admin/products" % desk.rstrip("/"),
        timeout=_HTTP_TIMEOUT,
    )
    if not isinstance(data, dict):
        return []
    products = data.get("products") or data.get("items") or []
    slugs: List[str] = []
    for p in products:
        if isinstance(p, dict):
            slug = str(p.get("slug") or p.get("product") or "").strip()
        else:
            slug = str(p).strip()
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def list_stale_routing(
    desk: str,
    product: str,
    *,
    http: Optional[HttpFn] = None,
    limit: int = _LIST_LIMIT,
) -> List[dict]:
    """List done/canceled tickets on *product* still labeled needs:routing.

    Double-filters client-side so a desk that ignores status/label still
    cannot return open work into the strip set.
    """
    desk = desk.rstrip("/")
    product = (product or "").strip()
    if not product:
        return []
    seen: Dict[str, dict] = {}
    for st in _LIST_STATUSES:
        for t in _list_tasks(
            desk, product, status=st, label=STALE_LABEL, limit=limit, http=http,
        ):
            if not is_stale_routing_candidate(t):
                continue
            tid = str(t.get("id") or "").strip()
            if tid and tid not in seen:
                seen[tid] = t
    return [seen[k] for k in sorted(seen)]


def remove_needs_routing(
    desk: str,
    product: str,
    task_id: str,
    *,
    http: Optional[HttpFn] = None,
) -> dict:
    """PATCH remove needs:routing only. Returns desk JSON (or error dict)."""
    http = http or _http_json
    q = urllib.parse.urlencode({"product": product})
    url = "%s/api/admin/tasks/%s/labels?%s" % (
        desk.rstrip("/"),
        urllib.parse.quote(str(task_id), safe=""),
        q,
    )
    return http(
        "PATCH",
        url,
        {"remove": [STALE_LABEL], "add": []},
        timeout=_HTTP_TIMEOUT,
    )


def scan_stale_routing(
    *,
    desk: str = "",
    products: Optional[Sequence[str]] = None,
    repair: bool = False,
    http: Optional[HttpFn] = None,
) -> Dict[str, Any]:
    """Scan one or many products; report or strip stale needs:routing.

    *products* None → discover via desk. Empty list after discovery → note.
    *repair* False → dry-run planned strip only. Live repair also requires
    desk_writes_allowed(); otherwise forced dry-run (hermetic).
    """
    desk = (desk or DEFAULT_DESK).rstrip("/")
    http = http or _http_json
    hermetic_block = bool(repair) and (not desk_writes_allowed())
    effective_repair = bool(repair) and (not hermetic_block)

    summary: Dict[str, Any] = {
        "ok": True,
        "desk": desk,
        "repair": bool(repair),
        "effective_repair": effective_repair,
        "dry_run": not effective_repair,
        "hermetic": hermetic_block,
        "label": STALE_LABEL,
        "products": [],
        "by_product": {},
        "planned": [],
        "stripped": [],
        "errors": [],
        "total": 0,
    }

    prod_list: List[str] = []
    if products is not None:
        for p in products:
            s = str(p or "").strip()
            if s and s not in prod_list:
                prod_list.append(s)
    else:
        try:
            prod_list = list_products(desk, http=http)
        except Exception as exc:
            summary["ok"] = False
            summary["errors"].append("product discovery failed: %.200s" % exc)
            return summary

    summary["products"] = list(prod_list)
    if not prod_list:
        summary["errors"].append("no products to scan")
        return summary

    all_planned: List[dict] = []
    for product in prod_list:
        try:
            tasks = list_stale_routing(desk, product, http=http)
        except Exception as exc:
            summary["errors"].append(
                "%s: list failed: %.200s" % (product, exc)
            )
            summary["by_product"][product] = {
                "count": 0,
                "error": str(exc)[:200],
            }
            continue
        planned = plan_strip(tasks)
        for row in planned:
            if not row.get("product"):
                row["product"] = product
        summary["by_product"][product] = {
            "count": len(planned),
            "task_ids": [r["task_id"] for r in planned],
        }
        all_planned.extend(planned)

    summary["planned"] = all_planned
    summary["total"] = len(all_planned)

    if not effective_repair:
        return summary

    for row in all_planned:
        tid = row["task_id"]
        product = str(row.get("product") or "")
        try:
            resp = remove_needs_routing(desk, product, tid, http=http)
        except Exception as exc:
            summary["errors"].append("%s: strip failed: %.200s" % (tid, exc))
            summary["ok"] = False
            continue
        if isinstance(resp, dict) and resp.get("ok") is False:
            err = resp.get("error") or resp
            summary["errors"].append("%s: strip refused: %s" % (tid, err))
            summary["ok"] = False
            continue
        summary["stripped"].append(tid)

    return summary


def format_report(summary: Dict[str, Any]) -> str:
    """Human-readable doctor section for a scan_stale_routing receipt."""
    lines: List[str] = []
    dry = summary.get("dry_run", True)
    mode = "report (dry-run)" if dry else "repair"
    if summary.get("hermetic"):
        mode = "report (repair blocked — hermetic / WORKFORCE_NO_DESK)"
    lines.append(
        "Stale %s: %s · desk %s"
        % (STALE_LABEL, mode, summary.get("desk") or "?")
    )
    by_p = summary.get("by_product") or {}
    if not by_p and summary.get("errors"):
        for e in summary["errors"]:
            lines.append("  note: %s" % e)
        return "\n".join(lines)

    total = int(summary.get("total") or 0)
    if total == 0 and not summary.get("errors"):
        lines.append("  clean — no done/canceled tickets still labeled")
        return "\n".join(lines)

    # Per-store counts
    for product in summary.get("products") or sorted(by_p.keys()):
        info = by_p.get(product) or {}
        if info.get("error"):
            lines.append("  %s: ERROR %s" % (product, info["error"]))
            continue
        n = int(info.get("count") or 0)
        lines.append("  %s: %d" % (product, n))

    lines.append("  total: %d" % total)

    planned = summary.get("planned") or []
    if planned and dry:
        # Cap print length for large bulk-label residue.
        show = planned[:40]
        ids = ", ".join(
            "%s(%s)" % (r["task_id"], r.get("status") or "?") for r in show
        )
        more = ""
        if len(planned) > len(show):
            more = " … +%d more" % (len(planned) - len(show))
        lines.append("  would_strip: %s%s" % (ids, more))
        lines.append("  (pass --repair to remove %s only on terminal)" % STALE_LABEL)
    elif summary.get("stripped"):
        lines.append(
            "  stripped: %s" % ", ".join(summary["stripped"][:40])
        )
        if len(summary["stripped"]) > 40:
            lines.append("  … +%d more" % (len(summary["stripped"]) - 40))

    for e in summary.get("errors") or []:
        lines.append("  error: %s" % e)

    return "\n".join(lines)

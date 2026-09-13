"""Desk JSON proxy, queue probe, and ticket holdings/ready/flags."""

import os
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from ...engine import _dig, _http_get_json, _is_timeout_exc
from ...roster import Worker
from .constants import (
    _BOARD_DESK_TIMEOUT_SECS,
    _BOARD_PROBE_RETRY_BACKOFF_SECS,
    _BOARD_PROBE_TIMEOUT_SECS,
    _desk,
)
from .helpers import _worker_identity_aliases


def _desk_json(path: str, timeout: float = 5.0) -> Optional[dict]:
    """GET DESK+path as JSON via ``_http_get_json``.

    *timeout* is host-neutral; board/scene paths pass a shorter bound so a
    hung desk cannot freeze Map. Any transport/parse/4xx failure
    still returns ``None`` — Map degrades, it does not raise.
    """
    try:
        return _http_get_json(_desk() + path, timeout=timeout)
    except Exception:
        return None


# ── Roster / worker helpers ───────────────────────────────────────────────

def _queue_human_link(w: Worker) -> str:
    """The way back: the queue probe is an API URL; the same desk serves the
    human view at /admin/tickets/<product>?label=... (a desk convention —
    conventions are the API, same as the law-stack walk)."""
    if not w.queue_url:
        return ""
    from urllib.parse import parse_qs, quote, urlsplit
    parts = urlsplit(w.queue_url)
    qs = parse_qs(parts.query)
    product = (qs.get("product") or qs.get("project") or [""])[0]
    if not product:
        return ""
    url = "%s://%s/admin/tickets/%s" % (parts.scheme, parts.netloc, product)
    label = (qs.get("label") or [""])[0]
    if label:
        url += "?label=" + quote(label)
    return url


# Board/scene display probe. Shorter than engine dispatch so
# a hung desk cannot freeze Map. Sockets always close via engine._http_get_json.
#
# wf-147: do **not** retry on timeout — with ~36 lanes fanned out, a 3s timeout
# + one retry made full /api/scene wall-clock ~6–16s when any product filter
# stalled (Map felt dead; clients hung up → BrokenPipe bursts on write).
# One short attempt: degrade to '?' and keep the glass under the 2s target.
# Retry only transient connection drops (reset / refused), not spent budget.
_BOARD_PROBE_TIMEOUT_SECS = 0.9
_BOARD_PROBE_RETRY_BACKOFF_SECS = 0.15
# Holdings / owner desk calls on the scene path share this tighter bound.
_BOARD_DESK_TIMEOUT_SECS = 1.0


def _is_connection_exc(exc: BaseException) -> bool:
    """True when a retry might help (peer reset / refused), not a timeout."""
    if _is_timeout_exc(exc):
        return False
    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return True
    name = type(exc).__name__
    if name in ("ConnectionResetError", "ConnectionRefusedError",
                "RemoteDisconnected"):
        return True
    msg = str(exc).lower()
    return ("connection reset" in msg or "connection refused" in msg
            or "remote end closed" in msg or "broken pipe" in msg)


def _worker_queue(w: Worker) -> str:
    """Ready count for this worker's lane — '?' when unprobed/unreachable.

    Uses the shared HTTP GET helper so timed-out polls close their sockets
   . One short attempt on timeout; one retry only on
    transient connection errors. Permanent failures still return '?'.
    """
    if not w.queue_url:
        return "—"
    for attempt in range(2):
        try:
            data = _http_get_json(w.queue_url, timeout=_BOARD_PROBE_TIMEOUT_SECS)
            return str(int(_dig(data, w.queue_count_key)))
        except Exception as exc:
            if attempt == 0 and _is_connection_exc(exc):
                time.sleep(_BOARD_PROBE_RETRY_BACKOFF_SECS)
                continue
            break
    return "?"


def _desk_owner_of(
    task_id: str, product: str = "",
) -> Tuple[str, bool, str]:
    """Latest Owner: marker on a ticket (PROCESS.md §5).

    Returns ``(owner, ok, error)``. ``ok=False`` means the detail fetch failed
    or the task could not be read — not a verified-empty Owner field.
    ``ok=True`` with an empty owner means the ticket was read and has no
    Owner marker. Bare numeric ids require an explicit *product* scope.
    """
    tid = str(task_id).strip()
    qid = urllib.parse.quote(tid)
    if tid.isdigit():
        if not product:
            return "", False, "bare task id requires project"
        path = "/api/admin/tasks/%s?project=%s" % (
            qid, urllib.parse.quote(product))
    else:
        path = "/api/admin/tasks/" + qid
    d = _desk_json(path, timeout=_BOARD_DESK_TIMEOUT_SECS)
    if d is None:
        return "", False, "detail unreachable"
    if not isinstance(d, dict):
        return "", False, "detail malformed"
    task = d.get("task") if isinstance(d.get("task"), dict) else None
    if not isinstance(task, dict):
        task = d if d.get("id") else None
    if not isinstance(task, dict):
        return "", False, "detail missing task"
    for c in reversed(task.get("comments") or []):
        body = str(c.get("body") or "")
        if body.startswith("Owner: "):
            line = body.split("\n", 1)[0][len("Owner: "):].strip()
            owner = line.split()[0].rstrip(".,;:") if line else ""
            return owner, True, ""
    return "", True, ""


def _holding_evidence(
    items: List[Dict[str, object]],
    *,
    state: str,
    error: str = "",
    partial: bool = False,
) -> Dict[str, object]:
    """Desk holding read model with explicit availability state."""
    return {
        "state": state,
        "source": "desk",
        "items": items,
        "error": error,
        "partial": partial,
    }


def _holding_not_queried() -> Dict[str, object]:
    return _holding_evidence([], state="not_queried")


def _worker_holdings_evidence(
    w: Worker,
    statuses: Tuple[str, ...] = ("in_progress", "in_review"),
) -> Dict[str, object]:
    """Desk holding probe with explicit availability metadata.

    Label filters are routing hints only — each row still needs a bounded
    Owner: detail lookup before it counts as a signed hold. Returns
    ``state`` (available | empty | partial | unavailable | not_queried)
    so consumers can tell verified empty from unreachable desk or failed
    detail reads.

    *statuses* defaults to both live and parked holds (personnel drawer). The
    scene bay teaser passes ``("in_progress",)`` only so one desk round-trip
    stays inside the Map latency budget.
    """
    product = ""
    label = ""
    if w.queue_url:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(w.queue_url).query)
            product = (qs.get("product") or qs.get("project") or [""])[0]
            label = (qs.get("label") or [""])[0]
        except Exception:
            product = ""
            label = ""
    if not product or product == "all":
        product = os.path.basename(os.path.abspath(w.workdir or "")).lower()
    if not product:
        return _holding_evidence([], state="empty", error="no product lane")
    aliases = set(_worker_identity_aliases(w))
    held: List[Dict[str, object]] = []
    seen: set = set()
    owner_lookups = 0
    owner_cap_hit = False
    _owner_cap = 5  # hard bound on Owner: detail GETs per holdings probe
    fetch_errors: List[str] = []
    detail_errors: List[str] = []
    any_ok = False
    for status in statuses:
        if label:
            path = (
                "/api/admin/tasks?product=%s&status=%s&label=%s&limit=10"
                % (urllib.parse.quote(product), status,
                   urllib.parse.quote(label)))
        else:
            path = (
                "/api/admin/tasks?product=%s&status=%s&limit=40"
                % (urllib.parse.quote(product), status))
        d = _desk_json(path, timeout=_BOARD_DESK_TIMEOUT_SECS)
        if d is None:
            fetch_errors.append("%s: desk unreachable" % status)
            continue
        if not isinstance(d, dict):
            fetch_errors.append("%s: malformed response" % status)
            continue
        tasks = d.get("tasks")
        if not isinstance(tasks, list):
            fetch_errors.append("%s: missing tasks list" % status)
            continue
        any_ok = True
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            if not tid or tid in seen:
                continue
            if owner_lookups >= _owner_cap:
                owner_cap_hit = True
                break
            owner, ok, detail_err = _desk_owner_of(tid, product)
            owner_lookups += 1
            if not ok:
                detail_errors.append("%s: %s" % (tid, detail_err))
                continue
            tok = (owner.strip().split()[0].rstrip(".,;:").lower()
                   if owner else "")
            if tok not in aliases:
                continue
            seen.add(tid)
            held.append({
                "id": tid,
                "title": str(t.get("title") or ""),
                "status": str(t.get("status") or status),
                "priority": t.get("priority"),
                "product": product,
                "owner": owner,
                "owner_verified": bool(owner),
                "updated_at": str(t.get("updated_at") or ""),
                "href": "%s/admin/desk?open=%s" % (
                    _desk().rstrip("/"), urllib.parse.quote(tid)),
            })
        if owner_cap_hit:
            break
    err = "; ".join(fetch_errors + detail_errors)
    if not any_ok:
        return _holding_evidence(
            [], state="unavailable", error=err or "desk unreachable")
    if detail_errors and not held:
        return _holding_evidence(
            [], state="partial", error=err, partial=True)
    if owner_cap_hit or fetch_errors or detail_errors:
        return _holding_evidence(
            held, state="partial", error=err, partial=True)
    if not held:
        return _holding_evidence([], state="empty")
    return _holding_evidence(held, state="available")


def _worker_holdings(
    w: Worker,
    statuses: Tuple[str, ...] = ("in_progress", "in_review"),
) -> List[Dict[str, object]]:
    """Compatibility list wrapper over :func:`_worker_holdings_evidence`."""
    ev = _worker_holdings_evidence(w, statuses=statuses)
    return list(ev.get("items") or [])


def _worker_ready_teaser(w: Worker, *, limit: int = 10) -> List[Dict[str, object]]:
    """Top of this worker's ready queue (for emptying queues)."""
    product = ""
    label = ""
    if w.queue_url:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(w.queue_url).query)
            product = (qs.get("product") or qs.get("project") or [""])[0]
            label = (qs.get("label") or [""])[0]
        except Exception:
            product = ""
    if not product or product == "all":
        product = os.path.basename(os.path.abspath(w.workdir or "")).lower()
    if not product:
        return []
    q = {"product": product}
    if label:
        q["label"] = label
    d = _desk_json("/api/admin/tasks/ready?" + urllib.parse.urlencode(q))
    tasks = (d or {}).get("tasks") if isinstance(d, dict) else None
    if not isinstance(tasks, list):
        tasks = []
    out: List[Dict[str, object]] = []
    for t in tasks[:limit]:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        if not tid:
            continue
        out.append({
            "id": tid,
            "title": str(t.get("title") or ""),
            "status": str(t.get("status") or "backlog"),
            "priority": t.get("priority"),
            "product": product,
            "href": "%s/admin/desk?open=%s" % (
                _desk().rstrip("/"), urllib.parse.quote(tid)),
        })
    return out


def _worker_flags(w: Worker) -> List[Dict[str, object]]:
    """Open tickets in this worker's lane not yet in active hands (backlog + parked).

    Governance layer: surfaces blocked or waiting work so the health badge
    isn't the only signal on the personnel card.  Empty list when queue_url
    is absent or DESK unreachable — always safe to skip rendering.
    """
    product = ""
    label = ""
    if w.queue_url:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(w.queue_url).query)
            product = (qs.get("product") or qs.get("project") or [""])[0]
            label = (qs.get("label") or [""])[0]
        except Exception:
            product = ""
    if not product or product == "all":
        product = os.path.basename(os.path.abspath(w.workdir or "")).lower()
    if not product or not label:
        return []
    flags: List[Dict[str, object]] = []
    seen: set = set()
    for status in ("backlog", "parked"):
        d = _desk_json(
            "/api/admin/tasks?product=%s&label=%s&status=%s&limit=30"
            % (urllib.parse.quote(product), urllib.parse.quote(label), status))
        for t in (d or {}).get("tasks") or []:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            task_labels = [str(lbl) for lbl in (t.get("labels") or [])]
            founder_gated = any("needs:founder" in lbl for lbl in task_labels)
            flags.append({
                "id": tid,
                "title": str(t.get("title") or ""),
                "status": status,
                "priority": t.get("priority"),
                "labels": task_labels,
                "founder_gated": founder_gated,
                "href": "%s/admin/desk?open=%s" % (
                    _desk().rstrip("/"), urllib.parse.quote(tid)),
            })
    return flags

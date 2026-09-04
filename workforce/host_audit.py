"""Host-mutation ghost-audit — mid-shift tier-2 residual.

Read-model over desk in_progress (and in_review) claims: scan title,
description, and comment bodies for the same tier-2 patterns used by
dispatch argv deny (engine.tier2_mutation_hit). Optional opt-in
``--include-run-logs`` tails ``local/run/<worker>.out`` (bounded, read-only).
Default is dry-run receipt; ``--live`` posts Blocked: when desk_writes_allowed().

Host-neutral: desk URL from env / flag; product from CLI; local_root from
CLI / env. Never writes local/ roster or run files, never invokes
launchctl. OS seatbelt (option B) is out of scope — see
workers/salem/designs/wf-160.md.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import List, Optional, Tuple

from ._utils import desk_base_url
from .capacity import hermetic_dry_run
from .engine import tier2_mutation_hit, _http_json, _fetch_task, _list_tasks

# Policy §B / design v1: open human gate whose title carries this phrase.
_FOUNDER_HOST_RE = re.compile(r"FOUNDER\s*·\s*host", re.IGNORECASE)

# Idempotent re-run: auditor's own Blocked body quotes patterns — skip noise.
_ALREADY_BLOCKED_RE = re.compile(
    r"Blocked:\s*Host-mutation ghost-audit",
    re.IGNORECASE,
)

# Seat label on claims (routing). Same form as marshal_release / daemon.
_WORKER_LABEL_RE = re.compile(r"^worker:(.+)$", re.IGNORECASE)

# Safe worker slug for path join — no separators, traversal, or empty.
_SAFE_WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_COMMENT_CAP = 40
_HTTP_TIMEOUT = 12.0
# Bounded tail of local/run/<worker>.out (design §2 — last N KB only).
_RUN_TAIL_BYTES = 64 * 1024

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


def safe_worker_name(name: str) -> Optional[str]:
    """Return a path-safe worker slug, or None if empty / traversal-shaped."""
    s = (name or "").strip()
    if not s or len(s) > 128:
        return None
    if s in (".", "..") or "/" in s or "\\" in s or "\x00" in s:
        return None
    if not _SAFE_WORKER_NAME_RE.match(s):
        return None
    return s


def worker_name_from_claim(
    task: dict,
    *,
    forced_worker: str = "",
) -> Optional[str]:
    """Resolve seat worker for optional run-log path.

    Order: explicit ``forced_worker`` (CLI ``--worker``) → first
    ``worker:<id>`` label on the claim. No Owner-marker fallback — Owner
    can be citizen / system and must not point the scanner at another
    seat's run file.
    """
    if forced_worker:
        return safe_worker_name(forced_worker)
    for raw in task.get("labels") or []:
        m = _WORKER_LABEL_RE.match(str(raw or "").strip())
        if m:
            return safe_worker_name(m.group(1))
    return None


def resolve_run_log_path(local_root: str, worker_name: str) -> Optional[str]:
    """Absolute path to ``local/run/<worker>.out`` if safely under run/.

    Read-only contract: returns None on bad names, missing local_root, or
    any path that would escape ``<local_root>/run`` after realpath.
    Does not create directories or open the file.
    """
    slug = safe_worker_name(worker_name)
    if not slug:
        return None
    root = (local_root or "").strip()
    if not root:
        return None
    run_dir = os.path.realpath(os.path.join(os.path.abspath(root), "run"))
    candidate = os.path.realpath(os.path.join(run_dir, "%s.out" % slug))
    # Must stay inside run_dir (prefix + separator, or exact — never parent).
    if candidate != run_dir and not candidate.startswith(run_dir + os.sep):
        return None
    if not candidate.endswith(".out"):
        return None
    if os.path.basename(candidate) != "%s.out" % slug:
        return None
    return candidate


def read_run_log_tail(
    local_root: str,
    worker_name: str,
    *,
    max_bytes: int = _RUN_TAIL_BYTES,
) -> Optional[str]:
    """Read last *max_bytes* of the seat run file, or None if absent/unreadable.

    Never writes. Missing file, bad name, and OSError all degrade to None
    (caller treats as no run surface — desk scan still applies).
    """
    path = resolve_run_log_path(local_root, worker_name)
    if path is None:
        return None
    if max_bytes <= 0:
        return None
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - int(max_bytes))
            fh.seek(start)
            data = fh.read(int(max_bytes))
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    if start > 0:
        # Drop partial first line when we mid-file started the tail.
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    return text


def scan_claim_surfaces(
    title: str,
    description: str,
    comments: Optional[List[dict]] = None,
    *,
    comment_cap: int = _COMMENT_CAP,
    run_log_text: Optional[str] = None,
    run_worker: str = "",
) -> Optional[Tuple[str, str]]:
    """Return (pattern, surface) for first tier-2 hit, or None if clear.

    *surface* is ``title``, ``description``, ``comment:<id|n>``, or
    ``run:<worker>`` when *run_log_text* is provided (priority after desk).
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

    # Optional run trail (design §2 surface 2) — only when caller opted in
    # and supplied a bounded tail. Desk hits always win over run.
    if run_log_text:
        hit = tier2_mutation_hit(run_log_text)
        if hit:
            slug = safe_worker_name(run_worker) or "unknown"
            return hit, "run:%s" % slug
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
    candidates = _list_tasks(
        desk, product, gate_type="human", limit=100,
        timeout=_HTTP_TIMEOUT, http=_http_json,
    )
    if not candidates:
        for st in ("backlog", "in_progress", "in_review"):
            candidates.extend(_list_tasks(
                desk, product, status=st, limit=50,
                timeout=_HTTP_TIMEOUT, http=_http_json,
            ))
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
        for t in _list_tasks(
            desk, product, status=st, label=label, limit=100,
            timeout=_HTTP_TIMEOUT, http=_http_json,
        ):
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
    run_log_text: Optional[str] = None,
    run_worker: str = "",
) -> dict:
    """Pure evaluation of one claim dict (+ optional full comments / run tail).

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

    hit = scan_claim_surfaces(
        title,
        description,
        comments,
        run_log_text=run_log_text,
        run_worker=run_worker,
    )
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
    include_run_logs: bool = False,
    local_root: str = "",
) -> dict:
    """Scan open claims on *product*; dry-run or live Blocked: on ungated hits.

    *ledger_append* optional callable(event, **kw) for HOST_MUTATION_DENY
    when live-blocking (caller may pass a Ledger.append bound method).

    *include_run_logs* opt-in: when True and *local_root* is set, also scan
    a bounded tail of ``local/run/<worker>.out`` for each claim with a
    resolvable seat. Default False keeps desk-only behavior. Never writes
    under *local_root*.

    Exit semantics for CLI: receipt[\"ungated_hits\"] > 0 → non-zero.
    """
    desk = (desk or desk_base_url()).rstrip("/")
    dry_run, hermetic_block = hermetic_dry_run(dry_run)

    summary: dict = {
        "ok": True,
        "product": product,
        "desk": desk,
        "dry_run": dry_run,
        "hermetic": bool(hermetic_block),
        "worker": worker or None,
        "include_run_logs": bool(include_run_logs),
        "claims_scanned": 0,
        "clear": 0,
        "gated_reports": 0,
        "would_block": 0,
        "blocked": 0,
        "already_blocked": 0,
        "ungated_hits": 0,
        "run_logs_scanned": 0,
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
                full = _fetch_task(desk, product, tid, timeout=_HTTP_TIMEOUT)
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

        run_text: Optional[str] = None
        run_worker = ""
        if include_run_logs and local_root:
            seat = worker_name_from_claim(t, forced_worker=worker)
            if seat:
                run_worker = seat
                run_text = read_run_log_tail(local_root, seat)
                if run_text is not None:
                    summary["run_logs_scanned"] += 1

        ev = evaluate_claim(
            t,
            gated=gated,
            comments=comments,
            run_log_text=run_text,
            run_worker=run_worker,
        )
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
    extra_flags = ""
    if summary.get("hermetic"):
        extra_flags += " hermetic=1"
    if summary.get("include_run_logs"):
        extra_flags += " run_logs=%d" % (summary.get("run_logs_scanned") or 0)
    lines = [
        "host-audit: product=%s dry_run=%s scanned=%d ungated_hits=%d%s"
        % (
            summary.get("product"),
            int(bool(summary.get("dry_run"))),
            summary.get("claims_scanned") or 0,
            summary.get("ungated_hits") or 0,
            extra_flags,
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

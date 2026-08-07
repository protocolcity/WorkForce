"""Unlanded shift-branch scan — board-green vs code-on-main drift.

PROCESS §5.1.3 requires hands to land commits on origin/main before close.
Shift isolation keeps work on ``workforce/shift/<id>`` until the
hand pushes. Engine post-shift FF-merges into the primary checkout only —
it never pushes. When a hand closes without landing, the board can read
done while live code stays pre-merge.

This module is **report-only** health surface for ``workforce doctor``
(and the efficiency job's mechanical pulse). It does not push, merge, or
rewrite history. Close-out mechanical gates (desk rejects) are other-repo
work (WorkLane / PROCESS).

Host-neutral: git cwd = roster ``workdir``; landing tip prefers
``origin/main`` then local ``main``/``master``. No hard-coded host paths.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .engine import (
    _SHIFT_BRANCH_PREFIX,
    _git,
    _is_git_workdir,
    _shift_branch_name,
    _shift_worktree_path,
)
from .roster import Roster, Worker

# Preferred landing targets in order (remote first — matches PROCESS §5.1.3).
_LANDING_REF_CANDIDATES = (
    "refs/remotes/origin/main",
    "origin/main",
    "refs/heads/main",
    "main",
    "refs/heads/master",
    "master",
)


def resolve_landing_ref(cwd: str) -> Optional[str]:
    """Return the best local ref name for 'main on origin', or None.

    Pure git probe. Prefers ``origin/main`` so a primary that FF'd from
    the shift but never pushed still counts as unlanded.
    """
    if not cwd or not _is_git_workdir(cwd):
        return None
    for ref in _LANDING_REF_CANDIDATES:
        probe = _git(cwd, "rev-parse", "--verify", ref)
        if probe.returncode == 0 and (probe.stdout or "").strip():
            return ref
    return None


def commits_ahead(cwd: str, tip: str, base: str) -> Optional[int]:
    """Count commits reachable from *tip* but not *base* (``base..tip``).

    Returns None when either rev is missing or git fails (caller treats
    as "cannot measure", not zero).
    """
    if not cwd or not tip or not base:
        return None
    for rev in (tip, base):
        chk = _git(cwd, "rev-parse", "--verify", rev)
        if chk.returncode != 0:
            return None
    counted = _git(cwd, "rev-list", "--count", "%s..%s" % (base, tip))
    if counted.returncode != 0:
        return None
    try:
        return int((counted.stdout or "").strip() or "0")
    except ValueError:
        return None


def _ref_exists(cwd: str, ref: str) -> bool:
    if not cwd or not ref:
        return False
    probe = _git(cwd, "rev-parse", "--verify", ref)
    return probe.returncode == 0 and bool((probe.stdout or "").strip())


def _tip_for_worker(worker: Worker, local_root: str, cwd: str) -> Optional[str]:
    """Best tip ref that may hold unlanded hand work for *worker*.

    Prefer the engine shift branch name when it exists; else the linked
    worktree HEAD if that path is a git worktree; else None (nothing to
    compare beyond primary — primary-vs-origin is a separate row when the
    worker uses shift isolation and primary itself is ahead).
    """
    name = (worker.name or "").strip()
    if not name:
        return None
    branch = _shift_branch_name(name)
    # Local branch ref first (worktree keeps it checked out).
    for candidate in (
        "refs/heads/" + branch,
        branch,
    ):
        if _ref_exists(cwd, candidate):
            return candidate
    # Worktree path may exist even if branch name lookup failed (orphan).
    if local_root:
        wt = _shift_worktree_path(local_root, name)
        if wt and _is_git_workdir(wt):
            head = _git(wt, "rev-parse", "HEAD")
            if head.returncode == 0 and (head.stdout or "").strip():
                return (head.stdout or "").strip()
    return None


def scan_worker(
    worker: Worker,
    local_root: str = "",
) -> Optional[Dict[str, Any]]:
    """Return an unlanded-row for *worker*, or None when clear / N/A.

    Scans only seats that opt into shift isolation (``shift_worktree``).
    Jobs and explicit opt-outs share the primary checkout and are out of
    this failure class.

    Row keys: worker, branch, landing_ref, commits_ahead, surface
    (``shift_branch`` | ``primary`` | ``both``), workdir.
    """
    if not getattr(worker, "shift_worktree", False):
        return None
    wd = (getattr(worker, "workdir", None) or "").strip()
    if not wd or not _is_git_workdir(wd):
        return None

    landing = resolve_landing_ref(wd)
    if not landing:
        return None  # no main tip to compare — cannot measure

    name = (worker.name or "").strip() or "?"
    branch = _shift_branch_name(name)
    tip = _tip_for_worker(worker, local_root, wd)

    shift_ahead = 0
    if tip:
        n = commits_ahead(wd, tip, landing)
        if n is None:
            # Tip may live only in the worktree; try counting from there.
            wt = _shift_worktree_path(local_root, name) if local_root else ""
            if wt and _is_git_workdir(wt):
                n = commits_ahead(wt, "HEAD", landing)
        shift_ahead = int(n or 0)

    primary_ahead = commits_ahead(wd, "HEAD", landing)
    if primary_ahead is None:
        primary_ahead = 0

    if shift_ahead <= 0 and primary_ahead <= 0:
        return None

    if shift_ahead > 0 and primary_ahead > 0:
        surface = "both"
        ahead = max(shift_ahead, primary_ahead)
    elif shift_ahead > 0:
        surface = "shift_branch"
        ahead = shift_ahead
    else:
        surface = "primary"
        ahead = primary_ahead

    return {
        "worker": name,
        "branch": branch if tip else "",
        "landing_ref": landing,
        "commits_ahead": ahead,
        "shift_ahead": shift_ahead,
        "primary_ahead": primary_ahead,
        "surface": surface,
        "workdir": wd,
    }


def scan_unlanded(
    roster: Roster,
    local_root: str = "",
    *,
    workers: Optional[Sequence[Worker]] = None,
) -> List[Dict[str, Any]]:
    """Scan roster (or an explicit worker list) for unlanded shift commits.

    Stable order by worker name. Pure relative to git state — no desk I/O.
    """
    rows: List[Dict[str, Any]] = []
    if workers is not None:
        seq = list(workers)
    else:
        seq = [roster.workers[k] for k in sorted(roster.workers)]
    for w in seq:
        row = scan_worker(w, local_root)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: r.get("worker") or "")
    return rows


def format_report(rows: Sequence[Dict[str, Any]]) -> str:
    """Human-readable doctor section for an unlanded scan."""
    lines: List[str] = []
    if not rows:
        lines.append(
            "Unlanded shift commits: clean — no shift seats "
            "ahead of landing ref"
        )
        return "\n".join(lines)

    total = sum(int(r.get("commits_ahead") or 0) for r in rows)
    lines.append(
        "Unlanded shift commits: %d seat(s) · %d commit(s) not "
        "on landing ref — PROCESS §5.1.3 land-on-main gap"
        % (len(rows), total)
    )
    for r in rows:
        lines.append(
            "  %s: %d ahead of %s (%s)%s"
            % (
                r.get("worker") or "?",
                int(r.get("commits_ahead") or 0),
                r.get("landing_ref") or "?",
                r.get("surface") or "?",
                (" via " + r["branch"]) if r.get("branch") else "",
            )
        )
    lines.append(
        "  next: from shift tree, `git push origin HEAD:main` when "
        "FF-able (never force); do not close until landing SHA is in Links"
    )
    return "\n".join(lines)


# Re-export branch prefix for tests / callers that mirror engine naming.
SHIFT_BRANCH_PREFIX = _SHIFT_BRANCH_PREFIX

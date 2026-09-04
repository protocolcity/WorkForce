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

Metric:
- Counts **unique patches** (``git cherry`` ``+``), not raw ancestry —
  re-landed content under a new SHA does not inflate the rollup.
- **Seat-local** unlanded = unique patches on the shift tip that are not
  already on primary ``HEAD`` (work the hand still holds off-primary).
- **Primary** unlanded = unique patches on primary ``HEAD`` not on the
  landing ref — attributed **once per workdir** so multi-seat
  neighborhoods do not multiply the same unpushed HEAD.

Divergence:
- A tip that is **not an ancestor of landing and landing is not an
  ancestor of the tip** cannot ``git push origin HEAD:main`` (non-fast-
  forward). Doctor labels those seats ``DIVERGED`` and advises a union
  merge, not an FF push. Still report-only — never
  merge or push. Cherry-equivalent patches on a split history still
  count as diverged (FF is about ancestry, not patch identity).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set

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
    as "cannot measure", not zero). Ancestry-only — prefer
    :func:`patches_ahead` for land-on-main health.
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


def patches_ahead(cwd: str, tip: str, base: str) -> Optional[int]:
    """Count tip commits whose patches are not already on *base*.

    Uses ``git cherry base tip``: ``+`` = unique patch (real unlanded work),
    ``-`` = equivalent patch already on base (false-positive ancestry —
    re-landed / cherry-picked / rebased onto main under a new SHA).

    Returns None when either rev is missing or cherry fails (caller may
    fall back to :func:`commits_ahead`).
    """
    if not cwd or not tip or not base:
        return None
    for rev in (tip, base):
        chk = _git(cwd, "rev-parse", "--verify", rev)
        if chk.returncode != 0:
            return None
    cherry = _git(cwd, "cherry", base, tip)
    if cherry.returncode != 0:
        return None
    unique = 0
    for line in (cherry.stdout or "").splitlines():
        s = line.strip()
        if s.startswith("+"):
            unique += 1
    return unique


def _ahead(cwd: str, tip: str, base: str) -> Optional[int]:
    """Preferred unlanded measure: unique patches, then ancestry count."""
    n = patches_ahead(cwd, tip, base)
    if n is not None:
        return n
    return commits_ahead(cwd, tip, base)


def _ref_exists(cwd: str, ref: str) -> bool:
    if not cwd or not ref:
        return False
    probe = _git(cwd, "rev-parse", "--verify", ref)
    return probe.returncode == 0 and bool((probe.stdout or "").strip())


def _rev_sha(cwd: str, rev: str) -> Optional[str]:
    if not cwd or not rev:
        return None
    p = _git(cwd, "rev-parse", "--verify", rev)
    if p.returncode != 0:
        return None
    sha = (p.stdout or "").strip()
    return sha or None


def _is_ancestor(cwd: str, maybe_anc: str, rev: str) -> bool:
    """True when *maybe_anc* is an ancestor of *rev* (or equal)."""
    if not cwd or not maybe_anc or not rev:
        return False
    p = _git(cwd, "merge-base", "--is-ancestor", maybe_anc, rev)
    return p.returncode == 0


def is_diverged(cwd: str, a: str, b: str) -> bool:
    """True when *a* and *b* exist, differ, and neither is an ancestor.

    That is the non-fast-forward class: ``git push origin HEAD:main``
    (and ``git merge --ff-only``) reject. Unique-patch count can be zero
    (cherry-equivalent content, split history) and this is still True.
    """
    sa = _rev_sha(cwd, a)
    sb = _rev_sha(cwd, b)
    if not sa or not sb or sa == sb:
        return False
    return (not _is_ancestor(cwd, a, b)) and (not _is_ancestor(cwd, b, a))


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

    Ahead counts are **unique patches**, split as:

    - ``shift_ahead`` — patches on the shift tip not on primary HEAD
      (seat-local work still off the primary checkout). Zero when the tip
      equals or is an ancestor of primary HEAD, or when every tip patch is
      already on the landing ref.
    - ``primary_ahead`` — patches on primary HEAD not on the landing ref.
      Callers (:func:`scan_unlanded`) attribute this once per workdir.

    Row keys: worker, branch, landing_ref, commits_ahead, surface
    (``shift_branch`` | ``primary`` | ``both``), workdir, shift_ahead,
    primary_ahead, diverged, landing_ahead.
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

    primary_ahead = _ahead(wd, "HEAD", landing)
    if primary_ahead is None:
        primary_ahead = 0

    shift_ahead = 0
    if tip:
        tip_sha = _rev_sha(wd, tip)
        head_sha = _rev_sha(wd, "HEAD")
        # Seat-local only: patches on tip that primary does not already have.
        # If tip == HEAD or tip ⊆ HEAD, seat work lives in primary and is
        # counted once via primary_ahead (shared-workdir safe).
        if tip_sha and head_sha and tip_sha == head_sha:
            shift_ahead = 0
        elif tip_sha and _is_ancestor(wd, tip, "HEAD"):
            shift_ahead = 0
        else:
            # Unique vs primary (seat still holds off-primary work).
            n_vs_primary = _ahead(wd, tip, "HEAD")
            if n_vs_primary is None and local_root and head_sha:
                wt = _shift_worktree_path(local_root, name)
                if wt and _is_git_workdir(wt):
                    # Count worktree HEAD vs primary SHA (same object db).
                    n_vs_primary = _ahead(wt, "HEAD", head_sha)
            n_vs_primary = int(n_vs_primary or 0)
            # Drop false positives already equivalent on landing.
            n_vs_land = _ahead(wd, tip, landing)
            if n_vs_land is None and local_root:
                wt = _shift_worktree_path(local_root, name)
                if wt and _is_git_workdir(wt):
                    n_vs_land = _ahead(wt, "HEAD", landing)
            n_vs_land = int(n_vs_land or 0)
            if n_vs_land <= 0:
                shift_ahead = 0
            else:
                # Seat-local unlanded cannot exceed either bound.
                shift_ahead = min(n_vs_primary, n_vs_land)

    # wf-226: FF-ability is ancestry, not cherry. A split history cannot
    # `git push origin HEAD:main` even when every patch is already on landing.
    compare = tip if tip else "HEAD"
    diverged = is_diverged(wd, compare, landing)
    landing_ahead = 0
    if diverged:
        n_land = _ahead(wd, landing, compare)
        landing_ahead = int(n_land or 0)

    if shift_ahead <= 0 and primary_ahead <= 0 and not diverged:
        return None

    if shift_ahead > 0 and primary_ahead > 0:
        surface = "both"
        ahead = shift_ahead + primary_ahead
    elif shift_ahead > 0:
        surface = "shift_branch"
        ahead = shift_ahead
    elif primary_ahead > 0:
        surface = "primary"
        ahead = primary_ahead
    else:
        # Diverged with no unique patches — still a land-on-main block.
        surface = "shift_branch" if tip else "primary"
        ahead = 0

    return {
        "worker": name,
        "branch": branch if tip else "",
        "landing_ref": landing,
        "commits_ahead": ahead,
        "shift_ahead": shift_ahead,
        "primary_ahead": primary_ahead,
        "diverged": diverged,
        "landing_ahead": landing_ahead,
        "surface": surface,
        "workdir": wd,
    }


def _dedupe_primary_by_workdir(
    rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attribute primary-checkout drift once per workdir.

    Multi-seat neighborhoods share one primary HEAD. Counting
    ``primary_ahead`` on every seat multiplies the same unpushed commits
    in the doctor rollup (e.g. 4 × 146 on oneseo-pos). First seat in
    stable name order keeps the primary contribution; later seats keep
    only their shift-branch unique patches.
    """
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        wd = (row.get("workdir") or "").strip()
        shift_n = int(row.get("shift_ahead") or 0)
        prim_n = int(row.get("primary_ahead") or 0)
        if prim_n > 0 and wd and wd in seen:
            if shift_n <= 0:
                continue  # pure primary duplicate of an earlier seat
            row = dict(row)
            row["primary_ahead"] = 0
            row["surface"] = "shift_branch"
            row["commits_ahead"] = shift_n
            out.append(row)
            continue
        if prim_n > 0 and wd:
            seen.add(wd)
            # Recompute commits_ahead after any prior mutation path.
            if shift_n > 0:
                row = dict(row)
                row["surface"] = "both"
                row["commits_ahead"] = shift_n + prim_n
        out.append(row)
    return out


def scan_unlanded(
    roster: Roster,
    local_root: str = "",
    *,
    workers: Optional[Sequence[Worker]] = None,
) -> List[Dict[str, Any]]:
    """Scan roster (or an explicit worker list) for unlanded shift commits.

    Stable order by worker name. Pure relative to git state — no desk I/O.
    Primary-ahead is deduped per workdir.
    """
    rows: List[Dict[str, Any]] = []
    if workers is not None:
        seq = list(workers)
    else:
        seq = [roster.workers[k] for k in sorted(roster.workers)]
    # Stable name order so primary attribution is deterministic.
    seq = sorted(seq, key=lambda w: (getattr(w, "name", None) or ""))
    for w in seq:
        row = scan_worker(w, local_root)
        if row:
            rows.append(row)
    rows = _dedupe_primary_by_workdir(rows)
    rows.sort(key=lambda r: r.get("worker") or "")
    return rows


def _is_remote_landing_ref(landing_ref: str) -> bool:
    """True when *landing_ref* is a remote-tracking ref (``origin/main``…).

    :func:`resolve_landing_ref` falls back to a local ``refs/heads/main`` /
    ``master`` when the workdir has no ``origin`` (e.g. oneseo-pos, recipes
    — HOST_REGISTRY.md "local-only"). Advisory text must key off which one
    it actually found, not assume remote.
    """
    ref = (landing_ref or "").strip()
    return ref.startswith("refs/remotes/") or ref.split("/", 1)[0] == "origin"


def _next_step_for(landing_ref: str) -> str:
    if _is_remote_landing_ref(landing_ref):
        return (
            "from shift tree, `git push origin HEAD:main` when FF-able "
            "(never force); do not close until landing SHA is in Links"
        )
    return (
        "no `origin` remote here — from the **primary checkout** (not the "
        "shift tree), `git merge --ff-only workforce/shift/<slug>` into "
        "`%s` (real `git merge` or rebase first if it's no longer "
        "FF-able); do not close until landing SHA is in Links" % (landing_ref or "main")
    )


_DIVERGED_NEXT = (
    "DIVERGED seats: merge landing into the shift tree (union, never "
    "overwrite — wf-171); `git push origin HEAD:main` / ff-only merge "
    "reject until landing is an ancestor"
)


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
    n_div = sum(1 for r in rows if r.get("diverged"))
    head = (
        "Unlanded shift commits: %d seat(s) · %d unique "
        "patch(es) not on landing ref — PROCESS §5.1.3 land-on-main gap"
        % (len(rows), total)
    )
    if n_div:
        head += " · %d DIVERGED (not FF-able)" % n_div
    lines.append(head)
    for r in rows:
        extra = ""
        if r.get("diverged"):
            land_n = int(r.get("landing_ahead") or 0)
            extra = " DIVERGED — not FF-able onto landing"
            if land_n:
                extra += " (%d unique on landing not on tip)" % land_n
        lines.append(
            "  %s: %d ahead of %s (%s)%s%s"
            % (
                r.get("worker") or "?",
                int(r.get("commits_ahead") or 0),
                r.get("landing_ref") or "?",
                r.get("surface") or "?",
                (" via " + r["branch"]) if r.get("branch") else "",
                extra,
            )
        )
    # Next-step advice depends on what resolve_landing_ref actually found
    # per seat — remote-backed and local-only workdirs need different
    # instructions, and both can appear in one workspace-wide scan.
    # Diverged seats must not be told to FF-push.
    seen_hints: List[str] = []
    any_diverged = False
    any_ffable = False
    for r in rows:
        if r.get("diverged"):
            any_diverged = True
            continue
        any_ffable = True
        hint = _next_step_for(r.get("landing_ref") or "")
        if hint not in seen_hints:
            seen_hints.append(hint)
    if any_diverged:
        lines.append("  next: " + _DIVERGED_NEXT)
    if any_ffable:
        for hint in seen_hints:
            lines.append("  next: " + hint)
    return "\n".join(lines)


# Re-export branch prefix for tests / callers that mirror engine naming.
SHIFT_BRANCH_PREFIX = _SHIFT_BRANCH_PREFIX

"""Seat branch and checkout pruning (wf-267).

After an order closes (integrator) or on demand (`workforce prune`), remove
merged remote task branches and clean seat checkouts when safe. Never deletes
unmerged work, dirty trees, open orders, or branches with an open PR.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .integrator import IntegratorError, append_ledger_row, load_config
from .routing_hygiene import is_terminal_status

_TASK_BRANCH_RE = re.compile(r"^workforce/task/([^/]+)/([^/]+)$")
_KEEP_TASK_RUN_NAMES = frozenset({"preparation.json", "attempts"})


class PruneError(RuntimeError):
    pass


def task_run_dir(config: Dict[str, Any], worker: str, task_id: str) -> str:
    checkout = checkout_path(config, worker, task_id)
    return os.path.dirname(checkout)


def _is_task_run_dir(path: str, config: Dict[str, Any]) -> bool:
    """True when *path* looks like a seat reservation folder, not a workspace root."""
    if not path or not os.path.isdir(path):
        return False
    norm = os.path.normpath(path)
    for forbidden in (config.get("workspace_root"), config.get("local_root")):
        if forbidden and norm == os.path.normpath(forbidden):
            return False
    prep = os.path.join(path, "preparation.json")
    attempts = os.path.join(path, "attempts")
    return os.path.isfile(prep) or os.path.isdir(attempts)


def checkout_path(config: Dict[str, Any], worker: str, task_id: str) -> str:
    templates = config.get("checkout_templates") or {}
    template = templates.get(worker) or config["checkout_template"]
    rel = template.format(worker=worker, task_id=task_id)
    return os.path.join(config["workspace_root"], rel)


def branch_name(config: Dict[str, Any], worker: str, task_id: str) -> str:
    return config["branch_template"].format(worker=worker, task_id=task_id)


def parse_task_branch(name: str) -> Optional[Tuple[str, str]]:
    """Return ``(worker, task_id)`` from a local or remote task branch name."""
    branch = (name or "").strip()
    if branch.startswith("origin/"):
        branch = branch[len("origin/"):]
    m = _TASK_BRANCH_RE.match(branch)
    if not m:
        return None
    return m.group(1), m.group(2)


def _task_run_scan_roots(config: Dict[str, Any]) -> List[str]:
    """Directories that may contain ``<worker>/<task_id>/`` reservation folders."""
    ws = config["workspace_root"]
    template = config["checkout_template"]
    if "{worker}" not in template:
        return []
    base_rel = template.split("{worker}", 1)[0].rstrip("/")
    roots = [
        os.path.join(ws, base_rel),
        os.path.join(ws, "workforce", base_rel),
        os.path.join(config["local_root"], "task-runs"),
    ]
    seen = set()
    out: List[str] = []
    for root in roots:
        norm = os.path.normpath(root)
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.isdir(norm):
            out.append(norm)
    return out


def discover_local_reservations(config: Dict[str, Any]) -> List[Dict[str, str]]:
    """Seat task-run folders that still exist under configured scan roots."""
    found: Dict[Tuple[str, str], Dict[str, str]] = {}
    for root in _task_run_scan_roots(config):
        try:
            workers = os.listdir(root)
        except OSError:
            continue
        for worker in workers:
            worker_dir = os.path.join(root, worker)
            if not os.path.isdir(worker_dir):
                continue
            try:
                task_ids = os.listdir(worker_dir)
            except OSError:
                continue
            for task_id in task_ids:
                reservation = os.path.join(worker_dir, task_id)
                if not os.path.isdir(reservation):
                    continue
                key = (worker, task_id)
                if key in found:
                    continue
                found[key] = {
                    "worker": worker,
                    "task_id": task_id,
                    "task_run_dir": reservation,
                    "checkout": checkout_path(config, worker, task_id),
                    "branch": branch_name(config, worker, task_id),
                }
    return sorted(found.values(), key=lambda row: (row["worker"], row["task_id"]))


def _run(argv: Sequence[str], cwd: Optional[str] = None) -> Dict[str, Any]:
    proc = subprocess.run(
        list(argv), cwd=cwd, capture_output=True, text=True,
    )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    return {
        "rc": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "output": stdout + stderr,
    }


def default_prune_ops(config: Dict[str, Any]) -> Dict[str, Callable]:
    """Real git/gh implementations for pruning, from *config* alone."""
    main_checkout = config["main_checkout"]
    pr_base = config["pr_base"]

    def checkout_clean(checkout: str) -> bool:
        if not checkout or not os.path.isdir(checkout):
            return True
        r = _run(["git", "status", "--porcelain"], cwd=checkout)
        return r["rc"] == 0 and not r["output"].strip()

    def head_ancestor_of_base(checkout: str) -> bool:
        if not checkout or not os.path.isdir(checkout):
            return False
        r = _run(
            ["git", "merge-base", "--is-ancestor", "HEAD", "origin/%s" % pr_base],
            cwd=checkout,
        )
        return r["rc"] == 0

    def remote_branch_exists(branch: str) -> bool:
        _run(["git", "fetch", "origin", branch], cwd=main_checkout)
        r = _run(["git", "rev-parse", "origin/%s" % branch], cwd=main_checkout)
        return r["rc"] == 0

    def branch_merged_into_base(branch: str) -> bool:
        _run(["git", "fetch", "origin", branch, pr_base], cwd=main_checkout)
        r = _run(
            ["git", "merge-base", "--is-ancestor", "origin/%s" % branch, "origin/%s" % pr_base],
            cwd=main_checkout,
        )
        return r["rc"] == 0

    def pr_state(branch: str) -> str:
        view = _run(["gh", "pr", "view", branch, "--json", "state"], cwd=main_checkout)
        if view["rc"] != 0:
            return "NONE"
        try:
            import json
            data = json.loads(view["stdout"].strip())
        except (ValueError, TypeError):
            return "NONE"
        return str(data.get("state") or "NONE").upper()

    def delete_remote_branch(branch: str) -> Dict[str, Any]:
        return _run(["git", "push", "origin", "--delete", branch], cwd=main_checkout)

    def remove_worktree(checkout: str) -> Dict[str, Any]:
        if not checkout or not os.path.isdir(checkout):
            return {"rc": 0, "output": ""}
        return _run(["git", "worktree", "remove", checkout], cwd=main_checkout)

    def list_remote_task_branches() -> List[str]:
        r = _run(["git", "branch", "-r", "--format=%(refname:short)"], cwd=main_checkout)
        if r["rc"] != 0:
            return []
        out = []
        for line in r["output"].splitlines():
            name = line.strip()
            if name.startswith("origin/") and parse_task_branch(name):
                out.append(name[len("origin/"):])
        return sorted(set(out))

    def post_note(task_id: str, body: str) -> Dict[str, Any]:
        from .capacity import hermetic_dry_run
        from ._utils import desk_base_url
        from .capacity import _req
        import urllib.parse

        dry, hermetic = hermetic_dry_run(False)
        if dry:
            return {"ok": True, "dry_run": True, "hermetic": hermetic, "body": body}
        desk = (config.get("desk") or desk_base_url()).rstrip("/")
        q = urllib.parse.urlencode({"product": config["project"]})
        url = "%s/api/admin/tasks/%s/comments?%s" % (
            desk, urllib.parse.quote(task_id, safe=""), q,
        )
        return _req("POST", url, {"body": body, "author": "workforce"})

    def fetch_task_status(task_id: str) -> str:
        from .engine import _fetch_task
        from ._utils import desk_base_url

        desk = (config.get("desk") or desk_base_url()).rstrip("/")
        task = _fetch_task(desk, config["project"], task_id)
        if not isinstance(task, dict):
            return ""
        return str(task.get("status") or "")

    return {
        "checkout_clean": checkout_clean,
        "head_ancestor_of_base": head_ancestor_of_base,
        "remote_branch_exists": remote_branch_exists,
        "branch_merged_into_base": branch_merged_into_base,
        "pr_state": pr_state,
        "delete_remote_branch": delete_remote_branch,
        "remove_worktree": remove_worktree,
        "list_remote_task_branches": list_remote_task_branches,
        "post_note": post_note,
        "fetch_task_status": fetch_task_status,
    }


def evaluate_prune(
    config: Dict[str, Any],
    worker: str,
    task_id: str,
    ops: Dict[str, Callable],
    *,
    task_status: Optional[str] = None,
    assume_pr_merged: bool = False,
) -> Dict[str, Any]:
    """Pure policy: what would be removed and why anything is kept."""
    checkout = checkout_path(config, worker, task_id)
    branch = branch_name(config, worker, task_id)
    result: Dict[str, Any] = {
        "worker": worker,
        "task_id": task_id,
        "checkout": checkout,
        "branch": branch,
        "actions": [],
        "kept": [],
        "notes": [],
    }

    if not (task_status or "").strip():
        result["kept"].append({"kind": "all", "reason": "status_unknown"})
        return result

    if not is_terminal_status(task_status):
        result["kept"].append({"kind": "all", "reason": "open_order"})
        return result

    pr_state_val = ops["pr_state"](branch)
    result["pr_state"] = pr_state_val
    if pr_state_val == "OPEN":
        result["kept"].append({"kind": "branch", "reason": "open_pr"})
        result["kept"].append({"kind": "checkout", "reason": "open_pr"})
        return result

    checkout_blocked = False
    remove_checkout = False
    prune_task_run = False
    if os.path.isdir(checkout):
        if not ops["checkout_clean"](checkout):
            checkout_blocked = True
            result["kept"].append({"kind": "checkout", "reason": "dirty"})
            result["kept"].append({"kind": "task_run", "reason": "dirty"})
            result["kept"].append({"kind": "branch", "reason": "dirty"})
        elif not ops["head_ancestor_of_base"](checkout):
            checkout_blocked = True
            result["kept"].append({"kind": "checkout", "reason": "head_not_in_main"})
            result["kept"].append({"kind": "task_run", "reason": "head_not_in_main"})
            result["kept"].append({"kind": "branch", "reason": "head_not_in_main"})
            result["notes"].append(
                "Note: seat checkout kept — HEAD is not on %s; branch left for rescue."
                % config["pr_base"]
            )
        else:
            remove_checkout = True
            prune_task_run = True
    else:
        reservation = task_run_dir(config, worker, task_id)
        if _is_task_run_dir(reservation, config):
            prune_task_run = True

    delete_branch = False
    if not checkout_blocked:
        if assume_pr_merged or pr_state_val == "MERGED":
            delete_branch = True
        elif pr_state_val in ("NONE", "CLOSED") and ops["remote_branch_exists"](branch):
            if ops["branch_merged_into_base"](branch):
                delete_branch = True

    if delete_branch and ops["remote_branch_exists"](branch):
        result["actions"].append({"kind": "branch", "branch": branch})
    if remove_checkout:
        result["actions"].append({"kind": "checkout", "path": checkout})
    if prune_task_run and not any(k.get("reason") == "dirty" for k in result["kept"]):
        result["actions"].append({
            "kind": "task_run",
            "path": task_run_dir(config, worker, task_id),
        })
    return result


def _prune_task_run_contents(task_run_path: str, dry_run: bool) -> List[str]:
    """Remove everything under a reservation except preparation.json and attempts/."""
    removed: List[str] = []
    if not os.path.isdir(task_run_path):
        return removed
    for name in os.listdir(task_run_path):
        if name in _KEEP_TASK_RUN_NAMES:
            continue
        target = os.path.join(task_run_path, name)
        removed.append(target)
        if dry_run:
            continue
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            try:
                os.remove(target)
            except OSError:
                pass
    return removed


def _append_prune_ledger(
    config: Dict[str, Any], event: str, worker: str, task_id: str, **kv: Any,
) -> None:
    append_ledger_row(
        config["local_root"], config["project"], event,
        ticket=task_id, worker=worker, **kv,
    )


def apply_prune(
    config: Dict[str, Any],
    plan: Dict[str, Any],
    ops: Dict[str, Callable],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute a plan from :func:`evaluate_prune`; ledger one row per removal."""
    worker = plan["worker"]
    task_id = plan["task_id"]
    receipt: Dict[str, Any] = {
        "worker": worker,
        "task_id": task_id,
        "dry_run": dry_run,
        "removed": [],
        "kept": list(plan.get("kept") or []),
        "notes": list(plan.get("notes") or []),
    }

    for note in receipt["notes"]:
        if dry_run:
            receipt.setdefault("would_note", []).append(note)
        else:
            ops["post_note"](task_id, note)

    checkout_planned = any(a.get("kind") == "checkout" for a in plan.get("actions") or [])
    checkout_cleared = not checkout_planned

    # Task-run cleanup is gated on the checkout having been removed, so the
    # checkout action must run first whatever order the plan lists them in
    # (a plan read back from JSON or built by hand may not be ordered).
    _order = {"branch": 0, "checkout": 1, "task_run": 2}
    ordered_actions = sorted(
        plan.get("actions") or [], key=lambda a: _order.get(a.get("kind"), 99),
    )

    for action in ordered_actions:
        kind = action.get("kind")
        if kind == "branch":
            branch = action["branch"]
            if dry_run:
                receipt["removed"].append({"kind": "branch", "branch": branch, "dry_run": True})
                continue
            if not ops["remote_branch_exists"](branch):
                continue
            out = ops["delete_remote_branch"](branch)
            if out.get("rc") == 0:
                receipt["removed"].append({"kind": "branch", "branch": branch})
                _append_prune_ledger(
                    config, "PRUNE", worker, task_id, target="branch", branch=branch,
                )
        elif kind == "checkout":
            path = action["path"]
            if dry_run:
                receipt["removed"].append({"kind": "checkout", "path": path, "dry_run": True})
                continue
            if not os.path.isdir(path):
                continue
            out = ops["remove_worktree"](path)
            if out.get("rc") == 0:
                checkout_cleared = True
                receipt["removed"].append({"kind": "checkout", "path": path})
                _append_prune_ledger(
                    config, "PRUNE", worker, task_id, target="checkout", path=path,
                )
            else:
                receipt["kept"].append({"kind": "checkout", "reason": "worktree_remove_failed"})
                receipt["kept"].append({"kind": "task_run", "reason": "worktree_remove_failed"})
        elif kind == "task_run":
            if not checkout_cleared:
                continue
            path = action["path"]
            removed_paths = _prune_task_run_contents(path, dry_run=dry_run)
            if removed_paths:
                receipt["removed"].append({
                    "kind": "task_run",
                    "path": path,
                    "entries": removed_paths,
                    "dry_run": dry_run,
                })
                if not dry_run:
                    _append_prune_ledger(
                        config, "PRUNE", worker, task_id, target="task_run", path=path,
                    )
    return receipt


def prune_one(
    config: Dict[str, Any],
    worker: str,
    task_id: str,
    ops: Dict[str, Callable],
    *,
    dry_run: bool = False,
    task_status: Optional[str] = None,
    assume_pr_merged: bool = False,
) -> Dict[str, Any]:
    plan = evaluate_prune(
        config, worker, task_id, ops,
        task_status=task_status, assume_pr_merged=assume_pr_merged,
    )
    receipt = apply_prune(config, plan, ops, dry_run=dry_run)
    receipt["plan"] = plan
    return receipt


def prune_after_close(
    config: Dict[str, Any],
    worker: str,
    task_id: str,
    ops: Optional[Dict[str, Callable]] = None,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Prune one just-closed order (integrator close path)."""
    live_ops = ops if ops is not None else default_prune_ops(config)
    return prune_one(
        config, worker, task_id, live_ops,
        dry_run=dry_run, task_status="done", assume_pr_merged=True,
    )


def discover_prune_candidates(
    config: Dict[str, Any],
    ops: Dict[str, Callable],
    *,
    http: Optional[Callable[..., dict]] = None,
) -> List[Dict[str, Any]]:
    """Union of local reservations and remote task branches for one sweep."""
    from .engine import _list_tasks
    from ._utils import desk_base_url

    desk = (config.get("desk") or desk_base_url()).rstrip("/")
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for row in discover_local_reservations(config):
        key = (row["worker"], row["task_id"])
        by_key[key] = dict(row)

    for branch in ops["list_remote_task_branches"]():
        parsed = parse_task_branch(branch)
        if not parsed:
            continue
        key = parsed
        by_key.setdefault(key, {
            "worker": parsed[0],
            "task_id": parsed[1],
            "branch": branch,
        })

    for task in _list_tasks(desk, config["project"], status="done", limit=500, http=http):
        if not isinstance(task, dict):
            continue
        tid = str(task.get("id") or "").strip()
        if not tid:
            continue
        labels = task.get("labels") or []
        seat = None
        for raw in labels:
            s = str(raw or "").strip()
            if s.startswith("worker:"):
                seat = s[len("worker:"):]
                break
        if seat:
            by_key.setdefault((seat, tid), {"worker": seat, "task_id": tid})

    candidates = []
    for worker, task_id in sorted(by_key):
        row = by_key[(worker, task_id)]
        status = ops["fetch_task_status"](task_id)
        candidates.append({
            "worker": worker,
            "task_id": task_id,
            "task_status": status,
            "source": row,
        })
    return candidates


def prune_pass(
    config: Dict[str, Any],
    *,
    ops: Optional[Dict[str, Callable]] = None,
    dry_run: bool = False,
    http: Optional[Callable[..., dict]] = None,
) -> List[Dict[str, Any]]:
    """Sweep merged branches and clean checkouts for terminal orders."""
    live_ops = ops if ops is not None else default_prune_ops(config)
    results: List[Dict[str, Any]] = []
    for cand in discover_prune_candidates(config, live_ops, http=http):
        receipt = prune_one(
            config, cand["worker"], cand["task_id"], live_ops,
            dry_run=dry_run, task_status=cand.get("task_status") or None,
        )
        if receipt.get("removed") or receipt.get("kept"):
            results.append(receipt)
    return results


def main(argv=None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True,
        help="Absolute path to a per-project integration config JSON",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be removed; write nothing",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        results = prune_pass(config, dry_run=args.dry_run)
    except (IntegratorError, PruneError) as exc:
        print("Prune pass stopped: %s" % exc, file=sys.stderr)
        return 1

    if not results:
        print("Prune pass: nothing to evaluate.")
        return 0

    for r in results:
        removed = r.get("removed") or []
        kept = r.get("kept") or []
        prefix = "would prune" if args.dry_run else "pruned"
        if removed:
            print("%s %s/%s — %d removal(s)" % (
                prefix, r["worker"], r["task_id"], len(removed),
            ))
            for item in removed:
                print("  - %s: %s" % (item.get("kind"), item.get("branch") or item.get("path")))
        for keep in kept:
            print("kept %s/%s — %s (%s)" % (
                r["worker"], r["task_id"], keep.get("kind"), keep.get("reason"),
            ))
        for note in r.get("notes") or []:
            print("  note: %s" % note)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

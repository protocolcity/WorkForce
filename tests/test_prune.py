"""Seat branch/checkout pruning (wf-267)."""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import integrator, prune  # noqa: E402
from tests.test_integrator import (  # noqa: E402
    _git,
    _init_bare_remote_and_clone,
    base_config,
    make_worker,
    write_roster,
)


def _git_env():
    return dict(
        os.environ,
        GIT_AUTHOR_NAME="prune-test",
        GIT_AUTHOR_EMAIL="prune-test@example.invalid",
        GIT_COMMITTER_NAME="prune-test",
        GIT_COMMITTER_EMAIL="prune-test@example.invalid",
    )


def _run_git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, env=_git_env())
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class FakePruneOps:
    def __init__(
        self,
        *,
        clean=True,
        head_in_main=True,
        pr_state="MERGED",
        remote_branch=True,
        branch_merged=True,
        task_status="done",
    ):
        self.clean = clean
        self.head_in_main = head_in_main
        self.pr_state_val = pr_state
        self.remote_branch = remote_branch
        self.branch_merged = branch_merged
        self.task_status = task_status
        self.notes = []
        self.deleted_branches = []
        self.removed_worktrees = []

    def checkout_clean(self, checkout):
        return self.clean

    def head_ancestor_of_base(self, checkout):
        return self.head_in_main

    def remote_branch_exists(self, branch):
        return self.remote_branch

    def branch_merged_into_base(self, branch):
        return self.branch_merged

    def pr_state(self, branch):
        return self.pr_state_val

    def delete_remote_branch(self, branch):
        self.deleted_branches.append(branch)
        return {"rc": 0, "output": ""}

    def remove_worktree(self, checkout):
        self.removed_worktrees.append(checkout)
        if os.path.isdir(checkout):
            import shutil
            shutil.rmtree(checkout)
        return {"rc": 0, "output": ""}

    def list_remote_task_branches(self):
        return []

    def post_note(self, task_id, body):
        self.notes.append((task_id, body))
        return {"ok": True}

    def fetch_task_status(self, task_id):
        return self.task_status

    def as_dict(self):
        return {
            "checkout_clean": self.checkout_clean,
            "head_ancestor_of_base": self.head_ancestor_of_base,
            "remote_branch_exists": self.remote_branch_exists,
            "branch_merged_into_base": self.branch_merged_into_base,
            "pr_state": self.pr_state,
            "delete_remote_branch": self.delete_remote_branch,
            "remove_worktree": self.remove_worktree,
            "list_remote_task_branches": self.list_remote_task_branches,
            "post_note": self.post_note,
            "fetch_task_status": self.fetch_task_status,
        }


def _seat_layout(tmp_path, worker="tester", task_id="wf-9"):
    workspace = tmp_path / "ws"
    remote, main = _init_bare_remote_and_clone(tmp_path)
    task_run = workspace / "local" / "task-runs" / worker / task_id
    checkout = task_run / "checkout"
    branch = "workforce/task/%s/%s" % (worker, task_id)
    _run_git(["worktree", "add", "-b", branch, str(checkout), "main"], str(main))
    task_run.mkdir(parents=True, exist_ok=True)
    (task_run / "preparation.json").write_text(json.dumps({"task_id": task_id}) + "\n")
    (task_run / "prompt.md").write_text("prompt\n")
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(
        tmp_path,
        roster_path,
        local_root=str(workspace / "local"),
        workspace_root=str(workspace),
        main_checkout=str(main),
        checkout_template="local/task-runs/{worker}/{task_id}/checkout",
        branch_template="workforce/task/{worker}/{task_id}",
    )
    return cfg, main, checkout, task_run, branch


def test_evaluate_prune_open_pr_kept():
    ops = FakePruneOps(pr_state="OPEN")
    plan = prune.evaluate_prune(
        {"pr_base": "main", "checkout_template": "c", "workspace_root": "/w",
         "branch_template": "workforce/task/{worker}/{task_id}"},
        "tester", "wf-1", ops.as_dict(), task_status="done",
    )
    assert plan["actions"] == []
    assert any(k["reason"] == "open_pr" for k in plan["kept"])


def test_evaluate_prune_dirty_kept(tmp_path):
    workspace = tmp_path / "ws"
    checkout = workspace / "local" / "task-runs" / "tester" / "wf-1" / "checkout"
    checkout.mkdir(parents=True)
    ops = FakePruneOps(clean=False)
    plan = prune.evaluate_prune(
        {
            "pr_base": "main",
            "checkout_template": "local/task-runs/{worker}/{task_id}/checkout",
            "workspace_root": str(workspace),
            "branch_template": "workforce/task/{worker}/{task_id}",
        },
        "tester", "wf-1", ops.as_dict(), task_status="done",
    )
    assert plan["actions"] == []
    assert any(k["reason"] == "dirty" for k in plan["kept"])


def test_evaluate_prune_head_not_in_main_kept_with_note(tmp_path):
    workspace = tmp_path / "ws"
    checkout = workspace / "local" / "task-runs" / "tester" / "wf-1" / "checkout"
    checkout.mkdir(parents=True)
    ops = FakePruneOps(head_in_main=False)
    plan = prune.evaluate_prune(
        {
            "pr_base": "main",
            "checkout_template": "local/task-runs/{worker}/{task_id}/checkout",
            "workspace_root": str(workspace),
            "branch_template": "workforce/task/{worker}/{task_id}",
        },
        "tester", "wf-1", ops.as_dict(), task_status="done",
    )
    assert plan["actions"] == []
    assert any(k["reason"] == "head_not_in_main" for k in plan["kept"])
    assert plan["notes"]


def test_merged_clean_removes_branch_checkout_and_task_run(tmp_path):
    cfg, main, checkout, task_run, branch = _seat_layout(tmp_path)
    ops = FakePruneOps()
    receipt = prune.prune_one(cfg, "tester", "wf-9", ops.as_dict(), task_status="done")
    kinds = {item["kind"] for item in receipt["removed"]}
    assert "branch" in kinds
    assert "checkout" in kinds
    assert "task_run" in kinds
    assert branch in ops.deleted_branches
    assert not (task_run / "prompt.md").exists()
    assert (task_run / "preparation.json").exists()


def test_prune_dry_run_writes_nothing(tmp_path):
    cfg, main, checkout, task_run, branch = _seat_layout(tmp_path)
    ops = FakePruneOps()
    receipt = prune.prune_one(cfg, "tester", "wf-9", ops.as_dict(), dry_run=True, task_status="done")
    assert receipt["removed"]
    assert all(item.get("dry_run") for item in receipt["removed"])
    assert ops.deleted_branches == []
    assert (task_run / "prompt.md").exists()


def test_prune_after_close_assumes_merged(tmp_path):
    cfg, main, checkout, task_run, branch = _seat_layout(tmp_path)
    ops = FakePruneOps(pr_state="NONE", remote_branch=False)
    receipt = prune.prune_after_close(cfg, "tester", "wf-9", ops.as_dict())
    assert any(item["kind"] == "checkout" for item in receipt["removed"])


def test_prune_pass_discovers_local_reservation(tmp_path):
    cfg, main, checkout, task_run, branch = _seat_layout(tmp_path)
    ops = FakePruneOps()

    def fake_http(method, url, body=None, timeout=None):
        return {"tasks": []}

    results = prune.prune_pass(cfg, ops=ops.as_dict(), http=fake_http)
    assert len(results) == 1
    assert results[0]["task_id"] == "wf-9"


def test_integrator_close_triggers_prune(tmp_path, monkeypatch):
    from tests.test_integrator import FakeOps, make_order

    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path)
    prune_calls = []

    def fake_prune_after_close(config, worker, task_id, ops=None, dry_run=False):
        prune_calls.append((worker, task_id))
        return {"removed": [{"kind": "checkout"}]}

    monkeypatch.setattr(prune, "prune_after_close", fake_prune_after_close)
    post_merge = {
        "pr": {"url": "http://pr/1"},
        "reviewer": "cursor-reviewer",
        "version": {"from": "0.1.0", "to": "0.1.1"},
        "later_findings": [],
    }
    result = {"worker": "tester"}
    out = integrator._finish_after_stage("wf-1", "workforce", cfg, ops.as_dict(), post_merge, result)
    assert out["outcome"] == "closed"
    assert prune_calls == [("tester", "wf-1")]
    assert result["prune"]["removed"]


def test_parse_task_branch():
    assert prune.parse_task_branch("workforce/task/wf-cursor-implementer/wf-267") == (
        "wf-cursor-implementer", "wf-267",
    )
    assert prune.parse_task_branch("origin/workforce/task/a/b") == ("a", "b")
    assert prune.parse_task_branch("feature/foo") is None


def test_prune_ledger_event_allowed(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    line = integrator.append_ledger_row(
        cfg["local_root"], cfg["project"], "PRUNE",
        ticket="wf-1", worker="tester", target="branch",
    )
    assert " PRUNE " in line

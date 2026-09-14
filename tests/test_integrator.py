"""Integrator job (wf-265) — pure policy + orchestration with fake ops."""

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import integrator  # noqa: E402
from workforce import roster as roster_mod  # noqa: E402
from workforce.roster import Worker  # noqa: E402


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def make_config(tmp_path, roster_path, **over):
    main_checkout = tmp_path / "main_checkout"
    main_checkout.mkdir(exist_ok=True)
    cfg_raw = dict(
        local_root=str(tmp_path / "local"),
        roster_path=roster_path,
        project="workforce",
        test_cmd=["/bin/sh", "-c", "exit 0"],
        pr_base="main",
        version_bump="patch",
        version_file="VERSION.json",
        stage_cmd=["/bin/sh", "-c", "exit 0"],
        activate_cmd=["/bin/sh", "-c", "exit 0"],
        main_checkout=str(main_checkout),
    )
    cfg_raw.update(over)
    path = tmp_path / "integration_config.json"
    path.write_text(json.dumps(cfg_raw))
    return integrator.load_config(str(path))


def make_worker(tmp_path, name="tester", command=None):
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)
    contract = tmp_path / "CONTRACT.md"
    prompt = tmp_path / "prompt.md"
    contract.write_text("# contract\n")
    prompt.write_text("do one slice\n")
    return Worker(
        name=name, workdir=str(workdir), contract=str(contract), prompt=str(prompt),
        identity=name, command=command or ["claude", "-p", "x"],
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:%s" % name,
        budget_secs=5, min_free_mb=1,
    )


def write_roster(tmp_path, workers, filename="roster.json"):
    path = tmp_path / filename
    raw = {"workers": {}}
    for w in workers:
        spec = {f: getattr(w, f) for f in Worker.__dataclass_fields__ if f != "name"}
        raw["workers"][w.name] = spec
    path.write_text(json.dumps(raw))
    return str(path)


# --------------------------------------------------------------------------
# Pure policy
# --------------------------------------------------------------------------


def test_reviewer_for_provider_routes_by_implementation_provider():
    assert integrator.reviewer_for_provider("cursor") == "workflow-reviewer"
    assert integrator.reviewer_for_provider("claude") == "cursor-reviewer"
    assert integrator.reviewer_for_provider("grok") == "cursor-reviewer"


def test_reviewer_for_provider_unknown_raises():
    with pytest.raises(integrator.IntegratorError):
        integrator.reviewer_for_provider("unknown-vendor")


def test_reviewer_by_provider_config_override(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = make_config(
        tmp_path, roster_path,
        reviewer_by_provider={"cursor": "custom-reviewer"},
    )
    assert integrator.reviewer_for_provider("cursor", cfg["reviewer_by_provider"]) == "custom-reviewer"
    # unaffected keys keep their default
    assert integrator.reviewer_for_provider("claude", cfg["reviewer_by_provider"]) == "cursor-reviewer"


def test_decide_after_suites_pass_proceeds():
    d = integrator.decide_after_suites(0, 0, 2)
    assert d["action"] == "proceed"


def test_decide_after_suites_fail_recovers_within_bound():
    d = integrator.decide_after_suites(1, 0, 2)
    assert d["action"] == "recover"


def test_decide_after_suites_stops_when_rounds_exhausted():
    d = integrator.decide_after_suites(1, 2, 2)
    assert d["action"] == "stop"


def test_decide_after_findings_clean_proceeds():
    assert integrator.decide_after_findings([], 0, 2)["action"] == "proceed"


def test_decide_after_findings_recovers_then_stops():
    assert integrator.decide_after_findings(["fix x"], 0, 1)["action"] == "recover"
    assert integrator.decide_after_findings(["fix x"], 1, 1)["action"] == "stop"


@pytest.mark.parametrize(
    "ci_status,findings,clean,expected",
    [
        ("success", [], True, True),
        ("failure", [], True, False),
        ("pending", [], True, False),
        ("success", ["needs work"], True, False),
        ("success", [], False, False),
    ],
)
def test_decide_merge_ready_never_merges_unless_all_clear(ci_status, findings, clean, expected):
    ok, reason = integrator.decide_merge_ready(
        ci_status=ci_status, findings=findings, checkout_clean=clean,
    )
    assert ok is expected
    assert reason


def test_bump_version_rules():
    assert integrator.bump_version("1.2.3", "patch") == "1.2.4"
    assert integrator.bump_version("1.2.3", "minor") == "1.3.0"
    assert integrator.bump_version("1.2.3", "major") == "2.0.0"


def test_bump_version_rejects_malformed_current():
    with pytest.raises(integrator.IntegratorError):
        integrator.bump_version("not-a-version", "patch")


def test_strip_test_hunks_from_diff_drops_test_files_only():
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n+x\n"
        "diff --git a/tests/test_foo.py b/tests/test_foo.py\n+y\n"
    )
    scoped = integrator.strip_test_hunks_from_diff(diff)
    assert "src/foo.py" in scoped
    assert "test_foo.py" not in scoped


def test_parse_reviewer_findings_json_and_clean_text():
    assert integrator.parse_reviewer_findings('{"findings": ["a", "b"]}') == ["a", "b"]
    assert integrator.parse_reviewer_findings("no findings") == []
    assert integrator.parse_reviewer_findings("") == []


def test_parse_reviewer_findings_unparseable_text_is_not_silently_clean():
    # Fail closed: an unreviewed/garbled reply must not merge as "clean".
    assert integrator.parse_reviewer_findings("some free-text reviewer reply") == [
        "some free-text reviewer reply"
    ]


def test_build_close_body_has_all_four_sections():
    body = integrator.build_close_body({
        "completed": "did x", "verification": "tests pass",
        "links": "http://pr", "follow_ups": "none",
    })
    assert body.startswith("Completed: did x")
    assert "Verification: tests pass" in body
    assert "Links: http://pr" in body
    assert "Follow-ups: none" in body


def test_coordinator_lock_is_fresh(tmp_path):
    lock = tmp_path / "COORDINATOR.lock"
    assert integrator.coordinator_lock_is_fresh(str(lock), 900) is False
    lock.write_text("x")
    now = os.path.getmtime(str(lock))
    assert integrator.coordinator_lock_is_fresh(str(lock), 900, now=now + 10) is True
    assert integrator.coordinator_lock_is_fresh(str(lock), 900, now=now + 1000) is False


def test_recovery_state_roundtrip(tmp_path):
    local_root = str(tmp_path / "local")
    assert integrator.read_recovery_state(local_root, "wf-1") == {"rounds_used": 0}
    integrator.write_recovery_state(local_root, "wf-1", {"rounds_used": 1})
    assert integrator.read_recovery_state(local_root, "wf-1") == {"rounds_used": 1}
    integrator.clear_recovery_state(local_root, "wf-1")
    assert integrator.read_recovery_state(local_root, "wf-1") == {"rounds_used": 0}


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------


def test_load_config_rejects_relative_config_path(tmp_path):
    with pytest.raises(integrator.IntegratorError):
        integrator.load_config("relative/path.json")


def test_load_config_rejects_missing_required_keys(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"local_root": str(tmp_path)}))
    with pytest.raises(integrator.IntegratorError):
        integrator.load_config(str(path))


def test_load_config_defaults(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = make_config(tmp_path, roster_path)
    assert cfg["max_recovery_rounds"] == 2
    assert cfg["coordinator_lock_ttl_secs"] == 2400
    assert cfg["coordinator_lock_path"] == os.path.join(cfg["local_root"], "COORDINATOR.lock")
    assert cfg["reviewer_by_provider"]["cursor"] == "workflow-reviewer"


# --------------------------------------------------------------------------
# Ledger + receipts
# --------------------------------------------------------------------------


def test_append_ledger_row_rejects_unknown_event(tmp_path):
    with pytest.raises(integrator.IntegratorError):
        integrator.append_ledger_row(str(tmp_path), "workforce", "NOT_AN_EVENT")


def test_append_ledger_row_and_receipt(tmp_path):
    line = integrator.append_ledger_row(str(tmp_path), "workforce", "SUITES", ticket="wf-1", rc=0)
    assert "SUITES" in line
    log_path = os.path.join(str(tmp_path), "ledger", "integrator-workforce.log")
    assert os.path.exists(log_path)

    path = integrator.write_receipt(str(tmp_path), "workforce", {
        "generated_at": integrator._utc_iso_z(), "task_id": "wf-1", "outcome": "closed",
    })
    assert os.path.exists(path)
    with open(path) as fh:
        data = json.load(fh)
    assert data["outcome"] == "closed"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_discover_candidates_filters_seat_project_lock_and_cap(tmp_path, monkeypatch):
    w1 = make_worker(tmp_path, name="tester", command=["claude", "-p", "x"])
    w2 = make_worker(tmp_path, name="busy-seat", command=["cursor-agent"])
    roster_path = write_roster(tmp_path, [w1, w2])
    cfg = make_config(tmp_path, roster_path, active_implementation_cap=5)

    tasks = [
        {"id": "wf-1", "labels": ["worker:tester"], "title": "one"},
        {"id": "wf-2", "labels": ["worker:busy-seat"], "title": "two"},
        {"id": "wf-3", "labels": ["worker:no-such-seat"], "title": "three"},
        {"id": "wf-4", "labels": [], "title": "four"},
    ]

    def fake_http(method, url, body=None, timeout=15.0):
        return {"ok": True, "tasks": tasks}

    # Simulate busy-seat holding a live lock — engine.lock_inspect reads
    # local_root/locks/<name>.lock; a live (non-orphan) lock excludes it.
    # A real §3 lock is a directory holding a pid file (engine.lock_inspect);
    # a JSON file of that name was never recognised, which the dropped cap hid.
    lock_dir = os.path.join(cfg["local_root"], "locks", "busy-seat.lock")
    os.makedirs(lock_dir, exist_ok=True)
    with open(os.path.join(lock_dir, "pid"), "w") as fh:
        fh.write(str(os.getpid()))

    candidates = integrator.discover_candidates(cfg, http=fake_http)
    ids = [c["task_id"] for c in candidates]
    assert ids == ["wf-1"]
    assert candidates[0]["provider"] == "claude"


def test_discover_candidates_bounds_to_headroom(tmp_path):
    workers = [make_worker(tmp_path, name="a"), make_worker(tmp_path, name="b")]
    roster_path = write_roster(tmp_path, workers)
    cfg = make_config(tmp_path, roster_path, active_implementation_cap=1)
    tasks = [
        {"id": "wf-1", "labels": ["worker:a"], "title": "one"},
        {"id": "wf-2", "labels": ["worker:b"], "title": "two"},
    ]

    def fake_http(method, url, body=None, timeout=15.0):
        return {"ok": True, "tasks": tasks}

    candidates = integrator.discover_candidates(cfg, http=fake_http)
    assert len(candidates) == 1
    assert candidates[0]["task_id"] == "wf-1"


# --------------------------------------------------------------------------
# run_one orchestration — fake ops, disposable checkout
# --------------------------------------------------------------------------


class FakeOps:
    """Records every call; lets a test script exactly what each op returns."""

    def __init__(self, tmp_path, **overrides):
        self.calls = []
        version_path = tmp_path / "VERSION.json"
        version_path.write_text(json.dumps({"version": "1.0.0"}))
        self._version_path = str(version_path)
        self.suite_rc = overrides.get("suite_rc", 0)
        self.review_findings = overrides.get("review_findings", [])
        self.review_output = overrides.get("review_output")
        self.review_retry = overrides.get("review_retry", False)
        self.review_empty = overrides.get("review_empty", False)
        self.review_stale = overrides.get("review_stale", False)
        self.ci_status = overrides.get("ci_status", "success")
        self.checkout_clean = overrides.get("checkout_clean_val", True)
        self.merge_rc = overrides.get("merge_rc", 0)
        self.pre_merge_sha = overrides.get("pre_merge_sha", "sha-main")
        self.merge_parent_sha = overrides.get("merge_parent_sha", "sha-main")
        self.stage_rc = overrides.get("stage_rc", 0)
        self.activate_rc = overrides.get("activate_rc", 0)
        self.verify_ok = overrides.get("verify_ok", True)
        self.verify_observed = overrides.get("verify_observed", "")
        self.seat_in_flight_val = overrides.get("seat_in_flight_val", False)
        self.sync_sha = overrides.get("sync_sha", "sha-main")
        self.push_rc = overrides.get("push_rc", 0)
        self.push_sha = overrides.get("push_sha", self.sync_sha)
        self.post_push_remote_sha = overrides.get("post_push_remote_sha", self.push_sha)
        self._remote_head_sha_calls = 0
        self.recovery_calls = []
        self.stage_ctx = None
        self.activate_ctx = None
        self.verify_ctx = None
        self.screenshot_ctx = None
        self._head_sha_calls = 0
        self.head_shas = overrides.get("head_shas", ["sha-head-1", "sha-head-2", "sha-head-3"])
        self.close_evidence = None
        self.sha_exists_val = overrides.get("sha_exists_val", True)

    def _record(self, name, *a, **kw):
        self.calls.append(name)

    def run_suites(self, checkout):
        self._record("run_suites")
        return {"rc": self.suite_rc, "output": "suite output" if self.suite_rc else ""}

    def checkout_clean_fn(self, checkout):
        self._record("checkout_clean")
        return self.checkout_clean

    def diff_text(self, checkout, base):
        self._record("diff_text")
        self.diff_text_base = base
        return "diff --git a/src/x.py b/src/x.py\n+1\n"

    def checkout_head_sha(self, checkout):
        self._record("checkout_head_sha")
        idx = min(self._head_sha_calls, len(self.head_shas) - 1)
        self._head_sha_calls += 1
        return self.head_shas[idx]

    def sha_exists(self, checkout, sha):
        self._record("sha_exists")
        return self.sha_exists_val

    def push_branch(self, checkout, branch):
        self._record("push_branch")
        return {"rc": 0, "output": ""}

    def open_or_update_pr(self, checkout, branch, base, title, body):
        self._record("open_or_update_pr")
        return {"number": 42, "url": "http://pr/42", "action": "opened"}

    def ci_status_fn(self, checkout, pr_number):
        self._record("ci_status")
        return self.ci_status

    def dispatch_reviewer(self, reviewer, prompt):
        self._record("dispatch_reviewer")
        if self.review_retry:
            return {"ok": False, "retry": True, "output": "reviewer dispatch skipped (lock held)"}
        if self.review_empty:
            return {"ok": False, "empty": True, "output": "reviewer output was empty after DONE"}
        if self.review_stale:
            return {"ok": False, "stale": True, "output": "reviewer output was unchanged after DONE (stale)"}
        output = self.review_output
        if output is None:
            output = json.dumps({"findings": self.review_findings})
        return {"ok": True, "output": output}

    def merge_pr(self, checkout, pr_number):
        self._record("merge_pr")
        return {"rc": self.merge_rc, "output": ""}

    def remote_head_sha(self, checkout, branch):
        self._record("remote_head_sha")
        self._remote_head_sha_calls += 1
        if self._remote_head_sha_calls == 1:
            return self.pre_merge_sha
        return self.post_push_remote_sha

    def merge_commit_parent_sha(self, checkout, branch):
        self._record("merge_commit_parent_sha")
        return self.merge_parent_sha

    def sync_main_checkout(self, main_checkout, branch):
        self._record("sync_main_checkout")
        return {"rc": 0 if self.sync_sha else 1, "sha": self.sync_sha}

    def commit_and_push_version(self, main_checkout, branch, new_version):
        self._record("commit_and_push_version")
        return {"rc": self.push_rc, "sha": self.push_sha if self.push_rc == 0 else ""}

    def dispatch_recovery(self, worker, preparation_path, reason):
        self._record("dispatch_recovery")
        self.recovery_calls.append((worker, preparation_path, reason))
        return {"ok": True, "output": ""}

    def read_version(self, checkout):
        self._record("read_version")
        with open(self._version_path) as fh:
            return json.load(fh)["version"]

    def write_version(self, checkout, new_version):
        self._record("write_version")
        with open(self._version_path, "w") as fh:
            json.dump({"version": new_version}, fh)

    def run_stage(self, ctx=None):
        self._record("run_stage")
        self.stage_ctx = ctx
        return {"rc": self.stage_rc, "output": ""}

    def run_activate(self, ctx=None):
        self._record("run_activate")
        self.activate_ctx = ctx
        return {"rc": self.activate_rc, "output": ""}

    def verify_installed_version(self, expected, ctx=None):
        self._record("verify_installed_version")
        self.verify_ctx = ctx
        return {"ok": self.verify_ok, "observed": self.verify_observed or expected}

    def capture_screenshots(self, ctx=None):
        self._record("capture_screenshots")
        self.screenshot_ctx = ctx
        return ["shot1.png"]

    def post_comment(self, task_id, body):
        self._record("post_comment")
        return {"ok": True}

    def release_seat(self, task_id, reason):
        self._record("release_seat")
        return {"ok": True}

    def close_order(self, task_id, evidence):
        self._record("close_order")
        self.close_evidence = evidence
        return {"ok": True}

    def as_dict(self):
        return {
            "run_suites": self.run_suites,
            "checkout_clean": self.checkout_clean_fn,
            "diff_text": self.diff_text,
            "checkout_head_sha": self.checkout_head_sha,
            "sha_exists": self.sha_exists,
            "push_branch": self.push_branch,
            "open_or_update_pr": self.open_or_update_pr,
            "ci_status": self.ci_status_fn,
            "dispatch_reviewer": self.dispatch_reviewer,
            "merge_pr": self.merge_pr,
            "remote_head_sha": self.remote_head_sha,
            "merge_commit_parent_sha": self.merge_commit_parent_sha,
            "sync_main_checkout": self.sync_main_checkout,
            "commit_and_push_version": self.commit_and_push_version,
            "dispatch_recovery": self.dispatch_recovery,
            "read_version": self.read_version,
            "write_version": self.write_version,
            "run_stage": self.run_stage,
            "run_activate": self.run_activate,
            "verify_installed_version": self.verify_installed_version,
            "capture_screenshots": self.capture_screenshots,
            "post_comment": self.post_comment,
            "release_seat": self.release_seat,
            "close_order": self.close_order,
        }


def make_order(task_id="wf-1", worker="tester", provider="claude", title="do a thing"):
    return {"task_id": task_id, "worker": worker, "provider": provider, "title": title}


def base_config(tmp_path, roster_path, **over):
    kwargs = dict(
        version_file="VERSION.json",
        checkout_template="checkout",
        workspace_root=str(tmp_path),
    )
    kwargs.update(over)
    return make_config(tmp_path, roster_path, **kwargs)


def test_run_one_dry_run_writes_nothing_and_calls_no_ops(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path)
    result = integrator.run_one(make_order(), cfg, ops.as_dict(), dry_run=True)
    assert result["outcome"] == "dry_run"
    assert ops.calls == []
    assert not os.path.exists(os.path.join(cfg["local_root"], "reports"))
    assert not os.path.exists(os.path.join(cfg["local_root"], "ledger"))


def test_run_one_skips_when_coordinator_lock_fresh(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    os.makedirs(cfg["local_root"], exist_ok=True)
    with open(cfg["coordinator_lock_path"], "w") as fh:
        fh.write("live")
    ops = FakeOps(tmp_path)
    result = integrator.run_one(make_order(), cfg, ops.as_dict(), dry_run=False)
    assert result["outcome"] == "skipped_coordinator_active"
    assert ops.calls == []


def test_run_one_suite_failure_recovers_without_merging(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, suite_rc=1)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "recovering"
    assert "release_seat" in ops.calls
    assert "merge_pr" not in ops.calls
    state = integrator.read_recovery_state(cfg["local_root"], "wf-1")
    assert state["rounds_used"] == 1


def test_run_one_suite_failure_stops_after_recovery_rounds_exhausted(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, max_recovery_rounds=1)
    integrator.write_recovery_state(cfg["local_root"], "wf-1", {"rounds_used": 1})
    ops = FakeOps(tmp_path, suite_rc=1)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "stopped"
    assert "merge_pr" not in ops.calls


def test_run_one_findings_recover_without_merging(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, review_findings=["fix the thing"])
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "recovering"
    assert "release_seat" in ops.calls
    assert "merge_pr" not in ops.calls


def test_run_one_never_merges_on_red_ci(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, ci_status="failure")
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "waiting"
    assert "merge_pr" not in ops.calls
    assert "close_order" not in ops.calls


def test_run_one_never_merges_on_dirty_checkout(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, checkout_clean_val=False)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "waiting"
    assert "merge_pr" not in ops.calls


def test_run_one_clean_pass_merges_bumps_stages_activates_and_closes(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "closed"
    for step in ("merge_pr", "write_version", "run_stage", "run_activate", "close_order"):
        assert step in ops.calls
    assert result["version"] == {"from": "1.0.0", "to": "1.0.1"}
    # recovery state is cleared on a real close
    assert integrator.read_recovery_state(cfg["local_root"], "wf-1") == {"rounds_used": 0}
    receipt_dir = os.path.join(cfg["local_root"], "reports", "integrator", "workforce")
    assert os.path.isdir(receipt_dir)
    assert len(os.listdir(receipt_dir)) == 1


def test_run_one_stage_failure_stops_before_activate(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, stage_rc=1)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "stage_failed"
    assert "run_activate" not in ops.calls
    assert "close_order" not in ops.calls


# --------------------------------------------------------------------------
# wf-265 review recovery — close-only-on-verified-install (finding 1)
# --------------------------------------------------------------------------


def test_run_one_does_not_close_when_install_verification_fails(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, verify_ok=False, verify_observed="1.0.0")
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "install_not_verified"
    assert "close_order" not in ops.calls
    assert "post_comment" in ops.calls


# --------------------------------------------------------------------------
# wf-265 review recovery — merge gate re-checked and merge_pr rc honoured
# (finding 2)
# --------------------------------------------------------------------------


def test_run_one_failed_merge_pr_does_not_bump_or_stage(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, merge_rc=1)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "merge_failed"
    assert "write_version" not in ops.calls
    assert "run_stage" not in ops.calls
    assert "close_order" not in ops.calls
    log_path = os.path.join(cfg["local_root"], "ledger", "integrator-workforce.log")
    with open(log_path) as fh:
        log = fh.read()
    assert " MERGE " not in log


# --------------------------------------------------------------------------
# wf-265 recovery 6, finding 1 — a SKIP/lock-held reviewer dispatch is a
# retry-later outcome, never a completed review or a consumed recovery
# round; finding 2 — an empty reviewer output after DONE fails the review
# rather than clearing the merge gate.
# --------------------------------------------------------------------------


def test_run_one_review_retry_never_merges_or_recovers(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, review_retry=True)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "review_retry"
    assert "release_seat" not in ops.calls
    assert "dispatch_recovery" not in ops.calls
    assert "merge_pr" not in ops.calls
    assert integrator.read_recovery_state(cfg["local_root"], "wf-1").get("rounds_used", 0) == 0


def test_run_one_review_empty_stops_with_blocked_comment_and_never_merges(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, review_empty=True)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "review_empty"
    assert "post_comment" in ops.calls
    assert "merge_pr" not in ops.calls
    assert "close_order" not in ops.calls


def test_run_one_review_stale_stops_with_blocked_comment_and_never_merges(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, review_stale=True)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "review_stale"
    assert "post_comment" in ops.calls
    assert "merge_pr" not in ops.calls
    assert "close_order" not in ops.calls


# --------------------------------------------------------------------------
# wf-268 — review rounds converge: correction-delta review, capped findings,
# JSON-numbered-list splitting, recovery rounds since last human clear
# --------------------------------------------------------------------------


def test_build_reviewer_prompt_round_one_reviews_whole_diff():
    order = make_order()
    prompt = integrator.build_reviewer_prompt(order, "diff --git a/x b/x\n+1\n")
    assert "correction" not in prompt.lower()
    assert "--- diff (tests excluded) ---" in prompt
    assert "top 3" in prompt


def test_build_reviewer_prompt_correction_round_scopes_to_delta_and_prior_findings():
    order = make_order()
    prompt = integrator.build_reviewer_prompt(
        order, "diff --git a/x b/x\n+2\n",
        previous_findings=["finding one", "finding two"],
        max_findings=2,
    )
    assert "correction round" in prompt.lower()
    assert "1. finding one" in prompt
    assert "2. finding two" in prompt
    assert "--- correction diff (tests excluded) ---" in prompt
    assert "not the" in prompt.lower() and "whole pr" in prompt.lower()
    assert "top 2" in prompt


def test_cap_findings_splits_blocking_and_later():
    findings = ["a", "b", "c", "d", "e"]
    blocking, later = integrator.cap_findings(findings, 3)
    assert blocking == ["a", "b", "c"]
    assert later == ["d", "e"]


def test_cap_findings_under_cap_has_no_later():
    blocking, later = integrator.cap_findings(["a"], 3)
    assert blocking == ["a"]
    assert later == []


def test_parse_reviewer_findings_splits_numbered_list_inside_one_json_entry():
    """pc-1492 rehearsal round 6: the reviewer folded five numbered items into
    one findings[] string; the ledger count must equal the five items, not one."""
    body = json.dumps({
        "findings": [
            "1. first issue\n2. second issue\n3. third issue\n4. fourth issue\n5. fifth issue",
        ],
    })
    findings = integrator.parse_reviewer_findings(body)
    assert len(findings) == 5
    assert findings[0].startswith("1.")
    assert findings[-1].startswith("5.")


def test_parse_reviewer_findings_json_array_entries_without_numbering_stay_whole():
    body = json.dumps({"findings": ["plain finding a", "plain finding b"]})
    assert integrator.parse_reviewer_findings(body) == ["plain finding a", "plain finding b"]


def test_run_one_round_one_reviews_full_pr_diff(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path)
    integrator.run_one(make_order(), cfg, ops.as_dict())
    assert ops.diff_text_base == cfg["pr_base"]


def test_run_one_correction_round_reviews_delta_since_prior_review_only(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, max_recovery_rounds=3)
    integrator.write_recovery_state(cfg["local_root"], "wf-1", {
        "rounds_used": 1,
        "last_review": {"sha": "sha-round-1", "findings": ["fix the thing"]},
    })
    ops = FakeOps(tmp_path)  # clean pass: closes the finding
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert ops.diff_text_base == "sha-round-1"
    assert result["outcome"] == "closed"


def test_run_one_findings_recovery_records_last_review_sha_and_findings_for_next_round(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, review_findings=["fix x"], head_shas=["sha-a"])
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "recovering"
    state = integrator.read_recovery_state(cfg["local_root"], "wf-1")
    assert state["last_review"] == {"sha": "sha-a", "findings": ["fix x"]}
    assert state["rounds_used"] == 1


# --------------------------------------------------------------------------
# wf-268 cursor-reviewer follow-up findings (PR 28):
# 1. a stale last_review.sha must not produce an empty correction delta
# 2. later findings must accumulate across rounds, never be dropped
# 3. capped findings must reach the ledger, at cap time and at close
# --------------------------------------------------------------------------


def test_run_one_stale_last_review_sha_falls_back_to_full_pr_base_review(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, max_recovery_rounds=3)
    integrator.write_recovery_state(cfg["local_root"], "wf-1", {
        "rounds_used": 1,
        "last_review": {"sha": "sha-gone", "findings": ["fix the thing"]},
    })
    ops = FakeOps(tmp_path, sha_exists_val=False)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert "sha_exists" in ops.calls
    assert ops.diff_text_base == cfg["pr_base"]
    assert result["outcome"] == "closed"


def test_run_one_live_last_review_sha_still_scopes_to_delta(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, max_recovery_rounds=3)
    integrator.write_recovery_state(cfg["local_root"], "wf-1", {
        "rounds_used": 1,
        "last_review": {"sha": "sha-round-1", "findings": ["fix the thing"]},
    })
    ops = FakeOps(tmp_path, sha_exists_val=True)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert ops.diff_text_base == "sha-round-1"
    assert result["outcome"] == "closed"


def test_run_one_accumulates_later_findings_across_recovery_rounds(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, max_findings_per_round=1, max_recovery_rounds=3)
    integrator.write_recovery_state(cfg["local_root"], "wf-1", {
        "rounds_used": 1,
        "last_review": {"sha": "sha-round-1", "findings": ["blocker one"]},
        "later_findings": ["earlier later a"],
    })
    ops = FakeOps(tmp_path, review_findings=["blocker two", "new later b"])
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "recovering"
    assert result["later_findings"] == ["earlier later a", "new later b"]
    state = integrator.read_recovery_state(cfg["local_root"], "wf-1")
    assert state["later_findings"] == ["earlier later a", "new later b"]


def test_run_one_accumulated_later_findings_reach_close_follow_ups(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, max_findings_per_round=1, max_recovery_rounds=3)
    integrator.write_recovery_state(cfg["local_root"], "wf-1", {
        "rounds_used": 1,
        "last_review": {"sha": "sha-round-1", "findings": []},
        "later_findings": ["earlier later a"],
    })
    ops = FakeOps(tmp_path)  # clean pass: no new findings, proceeds to merge
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "closed"
    assert ops.close_evidence["follow_ups"] == "earlier later a"


def test_run_one_writes_later_ledger_row_when_findings_are_capped(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, max_findings_per_round=2)
    ops = FakeOps(tmp_path, review_findings=["a", "b", "c", "d"])
    integrator.run_one(make_order(), cfg, ops.as_dict())
    log_path = os.path.join(cfg["local_root"], "ledger", "integrator-workforce.log")
    with open(log_path) as fh:
        log = fh.read()
    assert " LATER " in log
    assert "count=2" in log


def test_finish_after_stage_writes_later_ledger_row_at_close(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path)
    post_merge = {
        "pr": {"number": 1, "url": "http://pr/1"},
        "reviewer": "cursor-reviewer",
        "version": {"from": "1.0.0", "to": "1.0.1"},
        "later_findings": ["earlier later a", "new later b"],
    }
    result = {
        "generated_at": integrator._utc_iso_z(), "task_id": "wf-1", "worker": "tester",
        "dry_run": False, "outcome": None,
    }
    integrator._finish_after_stage("wf-1", "workforce", cfg, ops.as_dict(), post_merge, result)
    log_path = os.path.join(cfg["local_root"], "ledger", "integrator-workforce.log")
    with open(log_path) as fh:
        log = fh.read()
    assert " LATER " in log
    assert "count=2" in log


def test_run_one_caps_findings_and_records_the_rest_as_later(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, max_findings_per_round=2)
    ops = FakeOps(tmp_path, review_findings=["a", "b", "c", "d"])
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["findings"] == ["a", "b"]
    assert result["later_findings"] == ["c", "d"]
    assert result["outcome"] == "recovering"
    state = integrator.read_recovery_state(cfg["local_root"], "wf-1")
    assert state["last_review"]["findings"] == ["a", "b"]


def test_finish_after_stage_records_later_findings_as_close_follow_ups(tmp_path):
    """Findings below the cap's severity are recorded on the order as a
    follow-up note at close, never as a blocker (wf-268)."""
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path)
    post_merge = {
        "pr": {"number": 1, "url": "http://pr/1"},
        "reviewer": "cursor-reviewer",
        "version": {"from": "1.0.0", "to": "1.0.1"},
        "later_findings": ["minor nit"],
    }
    result = {
        "generated_at": integrator._utc_iso_z(), "task_id": "wf-1", "worker": "tester",
        "dry_run": False, "outcome": None,
    }
    out = integrator._finish_after_stage("wf-1", "workforce", cfg, ops.as_dict(), post_merge, result)
    assert out["outcome"] == "closed"
    assert ops.close_evidence["follow_ups"] == "minor nit"


def test_clear_recovery_round_state_resets_rounds_and_records_who_and_why(tmp_path):
    local_root = str(tmp_path / "local")
    integrator.write_recovery_state(local_root, "wf-1", {"rounds_used": 2})
    state = integrator.clear_recovery_round_state(local_root, "wf-1", "you", "seat fixed everything by hand")
    assert state["rounds_used"] == 0
    assert state["cleared_by"] == "you"
    assert state["cleared_reason"] == "seat fixed everything by hand"
    assert state["cleared_at"]
    assert integrator.read_recovery_state(local_root, "wf-1") == state


def test_run_one_stop_after_a_human_clear_reports_who_cleared_it(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, max_recovery_rounds=1)
    integrator.clear_recovery_round_state(cfg["local_root"], "wf-1", "you", "cleared for a fresh attempt")
    integrator.write_recovery_state(cfg["local_root"], "wf-1", {
        "rounds_used": 1, "cleared_by": "you", "cleared_reason": "cleared for a fresh attempt",
        "cleared_at": "2026-01-01T00:00:00Z",
    })
    ops = FakeOps(tmp_path, review_findings=["still broken"])
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "stopped"
    assert result["last_cleared"]["by"] == "you"
    assert result["last_cleared"]["reason"] == "cleared for a fresh attempt"


def test_main_clear_recovery_cli_resets_state(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg_raw = dict(
        local_root=str(tmp_path / "local"),
        roster_path=roster_path,
        project="workforce",
        test_cmd=["/bin/sh", "-c", "exit 0"],
        pr_base="main",
        version_bump="patch",
        version_file="VERSION.json",
        stage_cmd=["/bin/sh", "-c", "exit 0"],
        activate_cmd=["/bin/sh", "-c", "exit 0"],
        main_checkout=str(tmp_path / "main_checkout"),
    )
    (tmp_path / "main_checkout").mkdir(exist_ok=True)
    cfg_path = tmp_path / "integration_config.json"
    cfg_path.write_text(json.dumps(cfg_raw))
    local_root = cfg_raw["local_root"]
    integrator.write_recovery_state(local_root, "wf-1", {"rounds_used": 2})
    rc = integrator.main([
        "--config", str(cfg_path), "--clear-recovery", "wf-1",
        "--cleared-by", "you", "--reason", "seat fixed by hand",
    ])
    assert rc == 0
    state = integrator.read_recovery_state(local_root, "wf-1")
    assert state["rounds_used"] == 0
    assert state["cleared_by"] == "you"


# --------------------------------------------------------------------------
# wf-265 review recovery — findings parser (finding 3)
# --------------------------------------------------------------------------

_UPLIFT_DIR = "/Users/eliefrainseo/OneSeo/local/reports/parallel-plan-pass/uplift"
_uplift_available = os.path.isdir(_UPLIFT_DIR)


def _read_uplift(name):
    with open(os.path.join(_UPLIFT_DIR, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.skipif(not _uplift_available, reason="host-local reviewer transcripts not present")
def test_parse_reviewer_findings_real_cursor_pc_1485_three_items():
    findings = integrator.parse_reviewer_findings(_read_uplift("cursor-reviewer.out.pc-1485"))
    assert len(findings) == 3
    assert all("what looks correct" not in f.lower() for f in findings)


@pytest.mark.skipif(not _uplift_available, reason="host-local reviewer transcripts not present")
def test_parse_reviewer_findings_real_grok_workflow_pc_1484_three_items():
    findings = integrator.parse_reviewer_findings(_read_uplift("workflow-reviewer.out.pc-1484"))
    assert len(findings) == 3
    # Grok's NDJSON text events must be concatenated, not left as one blob
    # per streamed fragment.
    assert all(f.strip().startswith(("1.", "2.", "3.", "**1.", "**2.", "**3.")) for f in findings)


@pytest.mark.skipif(not _uplift_available, reason="host-local reviewer transcripts not present")
def test_parse_reviewer_findings_real_cursor_pc_1494_strips_trailing_checked_section():
    findings = integrator.parse_reviewer_findings(_read_uplift("cursor-reviewer.out.pc-1494"))
    assert len(findings) == 3
    joined = " ".join(findings).lower()
    assert "other acceptance items" not in joined
    assert "cleared inbox-report not read" not in joined


def test_parse_reviewer_findings_none_with_trailing_what_looks_correct_section():
    text = (
        "Findings: none\n\n"
        "What looks correct:\n"
        "- suites wiring matches the configured test_cmd\n"
        "- reviewer routing follows the provider table\n"
    )
    assert integrator.parse_reviewer_findings(text) == []


def test_parse_reviewer_findings_splits_numbered_grok_blob():
    text = "1. run_one closes on failed verify\n2. merge rc ignored\n3. parser mishandles prose"
    findings = integrator.parse_reviewer_findings(text)
    assert findings == [
        "1. run_one closes on failed verify",
        "2. merge rc ignored",
        "3. parser mishandles prose",
    ]


def test_parse_reviewer_findings_does_not_fail_open_on_word_none():
    """Second-pass review finding 2: a substring match on "none" must not
    swallow a real finding whose own text happens to contain that word."""
    assert integrator.parse_reviewer_findings(
        "returns none on validation failure"
    ) == ["returns none on validation failure"]


def test_parse_reviewer_findings_does_not_fail_open_on_word_clean():
    assert integrator.parse_reviewer_findings(
        "checkout is not clean after stage"
    ) == ["checkout is not clean after stage"]


def test_parse_reviewer_findings_concatenates_grok_ndjson_text_events():
    events = [
        {"type": "thought", "data": "thinking, not a finding"},
        {"type": "text", "data": "1. "},
        {"type": "text", "data": "first issue\n"},
        {"type": "text", "data": "2. second issue"},
    ]
    text = "--- pass 1 ---\n" + "\n".join(json.dumps(e) for e in events)
    assert integrator.parse_reviewer_findings(text) == ["1. first issue", "2. second issue"]


# --------------------------------------------------------------------------
# wf-265 review recovery — lock freshness from updated_at (finding 4)
# --------------------------------------------------------------------------


def test_coordinator_lock_is_fresh_reads_updated_at_json_field(tmp_path):
    lock = tmp_path / "COORDINATOR.lock"
    now = time.time()
    stale_mtime_but_fresh_updated_at = integrator._utc_iso_z(
        integrator._utcnow()
    )
    lock.write_text(json.dumps({
        "coordinator": "test", "pid": 1, "updated_at": stale_mtime_but_fresh_updated_at,
    }))
    # Backdate the file's mtime far past the TTL — only the JSON field
    # should decide freshness now, not the filesystem mtime.
    old = now - 10000
    os.utime(str(lock), (old, old))
    assert integrator.coordinator_lock_is_fresh(str(lock), 2400, now=now) is True


def test_coordinator_lock_is_fresh_expired_updated_at_field(tmp_path):
    lock = tmp_path / "COORDINATOR.lock"
    now = time.time()
    stale = integrator._utc_iso_z(
        integrator._utcnow() - __import__("datetime").timedelta(seconds=5000)
    )
    lock.write_text(json.dumps({"coordinator": "test", "pid": 1, "updated_at": stale}))
    assert integrator.coordinator_lock_is_fresh(str(lock), 2400, now=now) is False


def test_coordinator_lock_is_fresh_falls_back_to_mtime_without_updated_at(tmp_path):
    lock = tmp_path / "COORDINATOR.lock"
    lock.write_text("not json")
    now = os.path.getmtime(str(lock))
    assert integrator.coordinator_lock_is_fresh(str(lock), 2400, now=now + 10) is True
    assert integrator.coordinator_lock_is_fresh(str(lock), 2400, now=now + 10000) is False


def test_default_coordinator_lock_ttl_is_40_minutes():
    assert integrator._DEFAULT_COORDINATOR_LOCK_TTL_SECS == 2400


# --------------------------------------------------------------------------
# wf-265 review recovery — skip activate while a seat is in flight (finding 5)
# --------------------------------------------------------------------------


def _write_live_lock(local_root, worker):
    # engine.lock_inspect expects a lock *directory* containing a "pid" file
    # naming a live process — not a JSON file.
    lock_dir = os.path.join(local_root, "locks", "%s.lock" % worker)
    os.makedirs(lock_dir, exist_ok=True)
    with open(os.path.join(lock_dir, "pid"), "w") as fh:
        fh.write(str(os.getpid()))


def test_seat_in_flight_true_for_live_lock(tmp_path):
    local_root = str(tmp_path / "local")
    _write_live_lock(local_root, "some-seat")
    assert integrator.seat_in_flight(local_root) is True


def test_seat_in_flight_true_for_open_ledger_shift(tmp_path):
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    with open(os.path.join(ledger_dir, "some-seat.log"), "w") as fh:
        fh.write("%s START queue=ready budget_secs=600\n" % integrator._utc_iso_z())
    assert integrator.seat_in_flight(local_root) is True


def test_seat_in_flight_false_when_no_locks_or_open_shifts(tmp_path):
    assert integrator.seat_in_flight(str(tmp_path / "local")) is False


def test_run_one_skips_activate_when_seat_in_flight(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    _write_live_lock(cfg["local_root"], "other-seat")
    ops = FakeOps(tmp_path)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "activate_skipped"
    assert "run_activate" not in ops.calls
    assert "close_order" not in ops.calls
    # merge/version/stage already happened this pass
    assert "write_version" in ops.calls
    assert "run_stage" in ops.calls


def test_run_one_resumes_from_activate_after_seat_in_flight_without_remerging(tmp_path):
    """Second-pass review finding 1: activate_skipped must be resumable.

    A pass parked at ``activate_skipped`` persists merged/bumped/staged
    state; once the blocking seat clears, the next ``run_one`` call for the
    same order must finish from activate — never re-running suites, review,
    or ``merge_pr``.
    """
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    _write_live_lock(cfg["local_root"], "other-seat")
    ops = FakeOps(tmp_path)
    order = make_order()

    first = integrator.run_one(order, cfg, ops.as_dict())
    assert first["outcome"] == "activate_skipped"
    assert ops.calls.count("merge_pr") == 1
    assert "run_activate" not in ops.calls

    shutil.rmtree(os.path.join(cfg["local_root"], "locks"))
    second = integrator.run_one(order, cfg, ops.as_dict())
    assert second["outcome"] == "closed"
    assert ops.calls.count("merge_pr") == 1
    assert ops.calls.count("run_suites") == 1
    assert ops.calls.count("dispatch_reviewer") == 1
    assert ops.calls.count("write_version") == 1
    assert ops.calls.count("run_stage") == 1
    assert "run_activate" in ops.calls
    assert "close_order" in ops.calls
    assert second["version"] == {"from": "1.0.0", "to": "1.0.1"}


# --------------------------------------------------------------------------
# wf-265 review recovery — refuse the version bump when main moved
# (finding 6)
# --------------------------------------------------------------------------


def test_run_one_refuses_bump_when_origin_main_moved_during_merge(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, pre_merge_sha="sha-before", merge_parent_sha="sha-after-someone-else-merged")
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "main_moved"
    assert "write_version" not in ops.calls
    assert "run_stage" not in ops.calls
    assert "close_order" not in ops.calls


def test_run_one_stops_main_unverified_when_pre_merge_sha_empty(tmp_path):
    """Second-pass review finding 3: an empty pre-merge SHA must stop the
    order before merge/bump/stage, not be treated as unverifiable-but-ok."""
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, pre_merge_sha="")
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "main_unverified"
    assert "merge_pr" not in ops.calls
    assert "write_version" not in ops.calls
    assert "run_stage" not in ops.calls
    assert "close_order" not in ops.calls


def test_run_one_stops_main_unverified_when_merge_parent_sha_empty(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, merge_parent_sha="")
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "main_unverified"
    assert "merge_pr" in ops.calls
    assert "write_version" not in ops.calls
    assert "run_stage" not in ops.calls
    assert "close_order" not in ops.calls


def test_run_one_bumps_when_origin_main_did_not_move(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, pre_merge_sha="sha-main", merge_parent_sha="sha-main")
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "closed"
    assert "write_version" in ops.calls


# --------------------------------------------------------------------------
# wf-265 review recovery — dispatch engine recovery after findings/suite
# failure (finding 7)
# --------------------------------------------------------------------------


def test_run_one_suite_failure_dispatches_engine_recovery(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, suite_rc=1)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "recovering"
    assert "dispatch_recovery" in ops.calls
    worker, preparation_path, reason = ops.recovery_calls[0]
    assert worker == "tester"
    assert os.path.isabs(preparation_path)
    assert preparation_path.endswith("preparation.json")
    assert reason


def test_run_one_findings_recovery_dispatches_engine_recovery(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, review_findings=["fix the thing"])
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "recovering"
    assert "dispatch_recovery" in ops.calls
    worker, preparation_path, reason = ops.recovery_calls[0]
    assert worker == "tester"
    assert os.path.isabs(preparation_path)


def test_run_one_clean_pass_does_not_dispatch_recovery(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "closed"
    assert "dispatch_recovery" not in ops.calls


# --------------------------------------------------------------------------
# wf-265 recovery 3, finding 1 — bump_version local-suffix rule
# --------------------------------------------------------------------------


def test_bump_version_local_suffix_increments_trailing_integer():
    assert (
        integrator.bump_version("0.1.47+consolidation.56", "local-suffix")
        == "0.1.47+consolidation.57"
    )
    assert (
        integrator.bump_version("0.1.9+consolidation.13", "local-suffix")
        == "0.1.9+consolidation.14"
    )


def test_bump_version_local_suffix_rejects_bare_semver():
    with pytest.raises(integrator.IntegratorError):
        integrator.bump_version("1.2.3", "local-suffix")


def test_read_version_accepts_local_suffix_form(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        'name = "workforce"\nversion = "0.1.9+consolidation.13"\nother = "1.2.3"\n'
    )
    cfg = {"version_file": "pyproject.toml", "version_key": "version"}
    assert integrator._read_version(cfg, str(checkout)) == "0.1.9+consolidation.13"


def test_write_version_replaces_only_the_version_line_local_suffix(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    original = 'name = "workforce"\nversion = "0.1.9+consolidation.13"\nother = "1.2.3"\n'
    (checkout / "pyproject.toml").write_text(original)
    cfg = {"version_file": "pyproject.toml", "version_key": "version"}
    integrator._write_version(cfg, str(checkout), "0.1.9+consolidation.14")
    updated = (checkout / "pyproject.toml").read_text()
    assert updated == 'name = "workforce"\nversion = "0.1.9+consolidation.14"\nother = "1.2.3"\n'
    assert integrator._read_version(cfg, str(checkout)) == "0.1.9+consolidation.14"


def test_load_config_accepts_local_suffix_version_bump(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path, version_bump="local-suffix")
    assert cfg["version_bump"] == "local-suffix"


# --------------------------------------------------------------------------
# wf-265 recovery 3, finding 2 — version bump lands on a configured
# main_checkout, committed and pushed, with main_moved re-checked
# --------------------------------------------------------------------------


def test_run_one_bumps_version_on_main_checkout_not_seat_checkout(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "closed"
    assert "sync_main_checkout" in ops.calls
    assert "commit_and_push_version" in ops.calls
    assert ops.stage_ctx["checkout"] == cfg["main_checkout"]
    assert ops.activate_ctx["checkout"] == cfg["main_checkout"]


def test_run_one_stops_main_unverified_when_sync_main_checkout_fails(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, sync_sha="")
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "main_unverified"
    assert "write_version" not in ops.calls
    assert "run_stage" not in ops.calls


def test_run_one_stops_main_unverified_when_version_push_fails(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, push_rc=1)
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "main_unverified"
    assert "run_stage" not in ops.calls
    assert "close_order" not in ops.calls


def test_run_one_stops_main_moved_when_push_races_origin(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    cfg = base_config(tmp_path, roster_path)
    ops = FakeOps(tmp_path, push_sha="sha-ours", post_push_remote_sha="sha-someone-else-pushed")
    result = integrator.run_one(make_order(), cfg, ops.as_dict())
    assert result["outcome"] == "main_moved"
    assert "run_stage" not in ops.calls
    assert "close_order" not in ops.calls


# --------------------------------------------------------------------------
# wf-265 recovery 3, finding 3 — reviewer prompt/output via files, waiting
# for the reviewer's own ledger DONE/ERROR/STOP row
# --------------------------------------------------------------------------


# A reviewer_dispatch_cmd for tests that behaves like the real engine: reads
# the prompt file this dispatch just wrote, records its sha256 (truncated to
# 16 hex chars, matching engine._sha256) on its own START row, optionally
# writes findings to the output file, then appends DONE.
_FAKE_REVIEWER_SCRIPT = (
    "import hashlib, datetime, sys\n"
    "prompt_path, ledger_path, output_path, content = sys.argv[1:5]\n"
    "t = open(prompt_path, encoding='utf-8').read()\n"
    "h = hashlib.sha256(t.encode('utf-8')).hexdigest()[:16]\n"
    "ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')\n"
    "open(ledger_path, 'a').write(ts + ' START prompt_sha=' + h + '\\n' + ts + ' DONE\\n')\n"
    "if content != '__WF265_NO_WRITE__':\n"
    "    open(output_path, 'w').write(content)\n"
)


def _fake_reviewer_dispatch_cmd(prompt_path, ledger_path, output_path, content="__WF265_NO_WRITE__"):
    return [sys.executable, "-c", _FAKE_REVIEWER_SCRIPT, prompt_path, ledger_path, output_path, content]


def test_default_ops_dispatch_reviewer_writes_prompt_file_and_reads_output_file(tmp_path, monkeypatch):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    # A stale DONE row from an earlier run, already on disk before this
    # dispatch's offset is captured — it must never satisfy the wait even
    # though it shares the same terminal-event shape as the real one below.
    with open(ledger_path, "w") as fh:
        fh.write("2020-01-01T00:00:00Z DONE\n")
    prompt_path = str(run_dir / "cursor-reviewer.prompt.md")
    output_path = str(run_dir / "cursor-reviewer.out")
    cfg = base_config(
        tmp_path, roster_path,
        local_root=local_root,
        # The real reviewer job appends its own terminal row to its ledger
        # and writes the review to its output file; simulate both here so
        # the dispatch's offset-based wait and stale-output guard both see
        # fresh, matching evidence.
        reviewer_dispatch_cmd=_fake_reviewer_dispatch_cmd(
            prompt_path, ledger_path, output_path, '{"findings": []}',
        ),
        reviewer_prompt_paths={"cursor-reviewer": prompt_path},
        reviewer_output_paths={"cursor-reviewer": output_path},
    )

    ops = integrator.default_ops(cfg)
    result = ops["dispatch_reviewer"]("cursor-reviewer", "review this diff")
    assert result["ok"] is True
    assert result["output"] == '{"findings": []}'
    written_prompt = (run_dir / "cursor-reviewer.prompt.md").read_text()
    assert written_prompt.startswith("<!-- integrator-dispatch:")
    assert written_prompt.endswith("review this diff")
    copy_dir = os.path.join(local_root, "reports", "integrator", cfg["project"], "reviewer-prompts")
    assert os.path.isdir(copy_dir) and os.listdir(copy_dir)


def test_default_ops_dispatch_reviewer_fails_when_ledger_terminal_event_is_not_done(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    cfg = base_config(
        tmp_path, roster_path,
        local_root=local_root,
        reviewer_dispatch_cmd=["/bin/sh", "-c", "echo 2099-01-01T00:00:00Z ERROR reason=crashed >> " + ledger_path],
        reviewer_prompt_paths={"cursor-reviewer": str(run_dir / "cursor-reviewer.prompt.md")},
        reviewer_output_paths={"cursor-reviewer": str(run_dir / "cursor-reviewer.out")},
    )

    ops = integrator.default_ops(cfg)
    result = ops["dispatch_reviewer"]("cursor-reviewer", "review this diff")
    assert result["ok"] is False


def test_default_ops_dispatch_reviewer_refuses_when_no_mapping_configured(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    local_root = str(tmp_path / "local")
    cfg = base_config(
        tmp_path, roster_path,
        local_root=local_root,
        reviewer_dispatch_cmd=["/bin/sh", "-c", "true"],
        reviewer_prompt_paths={"workflow-reviewer": str(tmp_path / "workflow-reviewer.prompt.md")},
        reviewer_output_paths={"workflow-reviewer": str(tmp_path / "workflow-reviewer.out")},
    )
    ops = integrator.default_ops(cfg)
    with pytest.raises(integrator.IntegratorError):
        ops["dispatch_reviewer"]("cursor-reviewer", "review this diff")


# --------------------------------------------------------------------------
# wf-265 recovery 6, finding 1 — a SKIP result (lock held by another
# concurrently running shift of the same reviewer) is a retry-later
# outcome, never a completed review; and finding 2 — an empty output after
# DONE fails the review rather than parsing as no findings.
# --------------------------------------------------------------------------


def test_default_ops_dispatch_reviewer_retries_when_dispatch_output_says_lock_held(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    local_root = str(tmp_path / "local")
    cfg = base_config(
        tmp_path, roster_path,
        local_root=local_root,
        reviewer_dispatch_cmd=["/bin/sh", "-c", "echo 'skip: lock held (age 12s)'"],
        reviewer_prompt_paths={"cursor-reviewer": str(run_dir / "cursor-reviewer.prompt.md")},
        reviewer_output_paths={"cursor-reviewer": str(run_dir / "cursor-reviewer.out")},
    )
    ops = integrator.default_ops(cfg)
    result = ops["dispatch_reviewer"]("cursor-reviewer", "review this diff")
    assert result["ok"] is False
    assert result["retry"] is True


def test_default_ops_dispatch_reviewer_retries_on_skip_ledger_row_without_start(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    # Another shift of the same reviewer is mid-flight (already past START,
    # before our offset) and its DONE lands after our offset — the dispatch
    # command itself hit the lock and appended SKIP without ever reaching
    # its own START.
    with open(ledger_path, "w") as fh:
        fh.write("%s START\n" % integrator._utc_iso_z())
    cfg = base_config(
        tmp_path, roster_path,
        local_root=local_root,
        reviewer_dispatch_cmd=[
            "/bin/sh", "-c",
            "printf '%s SKIP reason=lock-held\\n' \"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\" >> " + ledger_path
            + " && printf '%s DONE\\n' \"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)\" >> " + ledger_path,
        ],
        reviewer_prompt_paths={"cursor-reviewer": str(run_dir / "cursor-reviewer.prompt.md")},
        reviewer_output_paths={"cursor-reviewer": str(run_dir / "cursor-reviewer.out")},
    )
    (run_dir / "cursor-reviewer.out").write_text("stale output from the other shift")

    ops = integrator.default_ops(cfg)
    result = ops["dispatch_reviewer"]("cursor-reviewer", "review this diff")
    assert result["ok"] is False
    assert result["retry"] is True


def test_default_ops_dispatch_reviewer_fails_review_empty_on_blank_output(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    prompt_path = str(run_dir / "cursor-reviewer.prompt.md")
    output_path = str(run_dir / "cursor-reviewer.out")
    cfg = base_config(
        tmp_path, roster_path,
        local_root=local_root,
        reviewer_dispatch_cmd=_fake_reviewer_dispatch_cmd(
            prompt_path, ledger_path, output_path, "   \n  ",
        ),
        reviewer_prompt_paths={"cursor-reviewer": prompt_path},
        reviewer_output_paths={"cursor-reviewer": output_path},
    )

    ops = integrator.default_ops(cfg)
    result = ops["dispatch_reviewer"]("cursor-reviewer", "review this diff")
    assert result["ok"] is False
    assert result["empty"] is True


# --------------------------------------------------------------------------
# wf-265 recovery 7, finding 3 — an output file untouched after DONE (still
# carrying whatever an earlier, unrelated dispatch left in it) fails as
# review_stale rather than being read as this dispatch's review.
# --------------------------------------------------------------------------


def test_default_ops_dispatch_reviewer_fails_review_stale_when_output_unchanged_after_done(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    prompt_path = str(run_dir / "cursor-reviewer.prompt.md")
    output_path = str(run_dir / "cursor-reviewer.out")
    (run_dir / "cursor-reviewer.out").write_text("stale findings from an earlier dispatch")
    cfg = base_config(
        tmp_path, roster_path,
        local_root=local_root,
        # This dispatch's own START/DONE land correctly (prompt_sha
        # matches), but the reviewer never rewrote its output file — the
        # leftover content from an earlier, unrelated dispatch must never
        # be read as this dispatch's review.
        reviewer_dispatch_cmd=_fake_reviewer_dispatch_cmd(prompt_path, ledger_path, output_path),
        reviewer_prompt_paths={"cursor-reviewer": prompt_path},
        reviewer_output_paths={"cursor-reviewer": output_path},
    )

    ops = integrator.default_ops(cfg)
    result = ops["dispatch_reviewer"]("cursor-reviewer", "review this diff")
    assert result["ok"] is False
    assert result["stale"] is True


def test_default_ops_dispatch_reviewer_accepts_output_when_size_unchanged_but_mtime_changed(tmp_path):
    # Same-length findings text as the stale leftover is not itself grounds
    # for review_stale as long as the file was genuinely rewritten (mtime
    # moved forward) — only "untouched" output is stale.
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    prompt_path = str(run_dir / "cursor-reviewer.prompt.md")
    output_path = str(run_dir / "cursor-reviewer.out")
    stale_text = "old findings same length"
    fresh_text = "new findings same length"
    assert len(stale_text) == len(fresh_text)
    (run_dir / "cursor-reviewer.out").write_text(stale_text)
    old_mtime = os.path.getmtime(output_path) - 5
    os.utime(output_path, (old_mtime, old_mtime))
    cfg = base_config(
        tmp_path, roster_path,
        local_root=local_root,
        reviewer_dispatch_cmd=_fake_reviewer_dispatch_cmd(
            prompt_path, ledger_path, output_path, fresh_text,
        ),
        reviewer_prompt_paths={"cursor-reviewer": prompt_path},
        reviewer_output_paths={"cursor-reviewer": output_path},
    )

    ops = integrator.default_ops(cfg)
    result = ops["dispatch_reviewer"]("cursor-reviewer", "review this diff")
    assert result["ok"] is True
    assert result["output"] == fresh_text


def test_parse_reviewer_findings_empty_string_is_no_findings():
    # parse_reviewer_findings itself still treats "" as no findings — the
    # empty-output-after-DONE check happens one layer up in dispatch_reviewer
    # / run_one, before parse_reviewer_findings is ever called on it.
    assert integrator.parse_reviewer_findings("") == []


# --------------------------------------------------------------------------
# wf-265 recovery 4 secondary — wait until reviewer_output_path is stable
# (size unchanged across two reads) before parsing
# --------------------------------------------------------------------------


def test_read_stable_file_waits_for_size_to_stop_changing(tmp_path):
    path = str(tmp_path / "out.txt")
    clock = {"t": 0.0}
    calls = {"n": 0}

    def now_fn():
        return clock["t"]

    def sleep_fn(secs):
        clock["t"] += secs
        calls["n"] += 1
        if calls["n"] == 1:
            with open(path, "w") as fh:
                fh.write("partial")
        elif calls["n"] == 2:
            pass  # same size as the previous read — now considered stable

    content = integrator._read_stable_file(
        path, timeout_secs=10, poll_interval_secs=1, now_fn=now_fn, sleep_fn=sleep_fn,
    )
    assert content == "partial"


def test_read_stable_file_times_out_when_file_never_appears(tmp_path):
    path = str(tmp_path / "missing.txt")
    clock = {"t": 0.0}

    def now_fn():
        return clock["t"]

    def sleep_fn(secs):
        clock["t"] += secs

    content = integrator._read_stable_file(
        path, timeout_secs=3, poll_interval_secs=1, now_fn=now_fn, sleep_fn=sleep_fn,
    )
    assert content is None


def test_latest_reviewer_terminal_event_ignores_rows_before_since_offset(tmp_path):
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    # Seed a terminal row (even one stamped "now") *before* capturing the
    # offset — a same-second stale row from an earlier run must never
    # satisfy a later dispatch's wait.
    with open(ledger_path, "w") as fh:
        fh.write("%s DONE\n" % integrator._utc_iso_z())
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    assert integrator.latest_reviewer_terminal_event(local_root, "cursor-reviewer", since_offset) is None

    with open(ledger_path, "a") as fh:
        fh.write("%s START\n" % integrator._utc_iso_z())
        fh.write("%s DONE\n" % integrator._utc_iso_z())
    assert integrator.latest_reviewer_terminal_event(local_root, "cursor-reviewer", since_offset) == "DONE"


def test_latest_reviewer_terminal_event_succeeds_on_done_followed_by_stop(tmp_path):
    # A real engine shift with max_passes 1 appends START, DONE and then its
    # own normal end-of-shift STOP row — that trailing STOP must never flip
    # a successful review into a failure.
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    with open(ledger_path, "w") as fh:
        fh.write("%s START\n" % integrator._utc_iso_z())
        fh.write("%s DONE\n" % integrator._utc_iso_z())
        fh.write("%s STOP\n" % integrator._utc_iso_z())
    assert integrator.latest_reviewer_terminal_event(local_root, "cursor-reviewer", since_offset) == "DONE"


def test_latest_reviewer_terminal_event_fails_on_error_even_after_done(tmp_path):
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    with open(ledger_path, "w") as fh:
        fh.write("%s START\n" % integrator._utc_iso_z())
        fh.write("%s DONE\n" % integrator._utc_iso_z())
        fh.write("%s ERROR reason=crashed\n" % integrator._utc_iso_z())
    assert integrator.latest_reviewer_terminal_event(local_root, "cursor-reviewer", since_offset) == "ERROR"


def test_latest_reviewer_terminal_event_none_on_lone_stop_with_no_done(tmp_path):
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    with open(ledger_path, "w") as fh:
        fh.write("%s STOP\n" % integrator._utc_iso_z())
    assert integrator.latest_reviewer_terminal_event(local_root, "cursor-reviewer", since_offset) is None


def test_latest_reviewer_terminal_event_done_without_start_is_not_accepted(tmp_path):
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    with open(ledger_path, "w") as fh:
        fh.write("%s DONE\n" % integrator._utc_iso_z())
    assert integrator.latest_reviewer_terminal_event(local_root, "cursor-reviewer", since_offset) is None


def test_latest_reviewer_terminal_event_skip_without_start_is_retry_later(tmp_path):
    # workforce dispatch exits 0 and appends SKIP (never reaching START) when
    # the reviewer's own lock is held by another concurrently running shift
    # — that is not this dispatch's review and must be reported as a
    # retry-later "SKIP", not a completed (or failed) review.
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    with open(ledger_path, "w") as fh:
        fh.write("%s SKIP reason=lock-held\n" % integrator._utc_iso_z())
    assert integrator.latest_reviewer_terminal_event(local_root, "cursor-reviewer", since_offset) == "SKIP"


def test_latest_reviewer_terminal_event_skip_wins_over_other_shifts_done(tmp_path):
    # wf-265 recovery 7, finding 1 — START(A) -> SKIP(B) -> DONE(A), all
    # after B's dispatch offset: B's own dispatch never started (it hit the
    # reviewer's lock, held by A's already-running shift) and must be
    # reported as SKIP (retry later), never as DONE just because *some*
    # START and DONE rows both landed in the post-offset tail.
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    with open(ledger_path, "w") as fh:
        fh.write("%s START prompt_sha=aaaaaaaaaaaaaaaa\n" % integrator._utc_iso_z())
        fh.write("%s SKIP reason=lock-held\n" % integrator._utc_iso_z())
        fh.write("%s DONE\n" % integrator._utc_iso_z())
    assert integrator.latest_reviewer_terminal_event(
        local_root, "cursor-reviewer", since_offset, expected_prompt_sha="bbbbbbbbbbbbbbbb",
    ) == "SKIP"


def test_latest_reviewer_terminal_event_requires_matching_prompt_sha_for_done(tmp_path):
    # A START row for a *different* dispatch's prompt (a concurrently
    # running shift that started before this dispatch and finishes after
    # its offset) must never let that shift's DONE satisfy this dispatch's
    # wait, even with no SKIP anywhere in the tail.
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    with open(ledger_path, "w") as fh:
        fh.write("%s START prompt_sha=aaaaaaaaaaaaaaaa\n" % integrator._utc_iso_z())
        fh.write("%s DONE\n" % integrator._utc_iso_z())
    assert integrator.latest_reviewer_terminal_event(
        local_root, "cursor-reviewer", since_offset, expected_prompt_sha="bbbbbbbbbbbbbbbb",
    ) is None


def test_latest_reviewer_terminal_event_accepts_done_when_prompt_sha_matches(tmp_path):
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "cursor-reviewer.log")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    with open(ledger_path, "w") as fh:
        fh.write("%s START prompt_sha=othershift0000000\n" % integrator._utc_iso_z())
        fh.write("%s DONE\n" % integrator._utc_iso_z())
        fh.write("%s START prompt_sha=aaaaaaaaaaaaaaaa\n" % integrator._utc_iso_z())
        fh.write("%s DONE\n" % integrator._utc_iso_z())
    assert integrator.latest_reviewer_terminal_event(
        local_root, "cursor-reviewer", since_offset, expected_prompt_sha="aaaaaaaaaaaaaaaa",
    ) == "DONE"


def test_wait_for_reviewer_ledger_polls_until_terminal_row_or_timeout(tmp_path):
    local_root = str(tmp_path / "local")
    ledger_dir = os.path.join(local_root, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    with open(os.path.join(ledger_dir, "cursor-reviewer.log"), "w") as fh:
        fh.write("2020-01-01T00:00:00Z DONE\n")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    clock = {"t": 0.0}
    sleeps = []

    def now_fn():
        return clock["t"]

    def sleep_fn(secs):
        sleeps.append(secs)
        clock["t"] += secs
        if len(sleeps) == 1:
            with open(os.path.join(ledger_dir, "cursor-reviewer.log"), "a") as fh:
                fh.write("%s START\n" % integrator._utc_iso_z())
        if len(sleeps) == 2:
            with open(os.path.join(ledger_dir, "cursor-reviewer.log"), "a") as fh:
                fh.write("%s DONE\n" % integrator._utc_iso_z())

    event = integrator.wait_for_reviewer_ledger(
        local_root, "cursor-reviewer", since_offset,
        timeout_secs=100, poll_interval_secs=1, now_fn=now_fn, sleep_fn=sleep_fn,
    )
    assert event == "DONE"
    assert len(sleeps) == 2


def test_wait_for_reviewer_ledger_times_out_without_terminal_row(tmp_path):
    local_root = str(tmp_path / "local")
    since_offset = integrator.reviewer_ledger_offset(local_root, "cursor-reviewer")
    clock = {"t": 0.0}

    def now_fn():
        return clock["t"]

    def sleep_fn(secs):
        clock["t"] += secs

    event = integrator.wait_for_reviewer_ledger(
        local_root, "cursor-reviewer", since_offset,
        timeout_secs=5, poll_interval_secs=2, now_fn=now_fn, sleep_fn=sleep_fn,
    )
    assert event is None


# --------------------------------------------------------------------------
# wf-265 recovery 3, finding 4 — {version}/{release_root}/{checkout}
# placeholders in stage/activate/verify/screenshot commands
# --------------------------------------------------------------------------


def test_substitute_placeholders_replaces_all_three():
    argv = ["/bin/tool", "--version={version}", "--out={release_root}", "--src={checkout}"]
    out = integrator.substitute_placeholders(
        argv, version="1.2.3", release_root="/releases/1.2.3", checkout="/checkout",
    )
    assert out == [
        "/bin/tool", "--version=1.2.3", "--out=/releases/1.2.3", "--src=/checkout",
    ]


def test_default_ops_stage_activate_verify_screenshot_receive_placeholders(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    local_root = str(tmp_path / "local")
    main_checkout = tmp_path / "main_checkout"
    main_checkout.mkdir(exist_ok=True)
    cfg = base_config(
        tmp_path, roster_path,
        local_root=local_root,
        main_checkout=str(main_checkout),
        release_root=str(tmp_path / "releases"),
        stage_cmd=["/bin/sh", "-c", 'printf "%s" "$1" > "%s/stage.txt"' % ("{version}", str(out_dir)), "_", "{version}"],
        activate_cmd=["/bin/sh", "-c", 'printf "%s" "$1" > "%s/activate.txt"' % ("{release_root}", str(out_dir)), "_", "{release_root}"],
        verify_cmd=["/bin/sh", "-c", "printf '%s' \"$1\"", "_", "{version}"],
        screenshot_cmd=["/bin/sh", "-c", "printf '%s\\n' \"$1\"", "_", "{checkout}"],
    )
    ops = integrator.default_ops(cfg)
    ctx = {"version": "9.9.9", "checkout": str(main_checkout)}

    stage = ops["run_stage"](ctx)
    assert stage["rc"] == 0
    assert (out_dir / "stage.txt").read_text() == "9.9.9"

    activate = ops["run_activate"](ctx)
    assert activate["rc"] == 0
    assert (out_dir / "activate.txt").read_text() == os.path.join(str(tmp_path / "releases"), "9.9.9")

    verified = ops["verify_installed_version"]("9.9.9", ctx)
    assert verified["ok"] is True

    shots = ops["capture_screenshots"](ctx)
    assert shots == [str(main_checkout)]


def test_default_ops_dispatch_reviewer_passes_file_and_data_dir_env(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    local_root = str(tmp_path / "local")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cfg = base_config(
        tmp_path, roster_path, local_root=local_root,
        reviewer_dispatch_cmd=["workforce", "dispatch", "{reviewer}"],
        reviewer_prompt_paths={"cursor-reviewer": str(run_dir / "cursor-reviewer.prompt.md")},
        reviewer_output_paths={"cursor-reviewer": str(run_dir / "cursor-reviewer.out")},
    )
    captured = {}

    real_run = integrator._run

    def spy_run(argv, cwd=None, input_text=None, env=None):
        captured["argv"] = list(argv)
        captured["env"] = env
        # rc != 0 so dispatch_reviewer returns before waiting on the
        # reviewer's own ledger — this test only pins the dispatch argv/env.
        return {"rc": 1, "output": ""}

    import workforce.integrator as integrator_mod
    integrator_mod._run = spy_run
    try:
        ops = integrator.default_ops(cfg)
        result = ops["dispatch_reviewer"]("cursor-reviewer", "review this diff")
    finally:
        integrator_mod._run = real_run

    assert result["ok"] is False
    assert "--file" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--file") + 1] == cfg["roster_path"]
    assert captured["env"]["WORKFORCE_DATA_DIR"] == str(Path(local_root).parent)


# --------------------------------------------------------------------------
# wf-265 recovery 4 — sync_main_checkout must check the rc of fetch,
# checkout and reset --hard, never trust a stale rev-parse HEAD
# --------------------------------------------------------------------------


def _git(args, cwd):
    _git_env = dict(os.environ, GIT_AUTHOR_NAME='integrator-test', GIT_AUTHOR_EMAIL='integrator-test@example.invalid',
                   GIT_COMMITTER_NAME='integrator-test', GIT_COMMITTER_EMAIL='integrator-test@example.invalid')
    import subprocess
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, env=_git_env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _init_bare_remote_and_clone(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare"], cwd=str(remote))
    clone = tmp_path / "main_checkout"
    _git(["clone", str(remote), str(clone)], cwd=str(tmp_path))
    _git(["config", "user.email", "t@example.com"], cwd=str(clone))
    _git(["config", "user.name", "t"], cwd=str(clone))
    (clone / "README.md").write_text("hi\n")
    _git(["add", "."], cwd=str(clone))
    _git(["commit", "-m", "init"], cwd=str(clone))
    _git(["push", "-u", "origin", "HEAD:main"], cwd=str(clone))
    return remote, clone


def test_default_ops_sync_main_checkout_returns_sha_on_clean_sync(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    remote, clone = _init_bare_remote_and_clone(tmp_path)
    cfg = base_config(tmp_path, roster_path, main_checkout=str(clone))
    ops = integrator.default_ops(cfg)
    result = ops["sync_main_checkout"](str(clone), "main")
    assert result["rc"] == 0
    assert result["sha"] == _git(["rev-parse", "HEAD"], cwd=str(clone))


def test_default_ops_sync_main_checkout_returns_empty_sha_when_fetch_fails(tmp_path, monkeypatch):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    remote, clone = _init_bare_remote_and_clone(tmp_path)
    cfg = base_config(tmp_path, roster_path, main_checkout=str(clone))

    real_run = integrator._run

    def spy_run(argv, cwd=None, input_text=None, env=None):
        if list(argv[:2]) == ["git", "fetch"]:
            return {"rc": 1, "output": "fetch failed"}
        return real_run(argv, cwd=cwd, input_text=input_text, env=env)

    import workforce.integrator as integrator_mod
    integrator_mod._run = spy_run
    try:
        ops = integrator.default_ops(cfg)
        result = ops["sync_main_checkout"](str(clone), "main")
    finally:
        integrator_mod._run = real_run
    assert result["rc"] != 0
    assert result["sha"] == ""


def test_default_ops_sync_main_checkout_returns_empty_sha_when_reset_fails(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    remote, clone = _init_bare_remote_and_clone(tmp_path)
    cfg = base_config(tmp_path, roster_path, main_checkout=str(clone))

    real_run = integrator._run

    def spy_run(argv, cwd=None, input_text=None, env=None):
        if list(argv[:3]) == ["git", "reset", "--hard"]:
            return {"rc": 1, "output": "reset failed"}
        return real_run(argv, cwd=cwd, input_text=input_text, env=env)

    import workforce.integrator as integrator_mod
    integrator_mod._run = spy_run
    try:
        ops = integrator.default_ops(cfg)
        result = ops["sync_main_checkout"](str(clone), "main")
    finally:
        integrator_mod._run = real_run
    assert result["rc"] != 0
    assert result["sha"] == ""


# --------------------------------------------------------------------------
# wf-265 recovery 3, finding 5 — dispatch_recovery passes --file roster_path
# and WORKFORCE_DATA_DIR; checkout prefers the Workdir: line of the seat's
# Owner claim comment over checkout_template
# --------------------------------------------------------------------------


def test_default_ops_dispatch_recovery_passes_file_and_data_dir_env(tmp_path):
    roster_path = write_roster(tmp_path, [make_worker(tmp_path)])
    local_root = str(tmp_path / "local")
    cfg = base_config(tmp_path, roster_path, local_root=local_root)
    captured = {}

    real_run = integrator._run

    def spy_run(argv, cwd=None, input_text=None, env=None):
        captured["argv"] = list(argv)
        captured["env"] = env
        return {"rc": 0, "output": ""}

    import workforce.integrator as integrator_mod
    integrator_mod._run = spy_run
    try:
        ops = integrator.default_ops(cfg)
        ops["dispatch_recovery"]("tester", "/abs/preparation.json", "suites failed")
    finally:
        integrator_mod._run = real_run

    assert "--file" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--file") + 1] == cfg["roster_path"]
    assert captured["env"]["WORKFORCE_DATA_DIR"] == str(Path(local_root).parent)


def test_workdir_from_comments_prefers_latest_owner_claim():
    comments = [
        {"body": "Intake: filed by you", "author": "you"},
        {"body": "Owner: tester\nWorkdir: /old/checkout\nStart: x", "author": "tester"},
        {"body": "Parked: done", "author": "tester"},
        {"body": "Owner: tester\nWorkdir: /new/checkout\nStart: y", "author": "tester"},
    ]
    assert integrator.workdir_from_comments(comments) == "/new/checkout"
    assert integrator.workdir_from_comments(comments, seat="tester") == "/new/checkout"


def test_workdir_from_comments_none_when_absent():
    assert integrator.workdir_from_comments([{"body": "Intake: filed by you"}]) is None
    assert integrator.workdir_from_comments(None) is None


def test_workdir_from_comments_ignores_claim_not_authored_by_seat():
    comments = [
        {"body": "Owner: other-seat\nWorkdir: /spoofed/checkout\nStart: x", "author": "other-seat"},
    ]
    assert integrator.workdir_from_comments(comments, seat="tester") is None


def test_workdir_from_comments_ignores_relative_path():
    comments = [
        {"body": "Owner: tester\nWorkdir: relative/checkout\nStart: x", "author": "tester"},
    ]
    assert integrator.workdir_from_comments(comments, seat="tester") is None


# --------------------------------------------------------------------------
# wf-265 recovery 5, finding 2 — workdir_from_comments only honours a
# Workdir ending with /<task_id>/checkout or matching the template
# expansion for that worker+task; otherwise falls back to the template
# --------------------------------------------------------------------------


def test_workdir_from_comments_accepts_path_ending_with_task_id_checkout(tmp_path):
    # A task-id-suffixed path that is *not* the template expansion is only
    # accepted when it is a real git worktree checked out to the expected
    # task branch (wf-265 recovery 6, finding 3).
    checkout = tmp_path / "some" / "tree" / "wf-265" / "checkout"
    checkout.mkdir(parents=True)
    _git(["init", "-q"], str(checkout))
    _git(["checkout", "-q", "-b", "workforce/task/tester/wf-265"], str(checkout))
    comments = [
        {"body": "Owner: tester\nWorkdir: %s\nStart: x" % checkout, "author": "tester"},
    ]
    assert integrator.workdir_from_comments(
        comments, seat="tester", task_id="wf-265",
        branch_template="workforce/task/{worker}/{task_id}",
    ) == str(checkout)


def test_workdir_from_comments_rejects_task_id_suffix_on_wrong_branch(tmp_path):
    checkout = tmp_path / "some" / "tree" / "wf-265" / "checkout"
    checkout.mkdir(parents=True)
    _git(["init", "-q"], str(checkout))
    _git(["checkout", "-q", "-b", "some-other-branch"], str(checkout))
    comments = [
        {"body": "Owner: tester\nWorkdir: %s\nStart: x" % checkout, "author": "tester"},
    ]
    assert integrator.workdir_from_comments(
        comments, seat="tester", task_id="wf-265",
        branch_template="workforce/task/{worker}/{task_id}",
    ) is None


def test_workdir_from_comments_rejects_task_id_suffix_when_directory_missing():
    comments = [
        {"body": "Owner: tester\nWorkdir: /no/such/tree/wf-265/checkout\nStart: x", "author": "tester"},
    ]
    assert integrator.workdir_from_comments(
        comments, seat="tester", task_id="wf-265",
        branch_template="workforce/task/{worker}/{task_id}",
    ) is None


def test_workdir_from_comments_accepts_path_matching_template_expansion(tmp_path):
    # The template arm must be branch-verified exactly like the suffix arm
    # (wf-265 recovery 7, finding 2) — a real worktree on the expected
    # branch at the exact template path is accepted.
    workspace_root = tmp_path / "root"
    checkout = workspace_root / "local" / "task-runs" / "tester" / "wf-265" / "checkout"
    checkout.mkdir(parents=True)
    _git(["init", "-q"], str(checkout))
    _git(["checkout", "-q", "-b", "workforce/task/tester/wf-265"], str(checkout))
    comments = [
        {"body": "Owner: tester\nWorkdir: %s\nStart: x" % checkout, "author": "tester"},
    ]
    assert integrator.workdir_from_comments(
        comments, seat="tester", task_id="wf-265",
        checkout_template="local/task-runs/{worker}/{task_id}/checkout",
        workspace_root=str(workspace_root),
        branch_template="workforce/task/{worker}/{task_id}",
    ) == str(checkout)


def test_workdir_from_comments_rejects_template_expansion_on_repointed_branch(tmp_path):
    # A template-expansion path that exists as a git worktree but is
    # repointed to the wrong branch (e.g. reused after a prior checkout was
    # torn down and rebuilt elsewhere) must not be trusted just because it
    # matches the template exactly.
    workspace_root = tmp_path / "root"
    checkout = workspace_root / "local" / "task-runs" / "tester" / "wf-265" / "checkout"
    checkout.mkdir(parents=True)
    _git(["init", "-q"], str(checkout))
    _git(["checkout", "-q", "-b", "some-other-branch"], str(checkout))
    comments = [
        {"body": "Owner: tester\nWorkdir: %s\nStart: x" % checkout, "author": "tester"},
    ]
    assert integrator.workdir_from_comments(
        comments, seat="tester", task_id="wf-265",
        checkout_template="local/task-runs/{worker}/{task_id}/checkout",
        workspace_root=str(workspace_root),
        branch_template="workforce/task/{worker}/{task_id}",
    ) is None


def test_workdir_from_comments_rejects_template_expansion_when_detached(tmp_path):
    workspace_root = tmp_path / "root"
    checkout = workspace_root / "local" / "task-runs" / "tester" / "wf-265" / "checkout"
    checkout.mkdir(parents=True)
    _git(["init", "-q"], str(checkout))
    _git(["commit", "--allow-empty", "-q", "-m", "x"], str(checkout))
    sha = _git(["rev-parse", "HEAD"], str(checkout))
    _git(["checkout", "-q", sha], str(checkout))
    comments = [
        {"body": "Owner: tester\nWorkdir: %s\nStart: x" % checkout, "author": "tester"},
    ]
    assert integrator.workdir_from_comments(
        comments, seat="tester", task_id="wf-265",
        checkout_template="local/task-runs/{worker}/{task_id}/checkout",
        workspace_root=str(workspace_root),
        branch_template="workforce/task/{worker}/{task_id}",
    ) is None


def test_workdir_from_comments_rejects_template_expansion_without_branch_template(tmp_path):
    # No branch_template configured means the template arm can never be
    # verified, so it must never be trusted blindly either.
    workspace_root = tmp_path / "root"
    checkout = workspace_root / "local" / "task-runs" / "tester" / "wf-265" / "checkout"
    checkout.mkdir(parents=True)
    _git(["init", "-q"], str(checkout))
    _git(["checkout", "-q", "-b", "workforce/task/tester/wf-265"], str(checkout))
    comments = [
        {"body": "Owner: tester\nWorkdir: %s\nStart: x" % checkout, "author": "tester"},
    ]
    assert integrator.workdir_from_comments(
        comments, seat="tester", task_id="wf-265",
        checkout_template="local/task-runs/{worker}/{task_id}/checkout",
        workspace_root=str(workspace_root),
    ) is None


def test_workdir_from_comments_rejects_stale_task_path():
    # A Workdir left over from an earlier order on the same seat
    # (.../wf-264/checkout) must never redirect wf-265's pipeline.
    comments = [
        {"body": "Owner: tester\nWorkdir: /some/tree/wf-264/checkout\nStart: x", "author": "tester"},
    ]
    assert integrator.workdir_from_comments(
        comments, seat="tester", task_id="wf-265",
        checkout_template="local/task-runs/{worker}/{task_id}/checkout",
        workspace_root="/root",
    ) is None


def test_discover_candidates_uses_workdir_from_owner_comment_over_template(tmp_path):
    w1 = make_worker(tmp_path, name="tester")
    roster_path = write_roster(tmp_path, [w1])
    cfg = make_config(tmp_path, roster_path, active_implementation_cap=5)
    # A real git worktree, task-id-suffixed but not the template expansion —
    # only accepted because it is checked out to the expected task branch.
    checkout = tmp_path / "real" / "wf-1" / "checkout"
    checkout.mkdir(parents=True)
    _git(["init", "-q"], str(checkout))
    _git(["checkout", "-q", "-b", "workforce/task/tester/wf-1"], str(checkout))
    tasks = [
        {
            "id": "wf-1", "labels": ["worker:tester"], "title": "one",
            "comments": [{"body": "Owner: tester\nWorkdir: %s\nStart: x" % checkout, "author": "tester"}],
        },
    ]

    def fake_http(method, url, body=None, timeout=15.0):
        return {"ok": True, "tasks": tasks}

    candidates = integrator.discover_candidates(cfg, http=fake_http)
    assert candidates[0]["checkout_override"] == str(checkout)


def test_discover_candidates_falls_back_to_template_and_records_stale_workdir_mismatch(tmp_path):
    w1 = make_worker(tmp_path, name="tester")
    roster_path = write_roster(tmp_path, [w1])
    local_root = str(tmp_path / "local")
    cfg = make_config(
        tmp_path, roster_path, active_implementation_cap=5, local_root=local_root,
    )
    tasks = [
        {
            "id": "wf-265", "labels": ["worker:tester"], "title": "one",
            "comments": [
                {"body": "Owner: tester\nWorkdir: /some/tree/wf-264/checkout\nStart: x", "author": "tester"},
            ],
        },
    ]

    def fake_http(method, url, body=None, timeout=15.0):
        return {"ok": True, "tasks": tasks}

    candidates = integrator.discover_candidates(cfg, http=fake_http)
    assert candidates[0]["checkout_override"] is None

    ledger_path = os.path.join(local_root, "ledger", "integrator-%s.log" % cfg["project"])
    with open(ledger_path, "r", encoding="utf-8") as fh:
        ledger_text = fh.read()
    assert "workdir_mismatch=/some/tree/wf-264/checkout" in ledger_text

def test_load_config_carries_active_implementation_cap(tmp_path):
    """wf-265 rehearsal: the documented active_implementation_cap key was dropped by
    load_config, so the capacity snapshot fell back to the product default of 1 and
    discovery returned nothing while one seat was in flight."""
    cfg = make_config(tmp_path, str(tmp_path / "roster.json"), active_implementation_cap=2)
    assert cfg["active_implementation_cap"] == 2
    cfg = make_config(tmp_path, str(tmp_path / "roster.json"))
    assert cfg["active_implementation_cap"] is None


def test_load_config_accepts_an_empty_screenshot_list(tmp_path):
    """The shipped integration_config.example.json carries screenshot_cmd: [] and
    must load; an empty list means no screenshots."""
    cfg = make_config(tmp_path, str(tmp_path / "roster.json"), screenshot_cmd=[])
    assert cfg.get("screenshot_cmd") is None


def test_parse_reviewer_findings_unwraps_a_json_findings_array():
    """Rehearsal on pc-1492: the reviewer answered the generated prompt with a JSON
    object holding a findings array; the parser must return one finding per entry."""
    body = 'Reviewing against the spec.\n{"findings": ["boot() drops the item deep link", "renderProjectPanel() loses focus", "#map-reset does not clear focus"]}'
    found = integrator.parse_reviewer_findings(body)
    assert found == ["boot() drops the item deep link", "renderProjectPanel() loses focus", "#map-reset does not clear focus"]
    assert integrator.parse_reviewer_findings('{"findings": []}') == []


def test_workdir_from_comments_accepts_blockquoted_lines(tmp_path):
    """pc-1487 rehearsal: WorkLane renders evidence notes as blockquotes, so the
    seat's Workdir: line arrives as "> Workdir: …" and must still count."""
    checkout = tmp_path / "wf-9" / "checkout"
    checkout.mkdir(parents=True)
    comments = [{"author": "tester", "body": "Evidence note:\n\n> Evidence: commit abc.\n> \n> Workdir: %s\n" % checkout}]
    assert integrator.workdir_from_comments(comments, seat="tester") == str(checkout)


def test_checkout_templates_override_per_worker(tmp_path):
    cfg = make_config(tmp_path, str(tmp_path / "roster.json"),
                      checkout_templates={"cursor-seat": "other/{worker}/{task_id}/checkout"})
    order = {"worker": "cursor-seat", "task_id": "wf-9"}
    assert integrator._checkout_path(cfg, order) == os.path.join(cfg["workspace_root"], "other/cursor-seat/wf-9/checkout")
    order2 = {"worker": "claude-seat", "task_id": "wf-9"}
    assert integrator._checkout_path(cfg, order2).endswith("local/task-runs/claude-seat/wf-9/checkout")


def test_run_one_stops_with_checkout_missing_when_suites_cannot_start(tmp_path, monkeypatch):
    """A missing checkout raises FileNotFoundError from the real run_suites; the
    pass must stop the order with checkout_missing, not die."""
    w = make_worker(tmp_path, name="tester", command=["claude", "-p", "x"])
    roster_path = write_roster(tmp_path, [w])
    cfg = make_config(tmp_path, roster_path)
    posted = []
    def run_suites(checkout):
        raise FileNotFoundError(checkout)
    ops = {"run_suites": run_suites, "post_comment": lambda tid, body: posted.append((tid, body))}
    order = {"task_id": "wf-9", "worker": "tester", "provider": "claude", "title": "t", "checkout_override": None}
    result = integrator.run_one(order, cfg, ops)
    assert result["outcome"] == "checkout_missing"
    assert posted and posted[0][0] == "wf-9"

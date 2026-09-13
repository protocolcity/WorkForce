"""Integrator job (wf-265) — pure policy + orchestration with fake ops."""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import integrator  # noqa: E402
from workforce import roster as roster_mod  # noqa: E402
from workforce.roster import Worker  # noqa: E402


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def make_config(tmp_path, roster_path, **over):
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
    locks_dir = os.path.join(cfg["local_root"], "locks")
    os.makedirs(locks_dir, exist_ok=True)
    with open(os.path.join(locks_dir, "busy-seat.lock"), "w") as fh:
        json.dump({"pid": os.getpid(), "started": integrator._utc_iso_z()}, fh)

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
        self.recovery_calls = []

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
        return "diff --git a/src/x.py b/src/x.py\n+1\n"

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
        output = self.review_output
        if output is None:
            output = json.dumps({"findings": self.review_findings})
        return {"ok": True, "output": output}

    def merge_pr(self, checkout, pr_number):
        self._record("merge_pr")
        return {"rc": self.merge_rc, "output": ""}

    def remote_head_sha(self, checkout, branch):
        self._record("remote_head_sha")
        return self.pre_merge_sha

    def merge_commit_parent_sha(self, checkout, branch):
        self._record("merge_commit_parent_sha")
        return self.merge_parent_sha

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

    def run_stage(self):
        self._record("run_stage")
        return {"rc": self.stage_rc, "output": ""}

    def run_activate(self):
        self._record("run_activate")
        return {"rc": self.activate_rc, "output": ""}

    def verify_installed_version(self, expected):
        self._record("verify_installed_version")
        return {"ok": self.verify_ok, "observed": self.verify_observed or expected}

    def capture_screenshots(self):
        self._record("capture_screenshots")
        return ["shot1.png"]

    def post_comment(self, task_id, body):
        self._record("post_comment")
        return {"ok": True}

    def release_seat(self, task_id, reason):
        self._record("release_seat")
        return {"ok": True}

    def close_order(self, task_id, evidence):
        self._record("close_order")
        return {"ok": True}

    def as_dict(self):
        return {
            "run_suites": self.run_suites,
            "checkout_clean": self.checkout_clean_fn,
            "diff_text": self.diff_text,
            "push_branch": self.push_branch,
            "open_or_update_pr": self.open_or_update_pr,
            "ci_status": self.ci_status_fn,
            "dispatch_reviewer": self.dispatch_reviewer,
            "merge_pr": self.merge_pr,
            "remote_head_sha": self.remote_head_sha,
            "merge_commit_parent_sha": self.merge_commit_parent_sha,
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

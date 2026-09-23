import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from workforce.task_runner import PreparationError, _acquire_lock, exclude_from_git, prepare, recover


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("original\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", "https://example.invalid/product")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("{authority}\nClaim {task_id} as {worker} in {project}. Branch {branch}.")
    law = tmp_path / "AGENTS.md"
    law.write_text("Claim before implementing.")
    monkeypatch.setenv("WL_AGENT_ID", "builder")
    config = dict(project="product", worker="builder", desk_url="http://127.0.0.1:1234",
                  required_label="execution:bounded", repository=str(repo),
                  expected_remote="https://example.invalid/product", base_ref="main",
                  state_dir=str(tmp_path / "runs"), prompt_template=str(prompt),
                  authority_chain=[str(law)], command=["provider", "--cwd", "{checkout}", "{prompt}"])
    task = dict(id="p-1", product="product", status="backlog",
                labels=["worker:builder", "execution:bounded"])
    return config, task


def feed(task):
    return lambda url: {"count": 1, "tasks": [task]}


def test_isolates_two_tasks_and_preserves_primary_wip(setup):
    config, task = setup
    repo = Path(config["repository"])
    (repo / "README.md").write_text("private host WIP\n")
    first = prepare(config, feed(task))
    second = prepare(config, feed(dict(task, id="p-2")))
    for result in (first, second):
        checkout = Path(result["checkout"])
        assert (checkout / "README.md").read_text() == "original\n"
        receipt = json.loads(Path(result["receipt"]).read_text())
        assert receipt["claimed"] is False
        assert receipt["state"] == "prepared"
        assert git(checkout, "branch", "--show-current") == receipt["branch"]
        assert result["task_id"] in result["argv"][-1]
    assert first["checkout"] != second["checkout"]
    assert (repo / "README.md").read_text() == "private host WIP\n"


def test_repeat_dispatch_preserves_dirty_checkout(setup):
    config, task = setup
    first = prepare(config, feed(task))
    work = Path(first["checkout"]) / "README.md"
    work.write_text("unfinished agent work\n")
    with pytest.raises(PreparationError, match="already prepared"):
        prepare(config, feed(task))
    assert work.read_text() == "unfinished agent work\n"


@pytest.mark.parametrize("change", [
    {"product": "other"}, {"status": "in_progress"}, {"gate_type": "human"},
    {"gate_type": "deferred"}, {"gate_type": "tracking"},
    {"labels": ["worker:other", "execution:bounded"]},
    {"labels": ["worker:builder", "worker:other", "execution:bounded"]},
    {"id": "../../escape"},
])
def test_rejects_foreign_or_unsafe_work(setup, change):
    config, task = setup
    with pytest.raises(PreparationError):
        prepare(config, feed(dict(task, **change)))
    assert not Path(config["state_dir"]).exists()


def test_empty_and_unapproved_feed_launch_nothing(setup):
    config, task = setup
    assert prepare(config, lambda url: {"count": 0, "tasks": []}) is None
    task["labels"] = ["worker:builder"]
    assert prepare(config, feed(task)) is None
    assert not Path(config["state_dir"]).exists()


@pytest.mark.parametrize("data", [{}, {"count": 2, "tasks": []}, {"count": True, "tasks": []}, {"ok": False, "count": 0, "tasks": []}])
def test_incomplete_feed_is_error_not_empty(setup, data):
    config, task = setup
    with pytest.raises(PreparationError, match="feed"):
        prepare(config, lambda url: data)


def test_auth_or_destination_failure_never_creates_task_tree(setup):
    config, task = setup
    config["auth_check"] = ["false"]
    with pytest.raises(PreparationError, match="authentication"):
        prepare(config, feed(task))
    del config["auth_check"]
    git(config["repository"], "remote", "set-url", "--push", "origin", "https://example.invalid/private")
    with pytest.raises(PreparationError, match="push destination"):
        prepare(config, feed(task))
    assert not Path(config["state_dir"]).exists()


def test_wrong_identity_refuses_even_empty_feed(setup, monkeypatch):
    config, task = setup
    monkeypatch.setenv("WL_AGENT_ID", "you")
    with pytest.raises(PreparationError, match="identity"):
        prepare(config, lambda url: {"count": 0, "tasks": []})


def test_failed_preparation_leaves_receipt_and_never_resets_branch(setup):
    config, task = setup
    git(config["repository"], "branch", "workforce/task/builder/p-1")
    with pytest.raises(PreparationError, match="git preparation"):
        prepare(config, feed(task))
    receipt = Path(config["state_dir"]) / "builder/p-1/preparation.json"
    assert json.loads(receipt.read_text())["state"] == "preparation_failed"
    with pytest.raises(PreparationError, match="already prepared"):
        prepare(config, feed(task))


def test_http_product_envelope_without_per_row_product(setup):
    config, task = setup
    del task["product"]
    result = prepare(config, lambda url: {"ok": True, "product": "product", "count": 1, "tasks": [task]})
    assert result["task_id"] == "p-1"


@pytest.mark.parametrize("envelope", ["foreign", None])
def test_missing_or_foreign_product_envelope_fails(setup, envelope):
    config, task = setup
    del task["product"]
    with pytest.raises(PreparationError):
        prepare(config, lambda url: {"product": envelope, "count": 1, "tasks": [task]})


def test_conflicting_envelope_and_row_fail(setup):
    config, task = setup
    with pytest.raises(PreparationError):
        prepare(config, lambda url: {"product": "foreign", "count": 1, "tasks": [task]})


def test_recover_requires_reason_and_known_receipt(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    with pytest.raises(PreparationError, match="reason"):
        recover(config, prepared["receipt"], "", feed(task))
    with pytest.raises(PreparationError, match="reason"):
        recover(config, prepared["receipt"], "   ", feed(task))


def test_recover_rejects_receipt_outside_state_dir(setup, tmp_path):
    config, task = setup
    prepared = prepare(config, feed(task))
    foreign = tmp_path / "elsewhere.json"
    foreign.write_text(Path(prepared["receipt"]).read_text())
    with pytest.raises(PreparationError, match="state directory"):
        recover(config, foreign, "resume after crash", feed(task))


def test_recover_rejects_missing_receipt(setup):
    config, task = setup
    missing = Path(config["state_dir"]) / "builder" / "p-1" / "preparation.json"
    with pytest.raises(PreparationError, match="does not exist"):
        recover(config, missing, "resume after crash", feed(task))


# ---------------------------------------------------------------- wf-279 routing_policy seam

import datetime as _dt  # noqa: E402


def _routing_now():
    return _dt.datetime.now(_dt.timezone.utc)


def _routing_iso_ago(**delta):
    return (_routing_now() - _dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _routing_iso_ahead(**delta):
    return (_routing_now() + _dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


ROUTING_HOST = "test-host"


def _write_routing_policy(tmp_path, seats, evaluation_results=None):
    doc = {"version": 1, "seats": seats, "evaluation_results": evaluation_results or []}
    path = tmp_path / "routing_policy.json"
    path.write_text(json.dumps(doc))
    return str(path)


def _bind_test_policy(config):
    from workforce.routing_binding import runner_digest
    p = Path(config["routing_policy"])
    raw = json.loads(p.read_text())
    for row in raw["seats"]:
        row.update(runner_sha256=runner_digest(config), max_run_units=1, budget_units="requests")
    p.write_text(json.dumps(raw))


def _routing_seat_row(worker="builder", project="product", **over):
    spec = dict(
        worker=worker, provider="claude", model="sonnet-5",
        reasoning_effort="medium", host=ROUTING_HOST, tools=["wl_show"],
        project=project, account_state="authenticated",
        observed_at=_routing_iso_ago(hours=1), expires_at=_routing_iso_ahead(hours=1),
        supported_efforts=["low", "medium", "high"], version=1,
        quota={"pool": "subscription", "pool_id": "acct-1", "units": "requests",
               "observed_at": _routing_iso_ago(minutes=2), "remaining": 5},
    )
    spec.update(over)
    return spec


def _bounded_edit_result(candidate_id):
    return {"candidate_id": candidate_id, "task_id": "edit-01", "accepted": True,
            "regressions": 0, "retries": 0, "observed_at": _routing_iso_ago(minutes=2)}


def test_routing_policy_requires_routing_host(setup, tmp_path):
    config, task = setup
    task["labels"] = ["worker:builder", "execution:bounded", "work-kind:implement", "risk:low"]
    config["routing_policy"] = _write_routing_policy(tmp_path, [_routing_seat_row()])
    with pytest.raises(PreparationError, match="routing_host"):
        prepare(config, feed(task))
    assert not Path(config["state_dir"]).exists()


def test_routing_policy_refuses_and_prepares_nothing(setup, tmp_path):
    """A configured routing_policy that refuses the head-of-queue task must
    stop before any reservation/worktree/provider launch is created --
    proving zero provider invocation, not merely a returned refusal."""
    config, task = setup
    task["labels"] = ["worker:builder", "execution:bounded", "work-kind:implement", "risk:low"]
    # No matching evidence for worker "builder" at all -> refuses.
    config["routing_policy"] = _write_routing_policy(tmp_path, [_routing_seat_row(worker="someone-else")])
    config["routing_host"] = ROUTING_HOST
    _bind_test_policy(config)
    with pytest.raises(ValueError, match="unambiguous"):
        prepare(config, feed(task))
    assert not Path(config["state_dir"]).exists()


def test_routing_policy_unqualified_seat_prepares_nothing(setup, tmp_path):
    config, task = setup
    task["labels"] = ["worker:builder", "execution:bounded", "work-kind:implement", "risk:low"]
    config["routing_policy"] = _write_routing_policy(
        tmp_path, [_routing_seat_row(account_state="unauthenticated")])
    config["routing_host"] = ROUTING_HOST
    _bind_test_policy(config)
    assert prepare(config, feed(task)) is None
    assert not Path(config["state_dir"]).exists()


def test_routing_policy_qualified_seat_prepares_normally(setup, tmp_path):
    config, task = setup
    task["labels"] = ["worker:builder", "execution:bounded", "work-kind:implement", "risk:low"]
    candidate_id = "claude/sonnet-5@medium#%s::product" % ROUTING_HOST
    config["routing_policy"] = _write_routing_policy(
        tmp_path, [_routing_seat_row()], evaluation_results=[_bounded_edit_result(candidate_id)])
    config["routing_host"] = ROUTING_HOST
    _bind_test_policy(config)
    result = prepare(config, feed(task))
    assert result is not None
    assert result["task_id"] == "p-1"


def test_routing_policy_missing_work_kind_label_prepares_nothing(setup, tmp_path):
    """Existing tasks without a work-kind label (e.g. legacy orders) must
    refuse cleanly, not raise, when a routing_policy is configured."""
    config, task = setup  # setup's task has no work-kind label
    candidate_id = "claude/sonnet-5@medium#%s::product" % ROUTING_HOST
    config["routing_policy"] = _write_routing_policy(
        tmp_path, [_routing_seat_row()], evaluation_results=[_bounded_edit_result(candidate_id)])
    config["routing_host"] = ROUTING_HOST
    _bind_test_policy(config)
    assert prepare(config, feed(task)) is None
    assert not Path(config["state_dir"]).exists()


def test_no_routing_policy_configured_keeps_legacy_eligible_zero_selection(setup):
    """Absent routing_policy, behavior is unchanged from before wf-279's
    integration: the head-of-queue task launches even with no work-kind
    label at all."""
    config, task = setup
    result = prepare(config, feed(task))
    assert result is not None


@pytest.mark.parametrize("change", [
    {"status": "done"}, {"status": "in_progress"}, {"gate_type": "human"},
    {"labels": ["worker:other", "execution:bounded"]},
])
def test_recover_rejects_unready_gated_done_or_wrong_owner(setup, change):
    config, task = setup
    prepared = prepare(config, feed(task))
    with pytest.raises(PreparationError):
        recover(config, prepared["receipt"], "resume after crash", feed(dict(task, **change)))


def test_recover_preserves_dirty_checkout_and_writes_unique_attempt(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    checkout = Path(prepared["checkout"])
    (checkout / "README.md").write_text("unfinished agent work\n")
    original_prompt = Path(prepared["receipt"]).parent / "prompt.md"
    original_prompt_text = original_prompt.read_text()

    result = recover(config, prepared["receipt"], "provider crashed mid-run", feed(task))

    assert result["checkout"] == prepared["checkout"]
    assert (checkout / "README.md").read_text() == "unfinished agent work\n"
    assert len(git(checkout, "log", "--oneline").splitlines()) == 1  # original commit preserved
    assert original_prompt.read_text() == original_prompt_text
    assert json.loads(Path(prepared["receipt"]).read_text())["state"] == "prepared"

    receipt = json.loads(Path(result["receipt"]).read_text())
    assert result["receipt"] != prepared["receipt"]
    assert receipt["state"] == "recovered"
    assert receipt["recovery_of"] == prepared["receipt"]
    assert receipt["recovery_reason"] == "provider crashed mid-run"
    assert receipt["claimed"] is False


def test_prepared_prompt_has_no_recovery_reason(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    prompt = Path(prepared["receipt"]).parent / "prompt.md"
    assert "Recovery" not in prompt.read_text()
    assert "recovery_reason" not in prompt.read_text()


def test_recovered_prompt_carries_the_reason_even_without_a_template_slot(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    # Fixture template has no {recovery_reason} slot.
    result = recover(config, prepared["receipt"], "three review findings unaddressed", feed(task))
    prompt = Path(result["receipt"]).parent / "prompt.md"
    text = prompt.read_text()
    assert "three review findings unaddressed" in text
    assert "recovery attempt 1" in text


def test_recovered_prompt_fills_template_slot_when_present(setup, tmp_path):
    config, task = setup
    prompt_path = Path(config["prompt_template"])
    prompt_path.write_text(prompt_path.read_text() + " Recovery reason: {recovery_reason} (attempt {recovery_attempt}, of {recovery_of}).")
    prepared = prepare(config, feed(task))
    result = recover(config, prepared["receipt"], "provider crashed mid-run", feed(task))
    text = Path(result["receipt"]).parent / "prompt.md"
    rendered = text.read_text()
    assert "Recovery reason: provider crashed mid-run (attempt 1, of %s)" % prepared["receipt"] in rendered


def test_recovered_prompt_survives_hostile_reason(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    hostile = 'fix %s and %(x)s\nline two\n"quoted" and \'single\' {braces}'
    result = recover(config, prepared["receipt"], hostile, feed(task))
    prompt = Path(result["receipt"]).parent / "prompt.md"
    text = prompt.read_text()
    assert text.count("Recovery: this is recovery attempt 1") == 1
    assert "\nline two\n" not in text
    assert "fix %s and %(x)s line two" in text
    # Full reason (newlines and all) is preserved in the receipt untouched.
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["recovery_reason"] == hostile


def test_recover_twice_never_overwrites_prior_attempt(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    first = recover(config, prepared["receipt"], "first crash", feed(task))
    second = recover(config, prepared["receipt"], "second crash", feed(task))
    assert first["receipt"] != second["receipt"]
    assert Path(first["receipt"]).exists()
    assert Path(second["receipt"]).exists()
    assert json.loads(Path(first["receipt"]).read_text())["recovery_reason"] == "first crash"
    assert json.loads(Path(second["receipt"]).read_text())["recovery_reason"] == "second crash"


def test_recover_rejects_tampered_checkout_path(setup, tmp_path):
    config, task = setup
    prepared = prepare(config, feed(task))
    receipt_path = Path(prepared["receipt"])
    receipt = json.loads(receipt_path.read_text())
    other_dir = tmp_path / "runs" / "elsewhere"
    other_dir.mkdir(parents=True)
    receipt["checkout"] = str(other_dir)
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(PreparationError, match="worktree"):
        recover(config, receipt_path, "resume after crash", feed(task))


def test_recover_rejects_wrong_remote(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    git(config["repository"], "remote", "set-url", "--push", "origin", "https://example.invalid/private")
    with pytest.raises(PreparationError, match="push destination"):
        recover(config, prepared["receipt"], "resume after crash", feed(task))


def test_recover_handoff_to_another_configured_worker(setup, monkeypatch):
    config, task = setup
    prepared = prepare(config, feed(task))

    other_config = dict(config, worker="other")
    monkeypatch.setenv("WL_AGENT_ID", "other")
    handoff_task = dict(task, labels=["worker:other", "execution:bounded"])

    result = recover(other_config, prepared["receipt"], "reassigned after builder went dark", feed(handoff_task))

    assert result["worker"] == "other"
    assert result["checkout"] == prepared["checkout"]
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["worker"] == "other"
    assert receipt["recovery_reason"] == "reassigned after builder went dark"


def test_double_start_lock_excludes_concurrent_launch(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    fd = _acquire_lock(prepared["lock"])
    try:
        with pytest.raises(PreparationError, match="active"):
            _acquire_lock(prepared["lock"])
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_lock_available_again_once_holder_releases(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    fd = _acquire_lock(prepared["lock"])
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    second_fd = _acquire_lock(prepared["lock"])
    fcntl.flock(second_fd, fcntl.LOCK_UN)
    os.close(second_fd)


def test_recover_of_nested_attempt_receipt_resolves_canonical_anchor(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    first = recover(config, prepared["receipt"], "first crash", feed(task))

    second = recover(config, first["receipt"], "second crash, pointed at nested receipt", feed(task))

    assert second["lock"] == prepared["lock"] == first["lock"]
    receipt = json.loads(Path(second["receipt"]).read_text())
    assert receipt["recovery_of"] == prepared["receipt"]  # canonical original, not the nested one
    assert receipt["recovery_source_receipt"] == first["receipt"]
    # Both attempts still live flat under the one reservation, never nested under each other.
    assert Path(second["receipt"]).parent.parent == Path(first["receipt"]).parent.parent


def test_nested_receipt_recovery_is_excluded_by_the_same_lock_as_original(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    first = recover(config, prepared["receipt"], "first crash", feed(task))
    second = recover(config, first["receipt"], "second crash", feed(task))

    fd = _acquire_lock(prepared["lock"])
    try:
        with pytest.raises(PreparationError, match="active"):
            _acquire_lock(second["lock"])
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _make_legacy(receipt_path):
    """Simulate a receipt written before the lock protocol existed."""
    data = json.loads(receipt_path.read_text())
    del data["lock_protocol"]
    receipt_path.write_text(json.dumps(data, indent=2) + "\n")


def test_legacy_receipt_refuses_recovery_without_explicit_acknowledgement(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    _make_legacy(Path(prepared["receipt"]))
    with pytest.raises(PreparationError, match="legacy|lock protocol"):
        recover(config, prepared["receipt"], "resume after crash", feed(task))
    with pytest.raises(PreparationError, match="legacy|lock protocol"):
        recover(config, prepared["receipt"], "resume after crash", feed(task), legacy_stop_evidence="   ")


def test_legacy_receipt_recovers_with_explicit_acknowledgement_and_evidence(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    _make_legacy(Path(prepared["receipt"]))
    evidence = "Operator confirmed via `ps -p 4821` that the prior provider process no longer exists (2026-09-13)."

    result = recover(config, prepared["receipt"], "resume after crash", feed(task),
                      legacy_stop_evidence=evidence)

    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["legacy_stop_acknowledged"] is True
    assert receipt["legacy_stop_evidence"] == evidence
    # The canonical original receipt itself is never rewritten by recovery.
    assert "legacy_stop_acknowledged" not in json.loads(Path(prepared["receipt"]).read_text())


def test_exec_retains_lock_and_releases_only_after_child_process_exits(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    lock_path = prepared["lock"]

    pid = os.fork()
    if pid == 0:  # pragma: no cover -- child branch, exercised but not measured
        try:
            fd = _acquire_lock(lock_path)
            os.set_inheritable(fd, True)
            os.execvp("sleep", ["sleep", "0.5"])
        except Exception:
            os._exit(1)
        os._exit(0)

    try:
        time.sleep(0.15)
        with pytest.raises(PreparationError, match="active"):
            _acquire_lock(lock_path)
    finally:
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0

    fd = _acquire_lock(lock_path)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def test_exclude_from_git_resolves_real_worktree_gitdir_and_is_idempotent(setup):
    config, task = setup
    prepared = prepare(config, feed(task))
    checkout = Path(prepared["checkout"])
    assert (checkout / ".git").is_file()  # a worktree checkout, not a real repo

    exclude_from_git(checkout, [".cursor/", ".grok/"])
    exclude_from_git(checkout, [".cursor/"])  # repeat call must not duplicate

    common = Path(git(Path(config["repository"]), "rev-parse", "--git-common-dir"))
    common = (Path(config["repository"]) / common).resolve() if not common.is_absolute() else common
    worktree_git_dir = next(p for p in (common / "worktrees").iterdir() if p.is_dir())
    exclude_text = (worktree_git_dir / "info" / "exclude").read_text()
    assert exclude_text.count(".cursor/") == 1
    assert exclude_text.count(".grok/") == 1


def test_exclude_from_git_noop_without_git_dir(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    exclude_from_git(checkout, [".cursor/"])  # no .git at all -- must not raise
    assert not (checkout / ".git").exists()


@pytest.mark.parametrize("change", ["model", "task", "head", "instructions", "quota", "budget", "tools"])
def test_bound_launch_rechecks_before_provider_exec(setup, tmp_path, change):
    from workforce.task_runner import _revalidate_launch
    config, task = setup
    task["labels"] += ["work-kind:implement", "risk:low"]
    candidate_id = "claude/sonnet-5@medium#%s::product" % ROUTING_HOST
    config["routing_policy"] = _write_routing_policy(tmp_path, [_routing_seat_row()],
        [_bounded_edit_result(candidate_id)])
    config["routing_host"] = ROUTING_HOST
    _bind_test_policy(config)
    result = prepare(config, feed(task))
    _revalidate_launch(config, result, feed(task))
    if change == "model":
        config["command"] += ["--model", "different"]
    elif change == "tools":
        config["command"] += ["--tools", "extra"]
    elif change == "task":
        task["labels"] += ["risk:high"]
    elif change == "head":
        git(result["checkout"], "commit", "--allow-empty", "-m", "different revision")
    elif change == "instructions":
        Path(config["authority_chain"][0]).write_text("Changed instructions")
    else:
        p = Path(config["routing_policy"]); raw = json.loads(p.read_text())
        if change == "quota": raw["seats"][0]["quota"]["remaining"] = None
        else: raw["seats"][0]["max_run_units"] = 10
        p.write_text(json.dumps(raw))
    with pytest.raises((ValueError, PreparationError)):
        _revalidate_launch(config, result, feed(task))


def test_supervisor_context_cannot_launch_a_different_ready_task(setup, tmp_path, monkeypatch):
    from workforce.routing_binding import CONTEXT_ENV, runner_digest, task_digest
    config, task = setup
    task["labels"] += ["work-kind:implement", "risk:low"]
    config["routing_policy"] = _write_routing_policy(tmp_path, [_routing_seat_row()],
        [_bounded_edit_result("claude/sonnet-5@medium#%s::product" % ROUTING_HOST)])
    config["routing_host"] = ROUTING_HOST
    _bind_test_policy(config)
    monkeypatch.setenv(CONTEXT_ENV, json.dumps(dict(policy=config["routing_policy"],
        host=ROUTING_HOST, task_id=task["id"], task_sha256=task_digest(task),
        runner_sha256=runner_digest(config))))
    with pytest.raises(PreparationError, match="selected work order changed"):
        prepare(config, feed(dict(task, id="p-2")))
    assert not Path(config["state_dir"]).exists()


def test_before_exec_hook_runs_under_inherited_reservation_lock(setup, tmp_path, monkeypatch):
    from workforce import task_runner as tr
    config,task=setup
    path=tmp_path/'runner.json';path.write_text(json.dumps(config))
    monkeypatch.setattr(tr,'prepare',lambda c: prepare(c,feed(task)))
    original_cwd=os.getcwd()
    seen=[]
    def hook(c,result):
        with pytest.raises(PreparationError,match='active'):
            _acquire_lock(result['lock'])
        seen.append('hook')
    class Launched(BaseException): pass
    def exec_stub(*args):
        seen.append('exec')
        raise Launched()
    monkeypatch.setattr(os,'execvpe',exec_stub)
    try:
        with pytest.raises(Launched): tr.main(['--config',str(path)],before_exec=hook)
    finally:
        os.chdir(original_cwd)
    assert seen==['hook','exec']

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

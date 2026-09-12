import json
from pathlib import Path
import subprocess

import pytest

from workforce.task_runner import PreparationError, prepare


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

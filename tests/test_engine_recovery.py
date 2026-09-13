"""wf-255: explicit task_runner reservation recovery routed through engine.dispatch.

Direct ``python -m workforce.task_runner --recover-receipt ...`` resumes a
reservation but is invisible to the engine (no ledger START/STOP, no engine
lock/budget, no run/<worker>.out). ``engine.dispatch(..., recover_receipt=...,
recovery_reason=...)`` runs the identical recovery as a shift: a real
task_runner subprocess, spawned exactly like any other task_runner-based
worker (RUNNING.md "Reusable task preparation"), against a tiny local HTTP
desk so the ready-eligibility re-check inside that subprocess is real.
"""

import fcntl
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import engine  # noqa: E402
from workforce import task_runner  # noqa: E402
from workforce.roster import Worker  # noqa: E402


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class _DeskHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.methods.append("GET")
        body = json.dumps(self.server.feed).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep test output quiet
        pass


class _GrowingQueueHandler(http.server.BaseHTTPRequestHandler):
    """Reports an ever-increasing ready count so a drain worker would never
    take the "no progress" multi-pass stop on its own — only a forced
    single-pass ceiling should limit it to one spawn."""

    def do_GET(self):
        self.server.calls += 1
        body = json.dumps({"count": self.server.calls}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def growing_queue():
    server = http.server.HTTPServer(("127.0.0.1", 0), _GrowingQueueHandler)
    server.calls = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def desk(task):
    server = http.server.HTTPServer(("127.0.0.1", 0), _DeskHandler)
    server.feed = {"ok": True, "count": 1, "product": "product", "tasks": [task]}
    server.methods = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def task():
    return dict(id="p-1", product="product", status="backlog",
                labels=["worker:builder", "execution:bounded"])


@pytest.fixture
def prepared(tmp_path, task, monkeypatch):
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
    prompt.write_text("{authority}\nClaim {task_id} as {worker}. Branch {branch}.")
    law = tmp_path / "AGENTS.md"
    law.write_text("Claim before implementing.")

    out_marker = tmp_path / "recovered.out"
    config = dict(
        project="product", worker="builder", desk_url="http://REPLACED",
        required_label="execution:bounded", repository=str(repo),
        expected_remote="https://example.invalid/product", base_ref="main",
        state_dir=str(tmp_path / "runs"), prompt_template=str(prompt),
        authority_chain=[str(law)],
        command=["/bin/sh", "-c", "echo recovered-ok >> '%s'" % out_marker],
    )
    config_path = tmp_path / "runner.json"

    monkeypatch.setenv("WL_AGENT_ID", "builder")
    result = task_runner.prepare(config, fetch=lambda url: dict(server_feed(task)))
    return config, config_path, result, out_marker


def server_feed(task):
    return {"ok": True, "count": 1, "product": "product", "tasks": [task]}


def make_worker(tmp_path, config, config_path, desk, **over):
    config = dict(config, desk_url="http://127.0.0.1:%d" % desk.server_port)
    config_path.write_text(json.dumps(config))
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)
    contract = tmp_path / "CONTRACT.md"
    prompt = tmp_path / "worker_prompt.md"
    contract.write_text("# contract v1\n")
    prompt.write_text("do one slice\n")
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"ok": True, "count": 1}))
    spec = dict(
        name="tester", workdir=str(workdir), contract=str(contract),
        prompt=str(prompt), identity="tester-id",
        command=[sys.executable, "-m", "workforce.task_runner", "--config", str(config_path)],
        queue_url="file://" + str(queue), budget_secs=15, min_free_mb=1,
    )
    spec.update(over)
    return Worker(**spec)


def ledger_text(tmp_path):
    p = tmp_path / "local" / "ledger" / "tester.log"
    return p.read_text() if p.exists() else ""


def local(tmp_path):
    return str(tmp_path / "local")


def test_recovery_dispatch_writes_ledger_rows_and_runs_the_recovered_shift(tmp_path, prepared, desk):
    config, config_path, result, out_marker = prepared
    worker = make_worker(tmp_path, config, config_path, desk)

    rc = engine.dispatch(worker, local(tmp_path),
                          recover_receipt=result["receipt"], recovery_reason="provider crashed mid-run")

    assert rc == 0
    assert out_marker.read_text() == "recovered-ok\n"
    text = ledger_text(tmp_path)
    lines = text.splitlines()
    start = next(l for l in lines if " START " in l)
    stop = next(l for l in lines if " STOP " in l or " DONE " in l)
    assert "recovery=1" in start
    assert "recovery=1" in stop
    assert desk.methods == ["GET"]  # no WorkLane writes — read-only ready probe


def test_recovery_refused_when_reservation_lock_is_held(tmp_path, prepared, desk):
    config, config_path, result, out_marker = prepared
    worker = make_worker(tmp_path, config, config_path, desk)

    fd = task_runner._acquire_lock(result["lock"])
    try:
        rc = engine.dispatch(worker, local(tmp_path),
                              recover_receipt=result["receipt"], recovery_reason="second start attempt")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert rc == 1
    assert not out_marker.exists()
    text = ledger_text(tmp_path)
    assert "ERROR" in text and "recovery=1" in text

    # The engine's own per-worker lock is released even though the shift failed.
    from workforce.engine import _Lock
    from workforce.ledger import Ledger
    ledger = Ledger(os.path.join(local(tmp_path), "ledger"), worker.name)
    retry_lock = _Lock(os.path.join(local(tmp_path), "locks"), worker.name, worker.budget_secs, ledger)
    retry_lock.acquire()
    retry_lock.release()


def test_dispatch_requires_both_recovery_flags_together(tmp_path, prepared, desk):
    config, config_path, result, out_marker = prepared
    worker = make_worker(tmp_path, config, config_path, desk)

    assert engine.dispatch(worker, local(tmp_path), recover_receipt=result["receipt"]) == 1
    assert not out_marker.exists()
    assert "recover-receipt and recovery-reason" in ledger_text(tmp_path)


@pytest.mark.parametrize("max_passes", [0, 3])
def test_recovery_forces_single_pass_regardless_of_max_passes(
    tmp_path, prepared, desk, growing_queue, max_passes,
):
    config, config_path, result, out_marker = prepared
    queue_url = "http://127.0.0.1:%d" % growing_queue.server_port
    worker = make_worker(
        tmp_path, config, config_path, desk,
        max_passes=max_passes, queue_url=queue_url,
    )

    rc = engine.dispatch(worker, local(tmp_path),
                          recover_receipt=result["receipt"], recovery_reason="single pass check")

    assert rc == 0
    # A second engine-driven pass would prepare attempts/2 and echo again.
    assert out_marker.read_text() == "recovered-ok\n"
    text = ledger_text(tmp_path)
    start = next(l for l in text.splitlines() if " START " in l)
    assert "recovery_single_pass=1" in start
    assert text.count(" DONE ") == 1


def test_recovery_skips_fallback_on_vendor_limit(tmp_path, prepared, desk):
    config, config_path, result, out_marker = prepared
    config["command"] = ["/bin/sh", "-c", "echo '429 rate limit' >&2; exit 1"]
    worker = make_worker(tmp_path, config, config_path, desk, fallback_runtime="never-run")

    rc = engine.dispatch(worker, local(tmp_path),
                          recover_receipt=result["receipt"], recovery_reason="vendor limit test")

    assert rc == 1
    assert not out_marker.exists()
    text = ledger_text(tmp_path)
    assert "fallback skipped during recovery" in text
    assert "recovery=1" in text
    assert "quota-fallback" not in text


def test_recovery_candidate_names_only_the_recovered_task(tmp_path, prepared, desk):
    config, config_path, result, out_marker = prepared
    other_tasks = [
        dict(id="p-1", product="product", status="backlog", labels=["worker:builder", "execution:bounded"]),
        dict(id="p-2", product="product", status="backlog", labels=["worker:builder", "execution:bounded"]),
        dict(id="p-3", product="product", status="backlog", labels=["worker:builder", "execution:bounded"]),
    ]
    desk.feed["tasks"] = other_tasks
    desk.feed["count"] = len(other_tasks)
    worker = make_worker(tmp_path, config, config_path, desk)

    rc = engine.dispatch(worker, local(tmp_path),
                          recover_receipt=result["receipt"], recovery_reason="candidate scope check")

    assert rc == 0
    text = ledger_text(tmp_path)
    candidate_lines = [l for l in text.splitlines() if " CANDIDATE " in l]
    assert len(candidate_lines) == 1
    assert "ticket=p-1" in candidate_lines[0]
    assert "p-2" not in candidate_lines[0]
    assert "p-3" not in candidate_lines[0]
    assert "recovery=1" in candidate_lines[0]


def test_recovery_receipt_outside_state_dir_is_refused(tmp_path, prepared, desk):
    config, config_path, result, out_marker = prepared
    worker = make_worker(tmp_path, config, config_path, desk)
    foreign = tmp_path / "elsewhere" / "preparation.json"
    foreign.parent.mkdir()
    foreign.write_text(Path(result["receipt"]).read_text())

    rc = engine.dispatch(worker, local(tmp_path),
                          recover_receipt=str(foreign), recovery_reason="foreign receipt")

    assert rc == 1
    assert not out_marker.exists()
    text = ledger_text(tmp_path)
    assert "outside the worker's state_dir" in text
    assert "START" not in text


def test_direct_task_runner_recovery_is_not_engine_visible(tmp_path, prepared, desk):
    """Documents the bypass this ticket fixes: direct recovery writes no ledger."""
    config, config_path, result, out_marker = prepared
    worker = make_worker(tmp_path, config, config_path, desk)

    direct = task_runner.recover(config, result["receipt"], "resume after crash",
                                  fetch=lambda url: server_feed({"id": "p-1", "product": "product",
                                                                  "status": "backlog",
                                                                  "labels": ["worker:builder", "execution:bounded"]}))
    assert direct["task_id"] == "p-1"
    assert ledger_text(tmp_path) == ""

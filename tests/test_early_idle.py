"""wf-176 — early-idle process decay (ALWAYS_WORK §9)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import early_idle, engine  # noqa: E402
from workforce.roster import Worker  # noqa: E402

from test_engine import ledger_text, local, make_worker  # noqa: E402
from test_multipass import drain_command  # noqa: E402


def test_classify_soft_ceiling():
    assert early_idle.is_soft_ceiling_reason("single-pass complete")
    assert early_idle.is_soft_ceiling_reason("max passes (3)")
    assert not early_idle.is_soft_ceiling_reason("queue empty")
    assert not early_idle.is_soft_ceiling_reason("budget floor (<30s left)")


def test_classify_lawful_exits():
    assert early_idle.is_lawful_exit_reason("queue empty")
    assert early_idle.is_lawful_exit_reason("budget floor (<30s left)")
    assert early_idle.is_lawful_exit_reason("killed at budget", "error")
    assert early_idle.is_lawful_exit_reason("no progress (3 -> 3)")
    assert early_idle.is_lawful_exit_reason("restocked (3 -> 4)")
    assert early_idle.is_lawful_exit_reason("drain hard cap (50 passes)")
    assert early_idle.is_lawful_exit_reason("vendor limit: 429", "vendor_limit")
    assert early_idle.is_lawful_exit_reason("anything", "error")
    assert not early_idle.is_lawful_exit_reason("single-pass complete")
    assert not early_idle.is_lawful_exit_reason("max passes (2)")


def test_is_early_idle_stop():
    assert early_idle.is_early_idle_stop("single-pass complete")
    assert early_idle.is_early_idle_stop("max passes (4)")
    assert not early_idle.is_early_idle_stop("queue empty")
    assert not early_idle.is_early_idle_stop("single-pass complete", "error")


def test_engine_warns_early_idle_on_single_pass_with_ready(tmp_path):
    """Soft ceiling + leftover ready → WARN reason=early-idle before STOP."""
    q = tmp_path / "queue.json"
    w = make_worker(
        tmp_path,
        command=drain_command(q),
        max_passes=1,
        min_pass_secs=0,
        kind="lane",
    )
    # make_worker seeds count=3; pin a deeper feed after Worker construction.
    q.write_text(json.dumps({"ok": True, "count": 5}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "WARN" in text and "reason=early-idle" in text
    assert "ready=4" in text  # drained 1 of 5
    assert 'stop="single-pass complete"' in text
    assert "single-pass complete" in text


def test_engine_no_early_idle_warn_when_queue_emptied(tmp_path):
    """Single-pass that leaves ready=0 is not early-idle."""
    q = tmp_path / "queue.json"
    w = make_worker(
        tmp_path,
        command=drain_command(q),
        max_passes=1,
        min_pass_secs=0,
        kind="lane",
    )
    q.write_text(json.dumps({"ok": True, "count": 1}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "single-pass complete" in text
    assert "reason=early-idle" not in text


def test_engine_no_early_idle_on_queue_empty_stop(tmp_path):
    """Budget-drain to empty is lawful — no early-idle WARN."""
    q = tmp_path / "queue.json"
    w = make_worker(
        tmp_path,
        command=drain_command(q),
        max_passes=0,
        min_pass_secs=0,
        budget_secs=30,
        kind="lane",
    )
    q.write_text(json.dumps({"ok": True, "count": 2}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "queue empty" in text
    assert "reason=early-idle" not in text


def test_engine_warns_on_max_passes_with_ready(tmp_path):
    q = tmp_path / "queue.json"
    w = make_worker(
        tmp_path,
        command=drain_command(q),
        max_passes=2,
        min_pass_secs=0,
        kind="lane",
    )
    q.write_text(json.dumps({"ok": True, "count": 10}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "max passes (2)" in text
    assert "reason=early-idle" in text
    assert "ready=8" in text


def test_scan_prefers_warn_breadcrumb(tmp_path):
    data = tmp_path / "local"
    ledger_dir = data / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "otto.log").write_text(
        "2026-08-07T10:00:00Z START identity=otto kind=lane queue=5 max_passes=1\n"
        "2026-08-07T10:01:00Z DONE rc=0 on_pass=1\n"
        "2026-08-07T10:01:01Z WARN reason=early-idle ready=4 stop=\"single-pass complete\" on_pass=1\n"
        "2026-08-07T10:01:02Z STOP reason=\"single-pass complete\"\n"
    )
    workers = {
        "otto": Worker(
            name="otto", workdir=str(tmp_path), contract="c", prompt="p",
            identity="otto", kind="lane",
            queue_url="http://127.0.0.1:8799/api/admin/tasks/ready?label=worker:otto",
            command=["true"],
        ),
    }
    findings = early_idle.scan_early_idle(str(data), workers)
    assert len(findings) == 1
    assert findings[0]["worker"] == "otto"
    assert findings[0]["source"] == "warn"
    assert findings[0]["ready"] == 4
    report = early_idle.format_early_idle_report(findings)
    assert "1 seat" in report
    assert "otto" in report
    assert "wf-176" in report


def test_scan_fallback_stop_without_warn(tmp_path):
    """Pre-wf-176 ledgers: soft-ceiling STOP + start queue >1 → smell."""
    data = tmp_path / "local"
    ledger_dir = data / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "mel.log").write_text(
        "2026-08-07T10:00:00Z START identity=mel kind=lane queue=3 max_passes=1\n"
        "2026-08-07T10:01:00Z DONE rc=0 on_pass=1\n"
        "2026-08-07T10:01:02Z STOP reason=\"single-pass complete\"\n"
    )
    workers = {
        "mel": Worker(
            name="mel", workdir=str(tmp_path), contract="c", prompt="p",
            identity="mel", kind="lane",
            queue_url="http://example/ready?label=worker:mel",
            command=["true"],
        ),
    }
    findings = early_idle.scan_early_idle(str(data), workers)
    assert len(findings) == 1
    assert findings[0]["source"] == "stop"
    assert findings[0]["ready"] == 2  # 3 start - 1 pass


def test_scan_skips_jobs_and_empty_report():
    workers = {
        "patrol": Worker(
            name="patrol", workdir="/tmp", contract="c", prompt="p",
            identity="patrol", kind="job", command=["true"],
        ),
    }
    assert early_idle.scan_early_idle("/nonexistent", workers) == []
    assert early_idle.format_early_idle_report([]) == "Early-idle: none"


def test_doctor_prints_early_idle_rollup(tmp_path, monkeypatch, capsys):
    from workforce import cli

    data = tmp_path / "engine"
    local_root = data / "local"
    local_root.mkdir(parents=True)
    roster = local_root / "roster.json"
    workdir = tmp_path / "hood"
    workdir.mkdir()
    contract = workdir / "CONTRACT.md"
    prompt = workdir / "prompt.md"
    contract.write_text("# c\n")
    prompt.write_text("p\n")
    roster.write_text(json.dumps({
        "workers": {
            "otto": {
                "kind": "lane",
                "workdir": str(workdir),
                "contract": str(contract),
                "prompt": str(prompt),
                "identity": "otto",
                "command": ["true"],
                "queue_url": (
                    "http://127.0.0.1:8799/api/admin/tasks/ready"
                    "?product=workforce&label=worker:otto"
                ),
                "max_passes": 1,
            },
        },
    }))
    ledger_dir = local_root / "ledger"
    ledger_dir.mkdir()
    (ledger_dir / "otto.log").write_text(
        "2026-08-07T12:00:00Z START identity=otto kind=lane queue=4\n"
        "2026-08-07T12:01:00Z DONE rc=0 on_pass=1\n"
        "2026-08-07T12:01:01Z WARN reason=early-idle ready=3 "
        "stop=\"single-pass complete\" on_pass=1\n"
        "2026-08-07T12:01:02Z STOP reason=\"single-pass complete\"\n"
    )
    process = tmp_path / "PROCESS-stub.md"
    process.write_text(
        "### 5.2) Identity\n\n| Agent id | Who |\n| --- | --- |\n"
        "| `otto` | test. |\n\n### 5.3) Other\n"
    )
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.setenv("WORKLANE_PROCESS", str(process))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor", "--skip-stale-routing"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Early-idle:" in out
    assert "otto" in out
    assert "wf-176" in out

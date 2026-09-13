"""Provider qualification matrix, constraint audit, and throughput — wf-263."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import engine  # noqa: E402
from workforce import provider_qualification as pq  # noqa: E402
from workforce.ledger import Ledger  # noqa: E402
from workforce.roster import Worker  # noqa: E402


def _worker(tmp_path, name="tester", **over):
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)
    contract = tmp_path / "CONTRACT.md"
    prompt = tmp_path / "prompt.md"
    contract.write_text("# c\n")
    prompt.write_text("p\n")
    spec = dict(
        name=name, workdir=str(workdir), contract=str(contract),
        prompt=str(prompt), identity=name,
        command=["cursor-agent", "--print"],
        kind="lane", budget_secs=900, max_passes=1, schedule="manual",
    )
    spec.update(over)
    return Worker(**spec)


def _write_shift(local_root, worker_name, outcome="ok", start=None, end=None):
    ledger = Ledger(os.path.join(local_root, "ledger"), worker_name)
    start = start or datetime.now(timezone.utc) - timedelta(hours=1)
    end = end or datetime.now(timezone.utc)
    ledger.append(
        "START", identity=worker_name, kind="lane", queue=1,
        budget_secs=900, max_passes=1,
    )
    ledger.append("DONE", rc=0, on_pass=1, secs=60, tok_in=100, tok_out=50)
    ledger.append("STOP", reason="single-pass complete")
    # Backdate by rewriting is not supported — tests use recent shifts only.


def test_resolve_active_implementation_cap_defaults_to_one(monkeypatch):
    monkeypatch.delenv(pq.ENV_ACTIVE_IMPLEMENTATION_CAP, raising=False)
    assert pq.resolve_active_implementation_cap() == 1


def test_resolve_active_implementation_cap_from_env(monkeypatch):
    monkeypatch.setenv(pq.ENV_ACTIVE_IMPLEMENTATION_CAP, "2")
    assert pq.resolve_active_implementation_cap() == 2


def test_resolve_active_implementation_cap_from_config():
    assert pq.resolve_active_implementation_cap({"active_implementation_cap": 3}) == 3


def test_count_active_implementations_ignores_orphan_lock(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    w = _worker(tmp_path)
    workers = {w.name: w}
    lock_dir = local / "locks" / ("%s.lock" % w.name)
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text("999999999")
    monkeypatch.setattr(engine, "_pid_alive", lambda pid: False)
    assert pq.count_active_implementations(str(local), workers) == []


def test_count_active_implementations_counts_live_lock(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    w = _worker(tmp_path)
    workers = {w.name: w}
    lock_dir = local / "locks" / ("%s.lock" % w.name)
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text("42")
    monkeypatch.setattr(engine, "_pid_alive", lambda pid: True)
    active = pq.count_active_implementations(str(local), workers)
    assert len(active) == 1
    assert active[0]["worker"] == w.name
    assert active[0]["provider"] == "cursor"


def test_dispatch_blocked_by_capacity_when_at_cap(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    w = _worker(tmp_path)
    workers = {w.name: w}
    lock_dir = local / "locks" / ("%s.lock" % w.name)
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text("42")
    monkeypatch.setattr(engine, "_pid_alive", lambda pid: True)
    reason = pq.dispatch_blocked_by_capacity(str(local), workers)
    assert reason is not None
    assert "cap 1 reached" in reason


def test_audit_adapter_constraints_covers_all_providers():
    rows = pq.audit_adapter_constraints()
    scopes = {r["scope"] for r in rows}
    for provider in pq.PROVIDERS:
        assert any(s == "adapter:%s" % provider for s in scopes)


def test_audit_seat_template_constraints_includes_wl_hand_tools():
    rows = pq.audit_seat_template_constraints()
    assert any(r["id"] == "worklane_hand_tools" and r["present"] == "yes" for r in rows)


def test_parse_evidence_records_validates_stages():
    raw = [{
        "seat": "wf-cursor-implementer",
        "provider": "cursor",
        "stage": "evidence_park",
        "status": "failed",
        "observed_at": "2026-09-13T15:05:55Z",
        "artifact": "pc-1480",
        "note": "wl_park refused by approval gate",
    }]
    recs = pq.parse_evidence_records(raw)
    assert len(recs) == 1
    assert recs[0].status == "failed"


def test_build_qualification_matrix_applies_evidence(tmp_path):
    w = _worker(tmp_path, name="wf-cursor-implementer")
    rec = pq.EvidenceRecord(
        seat="wf-cursor-implementer",
        provider="cursor",
        stage="signed_claim",
        status="passed",
        observed_at="2026-09-13T15:46:44Z",
        artifact="wf-263 claim",
    )
    matrix = pq.build_qualification_matrix({w.name: w}, [rec])
    row = matrix[w.name]
    assert row["stages"]["signed_claim"] == "passed"
    assert row["stages"]["discovery_auth"] == "untested"


def test_collect_throughput_metrics_from_ledger(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    w = _worker(tmp_path)
    _write_shift(str(local), w.name)
    metrics = pq.collect_throughput_metrics(str(local), {w.name: w}, window_days=7)
    assert metrics["by_worker"][w.name]["shifts"] == 1
    assert metrics["by_worker"][w.name]["ok"] == 1
    assert metrics["by_provider"]["cursor"]["shifts"] == 1


def test_write_qualification_report_creates_files(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    w = _worker(tmp_path)
    json_path, md_path = pq.write_qualification_report(
        str(local), {w.name: w}, window_days=7,
    )
    assert os.path.isfile(json_path)
    assert os.path.isfile(md_path)
    data = json.loads(open(json_path, encoding="utf-8").read())
    assert data["schema"] == pq.SCHEMA_ID
    assert "qualification_matrix" in data
    assert "constraint_audit" in data
    md = open(md_path, encoding="utf-8").read()
    assert "Provider qualification" in md
    assert "Qualification matrix" in md


def test_format_report_includes_related_work():
    report = pq.build_qualification_report(
        "/tmp/unused", {},
        window_days=7,
    )
    md = pq.format_qualification_report(report)
    assert "wf-260" in md
    assert "wf-262" in md

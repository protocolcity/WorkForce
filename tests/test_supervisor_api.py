"""Read-only supervisor pass evidence API (wf-254)."""

import datetime
import json
import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import board  # noqa: E402
from workforce import reports  # noqa: E402
import workforce.api.roster as _api_roster  # noqa: E402
import workforce.api.roster.models as _roster_models  # noqa: E402
from workforce.roster import Roster, Worker  # noqa: E402


def _local(tmp_path):
    local = tmp_path / "local"
    (local / "ledger").mkdir(parents=True)
    return local


def _worker(tmp_path, name, workdir):
    contract = tmp_path / "CONTRACT.md"
    contract.write_text("# c\n")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p\n")
    wd = tmp_path / workdir
    wd.mkdir(exist_ok=True)
    return Worker(name=name, workdir=str(wd), contract=str(contract),
                  prompt=str(prompt), identity=name, command=["true"])


def _patch_roster(monkeypatch, roster):
    monkeypatch.setattr(_api_roster, "_load_roster", lambda _root: roster)
    monkeypatch.setattr(_api_roster, "_worker_queue", lambda w: "0")
    monkeypatch.setattr(_api_roster, "_desk_json", lambda _p: None)


def _evidence_dir(local):
    d = local / "reports" / "supervisor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_evidence(local, filename, **over):
    payload = {
        "generated_at": "2026-09-12T10:00:00Z",
        "mode": "inspect",
        "provider_ok": True,
        "provider_error": None,
        "proposals": [],
        "dispatch_attempted": 0,
        "dispatch_started": 0,
        "dispatch_completed": 0,
        "dispatch_failed": 0,
        "dispatched": [],
    }
    payload.update(over)
    (_evidence_dir(local) / filename).write_text(json.dumps(payload))
    return payload


def test_missing_supervisor_dir_returns_empty():
    model = reports.supervisor_api_model("/tmp/does-not-exist")
    assert model == {"ok": True, "passes": [], "unreadable": 0}


def test_empty_supervisor_dir_returns_empty(tmp_path):
    local = _local(tmp_path)
    _evidence_dir(local)
    model = reports.supervisor_api_model(str(local))
    assert model == {"ok": True, "passes": [], "unreadable": 0}


def test_ordering_and_limit(tmp_path):
    local = _local(tmp_path)
    _write_evidence(local, "a.json", generated_at="2026-09-10T08:00:00Z")
    _write_evidence(local, "b.json", generated_at="2026-09-12T12:00:00Z")
    _write_evidence(local, "c.json", generated_at="2026-09-11T09:00:00Z")

    passes, unreadable = reports.scan_supervisor_passes(str(local))
    assert unreadable == 0
    assert [p["generated_at"] for p in passes] == [
        "2026-09-12T12:00:00Z",
        "2026-09-11T09:00:00Z",
        "2026-09-10T08:00:00Z",
    ]

    model = reports.supervisor_api_model(str(local), limit=2)
    assert len(model["passes"]) == 2
    assert model["passes"][0]["generated_at"] == "2026-09-12T12:00:00Z"


def test_malformed_file_counted_unreadable(tmp_path):
    local = _local(tmp_path)
    out = _evidence_dir(local)
    _write_evidence(local, "good.json", generated_at="2026-09-12T10:00:00Z")
    (out / "bad.json").write_text("{not json")
    (out / "thin.json").write_text(json.dumps({"generated_at": "2026-09-12T09:00:00Z"}))

    model = reports.supervisor_api_model(str(local))
    assert model["unreadable"] == 2
    assert len(model["passes"]) == 1
    assert model["passes"][0]["evidence_file"] == "good.json"


def test_older_report_without_pass_outcome_is_null(tmp_path):
    local = _local(tmp_path)
    _write_evidence(local, "legacy.json", generated_at="2026-06-01T00:00:00Z")

    row = reports.supervisor_api_model(str(local))["passes"][0]
    assert "pass_outcome" in row
    assert row["pass_outcome"] is None


def test_row_fields_from_rich_evidence(tmp_path):
    local = _local(tmp_path)
    _write_evidence(
        local,
        "run.json",
        generated_at="2026-09-12T11:00:00Z",
        mode="execute",
        pass_outcome="completed",
        provider_ok=False,
        provider_error="provider timed out",
        proposals=[
            {"action": {"worker": "a", "project": "workforce"}, "valid": True},
            {"action": {"worker": "b", "project": "workforce"}, "valid": False},
        ],
        dispatch_attempted=1,
        dispatch_started=1,
        dispatch_completed=1,
        dispatch_failed=0,
        dispatched=[{"worker": "a", "project": "workforce", "outcome": "completed"}],
    )

    row = reports.supervisor_api_model(str(local))["passes"][0]
    assert row["mode"] == "execute"
    assert row["pass_outcome"] == "completed"
    assert row["provider_ok"] is False
    assert row["provider_error"] == "provider timed out"
    assert row["proposals_total"] == 2
    assert row["proposals_valid"] == 1
    assert row["dispatch_attempted"] == 1
    assert row["dispatched"] == [
        {"worker": "a", "project": "workforce", "outcome": "completed"},
    ]
    assert row["evidence_file"] == "run.json"


def test_report_model_includes_supervisor_section(tmp_path, monkeypatch):
    local = _local(tmp_path)
    w = _worker(tmp_path, "lane", "hood")
    _patch_roster(monkeypatch, Roster(workers={"lane": w}, path="t"))
    _write_evidence(local, "recent.json", generated_at="2026-09-12T10:00:00Z")
    _write_evidence(local, "old.json", generated_at="2020-01-01T00:00:00Z")
    ( _evidence_dir(local) / "bad.json").write_text("[]")

    report = board.report_model(str(local), days=7)
    sup = report["supervisor"]
    assert sup["passes_in_window"] == 1
    assert sup["last_pass"]["evidence_file"] == "recent.json"
    assert sup["unreadable"] == 1


def _freeze_utcnow(monkeypatch, when):
    monkeypatch.setattr(reports, "_utcnow", lambda: when)


def test_supervisor_report_section_days_none_matches_report_default(tmp_path, monkeypatch):
    now = datetime.datetime(2026, 9, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
    _freeze_utcnow(monkeypatch, now)
    local = _local(tmp_path)
    _write_evidence(
        local,
        "three_days_ago.json",
        generated_at="2026-09-09T10:00:00Z",
    )

    section = reports.supervisor_report_section(str(local), days=None)
    assert section["passes_in_window"] == 1
    assert section["last_pass"]["evidence_file"] == "three_days_ago.json"

    narrow = reports.supervisor_report_section(str(local), days=1)
    assert narrow["passes_in_window"] == 0
    assert narrow["last_pass"] is None


def test_report_model_supervisor_window_aligns_when_days_none(tmp_path, monkeypatch):
    now = datetime.datetime(2026, 9, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
    _freeze_utcnow(monkeypatch, now)
    monkeypatch.setattr(_roster_models, "_utcnow", lambda: now)
    local = _local(tmp_path)
    w = _worker(tmp_path, "lane", "hood")
    _patch_roster(monkeypatch, Roster(workers={"lane": w}, path="t"))
    _write_evidence(
        local,
        "three_days_ago.json",
        generated_at="2026-09-09T10:00:00Z",
    )

    report = board.report_model(str(local), days=None)
    assert report["window_days"] == 7
    assert report["supervisor"]["passes_in_window"] == 1


def test_unparseable_generated_at_counted_unreadable(tmp_path):
    local = _local(tmp_path)
    _write_evidence(local, "bad-date.json", generated_at="not-a-date")

    model = reports.supervisor_api_model(str(local))
    assert model["unreadable"] == 1
    assert model["passes"] == []


def test_malformed_proposals_counted_unreadable(tmp_path):
    local = _local(tmp_path)
    _write_evidence(
        local,
        "bad-proposals.json",
        proposals=[1, "x", {"valid": True}],
    )
    _write_evidence(local, "good.json", generated_at="2026-09-12T10:00:00Z")

    model = reports.supervisor_api_model(str(local))
    assert model["unreadable"] == 1
    assert len(model["passes"]) == 1
    assert model["passes"][0]["evidence_file"] == "good.json"


def test_limit_param_parses_and_survives_junk():
    assert board._limit_param("/api/supervisor?limit=50") == 50
    assert board._limit_param("/api/supervisor?limit=500") == 100
    assert board._limit_param("/api/supervisor?limit=abc") == 20
    assert board._limit_param("/api/supervisor?limit=0") == 20
    assert board._limit_param("/api/supervisor") == 20


def test_http_supervisor_endpoint(tmp_path):
    local = _local(tmp_path)
    _write_evidence(local, "one.json", generated_at="2026-09-12T10:00:00Z")
    httpd = board.make_server(port=0, local_root=str(local), daemon=None)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/supervisor?limit=5" % port, timeout=10) as resp:
            payload = json.loads(resp.read())
        assert payload["ok"] is True
        assert len(payload["passes"]) == 1
        assert payload["passes"][0]["evidence_file"] == "one.json"
    finally:
        httpd.shutdown()

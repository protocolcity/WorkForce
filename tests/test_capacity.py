"""Capacity pool alerts — streak detection + dry-run drop, no live burn."""

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import capacity  # noqa: E402
from workforce.roster import Worker  # noqa: E402


class _FakeRoster:
    def __init__(self, workers):
        self.workers = workers


def _worker(name, cli="claude"):
    return Worker(
        name=name,
        workdir="/tmp",
        contract="/tmp/C.md",
        prompt="/tmp/p.md",
        identity=name,
        command=[cli, "-p"],
        kind="lane",
    )


def _write_ledger(local_root, name, lines):
    path = os.path.join(local_root, "ledger", "%s.log" % name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _shift_block(ts, outcome_reason, identity="w"):
    """One START..ERROR/STOP block."""
    if outcome_reason is None:
        return [
            "%s START identity=%s kind=lane budget_secs=60 dry_run=0" % (ts, identity),
            "%s DONE rc=0 on_pass=1" % ts,
            "%s STOP reason=\"single-pass complete\"" % ts,
        ]
    return [
        "%s START identity=%s kind=lane budget_secs=60 dry_run=0" % (ts, identity),
        "%s ERROR reason=\"%s\" rc=1 on_pass=1" % (ts, outcome_reason),
    ]


def test_consecutive_capacity_streak_counts_from_newest():
    shifts = [
        {"outcome": "vendor_limit", "reason": "vendor limit: x"},
        {"outcome": "vendor_limit", "reason": "vendor limit: y"},
        {"outcome": "ok", "reason": ""},
    ]
    assert capacity.consecutive_capacity_streak(shifts) == 2


def test_consecutive_capacity_streak_broken_by_ok():
    shifts = [
        {"outcome": "ok", "reason": ""},
        {"outcome": "vendor_limit", "reason": "vendor limit: x"},
        {"outcome": "vendor_limit", "reason": "vendor limit: y"},
    ]
    assert capacity.consecutive_capacity_streak(shifts) == 0


def test_consecutive_skips_running_and_skip():
    shifts = [
        {"outcome": "running", "reason": ""},
        {"outcome": "skip", "reason": "queue empty"},
        {"outcome": "vendor_limit", "reason": "vendor limit: x"},
    ]
    assert capacity.consecutive_capacity_streak(shifts) == 1


def test_detect_alert_on_n_consecutive(tmp_path):
    local_root = str(tmp_path / "local")
    lines = []
    # three consecutive capacity fails (newest last in file → parse reverses)
    for i, minute in enumerate(("01", "02", "03")):
        lines.extend(_shift_block(
            "2026-08-02T15:%s:00Z" % minute,
            "vendor limit: usage limit",
            identity="alice",
        ))
    _write_ledger(local_root, "alice", lines)
    roster = _FakeRoster({"alice": _worker("alice", "claude")})
    when = datetime.datetime(2026, 8, 2, 16, 0, tzinfo=datetime.timezone.utc)
    alerts = capacity.detect_capacity_alerts(
        roster, local_root, consecutive=3, seats_same_hour=99, when=when,
    )
    assert len(alerts) == 1
    assert alerts[0]["pool"] == "claude"
    assert alerts[0]["streak"] >= 3
    assert "alice" in alerts[0]["thrash_workers"]
    assert alerts[0]["inbox_key"] == "capacity-claude"
    assert "inbox-report:workforce:capacity-claude:2026-08-02" == alerts[0]["inbox_label"]


def test_detect_clears_after_successful_shift(tmp_path):
    local_root = str(tmp_path / "local")
    lines = []
    for minute in ("01", "02", "03"):
        lines.extend(_shift_block(
            "2026-08-02T15:%s:00Z" % minute,
            "vendor limit: usage limit",
            identity="alice",
        ))
    lines.extend(_shift_block("2026-08-02T16:00:00Z", None, identity="alice"))
    _write_ledger(local_root, "alice", lines)
    roster = _FakeRoster({"alice": _worker("alice", "claude")})
    when = datetime.datetime(2026, 8, 2, 17, 0, tzinfo=datetime.timezone.utc)
    alerts = capacity.detect_capacity_alerts(
        roster, local_root, consecutive=3, seats_same_hour=99, when=when,
    )
    assert alerts == []


def test_detect_alert_on_k_seats_same_hour(tmp_path):
    local_root = str(tmp_path / "local")
    # two seats, one capacity fail each in the same hour — below consecutive=3
    _write_ledger(local_root, "a", _shift_block(
        "2026-08-02T15:10:00Z", "vendor limit: 429", identity="a"))
    _write_ledger(local_root, "b", _shift_block(
        "2026-08-02T15:40:00Z", "vendor limit: usage limit", identity="b"))
    roster = _FakeRoster({
        "a": _worker("a", "codex"),
        "b": _worker("b", "codex"),
    })
    when = datetime.datetime(2026, 8, 2, 15, 50, tzinfo=datetime.timezone.utc)
    alerts = capacity.detect_capacity_alerts(
        roster, local_root, consecutive=99, seats_same_hour=2, when=when,
    )
    assert len(alerts) == 1
    assert alerts[0]["pool"] == "codex"
    assert alerts[0]["seats_hour"] == 2
    assert set(alerts[0]["hour_workers"]) == {"a", "b"}


def test_format_and_write_report(tmp_path):
    local_root = str(tmp_path / "local")
    alerts = [{
        "pool": "claude",
        "reason": "3+ consecutive capacity fails on: alice",
        "workers": ["alice"],
        "thrash_workers": ["alice"],
        "hour_workers": [],
        "streak": 3,
        "seats_hour": 0,
        "inbox_key": "capacity-claude",
        "inbox_label": "inbox-report:workforce:capacity-claude:2026-08-02",
        "glance": "Provider pool **claude** looks blocked.",
        "day": "2026-08-02",
        "project": "workforce",
    }]
    path = capacity.write_capacity_report(local_root, alerts, day="2026-08-02")
    assert os.path.isfile(path)
    body = open(path).read()
    assert "Capacity alerts" in body
    assert "claude" in body


def test_drop_capacity_dry_run_no_network(tmp_path, monkeypatch):
    """dry_run must not call the desk."""
    def boom(*a, **k):
        raise AssertionError("desk must not be contacted on dry_run")

    monkeypatch.setattr(capacity, "find_open_by_label", boom)
    monkeypatch.setattr(capacity, "_req", boom)
    alert = {
        "pool": "claude",
        "inbox_key": "capacity-claude",
        "inbox_label": "inbox-report:workforce:capacity-claude:2026-08-02",
        "glance": "blocked",
        "day": "2026-08-02",
        "project": "workforce",
    }
    receipt = capacity.drop_capacity_for_you(
        alert, report_path="/tmp/r.md", dry_run=True,
    )
    assert receipt["ok"] is True
    assert receipt["action"] == "would_create"


def test_drop_capacity_live_refused_under_pytest(monkeypatch):
    """wf-132: dry_run=False still does not touch the desk under pytest."""
    def boom(*a, **k):
        raise AssertionError("desk must not be contacted under pytest")

    monkeypatch.setattr(capacity, "find_open_by_label", boom)
    monkeypatch.setattr(capacity, "_req", boom)
    # conftest sets WORKFORCE_NO_DESK; also ensure PYTEST_CURRENT_TEST is set
    # (pytest always does) and ALLOW is off.
    monkeypatch.delenv("WORKFORCE_ALLOW_DESK", raising=False)
    alert = {
        "pool": "sh",
        "inbox_key": "capacity-sh",
        "inbox_label": "inbox-report:workforce:capacity-sh:2026-08-03",
        "glance": "fixture pool blocked",
        "day": "2026-08-03",
        "project": "workforce",
    }
    receipt = capacity.drop_capacity_for_you(
        alert, report_path="/tmp/r.md", dry_run=False,
    )
    assert receipt["ok"] is True
    assert receipt["action"] == "would_create"
    assert receipt.get("hermetic") is True


def test_desk_writes_allowed_env_matrix(monkeypatch):
    """wf-132: kill-switch / pytest / opt-in precedence."""
    monkeypatch.delenv("WORKFORCE_ALLOW_DESK", raising=False)
    monkeypatch.setenv("WORKFORCE_NO_DESK", "1")
    assert capacity.desk_writes_allowed() is False

    monkeypatch.delenv("WORKFORCE_NO_DESK", raising=False)
    # Under pytest PYTEST_CURRENT_TEST is always set → still False.
    assert capacity.desk_writes_allowed() is False

    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")
    assert capacity.desk_writes_allowed() is True


def test_pool_for_command_basename():
    assert capacity.pool_for_command(["/usr/local/bin/codex", "exec"]) == "codex"
    assert capacity.pool_for_command([]) == ""


def test_inbox_key_stable():
    assert capacity.inbox_key_for_pool("claude") == "capacity-claude"
    assert capacity.inbox_label("workforce", "claude", "2026-08-02") == (
        "inbox-report:workforce:capacity-claude:2026-08-02"
    )

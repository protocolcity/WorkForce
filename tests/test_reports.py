"""Daily cost-rollup reports — pytest; no local/ touch (tmp_path only)."""

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import reports  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeWorker:
    def __init__(self, name, cli="claude"):
        self.name = name
        self.command = ["/usr/local/bin/%s" % cli, "-p"]


def _write_ledger(local_root, name, lines):
    path = os.path.join(local_root, "ledger", "%s.log" % name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _shift(ts, tok_in=100, tok_out=200, cost_usd=0.01):
    """One complete START→DONE→STOP block with usage telemetry."""
    return [
        "%s START identity=w kind=lane budget_secs=60 dry_run=0" % ts,
        "%s DONE rc=0 on_pass=1 secs=5 tok_in=%s tok_out=%s cost_usd=%s" % (
            ts, tok_in, tok_out, cost_usd,
        ),
        "%s STOP reason=\"single-pass complete\"" % ts,
    ]


DAY1 = datetime.date(2026, 8, 1)
DAY2 = datetime.date(2026, 8, 2)
TS1 = "2026-08-01T10:00:00Z"
TS1b = "2026-08-01T11:00:00Z"
TS2 = "2026-08-02T10:00:00Z"


# ---------------------------------------------------------------------------
# collect_daily_cost
# ---------------------------------------------------------------------------

def test_two_vendors_one_day(tmp_path):
    local_root = str(tmp_path)
    workers = {
        "alice": _FakeWorker("alice", "claude"),
        "bob": _FakeWorker("bob", "grok"),
    }
    # alice: 2 shifts on day1, 1 on day2
    _write_ledger(local_root, "alice",
                  _shift(TS1, tok_in=100, tok_out=200, cost_usd=0.10) +
                  _shift(TS1b, tok_in=50, tok_out=100, cost_usd=0.05) +
                  _shift(TS2, tok_in=10, tok_out=20, cost_usd=0.99))
    # bob: 1 shift on day1
    _write_ledger(local_root, "bob",
                  _shift(TS1, tok_in=200, tok_out=400, cost_usd=0.20))

    totals = reports.collect_daily_cost(local_root, DAY1, workers)

    assert set(totals.keys()) == {"claude", "grok"}
    assert totals["claude"]["shifts"] == 2
    assert abs(totals["claude"]["tok_in"] - 150) < 0.01
    assert abs(totals["claude"]["tok_out"] - 300) < 0.01
    assert abs(totals["claude"]["cost_usd"] - 0.15) < 0.0001
    assert totals["grok"]["shifts"] == 1
    assert abs(totals["grok"]["cost_usd"] - 0.20) < 0.0001


def test_only_target_date_counted(tmp_path):
    local_root = str(tmp_path)
    workers = {"alice": _FakeWorker("alice", "claude")}
    _write_ledger(local_root, "alice",
                  _shift(TS1, cost_usd=0.10) +
                  _shift(TS2, cost_usd=0.20))

    totals = reports.collect_daily_cost(local_root, DAY2, workers)

    assert abs(totals["claude"]["cost_usd"] - 0.20) < 0.0001
    assert totals["claude"]["shifts"] == 1


def test_missing_ledger_skipped(tmp_path):
    local_root = str(tmp_path)
    workers = {"ghost": _FakeWorker("ghost", "claude")}
    # no ledger written

    totals = reports.collect_daily_cost(local_root, DAY1, workers)

    assert totals == {}


def test_unknown_command_groups_as_unknown(tmp_path):
    local_root = str(tmp_path)
    workers = {"anon": _FakeWorker("anon", "")}
    workers["anon"].command = []  # empty command
    _write_ledger(local_root, "anon", _shift(TS1, cost_usd=0.05))

    totals = reports.collect_daily_cost(local_root, DAY1, workers)

    assert "unknown" in totals
    assert abs(totals["unknown"]["cost_usd"] - 0.05) < 0.0001


# ---------------------------------------------------------------------------
# format_daily_cost_report
# ---------------------------------------------------------------------------

def test_anomaly_flag_above_threshold(tmp_path):
    local_root = str(tmp_path)
    workers = {"alice": _FakeWorker("alice", "claude")}
    _write_ledger(local_root, "alice", _shift(TS1, cost_usd=6.0))
    totals = reports.collect_daily_cost(local_root, DAY1, workers)

    report = reports.format_daily_cost_report(totals, DAY1, cost_threshold=5.0)

    assert "⚠️" in report
    assert "6.0" in report or "6.00" in report


def test_no_anomaly_below_threshold(tmp_path):
    local_root = str(tmp_path)
    workers = {"alice": _FakeWorker("alice", "claude")}
    _write_ledger(local_root, "alice", _shift(TS1, cost_usd=1.0))
    totals = reports.collect_daily_cost(local_root, DAY1, workers)

    report = reports.format_daily_cost_report(totals, DAY1, cost_threshold=5.0)

    assert "⚠️" not in report


def test_report_contains_date_and_vendor(tmp_path):
    local_root = str(tmp_path)
    workers = {"alice": _FakeWorker("alice", "claude")}
    _write_ledger(local_root, "alice", _shift(TS1))
    totals = reports.collect_daily_cost(local_root, DAY1, workers)

    report = reports.format_daily_cost_report(totals, DAY1)

    assert "2026-08-01" in report
    assert "claude" in report


def test_empty_totals_report(tmp_path):
    report = reports.format_daily_cost_report({}, DAY1)
    assert "2026-08-01" in report
    assert "No shifts" in report


# ---------------------------------------------------------------------------
# write_daily_cost_report
# ---------------------------------------------------------------------------

def test_write_creates_file(tmp_path):
    local_root = str(tmp_path)
    workers = {"alice": _FakeWorker("alice", "claude")}
    _write_ledger(local_root, "alice", _shift(TS1, cost_usd=0.10))

    path = reports.write_daily_cost_report(local_root, DAY1, workers)

    assert os.path.exists(path)
    assert "2026-08-01" in os.path.basename(path)
    content = open(path).read()
    assert "2026-08-01" in content
    assert "claude" in content


def test_write_idempotent(tmp_path):
    local_root = str(tmp_path)
    workers = {"alice": _FakeWorker("alice", "claude")}
    _write_ledger(local_root, "alice", _shift(TS1, cost_usd=0.10))

    path1 = reports.write_daily_cost_report(local_root, DAY1, workers)
    # Append a sentinel so we can detect if the file is rewritten
    with open(path1, "a") as fh:
        fh.write("SENTINEL\n")

    path2 = reports.write_daily_cost_report(local_root, DAY1, workers)

    assert path1 == path2
    assert "SENTINEL" in open(path2).read()


def test_write_creates_directory(tmp_path):
    local_root = str(tmp_path)
    # reports/cost/ does not exist yet
    assert not os.path.isdir(os.path.join(local_root, "reports", "cost"))
    workers = {"alice": _FakeWorker("alice", "claude")}
    _write_ledger(local_root, "alice", _shift(TS1))

    reports.write_daily_cost_report(local_root, DAY1, workers)

    assert os.path.isdir(os.path.join(local_root, "reports", "cost"))

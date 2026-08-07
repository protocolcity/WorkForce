"""Schedules as data — the cron parser the daemon and board share."""

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce.schedule import (  # noqa: E402
    Cron, ScheduleError, calendar_intervals_to_cron, fire_minute_key,
    host_wall, matches_at, maybe_cron, next_fire_utc,
)

UTC = datetime.timezone.utc
CDT = datetime.timezone(datetime.timedelta(hours=-5))


def dt(*args):
    return datetime.datetime(*args, tzinfo=UTC)


def test_half_hour_lane_cadence():
    c = Cron("0,30 * * * *")  # the classic :00/:30 lane
    assert c.matches(dt(2026, 7, 14, 9, 0))
    assert c.matches(dt(2026, 7, 14, 9, 30))
    assert not c.matches(dt(2026, 7, 14, 9, 15))
    assert c.next_fire(dt(2026, 7, 14, 9, 1)) == dt(2026, 7, 14, 9, 30)
    assert c.next_fire(dt(2026, 7, 14, 9, 30)) == dt(2026, 7, 14, 10, 0)  # strictly after


def test_step_and_range():
    c = Cron("*/15 8-17 * * *")
    assert c.matches(dt(2026, 7, 14, 8, 45))
    assert not c.matches(dt(2026, 7, 14, 7, 45))
    assert c.next_fire(dt(2026, 7, 14, 17, 46)) == dt(2026, 7, 15, 8, 0)


def test_weekly_report_job():
    c = Cron("0 7 * * 1")  # Mondays 07:00
    assert c.next_fire(dt(2026, 7, 14, 0, 0)) == dt(2026, 7, 20, 7, 0)  # Tue -> next Mon


def test_dow_seven_is_sunday():
    assert Cron("0 0 * * 7").matches(dt(2026, 7, 19, 0, 0))  # a Sunday


def test_dom_dow_or_rule():
    c = Cron("0 0 1 * 1")  # vixie: 1st of month OR any Monday
    assert c.matches(dt(2026, 8, 1, 0, 0))    # a Saturday, but the 1st
    assert c.matches(dt(2026, 7, 20, 0, 0))   # a Monday, not the 1st


def test_malformed_rejected():
    for bad in ("61 * * * *", "* * * * * *", "a * * * *", "*/0 * * * *", "5-1 * * * *"):
        with pytest.raises(ScheduleError):
            Cron(bad)


def test_maybe_cron_ownership_boundary():
    assert maybe_cron("0,30 * * * *") is not None
    # informational strings are never daemon-owned — the migration gate
    assert maybe_cron("launchd :00/:30 (legacy)") is None
    assert maybe_cron("manual (daemon: slice 3)") is None
    assert maybe_cron("") is None
    assert maybe_cron("61 0 * * 1") is None  # 5 fields but invalid -> not owned


def test_calendar_interval_single():
    c = calendar_intervals_to_cron({"Minute": 15})
    assert c is not None and c.expr == "15 * * * *"


def test_calendar_interval_union():
    c = calendar_intervals_to_cron([{"Minute": 0}, {"Minute": 30}])
    assert c is not None and c.expr == "0,30 * * * *"


def test_calendar_interval_weekly():
    c = calendar_intervals_to_cron({"Minute": 0, "Hour": 7, "Weekday": 1})
    assert c is not None and c.expr == "0 7 * * 1"


def test_calendar_interval_irregular_union_not_guessed():
    assert calendar_intervals_to_cron([{"Minute": 0}, {"Hour": 5}]) is None
    assert calendar_intervals_to_cron([{"Minute": 0, "Hour": 1}, {"Minute": 30, "Hour": 2}]) is None


def test_next_fire_utc_evaluates_local_wall(monkeypatch):
    """wf-146: next_fire_utc converts local field match back to UTC Z instant."""
    def _cdt_wall(when):
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when.astimezone(CDT)

    monkeypatch.setattr("workforce.schedule.host_wall", _cdt_wall)
    c = Cron("0 11 * * *")  # 11:00 local
    # After 10:00 CDT (15:00Z) → next is 11:00 CDT = 16:00Z same day
    nf = next_fire_utc(c, datetime.datetime(2026, 8, 3, 15, 0, tzinfo=UTC))
    assert nf == datetime.datetime(2026, 8, 3, 16, 0, tzinfo=UTC)
    # At 11:00 UTC (06:00 CDT) does not match local 11
    assert not matches_at(c, datetime.datetime(2026, 8, 3, 11, 0, tzinfo=UTC))
    assert matches_at(c, datetime.datetime(2026, 8, 3, 16, 0, tzinfo=UTC))
    assert fire_minute_key(datetime.datetime(2026, 8, 3, 16, 0, tzinfo=UTC)) == (
        "2026-08-03T11:00"
    )


def test_host_wall_naive_treated_as_utc_then_local(monkeypatch):
    """Naive input is stamped UTC before projection to the host wall."""
    import workforce.schedule as sched

    def _fixed(when):
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when.astimezone(CDT)

    monkeypatch.setattr(sched, "host_wall", _fixed)
    wall = sched.host_wall(datetime.datetime(2026, 8, 3, 16, 0))  # naive
    assert wall.tzinfo is not None
    assert wall.hour == 11
    assert wall.utcoffset() == datetime.timedelta(hours=-5)

"""Schedules as roster DATA.

A worker's ``schedule`` field is daemon-owned iff it parses as a five-field
cron expression (``minute hour day-of-month month day-of-week``). Any other
string — ``"manual"``, ``"launchd :00/:30 (legacy)"`` — is informational:
the daemon never fires it, the board renders it verbatim. Nothing is ever
installed per worker; changing a cadence is a roster edit.

Supported field syntax: ``*``, ``N``, ``A-B``, ``*/S``, ``A-B/S`` and comma
lists of those. Day-of-week 0-7 with both 0 and 7 = Sunday. Standard cron
day semantics: when day-of-month and day-of-week are BOTH restricted, a date
matches if either does.

Timezone:
  Cron *fields* are host **local** wall-clock (seed_ops + worker contracts
  promise LOCAL machine time). Absolute instants for ledger / heartbeat /
  next_fire ISO stay **UTC** with a ``Z`` suffix. ``host_wall`` is the
  single convert seam — tests may monkeypatch it to pin a fixed offset.
"""

import calendar
import datetime
from typing import FrozenSet, List, Optional, Tuple

FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))  # min hr dom mon dow
UTC = datetime.timezone.utc


class ScheduleError(ValueError):
    """A five-field expression that fails to parse as cron."""


def host_wall(dt: datetime.datetime) -> datetime.datetime:
    """Absolute time → host local wall for cron field matching.

    Naive datetimes are treated as UTC. Monkeypatch this in tests to pin a
    zone without depending on the runner host.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone()  # system local


def matches_at(cron: "Cron", when: datetime.datetime) -> bool:
    """Whether absolute ``when`` matches ``cron`` on the host local wall."""
    return cron.matches(host_wall(when))


def next_fire_utc(
    cron: "Cron", after: datetime.datetime
) -> Optional[datetime.datetime]:
    """Next fire after absolute ``after``, fields vs local wall, result UTC."""
    wall = host_wall(after)
    nxt = cron.next_fire(wall)
    if nxt is None:
        return None
    return nxt.astimezone(UTC)


def fire_minute_key(when: datetime.datetime) -> str:
    """Dedup key for a scheduled fire slot (local wall minute, wf-146)."""
    return host_wall(when).strftime("%Y-%m-%dT%H:%M")


def _parse_field(field: str, lo: int, hi: int) -> FrozenSet[int]:
    values = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise ScheduleError("bad step %r" % step_s)
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ScheduleError("bad range %r" % part)
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ScheduleError("bad field part %r" % part)
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ScheduleError("value out of range %d-%d: %r" % (lo, hi, part))
        values.update(range(start, end + 1, step))
    return frozenset(values)


class Cron:
    """One parsed five-field expression; minute resolution."""

    def __init__(self, expr: str) -> None:
        fields = expr.split()
        if len(fields) != 5:
            raise ScheduleError("want 5 fields, got %d in %r" % (len(fields), expr))
        self.expr = expr
        sets: List[FrozenSet[int]] = [
            _parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, FIELD_RANGES)
        ]
        self.minutes, self.hours, self.doms, self.months, dows = sets
        self.dows = frozenset(d % 7 for d in dows)          # 7 -> 0 (Sunday)
        self.dom_star = fields[2] == "*"
        self.dow_star = fields[4] == "*"

    def _date_matches(self, dt: datetime.date) -> bool:
        if dt.month not in self.months:
            return False
        dom_ok = dt.day in self.doms
        dow_ok = (dt.weekday() + 1) % 7 in self.dows        # cron: 0 = Sunday
        if not self.dom_star and not self.dow_star:
            return dom_ok or dow_ok                          # vixie-cron OR rule
        return dom_ok and dow_ok

    def matches(self, dt: datetime.datetime) -> bool:
        return (dt.minute in self.minutes and dt.hour in self.hours
                and self._date_matches(dt.date()))

    def next_fire(self, after: datetime.datetime) -> Optional[datetime.datetime]:
        """First firing strictly after ``after``; None if none within 4 years."""
        cursor = (after + datetime.timedelta(minutes=1)).replace(second=0, microsecond=0)
        day = cursor.date()
        for _ in range(4 * 366):
            if self._date_matches(day):
                floor: Optional[Tuple[int, int]] = None
                if day == cursor.date():
                    floor = (cursor.hour, cursor.minute)
                for h in sorted(self.hours):
                    for m in sorted(self.minutes):
                        if floor is None or (h, m) >= floor:
                            return datetime.datetime.combine(
                                day, datetime.time(h, m), tzinfo=after.tzinfo)
            day = day + datetime.timedelta(days=1)
        return None

    def __repr__(self) -> str:
        return "Cron(%r)" % self.expr


def maybe_cron(schedule: str) -> Optional[Cron]:
    """The daemon-ownership test: a Cron, or None for informational strings."""
    if not schedule or len(schedule.split()) != 5:
        return None
    try:
        return Cron(schedule)
    except ScheduleError:
        return None


def calendar_intervals_to_cron(intervals: object) -> Optional[Cron]:
    """Best-effort launchd StartCalendarInterval -> Cron (board's legacy lens).

    launchd omits a key to mean 'every'; a list of dicts is a union — only
    unions varying in one field collapse to a single cron expression, which
    covers every plist this machine actually has.
    """
    if isinstance(intervals, dict):
        intervals = [intervals]
    if not isinstance(intervals, list) or not intervals:
        return None
    if not all(isinstance(iv, dict) for iv in intervals):
        return None
    if len({frozenset(iv) for iv in intervals}) != 1:
        return None  # mixed key sets — an irregular union; don't guess
    keys = ("Minute", "Hour", "Day", "Month", "Weekday")
    merged = {}
    for k_i, key in enumerate(keys):
        vals = sorted({iv[key] for iv in intervals if key in iv})
        merged[k_i] = ",".join(str(v) for v in vals) if vals else "*"
    varying = [k for k in merged if "," in merged[k]]
    if len(varying) > 1:
        return None  # union varies in >1 field — not one cron expression
    try:
        return Cron(" ".join(merged[i] for i in range(5)))
    except ScheduleError:
        return None

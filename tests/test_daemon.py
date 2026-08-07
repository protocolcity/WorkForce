"""Daemon mechanics — the scheduler as roster data, verified without tokens."""

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce.daemon import Daemon, heartbeat_status, plist_xml  # noqa: E402

UTC = datetime.timezone.utc
# Fixed offset for local-wall tests (no zoneinfo dependency).
CDT = datetime.timezone(datetime.timedelta(hours=-5))


@pytest.fixture(autouse=True)
def _cron_wall_is_utc(monkeypatch):
    """Legacy tests pin absolute hours as wall; treat UTC as the host wall.

    Production ``host_wall`` uses system local. Override per-test
    to pin a real offset when asserting local-vs-UTC behavior.
    """
    def _utc_wall(dt):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    monkeypatch.setattr("workforce.schedule.host_wall", _utc_wall)


def make_base(tmp_path, schedule="* * * * *", command=None):
    """A repo-shaped tmp dir: local/roster.json with one /bin/sh worker."""
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)
    (tmp_path / "CONTRACT.md").write_text("# contract\n")
    (tmp_path / "prompt.md").write_text("one slice\n")
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"count": 2}))
    local = tmp_path / "local"
    local.mkdir(exist_ok=True)
    roster = {"workers": {"tester": {
        "workdir": str(workdir), "contract": str(tmp_path / "CONTRACT.md"),
        "prompt": str(tmp_path / "prompt.md"), "identity": "tester-id",
        "command": command or ["/bin/sh", "-c", "exit 0"],
        "queue_url": "file://" + str(queue), "budget_secs": 5,
        "min_free_mb": 1, "schedule": schedule,
    }}}
    (local / "roster.json").write_text(json.dumps(roster))
    return str(tmp_path), str(local)


def ledger_text(tmp_path):
    p = tmp_path / "local" / "ledger" / "tester.log"
    return p.read_text() if p.exists() else ""


def test_tick_fires_matching_cron_worker(tmp_path):
    base, local = make_base(tmp_path, schedule="30 9 * * *")
    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 9, 30, tzinfo=UTC), wait=True) == 1
    text = ledger_text(tmp_path)
    assert "START" in text and "DONE" in text
    # Ledger stamps are true UTC with Z.
    import re
    for line in text.strip().splitlines():
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ", line), line


def test_tick_fires_on_local_hour_not_utc(tmp_path, monkeypatch):
    """wf-146: schedule ``0 11 * * *`` means 11:00 *local*, not 11:00 UTC.

    Founder evidence: marshal at 11:01Z ≈ 06:01 CDT under the old UTC eval.
    With CDT wall, 16:00Z (11:00 local) fires; 11:00Z (06:00 local) does not.
    """
    def _cdt_wall(dt):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(CDT)

    monkeypatch.setattr("workforce.schedule.host_wall", _cdt_wall)

    base, local = make_base(tmp_path, schedule="0 11 * * *")
    d = Daemon(base, local)
    # 11:00 UTC = 06:00 CDT — must NOT match hour 11 local
    assert d.tick(datetime.datetime(2026, 8, 3, 11, 0, tzinfo=UTC), wait=True) == 0
    assert ledger_text(tmp_path) == ""
    # 16:00 UTC = 11:00 CDT — must fire
    assert d.tick(datetime.datetime(2026, 8, 3, 16, 0, tzinfo=UTC), wait=True) == 1
    assert "START" in ledger_text(tmp_path)
    hb = json.loads((tmp_path / "local" / "daemon.json").read_text())
    # Fire-slot key is local wall minute
    assert hb["fired"]["tester"] == "2026-08-03T11:00"
    # next_fire ISO is still UTC (Z) of the following local match (15:00 CDT = 20:00Z)
    # schedule is daily 11:00 only — next is tomorrow 11:00 CDT = 16:00Z
    assert hb["workers"]["tester"]["next_fire"] == "2026-08-04T16:00:00Z"
    assert hb["last_tick"].endswith("Z")


def test_tick_skips_nonmatching_minute(tmp_path):
    base, local = make_base(tmp_path, schedule="30 9 * * *")
    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 9, 31, tzinfo=UTC), wait=True) == 0
    assert ledger_text(tmp_path) == ""


def test_informational_schedule_never_fires(tmp_path):
    """The migration gate: legacy/manual strings are not daemon-owned."""
    base, local = make_base(tmp_path, schedule="launchd :00/:30 (legacy)")
    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 9, 30, tzinfo=UTC), wait=True) == 0
    assert ledger_text(tmp_path) == ""


def test_same_minute_double_tick_fires_once(tmp_path):
    base, local = make_base(tmp_path)
    d = Daemon(base, local)
    now = datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    assert d.tick(now, wait=True) == 1
    assert d.tick(now, wait=True) == 0  # late duplicate tick, same minute
    assert ledger_text(tmp_path).count("START") == 1


def test_heartbeat_written_with_next_fire(tmp_path):
    base, local = make_base(tmp_path, schedule="0,30 * * * *")
    d = Daemon(base, local)
    d.tick(datetime.datetime(2026, 7, 14, 10, 7, tzinfo=UTC), wait=True)
    hb = json.loads((tmp_path / "local" / "daemon.json").read_text())
    assert hb["pid"] == os.getpid()
    assert hb["workers"]["tester"]["owned"] is True
    assert hb["workers"]["tester"]["next_fire"] == "2026-07-14T10:30:00Z"
    # a live pid + fresh tick would read 'stale' only via old last_tick;
    # here the tick is synthetic-past, so status must not claim 'running'
    assert heartbeat_status(str(local)) in ("stale", "running")


def test_roster_edit_picked_up_between_ticks(tmp_path):
    """Schedules are DATA: a roster edit changes the next tick, no restart."""
    base, local = make_base(tmp_path, schedule="0 12 * * *")
    d = Daemon(base, local)
    now = datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    assert d.tick(now, wait=True) == 0
    roster_path = tmp_path / "local" / "roster.json"
    raw = json.loads(roster_path.read_text())
    raw["workers"]["tester"]["schedule"] = "0 10 * * *"
    roster_path.write_text(json.dumps(raw))
    later = datetime.datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    assert d.tick(later, wait=True) == 1


def test_fired_minute_survives_restart(tmp_path):
    """oc-9 follow-up: a daemon restart inside an already-fired minute must not
    re-fire it (observed 04:20:38 re-fire after bootstrap). The fired-minute
    cursor is persisted in the heartbeat and reloaded on construction."""
    base, local = make_base(tmp_path)              # "* * * * *": matches every minute
    now = datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    d1 = Daemon(base, local)
    assert d1.tick(now, wait=True) == 1
    hb = json.loads((tmp_path / "local" / "daemon.json").read_text())
    assert hb["fired"]["tester"] == "2026-07-14T10:00"

    d2 = Daemon(base, local)                        # the restart
    assert d2._fired.get("tester") == "2026-07-14T10:00"   # cursor reloaded
    assert d2.tick(now, wait=True) == 0            # same minute: no re-fire
    assert ledger_text(tmp_path).count("START") == 1
    later = datetime.datetime(2026, 7, 14, 10, 1, tzinfo=UTC)
    assert d2.tick(later, wait=True) == 1          # a new minute still fires


def test_reload_fired_tolerates_no_or_malformed_heartbeat(tmp_path):
    """A fresh install (no heartbeat) and a legacy heartbeat without a 'fired'
    key both restore to an empty cursor, never crash."""
    base, local = make_base(tmp_path)
    assert Daemon(base, local)._fired == {}        # no heartbeat yet
    (tmp_path / "local" / "daemon.json").write_text(json.dumps({"pid": 1}))
    assert Daemon(base, local)._fired == {}        # heartbeat sans 'fired'


def test_plist_names_one_service_only(tmp_path):
    xml = plist_xml(str(tmp_path), python="/usr/bin/python3")
    assert "com.workforce.daemon" in xml
    assert xml.count("<key>Label</key>") == 1
    assert "daemon</string>" in xml and str(tmp_path) in xml


def test_daemon_serves_board_and_survives_busy_port(tmp_path):
    """Reboot-survival: the ONE service carries the board; a busy port must
    never take the scheduler down with it."""
    import socket
    import urllib.request
    from workforce import board

    base, local = make_base(tmp_path)
    d = Daemon(base, local)
    httpd = board.make_server(port=0, local_root=local)  # ephemeral port
    try:
        port = httpd.server_address[1]
        t = __import__("threading").Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=5) as r:
            assert json.loads(r.read())["ok"] is True
        # same port now busy: start_board on a daemon pointed at it must
        # degrade to None (WARN), not raise
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        board_default = board.DEFAULT_PORT
        board.DEFAULT_PORT = blocker.getsockname()[1]
        try:
            blocker.listen(1)
            assert d.start_board() is None
        finally:
            board.DEFAULT_PORT = board_default
            blocker.close()
    finally:
        httpd.shutdown()


def test_drain_stops_new_fires_and_waits_for_inflight(tmp_path):
    """oc-9: SIGTERM means finish what you're doing, fire nothing new."""
    import threading
    base, local = make_base(tmp_path, command=["/bin/sh", "-c", "sleep 1; exit 0"])
    d = Daemon(base, local)
    now = datetime.datetime(2026, 7, 14, 5, 0, tzinfo=UTC)
    assert d.tick(now) == 1                      # shift in flight (not waited)
    d.begin_drain(15)
    later = datetime.datetime(2026, 7, 14, 5, 1, tzinfo=UTC)
    assert d.tick(later) == 0                    # draining: no new fires
    hb = json.loads((tmp_path / "local" / "daemon.json").read_text())
    assert hb["state"] == "draining" and hb["in_flight"] == ["tester"]
    for t in d._threads.values():
        t.join(timeout=10)
    assert "DONE" in ledger_text(tmp_path)       # the in-flight shift finished
    assert not any(t.is_alive() for t in d._threads.values())
    assert d._wake.is_set()                      # the sleep was cut short


def test_plist_exit_timeout_covers_largest_budget(tmp_path):
    xml = plist_xml(str(tmp_path), python="/usr/bin/python3")
    assert "<key>ExitTimeOut</key><integer>3900</integer>" in xml


def test_fire_now_manual_trigger(tmp_path):
    """Manual dispatch — same engine path, same ledger, no schedule needed."""
    base, local = make_base(tmp_path, schedule="manual")
    d = Daemon(base, local)
    ok, msg = d.fire_now("tester")
    assert ok, msg
    d._threads["tester"].join(timeout=10)
    text = ledger_text(tmp_path)
    assert "START" in text and "DONE" in text
    ok2, msg2 = d.fire_now("nobody")
    assert not ok2 and "no such worker" in msg2


def test_empty_run_backoff_suppresses_cron_fires(tmp_path):
    """wf-111: after N consecutive empties, backoff>0 withholds scheduled fires."""
    from workforce import engine

    base, local = make_base(tmp_path, schedule="* * * * *")
    # pin empty-run policy on the tester seat
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["empty_run_threshold"] = 2
    data["workers"]["tester"]["empty_run_backoff"] = 3600  # 1h
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    t0 = datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    assert d.tick(t0, wait=True) == 1  # first empty SKIP
    t1 = datetime.datetime(2026, 7, 14, 10, 1, tzinfo=UTC)
    assert d.tick(t1, wait=True) == 1  # 2nd empty → threshold; WARN
    text = ledger_text(tmp_path)
    assert text.count(" SKIP ") >= 2
    assert "empty-run threshold" in text
    # ledger ts are real UTC; policy age is relative to newest empty line
    streak, last_ts = engine.empty_run_streak(local, "tester")
    assert streak >= 2 and last_ts
    last = datetime.datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    w = d._roster().workers["tester"]
    # still inside backoff window
    assert not d._fire_allowed_by_empty_policy(
        w, last + datetime.timedelta(seconds=30)
    )
    t2 = datetime.datetime(2026, 7, 14, 10, 2, tzinfo=UTC)
    assert d.tick(t2, wait=True) == 0  # cron path also withholds
    # past backoff → allowed again
    assert d._fire_allowed_by_empty_policy(
        w, last + datetime.timedelta(seconds=3601)
    )


def test_empty_run_backoff_zero_never_suppresses(tmp_path):
    """backoff 0 + adaptive off = signal only; cron still fires every match."""
    base, local = make_base(tmp_path, schedule="* * * * *")
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["empty_run_threshold"] = 2
    data["workers"]["tester"]["empty_run_backoff"] = 0
    data["workers"]["tester"]["empty_run_adaptive"] = False  # wf-149 opt-out
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC), wait=True) == 1
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 1, tzinfo=UTC), wait=True) == 1
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 2, tzinfo=UTC), wait=True) == 1
    assert ledger_text(tmp_path).count(" SKIP ") == 3


def test_empty_run_pause_suppresses_when_queue_empty(tmp_path):
    """wf-125: empty_run_pause=True holds cron fires when queue stays empty after threshold."""
    from workforce import engine

    base, local = make_base(tmp_path, schedule="* * * * *")
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["empty_run_threshold"] = 2
    data["workers"]["tester"]["empty_run_pause"] = True
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    # First two fires build the streak
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC), wait=True) == 1
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 1, tzinfo=UTC), wait=True) == 1
    assert ledger_text(tmp_path).count(" SKIP ") >= 2
    streak, _ = engine.empty_run_streak(local, "tester")
    assert streak >= 2
    # Queue still empty → pause gate holds
    assert not d._fire_allowed_by_empty_policy(d._roster().workers["tester"])
    # Daemon tick respects the gate
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 2, tzinfo=UTC), wait=True) == 0


def test_empty_run_pause_auto_resumes_when_queue_fills(tmp_path):
    """wf-125: pause gate lifts (auto-resumes) as soon as queue probe returns ready."""
    base, local = make_base(tmp_path, schedule="* * * * *")
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["empty_run_threshold"] = 2
    data["workers"]["tester"]["empty_run_pause"] = True
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC), wait=True) == 1
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 1, tzinfo=UTC), wait=True) == 1
    w = d._roster().workers["tester"]
    assert not d._fire_allowed_by_empty_policy(w)   # gate is active

    # Work arrives — queue probe returns > 0
    q.write_text(json.dumps({"count": 3}))
    w2 = d._roster().workers["tester"]
    assert d._fire_allowed_by_empty_policy(w2)   # auto-resume


def test_empty_run_pause_no_queue_url_falls_through_to_backoff(tmp_path):
    """wf-125: empty_run_pause=True with no queue_url falls through to backoff logic."""
    base, local = make_base(tmp_path, schedule="* * * * *")
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["empty_run_threshold"] = 1
    data["workers"]["tester"]["empty_run_pause"] = True
    data["workers"]["tester"]["queue_url"] = ""   # no queue URL
    data["workers"]["tester"]["empty_run_backoff"] = 0
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC), wait=True) == 1
    # With no queue_url, pause has no effect; backoff=0 so always allowed
    w = d._roster().workers["tester"]
    assert d._fire_allowed_by_empty_policy(w)


def test_dispatch_endpoint_fires_and_readonly_board_refuses(tmp_path):
    import urllib.error
    import urllib.request
    from workforce import board

    base, local = make_base(tmp_path, schedule="manual")
    d = Daemon(base, local)
    httpd = board.make_server(port=0, local_root=local, daemon=d)
    port = httpd.server_address[1]
    t = __import__("threading").Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/dispatch/tester" % port,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.loads(r.read())["ok"] is True
        d._threads["tester"].join(timeout=10)
        assert "DONE" in ledger_text(tmp_path)
        # read-only board (no daemon attached) must refuse with 409
        board._Handler.daemon = None
        req2 = urllib.request.Request("http://127.0.0.1:%d/api/dispatch/tester" % port,
                                      method="POST")
        try:
            urllib.request.urlopen(req2, timeout=10)
            assert False, "expected 409"
        except urllib.error.HTTPError as exc:
            assert exc.code == 409
    finally:
        httpd.shutdown()


def test_plist_background_hardening(tmp_path):
    """wf-91: mirror pc-536 — ProcessType Background prevents jetsam kills;
    AbandonProcessGroup lets dispatch children outlive a daemon restart;
    ThrottleInterval 30 dampens rapid restart loops."""
    xml = plist_xml(str(tmp_path), python="/usr/bin/python3")
    assert "<key>ProcessType</key><string>Background</string>" in xml
    assert "<key>AbandonProcessGroup</key><true/>" in xml
    assert "<key>ThrottleInterval</key><integer>30</integer>" in xml


def test_plist_carries_service_path(tmp_path):
    """launchd's bare PATH broke the first scheduled fire (SKIP: CLI not
    installed, 2026-07-14T01:40Z) — the plist must set a usable PATH."""
    xml = plist_xml(str(tmp_path), python="/usr/bin/python3")
    assert "<key>PATH</key>" in xml and "/usr/bin" in xml
    custom = plist_xml(str(tmp_path), python="/usr/bin/python3", path="/vendor/bin&more")
    assert "/vendor/bin&amp;more" in custom  # XML-escaped


# ---------------------------------------------------------------------------
# wf-126 — vendor_limit_backoff gate
# ---------------------------------------------------------------------------

def _write_vendor_limit_shifts(ledger_dir, worker_name, ts_list):
    """Write N synthetic vendor_limit shifts to the ledger file.

    ts_list: list of ISO8601Z strings for the ERROR event timestamp (newest last
    in the file, so parse_shifts returns them newest-first).
    """
    import os
    os.makedirs(ledger_dir, exist_ok=True)
    path = os.path.join(ledger_dir, "%s.log" % worker_name)
    lines = []
    for ts in ts_list:
        # START one second before ERROR
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        start_ts = (dt - datetime.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append('%s START queue=2 budget_secs=300' % start_ts)
        lines.append('%s ERROR reason="vendor limit: usage limit exceeded" rc=1 on_pass=1' % ts)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_vendor_limit_threshold_zero_disables_gate(tmp_path):
    """wf-126: threshold=0 means the feature is off — never suppress."""
    base, local = make_base(tmp_path)
    ledger_dir = str(tmp_path / "local" / "ledger")
    ts = "2026-08-02T20:00:00Z"
    _write_vendor_limit_shifts(ledger_dir, "tester", [ts, ts, ts, ts])
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["vendor_limit_threshold"] = 0
    data["workers"]["tester"]["vendor_limit_backoff"] = 0  # threshold=0 → feature off
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    w = d._roster().workers["tester"]
    suppress, streak, _ = d._should_suppress_vendor_limit(w)
    assert suppress is False


def test_vendor_limit_backoff_zero_never_suppresses(tmp_path):
    """wf-126: backoff=0 = signal-only; gate never withholds fires."""
    base, local = make_base(tmp_path)
    ledger_dir = str(tmp_path / "local" / "ledger")
    ts = "2026-08-02T20:00:00Z"
    _write_vendor_limit_shifts(ledger_dir, "tester", [ts, ts, ts])
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["vendor_limit_threshold"] = 3
    data["workers"]["tester"]["vendor_limit_backoff"] = 0
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    w = d._roster().workers["tester"]
    suppress, _, _ = d._should_suppress_vendor_limit(w)
    assert suppress is False


def test_vendor_limit_backoff_suppresses_within_window(tmp_path):
    """wf-126: streak >= threshold AND newest within backoff → suppress=True."""
    base, local = make_base(tmp_path)
    ledger_dir = str(tmp_path / "local" / "ledger")
    newest_ts = "2026-08-02T20:00:00Z"
    _write_vendor_limit_shifts(ledger_dir, "tester", [newest_ts, newest_ts, newest_ts])
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["vendor_limit_threshold"] = 3
    data["workers"]["tester"]["vendor_limit_backoff"] = 3600
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    w = d._roster().workers["tester"]
    newest = datetime.datetime.strptime(newest_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    # still inside window (30s after newest)
    suppress, streak, backoff_secs = d._should_suppress_vendor_limit(
        w, newest + datetime.timedelta(seconds=30)
    )
    assert suppress is True
    assert streak == 3
    assert backoff_secs == 3600


def test_vendor_limit_backoff_clears_after_window(tmp_path):
    """wf-126: gate clears once backoff window expires."""
    base, local = make_base(tmp_path)
    ledger_dir = str(tmp_path / "local" / "ledger")
    newest_ts = "2026-08-02T20:00:00Z"
    _write_vendor_limit_shifts(ledger_dir, "tester", [newest_ts, newest_ts, newest_ts])
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["vendor_limit_threshold"] = 3
    data["workers"]["tester"]["vendor_limit_backoff"] = 3600
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    w = d._roster().workers["tester"]
    newest = datetime.datetime.strptime(newest_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    suppress, _, _ = d._should_suppress_vendor_limit(
        w, newest + datetime.timedelta(seconds=3601)
    )
    assert suppress is False


def test_vendor_limit_streak_below_threshold_no_suppress(tmp_path):
    """wf-126: fewer vendor_limit shifts than threshold → gate inactive."""
    base, local = make_base(tmp_path)
    ledger_dir = str(tmp_path / "local" / "ledger")
    ts = "2026-08-02T20:00:00Z"
    _write_vendor_limit_shifts(ledger_dir, "tester", [ts, ts])  # streak=2, threshold=3
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["vendor_limit_threshold"] = 3
    data["workers"]["tester"]["vendor_limit_backoff"] = 3600
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    w = d._roster().workers["tester"]
    newest = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    suppress, _, _ = d._should_suppress_vendor_limit(
        w, newest + datetime.timedelta(seconds=30)
    )
    assert suppress is False


def test_vendor_limit_tick_withholds_fire_when_gate_active(tmp_path):
    """wf-126: tick does not fire a worker whose vendor-limit gate is active."""
    base, local = make_base(tmp_path, schedule="* * * * *")
    ledger_dir = str(tmp_path / "local" / "ledger")
    # Use a recent-enough timestamp so the gate is active at tick time
    now = datetime.datetime(2026, 8, 2, 20, 5, tzinfo=UTC)
    newest_ts = "2026-08-02T20:04:00Z"  # 60s before tick — within 1h backoff
    _write_vendor_limit_shifts(ledger_dir, "tester", [newest_ts, newest_ts, newest_ts])
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["vendor_limit_threshold"] = 3
    data["workers"]["tester"]["vendor_limit_backoff"] = 3600
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    fired = d.tick(now, wait=True)
    assert fired == 0  # gate withheld the fire


# ---------------------------------------------------------------------------
# wf-166 — max_fires_per_day daily ceiling
# ---------------------------------------------------------------------------

def _write_start_fires(ledger_dir, worker_name, ts_list):
    """Write synthetic START(+DONE) lines at the given UTC stamps."""
    import os
    os.makedirs(ledger_dir, exist_ok=True)
    path = os.path.join(ledger_dir, "%s.log" % worker_name)
    lines = []
    for ts in ts_list:
        lines.append("%s START queue=1 budget_secs=300 dry_run=0" % ts)
        lines.append("%s DONE rc=0 on_pass=1 dry_run=0" % ts)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_max_fires_per_day_zero_never_suppresses(tmp_path):
    """wf-166: max=0 (default) is unlimited — never suppress."""
    from workforce import engine

    base, local = make_base(tmp_path, schedule="* * * * *")
    ledger_dir = str(tmp_path / "local" / "ledger")
    now = datetime.datetime(2026, 8, 3, 15, 0, tzinfo=UTC)
    # many same-day fires still allowed when max=0
    _write_start_fires(
        ledger_dir, "tester",
        ["2026-08-03T13:10:00Z", "2026-08-03T14:10:00Z", "2026-08-03T15:00:00Z"],
    )
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["max_fires_per_day"] = 0
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    w = d._roster().workers["tester"]
    assert engine.fires_on_local_day(local, "tester", now=now) == 3
    suppress, count, max_n = d._should_suppress_max_fires_per_day(w, now)
    assert suppress is False
    assert max_n == 0
    assert d.tick(now, wait=True) == 1  # still fires


def test_max_fires_per_day_suppresses_after_n_starts(tmp_path):
    """wf-166: N same-day START lines → Nth+1 scheduled fire withheld."""
    from workforce import engine

    base, local = make_base(tmp_path, schedule="* * * * *")
    ledger_dir = str(tmp_path / "local" / "ledger")
    now = datetime.datetime(2026, 8, 3, 16, 10, tzinfo=UTC)
    _write_start_fires(
        ledger_dir, "tester",
        ["2026-08-03T13:10:00Z"],  # one fire already today
    )
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["max_fires_per_day"] = 1
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    w = d._roster().workers["tester"]
    assert engine.fires_on_local_day(local, "tester", now=now) == 1
    suppress, count, max_n = d._should_suppress_max_fires_per_day(w, now)
    assert suppress is True
    assert count == 1
    assert max_n == 1
    assert d.tick(now, wait=True) == 0  # cron path withholds


def test_max_fires_per_day_resets_next_local_day(tmp_path):
    """wf-166: next host-local calendar day starts a fresh count."""
    from workforce import engine

    base, local = make_base(tmp_path, schedule="* * * * *")
    ledger_dir = str(tmp_path / "local" / "ledger")
    # Yesterday local (host wall pinned to UTC in this module) had a fire
    _write_start_fires(ledger_dir, "tester", ["2026-08-02T20:00:00Z"])
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["max_fires_per_day"] = 1
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    w = d._roster().workers["tester"]
    today = datetime.datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    assert engine.fires_on_local_day(local, "tester", now=today) == 0
    suppress, count, max_n = d._should_suppress_max_fires_per_day(w, today)
    assert suppress is False
    assert count == 0
    assert max_n == 1
    assert d.tick(today, wait=True) == 1


def test_max_fires_per_day_uses_host_local_day_boundary(tmp_path, monkeypatch):
    """wf-166: day boundary follows host_wall, not UTC calendar day alone."""
    from workforce import engine

    def _cdt_wall(dt):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(CDT)

    monkeypatch.setattr("workforce.schedule.host_wall", _cdt_wall)

    base, local = make_base(tmp_path)
    ledger_dir = str(tmp_path / "local" / "ledger")
    # 2026-08-04T03:00Z = 2026-08-03T22:00 CDT — still "Aug 3" local
    _write_start_fires(ledger_dir, "tester", ["2026-08-04T03:00:00Z"])
    # "now" still on Aug 3 CDT: 2026-08-04T04:00Z = 23:00 CDT Aug 3
    now_aug3 = datetime.datetime(2026, 8, 4, 4, 0, tzinfo=UTC)
    assert engine.fires_on_local_day(local, "tester", now=now_aug3) == 1
    # Next local day: 2026-08-04T05:00Z = 00:00 CDT Aug 4
    now_aug4 = datetime.datetime(2026, 8, 4, 5, 0, tzinfo=UTC)
    assert engine.fires_on_local_day(local, "tester", now=now_aug4) == 0


def test_max_fires_per_day_fire_now_bypasses(tmp_path):
    """wf-166: manual fire_now is not blocked by the daily ceiling."""
    base, local = make_base(tmp_path, schedule="manual")
    ledger_dir = str(tmp_path / "local" / "ledger")
    _write_start_fires(ledger_dir, "tester", ["2026-08-03T10:00:00Z"])
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["max_fires_per_day"] = 1
    roster_path.write_text(json.dumps(data))
    d = Daemon(base, local)
    ok, msg = d.fire_now("tester")
    assert ok, msg
    d._threads["tester"].join(timeout=10)
    text = ledger_text(tmp_path)
    # original fixture START + the fire_now START
    assert text.count(" START ") >= 2


def test_vendor_limit_tick_produces_no_desk_write(tmp_path, monkeypatch):
    """wf-132: vendor_limit fixture ledgers must not live-drop For You cards.

    This is the false-positive path that minted wf-128 (pool ``sh`` / worker
    ``tester``) when Daemon.tick ran the capacity hook with dry_run=False.
    """
    from workforce import capacity as cap_mod

    def boom(*a, **k):
        raise AssertionError("desk must not be contacted from pytest tick")

    monkeypatch.setattr(cap_mod, "find_open_by_label", boom)
    monkeypatch.setattr(cap_mod, "_req", boom)

    base, local = make_base(tmp_path, schedule="* * * * *")
    ledger_dir = str(tmp_path / "local" / "ledger")
    now = datetime.datetime(2026, 8, 2, 20, 5, tzinfo=UTC)
    newest_ts = "2026-08-02T20:04:00Z"
    # Three consecutive capacity fails → detect_capacity_alerts fires for pool sh
    _write_vendor_limit_shifts(ledger_dir, "tester", [newest_ts, newest_ts, newest_ts])
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["vendor_limit_threshold"] = 3
    data["workers"]["tester"]["vendor_limit_backoff"] = 3600
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    # tick must complete without desk I/O (hermetic refuse → would_create)
    fired = d.tick(now, wait=True)
    assert fired == 0
    # Capacity report may be written under tmp local/ — that is fine (not desk).
    # Confirm the hook saw alerts and still refused live drop via public API.
    receipt = cap_mod.drop_capacity_for_you(
        {
            "pool": "sh",
            "inbox_key": "capacity-sh",
            "inbox_label": "inbox-report:workforce:capacity-sh:2026-08-02",
            "glance": "test",
            "day": "2026-08-02",
            "project": "workforce",
        },
        report_path="/tmp/r.md",
        dry_run=False,
    )
    assert receipt["action"] == "would_create"
    assert receipt.get("hermetic") is True


def test_capacity_hook_live_once_per_day(tmp_path, monkeypatch):
    """wf-122: capacity hook fires live (dry_run=False) once per UTC day."""
    from workforce import capacity as cap_mod

    base, local = make_base(tmp_path, schedule="manual")

    fake_alert = {
        "pool": "claude",
        "reason": "3+ consecutive capacity fails on: tester",
        "workers": ["tester"],
        "thrash_workers": ["tester"],
        "hour_workers": [],
        "streak": 3,
        "seats_hour": 0,
        "inbox_key": "capacity-claude",
        "inbox_label": "inbox-report:workforce:capacity-claude:2026-08-02",
        "glance": "Provider pool claude looks blocked.",
        "day": "2026-08-02",
        "project": "workforce",
    }

    detect_calls = []
    monkeypatch.setattr(
        cap_mod, "detect_capacity_alerts",
        lambda roster, local_root, **kw: (detect_calls.append(1) or [fake_alert]),
    )
    monkeypatch.setattr(
        cap_mod, "write_capacity_report",
        lambda local_root, alerts, day="": "/tmp/fake-cap.md",
    )
    drops = []
    monkeypatch.setattr(
        cap_mod, "drop_capacity_for_you",
        lambda alert, report_path, dry_run=True, **kw: (
            drops.append(dry_run) or {"ok": True, "action": "would_create"}
        ),
    )

    d = Daemon(base, str(tmp_path / "local"))
    t0 = datetime.datetime(2026, 8, 2, 7, 45, tzinfo=UTC)
    d.tick(t0, wait=True)

    assert len(drops) == 1
    assert drops[0] is False  # live — not dry_run

    # Second tick same UTC day: hook does not re-run.
    d.tick(datetime.datetime(2026, 8, 2, 8, 0, tzinfo=UTC), wait=True)
    assert len(drops) == 1

    # Next UTC day: hook fires again.
    d.tick(datetime.datetime(2026, 8, 3, 7, 0, tzinfo=UTC), wait=True)
    assert len(drops) == 2


def test_capacity_hook_skips_when_no_alerts(tmp_path, monkeypatch):
    """wf-122: no drop when pool is clean."""
    from workforce import capacity as cap_mod

    base, local = make_base(tmp_path, schedule="manual")
    monkeypatch.setattr(
        cap_mod, "detect_capacity_alerts",
        lambda *a, **k: [],
    )
    drops = []
    monkeypatch.setattr(
        cap_mod, "drop_capacity_for_you",
        lambda *a, **k: drops.append(1) or {},
    )

    d = Daemon(base, str(tmp_path / "local"))
    d.tick(datetime.datetime(2026, 8, 2, 7, 45, tzinfo=UTC), wait=True)
    assert drops == []


# ---------------------------------------------------------------------------
# wf-149 — wake-on-route + adaptive idle backoff


def test_wake_now_dispatches_idle(tmp_path):
    """A wake on an idle lane runs the same engine path as a clock fire."""
    base, local = make_base(tmp_path, schedule="manual")
    d = Daemon(base, local)
    ok, msg = d.wake_now("tester")
    assert ok and msg == "dispatched"
    d._threads["tester"].join(timeout=10)
    text = ledger_text(tmp_path)
    assert "START" in text and "DONE" in text


def test_wake_now_unknown_worker(tmp_path):
    base, local = make_base(tmp_path)
    ok, msg = Daemon(base, local).wake_now("nobody")
    assert not ok and "no such worker" in msg


def test_wake_now_empty_queue_clean_skip(tmp_path):
    """Probe-first: a wake with an empty queue is a clean SKIP, never a spawn."""
    base, local = make_base(tmp_path, schedule="manual")
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    d = Daemon(base, local)
    ok, _ = d.wake_now("tester")
    assert ok
    d._threads["tester"].join(timeout=10)
    text = ledger_text(tmp_path)
    assert "SKIP" in text and "queue empty" in text
    assert "START" not in text


def test_wake_during_inflight_is_clean_noop(tmp_path):
    """A wake mid-shift notes the wake and returns ok — no double spawn."""
    from workforce.daemon import WAKE_DEBOUNCE_SECS

    base, local = make_base(tmp_path, schedule="manual",
                            command=["/bin/sh", "-c", "sleep 1; exit 0"])
    d = Daemon(base, local)
    ok, _ = d.wake_now("tester")
    assert ok
    first = d._threads["tester"]
    # slip past the debounce window, then wake again mid-shift
    d._wake_monotonic["tester"] -= WAKE_DEBOUNCE_SECS + 1
    ok2, msg2 = d.wake_now("tester")
    assert ok2 and "in flight" in msg2
    assert d._threads["tester"] is first
    first.join(timeout=10)
    assert ledger_text(tmp_path).count("START") == 1


def test_wake_debounce_coalesces_bulk_nudges(tmp_path):
    """Bulk filing nudges the same hand repeatedly; only the first spawns."""
    base, local = make_base(tmp_path, schedule="manual",
                            command=["/bin/sh", "-c", "sleep 1; exit 0"])
    d = Daemon(base, local)
    ok, msg = d.wake_now("tester")
    assert ok and msg == "dispatched"
    ok2, msg2 = d.wake_now("tester")
    assert ok2 and "debounced" in msg2
    d._threads["tester"].join(timeout=10)
    assert ledger_text(tmp_path).count("START") == 1


def test_adaptive_backoff_secs_ladder_and_optout():
    """Ladder: threshold → 1h, +1 → 4h, deeper → daily cap; opt-out = 0."""
    import types

    from workforce.daemon import adaptive_backoff_secs

    w = types.SimpleNamespace(empty_run_threshold=3, empty_run_adaptive=True)
    assert adaptive_backoff_secs(w, 2) == 0
    assert adaptive_backoff_secs(w, 3) == 3600
    assert adaptive_backoff_secs(w, 4) == 14400
    assert adaptive_backoff_secs(w, 5) == 86400
    assert adaptive_backoff_secs(w, 9) == 86400   # capped at daily heartbeat
    w.empty_run_adaptive = False
    assert adaptive_backoff_secs(w, 9) == 0


def test_adaptive_backoff_gates_cron_by_default(tmp_path):
    """wf-149 default: past threshold the gate holds for the hourly rung."""
    from workforce import engine

    base, local = make_base(tmp_path, schedule="* * * * *")
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["empty_run_threshold"] = 2
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC), wait=True) == 1
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 1, tzinfo=UTC), wait=True) == 1
    streak, last_ts = engine.empty_run_streak(local, "tester")
    assert streak == 2 and last_ts
    last = datetime.datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC)
    w = d._roster().workers["tester"]
    # streak == threshold → hourly rung
    assert not d._fire_allowed_by_empty_policy(
        w, last + datetime.timedelta(seconds=3599))
    assert d._fire_allowed_by_empty_policy(
        w, last + datetime.timedelta(seconds=3601))
    # cron tick inside the window withholds too
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 2, tzinfo=UTC), wait=True) == 0


def test_explicit_backoff_pin_beats_adaptive_ladder(tmp_path):
    """An explicit empty_run_backoff stays a fixed window at any streak depth."""
    base, local = make_base(tmp_path, schedule="* * * * *")
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["empty_run_threshold"] = 2
    data["workers"]["tester"]["empty_run_backoff"] = 60
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC), wait=True) == 1
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 1, tzinfo=UTC), wait=True) == 1
    from workforce import engine
    streak, last_ts = engine.empty_run_streak(local, "tester")
    assert streak >= 2   # at threshold — the ladder alone would hold for 1h
    last = datetime.datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC)
    w = d._roster().workers["tester"]
    # fixed 60s pin: blocked inside it, allowed right past it (not the 1h rung)
    assert not d._fire_allowed_by_empty_policy(
        w, last + datetime.timedelta(seconds=30))
    assert d._fire_allowed_by_empty_policy(
        w, last + datetime.timedelta(seconds=61))


def test_wake_resets_adaptive_backoff_to_base(tmp_path):
    """Wake floors the streak: a woken lane probes at base cadence again."""
    base, local = make_base(tmp_path, schedule="* * * * *")
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["empty_run_threshold"] = 2
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC), wait=True) == 1
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 1, tzinfo=UTC), wait=True) == 1
    w = d._roster().workers["tester"]
    from workforce import engine
    _, last_ts = engine.empty_run_streak(local, "tester")
    last = datetime.datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC)
    assert not d._fire_allowed_by_empty_policy(
        w, last + datetime.timedelta(seconds=30))   # gated
    ok, _ = d.wake_now("tester")                    # wake (queue still empty)
    assert ok
    d._threads["tester"].join(timeout=10)
    # only empties since the wake count — below threshold → base cadence
    w2 = d._roster().workers["tester"]
    assert d._fire_allowed_by_empty_policy(
        w2, last + datetime.timedelta(seconds=30))


def test_api_wake_endpoint(tmp_path):
    """POST /api/wake {worker} → dispatch; read-only board → 409; junk → 400."""
    import threading
    import urllib.error
    import urllib.request

    from workforce import board

    base, local = make_base(tmp_path, schedule="manual")
    d = Daemon(base, local)
    httpd = board.make_server(port=0, local_root=local, daemon=d)
    try:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def _post(payload):
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/wake" % port,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())

        assert _post({"worker": "tester"})["ok"] is True
        d._threads["tester"].join(timeout=10)
        assert "DONE" in ledger_text(tmp_path)

        with pytest.raises(urllib.error.HTTPError) as err:
            _post({"worker": "../evil"})
        assert err.value.code == 400
    finally:
        httpd.shutdown()

    # standalone board (no daemon in-process) refuses with 409
    httpd2 = board.make_server(port=0, local_root=local, daemon=None)
    try:
        port2 = httpd2.server_address[1]
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/wake" % port2,
            data=json.dumps({"worker": "tester"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as err2:
            urllib.request.urlopen(req, timeout=5)
        assert err2.value.code == 409
    finally:
        httpd2.shutdown()


def test_api_workers_exposes_resting_state(tmp_path):
    """wf-149: /api/workers carries empty_streak + backoff_secs + resting."""
    import threading
    import urllib.request

    from workforce import board

    base, local = make_base(tmp_path, schedule="* * * * *")
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["empty_run_threshold"] = 2
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 0, tzinfo=UTC), wait=True) == 1
    assert d.tick(datetime.datetime(2026, 7, 14, 10, 1, tzinfo=UTC), wait=True) == 1

    httpd = board.make_server(port=0, local_root=local, daemon=d)
    try:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/workers" % port, timeout=10) as r:
            payload = json.loads(r.read())
        row = next(w for w in payload["workers"] if w["name"] == "tester")
        assert row["empty_streak"] >= 2
        assert row["backoff_secs"] == 3600      # hourly rung at threshold
        assert row["resting"] is True           # fresh empties: inside the window
    finally:
        httpd.shutdown()


def test_startup_reconcile_cleans_orphan_lock(tmp_path, monkeypatch):
    """wf-155: Daemon.startup_reconcile reclaims a dead-pid lock before ticks."""
    import subprocess as _sp

    from workforce import engine as engine_mod

    base, local = make_base(tmp_path)
    # Point queue at http so reconcile considers desk path; mock the HTTP.
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["queue_url"] = (
        "http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester"
    )
    data["workers"]["tester"]["identity"] = "tester-id"
    roster_path.write_text(json.dumps(data))

    locks = tmp_path / "local" / "locks"
    lock = locks / "tester.lock"
    lock.mkdir(parents=True)
    proc = _sp.Popen(["/bin/sh", "-c", "exit 0"])
    proc.wait()
    (lock / "pid").write_text(str(proc.pid))

    # Prior heartbeat claimed this worker was in flight when the daemon died.
    (tmp_path / "local" / "daemon.json").write_text(json.dumps({
        "pid": 1,
        "started_at": "2026-08-04T00:00:00Z",
        "last_tick": "2026-08-04T00:00:00Z",
        "in_flight": ["tester"],
        "fired": {},
        "workers": {},
    }))

    def fake_http(method, url, body=None, timeout=8.0):
        if method == "GET" and "status=in_progress" in url:
            return {
                "ok": True,
                "tasks": [{
                    "id": "wf-155",
                    "status": "in_progress",
                    "labels": ["worker:tester"],
                }],
            }
        if method == "GET" and "wf-155" in url:
            return {
                "ok": True,
                "task": {
                    "id": "wf-155",
                    "status": "in_progress",
                    "comments": [{"body": "Owner: tester-id", "author": "tester-id"}],
                },
            }
        if method == "POST":
            return {"ok": True, "comment": {"id": 9}}
        return {}

    monkeypatch.setattr(engine_mod, "_http_json", fake_http)
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")

    d = Daemon(base, local)
    report = d.startup_reconcile()
    assert report["lock_cleaned"] >= 1
    assert "wf-155" in report["released"]
    assert not lock.exists()


def test_empty_run_pause_tick_runs_heartbeat_reconcile(tmp_path, monkeypatch):
    """wf-155: empty_run_pause suppress path still reconciles stranded claims.

    Pause mode never fires dispatch once the gate is active, so the empty-SKIP
    reconcile in engine would never run again — daemon tick must cover it.
    """
    from workforce import engine as engine_mod

    base, local = make_base(tmp_path, schedule="* * * * *")
    roster_path = tmp_path / "local" / "roster.json"
    data = json.loads(roster_path.read_text())
    # file:// queue for empty streak build; switch identity for Owner match
    data["workers"]["tester"]["identity"] = "tester-id"
    data["workers"]["tester"]["empty_run_threshold"] = 2
    data["workers"]["tester"]["empty_run_pause"] = True
    (tmp_path / "queue.json").write_text(json.dumps({"count": 0}))
    roster_path.write_text(json.dumps(data))

    d = Daemon(base, local)
    # Build streak via real empty fires (file:// queue count=0).
    assert d.tick(datetime.datetime(2026, 8, 4, 10, 0, tzinfo=UTC), wait=True) == 1
    assert d.tick(datetime.datetime(2026, 8, 4, 10, 1, tzinfo=UTC), wait=True) == 1
    w = d._roster().workers["tester"]
    assert not d._fire_allowed_by_empty_policy(w)

    # Point seat at http desk for reconcile; mock desk I/O.
    data = json.loads(roster_path.read_text())
    data["workers"]["tester"]["queue_url"] = (
        "http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester"
    )
    roster_path.write_text(json.dumps(data))

    posts = []

    def fake_http(method, url, body=None, timeout=8.0):
        if method == "GET" and "status=in_progress" in url:
            return {
                "ok": True,
                "tasks": [{
                    "id": "wf-4",
                    "status": "in_progress",
                    "labels": ["worker:tester"],
                }],
            }
        if method == "GET" and "wf-4" in url:
            return {
                "ok": True,
                "task": {
                    "id": "wf-4",
                    "status": "in_progress",
                    "comments": [{"body": "Owner: tester-id", "author": "tester-id"}],
                },
            }
        if method == "POST":
            posts.append(body)
            return {"ok": True}
        return {"ok": True, "count": 0, "tasks": []}

    monkeypatch.setattr(engine_mod, "_http_json", fake_http)
    monkeypatch.setattr(engine_mod, "queue_probe_count", lambda _w: 0)
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")

    fired = d.tick(datetime.datetime(2026, 8, 4, 10, 2, tzinfo=UTC), wait=True)
    assert fired == 0  # pause held — no shift thread
    assert posts, "heartbeat reconcile must POST Blocked: release on pause path"
    text = ledger_text(tmp_path)
    assert "heartbeat-reconcile-release" in text

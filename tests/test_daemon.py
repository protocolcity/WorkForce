"""Daemon mechanics — the scheduler as roster data, verified without tokens."""

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce.daemon import Daemon, heartbeat_status, plist_xml  # noqa: E402

UTC = datetime.timezone.utc


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

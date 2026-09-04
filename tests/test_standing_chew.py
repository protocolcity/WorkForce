"""wf-231 / pc-1419 — mill spawn retired; empty is SKIP, not a chew pass."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import engine  # noqa: E402

from test_engine import ledger_text, local, make_worker  # noqa: E402
from test_multipass import drain_command  # noqa: E402


def test_standing_chew_owed_always_false_for_lanes(tmp_path):
    w = make_worker(tmp_path)
    assert not engine.standing_chew_owed(local(tmp_path), w)


def test_standing_chew_not_owed_for_jobs(tmp_path):
    w = make_worker(tmp_path, kind="job")
    assert not engine.standing_chew_owed(local(tmp_path), w)


def test_wake_into_empty_skips_without_mill_spawn(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKFORCE_STANDING_CHEW", raising=False)
    marker = tmp_path / "ran"
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "touch %s" % marker],
        min_pass_secs=0,
    )
    (tmp_path / "queue.json").write_text(json.dumps({"ok": True, "count": 0}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert not marker.exists()
    text = ledger_text(tmp_path)
    assert " SKIP " in text and "queue empty" in text
    assert "standing_chew=1" not in text
    assert " START " not in text
    assert os.environ.get("WORKFORCE_STANDING_CHEW") != "1"


def test_dry_run_empty_also_skips(tmp_path):
    w = make_worker(tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({"ok": True, "count": 0}))
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    text = ledger_text(tmp_path)
    assert " SKIP " in text and "queue empty" in text
    assert "standing_chew=1" not in text
    assert not engine.standing_chew_owed(local(tmp_path), w)


def test_job_empty_still_skips_without_spawn(tmp_path):
    marker = tmp_path / "ran"
    w = make_worker(
        tmp_path,
        kind="job",
        command=["/bin/sh", "-c", "touch %s" % marker],
    )
    (tmp_path / "queue.json").write_text(json.dumps({"ok": True, "count": 0}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert not marker.exists()
    text = ledger_text(tmp_path)
    assert "SKIP" in text and "queue empty" in text
    assert " START " not in text


def test_legacy_skip_streak_does_not_owe_chew(tmp_path):
    w = make_worker(tmp_path)
    led_dir = tmp_path / "local" / "ledger"
    led_dir.mkdir(parents=True)
    (led_dir / "tester.log").write_text(
        "2026-08-30T00:00:00Z SKIP reason=\"queue empty\"\n"
        "2026-08-30T00:01:00Z SKIP reason=\"queue empty\"\n"
        "2026-08-30T00:02:00Z SKIP reason=\"queue empty\"\n"
    )
    assert not engine.standing_chew_owed(local(tmp_path), w)


def test_drain_loop_stops_when_queue_empty_no_extra_pass(tmp_path):
    q = tmp_path / "queue.json"
    w = make_worker(
        tmp_path,
        command=drain_command(q),
        max_passes=0,
        min_pass_secs=0,
        budget_secs=30,
    )
    q.write_text(json.dumps({"ok": True, "count": 2}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count(" DONE ") == 2
    assert "queue empty" in text
    assert "standing_chew=1" not in text
    assert json.loads(q.read_text())["count"] == 0


def test_standing_chew_env_not_set_on_empty_wake(tmp_path):
    seen = tmp_path / "env.txt"
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "printf %%s \"$WORKFORCE_STANDING_CHEW\" > %s"
                 % seen],
        min_pass_secs=0,
    )
    (tmp_path / "queue.json").write_text(json.dumps({"ok": True, "count": 0}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert not seen.exists()

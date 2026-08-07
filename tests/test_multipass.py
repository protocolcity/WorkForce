"""§6 multi-pass — the full-pattern lane behavior, token-free."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import engine  # noqa: E402

from test_engine import ledger_text, local, make_worker  # noqa: E402


def drain_command(queue_path):
    """A worker that 'finishes one ticket': decrements the queue file."""
    return ["/bin/sh", "-c",
            "python3 -c \"import json; p=%r; d=json.load(open(p)); "
            "d['count']-=1; json.dump(d, open(p,'w'))\"" % str(queue_path)]


def test_multipass_drains_until_queue_empty(tmp_path):
    q = tmp_path / "queue.json"
    w = make_worker(tmp_path, command=drain_command(q), max_passes=5, min_pass_secs=0)
    q.write_text(json.dumps({"ok": True, "count": 2}))  # after make_worker's default
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count("DONE") == 2
    assert "on_pass=2" in text and "queue empty" in text


def test_multipass_stops_at_pass_ceiling(tmp_path):
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"ok": True, "count": 50}))
    w = make_worker(tmp_path, command=drain_command(q), max_passes=3, min_pass_secs=0)
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count("DONE") == 3
    assert "max passes (3)" in text


def test_multipass_no_progress_stop(tmp_path):
    # worker succeeds but the queue never shrinks -> exactly one pass
    w = make_worker(tmp_path, max_passes=4, min_pass_secs=0)  # command: exit 0
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count("DONE") == 1
    assert "no progress (3 -> 3)" in text


def restock_command(queue_path):
    """Succeeds while growing the queue (close + file follow-up pattern)."""
    return ["/bin/sh", "-c",
            "python3 -c \"import json; p=%r; d=json.load(open(p)); "
            "d['count']+=1; json.dump(d, open(p,'w'))\"" % str(queue_path)]


def test_multipass_restocked_stop(tmp_path):
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"ok": True, "count": 3}))
    w = make_worker(tmp_path, command=restock_command(q), max_passes=4, min_pass_secs=0)
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count("DONE") == 1
    assert "restocked (3 -> 4)" in text
    assert "no progress" not in text


def test_multipass_budget_floor_stop(tmp_path):
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"ok": True, "count": 9}))
    w = make_worker(tmp_path, command=drain_command(q), max_passes=9,
                    budget_secs=5, min_pass_secs=9999)
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count("DONE") == 1
    assert "budget floor" in text


def test_single_pass_ledger_unchanged(tmp_path):
    w = make_worker(tmp_path)  # max_passes defaults to 1
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "single-pass complete" in ledger_text(tmp_path)


def test_max_passes_zero_drains_until_queue_empty(tmp_path):
    """wf-174: max_passes=0 is budget-driven drain (soft ceiling off)."""
    q = tmp_path / "queue.json"
    w = make_worker(
        tmp_path, command=drain_command(q), max_passes=0, min_pass_secs=0,
        budget_secs=30,
    )
    q.write_text(json.dumps({"ok": True, "count": 3}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count("DONE") == 3
    assert "queue empty" in text
    assert "max passes" not in text
    assert "drain hard cap" not in text


def test_max_passes_zero_hits_hard_cap(tmp_path, monkeypatch):
    """wf-174: drain mode still stops at MAX_PASSES_HARD (probe never empties)."""
    monkeypatch.setattr(engine, "MAX_PASSES_HARD", 3)
    q = tmp_path / "queue.json"
    # Queue stays at 5 forever — no progress would stop after pass 1 if
    # count didn't drop; shrink by 1 each pass so progress continues.
    w = make_worker(
        tmp_path, command=drain_command(q), max_passes=0, min_pass_secs=0,
        budget_secs=30,
    )
    q.write_text(json.dumps({"ok": True, "count": 99}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count("DONE") == 3
    assert "drain hard cap (3 passes)" in text


def test_effective_pass_ceiling():
    from workforce.roster import Worker

    base = dict(
        name="x", workdir="/tmp", contract="/c", prompt="/p",
        identity="x", command=["true"], queue_url="http://example/q",
    )
    assert engine.effective_pass_ceiling(Worker(max_passes=0, **base)) == engine.MAX_PASSES_HARD
    assert engine.effective_pass_ceiling(Worker(max_passes=1, **base)) == 1
    assert engine.effective_pass_ceiling(Worker(max_passes=4, **base)) == 4


def test_error_pass_is_infra_error(tmp_path):
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "exit 3"],
                    max_passes=4, min_pass_secs=0)
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "agent exit" in ledger_text(tmp_path) and "on_pass=1" in ledger_text(tmp_path)


def test_worker_output_captured_per_shift(tmp_path):
    """A worker that dies in 2s must leave its stderr behind (§8 log tail)."""
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "echo boom-reason >&2; exit 1"])
    assert engine.dispatch(w, local(tmp_path)) == 1
    out = (tmp_path / "local" / "run" / "tester.out").read_text()
    assert "boom-reason" in out and "--- pass 1 ---" in out
    # next shift truncates: a clean run leaves no stale boom
    w2 = make_worker(tmp_path, command=["/bin/sh", "-c", "echo fine; exit 0"])
    assert engine.dispatch(w2, local(tmp_path)) == 0
    out2 = (tmp_path / "local" / "run" / "tester.out").read_text()
    assert "fine" in out2 and "boom-reason" not in out2


def test_prompt_path_token(tmp_path):
    w = make_worker(tmp_path, command=["cli", "--prompt-file", "{prompt_path}"])
    assert engine._build_argv(w, "ignored") == ["cli", "--prompt-file", w.prompt]


def test_predirty_env_name_override(tmp_path):
    import subprocess
    out = tmp_path / "env.txt"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "env > %s" % out],
                    predirty_env="HOST_GUARD_SNAPSHOT")
    subprocess.run(["git", "-C", w.workdir, "init", "-q"], check=True)
    (tmp_path / "hood" / "wip.txt").write_text("citizen WIP\n")
    assert engine.dispatch(w, local(tmp_path)) == 0
    env_text = out.read_text()
    assert "HOST_GUARD_SNAPSHOT=" in env_text
    assert "WORKFORCE_PREDIRTY=" not in env_text


def test_predirty_scrubbed_from_inherited_base(monkeypatch, tmp_path):
    """§7 predirty is per-shift, not inheritable: a parent shift's snapshot
    in the ambient env must never reach the child — deterministically, whether
    or not the suite itself runs inside a live shift."""
    monkeypatch.setenv(engine.PREDIRTY_ENV, "/parent/shift/snapshot.txt")
    w = make_worker(tmp_path, predirty_env="HOST_GUARD_SNAPSHOT")
    # this shift sets its own custom var; the inherited default is gone
    env = engine._build_env(w, "/this/shift/snapshot.txt", None)
    assert engine.PREDIRTY_ENV not in env
    assert env["HOST_GUARD_SNAPSHOT"] == "/this/shift/snapshot.txt"
    # dry-run (predirty=None) must still not leak the parent's snapshot
    assert engine.PREDIRTY_ENV not in engine._build_env(w, None, None)

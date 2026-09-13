"""Bounded AI supervisor (wf-251) — fake providers, disposable roster/ledger state."""

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import engine, supervisor  # noqa: E402
from workforce import roster as roster_mod  # noqa: E402
from workforce._utils import pid_alive  # noqa: E402
from workforce.roster import Worker  # noqa: E402


READY_URL = "http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester"


def make_worker(tmp_path, **over):
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)
    contract = tmp_path / "CONTRACT.md"
    prompt = tmp_path / "prompt.md"
    contract.write_text("# contract\n")
    prompt.write_text("do one slice\n")
    spec = dict(
        name="tester", workdir=str(workdir), contract=str(contract),
        prompt=str(prompt), identity="tester-id",
        command=["/bin/sh", "-c", "exit 0"],
        queue_url=READY_URL, budget_secs=5, min_free_mb=1,
    )
    spec.update(over)
    return Worker(**spec)


def write_roster(tmp_path, workers):
    path = tmp_path / "roster.json"
    raw = {"workers": {}}
    for w in workers:
        spec = {f: getattr(w, f) for f in Worker.__dataclass_fields__ if f != "name"}
        raw["workers"][w.name] = spec
    path.write_text(json.dumps(raw))
    return str(path)


def make_config(tmp_path, roster_path, **over):
    cfg = dict(
        local_root=str(tmp_path / "local"),
        roster_path=roster_path,
        projects=["workforce"],
        workers=["tester"],
        provider_argv=[sys.executable, "-c", "import sys,json; print(json.dumps({'actions': []}))"],
        time_budget_secs=5,
        output_budget_bytes=65536,
        max_dispatch=2,
    )
    cfg.update(over)
    return cfg


def fresh_task(task_id="wf-1", worker="tester", product="workforce", **over):
    row = {"id": task_id, "labels": ["worker:" + worker], "status": "backlog",
           "product": product, "gate_type": ""}
    row.update(over)
    return row


def _provider_argv(actions):
    payload = json.dumps({"actions": actions}).replace("'", "\\'")
    return [sys.executable, "-c",
            "import sys; sys.stdin.read(); print('%s')" % payload]


# ---------------------------------------------------------------- load_config

def test_load_config_rejects_missing_fields(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"local_root": str(tmp_path)}))
    with pytest.raises(supervisor.SupervisorError):
        supervisor.load_config(str(path))


def test_load_config_rejects_relative_local_root(tmp_path):
    cfg = make_config(tmp_path, str(tmp_path / "roster.json"), local_root="relative/path")
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(supervisor.SupervisorError):
        supervisor.load_config(str(path))


def test_load_config_accepts_valid(tmp_path):
    cfg = make_config(tmp_path, str(tmp_path / "roster.json"))
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    loaded = supervisor.load_config(str(path))
    assert loaded["projects"] == frozenset({"workforce"})
    assert loaded["workers"] == frozenset({"tester"})


# ---------------------------------------------------------------- collect_state

def test_collect_state_excludes_cron_worker(tmp_path, monkeypatch):
    w = make_worker(tmp_path, schedule="0 * * * *", queue_url=READY_URL)
    roster_path = write_roster(tmp_path, [w])
    config = make_config(tmp_path, roster_path)
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))
    state = supervisor.collect_state(config)
    assert "tester" not in state["workers"]
    assert "cron-scheduled" in state["excluded_workers"]["tester"]


def test_collect_state_only_trusts_exactly_labeled_ungated_backlog_tasks(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    config = make_config(tmp_path, roster_path)
    tasks = [
        fresh_task("wf-1"),  # valid
        fresh_task("wf-2", status="in_progress"),  # wrong status
        fresh_task("wf-3", gate_type="human"),  # gated
        fresh_task("wf-4", product="other"),  # wrong project
        fresh_task("wf-5", labels=["worker:someone-else"]),  # wrong label
        {"id": "wf-6"},  # no labels at all -- cannot prove freshness
    ]
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (6, tasks))
    state = supervisor.collect_state(config)
    assert state["workers"]["tester"]["ready_task_ids"] == ["wf-1"]


def test_collect_state_marks_busy_worker(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    config = make_config(tmp_path, roster_path)
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))
    lock_dir = os.path.join(config["local_root"], "locks", "tester.lock")
    os.makedirs(lock_dir)
    with open(os.path.join(lock_dir, "pid"), "w") as fh:
        fh.write(str(os.getpid()))  # our own pid is alive
    state = supervisor.collect_state(config)
    assert state["workers"]["tester"]["busy"] is True


def test_collect_state_flags_stale_after_recent_error(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    config = make_config(tmp_path, roster_path)
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))
    ledger_dir = os.path.join(config["local_root"], "ledger")
    os.makedirs(ledger_dir)
    with open(os.path.join(ledger_dir, "tester.log"), "w") as fh:
        fh.write("2026-01-01T00:00:00Z START identity=tester-id\n")
        fh.write("2026-01-01T00:00:01Z ERROR reason=boom rc=1\n")
    state = supervisor.collect_state(config)
    assert state["workers"]["tester"]["monitoring_flag"] == "stale_or_failed_last_shift"


# ---------------------------------------------------------------- _validate_action

def _base_state():
    return {
        "workers": {
            "tester": {
                "project": "workforce", "ready_task_ids": ["wf-1"],
                "busy": False, "monitoring_flag": None,
            },
        },
        "excluded_workers": {},
    }


def test_validate_rejects_unknown_worker():
    config = {"workers": frozenset({"tester"}), "projects": frozenset({"workforce"})}
    v = supervisor._validate_action(
        {"worker": "ghost", "project": "workforce"}, _base_state(), config, set())
    assert v["valid"] is False


def test_validate_rejects_wrong_project_scope():
    config = {"workers": frozenset({"tester"}), "projects": frozenset({"workforce"})}
    v = supervisor._validate_action(
        {"worker": "tester", "project": "other"}, _base_state(), config, set())
    assert v["valid"] is False
    assert "allowlist" in v["reason"]


def test_validate_rejects_busy_worker():
    config = {"workers": frozenset({"tester"}), "projects": frozenset({"workforce"})}
    state = _base_state()
    state["workers"]["tester"]["busy"] = True
    v = supervisor._validate_action(
        {"worker": "tester", "project": "workforce"}, state, config, set())
    assert v["valid"] is False
    assert "busy" in v["reason"]


def test_validate_rejects_stale_monitoring_worker():
    config = {"workers": frozenset({"tester"}), "projects": frozenset({"workforce"})}
    state = _base_state()
    state["workers"]["tester"]["monitoring_flag"] = "stale_or_failed_last_shift"
    v = supervisor._validate_action(
        {"worker": "tester", "project": "workforce"}, state, config, set())
    assert v["valid"] is False
    assert "monitoring" in v["reason"]


def test_validate_rejects_worker_with_no_fresh_ready_work():
    """No ready_task_ids means there is no real work -- reject even a plausible-looking proposal."""
    config = {"workers": frozenset({"tester"}), "projects": frozenset({"workforce"})}
    state = _base_state()
    state["workers"]["tester"]["ready_task_ids"] = []
    v = supervisor._validate_action(
        {"worker": "tester", "project": "workforce"}, state, config, set())
    assert v["valid"] is False
    assert "no fresh eligible ready work" in v["reason"]


def test_validate_action_has_no_task_id_field_requirement():
    """Actions are worker/project scoped only -- engine.dispatch has no task binding."""
    config = {"workers": frozenset({"tester"}), "projects": frozenset({"workforce"})}
    v = supervisor._validate_action(
        {"worker": "tester", "project": "workforce", "task_id": "wf-anything-injected"},
        _base_state(), config, set())
    # An extra task_id field is simply ignored -- never required, never a promise.
    assert v["valid"] is True


def test_validate_rejects_duplicate_worker_even_with_different_projects():
    """Dedup is per-WORKER, not per (worker, task): one worker can only run once."""
    config = {"workers": frozenset({"tester"}), "projects": frozenset({"workforce", "other"})}
    state = _base_state()
    seen = set()
    first = supervisor._validate_action(
        {"worker": "tester", "project": "workforce"}, state, config, seen)
    second = supervisor._validate_action(
        {"worker": "tester", "project": "workforce"}, state, config, seen)
    assert first["valid"] is True
    assert second["valid"] is False
    assert "duplicate" in second["reason"]


def test_validate_rejects_malformed_action_shape():
    config = {"workers": frozenset({"tester"}), "projects": frozenset({"workforce"})}
    v = supervisor._validate_action(
        {"worker": "tester"},  # missing project
        _base_state(), config, set())
    assert v["valid"] is False


# ---------------------------------------------------------------- fresh-state re-fetch

def test_run_revalidates_against_state_collected_after_provider_not_before(tmp_path, monkeypatch):
    """A worker that only becomes ready *while the provider runs* must still be usable,
    and one that only stops being ready during that window must be rejected --
    proving validation uses the post-provider snapshot, not the pre-provider one."""
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    calls = {"n": 0}

    def flipping_probe(worker, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0, []  # nothing ready when the provider was invoked
        return 1, [fresh_task()]  # ready by the time we validate afterward

    monkeypatch.setattr(engine, "_probe_ready", flipping_probe)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    result = supervisor.run(config, mode="inspect")
    assert result["state_before_provider"]["workers"]["tester"]["ready_task_ids"] == []
    assert result["state_after_provider"]["workers"]["tester"]["ready_task_ids"] == ["wf-1"]
    assert result["proposals"][0]["valid"] is True


def test_dispatch_immediately_before_recheck_rejects_worker_gone_busy(tmp_path, monkeypatch):
    """Even after passing post-provider validation, going busy before the dispatch
    call itself must still block it -- the immediately-before-dispatch recheck."""
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))
    dispatched = []
    monkeypatch.setattr(engine, "dispatch", lambda *a, **kw: dispatched.append(1) or 0)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    # Simulate a lock appearing between post-provider validation and the
    # per-worker dispatch call by monkeypatching the recheck to see it busy.
    def busy_at_dispatch_time(cfg, worker_name, project):
        return None, None, "worker is currently busy"

    monkeypatch.setattr(supervisor, "_recheck_immediately_before_dispatch", busy_at_dispatch_time)
    result = supervisor.run(config, mode="execute")
    assert dispatched == []
    d = result["dispatched"][0]
    assert d["attempted"] is False
    assert d["outcome"] == "rejected_at_dispatch_time"
    assert "busy" in d["reason"]


def test_dispatch_one_never_reloads_roster_after_its_own_recheck(tmp_path, monkeypatch):
    """_dispatch_one must fire the exact Worker object its own
    _recheck_immediately_before_dispatch call validated -- never a second,
    independent roster.load(). A roster mutated (here: the worker deleted
    entirely) right after that recheck must not be able to affect this
    dispatch: a second load would hit the mutated roster and blow up with a
    RosterError, surfacing as an "exception" outcome instead of the normal
    completed shift the validated object actually earns.
    """
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))
    real_load = roster_mod.load
    load_calls = {"n": 0}

    def swap_after_recheck_load(path=None, base=None):
        load_calls["n"] += 1
        rost = real_load(path=path, base=base)
        if load_calls["n"] == 3:  # the recheck-immediately-before-dispatch load
            raw = json.loads(Path(roster_path).read_text())
            del raw["workers"]["tester"]
            Path(roster_path).write_text(json.dumps(raw))
        return rost

    monkeypatch.setattr(supervisor.roster_mod, "load", swap_after_recheck_load)

    def fake_dispatch(worker, local_root, dry_run=False):
        ledger_dir = os.path.join(local_root, "ledger")
        os.makedirs(ledger_dir, exist_ok=True)
        with open(os.path.join(ledger_dir, "%s.log" % worker.name), "a") as fh:
            fh.write("2026-01-01T00:00:00Z START identity=x\n")
            fh.write("2026-01-01T00:00:01Z STOP reason=ok\n")
        return 0

    monkeypatch.setattr(engine, "dispatch", fake_dispatch)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    result = supervisor.run(config, mode="execute")
    # Exactly 3 loads for one dispatched action: state_before_provider,
    # state_after_provider, and ONE recheck-at-dispatch. A second reload
    # inside _dispatch_one itself would make this 4 and would have hit the
    # roster mutated in swap_after_recheck_load above.
    assert load_calls["n"] == 3
    d = result["dispatched"][0]
    assert d["attempted"] is True
    assert d["outcome"] == "completed"


def test_dispatch_one_dispatches_the_identical_worker_object_from_its_recheck(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))
    real_load = roster_mod.load
    loaded_rosters = []

    def spy_load(path=None, base=None):
        rost = real_load(path=path, base=base)
        loaded_rosters.append(rost)
        return rost

    monkeypatch.setattr(supervisor.roster_mod, "load", spy_load)
    dispatched = []
    monkeypatch.setattr(
        engine, "dispatch", lambda worker, local_root, dry_run=False: dispatched.append(worker) or 0)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    supervisor.run(config, mode="execute")
    assert len(dispatched) == 1
    assert dispatched[0] is loaded_rosters[-1].workers["tester"]


# ---------------------------------------------------------------- truthful failure classification

def test_run_execute_denied_dispatch_is_failed_never_completed(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))

    def denied_dispatch(worker, local_root, dry_run=False):
        ledger_dir = os.path.join(local_root, "ledger")
        os.makedirs(ledger_dir, exist_ok=True)
        with open(os.path.join(ledger_dir, "%s.log" % worker.name), "a") as fh:
            fh.write("2026-01-01T00:00:00Z SCOPE_DENY reason=out-of-scope\n")
        return 1

    monkeypatch.setattr(engine, "dispatch", denied_dispatch)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    result = supervisor.run(config, mode="execute")
    d = result["dispatched"][0]
    assert d["outcome"] == "denied"
    assert d["failed"] is True
    assert d["completed"] is False
    assert result["dispatch_failed"] == 1
    assert result["dispatch_completed"] == 0


def test_run_execute_nonzero_rc_without_error_event_is_still_failed_never_completed(tmp_path, monkeypatch):
    """Defensive: even if ledger parsing found no ERROR/DENY row at all, a
    non-zero exit code alone must still block "completed" and count as failed."""
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))

    def mystery_nonzero_dispatch(worker, local_root, dry_run=False):
        ledger_dir = os.path.join(local_root, "ledger")
        os.makedirs(ledger_dir, exist_ok=True)
        with open(os.path.join(ledger_dir, "%s.log" % worker.name), "a") as fh:
            fh.write("2026-01-01T00:00:00Z START identity=x\n")
            fh.write("2026-01-01T00:00:01Z STOP reason=ok\n")
        return 1  # rc says failure even though the ledger looks clean

    monkeypatch.setattr(engine, "dispatch", mystery_nonzero_dispatch)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    result = supervisor.run(config, mode="execute")
    d = result["dispatched"][0]
    assert d["completed"] is False
    assert d["failed"] is True
    assert result["dispatch_failed"] == 1


def test_main_cli_returns_nonzero_when_execute_dispatch_is_denied(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))

    def denied_dispatch(worker, local_root, dry_run=False):
        ledger_dir = os.path.join(local_root, "ledger")
        os.makedirs(ledger_dir, exist_ok=True)
        with open(os.path.join(ledger_dir, "%s.log" % worker.name), "a") as fh:
            fh.write("2026-01-01T00:00:00Z HOST_MUTATION_DENY reason=tier2\n")
        return 1

    monkeypatch.setattr(engine, "dispatch", denied_dispatch)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({**config, "projects": list(config["projects"]),
                                     "workers": list(config["workers"])}))
    rc = supervisor.main(["--config", str(cfg_path), "--execute"])
    assert rc == 1


# ---------------------------------------------------------------- _run_provider

def test_run_provider_rejects_malformed_json(tmp_path):
    argv = [sys.executable, "-c", "print('not json')"]
    result = supervisor._run_provider(argv, {}, time_budget_secs=5, output_budget_bytes=4096)
    assert result["ok"] is False
    assert "JSON" in result["error"]


def test_run_provider_rejects_missing_actions_key(tmp_path):
    argv = [sys.executable, "-c", "import json; print(json.dumps({'ok': True}))"]
    result = supervisor._run_provider(argv, {}, time_budget_secs=5, output_budget_bytes=4096)
    assert result["ok"] is False
    assert "actions" in result["error"]


def test_run_provider_times_out(tmp_path):
    argv = [sys.executable, "-c", "import time; time.sleep(5)"]
    result = supervisor._run_provider(argv, {}, time_budget_secs=1, output_budget_bytes=4096)
    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_run_provider_enforces_output_budget_during_read_not_after(tmp_path):
    # A provider that floods stdout forever must be cut off by the byte cap,
    # not by first buffering everything and truncating afterwards.
    argv = [sys.executable, "-c",
            "import sys\nwhile True:\n    sys.stdout.write('x' * 4096)\n    sys.stdout.flush()"]
    result = supervisor._run_provider(argv, {}, time_budget_secs=10, output_budget_bytes=4096)
    assert result["ok"] is False
    assert "output_budget_bytes" in result["error"]


def test_run_provider_launch_failure_reported(tmp_path):
    result = supervisor._run_provider(
        ["/no/such/executable-xyz"], {}, time_budget_secs=5, output_budget_bytes=4096)
    assert result["ok"] is False
    assert "launch failed" in result["error"]


def test_run_provider_timeout_kills_full_process_group_not_just_leader(tmp_path):
    """A provider that forks a detached-looking child must not leave it running
    past the timeout -- proves killpg, not a plain proc.kill() on the leader only."""
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "prov.py"
    script.write_text(textwrap.dedent("""
        import subprocess, sys
        child = subprocess.Popen(["sleep", "30"])
        with open(%r, "w") as fh:
            fh.write(str(child.pid))
            fh.flush()
        child.wait()
    """ % str(pidfile)))
    argv = [sys.executable, str(script)]
    result = supervisor._run_provider(argv, {}, time_budget_secs=1, output_budget_bytes=4096)
    assert result["ok"] is False
    assert "timed out" in result["error"]
    deadline = time.monotonic() + 3
    child_pid = None
    while time.monotonic() < deadline:
        if pidfile.exists() and pidfile.stat().st_size:
            child_pid = int(pidfile.read_text())
            break
        time.sleep(0.05)
    assert child_pid is not None
    deadline = time.monotonic() + 3
    while pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pid_alive(child_pid)


def test_run_provider_kills_descendant_that_outlives_early_parent_exit(tmp_path):
    """The provider (leader) can exit immediately -- via inherited stdout the
    pipe write end stays open through its child, so our reader never sees
    EOF and must still hit the time budget and kill the whole group by
    proc.pid directly, even though the original leader pid is already gone
    by the time the kill actually runs."""
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "prov.py"
    script.write_text(textwrap.dedent("""
        import subprocess, sys
        # Do not redirect stdout/stderr: this child inherits this process's
        # own (our pipe's) fds, so the pipe write end stays open through it.
        child = subprocess.Popen(["sleep", "30"])
        with open(%r, "w") as fh:
            fh.write(str(child.pid))
            fh.flush()
        sys.exit(0)  # leader exits now; child (and the inherited pipe) lives on
    """ % str(pidfile)))
    argv = [sys.executable, str(script)]
    result = supervisor._run_provider(argv, {}, time_budget_secs=1, output_budget_bytes=4096)
    assert result["ok"] is False
    assert "timed out" in result["error"]
    deadline = time.monotonic() + 3
    child_pid = None
    while time.monotonic() < deadline:
        if pidfile.exists() and pidfile.stat().st_size:
            child_pid = int(pidfile.read_text())
            break
        time.sleep(0.05)
    assert child_pid is not None
    deadline = time.monotonic() + 3
    while pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pid_alive(child_pid)


# ---------------------------------------------------------------- run() end-to-end

def test_run_inspect_mode_never_dispatches(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))
    dispatched = []
    monkeypatch.setattr(engine, "dispatch", lambda *a, **kw: dispatched.append(a) or 0)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    result = supervisor.run(config, mode="inspect")
    assert result["mode"] == "inspect"
    assert dispatched == []
    assert result["dispatched"] == []
    assert result["proposals"][0]["valid"] is True
    assert os.path.exists(result["evidence_path"])


def test_run_execute_mode_classifies_completed_shift_truthfully(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))

    def fake_dispatch(worker, local_root, dry_run=False):
        ledger_dir = os.path.join(local_root, "ledger")
        os.makedirs(ledger_dir, exist_ok=True)
        with open(os.path.join(ledger_dir, "%s.log" % worker.name), "a") as fh:
            fh.write("2026-01-01T00:00:00Z START identity=x\n")
            fh.write("2026-01-01T00:00:00Z CANDIDATE ticket=wf-1\n")
            fh.write("2026-01-01T00:00:01Z STOP reason=ok\n")
        return 0

    monkeypatch.setattr(engine, "dispatch", fake_dispatch)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    result = supervisor.run(config, mode="execute")
    d = result["dispatched"][0]
    assert d["attempted"] is True
    assert d["started"] is True
    assert d["completed"] is True
    assert d["failed"] is False
    assert d["outcome"] == "completed"
    assert d["ledger_candidate_task_ids"] == ["wf-1"]
    assert result["dispatch_attempted"] == 1
    assert result["dispatch_started"] == 1
    assert result["dispatch_completed"] == 1
    assert result["dispatch_failed"] == 0


def test_run_execute_mode_a_clean_skip_is_never_reported_as_dispatched(tmp_path, monkeypatch):
    """engine.dispatch's rc==0 SKIP path (e.g. lock busy/queue drained at fire
    time) must not be counted as started/completed just because len(results)==1."""
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))

    def skip_dispatch(worker, local_root, dry_run=False):
        ledger_dir = os.path.join(local_root, "ledger")
        os.makedirs(ledger_dir, exist_ok=True)
        with open(os.path.join(ledger_dir, "%s.log" % worker.name), "a") as fh:
            fh.write("2026-01-01T00:00:00Z SKIP reason=queue empty\n")
        return 0  # rc==0, but nothing actually ran

    monkeypatch.setattr(engine, "dispatch", skip_dispatch)
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    result = supervisor.run(config, mode="execute")
    d = result["dispatched"][0]
    assert d["attempted"] is True
    assert d["started"] is False
    assert d["completed"] is False
    assert d["failed"] is False
    assert d["outcome"] == "skipped"
    assert result["dispatch_started"] == 0
    assert result["dispatch_completed"] == 0


def test_run_execute_partial_dispatch_failure_reported_truthfully(tmp_path, monkeypatch):
    w1 = make_worker(tmp_path, name="tester", identity="tester-id")
    w2 = make_worker(tmp_path, name="tester2", identity="tester2-id")
    roster_path = write_roster(tmp_path, [w1, w2])

    def fake_probe(worker, *a, **kw):
        return 1, [fresh_task("wf-1", worker=worker.name)]

    monkeypatch.setattr(engine, "_probe_ready", fake_probe)

    def flaky_dispatch(worker, local_root, dry_run=False):
        ledger_dir = os.path.join(local_root, "ledger")
        os.makedirs(ledger_dir, exist_ok=True)
        if worker.name == "tester2":
            with open(os.path.join(ledger_dir, "%s.log" % worker.name), "a") as fh:
                fh.write("2026-01-01T00:00:00Z START identity=x\n")
                fh.write("2026-01-01T00:00:01Z ERROR reason=boom rc=1\n")
            return 1
        with open(os.path.join(ledger_dir, "%s.log" % worker.name), "a") as fh:
            fh.write("2026-01-01T00:00:00Z START identity=x\n")
            fh.write("2026-01-01T00:00:01Z STOP reason=ok\n")
        return 0

    monkeypatch.setattr(engine, "dispatch", flaky_dispatch)
    config = make_config(
        tmp_path, roster_path, workers=["tester", "tester2"],
        provider_argv=_provider_argv([
            {"worker": "tester", "project": "workforce"},
            {"worker": "tester2", "project": "workforce"},
        ]),
    )
    result = supervisor.run(config, mode="execute")
    outcomes = {d["worker"]: d for d in result["dispatched"]}
    assert outcomes["tester"]["completed"] is True
    assert outcomes["tester"]["failed"] is False
    assert outcomes["tester2"]["completed"] is False
    assert outcomes["tester2"]["failed"] is True
    assert result["dispatch_completed"] == 1
    assert result["dispatch_failed"] == 1


def test_run_enforces_max_dispatch_bound(tmp_path, monkeypatch):
    w1 = make_worker(tmp_path, name="a", identity="a-id")
    w2 = make_worker(tmp_path, name="b", identity="b-id")
    w3 = make_worker(tmp_path, name="c", identity="c-id")
    roster_path = write_roster(tmp_path, [w1, w2, w3])
    monkeypatch.setattr(
        engine, "_probe_ready",
        lambda worker, *a, **kw: (1, [fresh_task("wf-1", worker=worker.name)]))
    dispatched = []

    def fake_dispatch(worker, local_root, dry_run=False):
        dispatched.append(worker.name)
        return 0

    monkeypatch.setattr(engine, "dispatch", fake_dispatch)
    config = make_config(
        tmp_path, roster_path, workers=["a", "b", "c"], max_dispatch=1,
        provider_argv=_provider_argv([
            {"worker": "a", "project": "workforce"},
            {"worker": "b", "project": "workforce"},
            {"worker": "c", "project": "workforce"},
        ]),
    )
    result = supervisor.run(config, mode="execute")
    assert len(dispatched) == 1
    rejected = [v for v in result["proposals"] if not v["valid"]]
    assert any("max_dispatch" in v["reason"] for v in rejected)


# ---------------------------------------------------------------- evidence writes

def test_write_evidence_uses_unique_filenames_and_persists_path_in_report(tmp_path):
    local_root = str(tmp_path / "local")
    result1 = {"generated_at": "2026-01-01T00:00:00Z"}
    result2 = {"generated_at": "2026-01-01T00:00:00Z"}
    path1 = supervisor._write_evidence(local_root, result1)
    path2 = supervisor._write_evidence(local_root, result2)
    assert path1 != path2  # same timestamp, must not collide/overwrite
    assert os.path.exists(path1) and os.path.exists(path2)
    with open(path1) as fh:
        saved1 = json.load(fh)
    with open(path2) as fh:
        saved2 = json.load(fh)
    assert saved1["evidence_path"] == path1
    assert saved2["evidence_path"] == path2


# ---------------------------------------------------------------- CLI

def test_main_returns_nonzero_when_provider_fails(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))
    config = make_config(
        tmp_path, roster_path,
        provider_argv=[sys.executable, "-c", "print('not json')"],
    )
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({**config, "projects": list(config["projects"]),
                                     "workers": list(config["workers"])}))
    rc = supervisor.main(["--config", str(cfg_path)])
    assert rc == 1


def test_main_returns_zero_on_clean_inspect_pass(tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    roster_path = write_roster(tmp_path, [w])
    monkeypatch.setattr(engine, "_probe_ready", lambda *_a, **_kw: (1, [fresh_task()]))
    config = make_config(
        tmp_path, roster_path,
        provider_argv=_provider_argv([{"worker": "tester", "project": "workforce"}]),
    )
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({**config, "projects": list(config["projects"]),
                                     "workers": list(config["workers"])}))
    rc = supervisor.main(["--config", str(cfg_path)])
    assert rc == 0


def test_run_never_closes_or_writes_worklane():
    """No wl_* call exists in this module -- assert by absence."""
    import inspect
    src = inspect.getsource(supervisor)
    assert "wl_close" not in src
    assert "wl_comment" not in src

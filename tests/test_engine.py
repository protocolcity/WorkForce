"""Engine mechanics — every RUNNER_SPEC MUST, verified without burning tokens."""

import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import engine  # noqa: E402
from workforce.roster import Roster, RosterError, Worker, load  # noqa: E402


def make_worker(tmp_path, **over):
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)
    contract = tmp_path / "CONTRACT.md"
    prompt = tmp_path / "prompt.md"
    contract.write_text("# contract v1\n")
    prompt.write_text("do one slice\n")
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"ok": True, "count": 3}))
    spec = dict(
        name="tester", workdir=str(workdir), contract=str(contract),
        prompt=str(prompt), identity="tester-id",
        command=["/bin/sh", "-c", "exit 0"],
        queue_url="file://" + str(queue), budget_secs=5, min_free_mb=1,
    )
    spec.update(over)
    return Worker(**spec)


def ledger_text(tmp_path):
    p = tmp_path / "local" / "ledger" / "tester.log"
    return p.read_text() if p.exists() else ""


def local(tmp_path):
    return str(tmp_path / "local")


def test_dispatch_success_records_start_done_with_law_hashes(tmp_path):
    w = make_worker(tmp_path)
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "START" in text and "DONE" in text and "STOP" in text
    assert "contract_sha=" in text and "prompt_sha=" in text  # §6 law-version pins
    assert "identity=tester-id" in text and "queue=3" in text


def test_law_change_changes_pinned_hash(tmp_path):
    w = make_worker(tmp_path)
    engine.dispatch(w, local(tmp_path))
    first = [l for l in ledger_text(tmp_path).splitlines() if " START " in l][0]
    (tmp_path / "CONTRACT.md").write_text("# contract v2 — tightened\n")
    engine.dispatch(w, local(tmp_path))
    second = [l for l in ledger_text(tmp_path).splitlines() if " START " in l][1]
    sha = lambda line: [t for t in line.split() if t.startswith("contract_sha=")][0]
    assert sha(first) != sha(second)


def test_dry_run_spawns_nothing(tmp_path):
    marker = tmp_path / "ran"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "touch %s" % marker])
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    assert not marker.exists()
    assert "dry_run=1" in ledger_text(tmp_path)


def test_dispatch_records_candidate_from_ready_tasks(tmp_path):
    """wf-250: START is followed by CANDIDATE ticket=… when ready lists tasks."""
    w = make_worker(tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({
        "ok": True,
        "count": 2,
        "product": "workforce",
        "tasks": [
            {"id": "wf-158", "title": "Dispatch candidate truth", "priority": 3},
            {"id": "wf-159", "title": "next", "priority": 2},
        ],
    }))
    # file:// probes have no product= query — CANDIDATE still carries ticket/title
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert " START " in text
    assert " CANDIDATE " in text and "ticket=wf-158" in text
    assert " CLAIM " not in text
    assert "ticket=wf-159" in text
    assert "title=" in text
    from workforce.ledger import open_candidates, open_claims
    assert open_claims(text) == []
    assert [c["ticket"] for c in open_candidates(text)] == []


def test_dispatch_candidate_not_confirmed_claim(tmp_path):
    """wf-250: worker that never wl_claims keeps candidate context, not ownership."""
    w = make_worker(tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({
        "ok": True,
        "count": 1,
        "tasks": [{"id": "wf-248", "title": "MCP blocked before claim"}],
    }))
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    text = ledger_text(tmp_path)
    from workforce.ledger import open_candidates, open_claims
    assert open_claims(text) == []
    cands = open_candidates(text)
    assert len(cands) == 0  # dry-run DONE closes the candidate window
    assert " CANDIDATE " in text and "ticket=wf-248" in text
    assert " CLAIM " not in text


def test_dispatch_candidate_absent_when_probe_is_count_only(tmp_path):
    """Count-only ready probes stay valid; CANDIDATE is best-effort."""
    w = make_worker(tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({"ok": True, "count": 1}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert " CANDIDATE " not in ledger_text(tmp_path)


def test_dry_run_records_candidate_then_clears(tmp_path):
    """Dry-run still writes CANDIDATE so verification can see handed work orders."""
    w = make_worker(tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({
        "ok": True, "count": 1,
        "tasks": [{"id": "wf-1", "title": "t"}],
    }))
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    text = ledger_text(tmp_path)
    assert " CANDIDATE " in text and "ticket=wf-1" in text
    assert " CLAIM " not in text
    assert "dry_run=1" in text
    from workforce.ledger import open_candidates, open_claims
    assert open_claims(text) == []
    assert open_candidates(text) == []


def test_empty_queue_skips_cleanly(tmp_path):
    """wf-231: empty SKIPs; no mill chew spawn."""
    w = make_worker(tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({"ok": True, "count": 0}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "SKIP" in text and "queue empty" in text
    assert "standing_chew=1" not in text
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "SKIP" in ledger_text(tmp_path) and "queue empty" in ledger_text(tmp_path)


def test_revalidate_skips_when_ready_drains_after_lock(tmp_path, monkeypatch):
    """wf-163: first probe non-empty, post-lock re-probe empty → no spawn."""
    marker = tmp_path / "ran"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "touch %s" % marker])
    (tmp_path / "queue.json").write_text(json.dumps({
        "ok": True, "count": 1,
        "tasks": [{"id": "pc-1111", "title": "claimed mid-flight"}],
    }))
    calls = {"n": 0}

    def _probe_then_drain(_w, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 1, [{"id": "pc-1111", "title": "claimed mid-flight"}]
        return 0, []

    monkeypatch.setattr(engine, "_probe_ready", _probe_then_drain)
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert not marker.exists()
    text = ledger_text(tmp_path)
    assert "SKIP" in text and "queue empty" in text
    assert " START " not in text
    assert calls["n"] >= 2


def test_revalidate_skips_foreign_claim_on_task_refetch(tmp_path, monkeypatch):
    """wf-163: ready list still has id but live task is in_progress Owner:you."""
    marker = tmp_path / "ran"
    w = make_worker(
        tmp_path,
        identity="tom",
        command=["/bin/sh", "-c", "touch %s" % marker],
        queue_url=(
            "http://desk.test/api/admin/tasks/ready"
            "?product=protocolcity&label=worker:tom"
        ),
    )
    monkeypatch.setattr(
        engine, "_probe_ready",
        lambda _w, **_kw: (1, [{"id": "pc-1111", "title": "atlas"}]),
    )
    monkeypatch.setattr(
        engine, "_fetch_task",
        lambda _desk, _product, _tid: {
            "id": "pc-1111",
            "status": "in_progress",
            "comments": [
                {"body": "Owner: you\nStart: mid-flight founder claim"},
            ],
        },
    )
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert not marker.exists()
    text = ledger_text(tmp_path)
    assert "SKIP" in text and "foreign claim" in text
    assert "skip-foreign-claim" in text
    assert "ticket=pc-1111" in text
    assert " START " not in text


def test_revalidate_keeps_backlog_and_dispatches(tmp_path, monkeypatch):
    """wf-163: re-fetch still backlog → CANDIDATE + spawn as before."""
    marker = tmp_path / "ran"
    w = make_worker(
        tmp_path,
        identity="tom",
        command=["/bin/sh", "-c", "touch %s" % marker],
        queue_url=(
            "http://desk.test/api/admin/tasks/ready"
            "?product=protocolcity&label=worker:tom"
        ),
    )
    monkeypatch.setattr(
        engine, "_probe_ready",
        lambda _w, **_kw: (1, [{"id": "pc-1111", "title": "atlas"}]),
    )
    monkeypatch.setattr(
        engine, "_fetch_task",
        lambda _desk, _product, _tid: {
            "id": "pc-1111",
            "status": "backlog",
            "comments": [],
        },
    )
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert marker.exists()
    text = ledger_text(tmp_path)
    assert " START " in text
    assert " CANDIDATE " in text and "ticket=pc-1111" in text
    assert " CLAIM " not in text
    assert "foreign claim" not in text


def test_task_still_claimable_policy():
    """Unit: only backlog is claimable; foreign Owner on in_progress is not."""
    ok, _ = engine._task_still_claimable({"status": "backlog"}, "tom")
    assert ok
    ok, detail = engine._task_still_claimable(
        {
            "status": "in_progress",
            "comments": [{"body": "Owner: you\n"}],
        },
        "tom",
    )
    assert not ok and "owner=you" in detail
    ok, detail = engine._task_still_claimable(
        {
            "status": "in_progress",
            "comments": [{"body": "Owner: tom\n"}],
        },
        "tom",
    )
    assert not ok and "owner=self" in detail


def test_empty_run_warns_once_on_nth_consecutive(tmp_path):
    """ALWAYS_WORK §4 / wf-111: Nth consecutive queue-empty → one WARN."""
    w = make_worker(tmp_path, empty_run_threshold=3)
    (tmp_path / "queue.json").write_text(json.dumps({"ok": True, "count": 0}))
    for _ in range(2):
        assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count(" SKIP ") == 2
    assert "empty-run threshold" not in text
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert text.count(" SKIP ") == 3
    assert text.count(" WARN ") == 1
    assert "empty-run threshold (3 consecutive queue empty)" in text
    # 4th empty does not re-WARN (streak past N until a real shift resets)
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert ledger_text(tmp_path).count(" WARN ") == 1


def test_empty_run_streak_resets_after_real_shift(tmp_path):
    """A successful dispatch breaks the empty streak so WARN can fire again."""
    w = make_worker(tmp_path, empty_run_threshold=2)
    q = tmp_path / "queue.json"
    empty_warn = "empty-run threshold"
    q.write_text(json.dumps({"ok": True, "count": 0}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert ledger_text(tmp_path).count(empty_warn) == 1
    q.write_text(json.dumps({"ok": True, "count": 1}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "DONE" in ledger_text(tmp_path)
    # Soft ceiling with ready still open may add early-idle WARN;
    # empty-run streak counts only empty-run threshold messages.
    q.write_text(json.dumps({"ok": True, "count": 0}))
    assert engine.dispatch(w, local(tmp_path)) == 0  # skip 1 after real work
    assert ledger_text(tmp_path).count(empty_warn) == 1  # not yet at threshold again
    assert engine.dispatch(w, local(tmp_path)) == 0  # skip 2 → warn 2
    assert ledger_text(tmp_path).count(empty_warn) == 2


def test_empty_run_non_empty_skip_does_not_count(tmp_path):
    """Only queue-empty SKIPs feed the streak — other SKIPs do not."""
    w = make_worker(tmp_path, empty_run_threshold=2,
                    command=["definitely-not-a-real-cli-xyz"])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "CLI" in ledger_text(tmp_path)
    streak, _ = engine.empty_run_streak(local(tmp_path), "tester")
    assert streak == 0


def test_empty_run_threshold_validation(tmp_path):
    with pytest.raises(RosterError, match="empty_run_threshold"):
        make_worker(tmp_path, empty_run_threshold=0).validate()
    with pytest.raises(RosterError, match="empty_run_backoff"):
        make_worker(tmp_path, empty_run_backoff=-1).validate()


def test_vendor_limit_backoff_validation(tmp_path):
    """wf-126: vendor_limit_* field validation rules."""
    with pytest.raises(RosterError, match="vendor_limit_threshold"):
        make_worker(tmp_path, vendor_limit_threshold=-1).validate()
    with pytest.raises(RosterError, match="vendor_limit_backoff"):
        make_worker(tmp_path, vendor_limit_backoff=-1).validate()
    with pytest.raises(RosterError, match="vendor_limit_backoff > 0 requires"):
        make_worker(tmp_path, vendor_limit_threshold=0, vendor_limit_backoff=60).validate()
    # valid combinations
    make_worker(tmp_path, vendor_limit_threshold=0, vendor_limit_backoff=0).validate()
    make_worker(tmp_path, vendor_limit_threshold=3, vendor_limit_backoff=0).validate()
    make_worker(tmp_path, vendor_limit_threshold=3, vendor_limit_backoff=3600).validate()


def test_queue_probe_count_returns_count_on_success(tmp_path):
    """wf-125: queue_probe_count returns the ready count when probe succeeds."""
    w = make_worker(tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({"count": 7}))
    assert engine.queue_probe_count(w) == 7


def test_queue_probe_count_returns_none_when_no_url(tmp_path):
    """wf-125: queue_probe_count returns None (fail open) when queue_url absent."""
    w = make_worker(tmp_path, queue_url="")
    assert engine.queue_probe_count(w) is None


def test_queue_probe_count_returns_none_on_error(tmp_path):
    """wf-125: queue_probe_count swallows probe errors (fail open for daemon use)."""
    w = make_worker(tmp_path, queue_url="file://" + str(tmp_path / "missing.json"))
    assert engine.queue_probe_count(w) is None


def test_unreachable_desk_is_infra_error(tmp_path):
    w = make_worker(tmp_path, queue_url="file://" + str(tmp_path / "missing.json"))
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "ERROR" in ledger_text(tmp_path) and "desk unreachable" in ledger_text(tmp_path)


def test_is_timeout_exc_recognizes_socket_and_url_timeouts():
    """wf-145: timeout classifier covers socket.timeout and URLError wrappers."""
    assert engine._is_timeout_exc(socket.timeout("timed out"))
    assert engine._is_timeout_exc(TimeoutError("timed out"))
    assert engine._is_timeout_exc(
        urllib.error.URLError(socket.timeout("timed out")))
    assert engine._is_timeout_exc(RuntimeError("The read operation timed out"))
    assert not engine._is_timeout_exc(RuntimeError("connection refused"))
    assert not engine._is_timeout_exc(urllib.error.HTTPError(
        "http://x", 400, "Bad Request", {}, None))


def test_probe_queue_retries_once_on_timeout_then_succeeds(tmp_path, monkeypatch):
    """wf-145: a single timeout is retried; second attempt success → no ERROR."""
    w = make_worker(tmp_path)
    calls = {"n": 0}

    def flaky(_url, timeout=10.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("timed out")
        return {"ok": True, "count": 4}

    monkeypatch.setattr(engine, "_http_get_json", flaky)
    monkeypatch.setattr(engine, "_PROBE_RETRY_BACKOFF_SECS", 0)
    assert engine._probe_queue(w) == 4
    assert calls["n"] == 2


def test_probe_queue_two_timeouts_is_infra_error(tmp_path, monkeypatch):
    """wf-145: retry exhausted on timeout → InfraError (desk unreachable)."""
    w = make_worker(tmp_path)
    calls = {"n": 0}

    def always_timeout(_url, timeout=10.0):
        calls["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(engine, "_http_get_json", always_timeout)
    monkeypatch.setattr(engine, "_PROBE_RETRY_BACKOFF_SECS", 0)
    with pytest.raises(engine.InfraError, match="desk unreachable"):
        engine._probe_queue(w)
    assert calls["n"] == 2  # initial + one retry


def test_probe_queue_non_timeout_fails_immediately(tmp_path, monkeypatch):
    """wf-145: HTTP/shape errors do not burn a retry — fail on first miss."""
    w = make_worker(tmp_path)
    calls = {"n": 0}

    def bad_request(_url, timeout=10.0):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            w.queue_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(engine, "_http_get_json", bad_request)
    with pytest.raises(engine.InfraError, match="desk unreachable"):
        engine._probe_queue(w)
    assert calls["n"] == 1


def test_http_get_json_file_scheme(tmp_path):
    """wf-145: file:// probes (tests + offline) still work via urllib path."""
    p = tmp_path / "q.json"
    p.write_text(json.dumps({"count": 11}))
    assert engine._http_get_json(p.as_uri()) == {"count": 11}


def test_http_get_json_closes_socket_on_timeout():
    """wf-145: client must not leave ESTABLISHED sockets after a timeout."""
    import threading

    # Raw accept-and-hold server models a slow desk that never responds.
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(5)
    srv.settimeout(5)
    held = []

    def accept_loop():
        try:
            c, _ = srv.accept()
            held.append(c)
            time.sleep(2)
        except Exception:
            pass

    th = threading.Thread(target=accept_loop, daemon=True)
    th.start()
    est = -1
    try:
        with pytest.raises(Exception):
            engine._http_get_json(
                "http://127.0.0.1:%d/slow" % port, timeout=0.2)
        # Give the finally: close a beat; client ESTABLISHED should be gone.
        time.sleep(0.1)
        pid = os.getpid()
        import shutil
        _lsof = shutil.which("lsof") or (
            "/usr/sbin/lsof" if os.path.isfile("/usr/sbin/lsof") else None
        )
        if _lsof is None:
            pytest.skip("lsof not available on this system")
        try:
            out = subprocess.check_output(
                [_lsof, "-nP", "-a", "-p", str(pid),
                 "-iTCP:%d" % port],
                text=True, stderr=subprocess.DEVNULL)
            est = sum(1 for line in out.splitlines() if "ESTABLISHED" in line)
        except subprocess.CalledProcessError:
            est = 0
    finally:
        for c in held:
            try:
                c.close()
            except Exception:
                pass
        srv.close()
    assert est == 0, "timed-out probe left %d ESTABLISHED socket(s)" % est


def test_open_http_conn_non_http_returns_none(tmp_path):
    """wf-228: file:// (and other non-http) skip http.client setup."""
    p = tmp_path / "q.json"
    p.write_text("{}")
    assert engine._open_http_conn(p.as_uri(), timeout=1.0) is None


def test_open_http_conn_http_returns_conn_and_path():
    """wf-228: http URL yields a live HTTPConnection plus request path."""
    import http.client

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    opened = engine._open_http_conn(
        "http://127.0.0.1:%d/ready?product=workforce" % port, timeout=1.0)
    assert opened is not None
    conn, path = opened
    try:
        assert path == "/ready?product=workforce"
        assert isinstance(conn, http.client.HTTPConnection)
    finally:
        conn.close()


def test_http_json_file_scheme(tmp_path):
    """wf-228: _http_json file:// fallback still parses JSON."""
    p = tmp_path / "q.json"
    p.write_text(json.dumps({"ok": True, "count": 2}))
    assert engine._http_json("GET", p.as_uri()) == {"ok": True, "count": 2}


def _serve_one_http(status: int, body: bytes, reason: str = "ERR"):
    """Accept one request, write a canned HTTP response, return (port, thread)."""
    import threading

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    srv.settimeout(2)

    def _run():
        try:
            c, _ = srv.accept()
            try:
                c.recv(4096)
                payload = (
                    "HTTP/1.1 %d %s\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: %d\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ) % (status, reason, len(body))
                c.sendall(payload.encode("ascii") + body)
            finally:
                c.close()
        except Exception:
            pass
        finally:
            srv.close()

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    return port, th


def test_http_get_json_raises_on_4xx():
    """wf-228: probe helper still raises HTTPError on 4xx (does not swallow)."""
    port, th = _serve_one_http(404, b'{"error":"missing"}', reason="Not Found")
    url = "http://127.0.0.1:%d/missing" % port
    with pytest.raises(urllib.error.HTTPError) as ei:
        engine._http_get_json(url, timeout=2.0)
    assert ei.value.code == 404
    th.join(timeout=2)


def test_http_json_returns_error_dict_on_4xx():
    """wf-228: desk-write helper still returns an error dict on 4xx (does not raise)."""
    port, th = _serve_one_http(409, b'{"error":"conflict"}', reason="Conflict")
    url = "http://127.0.0.1:%d/drop" % port
    out = engine._http_json("POST", url, body={"ticket": "wf-228"}, timeout=2.0)
    assert out.get("ok") is False
    assert out.get("error") == "conflict"
    th.join(timeout=2)


def test_missing_cli_skips(tmp_path):
    w = make_worker(tmp_path, command=["definitely-not-a-real-cli-xyz"])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "not installed" in ledger_text(tmp_path)


def test_lock_held_skips(tmp_path):
    # Lock held by a live process (our own pid) → SKIP.
    w = make_worker(tmp_path)
    lock_dir = tmp_path / "local" / "locks"
    lock_dir.mkdir(parents=True)
    lock = lock_dir / "tester.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "lock held" in ledger_text(tmp_path)


def test_orphan_empty_lock_is_reclaimed(tmp_path):
    # Empty lock dir (no pid file) left by a kill-9 → reclaimed, dispatch succeeds.
    w = make_worker(tmp_path)
    lock_dir = tmp_path / "local" / "locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "tester.lock").mkdir()
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "orphan-no-pid" in ledger_text(tmp_path)
    assert "START" in ledger_text(tmp_path)


def test_orphan_dead_pid_is_reclaimed(tmp_path):
    # Lock dir with a dead pid → reclaimed, dispatch succeeds.
    import subprocess as _sp
    w = make_worker(tmp_path)
    lock_dir = tmp_path / "local" / "locks"
    lock_dir.mkdir(parents=True)
    lock = lock_dir / "tester.lock"
    lock.mkdir()
    proc = _sp.Popen(["/bin/sh", "-c", "exit 0"])
    proc.wait()
    (lock / "pid").write_text(str(proc.pid))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "orphan-pid-dead" in ledger_text(tmp_path)
    assert "START" in ledger_text(tmp_path)


def test_budget_kill_is_infra_error(tmp_path):
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "sleep 30"], budget_secs=1)
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "killed at budget" in ledger_text(tmp_path)


def _fake_provider_argv(receipt_path):
    """A task_runner-shaped provider: prints the receipt line + a terminal
    JSON result, then hangs (wf-266 — cursor-agent observed doing exactly
    this after its own turn actually finished)."""
    script = (
        "import sys, time\n"
        "print('Prepared wf-1; not yet claimed. Receipt: %s'); sys.stdout.flush()\n"
        "print('{\"type\": \"result\", \"done\": true}'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    ) % receipt_path
    return [sys.executable, "-c", script]


def test_provider_lingering_after_parked_result_ends_as_done(tmp_path, monkeypatch):
    """wf-266: a provider that prints its terminal result then keeps running,
    once WorkLane shows its order left in_progress, ends the shift as DONE
    with the linger reason (not a budget-kill ERROR), well inside the grace
    period, and releases the lock."""
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"project": "workforce", "task_id": "wf-1"}))
    w = make_worker(
        tmp_path,
        command=_fake_provider_argv(receipt),
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        budget_secs=20,
        linger_grace_secs=1,
    )
    monkeypatch.setattr(engine, "_LINGER_POLL_SECS", 0.2)
    monkeypatch.setattr(engine, "_http_get_json", lambda *a, **k: {"count": 1})

    def fake_http(method, url, body=None, timeout=8.0):
        assert method == "GET" and "/wf-1" in url
        return {"ok": True, "task": {"id": "wf-1", "status": "in_review"}}

    monkeypatch.setattr(engine, "_http_json", fake_http)

    t0 = time.monotonic()
    assert engine.dispatch(w, local(tmp_path)) == 0
    elapsed = time.monotonic() - t0
    assert elapsed < 10  # well within budget_secs=20; grace is 1s
    text = ledger_text(tmp_path)
    assert "DONE" in text and "provider lingered after result" in text
    assert "killed at budget" not in text
    assert not (tmp_path / "local" / "locks" / "tester.lock").exists()


def test_provider_that_never_prints_a_result_keeps_budget_kill(tmp_path, monkeypatch):
    """wf-266: without a receipt/result to confirm ownership left this
    identity, the existing budget-kill ERROR is unchanged — the desk is
    never even contacted."""
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "sleep 30"],
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        budget_secs=1,
        linger_grace_secs=1,
    )
    monkeypatch.setattr(engine, "_LINGER_POLL_SECS", 0.2)
    monkeypatch.setattr(engine, "_http_get_json", lambda *a, **k: {"count": 1})

    def boom(*a, **k):
        raise AssertionError("desk must not be contacted without a task reference")

    monkeypatch.setattr(engine, "_http_json", boom)
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "killed at budget" in ledger_text(tmp_path)


def test_linger_armed_before_budget_still_ends_as_done(tmp_path, monkeypatch):
    """wf-266 review finding 1: once the linger is armed, a budget that
    expires before the (longer) linger grace elapses must still end the
    pass with the linger reason, not fall through to "killed at budget"."""
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"project": "workforce", "task_id": "wf-1"}))
    w = make_worker(
        tmp_path,
        command=_fake_provider_argv(receipt),
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        budget_secs=1,
        linger_grace_secs=30,
    )
    monkeypatch.setattr(engine, "_LINGER_POLL_SECS", 0.2)
    monkeypatch.setattr(engine, "_http_get_json", lambda *a, **k: {"count": 1})

    def fake_http(method, url, body=None, timeout=8.0):
        return {"ok": True, "task": {"id": "wf-1", "status": "in_review"}}

    monkeypatch.setattr(engine, "_http_json", fake_http)
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "DONE" in text and "provider lingered after result" in text
    assert "killed at budget" not in text


def _fake_intermediate_argv(receipt_path):
    """Prints the receipt then a non-terminal NDJSON diagnostic object
    (no ``type: end/result``), then hangs — never emits a real terminal
    result."""
    script = (
        "import sys, time\n"
        "print('Prepared wf-1; not yet claimed. Receipt: %s'); sys.stdout.flush()\n"
        "print('{\"type\": \"tool_use\", \"name\": \"bash\"}'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    ) % receipt_path
    return [sys.executable, "-c", script]


def test_linger_not_armed_on_intermediate_ndjson_object(tmp_path, monkeypatch):
    """wf-266 review finding 2: the last parsed top-level JSON object alone
    must not arm the linger — only a verified terminal result (type end/
    result, or the worker's own completion_field/completion_values) does.
    An intermediate diagnostic object plus a parked order must not force-end
    the pass early; the existing budget-kill applies unchanged."""
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"project": "workforce", "task_id": "wf-1"}))
    w = make_worker(
        tmp_path,
        command=_fake_intermediate_argv(receipt),
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        budget_secs=1,
        linger_grace_secs=30,
    )
    monkeypatch.setattr(engine, "_LINGER_POLL_SECS", 0.2)
    monkeypatch.setattr(engine, "_http_get_json", lambda *a, **k: {"count": 1})
    monkeypatch.setattr(
        engine, "_http_json",
        lambda *a, **k: {"ok": True, "task": {"id": "wf-1", "status": "in_review"}},
    )
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "killed at budget" in text
    assert "provider lingered after result" not in text


def _fake_cancelled_linger_argv(receipt_path):
    """Prints the receipt then grok's real pretty-printed cancelled-turn
    shape — a bare top-level ``{"stopReason": "cancelled", ...}`` with no
    ``type`` field at all (matches
    ``test_pass_result_json_pretty_printed_cancelled_still_found``) — then
    hangs, the shape a Grok pass produces on an internal vendor stop
    (wf-262)."""
    script = (
        "import sys, time\n"
        "print('Prepared wf-1; not yet claimed. Receipt: %s'); sys.stdout.flush()\n"
        "print('{\\n  \"stopReason\": \"cancelled\",\\n  \"sessionId\": \"sid-1\"\\n}');"
        " sys.stdout.flush()\n"
        "time.sleep(30)\n"
    ) % receipt_path
    return [sys.executable, "-c", script]


def test_linger_exit_classifies_cancelled_as_denied_not_done(tmp_path, monkeypatch):
    """wf-266 second review finding 2: _is_terminal_result must arm the
    linger for grok's real cancelled shape (no "type" field, just a
    completion_field value) — not only a synthetic {"type": "end", ...}
    wrapper — and the linger exit in dispatch must still run
    _classify_completion, so a lingering Grok pass whose own result carries
    stopReason=cancelled records the wf-262 ERROR "denied: cancelled", never
    a false DONE just because the process was confirmed parked."""
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"project": "workforce", "task_id": "wf-1"}))
    w = make_worker(
        tmp_path,
        command=_fake_cancelled_linger_argv(receipt),
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        budget_secs=20,
        linger_grace_secs=1,
        completion_field="stopReason",
        completion_values=["end_turn"],
    )
    monkeypatch.setattr(engine, "_LINGER_POLL_SECS", 0.2)
    monkeypatch.setattr(engine, "_http_get_json", lambda *a, **k: {"count": 1})
    monkeypatch.setattr(
        engine, "_http_json",
        lambda *a, **k: {"ok": True, "task": {"id": "wf-1", "status": "in_review"}},
    )
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "ERROR" in text and "denied: cancelled" in text
    assert "DONE" not in text or "provider lingered after result" not in text


def _fake_still_running_linger_argv(receipt_path):
    """Prints the receipt then a wrapper-terminal result whose configured
    stop value is neither in completion_values nor "cancelled" — e.g. a
    future/unmapped vendor stop reason — then hangs."""
    script = (
        "import sys, time\n"
        "print('Prepared wf-1; not yet claimed. Receipt: %s'); sys.stdout.flush()\n"
        "print('{\"type\": \"end\", \"stopReason\": \"still_running\"}'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    ) % receipt_path
    return [sys.executable, "-c", script]


def test_linger_exit_classifies_other_incomplete_as_error_not_done(tmp_path, monkeypatch):
    """wf-266 second review finding 1: the linger exit must mirror the
    rc==0 path for *any* configured-and-not-complete outcome, not only
    "cancelled" — a wrapper-terminal result whose stop value is some other
    unmapped/incomplete reason must record ERROR "incomplete: <value>",
    never a DONE just because the process was confirmed parked."""
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"project": "workforce", "task_id": "wf-1"}))
    w = make_worker(
        tmp_path,
        command=_fake_still_running_linger_argv(receipt),
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        budget_secs=20,
        linger_grace_secs=1,
        completion_field="stopReason",
        completion_values=["end_turn"],
    )
    monkeypatch.setattr(engine, "_LINGER_POLL_SECS", 0.2)
    monkeypatch.setattr(engine, "_http_get_json", lambda *a, **k: {"count": 1})
    monkeypatch.setattr(
        engine, "_http_json",
        lambda *a, **k: {"ok": True, "task": {"id": "wf-1", "status": "in_review"}},
    )
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "ERROR" in text and "incomplete: still_running" in text
    assert "provider lingered after result" not in text


def _fake_slow_park_argv(receipt_path):
    """Prints the receipt then a terminal result, then hangs; the desk
    keeps reporting in_progress (a slow park that never confirms before
    budget expiry)."""
    script = (
        "import sys, time\n"
        "print('Prepared wf-1; not yet claimed. Receipt: %s'); sys.stdout.flush()\n"
        "print('{\"type\": \"result\", \"done\": true}'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    ) % receipt_path
    return [sys.executable, "-c", script]


def test_budget_expiry_with_terminal_result_pending_park_is_linger_not_kill(
    tmp_path, monkeypatch,
):
    """wf-266 second review finding 3: once a verified terminal result is on
    disk, budget expiry before the desk confirms the order left in_progress
    (a slow park) must still end with the linger reason, never the bare
    "killed at budget" ERROR."""
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"project": "workforce", "task_id": "wf-1"}))
    w = make_worker(
        tmp_path,
        command=_fake_slow_park_argv(receipt),
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        budget_secs=1,
        linger_grace_secs=30,
    )
    monkeypatch.setattr(engine, "_LINGER_POLL_SECS", 0.2)
    monkeypatch.setattr(engine, "_http_get_json", lambda *a, **k: {"count": 1})
    monkeypatch.setattr(
        engine, "_http_json",
        lambda *a, **k: {"ok": True, "task": {"id": "wf-1", "status": "in_progress"}},
    )
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "DONE" in text and "provider lingered after result" in text
    assert "killed at budget" not in text


def test_lock_released_after_shift(tmp_path):
    w = make_worker(tmp_path)
    engine.dispatch(w, local(tmp_path))
    assert not (tmp_path / "local" / "locks" / "tester.lock").exists()


def test_child_env_scrubbed_and_identity_set(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_PRODUCT", "leaky")
    monkeypatch.setenv("TP_DEFAULT_PRODUCT", "leaky")
    monkeypatch.setenv("TP_AGENT_ID", "wrong-identity")
    out = tmp_path / "env.txt"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "env > %s" % out])
    assert engine.dispatch(w, local(tmp_path)) == 0
    env_text = out.read_text()
    assert "TP_AGENT_ID=tester-id" in env_text            # §2: exactly one identity
    assert "TP_PRODUCT=" not in env_text                   # §2: no ambient scope
    assert "TP_DEFAULT_PRODUCT=" not in env_text


def test_predirty_snapshot_written_for_git_workdir(tmp_path):
    w = make_worker(tmp_path)
    subprocess.run(["git", "-C", w.workdir, "init", "-q"], check=True)
    (tmp_path / "hood" / "wip.txt").write_text("citizen WIP\n")
    engine.dispatch(w, local(tmp_path))
    snap = tmp_path / "local" / "run" / "predirty-tester.txt"
    assert snap.exists() and "wip.txt" in snap.read_text()


def test_model_pin_substitution_and_unpinned_drop(tmp_path):
    w = make_worker(tmp_path, model="test-model-1")
    argv = engine._build_argv(w, "P")
    assert argv == ["/bin/sh", "-c", "exit 0"]
    w2 = make_worker(tmp_path, command=["cli", "--model", "{model}", "-p", "{prompt_text}"],
                     model="m1")
    assert engine._build_argv(w2, "brief") == ["cli", "--model", "m1", "-p", "brief"]
    w3 = make_worker(tmp_path, command=["cli", "--model={model}", "-p", "{prompt_text}"], model="")
    assert engine._build_argv(w3, "brief") == ["cli", "-p", "brief"]
    # the paired-flag form must not leave an orphaned --model behind
    # (incident 2026-07-14: claude parsed '-p' as the model name)
    w4 = make_worker(tmp_path, command=["cli", "--model", "{model}", "-p", "{prompt_text}"],
                     model="")
    assert engine._build_argv(w4, "brief") == ["cli", "-p", "brief"]
    w5 = make_worker(tmp_path, command=["cli", "--model", "{model}", "-p", "{prompt_text}"],
                     model="m9")
    assert engine._build_argv(w5, "brief") == ["cli", "--model", "m9", "-p", "brief"]


def test_roster_env_path_governs_cli_preflight(tmp_path):
    """A roster env PATH must satisfy preflight — the daemon runs on a bare
    launchd PATH, so vendor CLI locations are roster data like every seam."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "fake-vendor-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    w = make_worker(tmp_path, command=["fake-vendor-cli"])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "not installed" in ledger_text(tmp_path)  # not on ambient PATH
    w2 = make_worker(tmp_path, command=["fake-vendor-cli"],
                     env={"PATH": "%s:/usr/bin:/bin" % bindir})
    assert engine.dispatch(w2, local(tmp_path)) == 0
    assert "DONE" in ledger_text(tmp_path)           # found via roster PATH


def test_preflight_resolves_relative_and_absolute_script_paths(tmp_path):
    """Relative and absolute script entrypoints must not raise 'not installed'
    when the file exists and is executable — pc-1206 regression."""
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)

    # relative: ./scripts/run-job.sh inside workdir
    scripts = workdir / "scripts"
    scripts.mkdir()
    rel_script = scripts / "run-job.sh"
    rel_script.write_text("#!/bin/sh\nexit 0\n")
    rel_script.chmod(rel_script.stat().st_mode | stat.S_IEXEC)

    # absolute: /bin/sh (always present)
    abs_cmd = "/bin/sh"

    contract = tmp_path / "CONTRACT.md"
    prompt = tmp_path / "prompt.md"
    contract.write_text("# c\n")
    prompt.write_text("p\n")
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"ok": True, "count": 1}))

    for cmd in [["./scripts/run-job.sh", "-c", "exit 0"], [abs_cmd, "-c", "exit 0"]]:
        w = make_worker(
            tmp_path,
            workdir=str(workdir),
            command=cmd,
            queue_url="file://" + str(queue),
        )
        assert engine.dispatch(w, local(tmp_path)) == 0
        assert "not installed" not in ledger_text(tmp_path), (
            "preflight skipped valid script %r" % cmd[0]
        )


def test_missing_workdir_fails_closed_with_ledger_error(tmp_path):
    """wf-269: a stale/misconfigured roster workdir must not raise a bare
    OSError past dispatch()'s own error handling. os.statvfs on a missing
    workdir (the observed bp-supervisor agent-exit shape) has to surface as
    an ERROR row in the worker's own ledger, not an opaque exception that
    only the caller's generic except-clause ever sees."""
    w = make_worker(tmp_path, workdir=str(tmp_path / "does-not-exist"))
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "ERROR" in text
    assert "workdir unreadable" in text
    assert "START" not in text


def test_roster_isolates_duplicate_identity(tmp_path, caplog):
    """Second worker with duplicate identity is skipped; first survives."""
    import logging
    p = tmp_path / "roster.json"
    w = {"workdir": ".", "contract": "c", "prompt": "p", "identity": "same",
         "command": ["x"]}
    p.write_text(json.dumps({"workers": {"a": w, "b": dict(w)}}))
    with caplog.at_level(logging.ERROR, logger="workforce.roster"):
        r = load(str(p))
    assert "a" in r.workers
    assert "b" not in r.workers
    assert any("b" in rec.message and "one worker, one identity" in rec.message
               for rec in caplog.records)


def test_roster_isolates_unknown_field_keeps_sibling(tmp_path, caplog):
    """Worker with unknown field is skipped with ERROR; valid sibling survives."""
    import logging
    good = {"workdir": ".", "contract": "c", "prompt": "p", "identity": "good-id",
            "command": ["x"]}
    bad = {"workdir": ".", "contract": "c", "prompt": "p", "identity": "bad-id",
           "command": ["x"], "sudo": True}
    p = tmp_path / "roster.json"
    p.write_text(json.dumps({"workers": {"good": good, "bad": bad}}))
    with caplog.at_level(logging.ERROR, logger="workforce.roster"):
        r = load(str(p))
    assert "good" in r.workers
    assert "bad" not in r.workers
    assert any("bad" in rec.message and "unknown fields" in rec.message
               for rec in caplog.records)


def test_roster_isolates_bad_kind_keeps_sibling(tmp_path, caplog):
    """Worker with kind=worker is skipped with ERROR; valid sibling survives."""
    import logging
    good = {"workdir": ".", "contract": "c", "prompt": "p", "identity": "good-id",
            "command": ["x"]}
    bad = {"workdir": ".", "contract": "c", "prompt": "p", "identity": "bad-id",
           "command": ["x"], "kind": "worker"}
    p = tmp_path / "roster.json"
    p.write_text(json.dumps({"workers": {"good": good, "bad": bad}}))
    with caplog.at_level(logging.ERROR, logger="workforce.roster"):
        r = load(str(p))
    assert "good" in r.workers
    assert "bad" not in r.workers
    assert any("bad" in rec.message and "lane|job" in rec.message
               for rec in caplog.records)


def test_vendor_limit_exit_classifies_reason(tmp_path):
    """rc != 0 with 402 in output → reason 'vendor limit: …', not bare 'agent exit'."""
    w = make_worker(tmp_path, command=[
        "/bin/sh", "-c",
        "echo '402 Payment Required: usage balance exhausted'; exit 1",
    ])
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "vendor limit:" in text
    assert "agent exit" not in text


def test_vendor_limit_exit_with_429(tmp_path):
    """429 rate-limit exit → vendor_limit classification."""
    w = make_worker(tmp_path, command=[
        "/bin/sh", "-c",
        "echo 'error: 429 too many requests — rate limit exceeded'; exit 1",
    ])
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "vendor limit:" in ledger_text(tmp_path)


def test_codex_usage_limit_classifies_as_vendor_limit(tmp_path):
    """Codex usage-cap stderr → vendor limit, not bare agent exit."""
    w = make_worker(tmp_path, command=[
        "/bin/sh", "-c",
        "echo \"You've hit your usage limit. Try again after Aug 7.\"; exit 1",
    ])
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "vendor limit:" in text
    assert "agent exit" not in text


def test_claude_hit_your_limit_classifies_as_vendor_limit(tmp_path):
    """Claude Code 'hit your limit · resets …' → vendor limit."""
    w = make_worker(tmp_path, command=[
        "/bin/sh", "-c",
        "echo \"You've hit your limit · resets 4:10pm\"; exit 1",
    ])
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "vendor limit:" in ledger_text(tmp_path)


def test_claude_subscription_403_classifies_as_vendor_limit(tmp_path):
    """Claude Code 403 'disabled subscription access' → vendor limit, not agent exit."""
    w = make_worker(tmp_path, command=[
        "/bin/sh", "-c",
        'echo \'{"result":"Your organization has disabled Claude subscription access for Claude Code"}\'; exit 1',
    ])
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "vendor limit:" in text
    assert "agent exit" not in text


def test_json_usage_telemetry_does_not_false_classify(tmp_path):
    """JSON usage token counters alone must not classify as vendor limit."""
    w = make_worker(tmp_path, command=[
        "/bin/sh", "-c",
        'echo \'{"usage":{"input_tokens":12,"output_tokens":3},"result":"boom"}\'; exit 1',
    ])
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "agent exit" in ledger_text(tmp_path)
    assert "vendor limit:" not in ledger_text(tmp_path)


def test_plain_nonzero_exit_stays_agent_exit(tmp_path):
    """A generic non-zero exit with no vendor-limit output stays 'agent exit'."""
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "echo 'unrelated error'; exit 2"])
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "agent exit" in ledger_text(tmp_path)


def test_classify_exit_never_raises_on_missing_file(tmp_path):
    """_classify_exit must not raise when output file is absent."""
    result = engine._classify_exit(str(tmp_path / "nonexistent.out"))
    assert result == "agent exit"


# §6 authority-chain tests

def test_authority_chain_empty_no_required_is_noop(tmp_path):
    """Empty chain + required=False → normal dispatch; chain_len=0 in START."""
    w = make_worker(tmp_path)
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "START" in text and "chain_len=0" in text
    assert "NO_AUTHORITY_CHAIN" not in text


def test_authority_chain_required_empty_chain_errors(tmp_path):
    """authority_chain_required=True + empty chain → ERROR NO_AUTHORITY_CHAIN before START."""
    w = make_worker(tmp_path, authority_chain_required=True)
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "NO_AUTHORITY_CHAIN" in text
    assert "START" not in text


def test_authority_chain_populated_pins_in_start(tmp_path):
    """Populated chain → chain_len and chain_sha appear in START; files read at dispatch."""
    law = tmp_path / "city-law.md"
    law.write_text("# L0 city law\n")
    w = make_worker(tmp_path, authority_chain=[str(law)])
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "chain_len=1" in text
    assert "chain_sha=" in text


def test_authority_chain_multiple_files_all_pinned(tmp_path):
    """Multiple chain files → combined sha, chain_len=N."""
    l0 = tmp_path / "l0.md"
    l1 = tmp_path / "l1.md"
    l0.write_text("# L0\n")
    l1.write_text("# L1\n")
    w = make_worker(tmp_path, authority_chain=[str(l0), str(l1)])
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "chain_len=2" in text
    assert "chain_sha=" in text


def test_authority_chain_missing_file_errors(tmp_path):
    """Chain entry pointing to a missing file → ERROR before START."""
    w = make_worker(tmp_path, authority_chain=[str(tmp_path / "ghost.md")])
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "authority chain file unreadable" in text
    assert "START" not in text


def test_authority_chain_paths_exposed_in_env(tmp_path):
    """WORKFORCE_AUTHORITY_CHAIN_PATHS set in child env when chain is non-empty."""
    law = tmp_path / "law.md"
    law.write_text("# law\n")
    out = tmp_path / "env.txt"
    w = make_worker(tmp_path, authority_chain=[str(law)],
                    command=["/bin/sh", "-c", "env > %s" % out])
    assert engine.dispatch(w, local(tmp_path)) == 0
    env_text = out.read_text()
    assert "WORKFORCE_AUTHORITY_CHAIN_PATHS=" in env_text
    assert str(law) in env_text


def test_authority_chain_text_substituted_in_argv(tmp_path):
    """'{chain_text}' in command template is replaced with concatenated chain content."""
    law = tmp_path / "law.md"
    law.write_text("CHAIN_SENTINEL\n")
    out = tmp_path / "argv.txt"
    w = make_worker(tmp_path, authority_chain=[str(law)],
                    command=["/bin/sh", "-c", "echo '{chain_text}' > %s" % out,
                             "{chain_text}"])
    # build_argv directly
    entries = engine._load_chain([str(law)])
    chain_text = "\n\n".join(t for t, _ in entries)
    argv = engine._build_argv(w, "brief", chain_text=chain_text)
    assert any("CHAIN_SENTINEL" in tok for tok in argv)


def test_authority_chain_dry_run_loads_and_pins(tmp_path):
    """Dry-run still reads chain files and pins chain_sha in START."""
    law = tmp_path / "law.md"
    law.write_text("# law\n")
    w = make_worker(tmp_path, authority_chain=[str(law)])
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    text = ledger_text(tmp_path)
    assert "chain_len=1" in text and "chain_sha=" in text and "dry_run=1" in text


def test_authority_chain_no_paths_in_env_when_empty(tmp_path):
    """When chain is empty, WORKFORCE_AUTHORITY_CHAIN_PATHS is NOT set in child env."""
    out = tmp_path / "env.txt"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "env > %s" % out])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "WORKFORCE_AUTHORITY_CHAIN_PATHS" not in out.read_text()


# §8 ghost audit

def test_ghost_audit_fires_and_logs_ghost_event(tmp_path):
    """Populated ghost_audit runs before START; GHOST event with rc=0 is logged."""
    marker = tmp_path / "ghost_ran"
    w = make_worker(tmp_path, ghost_audit=["/bin/sh", "-c", "echo 'no orphans'; touch %s" % marker])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert marker.exists(), "ghost audit command did not run"
    text = ledger_text(tmp_path)
    assert "GHOST" in text and "rc=0" in text
    ghost_line = next(l for l in text.splitlines() if " GHOST " in l)
    start_line = next(l for l in text.splitlines() if " START " in l)
    assert ghost_line < start_line, "GHOST must precede START in the ledger"


def test_ghost_audit_empty_is_noop(tmp_path):
    """Empty ghost_audit (default) produces no GHOST event."""
    w = make_worker(tmp_path)
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "GHOST" not in ledger_text(tmp_path)


def test_ghost_audit_nonzero_rc_warns_not_aborts(tmp_path):
    """Nonzero ghost_audit rc → GHOST + WARN in ledger; shift still completes."""
    w = make_worker(tmp_path, ghost_audit=["/bin/sh", "-c", "echo 'orphan: t-1245'; exit 1"])
    assert engine.dispatch(w, local(tmp_path)) == 0  # shift ran, not aborted
    text = ledger_text(tmp_path)
    assert "GHOST" in text and "rc=1" in text
    assert "WARN" in text and "ghost-audit" in text
    assert "START" in text and "DONE" in text


def test_ghost_audit_skipped_in_dry_run(tmp_path):
    """Dry-run must not spawn the ghost audit command."""
    marker = tmp_path / "ghost_ran"
    w = make_worker(tmp_path, ghost_audit=["/bin/sh", "-c", "touch %s" % marker])
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    assert not marker.exists(), "ghost audit must not spawn in dry-run"
    assert "GHOST" not in ledger_text(tmp_path)


# §6 scope enforcement

def test_scope_home_not_set_always_dispatches(tmp_path):
    """No scope_home → no scope check; dispatch proceeds normally."""
    w = make_worker(tmp_path)
    assert w.scope_home == ""
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "SCOPE_DENY" not in ledger_text(tmp_path)


def test_scope_home_allow_within_home(tmp_path):
    """workdir under scope_home → allowed; no SCOPE_DENY."""
    home = tmp_path / "home"
    home.mkdir()
    hood = home / "neighborhood"
    hood.mkdir()
    w = make_worker(tmp_path, workdir=str(hood), scope_home=str(home))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "SCOPE_DENY" not in ledger_text(tmp_path)
    assert "DONE" in ledger_text(tmp_path)


def test_scope_home_deny_cross_cabinet(tmp_path):
    """workdir outside scope_home with no grants → SCOPE_DENY, rc=1, no START."""
    home = tmp_path / "home-A"
    home.mkdir()
    cross = tmp_path / "cabinet-B"
    cross.mkdir()
    w = make_worker(tmp_path, workdir=str(cross), scope_home=str(home))
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "SCOPE_DENY" in text
    assert "START" not in text


def test_scope_deny_logs_workdir_and_home(tmp_path):
    """SCOPE_DENY ledger entry includes workdir= and home= fields."""
    home = tmp_path / "home-A"
    home.mkdir()
    cross = tmp_path / "cabinet-B"
    cross.mkdir()
    w = make_worker(tmp_path, workdir=str(cross), scope_home=str(home))
    engine.dispatch(w, local(tmp_path))
    text = ledger_text(tmp_path)
    assert "workdir=" in text and "home=" in text


def test_scope_perimeter_grant_allows_cross_cabinet(tmp_path):
    """workdir outside scope_home but listed in perimeter_grants → allowed."""
    home = tmp_path / "home-A"
    home.mkdir()
    cross = tmp_path / "cabinet-B"
    cross.mkdir()
    w = make_worker(tmp_path, workdir=str(cross), scope_home=str(home),
                    perimeter_grants=[str(cross)])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "SCOPE_DENY" not in ledger_text(tmp_path)
    assert "DONE" in ledger_text(tmp_path)


def test_scope_check_fires_in_dry_run(tmp_path):
    """Scope enforcement is not skipped during dry-run — misconfiguration must be caught."""
    home = tmp_path / "home-A"
    home.mkdir()
    cross = tmp_path / "cabinet-B"
    cross.mkdir()
    w = make_worker(tmp_path, workdir=str(cross), scope_home=str(home))
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 1
    assert "SCOPE_DENY" in ledger_text(tmp_path)


# §9 host-mutation gate

def test_host_mutation_clean_command_allowed(tmp_path):
    """Standard worker command has no tier-2 patterns; dispatch proceeds normally."""
    w = make_worker(tmp_path)
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "HOST_MUTATION_DENY" not in ledger_text(tmp_path)
    assert "DONE" in ledger_text(tmp_path)


def test_host_mutation_production_label_refused(tmp_path):
    """Command containing a production protocolcity launchd label → HOST_MUTATION_DENY, rc=1."""
    w = make_worker(
        tmp_path,
        command=["launchctl", "bootstrap", "system",
                 "/Library/LaunchDaemons/com.protocolcity.workforce.plist"],
    )
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "HOST_MUTATION_DENY" in text
    assert "START" not in text


def test_host_mutation_test_label_allowed(tmp_path):
    """com.protocolcity.suite.test label is explicitly allowlisted; dispatch proceeds."""
    # Embed the test label as a shell $0 arg so it appears in argv but the
    # command still exits 0 — avoids having the subprocess fail on unrecognised args.
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "exit 0", "com.protocolcity.suite.test.local"],
    )
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "HOST_MUTATION_DENY" not in ledger_text(tmp_path)
    assert "DONE" in ledger_text(tmp_path)


def test_host_mutation_shared_port_refused(tmp_path):
    """Command referencing a shared city port (8799/8801/8797) → HOST_MUTATION_DENY."""
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "nc -l 8797"])
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "HOST_MUTATION_DENY" in ledger_text(tmp_path)
    assert "START" not in ledger_text(tmp_path)


def test_host_mutation_brew_install_refused(tmp_path):
    """brew install of a city package → HOST_MUTATION_DENY."""
    w = make_worker(tmp_path, command=["brew", "install", "protocolcity-daemon"])
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "HOST_MUTATION_DENY" in ledger_text(tmp_path)


def test_host_mutation_launchctl_bootstrap_refused(tmp_path):
    """launchctl bootstrap → HOST_MUTATION_DENY regardless of label."""
    w = make_worker(tmp_path, command=["launchctl", "bootstrap", "system", "/some/path"])
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "HOST_MUTATION_DENY" in ledger_text(tmp_path)


def test_host_mutation_deny_logs_pattern_and_argv_head(tmp_path):
    """HOST_MUTATION_DENY ledger entry includes pattern= and argv_head= fields."""
    w = make_worker(
        tmp_path,
        command=["launchctl", "bootout", "system/com.protocolcity.daemon"],
    )
    engine.dispatch(w, local(tmp_path))
    text = ledger_text(tmp_path)
    assert "pattern=" in text
    assert "argv_head=launchctl" in text


def test_host_mutation_check_fires_in_dry_run(tmp_path):
    """Misconfigured command is caught during dry-run — gate does not require a live run."""
    w = make_worker(
        tmp_path,
        command=["launchctl", "bootstrap", "system",
                 "com.protocolcity.workforce"],
    )
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 1
    assert "HOST_MUTATION_DENY" in ledger_text(tmp_path)


# wf-159 / wl-372 — kickstart + non-protocolcity live labels + wrappers

def test_host_mutation_launchctl_kickstart_live_label_refused(tmp_path):
    """launchctl kickstart of live desk label → HOST_MUTATION_DENY (incident class)."""
    w = make_worker(
        tmp_path,
        command=["launchctl", "kickstart", "-k",
                 "gui/501/com.ticketingprotocol.server"],
    )
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 1
    text = ledger_text(tmp_path)
    assert "HOST_MUTATION_DENY" in text
    assert "START" not in text
    assert "gate=" in text  # instruct: stage + FOUNDER · host


def test_host_mutation_ticketingprotocol_label_refused(tmp_path):
    """com.ticketingprotocol.* is a production launchd family (desk), not only com.protocolcity.*."""
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "echo com.ticketingprotocol.server"],
    )
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 1
    assert "HOST_MUTATION_DENY" in ledger_text(tmp_path)


def test_host_mutation_workforce_daemon_label_refused(tmp_path):
    """com.workforce.* (engine daemon) is tier-2 live service family."""
    w = make_worker(
        tmp_path,
        command=["launchctl", "kickstart", "gui/501/com.workforce.daemon"],
    )
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 1
    assert "HOST_MUTATION_DENY" in ledger_text(tmp_path)


def test_host_mutation_wrapper_restart_refused(tmp_path):
    """tk/blueprint/worklane/workforce serve|stop|restart → HOST_MUTATION_DENY.

    Argv embeds the wrapper phrase under /bin/sh so preflight's CLI-present
    check does not SKIP before the mutation guard runs.
    """
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "worklane restart"])
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 1
    assert "HOST_MUTATION_DENY" in ledger_text(tmp_path)


def test_host_mutation_killall_city_service_refused(tmp_path):
    """killall of a city service binary name → HOST_MUTATION_DENY."""
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "killall -9 ticketingprotocol"],
    )
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 1
    assert "HOST_MUTATION_DENY" in ledger_text(tmp_path)


# wf-102: worker fallback on quota hit


def test_build_fallback_argv_replaces_cli_and_model(tmp_path):
    """_build_fallback_argv() replaces command[0] with fallback_runtime and {model} with fallback_model."""
    w = make_worker(tmp_path,
                    command=["claude", "--model", "{model}", "-p", "{prompt_text}"],
                    fallback_runtime="cursor", fallback_model="cursor-model")
    argv = engine._build_fallback_argv(w, "the brief")
    assert argv[0] == "cursor"
    assert "--model" in argv
    assert "cursor-model" in argv
    assert "the brief" in argv


def test_build_fallback_argv_drops_model_when_empty(tmp_path):
    """_build_fallback_argv() drops the --model flag pair when fallback_model is empty."""
    w = make_worker(tmp_path,
                    command=["claude", "--model", "{model}", "-p", "{prompt_text}"],
                    fallback_runtime="cursor", fallback_model="")
    argv = engine._build_fallback_argv(w, "brief")
    assert argv[0] == "cursor"
    assert "--model" not in argv
    assert "brief" in argv


def test_build_fallback_argv_no_model_token(tmp_path):
    """command with no {model} token: only argv[0] is replaced."""
    w = make_worker(tmp_path,
                    command=["claude", "-p", "{prompt_text}"],
                    fallback_runtime="cursor", fallback_model="")
    argv = engine._build_fallback_argv(w, "brief")
    assert argv == ["cursor", "-p", "brief"]


def test_worker_validate_rejects_unknown_fallback_runtime(tmp_path):
    """fallback_runtime not in KNOWN_RUNTIMES → RosterError on validate()."""
    w = make_worker(tmp_path, fallback_runtime="zapgpt")
    with pytest.raises(RosterError, match="fallback_runtime"):
        w.validate()


def test_worker_validate_accepts_known_fallback_runtime(tmp_path):
    """fallback_runtime in KNOWN_RUNTIMES validates without error."""
    w = make_worker(tmp_path, fallback_runtime="cursor")
    w.validate()  # must not raise


def test_worker_validate_empty_fallback_runtime_is_fine(tmp_path):
    """Empty fallback_runtime (default) passes validation with no KNOWN_RUNTIMES check."""
    w = make_worker(tmp_path)
    assert w.fallback_runtime == ""
    w.validate()  # must not raise


def test_parse_shifts_threads_fallback_runtime(tmp_path):
    """fallback_runtime from a DONE event appears in the shift read-model."""
    from workforce.ledger import parse_shifts

    text = (
        "2026-07-29T10:00:00Z START queue=1 budget_secs=1500\n"
        "2026-07-29T10:01:00Z DONE rc=0 on_pass=1 fallback_runtime=cursor\n"
        "2026-07-29T10:01:00Z STOP reason=\"fallback complete (cursor)\"\n"
    )
    shifts = parse_shifts(text, limit=5)
    assert shifts
    s = shifts[0]
    assert s["fallback_runtime"] == "cursor"
    assert s["outcome"] == "ok"


def test_parse_shifts_fallback_runtime_empty_by_default(tmp_path):
    """Shifts without a fallback_runtime DONE key carry '' in the read-model."""
    from workforce.ledger import parse_shifts

    text = (
        "2026-07-29T10:00:00Z START queue=1 budget_secs=1500\n"
        "2026-07-29T10:01:00Z DONE rc=0 on_pass=1\n"
        "2026-07-29T10:01:00Z STOP reason=\"single-pass complete\"\n"
    )
    shifts = parse_shifts(text, limit=5)
    assert shifts[0]["fallback_runtime"] == ""


def test_fallback_triggered_on_quota_hit(tmp_path):
    """When primary exits with quota signal and fallback_runtime is set, fallback runs."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    # Primary: exits with a quota-limit message.
    claude = bindir / "claude"
    claude.write_text("#!/bin/sh\necho '429 Too Many Requests — rate limit exceeded'; exit 1\n")
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)

    # Fallback: succeeds silently.
    cursor = bindir / "cursor"
    cursor.write_text("#!/bin/sh\nexit 0\n")
    cursor.chmod(cursor.stat().st_mode | stat.S_IEXEC)

    fake_path = "%s:/usr/bin:/bin" % bindir
    w = make_worker(tmp_path,
                    command=["claude", "ignored-arg"],
                    fallback_runtime="cursor",
                    env={"PATH": fake_path})
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "quota-fallback" in text
    assert "fallback=cursor" in text
    assert "fallback_runtime=cursor" in text
    assert "fallback complete (cursor)" in text
    assert "STOP" in text


def test_fallback_not_triggered_on_non_quota_error(tmp_path):
    """A generic non-zero exit (no vendor-limit output) does NOT trigger the fallback."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    claude = bindir / "claude"
    claude.write_text("#!/bin/sh\necho 'some unrelated error'; exit 2\n")
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)

    fake_path = "%s:/usr/bin:/bin" % bindir
    w = make_worker(tmp_path,
                    command=["claude", "ignored-arg"],
                    fallback_runtime="cursor",
                    env={"PATH": fake_path})
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "quota-fallback" not in text
    assert "agent exit" in text


def test_fallback_failure_logs_error_with_fallback_runtime(tmp_path):
    """When the fallback itself fails, ERROR is logged with fallback_runtime= key."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    claude = bindir / "claude"
    claude.write_text("#!/bin/sh\necho '429 rate limit'; exit 1\n")
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    cursor = bindir / "cursor"
    cursor.write_text("#!/bin/sh\necho 'fallback also failed'; exit 1\n")
    cursor.chmod(cursor.stat().st_mode | stat.S_IEXEC)

    fake_path = "%s:/usr/bin:/bin" % bindir
    w = make_worker(tmp_path,
                    command=["claude", "arg"],
                    fallback_runtime="cursor",
                    env={"PATH": fake_path})
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "quota-fallback" in text
    assert "fallback_runtime=cursor" in text


def test_dry_run_with_fallback_runtime_set(tmp_path):
    """Dry-run with fallback_runtime set completes without spawning anything."""
    w = make_worker(tmp_path, fallback_runtime="cursor")
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    text = ledger_text(tmp_path)
    assert "dry_run=1" in text
    assert "quota-fallback" not in text  # dry-run never reaches the subprocess path


# ---------------------------------------------------------------------------
# wf-153 — shift worktree isolation (shared-checkout concurrent edits)
# ---------------------------------------------------------------------------

def _git_init_hood(tmp_path, content="base\n"):
    """Primary checkout with one committed file; returns (workdir, path_to_f)."""
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)
    f = workdir / "shared.txt"
    f.write_text(content)
    subprocess.run(["git", "-C", str(workdir), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(workdir), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workdir), "config", "user.name", "tester"],
        check=True,
    )
    # Avoid "master" vs "main" noise across git versions.
    subprocess.run(
        ["git", "-C", str(workdir), "checkout", "-q", "-b", "main"],
        check=True,
    )
    subprocess.run(["git", "-C", str(workdir), "add", "shared.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workdir), "commit", "-q", "-m", "init"],
        check=True,
    )
    return workdir, f


def test_shift_worktree_off_uses_primary_cwd(tmp_path):
    """Default: child cwd is primary workdir (no worktree under local/)."""
    workdir, _ = _git_init_hood(tmp_path)
    out = tmp_path / "cwd.txt"
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "pwd > %s" % out],
        shift_worktree=False,
    )
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert os.path.realpath(out.read_text().strip()) == os.path.realpath(str(workdir))
    assert not (tmp_path / "local" / "worktrees" / "tester").exists()
    assert "shift_worktree=" not in ledger_text(tmp_path)


def test_shift_worktree_isolates_primary_dirty_same_file(tmp_path):
    """Incident class: founder dirty on primary must not enter hand commit.

    Primary has uncommitted FOUNDER_WIP on shared.txt while hand shift with
    shift_worktree=true reads/writes/commits the same path — child must see
    committed base only; primary dirty must remain; hand commit must not
    contain FOUNDER_WIP.
    """
    workdir, shared = _git_init_hood(tmp_path, content="base\n")
    shared.write_text("FOUNDER_WIP\n")  # concurrent founder session dirty

    seen = tmp_path / "seen.txt"
    # Child runs in shift worktree: observe file, apply hand edit, commit.
    script = (
        "cat shared.txt > %(seen)s && "
        "printf 'hand-edit\\n' > shared.txt && "
        "git add shared.txt && "
        "git -c user.email=hand@example.com -c user.name=hand "
        "commit -q -m 'hand slice' && "
        "git rev-parse HEAD > %(rev)s"
    ) % {"seen": seen, "rev": tmp_path / "hand_rev.txt"}

    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", script],
        shift_worktree=True,
        budget_secs=30,
    )
    assert engine.dispatch(w, local(tmp_path)) == 0

    text = ledger_text(tmp_path)
    assert "shift_worktree=1" in text
    wt = tmp_path / "local" / "worktrees" / "tester"
    assert wt.is_dir()
    assert "shift_cwd=" in text

    # Hand saw committed base, not founder WIP.
    assert seen.read_text() == "base\n"
    # Primary still holds founder dirty.
    assert shared.read_text() == "FOUNDER_WIP\n"
    # Hand commit tree has hand-edit only.
    hand_rev = (tmp_path / "hand_rev.txt").read_text().strip()
    show = subprocess.run(
        ["git", "-C", str(wt), "show", "%s:shared.txt" % hand_rev],
        capture_output=True, text=True, check=True,
    )
    assert show.stdout == "hand-edit\n"
    assert "FOUNDER_WIP" not in show.stdout

    # Env exposes both roots.
    env_out = tmp_path / "env.txt"
    w2 = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "env > %s" % env_out],
        shift_worktree=True,
    )
    assert engine.dispatch(w2, local(tmp_path)) == 0
    env_text = env_out.read_text()
    assert "WORKFORCE_SHIFT_WORKDIR=" in env_text
    assert "WORKFORCE_PRIMARY_WORKDIR=" in env_text
    assert str(workdir) in env_text or os.path.realpath(str(workdir)) in env_text


def test_shift_worktree_dry_run_does_not_create(tmp_path):
    """Dry-run with flag on must not create worktrees or mutate refs."""
    _git_init_hood(tmp_path)
    w = make_worker(tmp_path, shift_worktree=True)
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    assert not (tmp_path / "local" / "worktrees" / "tester").exists()
    assert "dry_run=1" in ledger_text(tmp_path)
    assert "shift_worktree=1" in ledger_text(tmp_path)


def test_shift_finalize_ff_merges_into_primary_when_clean(tmp_path):
    """Slice 3: successful shift with clean trees FF-lands shift tip on primary."""
    workdir, shared = _git_init_hood(tmp_path, content="base\n")
    primary_before = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    script = (
        "printf 'hand-landed\\n' > shared.txt && "
        "git add shared.txt && "
        "git -c user.email=hand@example.com -c user.name=hand "
        "commit -q -m 'hand slice'"
    )
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", script],
        shift_worktree=True,
        budget_secs=30,
    )
    assert engine.dispatch(w, local(tmp_path)) == 0

    text = ledger_text(tmp_path)
    assert "shift_ff=1" in text
    primary_after = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert primary_after != primary_before
    show = subprocess.run(
        ["git", "-C", str(workdir), "show", "%s:shared.txt" % primary_after],
        capture_output=True, text=True, check=True,
    )
    assert show.stdout == "hand-landed\n"
    # Working tree on primary matches the landed tip (no leftover dirt).
    assert shared.read_text() == "hand-landed\n"


def test_shift_finalize_skips_when_primary_dirty(tmp_path):
    """Primary founder WIP blocks FF land; shift branch left for rescue."""
    workdir, shared = _git_init_hood(tmp_path, content="base\n")
    shared.write_text("FOUNDER_WIP\n")
    primary_before = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    script = (
        "printf 'hand-edit\\n' > shared.txt && "
        "git add shared.txt && "
        "git -c user.email=hand@example.com -c user.name=hand "
        "commit -q -m 'hand slice'"
    )
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", script],
        shift_worktree=True,
        budget_secs=30,
    )
    assert engine.dispatch(w, local(tmp_path)) == 0

    text = ledger_text(tmp_path)
    assert "shift finalize: primary dirty" in text
    assert "shift_ff=1" not in text
    primary_after = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert primary_after == primary_before
    assert shared.read_text() == "FOUNDER_WIP\n"


def test_shift_finalize_skips_when_not_ff_able(tmp_path):
    """Diverged primary vs shift → WARN, no force merge."""
    workdir, shared = _git_init_hood(tmp_path, content="base\n")

    # Hand commits on shift first (create worktree via a no-op dispatch path
    # is heavy — prepare worktree by hand, then diverge primary).
    script = (
        "printf 'hand-a\\n' > shared.txt && "
        "git add shared.txt && "
        "git -c user.email=hand@example.com -c user.name=hand "
        "commit -q -m 'hand a'"
    )
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", script],
        shift_worktree=True,
        budget_secs=30,
    )
    assert engine.dispatch(w, local(tmp_path)) == 0
    # First shift FFs into primary (clean). Now diverge primary with a
    # concurrent commit, then run a second shift that also commits — the
    # second shift merges primary at start, so to get non-ff we need primary
    # to move *after* shift prepare and *before* finalize. Simulate by
    # committing on primary while a dirty-less shift branch is ahead on a
    # different line: reset primary to pre-hand and commit founder line.
    hand_tip = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Reset primary to parent of hand tip, commit a divergent founder line.
    parent = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(workdir), "reset", "--hard", parent],
        check=True, capture_output=True,
    )
    shared.write_text("founder-diverge\n")
    subprocess.run(["git", "-C", str(workdir), "add", "shared.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workdir), "commit", "-q", "-m", "founder diverge"],
        check=True,
    )
    founder_tip = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert founder_tip != hand_tip

    # Point shift worktree back at the old hand tip (ahead on other line).
    wt = tmp_path / "local" / "worktrees" / "tester"
    subprocess.run(
        ["git", "-C", str(wt), "reset", "--hard", hand_tip],
        check=True, capture_output=True,
    )

    # Dispatch a no-op successful shift (shift already clean at hand_tip).
    # _prepare will try to merge primary into shift; that may create a merge
    # commit or fail. To isolate finalize, call it directly.
    from workforce.engine import Ledger, _finalize_shift_workdir

    led = Ledger(str(tmp_path / "local" / "ledger"), "tester")
    kv = _finalize_shift_workdir(w, str(wt), led)
    assert kv == {}
    text = (tmp_path / "local" / "ledger" / "tester.log").read_text()
    assert "not ff-able" in text
    # Primary unchanged (still founder tip).
    assert subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip() == founder_tip


def test_shift_finalize_noop_when_heads_equal(tmp_path):
    """No shift commits → finalize is silent (no shift_ff, no WARN)."""
    workdir, _ = _git_init_hood(tmp_path)
    before = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "true"],
        shift_worktree=True,
    )
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "shift_ff=1" not in text
    assert "shift finalize:" not in text
    after = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert after == before


# ---------------------------------------------------------------------------
# wf-155 — startup reconciliation (orphan lock + stranded in_progress)
# ---------------------------------------------------------------------------

def test_desk_origin_and_product_from_queue_url():
    url = "http://127.0.0.1:8799/api/admin/tasks/ready?product=workforce&label=worker:salem"
    assert engine.desk_origin_from_queue_url(url) == "http://127.0.0.1:8799"
    assert engine.product_from_queue_url(url) == "workforce"
    assert engine.desk_origin_from_queue_url("file:///tmp/q.json") is None
    assert engine.product_from_queue_url("http://desk/x") is None


def test_latest_owner_id_from_comments():
    comments = [
        {"body": "Intake: filed by you", "author": "you"},
        {"body": "Owner: blossom\nPlan:\n- a", "author": "blossom"},
        {"body": "Owner: salem (grok-4.5)\nClaimed.", "author": "salem"},
    ]
    assert engine.latest_owner_id(comments) == "salem"
    assert engine.latest_owner_id([]) is None
    assert engine.latest_owner_id([{"body": "no marker"}]) is None


def test_clean_orphan_lock_dead_pid(tmp_path):
    locks = tmp_path / "local" / "locks"
    lock = locks / "tester.lock"
    lock.mkdir(parents=True)
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    proc.wait()
    (lock / "pid").write_text(str(proc.pid))
    assert engine.clean_orphan_lock(local(tmp_path), "tester") is True
    assert not lock.exists()


def test_clean_orphan_lock_spares_live_pid(tmp_path):
    locks = tmp_path / "local" / "locks"
    lock = locks / "tester.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    assert engine.clean_orphan_lock(local(tmp_path), "tester") is False
    assert lock.exists()


def test_reconcile_dead_shift_releases_owned_ticket(tmp_path, monkeypatch):
    """Kill-during-shift simulation: orphan lock + Owner: marker → release."""
    # Dead lock
    locks = tmp_path / "local" / "locks"
    lock = locks / "tester.lock"
    lock.mkdir(parents=True)
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    proc.wait()
    (lock / "pid").write_text(str(proc.pid))

    desk = "http://desk.test"
    product = "workforce"
    queue = (
        "%s/api/admin/tasks/ready?product=%s&label=worker:tester" % (desk, product)
    )
    w = make_worker(tmp_path, queue_url=queue, identity="tester-id", name="tester")

    calls = []

    def fake_http(method, url, body=None, timeout=8.0):
        calls.append((method, url, body))
        if method == "GET" and "/api/admin/tasks?" in url and "status=in_progress" in url:
            return {
                "ok": True,
                "tasks": [{
                    "id": "wf-999",
                    "status": "in_progress",
                    "labels": ["worker:tester", "engine"],
                }],
            }
        if method == "GET" and "/api/admin/tasks/wf-999" in url:
            return {
                "ok": True,
                "task": {
                    "id": "wf-999",
                    "status": "in_progress",
                    "labels": ["worker:tester"],
                    "comments": [
                        {"body": "Owner: tester-id\nPlan: fix it", "author": "tester-id"},
                    ],
                },
            }
        if method == "POST" and "comments" in url:
            assert body and "Blocked:" in body["body"] and "Next step:" in body["body"]
            assert body["author"] == "tester-id"
            return {"ok": True, "comment": {"id": 1}}
        return {"ok": False, "error": "unexpected %s %s" % (method, url)}

    monkeypatch.setattr(engine, "_http_json", fake_http)
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")

    receipt = engine.reconcile_dead_shift(w, local(tmp_path))
    assert receipt["lock_cleaned"] is True
    assert receipt["released"] == ["wf-999"]
    assert not lock.exists()
    assert any(c[0] == "POST" for c in calls)
    text = ledger_text(tmp_path)
    assert "startup-reconcile-lock" in text
    assert "startup-reconcile-release" in text


def test_reconcile_dead_shift_skips_live_lock(tmp_path, monkeypatch):
    locks = tmp_path / "local" / "locks"
    lock = locks / "tester.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    w = make_worker(
        tmp_path,
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
    )
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("desk must not be contacted for live lock")

    monkeypatch.setattr(engine, "_http_json", boom)
    receipt = engine.reconcile_dead_shift(w, local(tmp_path))
    assert receipt["released"] == []
    assert "lock-live" in receipt["skipped"]
    assert not called
    assert lock.exists()


def test_reconcile_dead_shift_skips_foreign_owner(tmp_path, monkeypatch):
    locks = tmp_path / "local" / "locks"
    lock = locks / "tester.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("1")  # pid 1 may be alive on some hosts — force dead via mock
    # Ensure orphan: use a guaranteed-dead pid via subprocess
    import subprocess as _sp
    p = _sp.Popen(["/bin/sh", "-c", "exit 0"])
    p.wait()
    (lock / "pid").write_text(str(p.pid))

    w = make_worker(
        tmp_path,
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        identity="tester-id",
    )

    def fake_http(method, url, body=None, timeout=8.0):
        if method == "GET" and "status=in_progress" in url:
            return {
                "ok": True,
                "tasks": [{
                    "id": "wf-1",
                    "status": "in_progress",
                    "labels": ["worker:tester"],
                }],
            }
        if method == "GET" and "/wf-1" in url:
            return {
                "ok": True,
                "task": {
                    "id": "wf-1",
                    "status": "in_progress",
                    "comments": [{"body": "Owner: you\nmanual hold", "author": "you"}],
                },
            }
        if method == "POST":
            raise AssertionError("must not release foreign Owner")
        return {}

    monkeypatch.setattr(engine, "_http_json", fake_http)
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")
    receipt = engine.reconcile_dead_shift(w, local(tmp_path))
    assert receipt["released"] == []
    assert any("owner=you" in s for s in receipt["skipped"])


def test_startup_reconcile_force_from_prior_in_flight(tmp_path, monkeypatch):
    """Prior heartbeat in_flight + no lock still releases (lock already wiped)."""
    w = make_worker(
        tmp_path,
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        identity="tester-id",
        name="tester",
    )
    released = []

    def fake_http(method, url, body=None, timeout=8.0):
        if method == "GET" and "status=in_progress" in url:
            return {
                "ok": True,
                "tasks": [{
                    "id": "wf-2",
                    "status": "in_progress",
                    "labels": ["worker:tester"],
                }],
            }
        if method == "GET" and "/wf-2" in url:
            return {
                "ok": True,
                "task": {
                    "id": "wf-2",
                    "status": "in_progress",
                    "comments": [{"body": "Owner: tester-id", "author": "tester-id"}],
                },
            }
        if method == "POST":
            released.append("wf-2")
            return {"ok": True}
        return {}

    monkeypatch.setattr(engine, "_http_json", fake_http)
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")
    report = engine.startup_reconcile(
        {"tester": w}, local(tmp_path), prior_in_flight=["tester"],
    )
    assert "wf-2" in report["released"]
    assert released == ["wf-2"]


# ---------------------------------------------------------------------------
# wf-155 slice 2 — heartbeat reconcile on empty probe (ghost-audit catch-22)
# ---------------------------------------------------------------------------

def test_heartbeat_reconcile_releases_without_lock(tmp_path, monkeypatch):
    """No lock left after kill, empty ready → force-release stranded Owner: claim."""
    w = make_worker(
        tmp_path,
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        identity="tester-id",
        name="tester",
    )
    posts = []

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
            posts.append(body)
            assert body and "Heartbeat reconciliation" in body["body"]
            assert "Blocked:" in body["body"] and "Next step:" in body["body"]
            return {"ok": True}
        return {}

    monkeypatch.setattr(engine, "_http_json", fake_http)
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")
    receipt = engine.heartbeat_reconcile(w, local(tmp_path))
    assert receipt["source"] == "heartbeat"
    assert receipt["released"] == ["wf-155"]
    assert posts
    text = ledger_text(tmp_path)
    assert "heartbeat-reconcile-release" in text


def test_heartbeat_reconcile_skips_live_lock(tmp_path, monkeypatch):
    locks = tmp_path / "local" / "locks"
    lock = locks / "tester.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    w = make_worker(
        tmp_path,
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
    )
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("must not contact desk while lock is live")

    monkeypatch.setattr(engine, "_http_json", boom)
    receipt = engine.heartbeat_reconcile(w, local(tmp_path))
    assert receipt["released"] == []
    assert "lock-live" in receipt["skipped"]
    assert not called
    assert lock.exists()


def test_dispatch_empty_skip_runs_heartbeat_reconcile(tmp_path, monkeypatch):
    """Queue-empty SKIP path must force-release strands (ghost audit never runs)."""
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"count": 0}))
    w = make_worker(
        tmp_path,
        queue_url="http://desk.test/api/admin/tasks/ready?product=workforce&label=worker:tester",
        identity="tester-id",
        name="tester",
        min_free_mb=0,
    )
    # Force empty preflight without real http queue probe (wf-158: preflight
    # uses _probe_ready, which returns (count, tasks)).
    monkeypatch.setattr(engine, "_probe_ready", lambda _w, **_kw: (0, []))
    released = []

    def fake_http(method, url, body=None, timeout=8.0):
        if method == "GET" and "status=in_progress" in url:
            return {
                "ok": True,
                "tasks": [{
                    "id": "wf-3",
                    "status": "in_progress",
                    "labels": ["worker:tester"],
                }],
            }
        if method == "GET" and "wf-3" in url:
            return {
                "ok": True,
                "task": {
                    "id": "wf-3",
                    "status": "in_progress",
                    "comments": [{"body": "Owner: tester-id", "author": "tester-id"}],
                },
            }
        if method == "POST":
            released.append("wf-3")
            return {"ok": True}
        return {}

    monkeypatch.setattr(engine, "_http_json", fake_http)
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")
    rc = engine.dispatch(w, local(tmp_path))
    assert rc == 0
    text = ledger_text(tmp_path)
    assert "heartbeat-reconcile-release" in text
    assert released == ["wf-3"]
    # wf-231: empty SKIPs and still reconciles (no mill chew spawn)
    assert "SKIP" in text and "queue empty" in text
    assert "standing_chew=1" not in text
    released.clear()
    rc = engine.dispatch(w, local(tmp_path))
    assert rc == 0
    text = ledger_text(tmp_path)
    assert "SKIP" in text and "queue empty" in text


def test_list_tasks_filters_stamp_and_http_seam():
    """wf-216 — one desk list helper: filters, product-stamp, injectable http."""
    seen = []

    def fake_http(method, url, body=None, timeout=8.0):
        seen.append((method, url, timeout))
        return {
            "ok": True,
            "tasks": [
                {"id": "wf-1", "status": "backlog"},
                {"id": "wf-2", "status": "backlog", "product": "workforce"},
                "skip-me",
            ],
        }

    rows = engine._list_tasks(
        "http://desk.test/",
        "workforce",
        status="backlog",
        label="worker:salem",
        gate_type="human",
        limit=50,
        timeout=12.0,
        http=fake_http,
    )
    assert [r["id"] for r in rows] == ["wf-1", "wf-2"]
    assert rows[0]["product"] == "workforce"
    assert rows[1]["product"] == "workforce"
    assert len(seen) == 1
    method, url, timeout = seen[0]
    assert method == "GET"
    assert timeout == 12.0
    assert url.startswith("http://desk.test/api/admin/tasks?")
    assert "product=workforce" in url
    assert "status=backlog" in url
    assert "gate_type=human" in url
    assert "limit=50" in url
    assert "label=worker" in url


def test_list_tasks_non_dict_and_items_key():
    def not_dict(*a, **k):
        return ["not", "a", "dict"]

    assert engine._list_tasks(
        "http://desk.test", "workforce", http=not_dict,
    ) == []

    def items_payload(*a, **k):
        return {"items": [{"id": "a"}]}

    rows = engine._list_tasks(
        "http://desk.test", "workforce", http=items_payload,
    )
    assert rows[0]["id"] == "a"
    assert rows[0]["product"] == "workforce"


# wf-262 — stop-reason verification + bounded task_runner-recovery continuation
#
# A fake task_runner-shaped CLI: prints the same "Prepared ...; Receipt: ..."
# line every real task_runner.py invocation prints (fresh or recovered),
# then a JSON result whose stopReason depends only on whether its own argv
# carries --recover-receipt — mirroring grok's real rc==0/stopReason=cancelled
# behavior on a first pass and a genuine end_turn once "resumed".

_FAKE_TASK_RUNNER = '''import sys
from pathlib import Path
calls_path, receipt_path = sys.argv[1], sys.argv[2]
n = int(Path(calls_path).read_text()) if Path(calls_path).exists() else 0
Path(calls_path).write_text(str(n + 1))
print("Prepared demo-task; not yet claimed. Receipt: %s" % receipt_path)
if "--recover-receipt" in sys.argv:
    print('{"stopReason": "end_turn", "sessionId": "sid-1"}')
else:
    print('{"stopReason": "cancelled", "sessionId": "sid-1"}')
'''


def _fake_task_runner_worker(tmp_path, **over):
    script = tmp_path / "fake_task_runner.py"
    script.write_text(_FAKE_TASK_RUNNER)
    calls = tmp_path / "calls.txt"
    receipt = tmp_path / "preparation.json"
    receipt.write_text("{}")
    over.setdefault("completion_field", "stopReason")
    over.setdefault("completion_values", ["end_turn"])
    w = make_worker(
        tmp_path,
        command=[sys.executable, str(script), str(calls), str(receipt)],
        **over,
    )
    return w, calls


def test_completion_field_off_keeps_rc0_is_done(tmp_path):
    """A worker that never opts in keeps today's behavior: rc==0 is DONE,
    even though the (unchecked) JSON result says stopReason=cancelled."""
    w, calls = _fake_task_runner_worker(tmp_path, completion_field="", completion_values=[])
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "DONE" in text and "STOP" in text
    assert calls.read_text() == "1"


def test_incomplete_stop_with_no_continuation_budget_fails_honestly(tmp_path):
    """rc==0 + stopReason=cancelled + continuation_attempts=0 → ERROR, not DONE."""
    w, calls = _fake_task_runner_worker(tmp_path, continuation_attempts=0)
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "ERROR" in text
    assert "denied: cancelled" in text
    assert "DONE" not in text
    assert calls.read_text() == "1"  # never blindly retried


def test_permission_cancelled_stop_is_denied_not_end_turn_not_budget_kill(tmp_path):
    """wf-262: grok's real stopReason for a denied run_terminal_command
    (cancellation_category=permission_cancelled in its own trace) is the
    same "cancelled" string as any other cancelled turn — it must classify
    as incomplete/denied, never be confused with a genuine end_turn
    completion, and never be reported as a budget kill (a distinct failure
    mode with its own "killed at budget" ledger reason)."""
    w, calls = _fake_task_runner_worker(tmp_path, continuation_attempts=0)
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "denied: cancelled" in text
    assert "killed at budget" not in text
    assert "DONE" not in text
    assert calls.read_text() == "1"  # a denied-tool stop is never auto-continued


def test_cancelled_stop_never_continues_even_with_budget(tmp_path):
    """wf-262 review: a permission/user cancellation is a real denial, not an
    ordinary incomplete stop — it must never be retried even when
    continuation_attempts > 0, unlike a genuinely incomplete (non-cancelled)
    stop that still has budget."""
    w, calls = _fake_task_runner_worker(tmp_path, continuation_attempts=2)
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "denied: cancelled" in text
    assert "continuations=0" in text
    assert "DONE" not in text
    assert calls.read_text() == "1"  # exactly one process; zero continuations spawned


def test_incomplete_stop_auto_continues_through_task_runner_recovery(tmp_path):
    """One bounded continuation reaches end_turn → honest DONE, evidence of the
    resume. Uses a genuinely incomplete (non-cancelled) first stop, since a
    cancelled stop is terminal and never auto-continues (wf-262 review)."""
    script = '''import sys
from pathlib import Path
calls_path, receipt_path = sys.argv[1], sys.argv[2]
n = int(Path(calls_path).read_text()) if Path(calls_path).exists() else 0
Path(calls_path).write_text(str(n + 1))
print("Prepared demo-task; not yet claimed. Receipt: %s" % receipt_path)
if "--recover-receipt" in sys.argv:
    print('{"stopReason": "end_turn", "sessionId": "sid-1"}')
else:
    print('{"stopReason": "still_running", "sessionId": "sid-1"}')
'''
    script_path = tmp_path / "fake_task_runner_resumable.py"
    script_path.write_text(script)
    calls = tmp_path / "calls.txt"
    receipt = tmp_path / "preparation.json"
    receipt.write_text("{}")
    w = make_worker(
        tmp_path,
        command=[sys.executable, str(script_path), str(calls), str(receipt)],
        completion_field="stopReason", completion_values=["end_turn"],
        continuation_attempts=1,
    )
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "DONE" in text and "continuations=1" in text
    assert "STOP" in text
    assert calls.read_text() == "2"  # first pass + one continuation


def test_continuation_exhausted_still_incomplete_fails(tmp_path):
    """A CLI that never reports end_turn (and isn't cancelled — a distinct,
    non-terminal incomplete stop) exhausts its budget and fails, not loops
    forever."""
    always_running = '''import sys
print("Prepared demo-task; not yet claimed. Receipt: %s" % sys.argv[1])
print('{"stopReason": "still_running", "sessionId": "sid-1"}')
'''
    script = tmp_path / "always_running.py"
    script.write_text(always_running)
    receipt = tmp_path / "preparation.json"
    receipt.write_text("{}")
    w = make_worker(
        tmp_path,
        command=[sys.executable, str(script), str(receipt)],
        completion_field="stopReason", completion_values=["end_turn"],
        continuation_attempts=2,
    )
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "incomplete: still_running" in text
    assert "continuations=2" in text
    assert "DONE" not in text


def test_incomplete_stop_without_receipt_cannot_continue(tmp_path):
    """A pass with no task_runner receipt line can't be auto-continued — honest failure."""
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", 'echo \'{"stopReason": "still_running"}\'; exit 0'],
        completion_field="stopReason", completion_values=["end_turn"],
        continuation_attempts=3,
    )
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "incomplete: still_running (no receipt to continue)" in text


def test_completion_field_missing_from_output_is_incomplete(tmp_path):
    """rc==0 with JSON present but no stopReason key at all — never trust silently."""
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", 'echo \'{"other": "value"}\'; exit 0'],
        completion_field="stopReason", completion_values=["end_turn"],
    )
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "incomplete: missing stopReason" in ledger_text(tmp_path)


def test_unparseable_output_with_completion_field_is_incomplete(tmp_path):
    """rc==0 with no JSON at all in the output — configured verification still refuses to trust it."""
    w = make_worker(
        tmp_path,
        command=["/bin/sh", "-c", "echo 'plain text, no json'; exit 0"],
        completion_field="stopReason", completion_values=["end_turn"],
    )
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "incomplete: unparseable output" in ledger_text(tmp_path)


def test_classify_completion_pretty_printed_json(tmp_path):
    """Grok's real --output-format json is pretty-printed, not one compact line — must still parse."""
    out = tmp_path / "pretty.out"
    out.write_text('{\n  "stopReason": "end_turn",\n  "sessionId": "abc"\n}\n')
    configured, complete, stop_value = engine._classify_completion(
        make_worker(tmp_path, completion_field="stopReason", completion_values=["end_turn"]),
        str(out), 0,
    )
    assert configured is True
    assert complete is True
    assert stop_value == "end_turn"


def test_classify_completion_not_configured_defaults_complete(tmp_path):
    configured, complete, stop_value = engine._classify_completion(
        make_worker(tmp_path), str(tmp_path / "missing.out"), 0,
    )
    assert configured is False
    assert complete is True


def test_last_receipt_path_picks_the_most_recent_match(tmp_path):
    out = tmp_path / "run.out"
    out.write_text(
        "Prepared x; not yet claimed. Receipt: /a/1/preparation.json\n"
        "--- continuation 1 ---\n"
        "Prepared x; not yet claimed. Receipt: /a/2/preparation.json\n"
    )
    assert engine._last_receipt_path(str(out), 0) == "/a/2/preparation.json"


def test_last_receipt_path_absent_returns_empty(tmp_path):
    out = tmp_path / "run.out"
    out.write_text('{"stopReason": "cancelled"}\n')
    assert engine._last_receipt_path(str(out), 0) == ""


def test_pass_result_json_finds_terminal_object_after_ndjson(tmp_path):
    """wf-262 review: a normal grok pass streams NDJSON diagnostics before
    the terminal result (available_commands, thought events, ...) — the
    first JSON object in the slice has no stopReason at all, so picking it
    (rather than the last, terminal one) would misclassify a successful
    end_turn pass as incomplete."""
    out = tmp_path / "ndjson.out"
    out.write_text(
        '{"type": "available_commands", "commands": ["a", "b"]}\n'
        '{"type": "thought", "text": "thinking..."}\n'
        '{"type": "end", "stopReason": "end_turn", "sessionId": "sid-1"}\n'
    )
    obj = engine._pass_result_json(str(out), 0)
    assert obj == {"type": "end", "stopReason": "end_turn", "sessionId": "sid-1"}


def test_pass_result_json_pretty_printed_cancelled_still_found(tmp_path):
    """The pretty-printed single-object cancelled-turn shape (stopReason at
    the top) must still classify correctly alongside the NDJSON fix."""
    out = tmp_path / "pretty.out"
    out.write_text(
        'some stray banner text\n'
        '{\n  "stopReason": "cancelled",\n  "sessionId": "sid-1"\n}\n'
    )
    obj = engine._pass_result_json(str(out), 0)
    assert obj == {"stopReason": "cancelled", "sessionId": "sid-1"}


def test_classify_completion_ndjson_end_turn_is_done(tmp_path):
    """End-to-end: _classify_completion on an NDJSON-then-end slice must
    report the pass complete, not incomplete (wf-262 review finding 1)."""
    out = tmp_path / "ndjson.out"
    out.write_text(
        '{"type": "available_commands", "commands": []}\n'
        '{"type": "end", "stopReason": "end_turn", "sessionId": "sid-1"}\n'
    )
    configured, complete, stop_value = engine._classify_completion(
        make_worker(tmp_path, completion_field="stopReason", completion_values=["end_turn"]),
        str(out), 0,
    )
    assert configured is True
    assert complete is True
    assert stop_value == "end_turn"


def test_strip_recovery_flags_removes_existing_pairs(tmp_path):
    argv = [
        "task_runner.py", "--recover-receipt", "orig.json",
        "--recovery-reason", "orig reason", "--legacy-stop-evidence", "orig-evidence.json",
        "--other-flag", "kept",
    ]
    assert engine._strip_recovery_flags(argv) == [
        "task_runner.py", "--other-flag", "kept",
    ]


def test_strip_recovery_flags_noop_without_flags(tmp_path):
    argv = ["task_runner.py", "--other-flag", "kept"]
    assert engine._strip_recovery_flags(argv) == argv


def test_continuation_strips_prior_recovery_flags_from_baked_in_argv(tmp_path):
    """wf-262 review finding 2: when the worker's own argv already carries
    --recover-receipt/--recovery-reason (an operator-recovery dispatch), a
    continuation must not append a second pair — argparse last-wins would
    otherwise leave task_runner pointed at whichever receipt was appended
    last rather than this pass's own."""
    script = '''import sys
from pathlib import Path
calls_path, receipt_path = sys.argv[1], sys.argv[2]
n = int(Path(calls_path).read_text()) if Path(calls_path).exists() else 0
n += 1
Path(calls_path).write_text(str(n))
Path(calls_path + (".argv.%d" % n)).write_text(" ".join(sys.argv))
print("Prepared demo-task; not yet claimed. Receipt: %s" % receipt_path)
if n == 1:
    print('{"stopReason": "still_running", "sessionId": "sid-1"}')
else:
    print('{"stopReason": "end_turn", "sessionId": "sid-1"}')
'''
    script_path = tmp_path / "recording_task_runner.py"
    script_path.write_text(script)
    calls = tmp_path / "calls.txt"
    receipt = tmp_path / "preparation.json"
    receipt.write_text("{}")
    w = make_worker(
        tmp_path,
        command=[
            sys.executable, str(script_path), str(calls), str(receipt),
            "--recover-receipt", "orig.json", "--recovery-reason", "orig reason",
        ],
        completion_field="stopReason", completion_values=["end_turn"],
        continuation_attempts=1,
    )
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert calls.read_text() == "2"
    cont_argv = (tmp_path / "calls.txt.argv.2").read_text().split(" ")
    assert cont_argv.count("--recover-receipt") == 1
    assert cont_argv.count("--recovery-reason") == 1
    assert "orig.json" not in cont_argv
    assert cont_argv[cont_argv.index("--recover-receipt") + 1] == str(receipt)


def test_continuation_attempts_requires_completion_field(tmp_path):
    with pytest.raises(RosterError, match="continuation_attempts requires completion_field"):
        make_worker(tmp_path, continuation_attempts=1).validate()


def test_completion_values_requires_completion_field(tmp_path):
    with pytest.raises(RosterError, match="completion_values requires completion_field"):
        make_worker(tmp_path, completion_values=["end_turn"]).validate()


def test_continuation_attempts_negative_rejected(tmp_path):
    with pytest.raises(RosterError, match="continuation_attempts must be >= 0"):
        make_worker(
            tmp_path, completion_field="stopReason", continuation_attempts=-1,
        ).validate()


def test_pre_park_terminal_wait_is_capped_by_linger_grace(monkeypatch, tmp_path):
    """wf-266 third pass: a verified terminal result with the desk still in_progress
    must not hold the shift for the whole remaining budget; the pre-park wait is
    bounded by linger_grace_secs like the post-park linger."""
    import inspect
    from workforce import engine as eng
    src = inspect.getsource(eng._run_pass)
    assert "terminal_seen_at" in src
    assert "pending >= worker.linger_grace_secs" in src

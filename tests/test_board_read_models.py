"""The board's read models — shift parsing, law stack, health — no HTTP."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workforce.api.roster as _api_roster  # noqa: E402
from workforce.board import (  # noqa: E402
    _contract_rules, _law_stack, _worker_flags, _worker_queue, render_law, worker_model,
)
from workforce.ledger import open_claims, parse_shifts  # noqa: E402
from workforce.roster import Roster, Worker  # noqa: E402

LEDGER = """\
2026-07-14T01:40:00Z SKIP reason="CLI 'claude' not installed"
2026-07-14T02:10:00Z START identity=x kind=lane model=m budget_secs=1500 queue=1 contract_sha=aa prompt_sha=bb dry_run=0
2026-07-14T02:15:02Z DONE rc=0
2026-07-14T02:15:02Z STOP reason="single-pass complete"
2026-07-14T03:00:00Z START identity=x kind=lane model=m budget_secs=1500 max_passes=4 queue=80 contract_sha=aa prompt_sha=bb dry_run=0
2026-07-14T03:04:00Z DONE rc=0 on_pass=1
2026-07-14T03:08:00Z DONE rc=0 on_pass=2
2026-07-14T03:08:01Z STOP reason="no progress (79 -> 79)"
2026-07-14T03:30:00Z START identity=x kind=lane model=m budget_secs=5 queue=3 contract_sha=aa prompt_sha=bb dry_run=0
2026-07-14T03:30:05Z ERROR reason="killed at budget" budget_secs=5 on_pass=1
"""


def test_parse_shifts_groups_and_orders_newest_first():
    shifts = parse_shifts(LEDGER)
    assert [s["outcome"] for s in shifts] == ["error", "ok", "ok", "skip"]
    multi = shifts[1]
    assert multi["passes"] == 2 and multi["queue"] == "80"
    assert "no progress" in multi["reason"]
    assert shifts[3]["reason"].startswith("CLI 'claude'")


def test_parse_shifts_running_shift_stays_open():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = LEDGER + "%s START identity=x kind=lane queue=2 dry_run=0\n" % now
    shifts = parse_shifts(text)
    assert shifts[0]["outcome"] == "running" and shifts[0]["end_ts"] == ""


def test_parse_shifts_dry_run_closes_at_done():
    text = ("2026-07-14T04:00:00Z START identity=x kind=lane queue=2 dry_run=1\n"
            "2026-07-14T04:00:00Z DONE dry_run=1 argv_head=cli argv_len=3\n")
    s = parse_shifts(text)[0]
    assert s["outcome"] == "ok" and s["dry_run"] and s["reason"] == "dry-run"


def test_open_claims_empty_when_no_open_shift():
    assert open_claims(LEDGER) == []
    text = ("2026-07-14T04:00:00Z START identity=x kind=lane queue=1 dry_run=0\n"
            "2026-07-14T04:00:01Z CLAIM ticket=wf-1 title=hello product=workforce\n"
            "2026-07-14T04:00:02Z DONE rc=0 on_pass=1\n"
            "2026-07-14T04:00:02Z STOP reason=\"single-pass complete\"\n")
    assert open_claims(text) == []


def test_open_claims_tracks_running_shift_and_clears_on_error():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    open_text = (
        "%s START identity=x kind=lane queue=2 dry_run=0\n"
        "%s CLAIM ticket=wf-158 title=\"Engine claim\" product=workforce priority=3\n"
        "%s CLAIM ticket=wf-159 title=other product=workforce\n"
    ) % (now, now, now)
    held = open_claims(open_text)
    assert [c["ticket"] for c in held] == ["wf-158", "wf-159"]
    assert held[0]["title"] == "Engine claim"
    assert held[0]["product"] == "workforce"
    # multi-pass DONE does not clear
    mid = open_text + "%s DONE rc=0 on_pass=1\n" % now
    assert len(open_claims(mid)) == 2
    # ERROR closes the window
    closed = mid + "%s ERROR reason=\"killed at budget\"\n" % now
    assert open_claims(closed) == []


def test_open_claims_clears_on_dry_run_done():
    text = ("2026-07-14T04:00:00Z START identity=x kind=lane queue=1 dry_run=1\n"
            "2026-07-14T04:00:00Z CLAIM ticket=wf-1 title=t product=workforce\n"
            "2026-07-14T04:00:00Z DONE dry_run=1 argv_head=cli argv_len=3\n")
    assert open_claims(text) == []


def test_ledger_holdings_maps_open_claims(tmp_path):
    import datetime
    local = tmp_path / "local"
    (local / "ledger").mkdir(parents=True)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (local / "ledger" / "kai.log").write_text(
        "%s START identity=kai kind=lane queue=1 dry_run=0\n"
        "%s CLAIM ticket=wf-158 title=\"Engine-owned claim\" product=workforce priority=3\n"
        % (now, now)
    )
    held = _api_roster._ledger_holdings(str(local), "kai", owner="kai")
    assert len(held) == 1
    assert held[0]["id"] == "wf-158"
    assert held[0]["title"] == "Engine-owned claim"
    assert held[0]["product"] == "workforce"
    assert held[0]["priority"] == 3
    assert held[0]["owner"] == "kai"
    assert held[0]["source"] == "ledger"
    assert "open=wf-158" in held[0]["href"]


def make_worker(tmp_path):
    hood = tmp_path / "city" / "hood"
    hood.mkdir(parents=True)
    (tmp_path / "city" / "AGENTS.md").write_text("# city law\n")
    (hood / "AGENTS.md").write_text("# neighborhood law\n")
    contract = hood / "CONTRACT.md"
    contract.write_text("# contract\n\n## Lane\nwork lane:x tickets\n\n"
                        "## Never touch\nsecrets\nprod\n\n## Notes\nnot a rule\n")
    prompt = hood / "prompt.md"
    prompt.write_text("brief\n")
    return Worker(name="x", workdir=str(hood), contract=str(contract),
                  prompt=str(prompt), identity="x", command=["true"])


def test_worker_queue_reads_roster_dot_path(tmp_path):
    probe = tmp_path / "queue.json"
    probe.write_text('{"column_counts": {"backlog": 91}}')
    w = make_worker(tmp_path)
    w.queue_url = probe.as_uri()
    w.queue_count_key = "column_counts.backlog"
    assert _worker_queue(w) == "91"


def test_worker_queue_no_retry_on_timeout(tmp_path, monkeypatch):
    """wf-147: board probe does not retry timeouts (latency bound for Map)."""
    from workforce.api import roster as roster_mod

    w = make_worker(tmp_path)
    w.queue_url = "http://127.0.0.1:9/ready"
    w.queue_count_key = "count"
    calls = {"n": 0}

    def always_timeout(_url, timeout=1.0):
        calls["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(roster_mod, "_http_get_json", always_timeout)
    assert _worker_queue(w) == "?"
    assert calls["n"] == 1  # one attempt only — no second 1s burn


def test_worker_queue_retries_once_on_connection_reset(tmp_path, monkeypatch):
    """wf-147: still retry once on transient connection drop (not timeout)."""
    from workforce.api import roster as roster_mod

    w = make_worker(tmp_path)
    w.queue_url = "http://127.0.0.1:9/ready"
    w.queue_count_key = "count"
    calls = {"n": 0}

    def flaky(_url, timeout=1.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("connection reset by peer")
        return {"count": 2}

    monkeypatch.setattr(roster_mod, "_http_get_json", flaky)
    monkeypatch.setattr(roster_mod, "_BOARD_PROBE_RETRY_BACKOFF_SECS", 0)
    assert _worker_queue(w) == "2"
    assert calls["n"] == 2


def test_worker_queue_question_after_connection_failures(tmp_path, monkeypatch):
    """wf-147: two connection failures → '?' (display degrades, no crash)."""
    from workforce.api import roster as roster_mod

    w = make_worker(tmp_path)
    w.queue_url = "http://127.0.0.1:9/ready"
    calls = {"n": 0}

    def always_reset(_url, timeout=1.0):
        calls["n"] += 1
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr(roster_mod, "_http_get_json", always_reset)
    monkeypatch.setattr(roster_mod, "_BOARD_PROBE_RETRY_BACKOFF_SECS", 0)
    assert _worker_queue(w) == "?"
    assert calls["n"] == 2


def test_law_stack_walks_conventions(tmp_path):
    stack = _law_stack(make_worker(tmp_path))
    labels = [(e["label"], os.path.basename(e["path"])) for e in stack]
    assert ("city rules", "AGENTS.md") in labels
    assert labels[-2:] == [("contract", "CONTRACT.md"), ("prompt", "prompt.md")]
    hood_agents = [e for e in stack if e["label"] == "neighborhood rules"]
    assert len(hood_agents) == 1 and hood_agents[0]["sha"]


def test_crashed_shift_detected_past_budget_grace():
    text = ("2026-07-14T00:00:00Z START identity=x kind=lane budget_secs=300 "
            "queue=5 dry_run=0\n")  # hours ago, no terminal event
    s = parse_shifts(text)[0]
    assert s["outcome"] == "crashed"
    assert "no terminal event" in s["reason"]


def test_workplaces_grouped_from_roster(tmp_path):
    import json as _json
    from workforce.board import _workplaces
    from workforce.roster import Roster

    w1 = make_worker(tmp_path)
    hood2 = tmp_path / "city" / "hood2"
    hood2.mkdir()
    w2 = Worker(name="y", workdir=str(hood2), contract=str(w1.contract),
                prompt=str(w1.prompt), identity="y", command=["true"],
                queue_url="http://127.0.0.1:9999/api?x=1")
    roster = Roster(workers={"x": w1, "y": w2}, path="test")
    local = tmp_path / "local"
    local.mkdir()
    (local / "platforms.json").write_text(_json.dumps(
        {"platforms": [], "workplace_names": {"hood2": "PublicName"}}))
    groups = _workplaces(roster, str(local),
                         {"x": {"cls": "ok"}, "y": {"cls": "err"}},
                         {"x": "3", "y": "?"})
    by_label = {g["label"]: g for g in groups}
    assert set(by_label) == {"hood", "PublicName"}
    assert by_label["hood"]["queue"] == 3 and by_label["hood"]["worst"] == "ok"
    assert by_label["PublicName"]["worst"] == "err"
    assert "http://127.0.0.1:9999" in by_label["PublicName"]["desks"]


def test_contract_rules_picks_rule_headings(tmp_path):
    w = make_worker(tmp_path)
    rules = _contract_rules(w.contract)
    titles = [r["title"] for r in rules]
    assert "Lane" in titles and "Never touch" in titles
    assert "Notes" not in titles


def test_render_law_level_badge(tmp_path):
    """Law lens page for contract/prompt shows the L-level badge, not the opaque stackN index.
    Back link is person-scoped (/worker/<name>), not the generic Roster root."""
    import json

    w = make_worker(tmp_path)
    # Roster.json lives at <base>/local/roster.json; base = parent of local_root.
    local = tmp_path / "city" / "local"
    local.mkdir(exist_ok=True)
    (local / "ledger").mkdir(exist_ok=True)
    (local / "roster.json").write_text(json.dumps({"workers": {"x": {
        "workdir": w.workdir, "contract": w.contract, "prompt": w.prompt,
        "identity": "x", "command": ["true"],
    }}}))

    stack = _law_stack(w)
    c_idx = next(i for i, e in enumerate(stack) if e["label"] == "contract")
    p_idx = next(i for i, e in enumerate(stack) if e["label"] == "prompt")

    # Contract paper (L2 in the standard 4-entry stack)
    page = render_law(str(local), "x", "stack%d" % c_idx)
    assert page is not None
    c_badge = "%s · contract" % stack[c_idx]["level"]
    assert c_badge in page          # "L2 · contract" appears in the rendered page
    assert "CONTRACT.md" in page
    assert "/worker/x" in page      # back link is person-scoped

    # Prompt paper (L3)
    page_p = render_law(str(local), "x", "stack%d" % p_idx)
    assert page_p is not None
    p_badge = "%s · prompt" % stack[p_idx]["level"]
    assert p_badge in page_p        # "L3 · prompt" appears
    assert "prompt.md" in page_p
    assert "/worker/x" in page_p

    # Named routes still resolve
    page_named = render_law(str(local), "x", "contract")
    assert page_named is not None
    assert c_badge in page_named


def test_vendor_limit_error_parses_as_distinct_outcome():
    """ERROR with reason 'vendor limit: …' → outcome 'vendor_limit', not 'error'."""
    text = (
        "2026-07-14T03:00:00Z START identity=x kind=lane budget_secs=1500 queue=5 dry_run=0\n"
        '2026-07-14T03:01:00Z ERROR reason="vendor limit: 402 Payment Required" rc=1 on_pass=1\n'
    )
    shifts = parse_shifts(text)
    assert shifts[0]["outcome"] == "vendor_limit"
    assert shifts[0]["reason"].startswith("vendor limit:")


def test_generic_error_stays_error_outcome():
    """ERROR with reason 'agent exit' stays 'error', not 'vendor_limit'."""
    text = (
        "2026-07-14T03:00:00Z START identity=x kind=lane budget_secs=1500 queue=5 dry_run=0\n"
        "2026-07-14T03:01:00Z ERROR reason=\"agent exit\" rc=1 on_pass=1\n"
    )
    shifts = parse_shifts(text)
    assert shifts[0]["outcome"] == "error"


def test_vendor_limit_health_is_amber_not_err(tmp_path):
    """A worker whose last shift was a vendor-limit exit gets amber health, not err."""
    from workforce.board import _worker_health
    from workforce.ledger import Ledger

    w = make_worker(tmp_path)
    ledger_dir = tmp_path / "local" / "ledger"
    ledger_dir.mkdir(parents=True)
    led = Ledger(str(tmp_path / "local" / "ledger"), "x")
    led.append("START", identity="x", kind="lane", budget_secs=1500, queue=5, dry_run=0)
    led.append("ERROR", reason="vendor limit: 402 Payment Required", rc=1, on_pass=1)

    health = _worker_health(str(tmp_path / "local"), w, "5")
    assert health["cls"] == "amber"
    assert "vendor limit" in health["why"]


def test_outcome_cls_maps_vendor_limit_to_amber():
    from workforce.board import OUTCOME_CLS
    assert OUTCOME_CLS.get("vendor_limit") == "amber"
    assert OUTCOME_CLS.get("error") == "err"


def test_worker_health_idle_at_empty_queue_threshold(tmp_path):
    """wf-125: health returns 'idle' cls when empty-queue streak reaches threshold."""
    import json
    from workforce.board import _worker_health
    from workforce.ledger import Ledger

    w = make_worker(tmp_path)
    w.empty_run_threshold = 2
    ledger_dir = tmp_path / "local" / "ledger"
    ledger_dir.mkdir(parents=True)
    led = Ledger(str(ledger_dir), "x")
    # Write 2 consecutive queue-empty SKIPs (= threshold) and the resulting WARN
    led.append("SKIP", reason="queue empty")
    led.append("SKIP", reason="queue empty")
    led.append("WARN", reason="empty-run threshold (2 consecutive queue empty)", streak=2)

    health = _worker_health(str(tmp_path / "local"), w, "0")
    assert health["cls"] == "idle"
    assert "empty-queue streak" in health["why"]


def test_worker_health_ok_below_empty_queue_threshold(tmp_path):
    """wf-125: health stays 'ok' when empty streak is below threshold."""
    from workforce.board import _worker_health
    from workforce.ledger import Ledger

    w = make_worker(tmp_path)
    w.empty_run_threshold = 3
    ledger_dir = tmp_path / "local" / "ledger"
    ledger_dir.mkdir(parents=True)
    led = Ledger(str(ledger_dir), "x")
    led.append("SKIP", reason="queue empty")
    led.append("SKIP", reason="queue empty")
    # Only 2 < threshold=3 → not idle

    health = _worker_health(str(tmp_path / "local"), w, "0")
    assert health["cls"] != "idle"


# ── FLAGS tests ──────────────────────────────────────────────────────

def _flag_worker(tmp_path, queue_url=""):
    w = make_worker(tmp_path)
    w.queue_url = queue_url
    return w


def test_worker_flags_empty_when_no_queue_url(tmp_path):
    w = _flag_worker(tmp_path, queue_url="")
    assert _worker_flags(w) == []


def test_worker_flags_empty_when_label_missing_from_queue_url(tmp_path):
    w = _flag_worker(tmp_path, queue_url="http://127.0.0.1:9999/api?product=workforce")
    assert _worker_flags(w) == []


def test_worker_flags_queries_backlog_and_parked(tmp_path, monkeypatch):
    calls = []

    def fake_desk(path):
        calls.append(path)
        if "status=backlog" in path:
            return {"tasks": [{"id": "wf-60", "title": "flags row",
                               "labels": ["worker:x"]}]}
        if "status=parked" in path:
            return {"tasks": [{"id": "wf-9", "title": "ghost audit",
                               "labels": ["needs:founder-decision", "worker:x"]}]}
        return None

    monkeypatch.setattr(_api_roster, "_desk_json", fake_desk)
    w = _flag_worker(tmp_path,
                     queue_url="http://127.0.0.1:9999/api?product=workforce&label=worker:x")
    flags = _worker_flags(w)

    assert any("backlog" in c and "worker%3Ax" in c for c in calls), calls
    assert any("parked" in c for c in calls), calls
    ids = [f["id"] for f in flags]
    assert "wf-60" in ids and "wf-9" in ids


def test_worker_flags_marks_founder_gated(tmp_path, monkeypatch):
    def fake_desk(path):
        if "status=parked" in path:
            return {"tasks": [{"id": "wf-9", "title": "ghost audit",
                               "labels": ["needs:founder-decision"]}]}
        return {"tasks": []}

    monkeypatch.setattr(_api_roster, "_desk_json", fake_desk)
    w = _flag_worker(tmp_path,
                     queue_url="http://127.0.0.1:9999/api?product=workforce&label=worker:x")
    flags = _worker_flags(w)
    parked = next(f for f in flags if f["id"] == "wf-9")
    assert parked["founder_gated"] is True


def test_worker_flags_non_gated_ticket_not_marked(tmp_path, monkeypatch):
    def fake_desk(path):
        if "status=backlog" in path:
            return {"tasks": [{"id": "wf-60", "title": "flags row",
                               "labels": ["worker:x", "ops"]}]}
        return {"tasks": []}

    monkeypatch.setattr(_api_roster, "_desk_json", fake_desk)
    w = _flag_worker(tmp_path,
                     queue_url="http://127.0.0.1:9999/api?product=workforce&label=worker:x")
    flags = _worker_flags(w)
    assert len(flags) == 1
    assert flags[0]["founder_gated"] is False


def test_worker_model_includes_flags(tmp_path, monkeypatch):
    import json
    import workforce.board as board_mod

    hood = tmp_path / "city" / "hood"
    hood.mkdir(parents=True)
    (tmp_path / "city" / "AGENTS.md").write_text("# city\n")
    (hood / "AGENTS.md").write_text("# hood\n")
    contract = hood / "CONTRACT.md"
    contract.write_text("# contract\n")
    prompt = hood / "prompt.md"
    prompt.write_text("brief\n")

    local = tmp_path / "city" / "local"
    local.mkdir(exist_ok=True)
    (local / "ledger").mkdir(exist_ok=True)
    w = Worker(name="x", workdir=str(hood), contract=str(contract),
               prompt=str(prompt), identity="x", command=["true"],
               queue_url="http://127.0.0.1:9999/api?product=workforce&label=worker:x")
    (local / "roster.json").write_text(json.dumps({"workers": {"x": {
        "workdir": str(hood), "contract": str(contract), "prompt": str(prompt),
        "identity": "x", "command": ["true"],
        "queue_url": "http://127.0.0.1:9999/api?product=workforce&label=worker:x",
    }}}))

    monkeypatch.setattr(_api_roster, "_worker_holdings", lambda _w: [])
    monkeypatch.setattr(_api_roster, "_worker_ready_teaser", lambda _w, **_kw: [])
    monkeypatch.setattr(_api_roster, "_worker_flags",
                        lambda _w: [{"id": "wf-60", "title": "flags row",
                                     "status": "backlog", "founder_gated": False,
                                     "labels": [], "priority": 3, "href": ""}])
    monkeypatch.setattr(_api_roster, "_worker_queue", lambda _w: "5")

    model = board_mod.worker_model(str(local), "x")
    assert model is not None
    assert "flags" in model
    assert model["flags"][0]["id"] == "wf-60"


# ── wf-106: /api/workers parallel fan-out + ?light=1 ────────────────────────

def _api_workers_setup(tmp_path):
    """Minimal roster + local dir for /api/workers HTTP tests."""
    import json as _json
    contract = tmp_path / "CONTRACT.md"
    contract.write_text("# c\n")
    prompt_f = tmp_path / "prompt.md"
    prompt_f.write_text("p\n")
    local = tmp_path / "local"
    (local / "ledger").mkdir(parents=True)
    workers_data = {}
    worker_objs = {}
    for name in ("alpha", "beta"):
        wd = tmp_path / name
        wd.mkdir()
        worker_objs[name] = Worker(
            name=name, workdir=str(wd), contract=str(contract),
            prompt=str(prompt_f), identity=name, command=["true"])
        workers_data[name] = {
            "workdir": str(wd), "contract": str(contract),
            "prompt": str(prompt_f), "identity": name, "command": ["true"],
        }
    (local / "roster.json").write_text(_json.dumps({"workers": workers_data}))
    roster = Roster(workers=worker_objs, path="t")
    return local, roster


def test_api_workers_light_skips_probes(tmp_path, monkeypatch):
    """?light=1 must not call _worker_queue; returns queue='—', health='ok',
    last_shift=null for each worker."""
    import json as _json, threading, urllib.request
    import workforce.board as board_mod

    local, roster = _api_workers_setup(tmp_path)
    calls = []

    monkeypatch.setattr(board_mod, "_load_roster", lambda _root: roster)
    monkeypatch.setattr(board_mod, "_worker_queue", lambda w: calls.append(w.name) or "99")

    httpd = board_mod.make_server(port=0, local_root=str(local))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()
    try:
        resp = urllib.request.urlopen(
            "http://127.0.0.1:%d/api/workers?light=1" % port, timeout=5)
        data = _json.loads(resp.read())
    finally:
        httpd.server_close()
        t.join(timeout=3)

    assert calls == [], "light=1 must not probe queues; got calls: %s" % calls
    by_name = {w["name"]: w for w in data["workers"]}
    assert by_name["alpha"]["queue"] == "—"
    assert by_name["alpha"]["health"] == "ok"
    assert by_name["alpha"]["last_shift"] is None
    assert by_name["beta"]["queue"] == "—"


def test_api_workers_no_light_parallel_queue_values(tmp_path, monkeypatch):
    """No ?light — parallel fan-out returns per-worker queue values."""
    import json as _json, threading, urllib.request
    import workforce.board as board_mod

    local, roster = _api_workers_setup(tmp_path)
    queue_map = {"alpha": "3", "beta": "7"}

    monkeypatch.setattr(board_mod, "_load_roster", lambda _root: roster)
    monkeypatch.setattr(board_mod, "_worker_queue", lambda w: queue_map[w.name])

    httpd = board_mod.make_server(port=0, local_root=str(local))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()
    try:
        resp = urllib.request.urlopen(
            "http://127.0.0.1:%d/api/workers" % port, timeout=5)
        data = _json.loads(resp.read())
    finally:
        httpd.server_close()
        t.join(timeout=3)

    by_name = {w["name"]: w for w in data["workers"]}
    assert by_name["alpha"]["queue"] == "3"
    assert by_name["beta"]["queue"] == "7"
    assert by_name["alpha"]["last_shift"] is None   # no ledger entries
    assert "health" in by_name["alpha"]


# ── wf-147: scene latency bounds + client-gone write ──────────────────────


def test_worker_holdings_uses_label_filter_skips_owner_walk(tmp_path, monkeypatch):
    """Labeled queue_url → one list GET per status; no Owner: detail walks."""
    from workforce.api import roster as roster_mod

    w = make_worker(tmp_path)
    w.name = "salem"
    w.identity = "salem"
    w.queue_url = (
        "http://127.0.0.1:8799/api/admin/tasks/ready"
        "?product=workforce&label=worker:salem"
    )
    calls = []

    def fake_desk(path, timeout=5.0):
        calls.append(path)
        if "status=in_progress" in path and "label=" in path:
            return {"tasks": [
                {"id": "wf-147", "title": "scene latency", "status": "in_progress",
                 "priority": 2, "updated_at": "2026-08-03T16:00:00Z"},
            ]}
        return {"tasks": []}

    monkeypatch.setattr(roster_mod, "_desk_json", fake_desk)

    def boom_owner(_tid):
        raise AssertionError("Owner walk must not run for labeled lanes")

    monkeypatch.setattr(roster_mod, "_desk_owner_of", boom_owner)
    held = roster_mod._worker_holdings(w)
    assert [h["id"] for h in held] == ["wf-147"]
    assert all("label=worker%3Asalem" in c or "label=worker:salem" in c
               for c in calls if "status=" in c)
    assert not any("/api/admin/tasks/wf-" in c for c in calls)


def test_client_gone_write_swallows_broken_pipe(tmp_path):
    """wf-147: client disconnect mid-write must not raise."""
    import workforce.board as board_mod

    class _Gone:
        def write(self, _data):
            raise BrokenPipeError(32, "Broken pipe")

    h = board_mod._Handler.__new__(board_mod._Handler)
    h.wfile = _Gone()
    h.path = "/api/scene?light=0"
    # must not raise
    h._client_gone_write(b'{"ok":true}')

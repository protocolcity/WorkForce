"""The dispatch scene — scene_model facts + render_scene skeleton.

Token-free: scene_model is a pure read model over roster/heartbeat/ledger;
render_scene is a static string. The per-second animation lives in the
browser (setInterval) and is out of scope for a server-side test.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import board  # noqa: E402
import workforce.api.roster as _api_roster  # noqa: E402
from workforce.roster import Roster, Worker  # noqa: E402


def _local(tmp_path):
    local = tmp_path / "local"
    (local / "ledger").mkdir(parents=True)
    return local


def _worker(tmp_path, name, workdir, **kw):
    contract = tmp_path / "CONTRACT.md"
    contract.write_text("# c\n")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p\n")
    wd = tmp_path / workdir
    wd.mkdir(exist_ok=True)
    return Worker(name=name, workdir=str(wd), contract=str(contract),
                  prompt=str(prompt), identity=name, command=["true"], **kw)


def _patch_roster(monkeypatch, roster):
    monkeypatch.setattr(_api_roster, "_load_roster", lambda _root: roster)
    # keep the model network-free and deterministic in the test
    monkeypatch.setattr(_api_roster, "_worker_queue", lambda w: "0")


def test_scene_model_groups_workers_into_workplace_sectors(tmp_path, monkeypatch):
    local = _local(tmp_path)
    w1 = _worker(tmp_path, "alpha", "hoodA")
    w2 = _worker(tmp_path, "beta", "hoodB")
    w3 = _worker(tmp_path, "gamma", "hoodA")  # shares hoodA with alpha
    roster = Roster(workers={"alpha": w1, "beta": w2, "gamma": w3}, path="t")
    _patch_roster(monkeypatch, roster)

    model = board.scene_model(str(local))

    assert model["daemon"] == "stopped"          # no heartbeat on a fresh local
    assert model["generated_at"].endswith("Z")
    by_place = {s["workplace"]: s for s in model["sectors"]}
    # Citizen "You" card is always prepended; workplaces group by workdir.
    assert "You" in by_place
    assert set(by_place) >= {"You", "hoodA", "hoodB"}
    assert {w["name"] for w in by_place["hoodA"]["workers"]} == {"alpha", "gamma"}
    assert [w["name"] for w in by_place["hoodB"]["workers"]] == ["beta"]
    # every worker carries the raw facts the scene JS renders
    a = by_place["hoodA"]["workers"][0]
    for key in ("name", "kind", "display", "cli", "model", "schedule", "owned",
                "next_fire", "queue", "health", "last_shift", "holding"):
        assert key in a
    assert a["cli"] == "true"  # command=["true"] fixture
    assert a["holding"] == []  # not in_flight


def test_scene_model_exposes_display_when_set(tmp_path, monkeypatch):
    local = _local(tmp_path)
    w = _worker(tmp_path, "otto", "hoodO",
                display="Otto · Systems Engineer", succeeds="claude-workforce")
    roster = Roster(workers={"otto": w}, path="t")
    _patch_roster(monkeypatch, roster)
    workers = {row["name"]: row
               for s in board.scene_model(str(local))["sectors"]
               for row in s["workers"]}
    assert workers["otto"]["display"] == "Otto · Systems Engineer"


def test_worker_model_exposes_succeeds_for_personnel_drawer(tmp_path, monkeypatch):
    local = _local(tmp_path)
    w = _worker(tmp_path, "otto", "hoodO",
                display="Otto · Systems Engineer", succeeds="claude-workforce")
    roster = Roster(workers={"otto": w}, path="t")
    _patch_roster(monkeypatch, roster)
    model = board.worker_model(str(local), "otto")
    assert model is not None
    assert model["display"] == "Otto · Systems Engineer"
    assert model["succeeds"] == "claude-workforce"


def test_bay_js_prefers_display_over_name():
    """SCENE_JS bay() label: display when set, else name."""
    assert "w.display" in board.SCENE_JS
    assert "succeeds " in board.SCENE_JS


def test_bay_js_exposes_payroll_and_live_claim_and_paused():
    """Floor-watch slice: model/CLI subtitle, claim teaser, PAUSED chip."""
    js = board.SCENE_JS
    assert "function payrollLine" in js
    assert "bay-pay" in js
    assert "claimHref" in js
    assert "function isPaused" in js
    assert '"PAUSED"' in js or "return \"PAUSED\"" in js


def test_scene_model_cli_from_command_path(tmp_path, monkeypatch):
    local = _local(tmp_path)
    w = _worker(tmp_path, "kai", "hoodK")
    w.command = ["/tmp/fixture/.grok/bin/grok", "--model", "{model}"]
    w.model = "grok-4.5"
    roster = Roster(workers={"kai": w}, path="t")
    _patch_roster(monkeypatch, roster)
    workers = {row["name"]: row
               for s in board.scene_model(str(local))["sectors"]
               for row in s["workers"]}
    assert workers["kai"]["cli"] == "grok"
    assert workers["kai"]["model"] == "grok-4.5"


def test_scene_model_holding_teaser_only_when_in_flight(tmp_path, monkeypatch):
    local = _local(tmp_path)
    active = _worker(tmp_path, "morgan", "hoodM")
    idle = _worker(tmp_path, "riley", "hoodR")
    roster = Roster(workers={"morgan": active, "riley": idle}, path="t")
    _patch_roster(monkeypatch, roster)
    monkeypatch.setattr(_api_roster, "heartbeat_status", lambda _root: "running")
    monkeypatch.setattr(
        _api_roster, "read_heartbeat",
        lambda _root: {"pid": 1, "last_tick": "2026-07-16T12:00:00Z",
                       "state": "scheduling", "in_flight": ["morgan"]})
    calls = []

    def fake_holdings(w):
        calls.append(w.name)
        return [{"id": "ts-1", "title": "Land the bay claim line",
                 "status": "in_progress", "href": "http://desk/t/ts-1"}]

    monkeypatch.setattr(_api_roster, "_worker_holdings", fake_holdings)
    model = board.scene_model(str(local))
    workers = {row["name"]: row
               for s in model["sectors"]
               for row in s["workers"]}
    assert model["in_flight"] == ["morgan"]
    assert calls == ["morgan"]
    assert workers["morgan"]["holding"][0]["id"] == "ts-1"
    assert workers["riley"]["holding"] == []


def test_scene_model_owned_and_next_fire_from_cron(tmp_path, monkeypatch):
    local = _local(tmp_path)
    cronw = _worker(tmp_path, "sched", "hoodC", schedule="*/5 * * * *")
    manualw = _worker(tmp_path, "hand", "hoodD", schedule="manual")
    roster = Roster(workers={"sched": cronw, "hand": manualw}, path="t")
    _patch_roster(monkeypatch, roster)

    workers = {w["name"]: w
               for s in board.scene_model(str(local))["sectors"]
               for w in s["workers"]}
    assert workers["sched"]["owned"] is True and workers["sched"]["next_fire"].endswith("Z")
    assert workers["hand"]["owned"] is False and workers["hand"]["next_fire"] == ""


def test_scene_model_reads_last_real_shift(tmp_path, monkeypatch):
    local = _local(tmp_path)
    w = _worker(tmp_path, "worked", "hoodE")
    roster = Roster(workers={"worked": w}, path="t")
    _patch_roster(monkeypatch, roster)
    ledger = local / "ledger" / "worked.log"
    ledger.write_text(
        "2026-07-14T02:10:00Z START identity=x kind=lane model=m budget_secs=1500 "
        "queue=1 contract_sha=aa prompt_sha=bb dry_run=0\n"
        "2026-07-14T02:15:02Z DONE rc=0\n"
        "2026-07-14T02:15:02Z STOP reason=\"single-pass complete\"\n")

    workers = {w["name"]: w
               for s in board.scene_model(str(local))["sectors"]
               for w in s["workers"]}
    w0 = workers["worked"]
    assert w0["last_shift"]["outcome"] == "ok"
    assert w0["last_shift"]["ts"].startswith("2026-07-14")


def test_scene_model_reports_draining(tmp_path, monkeypatch):
    import datetime
    local = _local(tmp_path)
    roster = Roster(workers={}, path="t")
    _patch_roster(monkeypatch, roster)
    from workforce import daemon as _d
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (local / _d.HEARTBEAT).write_text(json.dumps({
        "pid": os.getpid(), "last_tick": now, "state": "draining",
        "in_flight": ["alpha", "beta"]}))

    model = board.scene_model(str(local))
    assert model["daemon"] == "draining"
    assert model["in_flight"] == ["alpha", "beta"]


def _feed(entries):
    return {"entries": entries}


def test_scene_tape_keeps_only_closed_status_changes(tmp_path, monkeypatch):
    local = _local(tmp_path)
    # A realistic /api/dev/activity mix: comments, non-terminal transitions,
    # and terminal closures. Only done|canceled status_changes are traffic.
    entries = [
        {"entry_type": "comment", "task_id": "oc-9", "task_title": "drain",
         "new_status": "", "created_at": "2026-07-14T17:00:00Z", "body": "wip"},
        {"entry_type": "status_change", "task_id": "oc-9", "task_title": "drain",
         "new_status": "in_progress", "created_at": "2026-07-14T17:01:00Z"},
        {"entry_type": "status_change", "task_id": "oc-11", "task_title": "the floor",
         "new_status": "done", "created_at": "2026-07-14T17:05:00Z"},
        {"entry_type": "status_change", "task_id": "oc-8", "task_title": "dead idea",
         "new_status": "canceled", "created_at": "2026-07-14T17:06:00Z"},
    ]
    monkeypatch.setattr(_api_roster, "_desk_json", lambda path: _feed(entries))

    tape = board.scene_tape(str(local))

    assert tape["desk_ok"] is True
    assert tape["generated_at"].endswith("Z")
    assert tape["desk"] == board.DESK          # the config seam, for links
    ids = [(c["task_id"], c["status"]) for c in tape["closed"]]
    assert ids == [("oc-11", "done"), ("oc-8", "canceled")]
    assert tape["closed"][0]["title"] == "the floor"
    assert tape["closed"][0]["ts"] == "2026-07-14T17:05:00Z"


def test_scene_tape_degrades_when_desk_unreachable(tmp_path, monkeypatch):
    local = _local(tmp_path)
    monkeypatch.setattr(_api_roster, "_desk_json", lambda path: None)  # desk down

    tape = board.scene_tape(str(local))

    assert tape["desk_ok"] is False
    assert tape["closed"] == []
    assert tape["generated_at"].endswith("Z")  # still a well-formed frame


def test_scene_tape_hits_the_activity_feed(tmp_path, monkeypatch):
    local = _local(tmp_path)
    seen = {}

    def _spy(path):
        seen["path"] = path
        return _feed([])

    monkeypatch.setattr(_api_roster, "_desk_json", _spy)
    board.scene_tape(str(local))
    # reuses render_board's desk proxy path; host stays in DESK config
    assert seen["path"].startswith("/api/dev/activity")


def test_render_scene_keeps_desk_traffic_off_d0(tmp_path, monkeypatch):
    """Suite boundary: closures live on Desk, not Roster home."""
    local = _local(tmp_path)
    _patch_roster(monkeypatch, Roster(workers={}, path="t"))
    html = board.render_scene(str(local))
    assert "id='tape'" not in html
    assert "pollTape" not in html
    assert "TRAFFIC · 24H" not in html
    assert "/api/scene-tape" not in html
    # endpoint may remain for benches; D0 must not poll it
    assert "/api/city" not in html


def test_worker_model_feeds_the_personnel_drawer(tmp_path, monkeypatch):
    local = _local(tmp_path)
    w = _worker(tmp_path, "delta", "hoodF", schedule="*/10 * * * *")
    roster = Roster(workers={"delta": w}, path="t")
    _patch_roster(monkeypatch, roster)

    m = board.worker_model(str(local), "delta")
    assert m["name"] == "delta" and m["owned"] is True
    assert m["next_fire"].endswith("Z")
    # the law stack carries levels and lens hrefs, never raw paths
    assert m["law"][0]["level"] == "L0"
    assert any(e["label"] == "contract" for e in m["law"])
    for e in m["law"]:
        assert "path" not in e
        if e["sha"]:
            assert e["href"].startswith("/law/delta/stack")
    assert m["shifts"] == []
    # unknown workers have no file
    assert board.worker_model(str(local), "nobody") is None


def test_render_scene_wires_the_personnel_drawer(tmp_path, monkeypatch):
    local = _local(tmp_path)
    _patch_roster(monkeypatch, Roster(workers={}, path="t"))
    html = board.render_scene(str(local))
    for marker in ("id='pf'", "id='pfscrim'", "openPF", "/api/worker/",
                   "PERSONNEL FILE"):
        assert marker in html


def test_render_scene_wires_set_count_focus_and_pf_deep_links(tmp_path, monkeypatch):
    """wf-52: set-counts are hits; ?focus= highlights floor; ?pf= opens PF."""
    local = _local(tmp_path)
    _patch_roster(monkeypatch, Roster(workers={}, path="t"))
    html = board.render_scene(str(local))
    # Q= / stuck set-counts → Desk filter or floor focus (Click ladder)
    for marker in ("function deskFilterHref", "function sectorMetaHtml",
                   "q-hit", 'data-focus="stuck"', "admin/desk"):
        assert marker in html
    # Floor focus: query param, apply, and tick() must not wipe marks
    for marker in ("function focusParams", "function applyFloorFocus",
                   "function setFloorFocus", "focus-dim", "focus-hit",
                   "keepDim", "keepHit",
                   "?focus=stuck|quiet|ready|next"):
        assert marker in html
    # Office / external deep-link: ?pf=<name> opens the summary drawer
    for marker in ("function maybeOpenPfFromQuery", 'q.get("pf")',
                   "openPF(pf)", 'href^="/worker/"'):
        assert marker in html
    # Bay cards expose stuck/ready for focus matching
    for marker in ('data-stuck="', 'data-ready="', "bayMatchesFocus"):
        assert marker in html


def test_render_scene_is_self_contained_skeleton(tmp_path, monkeypatch):
    local = _local(tmp_path)
    _patch_roster(monkeypatch, Roster(workers={}, path="t"))
    html = board.render_scene(str(local))
    # the scene polls the board's OWN facts, not the city lens
    assert "/api/scene" in html and "/api/city" not in html
    # ratified D0 anatomy + perimeter lock
    for marker in ("masthead", "CARRIER", "wallTime",
                   "class='crt'", "id='grid'", "above-floor",
                   "minmax(0,1fr)", "suite-doors"):
        assert marker in html
    # roster read: cabinet-first groups + status / working-on / next / dispatch
    # + permanent strip / hired columns / hire drawer / cabinet filter rail
    for marker in ("sectorSlug", 'id="sector-', "sector-bldg", "#e2d9c2",
                   "workerState", "statusLabel", "workingOn", "sectorEyebrow",
                   "Dispatch", "ON SHIFT", "permanent-strip", "hired-floor",
                   "id='permanent'", "renderPermanentStrip",
                   "hire-btn", "openHire", "/api/hire",
                   "id='cabinetRail'", "cabinet-rail", "renderCabinetRail",
                   "applyCabinetFilter", "cab-chip"):
        assert marker in html
    assert "Clock in" not in html
    assert "HIRED" not in html.split("function sectorTitle")[1].split("function ")[0]
    assert "id='floorStrip'" not in html
    assert "renderFloorStrip" not in html
    # cron speech stays for the personnel drawer, not the bay face
    for marker in ("function cronSpeech", "hourly at :45", "_CRON_SPEECH_PIN"):
        assert marker in html
    assert "setInterval" in html and "requestAnimationFrame" not in html
    assert "href='/board'" in html
    assert "href='/report'" in html
    assert "var CAN_DISPATCH=false" in html
    assert "var CAN_DISPATCH=true" in board.render_scene("local", can_dispatch=True)


# ── the report: the floor's strategic view ──────────────────────


def test_report_model_verdicts_quiet_and_fires(tmp_path, monkeypatch):
    import datetime as _dt
    local = _local(tmp_path)
    fresh = _worker(tmp_path, "fresh", "hoodR", schedule="*/5 * * * *")
    idle = _worker(tmp_path, "idle", "hoodR")  # manual, never dispatched
    roster = Roster(workers={"fresh": fresh, "idle": idle}, path="t")
    _patch_roster(monkeypatch, roster)
    monkeypatch.setattr(_api_roster, "_desk_json", lambda _p: None)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (local / "ledger" / "fresh.log").write_text(
        "{0} START identity=x kind=lane model=m budget_secs=1500 "
        "queue=1 contract_sha=aa prompt_sha=bb dry_run=0\n"
        "{0} DONE rc=0\n"
        "{0} STOP reason=\"single-pass complete\"\n".format(ts))

    m = board.report_model(str(local), days=7)

    by = {w["name"]: w for w in m["workers"]}
    assert by["fresh"]["verdict"] == "steady"
    assert by["fresh"]["ok"] == 1 and by["fresh"]["total"] == 1
    assert by["idle"]["verdict"] == "off rota"
    # the quiet list carries the never-fired worker, not the fresh one
    assert {e["name"] for e in m["quiet"]} == {"idle"}
    assert [f["name"] for f in m["next_fires"]] == ["fresh"]
    # desk down degrades, never raises — the board holds its own facts
    assert m["desk"]["ok"] is False
    assert m["window_days"] == 7


def test_report_model_joins_desk_authors_by_identity(tmp_path, monkeypatch):
    local = _local(tmp_path)
    w = _worker(tmp_path, "claude-hood", "hoodJ")
    _patch_roster(monkeypatch, Roster(workers={"claude-hood": w}, path="t"))
    monkeypatch.setattr(_api_roster, "_desk_json", lambda _p: {
        "ok": True, "window_days": 7,
        "authors": [{"author": "claude-hood", "filed": 3, "closed": 5},
                    {"author": "owner-terminal", "filed": 9, "closed": 2}],
        "lanes": [{"lane": "claude-hood", "filed": 4, "closed": 4}]})

    m = board.report_model(str(local), days=7)

    assert m["desk"]["ok"] is True
    by = {a["author"]: a for a in m["desk"]["authors"]}
    assert by["claude-hood"]["worker"] == "claude-hood"   # identity join
    assert by["owner-terminal"]["worker"] == ""         # signer, not employed
    assert m["desk"]["lanes"][0]["lane"] == "claude-hood"


def test_render_report_is_a_bay_of_the_floor():
    html = board.render_report("local", days=14)
    for marker in ("verdictStrip", "thruRows", "quietStamp", "fireList",
                   "back to Roster", "href='/board'",
                   "/api/report?days=14", "var WINDOW_DAYS=14"):
        assert marker in html
    # setInterval, never requestAnimationFrame (the proven constraint)
    assert "setInterval" in html and "requestAnimationFrame" not in html


def test_scene_footer_is_room_verbs_only(tmp_path, monkeypatch):
    local = _local(tmp_path)
    _patch_roster(monkeypatch, Roster(workers={}, path="t"))
    html = board.render_scene(str(local))
    # D0 footer: in-room verbs + census — no poll chrome, no desk traffic
    assert "foot-verbs" in html
    assert "href='/report'>Overview</a>" in html
    assert "href='/board'" in html
    assert "id='poll'" not in html
    assert "id='sum'" in html
    assert "report sheet" not in html


def test_days_param_parses_and_survives_junk():
    assert board._days_param("/report?days=30") == 30
    assert board._days_param("/api/report?days=abc") is None
    assert board._days_param("/report") is None


# ── wf-84: light scene schema contract + generation token ─────────────────


def test_light_scene_schema_sentinels(tmp_path, monkeypatch):
    """light=1 payload uses fixed sentinels; stable fields still present."""
    local = _local(tmp_path)
    w = _worker(tmp_path, "kai", "hoodK", schedule="*/5 * * * *")
    roster = Roster(workers={"kai": w}, path="t")
    _patch_roster(monkeypatch, roster)

    model = _api_roster.scene_model(str(local), light=True)

    assert model["light"] is True
    assert model["services"] == []
    assert model["runtimes"] == {"detected": [], "pool": []}

    workers = {row["name"]: row
               for s in model["sectors"]
               for row in s["workers"]}
    k = workers["kai"]
    # stripped fields → sentinels (no null-guards needed in suite consumers)
    assert k["cli"] == ""
    assert k["queue"] == "—"
    assert k["health"] == "ok"
    assert k["why"] == "light"
    assert k["holding"] == []
    assert k["last_shift"] is None
    # stable fields still present and typed
    for field in ("name", "kind", "display", "model", "schedule",
                  "owned", "owner", "skill", "next_fire"):
        assert field in k, "missing stable field: %s" % field
    assert k["name"] == "kai"
    assert k["owned"] is True


def test_generation_token_moves_on_roster_change(tmp_path):
    """Token must differ after roster.json is written (hire/fire path)."""
    local = tmp_path / "local"
    (local / "ledger").mkdir(parents=True)
    roster_file = local / "roster.json"
    roster_file.write_text('{"workers": {}}')

    t1 = _api_roster.generation_token(str(local))["token"]

    import time
    time.sleep(0.01)
    roster_file.write_text('{"workers": {"otto": {}}}')

    t2 = _api_roster.generation_token(str(local))["token"]
    assert t1 != t2, "token did not change after roster.json rewrite"


def test_generation_token_moves_on_in_flight_change(tmp_path):
    """Token must differ when the in_flight list changes (daemon dispatch)."""
    import json as _json
    local = tmp_path / "local"
    (local / "ledger").mkdir(parents=True)

    hb_file = local / "daemon.json"
    hb_file.write_text(_json.dumps({"pid": 1, "last_tick": "2026-07-26T00:00:00Z",
                                    "state": "scheduling", "in_flight": []}))

    t1 = _api_roster.generation_token(str(local))["token"]

    import time
    time.sleep(0.01)
    hb_file.write_text(_json.dumps({"pid": 1, "last_tick": "2026-07-26T00:00:01Z",
                                    "state": "scheduling", "in_flight": ["otto"]}))

    t2 = _api_roster.generation_token(str(local))["token"]
    assert t1 != t2, "token did not change when in_flight grew"


def test_generation_token_in_flight_field_mirrors_heartbeat(tmp_path):
    """The token response carries the current in_flight list for suite Map."""
    import json as _json
    local = tmp_path / "local"
    (local / "ledger").mkdir(parents=True)
    hb_file = local / "daemon.json"
    hb_file.write_text(_json.dumps({"pid": 1, "last_tick": "2026-07-26T00:00:00Z",
                                    "state": "scheduling", "in_flight": ["kai", "morgan"]}))

    result = _api_roster.generation_token(str(local))
    assert sorted(result["in_flight"]) == ["kai", "morgan"]
    assert result["daemon"] in ("running", "stopped", "draining")

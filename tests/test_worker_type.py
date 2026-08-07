"""wf-154 — citizen three-tier taxonomy: derived worker_type, /api/workers
exposure, hire type= alias. Wire values stay kind=lane|job; type is never
persisted."""

import json
import threading
import urllib.request

import pytest

from workforce.hire import resolve_type_alias
from workforce.roster import Roster, RosterError, Worker, WORKER_TYPES


def _worker(tmp_path, name="w", **kw):
    contract = tmp_path / "CONTRACT.md"
    contract.write_text("# c\n")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p\n")
    wd = tmp_path / name
    wd.mkdir(exist_ok=True)
    base = dict(
        name=name, workdir=str(wd), contract=str(contract),
        prompt=str(prompt), identity=name, command=["true"])
    base.update(kw)
    return Worker(**base)


# ── derivation matrix ───────────────────────────────────────────────────────

def test_lane_is_agent(tmp_path):
    assert _worker(tmp_path, kind="lane").worker_type == "agent"


def test_job_with_staff_is_staff(tmp_path):
    assert _worker(tmp_path, kind="job", staff=True).worker_type == "staff"


def test_job_without_staff_is_job(tmp_path):
    assert _worker(tmp_path, kind="job", staff=False).worker_type == "job"


def test_lane_ignores_staff_flag(tmp_path):
    # Legacy oddity: a lane wearing staff=true still reads as agent —
    # kind is authoritative for the claims-vs-observes split.
    assert _worker(tmp_path, kind="lane", staff=True).worker_type == "agent"


def test_worker_types_constant_matches_derivation():
    assert WORKER_TYPES == ("agent", "staff", "job")


# ── hire type= alias ────────────────────────────────────────────────────────

def test_alias_empty_passthrough():
    assert resolve_type_alias("", "lane", None) == ("lane", None)
    assert resolve_type_alias("", "job", True) == ("job", True)


def test_alias_agent_maps_to_lane():
    assert resolve_type_alias("agent", "lane", None) == ("lane", False)


def test_alias_staff_maps_to_job_staff():
    assert resolve_type_alias("staff", "lane", None) == ("job", True)


def test_alias_job_maps_to_job_no_staff():
    assert resolve_type_alias("job", "lane", None) == ("job", False)


def test_alias_unknown_type_raises():
    with pytest.raises(RosterError):
        resolve_type_alias("plumbing", "lane", None)


def test_alias_agent_conflicts_with_explicit_kind_job():
    with pytest.raises(RosterError):
        resolve_type_alias("agent", "job", None)


def test_alias_staff_conflicts_with_explicit_no_staff():
    with pytest.raises(RosterError):
        resolve_type_alias("staff", "lane", False)


def test_alias_job_conflicts_with_explicit_staff_true():
    with pytest.raises(RosterError):
        resolve_type_alias("job", "job", True)


# ── /api/workers exposure ───────────────────────────────────────────────────

def test_api_workers_carries_type_and_staff(tmp_path, monkeypatch):
    import workforce.board as board_mod

    local = tmp_path / "local"
    (local / "ledger").mkdir(parents=True)
    workers = {
        "hand": _worker(tmp_path, "hand", kind="lane"),
        "seat": _worker(tmp_path, "seat", kind="job", staff=True),
        "pipe": _worker(tmp_path, "pipe", kind="job"),
    }
    roster = Roster(workers=workers, path="t")
    monkeypatch.setattr(board_mod, "_load_roster", lambda _root: roster)

    httpd = board_mod.make_server(port=0, local_root=str(local))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()
    try:
        resp = urllib.request.urlopen(
            "http://127.0.0.1:%d/api/workers?light=1" % port, timeout=5)
        data = json.loads(resp.read())
    finally:
        httpd.server_close()
        t.join(timeout=3)

    by = {w["name"]: w for w in data["workers"]}
    assert by["hand"]["type"] == "agent" and by["hand"]["staff"] is False
    assert by["seat"]["type"] == "staff" and by["seat"]["staff"] is True
    assert by["pipe"]["type"] == "job" and by["pipe"]["staff"] is False


# ── hire end-to-end ─────────────────────────────────────────────────────────

def test_hire_type_staff_end_to_end(tmp_path):
    from workforce.hire import hire

    wd = tmp_path / "city" / ".protocolcity" / "ops"
    wd.mkdir(parents=True)
    result = hire(
        name="papers-sync", workdir=str(wd), role="papers sync",
        worker_type="staff", schedule="0 8 * * *", command=["true"],
        roster_path=str(tmp_path / "roster.json"), base=str(tmp_path),
    )
    assert result["worker"]["kind"] == "job"
    assert result["worker"]["staff"] is True
    assert result["worker"]["type"] == "staff"


def test_hire_type_agent_outside_ops_end_to_end(tmp_path):
    from workforce.hire import hire

    wd = tmp_path / "proj"
    wd.mkdir()
    result = hire(
        name="rex", workdir=str(wd), role="Builder",
        worker_type="agent", command=["true"],
        queue_url="http://127.0.0.1:1/api?label=worker:rex",
        roster_path=str(tmp_path / "roster.json"), base=str(tmp_path),
    )
    assert result["worker"]["kind"] == "lane"
    assert result["worker"]["staff"] is False
    assert result["worker"]["type"] == "agent"

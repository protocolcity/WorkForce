"""Mode B workforce repin — stage, apply, .bak, hermetic drop."""

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import capacity  # noqa: E402
from workforce import capacity_policy as cp  # noqa: E402
from workforce import cli  # noqa: E402
from workforce import repin  # noqa: E402
from workforce.roster import Worker, load as load_roster  # noqa: E402


def _policy_dict(**overrides):
    base = {
        "schema": cp.SCHEMA_ID,
        "seats_per_day": 10,
        "cooldown_hours": 0,  # default off in tests; set explicitly for cooldown cases
        "allowed_fields": ["model", "command"],
        "pin_pairs": [
            {
                "id": "generalist-claude-grok",
                "tier": "generalist",
                "a": {"model": "claude-sonnet-4-6", "runtime": "claude"},
                "b": {"model": "grok-4.5", "runtime": "grok"},
                "bidirectional": True,
            }
        ],
        "stay_pinned_tiers": ["heavy_multipass"],
    }
    base.update(overrides)
    return base


def _write_policy(path, **overrides):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_policy_dict(**overrides)), encoding="utf-8")
    return str(path)


def _write_roster(path, workers_spec):
    path.parent.mkdir(parents=True, exist_ok=True)
    workers = {}
    for name, spec in workers_spec.items():
        row = {
            "kind": "lane",
            "workdir": str(path.parent),
            "contract": str(path.parent / ("c-%s.md" % name)),
            "prompt": str(path.parent / ("p-%s.md" % name)),
            "identity": name,
            "command": ["claude", "--model", "{model}", "-p", "{prompt_text}"],
            "model": "claude-sonnet-4-6",
        }
        row.update(spec)
        workers[name] = row
        # touch paper paths so nothing assumes missing files at load
        (path.parent / ("c-%s.md" % name)).write_text("# c\n")
        (path.parent / ("p-%s.md" % name)).write_text("# p\n")
    path.write_text(json.dumps({"workers": workers}, indent=2) + "\n")
    return str(path)


class _FakeRoster:
    def __init__(self, workers, path=""):
        self.workers = workers
        self.path = path

    def worker(self, name):
        try:
            return self.workers[name]
        except KeyError:
            from workforce.roster import RosterError
            raise RosterError("no worker %r" % name)


def _worker(name, model="claude-sonnet-4-6", command=None):
    return Worker(
        name=name,
        workdir="/tmp",
        contract="/tmp/C.md",
        prompt="/tmp/p.md",
        identity=name,
        command=command or ["claude", "--model", "{model}", "-p", "{prompt_text}"],
        kind="lane",
        model=model,
    )


def test_plan_worker_fields_model_and_command():
    policy = cp.validate_policy_dict(_policy_dict())
    w = _worker("blossom")
    fields = repin.plan_worker_fields(w, "grok-4.5", policy)
    assert fields["model"]["from"] == "claude-sonnet-4-6"
    assert fields["model"]["to"] == "grok-4.5"
    assert "command" in fields
    assert fields["command"]["to"][0] == "grok"


def test_plan_refuses_disallowed_transition():
    policy = cp.validate_policy_dict(_policy_dict())
    w = _worker("blossom")
    with pytest.raises(repin.RepinError, match="not allowed"):
        repin.plan_worker_fields(w, "claude-haiku-4-5-20251001", policy)


def test_stage_writes_diff_not_roster(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    policy_path = _write_policy(local / "capacity_policy.json")
    roster_path = _write_roster(
        local / "roster.json",
        {"blossom": {}},
    )
    before = (local / "roster.json").read_text()
    roster = load_roster(path=roster_path, base=str(tmp_path))
    result = repin.stage_repin(
        roster,
        str(local),
        ["blossom"],
        "grok-4.5",
        policy_path=policy_path,
        reason="capacity restore test",
        created_by="salem",
        enforce_caps=True,
    )
    assert result["ok"] is True
    assert os.path.isfile(result["path"])
    assert "roster-diff-" in result["path"]
    # Live roster untouched
    assert (local / "roster.json").read_text() == before
    diff = json.loads(open(result["path"]).read())
    assert diff["schema"] == repin.SCHEMA_ID
    assert diff["mode"] == "B"
    assert diff["created_by"] == "salem"
    assert diff["changes"][0]["worker"] == "blossom"
    assert diff["changes"][0]["fields"]["model"]["to"] == "grok-4.5"


def test_apply_writes_bak_and_merges(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    policy_path = _write_policy(local / "capacity_policy.json")
    roster_path = _write_roster(
        local / "roster.json",
        {"blossom": {}},
    )
    roster = load_roster(path=roster_path, base=str(tmp_path))
    stage = repin.stage_repin(
        roster, str(local), ["blossom"], "grok-4.5",
        policy_path=policy_path, enforce_caps=False,
    )
    applied = repin.apply_repin(
        stage["path"],
        roster_path=roster_path,
        local_root=str(local),
        policy_path=policy_path,
        base=str(tmp_path),
    )
    assert applied["ok"] is True
    assert os.path.isfile(applied["bak_path"])
    assert "bak-repin-" in applied["bak_path"]
    raw = json.loads(open(roster_path).read())
    assert raw["workers"]["blossom"]["model"] == "grok-4.5"
    assert raw["workers"]["blossom"]["command"][0] == "grok"
    # bak holds prior pin
    bak = json.loads(open(applied["bak_path"]).read())
    assert bak["workers"]["blossom"]["model"] == "claude-sonnet-4-6"
    # staged file moved under applied/
    assert "applied" in applied["applied_diff_path"]
    assert not os.path.isfile(stage["path"])


def test_apply_refuses_stale_from(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    policy_path = _write_policy(local / "capacity_policy.json")
    roster_path = _write_roster(
        local / "roster.json",
        {"blossom": {}},
    )
    roster = load_roster(path=roster_path, base=str(tmp_path))
    stage = repin.stage_repin(
        roster, str(local), ["blossom"], "grok-4.5",
        policy_path=policy_path, enforce_caps=False,
    )
    # Mutate live model so staged from no longer matches
    raw = json.loads(open(roster_path).read())
    raw["workers"]["blossom"]["model"] = "something-else"
    open(roster_path, "w").write(json.dumps(raw, indent=2) + "\n")
    with pytest.raises(repin.RepinError, match="stale"):
        repin.apply_repin(
            stage["path"],
            roster_path=roster_path,
            local_root=str(local),
            policy_path=policy_path,
            base=str(tmp_path),
        )
    # live roster not overwritten with partial apply
    live = json.loads(open(roster_path).read())
    assert live["workers"]["blossom"]["model"] == "something-else"


def test_drop_repin_dry_run_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("desk must not be contacted on dry_run")

    monkeypatch.setattr(capacity, "find_open_by_label", boom)
    monkeypatch.setattr(capacity, "_req", boom)
    stage = {
        "path": "/tmp/local/staged/roster-diff-20260803T010203Z.json",
        "stage_id": "20260803T010203Z",
        "day": "2026-08-03",
        "diff": {
            "created_by": "salem",
            "reason": "test",
            "changes": [
                {
                    "worker": "blossom",
                    "fields": {
                        "model": {"from": "claude-sonnet-4-6", "to": "grok-4.5"},
                    },
                }
            ],
        },
    }
    receipt = repin.drop_repin_for_you(stage, dry_run=True)
    assert receipt["ok"] is True
    assert receipt["action"] == "would_create"
    assert "repin-20260803T010203Z" in receipt["label"]


def test_drop_repin_live_refused_under_pytest(monkeypatch):
    """wf-132: dry_run=False still does not touch the desk under pytest."""
    def boom(*a, **k):
        raise AssertionError("desk must not be contacted under pytest")

    monkeypatch.setattr(capacity, "find_open_by_label", boom)
    monkeypatch.setattr(capacity, "_req", boom)
    monkeypatch.delenv("WORKFORCE_ALLOW_DESK", raising=False)
    stage = {
        "path": "/tmp/x.json",
        "stage_id": "20260803T999999Z",
        "day": "2026-08-03",
        "diff": {
            "changes": [
                {
                    "worker": "w",
                    "fields": {"model": {"from": "a", "to": "b"}},
                }
            ],
        },
    }
    receipt = repin.drop_repin_for_you(stage, dry_run=False)
    assert receipt["ok"] is True
    assert receipt["action"] == "would_create"
    assert receipt.get("hermetic") is True


def test_seats_per_day_cap(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    policy_path = _write_policy(
        local / "capacity_policy.json", seats_per_day=1, cooldown_hours=0,
    )
    roster_path = _write_roster(
        local / "roster.json",
        {
            "a": {},
            "b": {},
        },
    )
    roster = load_roster(path=roster_path, base=str(tmp_path))
    repin.stage_repin(
        roster, str(local), ["a"], "grok-4.5",
        policy_path=policy_path, enforce_caps=True,
    )
    with pytest.raises(repin.RepinError, match="seats_per_day"):
        repin.stage_repin(
            roster, str(local), ["b"], "grok-4.5",
            policy_path=policy_path, enforce_caps=True,
        )


def test_cooldown_blocks_restage(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    policy_path = _write_policy(
        local / "capacity_policy.json", seats_per_day=10, cooldown_hours=6,
    )
    roster_path = _write_roster(
        local / "roster.json",
        {"blossom": {}},
    )
    roster = load_roster(path=roster_path, base=str(tmp_path))
    t0 = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.timezone.utc)
    repin.stage_repin(
        roster, str(local), ["blossom"], "grok-4.5",
        policy_path=policy_path, when=t0, enforce_caps=True,
    )
    t1 = t0 + datetime.timedelta(hours=1)
    with pytest.raises(repin.RepinError, match="cooldown"):
        repin.stage_repin(
            roster, str(local), ["blossom"], "grok-4.5",
            policy_path=policy_path, when=t1, enforce_caps=True,
        )


def test_cli_stage_and_apply(tmp_path, capsys, monkeypatch):
    """End-to-end CLI: stage then citizen apply under tmp data dir."""
    local = tmp_path / "local"
    local.mkdir()
    policy_path = _write_policy(local / "capacity_policy.json", cooldown_hours=0)
    roster_path = _write_roster(local / "roster.json", {"blossom": {}})
    monkeypatch.chdir(tmp_path)
    # Stage
    rc = cli.main([
        "--file", roster_path,
        "repin", "blossom", "--to", "grok-4.5",
        "--policy", policy_path,
        "--reason", "cli-test",
        "--created-by", "salem",
        "--no-cap",
    ])
    out = capsys.readouterr()
    assert rc == 0
    assert "repin: staged" in out.out
    assert "dry-run only" in out.out
    # Find staged path
    staged = list((local / "staged").glob("roster-diff-*.json"))
    assert len(staged) == 1
    # Apply
    rc2 = cli.main([
        "--file", roster_path,
        "repin", "--apply", str(staged[0]),
        "--policy", policy_path,
    ])
    out2 = capsys.readouterr()
    assert rc2 == 0
    assert "repin: applied blossom" in out2.out
    raw = json.loads(open(roster_path).read())
    assert raw["workers"]["blossom"]["model"] == "grok-4.5"


def test_mode_a_diff_refused(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    policy_path = _write_policy(local / "capacity_policy.json")
    bad = local / "staged" / "roster-diff-bad.json"
    bad.parent.mkdir(parents=True)
    bad.write_text(json.dumps({
        "schema": repin.SCHEMA_ID,
        "mode": "A",
        "changes": [
            {
                "worker": "x",
                "fields": {
                    "model": {"from": "claude-sonnet-4-6", "to": "grok-4.5"},
                },
            }
        ],
    }))
    with pytest.raises(repin.RepinError, match="Mode A"):
        repin.load_diff(str(bad))


def test_inbox_key_stable():
    assert repin.inbox_key_for_stage("20260803T060551Z") == "repin-20260803T060551Z"
    assert repin.inbox_label("workforce", "20260803T060551Z", "2026-08-03") == (
        "inbox-report:workforce:repin-20260803T060551Z:2026-08-03"
    )

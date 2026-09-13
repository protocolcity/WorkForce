"""Hire write path — papers + roster row (STAFFING §2)."""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from workforce import hire as hire_mod
from workforce._utils import desk_base_url
from workforce.roster import RosterError, load


def test_slugify():
    assert hire_mod.slugify("Neo Market") == "neo-market"
    assert hire_mod.slugify("  Ames  ") == "ames"


def test_hire_rejects_slug_already_on_roster(tmp_path):
    """Staff (or any) worker already on the roster can't be hired again."""
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "office-steward": {
                "staff": True,
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "office-steward",
                "command": ["true"],
            }
        }
    }))
    with pytest.raises(RosterError, match="already on the roster"):
        hire_mod.hire(
            name="office-steward",
            workdir=str(hood),
            role="Steward",
            base=str(tmp_path),
            roster_path=str(roster_path),
            dry_run=True,
        )


def test_hire_rejects_you(tmp_path):
    hood = tmp_path / "hood"
    hood.mkdir()
    with pytest.raises(RosterError, match="permanent"):
        hire_mod.hire(
            name="you",
            workdir=str(hood),
            role="Owner",
            base=str(tmp_path),
            roster_path=str(tmp_path / "local" / "roster.json"),
            dry_run=True,
        )


def test_hire_plants_papers_and_arms_roster(tmp_path):
    hood = tmp_path / "gridfinity"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    # Seed a minimal valid roster so load() after hire succeeds with peers
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")

    result = hire_mod.hire(
        name="Neo",
        workdir=str(hood),
        role="Market Analyst",
        project="gridfinity",
        base=str(tmp_path),
        roster_path=str(roster_path),
        plant=True,
    )
    assert result["ok"] is True
    assert result["armed"] is True
    assert result["worker"]["name"] == "neo"
    assert result["worker"]["display"] == "Neo · Market Analyst"
    contract = hood / "workers" / "neo" / "CONTRACT.md"
    prompt = hood / "workers" / "neo" / "prompt.md"
    assert contract.is_file() and prompt.is_file()
    raw = json.loads(roster_path.read_text())
    assert "neo" in raw["workers"]
    assert raw["workers"]["neo"]["identity"] == "neo"
    qurl = raw["workers"]["neo"]["queue_url"]
    assert "gridfinity" in qurl
    # Exclusive hand feed — product alone is not enough (starves siblings / dual-claims)
    assert "label=worker:neo" in qurl
    assert "product=gridfinity" in qurl
    assert qurl.startswith(
        desk_base_url().rstrip("/") + "/api/admin/tasks/ready"
    )
    body = contract.read_text()
    assert "worker:neo" in body
    assert "lane:neo" not in body
    # Live load still validates
    r = load(path=str(roster_path), base=str(tmp_path))
    assert "neo" in r.workers
    assert any("Desk" in s or "PROCESS" in s for s in result["next_steps"])


def test_hire_dry_run_does_not_write_roster(tmp_path):
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")

    result = hire_mod.hire(
        name="Kai",
        workdir=str(hood),
        role="Patrol",
        base=str(tmp_path),
        roster_path=str(roster_path),
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["armed"] is False
    raw = json.loads(roster_path.read_text())
    assert "kai" not in raw["workers"]
    # Papers still planted so the citizen can review before arming
    assert (hood / "workers" / "kai" / "CONTRACT.md").is_file()


def test_hire_default_queue_url_uses_desk_base_url(tmp_path, monkeypatch):
    """wf-219: default lane feed follows desk_base_url(), not a loopback literal."""
    for key in ("WL_DESK_URL", "TP_DESK_URL", "WORKFORCE_DESK", "WORKFORCE_DESK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WL_DESK_URL", "http://desk.test:9999/")
    hood = tmp_path / "hood"
    hood.mkdir()
    result = hire_mod.hire(
        name="neo",
        workdir=str(hood),
        role="Analyst",
        project="gridfinity",
        base=str(tmp_path),
        roster_path=str(tmp_path / "local" / "roster.json"),
        dry_run=True,
    )
    qurl = result["worker"]["queue_url"]
    assert qurl.startswith("http://desk.test:9999/api/admin/tasks/ready?")
    assert "product=gridfinity" in qurl
    assert "label=worker:neo" in qurl
    assert "127.0.0.1:8799" not in qurl


def test_hire_default_queue_url_fallback_when_desk_env_unset(tmp_path, monkeypatch):
    """wf-219: no desk env → loopback :8799, still exclusive label= form."""
    for key in ("WL_DESK_URL", "TP_DESK_URL", "WORKFORCE_DESK", "WORKFORCE_DESK"):
        monkeypatch.delenv(key, raising=False)
    hood = tmp_path / "hood"
    hood.mkdir()
    result = hire_mod.hire(
        name="neo",
        workdir=str(hood),
        role="Analyst",
        project="workforce",
        base=str(tmp_path),
        roster_path=str(tmp_path / "local" / "roster.json"),
        dry_run=True,
    )
    qurl = result["worker"]["queue_url"]
    assert qurl.startswith(
        "http://127.0.0.1:8799/api/admin/tasks/ready?product=workforce&label=worker:neo"
    )


def test_hire_explicit_queue_url_wins_over_desk_base(tmp_path, monkeypatch):
    """wf-219: caller-supplied queue_url is not rewritten to desk_base_url()."""
    monkeypatch.setenv("WL_DESK_URL", "http://desk.test:9999")
    hood = tmp_path / "hood"
    hood.mkdir()
    given = "http://explicit.test:1/api/admin/tasks/ready?product=foo&label=worker:neo"
    result = hire_mod.hire(
        name="neo",
        workdir=str(hood),
        role="Analyst",
        queue_url=given,
        base=str(tmp_path),
        roster_path=str(tmp_path / "local" / "roster.json"),
        dry_run=True,
    )
    assert result["worker"]["queue_url"] == given


def test_hire_rejects_worker_param_queue_url(tmp_path):
    """`queue_url` with ?worker= (no label=worker:) is rejected at hire time."""
    hood = tmp_path / "hood"
    hood.mkdir()
    with pytest.raises(RosterError, match="worker="):
        hire_mod.hire(
            name="garfield",
            workdir=str(hood),
            role="Patrol",
            queue_url="http://127.0.0.1:8799/api/admin/tasks/ready?product=foo&worker=garfield",
            base=str(tmp_path),
            roster_path=str(tmp_path / "local" / "roster.json"),
            dry_run=True,
        )


def test_hire_rejects_shorthand_model(tmp_path):
    """Shorthand model pins (e.g. claude-sonnet) hard-fail — capacity rails need full ids."""
    hood = tmp_path / "hood"
    hood.mkdir()
    with pytest.raises(RosterError, match="unknown model pin"):
        hire_mod.hire(
            name="Neo",
            workdir=str(hood),
            role="Analyst",
            model="claude-sonnet",
            base=str(tmp_path),
            roster_path=str(tmp_path / "local" / "roster.json"),
            dry_run=True,
        )


def test_hire_accepts_full_model_pin(tmp_path):
    """Full capacity-policy model ids are accepted."""
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")
    result = hire_mod.hire(
        name="Pinok",
        workdir=str(hood),
        role="Analyst",
        model="claude-sonnet-4-6",
        base=str(tmp_path),
        roster_path=str(roster_path),
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["worker"]["model"] == "claude-sonnet-4-6"
    assert "section_52_row" in result
    assert "`pinok`" in result["section_52_row"]


def test_validate_model_pin_empty_is_vendor_default():
    assert hire_mod.validate_model_pin("") == ""
    assert hire_mod.validate_model_pin("  ") == ""
    assert hire_mod.validate_model_pin("default") == ""


def test_hire_lane_defaults_shift_worktree_on(tmp_path):
    """Code lanes default shift_worktree=true so new hires isolate."""
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")
    result = hire_mod.hire(
        name="LaneHand",
        workdir=str(hood),
        role="Builder",
        kind="lane",
        base=str(tmp_path),
        roster_path=str(roster_path),
        plant=True,
    )
    assert result["worker"]["shift_worktree"] is True
    raw = json.loads(roster_path.read_text())
    assert raw["workers"]["lanehand"]["shift_worktree"] is True
    body = (hood / "workers" / "lanehand" / "CONTRACT.md").read_text()
    assert "WORKFORCE_SHIFT_WORKDIR" in body or "shift worktree" in body.lower()


def test_hire_job_defaults_shift_worktree_off(tmp_path):
    """Jobs default shift_worktree off (no isolation cost for patrol seats)."""
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")
    result = hire_mod.hire(
        name="Patrol",
        workdir=str(hood),
        role="Night watch",
        kind="job",
        base=str(tmp_path),
        roster_path=str(roster_path),
        plant=True,
    )
    assert result["worker"]["shift_worktree"] is False
    raw = json.loads(roster_path.read_text())
    # false omitted from roster JSON (older-daemon hygiene)
    assert "shift_worktree" not in raw["workers"]["patrol"]


def test_hire_shift_worktree_explicit_override(tmp_path):
    """--no-shift-worktree / explicit False wins over lane default."""
    hood = tmp_path / "hood"
    hood.mkdir()
    result = hire_mod.hire(
        name="OptOut",
        workdir=str(hood),
        role="Lane",
        kind="lane",
        shift_worktree=False,
        base=str(tmp_path),
        roster_path=str(tmp_path / "local" / "roster.json"),
        dry_run=True,
    )
    assert result["worker"]["shift_worktree"] is False


def test_hire_lane_opt_out_persists_false(tmp_path):
    """wf-153 slice 4: lane opt-out must write false so load cannot re-enable."""
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")
    hire_mod.hire(
        name="OptOut",
        workdir=str(hood),
        role="Lane",
        kind="lane",
        shift_worktree=False,
        base=str(tmp_path),
        roster_path=str(roster_path),
        plant=True,
    )
    raw = json.loads(roster_path.read_text())
    assert raw["workers"]["optout"]["shift_worktree"] is False
    loaded = load(path=str(roster_path), base=str(tmp_path))
    assert loaded.worker("optout").shift_worktree is False


def test_load_lane_absent_shift_worktree_defaults_on(tmp_path):
    """wf-153 slice 4: pre-flag lane rows isolate without citizen rewrite."""
    hood = tmp_path / "hood"
    hood.mkdir()
    (hood / "c.md").write_text("c\n")
    (hood / "p.md").write_text("p\n")
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({
        "workers": {
            "coder": {
                "kind": "lane",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "coder",
                "command": ["true"],
                "queue_url": "http://127.0.0.1:9/ready",
                # shift_worktree key intentionally absent
            },
            "patrol": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "patrol",
                "command": ["true"],
            },
            "optout": {
                "kind": "lane",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "optout",
                "command": ["true"],
                "queue_url": "http://127.0.0.1:9/ready",
                "shift_worktree": False,
            },
        }
    }))
    r = load(path=str(roster_path), base=str(tmp_path))
    assert r.worker("coder").shift_worktree is True
    assert r.worker("patrol").shift_worktree is False
    assert r.worker("optout").shift_worktree is False


def test_hire_lane_defaults_max_passes_zero(tmp_path):
    """wf-174: new lanes hire with max_passes=0 (budget-driven drain)."""
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")
    result = hire_mod.hire(
        name="DrainHand",
        workdir=str(hood),
        role="Builder",
        kind="lane",
        base=str(tmp_path),
        roster_path=str(roster_path),
        plant=True,
    )
    assert result["worker"]["max_passes"] == 0
    raw = json.loads(roster_path.read_text())
    assert raw["workers"]["drainhand"]["max_passes"] == 0
    loaded = load(path=str(roster_path), base=str(tmp_path))
    assert loaded.worker("drainhand").max_passes == 0


def test_load_lane_absent_max_passes_defaults_zero(tmp_path):
    """wf-174: pre-flag lane rows without max_passes key drain by default."""
    hood = tmp_path / "hood"
    hood.mkdir()
    (hood / "c.md").write_text("c\n")
    (hood / "p.md").write_text("p\n")
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({
        "workers": {
            "coder": {
                "kind": "lane",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "coder",
                "command": ["true"],
                "queue_url": "http://127.0.0.1:9/ready",
                # max_passes key intentionally absent
            },
            "patrol": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "patrol",
                "command": ["true"],
            },
            "single": {
                "kind": "lane",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "single",
                "command": ["true"],
                "queue_url": "http://127.0.0.1:9/ready",
                "max_passes": 1,  # explicit single-pass opt-out
            },
        }
    }))
    r = load(path=str(roster_path), base=str(tmp_path))
    assert r.worker("coder").max_passes == 0
    assert r.worker("patrol").max_passes == 1
    assert r.worker("single").max_passes == 1


def test_load_lane_without_queue_absent_max_passes_stays_single(tmp_path):
    """wf-174: queue-less lane cannot default to drain (needs probe)."""
    hood = tmp_path / "hood"
    hood.mkdir()
    (hood / "c.md").write_text("c\n")
    (hood / "p.md").write_text("p\n")
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({
        "workers": {
            "orphan": {
                "kind": "lane",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "orphan",
                "command": ["true"],
                # no queue_url, no max_passes
            },
        }
    }))
    r = load(path=str(roster_path), base=str(tmp_path))
    assert r.worker("orphan").max_passes == 1


def test_hire_emits_section_52_row_when_unregistered(tmp_path, monkeypatch):
    """New identity gets a paste-ready §5.2 row in next_steps."""
    hood = tmp_path / "hood"
    hood.mkdir()
    process = tmp_path / "PROCESS.md"
    process.write_text(
        "# Process\n\n### 5.2) Identity\n\n"
        "| Agent id | Who |\n| --- | --- |\n"
        "| `salem` | Salem · Systems Engineer. |\n\n"
        "### 5.3) Other\n"
    )
    monkeypatch.setenv("WORKLANE_PROCESS", str(process))
    result = hire_mod.hire(
        name="brand-new-hand",
        workdir=str(hood),
        role="Tester",
        base=str(tmp_path),
        roster_path=str(tmp_path / "local" / "roster.json"),
        dry_run=True,
    )
    assert result["identity_registered"] is False
    assert any("`brand-new-hand`" in s for s in result["next_steps"])
    assert result["section_52_row"].startswith("| `brand-new-hand` |")


def test_hire_rejects_duplicate(tmp_path):
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    hire_mod.hire(
        name="Riley",
        workdir=str(hood),
        role="Clerk",
        base=str(tmp_path),
        roster_path=str(roster_path),
    )
    with pytest.raises(RosterError, match="already"):
        hire_mod.hire(
            name="Riley",
            workdir=str(hood),
            role="Clerk",
            base=str(tmp_path),
            roster_path=str(roster_path),
        )


def test_hire_writes_relative_paths_to_roster(tmp_path):
    """Roster JSON must store relative paths so workspace moves don't break dispatch."""
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    hire_mod.hire(
        name="Dana",
        workdir=str(hood),
        role="Clerk",
        base=str(tmp_path),
        roster_path=str(roster_path),
    )
    raw = json.loads(roster_path.read_text())
    spec = raw["workers"]["dana"]
    assert not os.path.isabs(spec["workdir"]), "workdir must be relative in roster JSON"
    assert not os.path.isabs(spec["contract"]), "contract must be relative in roster JSON"
    assert not os.path.isabs(spec["prompt"]), "prompt must be relative in roster JSON"
    # Paths should be relative to base (tmp_path)
    assert spec["workdir"] == os.path.relpath(str(hood), str(tmp_path))
    assert spec["contract"] == os.path.relpath(
        str(hood / "workers" / "dana" / "CONTRACT.md"), str(tmp_path)
    )


def test_load_resolves_relative_paths_to_absolute(tmp_path):
    """roster.load() must resolve relative paths to absolute so dispatch has real paths."""
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    hire_mod.hire(
        name="Morgan",
        workdir=str(hood),
        role="Analyst",
        base=str(tmp_path),
        roster_path=str(roster_path),
    )
    r = load(path=str(roster_path), base=str(tmp_path))
    w = r.workers["morgan"]
    assert os.path.isabs(w.workdir), "Worker.workdir must be absolute after load"
    assert os.path.isabs(w.contract), "Worker.contract must be absolute after load"
    assert os.path.isabs(w.prompt), "Worker.prompt must be absolute after load"
    assert w.workdir == str(hood)


def test_load_handles_legacy_absolute_paths(tmp_path):
    """Existing roster entries with absolute paths must still load without error (backward compat)."""
    hood = tmp_path / "hood"
    hood.mkdir()
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "legacy": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "legacy",
                "command": ["true"],
            }
        }
    }))
    r = load(path=str(roster_path), base=str(tmp_path))
    w = r.workers["legacy"]
    assert w.workdir == str(hood)
    assert os.path.isabs(w.workdir)


# --- staff / city-ops ---


def test_is_city_ops_workdir():
    assert hire_mod.is_city_ops_workdir("/city/.protocolcity/ops") is True
    assert hire_mod.is_city_ops_workdir("/city/.protocolcity/ops/") is True
    assert hire_mod.is_city_ops_workdir("/city/.protocolcity/ops/workers/x") is True
    assert hire_mod.is_city_ops_workdir("/city/workforce") is False
    assert hire_mod.is_city_ops_workdir("/city/protocolcity/ops") is False
    assert hire_mod.is_city_ops_workdir("") is False


def test_load_coerces_city_ops_staff_true(tmp_path):
    """JSON staff=false on city-ops workdir → Worker.staff True at load."""
    ops = tmp_path / ".protocolcity" / "ops"
    ops.mkdir(parents=True)
    (ops / "c.md").write_text("# c\n")
    (ops / "p.md").write_text("p\n")
    roster_path = tmp_path / "local" / "roster.json"
    roster_path.parent.mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "chief-of-staff": {
                "kind": "job",
                "workdir": str(ops),
                "contract": str(ops / "c.md"),
                "prompt": str(ops / "p.md"),
                "identity": "chief-of-staff",
                "command": ["true"],
                "staff": False,
            }
        }
    }))
    r = load(path=str(roster_path), base=str(tmp_path))
    assert r.workers["chief-of-staff"].staff is True
    # Disk unchanged — hands never rewrite local/; load is the heal.
    raw = json.loads(roster_path.read_text())
    assert raw["workers"]["chief-of-staff"]["staff"] is False


def test_hire_auto_staff_for_city_ops_workdir(tmp_path):
    """Hire into .protocolcity/ops sets staff=true without --staff."""
    ops = tmp_path / ".protocolcity" / "ops"
    ops.mkdir(parents=True)
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(ops),
                "contract": str(ops / "c.md"),
                "prompt": str(ops / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (ops / "c.md").write_text("# c\n")
    (ops / "p.md").write_text("p\n")
    result = hire_mod.hire(
        name="chief-of-staff",
        workdir=str(ops),
        role="Chief of Staff",
        kind="job",
        base=str(tmp_path),
        roster_path=str(roster_path),
        plant=True,
    )
    assert result["ok"] is True
    assert result["worker"]["staff"] is True
    raw = json.loads(roster_path.read_text())
    assert raw["workers"]["chief-of-staff"]["staff"] is True
    r = load(path=str(roster_path), base=str(tmp_path))
    assert r.workers["chief-of-staff"].staff is True


def test_hire_ordinary_workdir_not_staff(tmp_path):
    """Non-ops cabinets stay staff=false so they keep their product sector."""
    hood = tmp_path / "gridfinity"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")
    result = hire_mod.hire(
        name="Neo",
        workdir=str(hood),
        role="Analyst",
        base=str(tmp_path),
        roster_path=str(roster_path),
    )
    assert result["worker"]["staff"] is False
    raw = json.loads(roster_path.read_text())
    # staff=false omitted from roster JSON (default)
    assert "staff" not in raw["workers"]["neo"] or raw["workers"]["neo"].get("staff") is False


def test_hire_staff_explicit_override(tmp_path):
    """--staff forces true; staff=False opts out even on city-ops."""
    hood = tmp_path / "hood"
    hood.mkdir()
    ops = tmp_path / ".protocolcity" / "ops"
    ops.mkdir(parents=True)
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({"workers": {}}))

    forced = hire_mod.hire(
        name="forced-staff",
        workdir=str(hood),
        role="Helper",
        kind="job",
        staff=True,
        base=str(tmp_path),
        roster_path=str(roster_path),
        dry_run=True,
    )
    assert forced["worker"]["staff"] is True

    opted = hire_mod.hire(
        name="opted-out",
        workdir=str(ops),
        role="Helper",
        kind="job",
        staff=False,
        base=str(tmp_path),
        roster_path=str(roster_path),
        dry_run=True,
    )
    assert opted["worker"]["staff"] is False


def _git_init(workdir, *, origin=None):
    """Minimal repo so git_remote_status() resolves deterministically."""
    subprocess.run(["git", "-C", str(workdir), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(workdir), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workdir), "config", "user.name", "tester"],
        check=True,
    )
    if origin:
        subprocess.run(
            ["git", "-C", str(workdir), "remote", "add", "origin", origin],
            check=True,
        )


# --- wf-172: Land-it procedure in hire plant (fallback + city-template graft) ---


def _assert_land_it_body(body: str) -> None:
    """Hard Land-it law (not a soft 'land on main' one-liner)."""
    lower = body.lower()
    assert "land it" in lower
    assert "union" in lower
    assert "origin/main" in lower or "origin/main" in body
    assert "landing" in lower and ("sha" in lower or "commit" in lower)
    assert "re-read" in lower or "reread" in lower or "re-read" in body.lower()


def test_has_land_it_blurb_rejects_soft_land_line():
    soft = (
        "Land commits on main per PROCESS §5.1.3 from the shift tree "
        "(e.g. `git push origin HEAD:main` when FF-able)."
    )
    assert hire_mod._has_land_it_blurb(soft) is False
    assert hire_mod._has_land_it_blurb(
        "## Land it\nCite the landing commit SHA on origin/main."
    ) is True


def test_ensure_grafts_land_it_when_soft_shift_present():
    """City / older soft shift blurb must still get Land-it."""
    soft = (
        "# hand — Employment Contract\n\n"
        "## Shift worktree\n\n"
        "When shift_worktree is true, cwd=$WORKFORCE_SHIFT_WORKDIR.\n"
        "Land commits on main per PROCESS §5.1.3.\n"
    )
    out = hire_mod._ensure_shift_worktree_blurb(
        soft, dest="/tmp/CONTRACT.md", slug="hand"
    )
    _assert_land_it_body(out)
    # Graft once — re-ensure is idempotent
    again = hire_mod._ensure_shift_worktree_blurb(
        out, dest="/tmp/CONTRACT.md", slug="hand"
    )
    assert again.count("## Land it") == 1


def test_ensure_grafts_land_it_on_prompt_soft_line():
    soft = (
        "If `$WORKFORCE_SHIFT_WORKDIR` is set, work and commit there "
        "(shift isolation); land on origin/main before close-out.\n"
    )
    out = hire_mod._ensure_shift_worktree_blurb(
        soft, dest="/tmp/prompt.md", slug="hand"
    )
    _assert_land_it_body(out)


def test_hire_plants_land_it_procedure(tmp_path, monkeypatch):
    """New hires get Land-it without per-product CONTRACT edits."""
    # Force fallback plant so the test does not depend on a city checkout.
    monkeypatch.setattr(hire_mod, "_template_dir", lambda: None)
    hood = tmp_path / "hood"
    hood.mkdir()
    _git_init(hood, origin="https://example.invalid/hood.git")
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")
    result = hire_mod.hire(
        name="LandHand",
        workdir=str(hood),
        role="Builder",
        kind="lane",
        base=str(tmp_path),
        roster_path=str(roster_path),
        plant=True,
    )
    assert result["ok"] is True
    contract = (hood / "workers" / "landhand" / "CONTRACT.md").read_text()
    prompt = (hood / "workers" / "landhand" / "prompt.md").read_text()
    _assert_land_it_body(contract)
    _assert_land_it_body(prompt)
    # Procedure step before close-out, not only a shift footer.
    assert "**Land it**" in contract
    assert "union" in contract.lower()
    assert "FF-only" in contract or "FF-able" in contract


def test_hire_city_template_soft_shift_gains_land_it(tmp_path, monkeypatch):
    """City template with soft land line still receives hard Land-it append."""
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "worker-CONTRACT.md").write_text(
        "# {slug} — Employment Contract (L2)\n\n"
        "## Procedure\n\n"
        "1. Claim\n2. Work\n3. Verify\n4. Close out\n\n"
        "## Shift worktree\n\n"
        "cwd=$WORKFORCE_SHIFT_WORKDIR. Land commits on main per PROCESS.\n"
    )
    (tdir / "worker-prompt.md").write_text(
        "You are `{slug}`.\n"
        "If `$WORKFORCE_SHIFT_WORKDIR` is set, land on origin/main before close-out.\n"
    )
    monkeypatch.setattr(hire_mod, "_template_dir", lambda: tdir)
    hood = tmp_path / "hood"
    hood.mkdir()
    _git_init(hood, origin="https://example.invalid/hood.git")
    contract, prompt = hire_mod.plant_papers(
        str(hood), "grafted", role="Builder", store="demo", neighborhood="Demo"
    )
    cbody = open(contract, encoding="utf-8").read()
    pbody = open(prompt, encoding="utf-8").read()
    _assert_land_it_body(cbody)
    _assert_land_it_body(pbody)
    assert cbody.count("## Land it") == 1


def test_hire_plants_local_only_land_it(tmp_path, monkeypatch):
    """wf-196: no origin remote (e.g. oneseo-pos, recipes) — land locally, not push."""
    monkeypatch.setattr(hire_mod, "_template_dir", lambda: None)
    hood = tmp_path / "hood"
    hood.mkdir()
    _git_init(hood)  # git repo, no origin — HOST_REGISTRY.md "local-only"
    contract, prompt = hire_mod.plant_papers(
        str(hood), "localhand", role="Builder", store="demo", neighborhood="Demo"
    )
    cbody = open(contract, encoding="utf-8").read()
    pbody = open(prompt, encoding="utf-8").read()
    for body in (cbody, pbody):
        low = body.lower()
        assert "no `origin`" in low or "no origin" in low
        assert "merge --ff-only" in body
        assert "push origin" not in low
        assert "landing commit sha" in low or "landing sha" in low or "landing commit" in low


def test_hire_plants_no_git_land_it(tmp_path, monkeypatch):
    """wf-196: workdir isn't a git repo at all — no branch/push language at all."""
    monkeypatch.setattr(hire_mod, "_template_dir", lambda: None)
    hood = tmp_path / "hood"
    hood.mkdir()  # deliberately no git init
    contract, prompt = hire_mod.plant_papers(
        str(hood), "nogithand", role="Builder", store="demo", neighborhood="Demo"
    )
    cbody = open(contract, encoding="utf-8").read()
    pbody = open(prompt, encoding="utf-8").read()
    for body in (cbody, pbody):
        low = body.lower()
        assert "not a git repo" in low  # contract: "repository", prompt: "repo"
        assert "push origin" not in low
        assert "merge --ff-only" not in low


def test_hire_fallback_prompt_teaches_project_not_neighborhood(tmp_path, monkeypatch):
    """wf-224 / pc-1380: fallback papers use project, not neighborhood-as-L1."""
    monkeypatch.setattr(hire_mod, "_template_dir", lambda: None)
    hood = tmp_path / "hood"
    hood.mkdir()
    contract, prompt = hire_mod.plant_papers(
        str(hood), "hand", role="Builder", store="demo", neighborhood="Demo"
    )
    pbody = open(prompt, encoding="utf-8").read()
    cbody = open(contract, encoding="utf-8").read()
    # Wire id NEIGHBORHOOD_NAME still fills the project display name.
    assert "Demo" in pbody
    assert "project instructions" in pbody.lower()
    assert "in **Demo**" in pbody
    assert "neighborhood law" not in pbody.lower()
    assert "in the demo neighborhood" not in pbody.lower()
    assert "neighborhood" not in pbody.lower()
    assert "run the project's checks" in cbody.lower()
    assert "neighborhood" not in cbody.lower()
    assert "cabinet" not in pbody.lower()
    assert "cabinet" not in cbody.lower()


def test_hire_plant_mapping_uses_project_not_cabinet(tmp_path, monkeypatch):
    """wf-224: city-template fills still say project, not cabinet."""
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "worker-CONTRACT.md").write_text(
        "criteria: {CLAIM_CRITERIA — e.g. \"single-file, verifiable by the test suite, no schema changes\"}\n"
        "forbid: {FORBIDDEN_AREA_2}\n"
        "place: {{NEIGHBORHOOD_NAME}}\n"
    )
    (tdir / "worker-prompt.md").write_text(
        "You are {{WORKER_ID}} in **{{NEIGHBORHOOD_NAME}}**.\n"
    )
    monkeypatch.setattr(hire_mod, "_template_dir", lambda: tdir)
    hood = tmp_path / "hood"
    hood.mkdir()
    contract, prompt = hire_mod.plant_papers(
        str(hood), "hand", role="", store="demo", neighborhood="Demo"
    )
    cbody = open(contract, encoding="utf-8").read()
    pbody = open(prompt, encoding="utf-8").read()
    assert "work assigned to this project" in cbody
    assert "other projects' workers/ trees" in cbody
    assert "cabinet" not in cbody.lower()
    assert "place: Demo" in cbody
    assert "in **Demo**" in pbody


def test_git_remote_status(tmp_path):
    origin_dir = tmp_path / "origin_repo"
    origin_dir.mkdir()
    _git_init(origin_dir, origin="https://example.invalid/x.git")
    assert hire_mod.git_remote_status(str(origin_dir)) == "origin"

    local_dir = tmp_path / "local_repo"
    local_dir.mkdir()
    _git_init(local_dir)
    assert hire_mod.git_remote_status(str(local_dir)) == "local-only"

    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    assert hire_mod.git_remote_status(str(plain_dir)) == "no-git"


def _seat_base(tmp_path):
    """A disposable WorkForce base: local/ + a git repo for the generated seat."""
    (tmp_path / "local").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    return repo


def test_generate_seat_folder_dry_run_writes_nothing(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), dry_run=True,
    )
    assert result["dry_run"] is True
    assert set(result["files"]) == {
        "runner.json", "launch.py", "mcp.json", "CONTRACT.md", "prompt.md",
        "permissions.json",
    }
    seat_dir = tmp_path / "local" / "worker-config" / "demo"
    assert not seat_dir.exists()
    for flag in ("--force", "--yolo"):
        assert flag not in result["command"]


def test_generate_seat_folder_writes_five_files_and_roster_row(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    seat_dir = tmp_path / "local" / "worker-config" / "demo"
    for filename in ("runner.json", "launch.py", "mcp.json", "CONTRACT.md", "prompt.md"):
        assert (seat_dir / filename).is_file()

    er = load(result["roster_path"], base=str(tmp_path))
    assert "demo" in er.workers
    w = er.workers["demo"]
    assert w.identity == "demo"
    assert w.schedule  # not held -> a real cron schedule


def test_generate_seat_folder_held_clears_the_schedule(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), held=True,
    )
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].schedule == ""


def test_generate_seat_folder_rejects_bypass_free_but_unknown_provider(tmp_path):
    repo = _seat_base(tmp_path)
    with pytest.raises(hire_mod.AdapterError, match="unknown provider"):
        hire_mod.generate_seat_folder(
            name="demo", provider="chatgpt", project="recipes",
            repository=str(repo), base=str(tmp_path), dry_run=True,
        )


def test_generate_seat_folder_refuses_to_clobber_without_regenerate(tmp_path):
    repo = _seat_base(tmp_path)
    hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    with pytest.raises(RosterError, match="regenerate"):
        hire_mod.generate_seat_folder(
            name="demo", provider="cursor", project="recipes",
            repository=str(repo), base=str(tmp_path),
        )


def test_generate_seat_folder_refuses_roster_only_name_before_any_write(tmp_path):
    """A roster row with no generated folder (e.g. an old --workdir hire)
    must refuse before writing anything, not after creating the folder."""
    repo = _seat_base(tmp_path)
    roster_path = tmp_path / "local" / "roster.json"
    roster_path.write_text(json.dumps({
        "workers": {
            "demo": {
                "kind": "lane",
                "workdir": str(repo),
                "contract": str(repo / "c.md"),
                "prompt": str(repo / "p.md"),
                "identity": "demo",
                "command": ["true"],
            }
        }
    }))
    with pytest.raises(RosterError, match="already on the roster"):
        hire_mod.generate_seat_folder(
            name="demo", provider="cursor", project="recipes",
            repository=str(repo), base=str(tmp_path),
        )
    seat_dir = tmp_path / "local" / "worker-config" / "demo"
    assert not seat_dir.exists()


def test_generate_seat_folder_regenerate_backs_up_and_keeps_held(tmp_path):
    repo = _seat_base(tmp_path)
    hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), held=True,
    )
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), regenerate=True,
    )
    assert result["regenerated"] is True
    backup = result["backup"]
    assert os.path.isdir(backup)
    assert os.path.isfile(os.path.join(backup, "CONTRACT.md"))
    seat_dir = tmp_path / "local" / "worker-config" / "demo"
    assert (seat_dir / "CONTRACT.md").is_file()
    er = load(result["roster_path"], base=str(tmp_path))
    # regenerate keeps the row's held state (schedule stays cleared)
    assert er.workers["demo"].schedule == ""


def test_generate_seat_folder_regenerate_keeps_identity_and_custom_schedule(tmp_path):
    """--regenerate must carry the prior row's identity and cron schedule
    through unchanged unless the caller explicitly overrides them."""
    repo = _seat_base(tmp_path)
    hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), identity="demo-signer",
    )
    roster_path = tmp_path / "local" / "roster.json"
    raw = json.loads(roster_path.read_text())
    raw["workers"]["demo"]["schedule"] = "0 9 * * 1-5"
    roster_path.write_text(json.dumps(raw))

    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), regenerate=True,
    )
    assert result["worker"]["identity"] == "demo-signer"
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].identity == "demo-signer"
    assert er.workers["demo"].schedule == "0 9 * * 1-5"


# --- wf-261: gaps from the first real hire -----------------------------


def test_generate_seat_folder_defaults_to_manual_schedule(tmp_path):
    """A generated seat must never start unattended by accident."""
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].schedule == "manual"


def test_generate_seat_folder_honours_explicit_schedule(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), schedule="*/15 * * * *",
    )
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].schedule == "*/15 * * * *"


def test_generate_seat_folder_regenerate_schedule_override_wins_over_prior(tmp_path):
    repo = _seat_base(tmp_path)
    hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), schedule="0 9 * * 1-5",
    )
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), regenerate=True,
        schedule="manual",
    )
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].schedule == "manual"


def test_generate_seat_folder_authority_chain_is_workspace_then_repo_then_contract(tmp_path):
    """Rule: workspace AGENTS.md, then <repository>/AGENTS.md, then CONTRACT.md."""
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("# workspace law\n")
    data_home = workspace / "workforce"
    (data_home / "local").mkdir(parents=True)
    repo = workspace / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# project law\n")
    _git_init(repo)

    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(data_home),
    )
    er = load(result["roster_path"], base=str(data_home))
    w = er.workers["demo"]
    assert w.authority_chain == [
        str(workspace / "AGENTS.md"),
        str(repo / "AGENTS.md"),
        w.contract,
    ]


def test_generate_seat_folder_authority_chain_explicit_workspace_flag(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# workspace law\n")
    base = tmp_path / "elsewhere"
    (base / "local").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(base), workspace=str(workspace),
    )
    er = load(result["roster_path"], base=str(base))
    assert er.workers["demo"].authority_chain[0] == str(workspace / "AGENTS.md")


@pytest.mark.parametrize("provider,pin", [("cursor", "composer-2.5"), ("grok", "grok-4.6")])
def test_generate_seat_folder_defaults_model_to_adapter_pin(tmp_path, provider, pin):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider=provider, project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].model == pin


def test_generate_seat_folder_explicit_model_wins_over_adapter_pin(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path), model="cursor-grok-4.5-low",
    )
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].model == "cursor-grok-4.5-low"


def test_generate_seat_folder_claude_model_stays_vendor_default(tmp_path):
    """claude's own default is already a deliberate pin — no override needed."""
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="claude", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].model == ""


def test_generate_seat_folder_seat_root_defaults_under_workspace(tmp_path):
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("# workspace law\n")
    data_home = workspace / "workforce"
    (data_home / "local").mkdir(parents=True)
    repo = workspace / "repo"
    repo.mkdir()
    _git_init(repo)

    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(data_home),
    )
    assert result["seat_dir"] == str(workspace / "local" / "worker-config" / "demo")


def test_generate_seat_folder_seat_root_override(tmp_path):
    repo = _seat_base(tmp_path)
    seat_root = tmp_path / "custom-seats"
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
        worker_config_root=str(seat_root),
    )
    assert result["seat_dir"] == str(seat_root / "demo")


def test_generate_seat_folder_worklane_paths_default_under_workspace(tmp_path):
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("# workspace law\n")
    data_home = workspace / "workforce"
    (data_home / "local").mkdir(parents=True)
    repo = workspace / "repo"
    repo.mkdir()
    _git_init(repo)

    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(data_home),
    )
    mcp = json.loads(result["file_bodies"]["mcp.json"])
    server = mcp["mcpServers"]["worklane"]
    assert server["command"] == str(
        workspace / "local" / "worklane" / "current" / "venv" / "bin" / "python"
    )
    assert server["env"]["WORKLANE_RUNTIME_DIR"] == str(
        workspace / "worklane" / "worklane" / "local"
    )


def test_generate_seat_folder_roster_row_uses_hiring_interpreter(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].command[0] == sys.executable


def test_generate_seat_folder_roster_row_carries_identity_env_block(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    er = load(result["roster_path"], base=str(tmp_path))
    env = er.workers["demo"].env
    assert env["WL_AGENT_ID"] == "demo"
    assert env["TP_AGENT_ID"] == "demo"
    assert env["WORKLANE_RUNTIME_DIR"]
    assert env["PATH"] == os.environ.get("PATH", "")


def test_generate_seat_folder_workdir_scope_home_and_perimeter_grants(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    er = load(result["roster_path"], base=str(tmp_path))
    w = er.workers["demo"]
    seat_dir = str(tmp_path / "local" / "worker-config" / "demo")
    assert w.workdir == seat_dir
    assert w.scope_home == seat_dir
    assert w.perimeter_grants == [seat_dir, str(repo)]


def test_generate_seat_folder_contract_and_prompt_paths_are_absolute(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    raw = json.loads((tmp_path / "local" / "roster.json").read_text())
    spec = raw["workers"]["demo"]
    assert os.path.isabs(spec["contract"])
    assert os.path.isabs(spec["prompt"])
    assert os.path.isabs(spec["workdir"])


def test_generate_seat_folder_lane_defaults_max_passes_one(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    er = load(result["roster_path"], base=str(tmp_path))
    assert er.workers["demo"].max_passes == 1
    assert er.workers["demo"].min_pass_secs == 600


def test_generate_seat_folder_prompt_uses_task_runner_placeholders(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    prompt = result["file_bodies"]["prompt.md"]
    for token in ("{authority}", "{task_id}", "{checkout}", "{branch}"):
        assert token in prompt
    assert "wl_show" in prompt and "wl_claim" in prompt and "wl_park" in prompt


def test_generate_seat_folder_grok_gets_trust_flag_and_project_config(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="grok", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    assert "--trust" in result["command"]
    grok_config_key = os.path.join(".grok", "config.toml")
    assert grok_config_key in result["file_bodies"]
    body = result["file_bodies"][grok_config_key]
    assert "demo" in body
    seat_dir = tmp_path / "local" / "worker-config" / "demo"
    assert (seat_dir / ".grok" / "config.toml").is_file()
    launch_body = (seat_dir / "launch.py").read_text()
    assert "_drop_grok_config" in launch_body


def test_generate_seat_folder_cursor_gets_permissions_and_planting_launch(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="cursor", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    seat_dir = tmp_path / "local" / "worker-config" / "demo"
    assert (seat_dir / "permissions.json").is_file()
    permissions = json.loads((seat_dir / "permissions.json").read_text())
    allow = permissions["permissions"]["allow"]
    assert any("Mcp(worklane:wl_show)" == a for a in allow)
    deny = permissions["permissions"]["deny"]
    assert any("Mcp(worklane:wl_close)" == d for d in deny)
    launch_body = (seat_dir / "launch.py").read_text()
    assert ".cursor" in launch_body and "cli.json" in launch_body


def test_generate_seat_folder_claude_launch_py_has_no_provider_planting(tmp_path):
    repo = _seat_base(tmp_path)
    result = hire_mod.generate_seat_folder(
        name="demo", provider="claude", project="recipes",
        repository=str(repo), base=str(tmp_path),
    )
    seat_dir = tmp_path / "local" / "worker-config" / "demo"
    launch_body = (seat_dir / "launch.py").read_text()
    assert ".cursor" not in launch_body and ".grok" not in launch_body


# --- wf-261 review findings: recovery planting, no-overwrite, git-exclude ---


def _exec_launch_py(source, seat_dir, argv, monkeypatch, **patched):
    """Exec a generated launch.py body as __main__, with task_runner patched.

    Runs the real generated source (not a reimplementation) so these tests
    catch the same regressions review found: planting skipped on recovery,
    and an existing task config overwritten. ``__file__`` is set to the seat
    dir's own launch.py so ``h=Path(__file__).parent`` resolves like a real
    dispatch.
    """
    from workforce import task_runner as task_runner_mod
    for name, value in patched.items():
        monkeypatch.setattr(task_runner_mod, name, value)
    monkeypatch.setattr(sys, "argv", ["launch.py"] + argv)
    globs = {"__name__": "__main__", "__file__": str(seat_dir / "launch.py")}
    with pytest.raises(SystemExit):
        exec(compile(source, "<launch.py>", "exec"), globs)


def test_launch_py_cursor_plants_identity_on_recovery_path(tmp_path, monkeypatch):
    seat_dir = tmp_path / "seat"; seat_dir.mkdir()
    (seat_dir / "mcp.json").write_text('{"mcpServers": {}}')
    (seat_dir / "permissions.json").write_text('{"permissions": {}}')
    checkout = tmp_path / "checkout"; checkout.mkdir()
    (checkout / ".git").mkdir()
    receipt = tmp_path / "preparation.json"
    receipt.write_text(json.dumps({"checkout": str(checkout)}))

    def fail_prepare(*a, **k):
        raise AssertionError("prepare() must not run on the recovery path")

    _exec_launch_py(
        hire_mod.seat_templates.LAUNCH_PY_CURSOR, seat_dir,
        ["--config", str(seat_dir / "runner.json"), "--recover-receipt", str(receipt),
         "--recovery-reason", "provider crashed"],
        monkeypatch, main=lambda argv: 0, prepare=fail_prepare,
    )

    assert (checkout / ".cursor" / "mcp.json").read_text() == '{"mcpServers": {}}'
    assert (checkout / ".cursor" / "cli.json").read_text() == '{"permissions": {}}'
    exclude = (checkout / ".git" / "info" / "exclude").read_text()
    assert ".cursor/" in exclude


def test_launch_py_grok_never_overwrites_existing_checkout_config(tmp_path, monkeypatch):
    seat_dir = tmp_path / "seat"; seat_dir.mkdir()
    (seat_dir / ".grok").mkdir()
    (seat_dir / ".grok" / "config.toml").write_text("# seat config\n")
    checkout = tmp_path / "checkout"; checkout.mkdir()
    (checkout / ".git").mkdir()
    (checkout / ".grok").mkdir()
    (checkout / ".grok" / "config.toml").write_text("# task-provided config, keep me\n")
    receipt = tmp_path / "preparation.json"
    receipt.write_text(json.dumps({"checkout": str(checkout)}))

    from workforce import task_runner as task_runner_mod
    globs = {"__name__": "__main__", "__file__": str(seat_dir / "launch.py")}
    monkeypatch.setattr(task_runner_mod, "main", lambda argv: 0)
    monkeypatch.setattr(sys, "argv",
                         ["launch.py", "--config", str(seat_dir / "runner.json"),
                          "--recover-receipt", str(receipt), "--recovery-reason", "provider crashed"])
    with pytest.raises(SystemExit):
        exec(compile(hire_mod.seat_templates.LAUNCH_PY_GROK, "<launch.py>", "exec"), globs)

    assert (checkout / ".grok" / "config.toml").read_text() == "# task-provided config, keep me\n"
    exclude = (checkout / ".git" / "info" / "exclude").read_text()
    assert ".grok/" in exclude


def test_launch_py_grok_plants_config_on_recovery_when_absent(tmp_path, monkeypatch):
    seat_dir = tmp_path / "seat"; seat_dir.mkdir()
    (seat_dir / ".grok").mkdir()
    (seat_dir / ".grok" / "config.toml").write_text("# seat config\n")
    checkout = tmp_path / "checkout"; checkout.mkdir()
    (checkout / ".git").mkdir()
    receipt = tmp_path / "preparation.json"
    receipt.write_text(json.dumps({"checkout": str(checkout)}))

    from workforce import task_runner as task_runner_mod
    globs = {"__name__": "__main__", "__file__": str(seat_dir / "launch.py")}
    monkeypatch.setattr(task_runner_mod, "main", lambda argv: 0)
    monkeypatch.setattr(sys, "argv",
                         ["launch.py", "--config", str(seat_dir / "runner.json"),
                          "--recover-receipt", str(receipt), "--recovery-reason", "provider crashed"])
    with pytest.raises(SystemExit):
        exec(compile(hire_mod.seat_templates.LAUNCH_PY_GROK, "<launch.py>", "exec"), globs)

    assert (checkout / ".grok" / "config.toml").read_text() == "# seat config\n"
    exclude = (checkout / ".git" / "info" / "exclude").read_text()
    assert ".grok/" in exclude

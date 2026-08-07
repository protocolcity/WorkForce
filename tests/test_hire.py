"""Hire write path — papers + roster row (STAFFING §2)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from workforce import hire as hire_mod
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
    contract, prompt = hire_mod.plant_papers(
        str(hood), "grafted", role="Builder", store="demo", neighborhood="Demo"
    )
    cbody = open(contract, encoding="utf-8").read()
    pbody = open(prompt, encoding="utf-8").read()
    _assert_land_it_body(cbody)
    _assert_land_it_body(pbody)
    assert cbody.count("## Land it") == 1

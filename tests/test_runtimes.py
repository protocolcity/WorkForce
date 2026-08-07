"""Tests for workforce.runtimes — detect(), staffing_pool(), and the CLI verb."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import cli as cli_mod
from workforce.runtimes import KNOWN_RUNTIMES, detect, staffing_pool


# ── detect() ──────────────────────────────────────────────────────────────────


def test_detect_returns_entry_for_every_known_runtime(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    result = detect()
    assert set(result.keys()) == set(KNOWN_RUNTIMES)


def test_detect_resolves_installed_cli(monkeypatch):
    def fake_which(name, path=None):
        return "/usr/local/bin/%s" % name if name == "claude" else None

    monkeypatch.setattr("shutil.which", fake_which)
    result = detect()
    assert result["claude"] == "/usr/local/bin/claude"
    assert result["codex"] is None


def test_detect_not_installed_returns_none(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    result = detect()
    assert all(v is None for v in result.values())


def test_detect_forwards_env_path_to_which(monkeypatch):
    seen_paths = []

    def fake_which(name, path=None):
        seen_paths.append(path)
        return None

    monkeypatch.setattr("shutil.which", fake_which)
    detect(env_path="/custom/bin")
    assert all(p == "/custom/bin" for p in seen_paths)
    assert len(seen_paths) == len(KNOWN_RUNTIMES)


# ── staffing_pool() ───────────────────────────────────────────────────────────


def test_staffing_pool_without_roster_has_empty_workers():
    detected = {name: "/bin/%s" % name for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected, roster=None)
    assert len(pool) == len(KNOWN_RUNTIMES)
    for entry in pool:
        assert entry["workers"] == []


def test_staffing_pool_preserves_known_runtime_order():
    detected = {name: None for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected)
    assert [e["cli"] for e in pool] == KNOWN_RUNTIMES


def test_staffing_pool_cross_refs_roster(tmp_path):
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["claude", "-p", "workers/otto/prompt.md"],
            },
            "grok-worker": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "grok-worker",
                "command": ["grok", "--prompt", "@prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))
    r = roster_mod.load(str(rpath))

    detected = {name: "/bin/%s" % name for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected, roster=r)
    by_cli = {e["cli"]: e for e in pool}

    assert "otto" in by_cli["claude"]["workers"]
    assert "grok-worker" in by_cli["grok"]["workers"]
    assert by_cli["codex"]["workers"] == []
    assert by_cli["cursor"]["workers"] == []


def test_staffing_pool_handles_full_path_command(tmp_path):
    """command[0] = full path (e.g. /usr/local/bin/claude) is resolved by basename."""
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["/usr/local/bin/claude", "-p", "prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))
    r = roster_mod.load(str(rpath))

    detected = {name: "/usr/local/bin/%s" % name for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected, roster=r)
    by_cli = {e["cli"]: e for e in pool}
    assert "otto" in by_cli["claude"]["workers"]


# ── CLI verb ──────────────────────────────────────────────────────────────────


def test_cli_runtimes_succeeds_without_roster(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    monkeypatch.setenv("WORKFORCE_ROSTER", str(tmp_path / "nonexistent.json"))
    rc = cli_mod.main(["runtimes"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "RUNTIME STAFFING POOL" in captured.out
    # All known runtimes appear in output even when not installed.
    for name in KNOWN_RUNTIMES:
        assert name in captured.out


def test_cli_runtimes_shows_installed_path(capsys, monkeypatch, tmp_path):
    def fake_which(name, path=None):
        return "/usr/local/bin/claude" if name == "claude" else None

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setenv("WORKFORCE_ROSTER", str(tmp_path / "nonexistent.json"))
    rc = cli_mod.main(["runtimes"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "/usr/local/bin/claude" in captured.out
    assert "not installed" in captured.out  # the other runtimes


def test_scene_model_includes_runtimes(monkeypatch, tmp_path):
    """scene_model() carries a 'runtimes' key with one entry per known runtime."""
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    from workforce import board as board_mod

    model = board_mod.scene_model(str(tmp_path))
    assert "runtimes" in model
    pool = model["runtimes"]
    assert len(pool) == len(KNOWN_RUNTIMES)
    assert [e["cli"] for e in pool] == KNOWN_RUNTIMES
    assert all(e["path"] is None for e in pool)
    assert all(e["workers"] == [] for e in pool)


def test_cli_runtimes_employment_status(capsys, monkeypatch, tmp_path):
    """Installed CLI employed by a roster worker shows count and quota-hits column."""
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["claude", "-p", "workers/otto/prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))

    monkeypatch.setattr("shutil.which",
                        lambda name, path=None: "/usr/local/bin/%s" % name if name == "claude" else None)
    rc = cli_mod.main(["--file", str(rpath), "runtimes"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "1 worker" in captured.out
    assert "quota hits (7d)" in captured.out
    # claude is employed; the other installed runtimes show "available"
    lines = [l for l in captured.out.splitlines() if "claude" in l]
    assert lines and "worker" in lines[0]


# ── limit_hits telemetry ──────────────────────────────────────────────────────


def test_staffing_pool_employed_key_equals_worker_count(tmp_path):
    """employed key always equals len(workers)."""
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "a": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "a",
                "command": ["claude", "-p", "prompt.md"],
            },
            "b": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "b",
                "command": ["claude", "-p", "prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))
    r = roster_mod.load(str(rpath))
    detected = {name: "/bin/%s" % name for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected, r)
    by_cli = {e["cli"]: e for e in pool}
    assert by_cli["claude"]["employed"] == 2
    assert by_cli["grok"]["employed"] == 0


def test_staffing_pool_limit_hits_zero_without_local_root(tmp_path):
    """limit_hits is 0 when local_root is not provided."""
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["claude", "-p", "prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))
    r = roster_mod.load(str(rpath))
    detected = {name: "/bin/%s" % name for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected, r)  # no local_root
    assert all(e["limit_hits"] == 0 for e in pool)


def test_staffing_pool_limit_hits_counts_vendor_limit_shifts(tmp_path):
    """limit_hits counts ERROR reason='vendor limit:...' shifts in the last 7 days."""
    import datetime
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["claude", "-p", "prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))
    r = roster_mod.load(str(rpath))

    # Write a fixture ledger: two vendor_limit shifts + one ok shift.
    now = datetime.datetime.now(datetime.timezone.utc)
    def ts(offset_hours=0):
        return (now - datetime.timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    ledger_dir = tmp_path / "local" / "ledger"
    ledger_dir.mkdir(parents=True)
    ledger_file = ledger_dir / "otto.log"
    ledger_file.write_text(
        "%s START queue=3\n"
        "%s ERROR reason=\"vendor limit: 429 Too Many Requests\"\n"
        "%s START queue=2\n"
        "%s ERROR reason=\"vendor limit: rate limit exceeded\"\n"
        "%s START queue=1\n"
        "%s DONE rc=0 on_pass=1\n"
        "%s STOP reason=\"queue empty\"\n"
        % (ts(5), ts(4), ts(3), ts(2), ts(1), ts(0), ts(0))
    )

    detected = {name: "/bin/%s" % name for name in KNOWN_RUNTIMES}
    local_root = str(tmp_path / "local")
    pool = staffing_pool(detected, r, local_root=local_root)
    by_cli = {e["cli"]: e for e in pool}
    assert by_cli["claude"]["limit_hits"] == 2
    assert by_cli["grok"]["limit_hits"] == 0


def test_staffing_pool_limit_hits_zero_on_missing_ledger(tmp_path):
    """No ledger file → limit_hits is 0, never raises."""
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["claude", "-p", "prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))
    r = roster_mod.load(str(rpath))
    detected = {name: "/bin/%s" % name for name in KNOWN_RUNTIMES}
    # local_root points at a nonexistent directory — no ledger files.
    pool = staffing_pool(detected, r, local_root=str(tmp_path / "nonexistent"))
    assert all(e["limit_hits"] == 0 for e in pool)


def test_staffing_pool_limit_hits_excludes_old_shifts(tmp_path):
    """Shifts older than 7 days are not counted in limit_hits."""
    import datetime
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["claude", "-p", "prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))
    r = roster_mod.load(str(rpath))

    now = datetime.datetime.now(datetime.timezone.utc)
    def ts(offset_days=0):
        return (now - datetime.timedelta(days=offset_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    ledger_dir = tmp_path / "local" / "ledger"
    ledger_dir.mkdir(parents=True)
    ledger_file = ledger_dir / "otto.log"
    # One shift in window, one outside (9 days ago).
    ledger_file.write_text(
        "%s START queue=1\n"
        "%s ERROR reason=\"vendor limit: 429\"\n"
        "%s START queue=1\n"
        "%s ERROR reason=\"vendor limit: old\"\n"
        % (ts(1), ts(1), ts(9), ts(9))
    )

    detected = {name: "/bin/%s" % name for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected, r, local_root=str(tmp_path / "local"))
    by_cli = {e["cli"]: e for e in pool}
    assert by_cli["claude"]["limit_hits"] == 1

"""CLI smoke tests — retired 'open' subcommand + daemon-plist door hint."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import cli  # noqa: E402
from workforce import board  # noqa: E402


def test_open_deprecated_exits_nonzero(capsys):
    rc = cli.main(["open"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "DEPRECATED" in captured.err
    assert "API-only" in captured.err
    assert captured.out.strip() == ""


def test_open_print_flag_still_deprecated(capsys):
    rc = cli.main(["open", "--print"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "DEPRECATED" in captured.err


def test_open_port_flag_still_deprecated(capsys):
    rc = cli.main(["open", "--port", "9111"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "DEPRECATED" in captured.err


def test_daemon_plist_stdout_is_clean_xml_url_hint_on_stderr(capsys):
    rc = cli.main(["daemon-plist"])
    captured = capsys.readouterr()

    assert rc == 0
    # stdout is the plist and nothing else — safe to redirect into a .plist file.
    assert captured.out.startswith("<?xml")
    assert captured.out.rstrip().endswith("</plist>")
    assert "http://127.0.0.1" not in captured.out
    # The door is mentioned, on stderr.
    assert "http://127.0.0.1:%d" % board.DEFAULT_PORT in captured.err


def test_daemon_plist_bakes_data_dir_when_env_set(tmp_path, monkeypatch, capsys):
    """WORKFORCE_DATA_DIR is carried into the plist so the daemon is self-contained."""
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(tmp_path))
    rc = cli.main(["daemon-plist"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out.startswith("<?xml")
    # data_dir appears as both WorkingDirectory and an EnvironmentVariables entry.
    assert str(tmp_path) in captured.out
    assert "WORKFORCE_DATA_DIR" in captured.out


# --- doctor subcommand ---

import json  # noqa: E402


def _write_roster(path, names):
    path.parent.mkdir(parents=True, exist_ok=True)
    workers = {}
    for n in names:
        workers[n] = {
            "kind": "job",
            "workdir": str(path.parent),
            "contract": str(path.parent / "c.md"),
            "prompt": str(path.parent / "p.md"),
            "identity": n,
            "command": ["true"],
        }
    path.write_text(json.dumps({"workers": workers}))


def _stub_section_52(tmp_path, monkeypatch, names):
    """Point WORKLANE_PROCESS at a minimal §5.2 table covering *names* (hermetic)."""
    rows = "\n".join("| `%s` | test. |" % n for n in names)
    process = tmp_path / "PROCESS-stub.md"
    process.write_text(
        "### 5.2) Identity\n\n| Agent id | Who |\n| --- | --- |\n%s\n\n### 5.3) Other\n"
        % rows
    )
    monkeypatch.setenv("WORKLANE_PROCESS", str(process))
    return process


def test_doctor_no_suite_roster_exits_ok(tmp_path, monkeypatch, capsys):
    """Without a suite roster configured, doctor reports unconfigured and exits 0."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["alpha"])
    _stub_section_52(tmp_path, monkeypatch, ["alpha"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not configured" in out
    assert "doctor: OK" in out


def test_doctor_matching_rosters_exits_ok(tmp_path, monkeypatch, capsys):
    """Identical worker keys in both homes → no drift, exits 0."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["alpha", "beta"])
    suite = tmp_path / "suite" / "roster.json"
    _write_roster(suite, ["alpha", "beta"])
    _stub_section_52(tmp_path, monkeypatch, ["alpha", "beta"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    rc = cli.main(["doctor", "--suite-roster", str(suite)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out
    assert "DRIFT" not in out


def test_doctor_detects_engine_only_worker(tmp_path, monkeypatch, capsys):
    """Worker present in engine but missing from suite → DRIFT fault, exits 1."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["alpha", "beta"])
    suite = tmp_path / "suite" / "roster.json"
    _write_roster(suite, ["alpha"])
    _stub_section_52(tmp_path, monkeypatch, ["alpha", "beta"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    rc = cli.main(["doctor", "--suite-roster", str(suite)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "beta" in captured.err
    assert "engine only" in captured.err


def test_doctor_detects_suite_only_worker(tmp_path, monkeypatch, capsys):
    """Worker present in suite but missing from engine → DRIFT fault, exits 1."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["alpha"])
    suite = tmp_path / "suite" / "roster.json"
    _write_roster(suite, ["alpha", "ghost"])
    _stub_section_52(tmp_path, monkeypatch, ["alpha"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    rc = cli.main(["doctor", "--suite-roster", str(suite)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "ghost" in captured.err
    assert "suite only" in captured.err


def test_doctor_suite_path_from_env(tmp_path, monkeypatch, capsys):
    """WORKFORCE_SUITE_ROSTER env var is respected as the suite path."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["alpha"])
    suite = tmp_path / "suite" / "roster.json"
    _write_roster(suite, ["alpha"])
    _stub_section_52(tmp_path, monkeypatch, ["alpha"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.setenv("WORKFORCE_SUITE_ROSTER", str(suite))
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out


def test_doctor_flags_unregistered_identity(tmp_path, monkeypatch, capsys):
    """Roster identity missing from PROCESS §5.2 → IDENTITY fault."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["alpha", "ghost-hand"])
    _stub_section_52(tmp_path, monkeypatch, ["alpha"])  # ghost-hand intentionally absent
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "ghost-hand" in captured.err
    assert "IDENTITY" in captured.err
    assert "§5.2" in captured.err or "PROCESS" in captured.err


def test_doctor_section_52_all_registered_ok(tmp_path, monkeypatch, capsys):
    """When every roster identity is in §5.2, doctor stays green."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["alpha", "beta"])
    _stub_section_52(tmp_path, monkeypatch, ["alpha", "beta"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "registered" in captured.out
    assert "IDENTITY" not in captured.err


def test_doctor_flags_city_ops_without_staff(tmp_path, monkeypatch, capsys):
    """City-ops workdir with staff falsy → STAFF fault."""
    data = tmp_path / "engine"
    ops = data / ".protocolcity" / "ops"
    ops.mkdir(parents=True)
    (ops / "c.md").write_text("# c\n")
    (ops / "p.md").write_text("p\n")
    roster = data / "local" / "roster.json"
    roster.parent.mkdir(parents=True)
    roster.write_text(json.dumps({
        "workers": {
            "chief-of-staff": {
                "kind": "job",
                "workdir": str(ops),
                "contract": str(ops / "c.md"),
                "prompt": str(ops / "p.md"),
                "identity": "chief-of-staff",
                "command": ["true"],
                # staff omitted / false — the bug under test
            }
        }
    }))
    _stub_section_52(tmp_path, monkeypatch, ["chief-of-staff"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "STAFF" in captured.err
    assert "chief-of-staff" in captured.err
    assert "city-ops" in captured.err or ".protocolcity/ops" in captured.err


def test_doctor_city_ops_with_staff_ok(tmp_path, monkeypatch, capsys):
    """City-ops workdir with staff=true is clean."""
    data = tmp_path / "engine"
    ops = data / ".protocolcity" / "ops"
    ops.mkdir(parents=True)
    (ops / "c.md").write_text("# c\n")
    (ops / "p.md").write_text("p\n")
    roster = data / "local" / "roster.json"
    roster.parent.mkdir(parents=True)
    roster.write_text(json.dumps({
        "workers": {
            "chief-of-staff": {
                "kind": "job",
                "workdir": str(ops),
                "contract": str(ops / "c.md"),
                "prompt": str(ops / "p.md"),
                "identity": "chief-of-staff",
                "command": ["true"],
                "staff": True,
            }
        }
    }))
    _stub_section_52(tmp_path, monkeypatch, ["chief-of-staff"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "STAFF" not in captured.err
    assert "doctor: OK" in captured.out


# --- one-file roster law ---


def test_doctor_single_home_symlink_exits_ok(tmp_path, monkeypatch, capsys):
    """Engine roster is a symlink to the canonical (suite) file → samefile True → single-home OK."""
    canonical = tmp_path / "city" / "local" / "roster.json"
    _write_roster(canonical, ["alpha", "beta"])
    data = tmp_path / "engine"
    local_dir = data / "local"
    local_dir.mkdir(parents=True)
    symlink = local_dir / "roster.json"
    symlink.symlink_to(canonical)
    _stub_section_52(tmp_path, monkeypatch, ["alpha", "beta"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    rc = cli.main(["doctor", "--suite-roster", str(canonical)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "single-home" in out
    assert "one physical roster" in out
    assert "FAULT" not in out
    assert "DRIFT" not in out


def test_doctor_single_home_via_env_symlink_exits_ok(tmp_path, monkeypatch, capsys):
    """WORKFORCE_SUITE_ROSTER pointing at the symlink target is also recognised as single-home."""
    canonical = tmp_path / "city" / "local" / "roster.json"
    _write_roster(canonical, ["gamma"])
    data = tmp_path / "engine"
    local_dir = data / "local"
    local_dir.mkdir(parents=True)
    (local_dir / "roster.json").symlink_to(canonical)
    _stub_section_52(tmp_path, monkeypatch, ["gamma"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.setenv("WORKFORCE_SUITE_ROSTER", str(canonical))
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "single-home" in out
    assert "one physical roster" in out


# --- queue_url lint ---


def _write_roster_worker(path, name, extra=None):
    """Write a roster with one worker, merging extra fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    spec = {
        "kind": "lane",
        "workdir": str(path.parent),
        "contract": str(path.parent / "c.md"),
        "prompt": str(path.parent / "p.md"),
        "identity": name,
        "command": ["true"],
    }
    if extra:
        spec.update(extra)
    path.write_text(json.dumps({"workers": {name: spec}}))


def test_doctor_flags_lane_missing_queue_url(tmp_path, monkeypatch, capsys):
    """Lane worker with no queue_url is a QUEUE fault — doctor exits 1."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "garfield")
    _stub_section_52(tmp_path, monkeypatch, ["garfield"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "garfield" in captured.err
    assert "queue_url" in captured.err


def test_doctor_flags_worker_param_in_queue_url(tmp_path, monkeypatch, capsys):
    """Lane worker with ?worker= form (missing label=worker:) is a QUEUE fault."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "garfield", extra={
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks/ready?product=foo&worker=garfield",
    })
    _stub_section_52(tmp_path, monkeypatch, ["garfield"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "garfield" in captured.err
    assert "worker=" in captured.err or "label=worker:" in captured.err


def test_doctor_passes_lane_with_correct_queue_url(tmp_path, monkeypatch, capsys):
    """Lane worker with label=worker: form passes doctor queue lint."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "garfield", extra={
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks/ready?product=foo&label=worker:garfield",
    })
    _stub_section_52(tmp_path, monkeypatch, ["garfield"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "doctor: OK" in out


def test_doctor_notes_lane_git_without_shift_worktree(tmp_path, monkeypatch, capsys):
    """wf-153: lane on a git workdir with flag off is a NOTE, not a FAULT."""
    import subprocess

    data = tmp_path / "engine"
    hood = data / "hood"
    hood.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(hood), check=True, capture_output=True)
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "coder", extra={
        "workdir": str(hood),
        "contract": str(hood / "c.md"),
        "prompt": str(hood / "p.md"),
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks/ready?product=x&label=worker:coder",
        "shift_worktree": False,
    })
    (hood / "c.md").write_text("c\n")
    (hood / "p.md").write_text("p\n")
    _stub_section_52(tmp_path, monkeypatch, ["coder"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "doctor: OK" in captured.out
    assert "Shift worktree" in captured.out
    assert "coder" in captured.out


def test_doctor_notes_max_fires_per_day_when_set(tmp_path, monkeypatch, capsys):
    """wf-166: non-zero max_fires_per_day is a doctor NOTE, not a FAULT."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "cos", extra={
        "kind": "job",
        "command": ["true"],
        "max_fires_per_day": 1,
        "schedule": "10 * * * *",
    })
    _stub_section_52(tmp_path, monkeypatch, ["cos"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "doctor: OK" in captured.out
    assert "Daily fire ceiling" in captured.out
    assert "cos=1" in captured.out


def test_doctor_no_day_cap_note_when_unlimited(tmp_path, monkeypatch, capsys):
    """wf-166: max_fires_per_day absent/0 → unlimited note, still OK."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["jobber"])
    _stub_section_52(tmp_path, monkeypatch, ["jobber"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Daily fire ceiling: no seats pinned" in captured.out


def test_doctor_no_shift_note_when_flag_on(tmp_path, monkeypatch, capsys):
    """Lane with shift_worktree true is not listed in enablement notes."""
    import subprocess

    data = tmp_path / "engine"
    hood = data / "hood"
    hood.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(hood), check=True, capture_output=True)
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "coder", extra={
        "workdir": str(hood),
        "contract": str(hood / "c.md"),
        "prompt": str(hood / "p.md"),
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks/ready?product=x&label=worker:coder",
        "shift_worktree": True,
    })
    (hood / "c.md").write_text("c\n")
    (hood / "p.md").write_text("p\n")
    _stub_section_52(tmp_path, monkeypatch, ["coder"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no code-lane enablement notes" in out


def test_doctor_no_shift_note_when_key_absent_lane(tmp_path, monkeypatch, capsys):
    """wf-153 slice 4: absent key loads as on for lanes — no enablement note."""
    import subprocess

    data = tmp_path / "engine"
    hood = data / "hood"
    hood.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(hood), check=True, capture_output=True)
    roster = data / "local" / "roster.json"
    # no shift_worktree key — load defaults true for kind=lane
    _write_roster_worker(roster, "coder", extra={
        "workdir": str(hood),
        "contract": str(hood / "c.md"),
        "prompt": str(hood / "p.md"),
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks/ready?product=x&label=worker:coder",
    })
    (hood / "c.md").write_text("c\n")
    (hood / "p.md").write_text("p\n")
    _stub_section_52(tmp_path, monkeypatch, ["coder"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no code-lane enablement notes" in out


# --- doctor CLI-on-PATH and capacity checks ---

def test_doctor_flags_cli_not_on_path(tmp_path, monkeypatch, capsys):
    """Lane worker whose CLI binary is not on PATH is flagged as a CLI fault."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "ghost", extra={
        "command": ["__not_on_path_abc123__"],
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks?label=worker:ghost",
    })
    _stub_section_52(tmp_path, monkeypatch, ["ghost"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "CLI:" in captured.err
    assert "ghost" in captured.err


def test_doctor_ok_cli_on_path(tmp_path, monkeypatch, capsys):
    """Lane worker whose CLI is on PATH ('true') passes the PATH check."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "specter", extra={
        "command": ["true"],
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks?label=worker:specter",
    })
    _stub_section_52(tmp_path, monkeypatch, ["specter"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "doctor: OK" in out


def test_doctor_ok_absolute_path_cli(tmp_path, monkeypatch, capsys):
    """Absolute argv[0] that exists + is executable is not a CLI fault.

    Mirrors venv-pinned seats: basename (e.g. 'python') may be off PATH while
    the absolute interpreter path is valid.
    """
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    interp = bin_dir / "python"
    interp.write_text("#!/bin/sh\nexit 0\n")
    interp.chmod(0o755)
    _write_roster_worker(roster, "visual-sweep", extra={
        "command": [str(interp)],
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks?label=worker:visual-sweep",
    })
    _stub_section_52(tmp_path, monkeypatch, ["visual-sweep"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    # Ensure bare 'python' is not what rescues us — only the absolute path.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "doctor: OK" in captured.out
    assert "CLI:" not in captured.err


def test_doctor_flags_absolute_path_missing(tmp_path, monkeypatch, capsys):
    """Absolute argv[0] that does not exist is still a CLI fault."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    missing = str(tmp_path / "no" / "such" / "python")
    _write_roster_worker(roster, "ghost", extra={
        "command": [missing],
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks?label=worker:ghost",
    })
    _stub_section_52(tmp_path, monkeypatch, ["ghost"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "CLI:" in captured.err
    assert "ghost" in captured.err
    assert missing in captured.err or "not executable" in captured.err


def test_command_present_unit():
    """_command_present: abs path vs bare name."""
    assert cli._command_present("true") is True
    assert cli._command_present("__not_on_path_xyz_wf142__") is False
    assert cli._command_present("/no/such/abs/binary_wf142") is False


def test_doctor_flags_capacity_streak(tmp_path, monkeypatch, capsys):
    """Worker with N consecutive vendor_limit fails surfaces a CAPACITY fault."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "blossom", extra={
        "command": ["true"],
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks?label=worker:blossom",
    })
    ledger_dir = data / "local" / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for minute in ("01", "02", "03"):
        ts = "2026-08-02T15:%s:00Z" % minute
        lines.extend([
            "%s START identity=blossom kind=lane budget_secs=60 dry_run=0" % ts,
            "%s ERROR reason=\"vendor limit: usage limit\" rc=1 on_pass=1" % ts,
        ])
    (ledger_dir / "blossom.log").write_text("\n".join(lines) + "\n")
    _stub_section_52(tmp_path, monkeypatch, ["blossom"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "CAPACITY" in captured.err


def test_doctor_no_capacity_fault_when_clear(tmp_path, monkeypatch, capsys):
    """Worker whose most recent shift succeeded does not trigger a CAPACITY fault."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "garfield", extra={
        "command": ["true"],
        "queue_url": "http://127.0.0.1:8799/api/admin/tasks?label=worker:garfield",
    })
    ledger_dir = data / "local" / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "2026-08-02T15:01:00Z START identity=garfield kind=lane budget_secs=60 dry_run=0",
        "2026-08-02T15:01:00Z ERROR reason=\"vendor limit: usage limit\" rc=1 on_pass=1",
        "2026-08-02T15:02:00Z START identity=garfield kind=lane budget_secs=60 dry_run=0",
        "2026-08-02T15:02:00Z DONE rc=0 on_pass=1",
        "2026-08-02T15:02:00Z STOP reason=\"single-pass complete\"",
    ]
    (ledger_dir / "garfield.log").write_text("\n".join(lines) + "\n")
    _stub_section_52(tmp_path, monkeypatch, ["garfield"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "doctor: OK" in out

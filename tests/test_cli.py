"""CLI board-discovery story — the door gets found without a browser."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import cli  # noqa: E402
from workforce import board  # noqa: E402


def test_open_print_emits_board_url(capsys):
    rc = cli.main(["open", "--print"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "http://127.0.0.1:%d" % board.DEFAULT_PORT


def test_open_print_honors_port_override(capsys):
    rc = cli.main(["open", "--print", "--port", "9111"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "http://127.0.0.1:9111"


def test_open_launches_default_browser(capsys, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append(argv)

    monkeypatch.setattr("subprocess.Popen", FakePopen)
    rc = cli.main(["open"])
    out = capsys.readouterr().out.strip()

    assert rc == 0
    assert out == "http://127.0.0.1:%d" % board.DEFAULT_PORT  # URL always printed
    assert len(calls) == 1
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    assert calls[0] == [opener, out]


def test_open_opener_missing_is_nonfatal_and_url_survives(capsys, monkeypatch):
    def boom(argv, **kw):
        raise OSError("no such opener")

    monkeypatch.setattr("subprocess.Popen", boom)
    rc = cli.main(["open"])
    captured = capsys.readouterr()

    assert rc == 1
    # The URL still reached stdout — the point of the command survives a dead opener.
    assert captured.out.strip() == "http://127.0.0.1:%d" % board.DEFAULT_PORT
    assert "could not launch" in captured.err


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
            "workdir": str(path.parent),
            "contract": str(path.parent / "c.md"),
            "prompt": str(path.parent / "p.md"),
            "identity": n,
            "command": ["true"],
        }
    path.write_text(json.dumps({"workers": workers}))


def test_doctor_no_suite_roster_exits_ok(tmp_path, monkeypatch, capsys):
    """Without a suite roster configured, doctor reports unconfigured and exits 0."""
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["alpha"])
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
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.setenv("WORKFORCE_SUITE_ROSTER", str(suite))
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out


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
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.setenv("WORKFORCE_SUITE_ROSTER", str(canonical))
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "single-home" in out
    assert "one physical roster" in out

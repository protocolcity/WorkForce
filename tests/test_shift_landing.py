"""wf-171 — unlanded shift-branch scan (doctor health surface)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import shift_landing as sl  # noqa: E402
from workforce import cli  # noqa: E402
from workforce.roster import Worker  # noqa: E402


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
    )


def _init_repo(path, *, commit_msg="init"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "test")
    # Default branch main (git version variance).
    _git(path, "checkout", "-B", "main", check=False)
    (path / "README").write_text("base\n")
    _git(path, "add", "README")
    _git(path, "commit", "-m", commit_msg)
    return path


def _make_worker(tmp_path, name="coder", **over):
    hood = tmp_path / "hood"
    if not hood.exists():
        _init_repo(hood)
    contract = hood / "CONTRACT.md"
    prompt = hood / "prompt.md"
    if not contract.exists():
        contract.write_text("c\n")
    if not prompt.exists():
        prompt.write_text("p\n")
    spec = dict(
        name=name,
        workdir=str(hood),
        contract=str(contract),
        prompt=str(prompt),
        identity=name,
        command=["true"],
        queue_url="http://127.0.0.1:8799/api/admin/tasks/ready?label=worker:%s" % name,
        kind="lane",
        shift_worktree=True,
    )
    spec.update(over)
    return Worker(**spec), hood


def test_resolve_landing_ref_prefers_main(tmp_path):
    hood = _init_repo(tmp_path / "r")
    ref = sl.resolve_landing_ref(str(hood))
    assert ref in ("main", "refs/heads/main")


def test_resolve_landing_ref_prefers_origin_main(tmp_path):
    primary = _init_repo(tmp_path / "primary")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(bare))
    # re-init bare properly
    subprocess.run(
        ["git", "init", "--bare", str(bare)], check=True, capture_output=True,
    )
    _git(primary, "remote", "add", "origin", str(bare))
    _git(primary, "push", "-u", "origin", "main")
    # Local commit ahead of origin — landing ref still origin/main
    (primary / "README").write_text("local only\n")
    _git(primary, "add", "README")
    _git(primary, "commit", "-m", "local")
    ref = sl.resolve_landing_ref(str(primary))
    assert ref in ("origin/main", "refs/remotes/origin/main")


def test_commits_ahead_counts(tmp_path):
    hood = _init_repo(tmp_path / "r")
    base = _git(hood, "rev-parse", "HEAD").stdout.strip()
    (hood / "README").write_text("v2\n")
    _git(hood, "add", "README")
    _git(hood, "commit", "-m", "v2")
    assert sl.commits_ahead(str(hood), "HEAD", base) == 1
    assert sl.commits_ahead(str(hood), base, "HEAD") == 0


def test_scan_worker_clear_when_on_main(tmp_path):
    w, hood = _make_worker(tmp_path)
    assert sl.scan_worker(w, str(tmp_path / "local")) is None


def test_scan_worker_skips_when_shift_worktree_off(tmp_path):
    w, hood = _make_worker(tmp_path, shift_worktree=False)
    # Put commits on a shift-looking branch anyway — still skip.
    _git(hood, "checkout", "-B", "workforce/shift/coder")
    (hood / "README").write_text("shift\n")
    _git(hood, "add", "README")
    _git(hood, "commit", "-m", "shift work")
    _git(hood, "checkout", "main")
    assert sl.scan_worker(w, str(tmp_path / "local")) is None


def test_scan_worker_flags_shift_branch_ahead(tmp_path):
    w, hood = _make_worker(tmp_path, name="ring")
    # Create shift branch with 2 commits not on main.
    _git(hood, "checkout", "-B", "workforce/shift/ring")
    (hood / "a.txt").write_text("a\n")
    _git(hood, "add", "a.txt")
    _git(hood, "commit", "-m", "a")
    (hood / "b.txt").write_text("b\n")
    _git(hood, "add", "b.txt")
    _git(hood, "commit", "-m", "b")
    _git(hood, "checkout", "main")
    row = sl.scan_worker(w, str(tmp_path / "local"))
    assert row is not None
    assert row["worker"] == "ring"
    assert row["commits_ahead"] == 2
    assert row["surface"] == "shift_branch"
    assert "shift/ring" in row["branch"]


def test_scan_worker_flags_primary_ahead_of_origin(tmp_path):
    """FF-to-primary without push still counts as unlanded vs origin/main."""
    w, hood = _make_worker(tmp_path, name="hand")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare)], check=True, capture_output=True,
    )
    _git(hood, "remote", "add", "origin", str(bare))
    _git(hood, "push", "-u", "origin", "main")
    # Commit on primary only (simulates post-shift FF, no push).
    (hood / "c.txt").write_text("c\n")
    _git(hood, "add", "c.txt")
    _git(hood, "commit", "-m", "unpushed")
    row = sl.scan_worker(w, str(tmp_path / "local"))
    assert row is not None
    assert row["primary_ahead"] == 1
    assert row["surface"] in ("primary", "both")
    assert "origin/main" in row["landing_ref"]


def test_format_report_clean_and_dirty():
    clean = sl.format_report([])
    assert "clean" in clean
    dirty = sl.format_report([{
        "worker": "ring",
        "branch": "workforce/shift/ring",
        "landing_ref": "origin/main",
        "commits_ahead": 34,
        "surface": "shift_branch",
    }])
    assert "34" in dirty
    assert "ring" in dirty
    assert "wf-171" in dirty
    assert "git push origin HEAD:main" in dirty


def test_doctor_reports_unlanded_note(tmp_path, monkeypatch, capsys):
    """Doctor prints unlanded section; exit stays 0 (note, not FAULT)."""
    from tests.test_cli import _write_roster_worker, _stub_section_52

    data = tmp_path / "engine"
    hood = data / "hood"
    _init_repo(hood)
    _git(hood, "checkout", "-B", "workforce/shift/coder")
    (hood / "x.txt").write_text("x\n")
    _git(hood, "add", "x.txt")
    _git(hood, "commit", "-m", "unlanded")
    _git(hood, "checkout", "main")

    roster = data / "local" / "roster.json"
    _write_roster_worker(roster, "coder", extra={
        "workdir": str(hood),
        "contract": str(hood / "c.md"),
        "prompt": str(hood / "p.md"),
        "queue_url": (
            "http://127.0.0.1:8799/api/admin/tasks/ready?"
            "product=x&label=worker:coder"
        ),
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
    assert "Unlanded shift commits" in out
    assert "coder" in out
    assert "doctor: OK" in out


def test_doctor_clean_unlanded_when_no_shift_seats(tmp_path, monkeypatch, capsys):
    from tests.test_cli import _write_roster, _stub_section_52

    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    _write_roster(roster, ["jobber"])  # job → shift_worktree false
    _stub_section_52(tmp_path, monkeypatch, ["jobber"])
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Unlanded shift commits" in out
    assert "clean" in out

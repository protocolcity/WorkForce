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


def test_patches_ahead_unique_and_already_landed(tmp_path):
    """wf-192: cherry-equivalent patches on main are not unlanded."""
    hood = _init_repo(tmp_path / "r")
    main_base = _git(hood, "rev-parse", "HEAD").stdout.strip()
    # Two sibling commits with identical trees (same patch, different SHAs).
    # Built with commit-tree so neither is an ancestor of the other.
    (hood / "ARCHITECTURE.md").write_text("# arch\n")
    _git(hood, "add", "ARCHITECTURE.md")
    tree = _git(hood, "write-tree").stdout.strip()
    shift_tip = _git(
        hood, "commit-tree", tree, "-p", main_base, "-m", "plant ARCHITECTURE.md (shift)",
    ).stdout.strip()
    main_tip = _git(
        hood, "commit-tree", tree, "-p", main_base, "-m", "plant ARCHITECTURE.md (main)",
    ).stdout.strip()
    _git(hood, "update-ref", "refs/heads/workforce/shift/demo", shift_tip)
    _git(hood, "update-ref", "refs/heads/main", main_tip)
    assert shift_tip != main_tip
    # Ancestry: each tip is 1 commit ahead of the other base line.
    assert sl.commits_ahead(str(hood), shift_tip, main_base) == 1
    assert sl.commits_ahead(str(hood), shift_tip, main_tip) == 1
    # Patch already on main tip → cherry reports 0 unique.
    assert sl.patches_ahead(str(hood), shift_tip, main_base) == 1
    assert sl.patches_ahead(str(hood), shift_tip, main_tip) == 0


def test_scan_worker_clear_when_patch_already_on_landing(tmp_path):
    """Shift tip whose patch is already on main via FF cherry-pick → clear.

    Split-history cherry-equivalence (same tree, neither ancestor) is
    ``test_scan_worker_diverged_even_when_patches_equivalent``.
    """
    w, hood = _make_worker(tmp_path, name="demo")
    _git(hood, "checkout", "-B", "workforce/shift/demo")
    (hood / "ARCHITECTURE.md").write_text("# arch\n")
    _git(hood, "add", "ARCHITECTURE.md")
    _git(hood, "commit", "-m", "plant ARCHITECTURE.md")
    tip = _git(hood, "rev-parse", "HEAD").stdout.strip()
    _git(hood, "checkout", "main")
    _git(hood, "cherry-pick", "--ff", tip)
    # Explicit fast-forward avoids relying on identical commit timestamps.
    assert sl.scan_worker(w, str(tmp_path / "local")) is None


def test_scan_unlanded_dedupes_primary_per_workdir(tmp_path):
    """Shared workdir: primary_ahead attributed once."""
    primary = _init_repo(tmp_path / "primary")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare)], check=True, capture_output=True,
    )
    _git(primary, "remote", "add", "origin", str(bare))
    _git(primary, "push", "-u", "origin", "main")
    # Primary only: 2 unpushed commits (shared by both seats).
    for i in range(2):
        (primary / ("p%d.txt" % i)).write_text("p%d\n" % i)
        _git(primary, "add", "p%d.txt" % i)
        _git(primary, "commit", "-m", "primary-%d" % i)
    # Seat a: no distinct shift tip work (shift branch at old main?).
    # Create shift branch for ring that is already on origin (ancestor).
    origin_main = _git(primary, "rev-parse", "origin/main").stdout.strip()
    _git(primary, "branch", "workforce/shift/ring", origin_main)
    # Seat b: shift branch with 1 unique patch on top of primary.
    _git(primary, "checkout", "-B", "workforce/shift/stock")
    (primary / "stock.txt").write_text("stock\n")
    _git(primary, "add", "stock.txt")
    _git(primary, "commit", "-m", "stock-only")
    _git(primary, "checkout", "main")

    ring, _ = _make_worker(
        tmp_path, name="ring",
        workdir=str(primary),
        contract=str(primary / "c-ring.md"),
        prompt=str(primary / "p-ring.md"),
    )
    (primary / "c-ring.md").write_text("c\n")
    (primary / "p-ring.md").write_text("p\n")
    stock, _ = _make_worker(
        tmp_path, name="stock",
        workdir=str(primary),
        contract=str(primary / "c-stock.md"),
        prompt=str(primary / "p-stock.md"),
    )
    (primary / "c-stock.md").write_text("c\n")
    (primary / "p-stock.md").write_text("p\n")

    # Without dedup, ring+stock would each carry primary_ahead=2 → total 4+.
    rows = sl.scan_unlanded(
        None,  # type: ignore[arg-type]
        str(tmp_path / "local"),
        workers=[ring, stock],
    )
    by = {r["worker"]: r for r in rows}
    assert "ring" in by
    assert by["ring"]["surface"] == "primary"
    assert by["ring"]["primary_ahead"] == 2
    assert by["ring"]["commits_ahead"] == 2
    # stock is later alphabetically? ring < stock, so ring keeps primary.
    assert "stock" in by
    assert by["stock"]["primary_ahead"] == 0
    assert by["stock"]["surface"] == "shift_branch"
    assert by["stock"]["shift_ahead"] == 1
    assert by["stock"]["commits_ahead"] == 1
    # Rollup total is 2 + 1, not 2 + max(1,2) + 2.
    assert sum(r["commits_ahead"] for r in rows) == 3


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
    assert row.get("diverged") is False  # main is ancestor — FF-able


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


def test_is_diverged_neither_ancestor(tmp_path):
    """Sibling commits: neither SHA is an ancestor of the other."""
    hood = _init_repo(tmp_path / "r")
    base = _git(hood, "rev-parse", "HEAD").stdout.strip()
    (hood / "a.txt").write_text("a\n")
    _git(hood, "add", "a.txt")
    _git(hood, "commit", "-m", "a")
    a = _git(hood, "rev-parse", "HEAD").stdout.strip()
    _git(hood, "reset", "--hard", base)
    (hood / "b.txt").write_text("b\n")
    _git(hood, "add", "b.txt")
    _git(hood, "commit", "-m", "b")
    b = _git(hood, "rev-parse", "HEAD").stdout.strip()
    assert sl.is_diverged(str(hood), a, b) is True
    assert sl.is_diverged(str(hood), a, a) is False
    assert sl.is_diverged(str(hood), base, a) is False  # base is ancestor of a


def test_scan_worker_flags_diverged_shift(tmp_path):
    """Shift tip and main each have unique commits — not FF-able."""
    w, hood = _make_worker(tmp_path, name="nala")
    _git(hood, "checkout", "-B", "workforce/shift/nala")
    (hood / "nala.txt").write_text("nala\n")
    _git(hood, "add", "nala.txt")
    _git(hood, "commit", "-m", "nala-only")
    _git(hood, "checkout", "main")
    (hood / "main.txt").write_text("main\n")
    _git(hood, "add", "main.txt")
    _git(hood, "commit", "-m", "main-only")
    row = sl.scan_worker(w, str(tmp_path / "local"))
    assert row is not None
    assert row["worker"] == "nala"
    assert row["diverged"] is True
    assert row["shift_ahead"] == 1
    assert row["landing_ahead"] >= 1
    assert "shift/nala" in row["branch"]


def test_scan_worker_diverged_even_when_patches_equivalent(tmp_path):
    """Split history with cherry-equivalent trees is still DIVERGED.

    FF-ability is ancestry. Doctor used to return None (clear) when
    `git cherry` reported 0 unique patches, hiding a non-fast-forward wall.
    """
    w, hood = _make_worker(tmp_path, name="nala")
    main_base = _git(hood, "rev-parse", "HEAD").stdout.strip()
    (hood / "ARCHITECTURE.md").write_text("# arch\n")
    _git(hood, "add", "ARCHITECTURE.md")
    tree = _git(hood, "write-tree").stdout.strip()
    shift_tip = _git(
        hood, "commit-tree", tree, "-p", main_base, "-m", "plant (shift)",
    ).stdout.strip()
    main_tip = _git(
        hood, "commit-tree", tree, "-p", main_base, "-m", "plant (main)",
    ).stdout.strip()
    _git(hood, "update-ref", "refs/heads/workforce/shift/nala", shift_tip)
    _git(hood, "update-ref", "refs/heads/main", main_tip)
    assert sl.patches_ahead(str(hood), shift_tip, main_tip) == 0
    assert sl.is_diverged(str(hood), shift_tip, main_tip) is True
    row = sl.scan_worker(w, str(tmp_path / "local"))
    assert row is not None
    assert row["diverged"] is True
    assert row["commits_ahead"] == 0
    assert row["shift_ahead"] == 0


def test_format_report_diverged_does_not_advise_ff_push():
    """Diverged-only rollup must not say push-when-FF-able."""
    dirty = sl.format_report([{
        "worker": "nala",
        "branch": "workforce/shift/nala",
        "landing_ref": "origin/main",
        "commits_ahead": 78,
        "surface": "shift_branch",
        "diverged": True,
        "landing_ahead": 72,
    }])
    assert "DIVERGED" in dirty
    assert "nala" in dirty
    assert "union" in dirty
    assert "not FF-able" in dirty
    # The FF-only next-step is for non-diverged seats only.
    assert "when FF-able" not in dirty


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
    assert "unique patch" in dirty
    assert "git push origin HEAD:main" in dirty


def test_format_report_local_only_landing_ref_gives_merge_advice():
    """wf-196: no origin (oneseo-pos, recipes) — advise a local merge, not a push."""
    dirty = sl.format_report([{
        "worker": "binx",
        "branch": "workforce/shift/binx",
        "landing_ref": "refs/heads/main",
        "commits_ahead": 1,
        "surface": "shift_branch",
    }])
    assert "git push origin" not in dirty
    assert "merge --ff-only" in dirty
    assert "no `origin` remote" in dirty


def test_format_report_mixes_remote_and_local_hints():
    """One rollup can span a GitHub-backed project and a local-only one."""
    dirty = sl.format_report([
        {
            "worker": "salem",
            "branch": "workforce/shift/salem",
            "landing_ref": "origin/main",
            "commits_ahead": 2,
            "surface": "shift_branch",
        },
        {
            "worker": "stock",
            "branch": "workforce/shift/stock",
            "landing_ref": "refs/heads/main",
            "commits_ahead": 1,
            "surface": "shift_branch",
        },
    ])
    assert "git push origin HEAD:main" in dirty
    assert "merge --ff-only" in dirty


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

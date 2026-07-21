"""Engine mechanics — every RUNNER_SPEC MUST, verified without burning tokens."""

import json
import os
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import engine  # noqa: E402
from workforce.roster import Roster, RosterError, Worker, load  # noqa: E402


def make_worker(tmp_path, **over):
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)
    contract = tmp_path / "CONTRACT.md"
    prompt = tmp_path / "prompt.md"
    contract.write_text("# contract v1\n")
    prompt.write_text("do one slice\n")
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"ok": True, "count": 3}))
    spec = dict(
        name="tester", workdir=str(workdir), contract=str(contract),
        prompt=str(prompt), identity="tester-id",
        command=["/bin/sh", "-c", "exit 0"],
        queue_url="file://" + str(queue), budget_secs=5, min_free_mb=1,
    )
    spec.update(over)
    return Worker(**spec)


def ledger_text(tmp_path):
    p = tmp_path / "local" / "ledger" / "tester.log"
    return p.read_text() if p.exists() else ""


def local(tmp_path):
    return str(tmp_path / "local")


def test_dispatch_success_records_start_done_with_law_hashes(tmp_path):
    w = make_worker(tmp_path)
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "START" in text and "DONE" in text and "STOP" in text
    assert "contract_sha=" in text and "prompt_sha=" in text  # §6 law-version pins
    assert "identity=tester-id" in text and "queue=3" in text


def test_law_change_changes_pinned_hash(tmp_path):
    w = make_worker(tmp_path)
    engine.dispatch(w, local(tmp_path))
    first = [l for l in ledger_text(tmp_path).splitlines() if " START " in l][0]
    (tmp_path / "CONTRACT.md").write_text("# contract v2 — tightened\n")
    engine.dispatch(w, local(tmp_path))
    second = [l for l in ledger_text(tmp_path).splitlines() if " START " in l][1]
    sha = lambda line: [t for t in line.split() if t.startswith("contract_sha=")][0]
    assert sha(first) != sha(second)


def test_dry_run_spawns_nothing(tmp_path):
    marker = tmp_path / "ran"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "touch %s" % marker])
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    assert not marker.exists()
    assert "dry_run=1" in ledger_text(tmp_path)


def test_empty_queue_skips_cleanly(tmp_path):
    w = make_worker(tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({"ok": True, "count": 0}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "SKIP" in ledger_text(tmp_path) and "queue empty" in ledger_text(tmp_path)


def test_unreachable_desk_is_infra_error(tmp_path):
    w = make_worker(tmp_path, queue_url="file://" + str(tmp_path / "missing.json"))
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "ERROR" in ledger_text(tmp_path) and "desk unreachable" in ledger_text(tmp_path)


def test_missing_cli_skips(tmp_path):
    w = make_worker(tmp_path, command=["definitely-not-a-real-cli-xyz"])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "not installed" in ledger_text(tmp_path)


def test_lock_held_skips(tmp_path):
    w = make_worker(tmp_path)
    lock_dir = tmp_path / "local" / "locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "tester.lock").mkdir()
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "lock held" in ledger_text(tmp_path)


def test_budget_kill_is_infra_error(tmp_path):
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "sleep 30"], budget_secs=1)
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "killed at budget" in ledger_text(tmp_path)


def test_lock_released_after_shift(tmp_path):
    w = make_worker(tmp_path)
    engine.dispatch(w, local(tmp_path))
    assert not (tmp_path / "local" / "locks" / "tester.lock").exists()


def test_child_env_scrubbed_and_identity_set(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_PRODUCT", "leaky")
    monkeypatch.setenv("TP_DEFAULT_PRODUCT", "leaky")
    monkeypatch.setenv("TP_AGENT_ID", "wrong-identity")
    out = tmp_path / "env.txt"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "env > %s" % out])
    assert engine.dispatch(w, local(tmp_path)) == 0
    env_text = out.read_text()
    assert "TP_AGENT_ID=tester-id" in env_text            # §2: exactly one identity
    assert "TP_PRODUCT=" not in env_text                   # §2: no ambient scope
    assert "TP_DEFAULT_PRODUCT=" not in env_text


def test_predirty_snapshot_written_for_git_workdir(tmp_path):
    w = make_worker(tmp_path)
    subprocess.run(["git", "-C", w.workdir, "init", "-q"], check=True)
    (tmp_path / "hood" / "wip.txt").write_text("citizen WIP\n")
    engine.dispatch(w, local(tmp_path))
    snap = tmp_path / "local" / "run" / "predirty-tester.txt"
    assert snap.exists() and "wip.txt" in snap.read_text()


def test_model_pin_substitution_and_unpinned_drop(tmp_path):
    w = make_worker(tmp_path, model="test-model-1")
    argv = engine._build_argv(w, "P")
    assert argv == ["/bin/sh", "-c", "exit 0"]
    w2 = make_worker(tmp_path, command=["cli", "--model", "{model}", "-p", "{prompt_text}"],
                     model="m1")
    assert engine._build_argv(w2, "brief") == ["cli", "--model", "m1", "-p", "brief"]
    w3 = make_worker(tmp_path, command=["cli", "--model={model}", "-p", "{prompt_text}"], model="")
    assert engine._build_argv(w3, "brief") == ["cli", "-p", "brief"]
    # the paired-flag form must not leave an orphaned --model behind
    # (incident 2026-07-14: claude parsed '-p' as the model name)
    w4 = make_worker(tmp_path, command=["cli", "--model", "{model}", "-p", "{prompt_text}"],
                     model="")
    assert engine._build_argv(w4, "brief") == ["cli", "-p", "brief"]
    w5 = make_worker(tmp_path, command=["cli", "--model", "{model}", "-p", "{prompt_text}"],
                     model="m9")
    assert engine._build_argv(w5, "brief") == ["cli", "--model", "m9", "-p", "brief"]


def test_roster_env_path_governs_cli_preflight(tmp_path):
    """A roster env PATH must satisfy preflight — the daemon runs on a bare
    launchd PATH, so vendor CLI locations are roster data like every seam."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "fake-vendor-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    w = make_worker(tmp_path, command=["fake-vendor-cli"])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "not installed" in ledger_text(tmp_path)  # not on ambient PATH
    w2 = make_worker(tmp_path, command=["fake-vendor-cli"],
                     env={"PATH": "%s:/usr/bin:/bin" % bindir})
    assert engine.dispatch(w2, local(tmp_path)) == 0
    assert "DONE" in ledger_text(tmp_path)           # found via roster PATH


def test_roster_rejects_duplicate_identity(tmp_path):
    p = tmp_path / "roster.json"
    w = {"workdir": ".", "contract": "c", "prompt": "p", "identity": "same",
         "command": ["x"]}
    p.write_text(json.dumps({"workers": {"a": w, "b": dict(w)}}))
    with pytest.raises(RosterError, match="one worker, one identity"):
        load(str(p))


def test_roster_rejects_unknown_fields_and_bad_kind(tmp_path):
    p = tmp_path / "roster.json"
    p.write_text(json.dumps({"workers": {"a": {
        "workdir": ".", "contract": "c", "prompt": "p", "identity": "i",
        "command": ["x"], "sudo": True}}}))
    with pytest.raises(RosterError, match="unknown fields"):
        load(str(p))


def test_vendor_limit_exit_classifies_reason(tmp_path):
    """rc != 0 with 402 in output → reason 'vendor limit: …', not bare 'agent exit'."""
    w = make_worker(tmp_path, command=[
        "/bin/sh", "-c",
        "echo '402 Payment Required: usage balance exhausted'; exit 1",
    ])
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "vendor limit:" in text
    assert "agent exit" not in text


def test_vendor_limit_exit_with_429(tmp_path):
    """429 rate-limit exit → vendor_limit classification."""
    w = make_worker(tmp_path, command=[
        "/bin/sh", "-c",
        "echo 'error: 429 too many requests — rate limit exceeded'; exit 1",
    ])
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "vendor limit:" in ledger_text(tmp_path)


def test_plain_nonzero_exit_stays_agent_exit(tmp_path):
    """A generic non-zero exit with no vendor-limit output stays 'agent exit'."""
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "echo 'unrelated error'; exit 2"])
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "agent exit" in ledger_text(tmp_path)


def test_classify_exit_never_raises_on_missing_file(tmp_path):
    """_classify_exit must not raise when output file is absent."""
    result = engine._classify_exit(str(tmp_path / "nonexistent.out"))
    assert result == "agent exit"


# §6 authority-chain tests

def test_authority_chain_empty_no_required_is_noop(tmp_path):
    """Empty chain + required=False → normal dispatch; chain_len=0 in START."""
    w = make_worker(tmp_path)
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "START" in text and "chain_len=0" in text
    assert "NO_AUTHORITY_CHAIN" not in text


def test_authority_chain_required_empty_chain_errors(tmp_path):
    """authority_chain_required=True + empty chain → ERROR NO_AUTHORITY_CHAIN before START."""
    w = make_worker(tmp_path, authority_chain_required=True)
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "NO_AUTHORITY_CHAIN" in text
    assert "START" not in text


def test_authority_chain_populated_pins_in_start(tmp_path):
    """Populated chain → chain_len and chain_sha appear in START; files read at dispatch."""
    law = tmp_path / "city-law.md"
    law.write_text("# L0 city law\n")
    w = make_worker(tmp_path, authority_chain=[str(law)])
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "chain_len=1" in text
    assert "chain_sha=" in text


def test_authority_chain_multiple_files_all_pinned(tmp_path):
    """Multiple chain files → combined sha, chain_len=N."""
    l0 = tmp_path / "l0.md"
    l1 = tmp_path / "l1.md"
    l0.write_text("# L0\n")
    l1.write_text("# L1\n")
    w = make_worker(tmp_path, authority_chain=[str(l0), str(l1)])
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "chain_len=2" in text
    assert "chain_sha=" in text


def test_authority_chain_missing_file_errors(tmp_path):
    """Chain entry pointing to a missing file → ERROR before START."""
    w = make_worker(tmp_path, authority_chain=[str(tmp_path / "ghost.md")])
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "authority chain file unreadable" in text
    assert "START" not in text


def test_authority_chain_paths_exposed_in_env(tmp_path):
    """WORKFORCE_AUTHORITY_CHAIN_PATHS set in child env when chain is non-empty."""
    law = tmp_path / "law.md"
    law.write_text("# law\n")
    out = tmp_path / "env.txt"
    w = make_worker(tmp_path, authority_chain=[str(law)],
                    command=["/bin/sh", "-c", "env > %s" % out])
    assert engine.dispatch(w, local(tmp_path)) == 0
    env_text = out.read_text()
    assert "WORKFORCE_AUTHORITY_CHAIN_PATHS=" in env_text
    assert str(law) in env_text


def test_authority_chain_text_substituted_in_argv(tmp_path):
    """'{chain_text}' in command template is replaced with concatenated chain content."""
    law = tmp_path / "law.md"
    law.write_text("CHAIN_SENTINEL\n")
    out = tmp_path / "argv.txt"
    w = make_worker(tmp_path, authority_chain=[str(law)],
                    command=["/bin/sh", "-c", "echo '{chain_text}' > %s" % out,
                             "{chain_text}"])
    # build_argv directly
    entries = engine._load_chain([str(law)])
    chain_text = "\n\n".join(t for t, _ in entries)
    argv = engine._build_argv(w, "brief", chain_text=chain_text)
    assert any("CHAIN_SENTINEL" in tok for tok in argv)


def test_authority_chain_dry_run_loads_and_pins(tmp_path):
    """Dry-run still reads chain files and pins chain_sha in START."""
    law = tmp_path / "law.md"
    law.write_text("# law\n")
    w = make_worker(tmp_path, authority_chain=[str(law)])
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    text = ledger_text(tmp_path)
    assert "chain_len=1" in text and "chain_sha=" in text and "dry_run=1" in text


def test_authority_chain_no_paths_in_env_when_empty(tmp_path):
    """When chain is empty, WORKFORCE_AUTHORITY_CHAIN_PATHS is NOT set in child env."""
    out = tmp_path / "env.txt"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "env > %s" % out])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "WORKFORCE_AUTHORITY_CHAIN_PATHS" not in out.read_text()


# §8 ghost audit

def test_ghost_audit_fires_and_logs_ghost_event(tmp_path):
    """Populated ghost_audit runs before START; GHOST event with rc=0 is logged."""
    marker = tmp_path / "ghost_ran"
    w = make_worker(tmp_path, ghost_audit=["/bin/sh", "-c", "echo 'no orphans'; touch %s" % marker])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert marker.exists(), "ghost audit command did not run"
    text = ledger_text(tmp_path)
    assert "GHOST" in text and "rc=0" in text
    ghost_line = next(l for l in text.splitlines() if " GHOST " in l)
    start_line = next(l for l in text.splitlines() if " START " in l)
    assert ghost_line < start_line, "GHOST must precede START in the ledger"


def test_ghost_audit_empty_is_noop(tmp_path):
    """Empty ghost_audit (default) produces no GHOST event."""
    w = make_worker(tmp_path)
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "GHOST" not in ledger_text(tmp_path)


def test_ghost_audit_nonzero_rc_warns_not_aborts(tmp_path):
    """Nonzero ghost_audit rc → GHOST + WARN in ledger; shift still completes."""
    w = make_worker(tmp_path, ghost_audit=["/bin/sh", "-c", "echo 'orphan: t-1245'; exit 1"])
    assert engine.dispatch(w, local(tmp_path)) == 0  # shift ran, not aborted
    text = ledger_text(tmp_path)
    assert "GHOST" in text and "rc=1" in text
    assert "WARN" in text and "ghost-audit" in text
    assert "START" in text and "DONE" in text


def test_ghost_audit_skipped_in_dry_run(tmp_path):
    """Dry-run must not spawn the ghost audit command."""
    marker = tmp_path / "ghost_ran"
    w = make_worker(tmp_path, ghost_audit=["/bin/sh", "-c", "touch %s" % marker])
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    assert not marker.exists(), "ghost audit must not spawn in dry-run"
    assert "GHOST" not in ledger_text(tmp_path)


# §6 scope enforcement

def test_scope_home_not_set_always_dispatches(tmp_path):
    """No scope_home → no scope check; dispatch proceeds normally."""
    w = make_worker(tmp_path)
    assert w.scope_home == ""
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "SCOPE_DENY" not in ledger_text(tmp_path)


def test_scope_home_allow_within_home(tmp_path):
    """workdir under scope_home → allowed; no SCOPE_DENY."""
    home = tmp_path / "home"
    home.mkdir()
    hood = home / "neighborhood"
    hood.mkdir()
    w = make_worker(tmp_path, workdir=str(hood), scope_home=str(home))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "SCOPE_DENY" not in ledger_text(tmp_path)
    assert "DONE" in ledger_text(tmp_path)


def test_scope_home_deny_cross_cabinet(tmp_path):
    """workdir outside scope_home with no grants → SCOPE_DENY, rc=1, no START."""
    home = tmp_path / "home-A"
    home.mkdir()
    cross = tmp_path / "cabinet-B"
    cross.mkdir()
    w = make_worker(tmp_path, workdir=str(cross), scope_home=str(home))
    assert engine.dispatch(w, local(tmp_path)) == 1
    text = ledger_text(tmp_path)
    assert "SCOPE_DENY" in text
    assert "START" not in text


def test_scope_deny_logs_workdir_and_home(tmp_path):
    """SCOPE_DENY ledger entry includes workdir= and home= fields."""
    home = tmp_path / "home-A"
    home.mkdir()
    cross = tmp_path / "cabinet-B"
    cross.mkdir()
    w = make_worker(tmp_path, workdir=str(cross), scope_home=str(home))
    engine.dispatch(w, local(tmp_path))
    text = ledger_text(tmp_path)
    assert "workdir=" in text and "home=" in text


def test_scope_perimeter_grant_allows_cross_cabinet(tmp_path):
    """workdir outside scope_home but listed in perimeter_grants → allowed."""
    home = tmp_path / "home-A"
    home.mkdir()
    cross = tmp_path / "cabinet-B"
    cross.mkdir()
    w = make_worker(tmp_path, workdir=str(cross), scope_home=str(home),
                    perimeter_grants=[str(cross)])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "SCOPE_DENY" not in ledger_text(tmp_path)
    assert "DONE" in ledger_text(tmp_path)


def test_scope_check_fires_in_dry_run(tmp_path):
    """Scope enforcement is not skipped during dry-run — misconfiguration must be caught."""
    home = tmp_path / "home-A"
    home.mkdir()
    cross = tmp_path / "cabinet-B"
    cross.mkdir()
    w = make_worker(tmp_path, workdir=str(cross), scope_home=str(home))
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 1
    assert "SCOPE_DENY" in ledger_text(tmp_path)

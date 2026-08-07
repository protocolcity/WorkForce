"""Host-mutation ghost-audit — pure scan + dry-run, no live desk burn."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import engine, host_audit  # noqa: E402


# --- pure helper (shared with dispatch) ------------------------------------


def test_tier2_mutation_hit_kickstart_live_label():
    text = "launchctl kickstart -k gui/501/com.ticketingprotocol.server"
    hit = engine.tier2_mutation_hit(text)
    assert hit is not None
    # First matching block wins (label family often before launchctl verb).
    assert "ticketingprotocol" in hit or "launchctl" in hit


def test_tier2_mutation_hit_clean_progress():
    assert engine.tier2_mutation_hit(
        "Progress: landed tests for capacity dry-run — salem"
    ) is None


def test_tier2_mutation_hit_allowlisted_test_label():
    assert engine.tier2_mutation_hit(
        "launchctl bootstrap gui/501 com.protocolcity.suite.test.local"
    ) is None


def test_tier2_mutation_hit_empty():
    assert engine.tier2_mutation_hit("") is None
    assert engine.tier2_mutation_hit(None) is None  # type: ignore[arg-type]


# --- claim surface scan ----------------------------------------------------


def test_scan_comment_with_kickstart_hits():
    comments = [
        {"id": 1, "body": "Owner: lili\nPlan: fix desk"},
        {
            "id": 2,
            "body": (
                "Progress: ran launchctl kickstart -k "
                "gui/501/com.ticketingprotocol.server to revive desk"
            ),
        },
    ]
    hit = host_audit.scan_claim_surfaces(
        "desk flaky", "investigate", comments,
    )
    assert hit is not None
    pattern, surface = hit
    assert "ticketingprotocol" in pattern or "launchctl" in pattern
    assert surface.startswith("comment:")


def test_scan_clean_progress_clear():
    comments = [
        {"id": 10, "body": "Owner: salem\nPlan: pure helper + CLI"},
        {"id": 11, "body": "Progress: pytest green — salem"},
    ]
    assert host_audit.scan_claim_surfaces(
        "wf-160 slice 2", "implement host-audit", comments,
    ) is None


def test_scan_allowlisted_test_label_clear():
    comments = [{
        "id": 3,
        "body": "spawned com.protocolcity.suite.test.local for fixture",
    }]
    assert host_audit.scan_claim_surfaces("t", "d", comments) is None


def test_scan_skips_prior_blocked_comment_noise():
    """Auditor's own Blocked body quotes the pattern — must not re-hit."""
    comments = [
        {
            "id": 9,
            "body": host_audit.build_blocked_body(
                r"\blaunchctl\s+(?:bootstrap|bootout|load|unload|kickstart|"
                r"enable|disable|kill|start|stop)\b",
                "comment:8",
                "wf-99",
            ),
        },
    ]
    assert host_audit.scan_claim_surfaces("t", "d", comments) is None


def test_already_host_mutation_blocked():
    comments = [{
        "body": "Blocked: Host-mutation ghost-audit — x\n"
                "Next step: stage only",
    }]
    assert host_audit.already_host_mutation_blocked(comments) is True
    assert host_audit.already_host_mutation_blocked(
        [{"body": "Progress: ok"}]
    ) is False


# --- gate exception --------------------------------------------------------


def test_has_founder_host_gate_title():
    tasks = [{"id": "wf-1", "title": "FOUNDER · host: restart desk", "gate_type": "human"}]
    assert host_audit.has_founder_host_gate(tasks) is True


def test_has_founder_host_gate_absent():
    tasks = [{"id": "wf-2", "title": "fix typo in board", "gate_type": "human"}]
    assert host_audit.has_founder_host_gate(tasks) is False


def test_evaluate_ungated_would_block():
    task = {
        "id": "wf-372",
        "status": "in_progress",
        "title": "desk down",
        "description": "",
        "comments": [{
            "id": 1,
            "body": "ran launchctl kickstart gui/501/com.ticketingprotocol.server",
        }],
    }
    ev = host_audit.evaluate_claim(task, gated=False)
    assert ev["action"] == "would_block"
    assert "Blocked:" in ev["body"]
    assert "Next step:" in ev["body"]
    assert ev["surface"].startswith("comment:")


def test_evaluate_gated_report_only():
    task = {
        "id": "wf-372",
        "status": "in_progress",
        "title": "desk down",
        "description": "",
        "comments": [{
            "id": 1,
            "body": "ran launchctl kickstart gui/501/com.ticketingprotocol.server",
        }],
    }
    ev = host_audit.evaluate_claim(task, gated=True)
    assert ev["action"] == "gated_report"
    assert ev.get("gated") is True
    assert "body" not in ev or not ev.get("body")


def test_evaluate_already_blocked_idempotent():
    task = {
        "id": "wf-1",
        "title": "x",
        "description": "",
        "comments": [{
            "body": "Blocked: Host-mutation ghost-audit — "
                    "tier-2 pattern launchctl found in comment:1\nNext step: stage",
        }],
    }
    ev = host_audit.evaluate_claim(task, gated=False)
    assert ev["action"] == "already_blocked"


def test_evaluate_clear():
    task = {
        "id": "wf-2",
        "title": "docs only",
        "description": "no host work",
        "comments": [{"body": "Owner: salem\nPlan: design"}],
    }
    assert host_audit.evaluate_claim(task, gated=False)["action"] == "clear"


# --- audit_product dry-run / hermetic (no network on pure path) ------------


def test_audit_product_dry_run_would_block(monkeypatch):
    """Synthetic wl-372 class: ungated hit → would_block, no desk write."""
    claim = {
        "id": "wf-372",
        "status": "in_progress",
        "title": "revive desk",
        "description": "",
        "labels": ["worker:lili"],
        "comments": [{
            "id": 7,
            "body": (
                "Progress: launchctl kickstart -k "
                "gui/501/com.ticketingprotocol.server"
            ),
        }],
    }
    posts = []

    monkeypatch.setattr(host_audit, "list_open_founder_host_gates", lambda *a, **k: [])
    monkeypatch.setattr(host_audit, "list_claim_tasks", lambda *a, **k: [claim])
    monkeypatch.setattr(
        host_audit, "_fetch_task", lambda *a, **k: claim,
    )
    monkeypatch.setattr(
        host_audit, "_post_blocked",
        lambda *a, **k: posts.append(a) or {"ok": True},
    )

    summary = host_audit.audit_product("workforce", dry_run=True)
    assert summary["ok"] is True
    assert summary["dry_run"] is True
    assert summary["ungated_hits"] == 1
    assert summary["would_block"] == 1
    assert summary["blocked"] == 0
    assert posts == []


def test_audit_product_gated_no_block(monkeypatch):
    claim = {
        "id": "wf-372",
        "status": "in_progress",
        "title": "revive desk",
        "description": "",
        "comments": [{
            "id": 7,
            "body": "launchctl kickstart gui/501/com.ticketingprotocol.server",
        }],
    }
    gate = {
        "id": "wf-900",
        "title": "FOUNDER · host: restart desk",
        "gate_type": "human",
        "status": "backlog",
    }
    posts = []
    monkeypatch.setattr(
        host_audit, "list_open_founder_host_gates", lambda *a, **k: [gate],
    )
    monkeypatch.setattr(host_audit, "list_claim_tasks", lambda *a, **k: [claim])
    monkeypatch.setattr(host_audit, "_fetch_task", lambda *a, **k: claim)
    monkeypatch.setattr(
        host_audit, "_post_blocked",
        lambda *a, **k: posts.append(a) or {"ok": True},
    )

    summary = host_audit.audit_product("workforce", dry_run=True)
    assert summary["founder_host_gate"] is True
    assert summary["gated_reports"] == 1
    assert summary["ungated_hits"] == 0
    assert posts == []


def test_audit_product_live_refused_under_pytest(monkeypatch):
    """Hermetic: --live under pytest still does not POST."""
    claim = {
        "id": "wf-372",
        "status": "in_progress",
        "title": "x",
        "description": "",
        "comments": [{
            "id": 1,
            "body": "launchctl kickstart gui/501/com.ticketingprotocol.server",
        }],
    }
    posts = []
    monkeypatch.setattr(host_audit, "list_open_founder_host_gates", lambda *a, **k: [])
    monkeypatch.setattr(host_audit, "list_claim_tasks", lambda *a, **k: [claim])
    monkeypatch.setattr(host_audit, "_fetch_task", lambda *a, **k: claim)
    monkeypatch.setattr(
        host_audit, "_post_blocked",
        lambda *a, **k: posts.append(a) or {"ok": True},
    )
    monkeypatch.delenv("WORKFORCE_ALLOW_DESK", raising=False)

    summary = host_audit.audit_product("workforce", dry_run=False)
    assert summary["hermetic"] is True
    assert summary["dry_run"] is True
    assert summary["would_block"] == 1
    assert summary["blocked"] == 0
    assert posts == []


def test_audit_product_live_posts_when_allowed(monkeypatch):
    claim = {
        "id": "wf-372",
        "status": "in_progress",
        "title": "x",
        "description": "",
        "comments": [{
            "id": 1,
            "body": "launchctl kickstart gui/501/com.ticketingprotocol.server",
        }],
    }
    posts = []
    monkeypatch.setattr(host_audit, "list_open_founder_host_gates", lambda *a, **k: [])
    monkeypatch.setattr(host_audit, "list_claim_tasks", lambda *a, **k: [claim])
    monkeypatch.setattr(host_audit, "_fetch_task", lambda *a, **k: claim)
    monkeypatch.setattr(
        host_audit, "_post_blocked",
        lambda desk, product, tid, body, author: posts.append(
            (tid, body, author)
        ) or {"ok": True},
    )
    monkeypatch.setattr(host_audit, "desk_writes_allowed", lambda: True)

    summary = host_audit.audit_product(
        "workforce", dry_run=False, author="workforce",
    )
    assert summary["blocked"] == 1
    assert summary["would_block"] == 0
    assert len(posts) == 1
    assert posts[0][0] == "wf-372"
    assert "Blocked:" in posts[0][1]
    assert posts[0][2] == "workforce"


def test_format_receipt_includes_ungated():
    text = host_audit.format_receipt({
        "product": "workforce",
        "dry_run": True,
        "claims_scanned": 1,
        "ungated_hits": 1,
        "results": [{
            "action": "would_block",
            "task_id": "wf-372",
            "pattern": "kickstart",
            "surface": "comment:1",
        }],
    })
    assert "host-audit:" in text
    assert "would_block wf-372" in text
    assert "--live" in text


# --- dispatch still first line of defense (regression via shared helper) ---


def test_dispatch_still_denies_kickstart_argv(tmp_path):
    """Incident class still fails at dispatch without needing host-audit."""
    from tests.test_engine import make_worker, local, ledger_text

    w = make_worker(
        tmp_path,
        command=["launchctl", "kickstart", "-k",
                 "gui/501/com.ticketingprotocol.server"],
    )
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 1
    assert "HOST_MUTATION_DENY" in ledger_text(tmp_path)


def test_cli_host_audit_help():
    from workforce.cli import main
    with pytest.raises(SystemExit) as ei:
        main(["host-audit", "--help"])
    assert ei.value.code == 0

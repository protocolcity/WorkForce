"""wf-167 — CoS daily digest upsert (pure logic + mocked desk)."""

from __future__ import annotations

import pytest

from workforce import digest_upsert as du


def test_digest_title_and_labels():
    assert du.digest_title("2026-08-03") == (
        "Chief-of-staff daily digest · 2026-08-03"
    )
    assert du.digest_day_label("2026-08-03") == "ops:digest:2026-08-03"
    labs = du.digest_labels("workforce", "2026-08-03")
    assert "worker:you" in labs
    assert "you:note" in labs
    assert "ops:digest" in labs
    assert "ops:digest:2026-08-03" in labs
    assert "product:workforce" in labs


def test_task_matches_day_by_day_label():
    t = {
        "id": "wf-9",
        "title": "other",
        "labels": ["ops:digest:2026-08-04", "worker:you"],
        "status": "backlog",
    }
    assert du.task_matches_day(t, "2026-08-04") is True
    assert du.task_matches_day(t, "2026-08-03") is False


def test_task_matches_day_by_canonical_title():
    t = {
        "id": "wf-148",
        "title": "Chief-of-staff daily digest · 2026-08-03",
        "labels": ["product:workforce", "ops:digest", "worker:you", "you:note"],
        "status": "done",
    }
    assert du.task_matches_day(t, "2026-08-03") is True
    assert du.task_matches_day(t, "2026-08-04") is False


def test_task_matches_legacy_ops_digest_hyphen():
    """wf-144 used ops-digest (hyphen) not ops:digest."""
    t = {
        "id": "wf-144",
        "title": "Chief-of-staff daily digest · 2026-08-03",
        "labels": ["worker:you", "you:note", "ops-digest", "product:workforce"],
        "status": "done",
    }
    assert du.task_matches_day(t, "2026-08-03") is True


def test_find_same_day_prefers_open_over_done():
    tasks = [
        {
            "id": "wf-done",
            "title": "Chief-of-staff daily digest · 2026-08-03",
            "labels": ["ops:digest"],
            "status": "done",
            "created_at": "2026-08-03T10:00:00+00:00",
        },
        {
            "id": "wf-open",
            "title": "Chief-of-staff daily digest · 2026-08-03",
            "labels": ["ops:digest"],
            "status": "backlog",
            "created_at": "2026-08-03T12:00:00+00:00",
        },
    ]
    hit = du.find_same_day_digest(tasks, "2026-08-03")
    assert hit is not None
    assert hit["id"] == "wf-open"


def test_find_same_day_reuses_done_when_no_open():
    tasks = [
        {
            "id": "wf-148",
            "title": "Chief-of-staff daily digest · 2026-08-03",
            "labels": ["ops:digest", "worker:you", "you:note"],
            "status": "done",
            "created_at": "2026-08-03T10:00:00+00:00",
        },
        {
            "id": "wf-other",
            "title": "Chief-of-staff daily digest · 2026-08-02",
            "labels": ["ops:digest"],
            "status": "done",
            "created_at": "2026-08-02T10:00:00+00:00",
        },
    ]
    hit = du.find_same_day_digest(tasks, "2026-08-03")
    assert hit is not None
    assert hit["id"] == "wf-148"


def test_find_same_day_skips_canceled():
    tasks = [
        {
            "id": "wf-x",
            "title": "Chief-of-staff daily digest · 2026-08-03",
            "labels": ["ops:digest"],
            "status": "canceled",
        },
    ]
    assert du.find_same_day_digest(tasks, "2026-08-03") is None


def test_plan_create_dry_run():
    plan = du.plan_digest_upsert(
        None, "2026-08-04", "## Glance\nhi", project="workforce", dry_run=True,
    )
    assert plan["ok"] is True
    assert plan["action"] == "would_create"
    assert plan["create"]["title"].startswith("Chief-of-staff daily digest")
    assert "ops:digest:2026-08-04" in plan["create"]["labels"]
    assert plan["create"]["description"] == "## Glance\nhi"


def test_plan_update_when_existing():
    existing = {
        "id": "wf-148",
        "status": "done",
        "title": "Chief-of-staff daily digest · 2026-08-03",
    }
    plan = du.plan_digest_upsert(
        existing, "2026-08-03", "updated body", dry_run=True,
    )
    assert plan["action"] == "would_update"
    assert plan["task_id"] == "wf-148"
    assert plan["prior_status"] == "done"
    assert plan["patch"]["description"] == "updated body"


def test_upsert_dry_run_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("desk must not be contacted on dry_run")

    monkeypatch.setattr(du, "_req", boom)
    monkeypatch.setattr(du, "list_digest_candidates", boom)
    receipt = du.upsert_cos_digest(
        "body", day="2026-08-04", dry_run=True,
    )
    assert receipt["ok"] is True
    assert receipt["action"] == "would_create"
    assert receipt["dry_run"] is True


def test_upsert_update_via_candidates_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("HTTP must not run when candidates injected")

    monkeypatch.setattr(du, "_req", boom)
    candidates = [
        {
            "id": "wf-148",
            "title": "Chief-of-staff daily digest · 2026-08-03",
            "labels": ["ops:digest", "worker:you", "you:note"],
            "status": "done",
            "created_at": "2026-08-03T10:00:00+00:00",
        },
    ]
    receipt = du.upsert_cos_digest(
        "new body",
        day="2026-08-03",
        dry_run=True,
        candidates=candidates,
    )
    assert receipt["action"] == "would_update"
    assert receipt["task_id"] == "wf-148"


def test_upsert_live_refused_under_pytest(monkeypatch):
    """Hermetic: dry_run=False still does not touch the desk under pytest."""
    def boom(*a, **k):
        raise AssertionError("desk must not be contacted under pytest")

    monkeypatch.setattr(du, "_req", boom)
    monkeypatch.setattr(du, "list_digest_candidates", boom)
    monkeypatch.delenv("WORKFORCE_ALLOW_DESK", raising=False)
    receipt = du.upsert_cos_digest(
        "body", day="2026-08-04", dry_run=False,
    )
    assert receipt["ok"] is True
    assert receipt["action"] == "would_create"
    assert receipt.get("hermetic") is True


def test_upsert_live_create_mocked(monkeypatch):
    calls = []

    def fake_req(method, url, body=None, timeout=20.0):
        calls.append((method, url, body))
        if method == "POST" and "/api/admin/tasks?" in url:
            return {"ok": True, "task": {"id": "wf-999", "status": "backlog"}}
        return {"ok": True}

    monkeypatch.setattr(du, "_req", fake_req)
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")
    receipt = du.upsert_cos_digest(
        "## Glance\nok",
        day="2026-08-05",
        dry_run=False,
        existing=None,
        candidates=[],  # force create without list
    )
    assert receipt["ok"] is True
    assert receipt["action"] == "created"
    assert receipt["task_id"] == "wf-999"
    assert any(c[0] == "POST" for c in calls)
    create_bodies = [c[2] for c in calls if c[0] == "POST" and c[2]]
    assert create_bodies
    assert "worker:you" in create_bodies[0]["labels"]
    assert "you:note" in create_bodies[0]["labels"]
    assert "ops:digest" in create_bodies[0]["labels"]


def test_upsert_live_update_mocked(monkeypatch):
    calls = []
    existing = {
        "id": "wf-148",
        "title": "Chief-of-staff daily digest · 2026-08-03",
        "labels": ["ops:digest", "worker:you", "you:note"],
        "status": "done",
    }

    def fake_req(method, url, body=None, timeout=20.0):
        calls.append((method, url, body))
        return {"ok": True, "task": existing}

    monkeypatch.setattr(du, "_req", fake_req)
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")
    receipt = du.upsert_cos_digest(
        "patched",
        day="2026-08-03",
        dry_run=False,
        existing=existing,
    )
    assert receipt["action"] == "updated"
    assert receipt["task_id"] == "wf-148"
    assert any(c[0] == "PATCH" for c in calls)
    patches = [c[2] for c in calls if c[0] == "PATCH"]
    assert patches[0]["description"] == "patched"


def test_format_receipt_mentions_action():
    text = du.format_receipt({
        "action": "would_create",
        "day": "2026-08-04",
        "project": "workforce",
        "title": "Chief-of-staff daily digest · 2026-08-04",
        "day_label": "ops:digest:2026-08-04",
        "dry_run": True,
        "labels": ["worker:you", "ops:digest"],
    })
    assert "would_create" in text
    assert "2026-08-04" in text

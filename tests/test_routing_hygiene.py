"""wf-168 — stale needs:routing on done/canceled (doctor report + --repair)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from workforce import routing_hygiene as rh
from workforce import cli


# --- pure filters ---


def test_is_stale_routing_candidate_terminal_only():
    assert rh.is_stale_routing_candidate({
        "id": "wl-1", "status": "done", "labels": ["needs:routing"],
    })
    assert rh.is_stale_routing_candidate({
        "id": "wl-2", "status": "canceled", "labels": ["needs:routing", "other"],
    })
    assert rh.is_stale_routing_candidate({
        "id": "wl-3", "status": "cancelled", "labels": ["needs:routing"],
    })
    # Open work must never strip
    assert not rh.is_stale_routing_candidate({
        "id": "ts-1", "status": "backlog", "labels": ["needs:routing"],
    })
    assert not rh.is_stale_routing_candidate({
        "id": "ts-2", "status": "in_progress", "labels": ["needs:routing"],
    })
    assert not rh.is_stale_routing_candidate({
        "id": "ts-3", "status": "in_review", "labels": ["needs:routing"],
    })
    # Terminal without the label
    assert not rh.is_stale_routing_candidate({
        "id": "wf-1", "status": "done", "labels": ["worker:salem"],
    })
    assert not rh.is_stale_routing_candidate({"id": "x", "status": "done"})


def test_plan_strip_filters_and_sorts():
    tasks = [
        {"id": "b-2", "status": "done", "labels": ["needs:routing"],
         "product": "worklane", "title": "second"},
        {"id": "a-1", "status": "backlog", "labels": ["needs:routing"],
         "product": "worklane", "title": "open — keep"},
        {"id": "a-9", "status": "canceled", "labels": ["needs:routing"],
         "product": "demo", "title": "cancel"},
        {"id": "a-1-dup", "status": "done", "labels": ["other"],
         "product": "worklane"},
    ]
    planned = rh.plan_strip(tasks)
    ids = [r["task_id"] for r in planned]
    assert ids == ["a-9", "b-2"]  # product then id
    assert all(r["status"] in ("done", "canceled") for r in planned)


# --- mocked desk HTTP ---


class _FakeDesk:
    """Minimal in-memory desk for list/products/labels."""

    def __init__(self, products: List[str], tasks: List[dict]):
        self.products = products
        self.tasks = {str(t["id"]): dict(t) for t in tasks}
        self.patches: List[Tuple[str, dict]] = []

    def __call__(
        self,
        method: str,
        url: str,
        body: Optional[dict] = None,
        timeout: float = 0,
    ) -> dict:
        method = method.upper()
        if method == "GET" and url.rstrip("/").endswith("/api/admin/products"):
            return {
                "ok": True,
                "products": [{"slug": s} for s in self.products],
            }
        if method == "GET" and "/api/admin/tasks?" in url:
            return self._list(url)
        if method == "PATCH" and "/labels" in url:
            return self._patch_labels(url, body or {})
        return {"ok": False, "error": "unexpected %s %s" % (method, url)}

    def _list(self, url: str) -> dict:
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(url).query)
        product = (q.get("product") or q.get("project") or [""])[0]
        status = (q.get("status") or [""])[0]
        label = (q.get("label") or [""])[0]
        out = []
        for t in self.tasks.values():
            p = str(t.get("product") or t.get("project") or "")
            if product and p != product:
                continue
            if status and str(t.get("status") or "").lower() != status.lower():
                # American canceled vs cancelled
                if not (
                    status == "canceled"
                    and str(t.get("status") or "").lower() == "cancelled"
                ):
                    continue
            labs = [str(x) for x in (t.get("labels") or [])]
            if label and label not in labs:
                continue
            out.append(dict(t))
        return {"ok": True, "tasks": out}

    def _patch_labels(self, url: str, body: dict) -> dict:
        # .../api/admin/tasks/{id}/labels?product=
        from urllib.parse import parse_qs, unquote, urlsplit

        path = urlsplit(url).path
        # /api/admin/tasks/<id>/labels
        parts = path.rstrip("/").split("/")
        tid = unquote(parts[-2]) if len(parts) >= 2 else ""
        product = (parse_qs(urlsplit(url).query).get("product") or [""])[0]
        self.patches.append((tid, dict(body)))
        t = self.tasks.get(tid)
        if t is None:
            return {"ok": False, "error": "not found"}
        remove = set(str(x) for x in (body.get("remove") or []))
        add = [str(x) for x in (body.get("add") or [])]
        labs = [str(x) for x in (t.get("labels") or []) if str(x) not in remove]
        for a in add:
            if a not in labs:
                labs.append(a)
        t["labels"] = labs
        # Safety assert in tests: never invent other removals
        assert remove <= {rh.STALE_LABEL}
        return {"ok": True, "task": dict(t), "product": product}


def _sample_tasks() -> List[dict]:
    return [
        {
            "id": "wl-290", "status": "canceled", "product": "worklane",
            "labels": ["needs:routing", "parent:x"], "title": "canceled stale",
        },
        {
            "id": "wl-281", "status": "done", "product": "worklane",
            "labels": ["needs:routing"], "title": "done stale",
        },
        {
            "id": "wl-open", "status": "backlog", "product": "worklane",
            "labels": ["needs:routing"], "title": "OPEN — must keep stamp",
        },
        {
            "id": "ts-2369", "status": "done", "product": "demo",
            "labels": ["needs:routing", "worker:you"], "title": "ts done stale",
        },
        {
            "id": "ts-live", "status": "in_progress", "product": "demo",
            "labels": ["needs:routing"], "title": "live claim",
        },
        {
            "id": "pc-clean", "status": "done", "product": "protocolcity",
            "labels": ["worker:figaro"], "title": "already clean",
        },
    ]


def test_scan_report_counts_and_planned_strip_set():
    desk = _FakeDesk(
        ["worklane", "demo", "protocolcity"],
        _sample_tasks(),
    )
    summary = rh.scan_stale_routing(
        desk="http://desk.test",
        products=None,
        repair=False,
        http=desk,
    )
    assert summary["ok"]
    assert summary["dry_run"] is True
    assert summary["total"] == 3
    assert summary["by_product"]["worklane"]["count"] == 2
    assert summary["by_product"]["demo"]["count"] == 1
    assert summary["by_product"]["protocolcity"]["count"] == 0
    planned_ids = {r["task_id"] for r in summary["planned"]}
    assert planned_ids == {"wl-290", "wl-281", "ts-2369"}
    assert "wl-open" not in planned_ids
    assert "ts-live" not in planned_ids
    assert desk.patches == []  # no writes on dry-run

    text = rh.format_report(summary)
    assert "would_strip" in text
    assert "wl-290" in text
    assert "--repair" in text


def test_scan_repair_strips_only_needs_routing(monkeypatch):
    # Allow live path under pytest for this unit test (module-level guard).
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")
    monkeypatch.delenv("WORKFORCE_NO_DESK", raising=False)

    desk = _FakeDesk(["worklane", "demo"], _sample_tasks())
    summary = rh.scan_stale_routing(
        desk="http://desk.test",
        products=["worklane", "demo"],
        repair=True,
        http=desk,
    )
    assert summary["ok"]
    assert summary["effective_repair"] is True
    assert set(summary["stripped"]) == {"wl-281", "wl-290", "ts-2369"}
    # Open tickets untouched
    assert "needs:routing" in desk.tasks["wl-open"]["labels"]
    assert "needs:routing" in desk.tasks["ts-live"]["labels"]
    # Terminal stripped of only needs:routing; other labels remain
    assert "needs:routing" not in desk.tasks["wl-290"]["labels"]
    assert "parent:x" in desk.tasks["wl-290"]["labels"]
    assert "needs:routing" not in desk.tasks["ts-2369"]["labels"]
    assert "worker:you" in desk.tasks["ts-2369"]["labels"]
    # Only remove needs:routing in every PATCH
    for _tid, body in desk.patches:
        assert body.get("remove") == [rh.STALE_LABEL]
        assert body.get("add") == []


def test_repair_blocked_under_hermetic_no_desk(monkeypatch):
    monkeypatch.setenv("WORKFORCE_NO_DESK", "1")
    monkeypatch.delenv("WORKFORCE_ALLOW_DESK", raising=False)
    desk = _FakeDesk(["worklane"], _sample_tasks())
    summary = rh.scan_stale_routing(
        desk="http://desk.test",
        products=["worklane"],
        repair=True,
        http=desk,
    )
    assert summary["hermetic"] is True
    assert summary["effective_repair"] is False
    assert summary["dry_run"] is True
    assert summary["stripped"] == []
    assert desk.patches == []
    assert "needs:routing" in desk.tasks["wl-281"]["labels"]
    text = rh.format_report(summary)
    assert "hermetic" in text.lower() or "dry-run" in text


def test_list_stale_routing_client_side_rejects_open():
    """Even if desk ignores status filter, client refuses open rows."""
    def bad_http(method, url, body=None, timeout=0):
        return {
            "ok": True,
            "tasks": [
                {
                    "id": "open-1", "status": "backlog",
                    "labels": ["needs:routing"], "product": "worklane",
                },
                {
                    "id": "done-1", "status": "done",
                    "labels": ["needs:routing"], "product": "worklane",
                },
            ],
        }

    rows = rh.list_stale_routing(
        "http://desk.test", "worklane", http=bad_http,
    )
    assert [r["id"] for r in rows] == ["done-1"]


def test_doctor_cli_skips_under_no_desk(tmp_path, monkeypatch, capsys):
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    roster.parent.mkdir(parents=True)
    roster.write_text(json.dumps({
        "workers": {
            "alpha": {
                "kind": "job",
                "workdir": str(data),
                "contract": str(data / "c.md"),
                "prompt": str(data / "p.md"),
                "identity": "alpha",
                "command": ["true"],
            }
        }
    }))
    process = tmp_path / "PROCESS-stub.md"
    process.write_text(
        "### 5.2) Identity\n\n| Agent id | Who |\n| --- | --- |\n"
        "| `alpha` | test. |\n\n### 5.3) Other\n"
    )
    monkeypatch.setenv("WORKLANE_PROCESS", str(process))
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    monkeypatch.setenv("WORKFORCE_NO_DESK", "1")
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)
    monkeypatch.delenv("WORKFORCE_ALLOW_DESK", raising=False)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Stale needs:routing: skipped (WORKFORCE_NO_DESK)" in out
    assert "doctor: OK" in out


def test_doctor_cli_reports_with_mock(tmp_path, monkeypatch, capsys):
    data = tmp_path / "engine"
    roster = data / "local" / "roster.json"
    roster.parent.mkdir(parents=True)
    roster.write_text(json.dumps({
        "workers": {
            "alpha": {
                "kind": "job",
                "workdir": str(data),
                "contract": str(data / "c.md"),
                "prompt": str(data / "p.md"),
                "identity": "alpha",
                "command": ["true"],
            }
        }
    }))
    process = tmp_path / "PROCESS-stub.md"
    process.write_text(
        "### 5.2) Identity\n\n| Agent id | Who |\n| --- | --- |\n"
        "| `alpha` | test. |\n\n### 5.3) Other\n"
    )
    monkeypatch.setenv("WORKLANE_PROCESS", str(process))
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(data))
    # Opt into desk path under pytest; inject mock via module default http
    # by patching scan to use FakeDesk through monkeypatch on _http_json.
    monkeypatch.setenv("WORKFORCE_ALLOW_DESK", "1")
    monkeypatch.delenv("WORKFORCE_NO_DESK", raising=False)
    monkeypatch.delenv("WORKFORCE_SUITE_ROSTER", raising=False)

    fake = _FakeDesk(["worklane"], _sample_tasks())
    monkeypatch.setattr(rh, "_http_json", fake)
    # scan imports _http_json at call time from engine via default; patch
    # scan's default by wrapping scan_stale_routing to force http=
    real_scan = rh.scan_stale_routing

    def _scan(**kwargs):
        kwargs.setdefault("http", fake)
        kwargs.setdefault("products", ["worklane"])
        return real_scan(**kwargs)

    monkeypatch.setattr(rh, "scan_stale_routing", _scan)
    # cli imports routing_hygiene inside the branch — patch package attr
    import workforce.routing_hygiene as mod
    monkeypatch.setattr(mod, "scan_stale_routing", _scan)

    rc = cli.main(["doctor", "--desk", "http://desk.test", "--product", "worklane"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "would_strip" in captured.out or "worklane: 2" in captured.out
    assert "doctor: OK" in captured.out
    assert fake.patches == []

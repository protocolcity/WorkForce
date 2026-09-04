"""wf-218 / wf-227 / wf-229 / wf-230 — live desk_base_url + engine_port."""

import os
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce._utils import (  # noqa: E402
    _DEFAULT_DESK_FALLBACK,
    _DEFAULT_ENGINE_PORT,
    _DESK_ENV_KEYS,
    desk_base_url,
    engine_api_url,
    engine_port,
)
from workforce.daemon import Daemon  # noqa: E402


_KEYS = _DESK_ENV_KEYS


@pytest.fixture
def _clear_desk_env(monkeypatch):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)


def test_desk_base_url_fallback_when_unset(_clear_desk_env):
    assert desk_base_url() == _DEFAULT_DESK_FALLBACK


def test_wl_desk_url_wins_over_legacy(_clear_desk_env, monkeypatch):
    monkeypatch.setenv("WORKFORCE_DESK", "http://legacy:1")
    monkeypatch.setenv("WORKFORCE_DESK", "http://legacy:2")
    monkeypatch.setenv("TP_DESK_URL", "http://drop:3")
    monkeypatch.setenv("WL_DESK_URL", "http://drop:4")
    assert desk_base_url() == "http://drop:4"


def test_tp_desk_url_when_wl_unset(_clear_desk_env, monkeypatch):
    monkeypatch.setenv("WORKFORCE_DESK", "http://legacy:2")
    monkeypatch.setenv("TP_DESK_URL", "http://drop:3")
    assert desk_base_url() == "http://drop:3"


def test_workforce_desk_when_drop_family_unset(_clear_desk_env, monkeypatch):
    monkeypatch.setenv("WORKFORCE_DESK", "http://legacy:1")
    monkeypatch.setenv("WORKFORCE_DESK", "http://legacy:2")
    assert desk_base_url() == "http://legacy:2"


def test_workforce_desk_last_legacy(_clear_desk_env, monkeypatch):
    monkeypatch.setenv("WORKFORCE_DESK", "http://legacy:1")
    assert desk_base_url() == "http://legacy:1"


def test_empty_string_skips_to_next(_clear_desk_env, monkeypatch):
    monkeypatch.setenv("WL_DESK_URL", "   ")
    monkeypatch.setenv("TP_DESK_URL", "")
    monkeypatch.setenv("WORKFORCE_DESK", "http://legacy:2")
    assert desk_base_url() == "http://legacy:2"


def test_poll_desk_events_reads_wl_desk_url_live(tmp_path, monkeypatch):
    """Daemon event poll must not ignore WL_DESK_URL (the split-brain)."""
    captured = []

    class _Resp:
        def read(self):
            return b'{"events":[],"cursor":7}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured.append(req.full_url)
        return _Resp()

    monkeypatch.setattr("workforce.daemon.urllib.request.urlopen", fake_urlopen)
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WL_DESK_URL", "http://desk.test:9999")
    local = tmp_path / "local"
    local.mkdir()
    d = Daemon(str(tmp_path), str(local))
    events, cursor = d._poll_desk_events("workforce", 0)
    assert events == []
    assert cursor == 7
    assert captured, "urlopen was not called"
    assert captured[0].startswith("http://desk.test:9999/api/events?")
    assert "project=workforce" in captured[0]


def test_poll_desk_events_falls_back_to_workforce_desk(tmp_path, monkeypatch):
    captured = []

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured.append(req.full_url)
        return _Resp()

    monkeypatch.setattr("workforce.daemon.urllib.request.urlopen", fake_urlopen)
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WORKFORCE_DESK", "http://board-join:8799")
    local = tmp_path / "local"
    local.mkdir()
    d = Daemon(str(tmp_path), str(local))
    d._poll_desk_events("workforce", 0)
    assert captured[0].startswith("http://board-join:8799/api/events?")


# --- wf-227 live desk_base_url() at drop / board use time ---


def test_no_import_time_desk_snapshot():
    """DEFAULT_DESK / DESK must not freeze desk_base_url() at import."""
    root = Path(__file__).resolve().parents[1] / "workforce"
    cap = (root / "capacity.py").read_text(encoding="utf-8")
    roster = (root / "api" / "roster.py").read_text(encoding="utf-8")
    assert "DEFAULT_DESK = desk_base_url()" not in cap
    assert "DESK = desk_base_url()" not in roster
    assert "def _desk()" in roster
    assert 'DEFAULT_PORT = int(os.environ.get("WORKFORCE_PORT")' not in roster


def test_drop_capacity_fallback_reads_wl_desk_url_live(_clear_desk_env, monkeypatch):
    from workforce import capacity

    monkeypatch.setenv("WL_DESK_URL", "http://desk.test:9999")
    alert = {
        "pool": "claude",
        "inbox_key": "capacity-claude",
        "inbox_label": "inbox-report:workforce:capacity-claude:2026-08-02",
        "glance": "blocked",
        "day": "2026-08-02",
        "project": "workforce",
    }
    receipt = capacity.drop_capacity_for_you(
        alert, report_path="/tmp/r.md", dry_run=True,
    )
    assert receipt["desk"] == "http://desk.test:9999"


def test_digest_upsert_fallback_reads_wl_desk_url_live(_clear_desk_env, monkeypatch):
    from workforce import digest_upsert

    monkeypatch.setenv("WL_DESK_URL", "http://desk.test:9999")
    receipt = digest_upsert.upsert_cos_digest("body", dry_run=True)
    assert receipt["desk"] == "http://desk.test:9999"


def test_scene_tape_desk_reads_wl_desk_url_live(tmp_path, _clear_desk_env, monkeypatch):
    from workforce import board
    import workforce.api.roster as _api_roster

    monkeypatch.setenv("WL_DESK_URL", "http://desk.test:9999")
    monkeypatch.setattr(_api_roster, "_desk_json", lambda path, timeout=None: None)
    local = tmp_path / "local"
    (local / "ledger").mkdir(parents=True)
    tape = board.scene_tape(str(local))
    assert tape["desk"] == "http://desk.test:9999"


def test_desk_json_reads_wl_desk_url_live(_clear_desk_env, monkeypatch):
    captured = []

    def fake_get(url, timeout=5.0):
        captured.append((url, timeout))
        return {"ok": True}

    monkeypatch.setattr("workforce.api.roster._http_get_json", fake_get)
    monkeypatch.setenv("WL_DESK_URL", "http://desk.test:9999")
    import workforce.api.roster as ar

    out = ar._desk_json("/api/dev/activity")
    assert out == {"ok": True}
    assert captured == [("http://desk.test:9999/api/dev/activity", 5.0)]


def test_desk_json_swallows_http_errors(_clear_desk_env, monkeypatch):
    """wf-229: probe helper raises on 4xx; wrapper still returns None."""

    def boom(url, timeout=5.0):
        raise urllib.error.HTTPError(url, 404, "missing", None, None)

    monkeypatch.setattr("workforce.api.roster._http_get_json", boom)
    monkeypatch.setenv("WL_DESK_URL", "http://desk.test:9999")
    import workforce.api.roster as ar

    assert ar._desk_json("/api/dev/activity") is None


def test_desk_json_forwards_timeout(_clear_desk_env, monkeypatch):
    """wf-229 / wf-147: board/scene short bound reaches the probe helper."""
    captured = []

    def fake_get(url, timeout=5.0):
        captured.append(timeout)
        return {}

    monkeypatch.setattr("workforce.api.roster._http_get_json", fake_get)
    monkeypatch.setenv("WL_DESK_URL", "http://desk.test:9999")
    import workforce.api.roster as ar

    assert ar._desk_json("/api/dev/activity", timeout=0.9) == {}
    assert captured == [0.9]


# --- wf-221 engine_api_url (own door; not a desk seam) ---


def test_engine_api_url_default_when_port_unset(monkeypatch):
    monkeypatch.delenv("WORKFORCE_PORT", raising=False)
    assert engine_api_url() == "http://127.0.0.1:%d" % _DEFAULT_ENGINE_PORT


def test_engine_api_url_honors_workforce_port(monkeypatch):
    monkeypatch.setenv("WORKFORCE_PORT", "9111")
    assert engine_api_url() == "http://127.0.0.1:9111"


def test_engine_api_url_explicit_port_wins_over_env(monkeypatch):
    monkeypatch.setenv("WORKFORCE_PORT", "9111")
    assert engine_api_url(9222) == "http://127.0.0.1:9222"


def test_engine_api_url_garbage_env_falls_back(monkeypatch):
    monkeypatch.setenv("WORKFORCE_PORT", "not-a-port")
    assert engine_api_url() == "http://127.0.0.1:%d" % _DEFAULT_ENGINE_PORT


def test_engine_api_url_empty_env_falls_back(monkeypatch):
    monkeypatch.setenv("WORKFORCE_PORT", "  ")
    assert engine_api_url() == "http://127.0.0.1:%d" % _DEFAULT_ENGINE_PORT


def test_engine_port_tracks_workforce_port(monkeypatch):
    monkeypatch.setenv("WORKFORCE_PORT", "9111")
    assert engine_port() == 9111
    assert engine_port(9222) == 9222
    monkeypatch.setenv("WORKFORCE_PORT", "not-a-port")
    assert engine_port() == _DEFAULT_ENGINE_PORT


def test_make_server_binds_live_workforce_port(monkeypatch):
    """wf-230 — bind reads WORKFORCE_PORT at call time, not import snapshot."""
    from workforce import board

    monkeypatch.setenv("WORKFORCE_PORT", "0")
    assert board.DEFAULT_PORT == _DEFAULT_ENGINE_PORT
    httpd = board.make_server(local_root="local")
    try:
        bound = int(httpd.server_address[1])
        assert bound > 0
        assert bound != _DEFAULT_ENGINE_PORT
    finally:
        httpd.server_close()


def test_api_health_reports_listen_port(monkeypatch):
    """wf-230 — /api/health.port is the bound socket, not DEFAULT_PORT."""
    import json
    import threading
    import urllib.request
    from workforce import board

    monkeypatch.setenv("WORKFORCE_PORT", "0")
    httpd = board.make_server(local_root="local")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = int(httpd.server_address[1])
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/api/health" % port, timeout=5
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["port"] == port
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=3)

"""The board — the workforce office, served on its own port.

JSON API for the suite Map (roster + shift ledgers + desk join). Three
data sources, all seams:

  1. The roster + ledgers (this product's own state).
  2. ``launchctl list`` — TRANSITIONAL adapter for the legacy hand-rolled
     lanes; each row disappears as its lane migrates onto the daemon.
  3. The desk's published dev feed (activity + summary) — the desk half of
     the join, consumed over HTTP, never imported.

Own port (default 8797). Glass is the suite Map at :8801/roster — this
process serves ``/api/*`` only.

Package peel of the former ``workforce/board.py`` monolith. Existing
imports stay stable: ``from workforce.board import make_server`` and
``import workforce.board as board_mod`` keep working.

Handler methods are rebound onto this package's globals so tests that
monkeypatch ``workforce.board._load_roster`` / ``_worker_queue`` still
reach ``_Handler.do_GET``.
"""

from __future__ import annotations

import concurrent.futures  # noqa: F401
import datetime  # noqa: F401
import html  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sys  # noqa: F401
import types
import urllib.parse  # noqa: F401
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401
from typing import Dict, Optional  # noqa: F401

from .._utils import (  # noqa: F401
    _ago,
    _fmt_fire,
    _parse_iso_z,
    _utc_iso_z,
    _utcnow,
    engine_port,
)
from ..daemon import adaptive_backoff_secs, heartbeat_status, read_heartbeat  # noqa: F401
from ..engine import empty_run_streak  # noqa: F401
from ..ledger import Ledger, parse_shifts  # noqa: F401
from ..roster import RosterError  # noqa: F401
from ..schedule import maybe_cron, next_fire_utc  # noqa: F401

from ..reports import supervisor_api_model  # noqa: F401

from ..api.roster import (  # noqa: F401
    DEFAULT_PORT, CITYHALL, _BRAND_TITLE,
    generation_token, scene_model, scene_tape, report_model, worker_model,
    _load_roster, _worker_queue, _worker_health, _launchctl_rota,
    _cli_label, _kind_label, _worker_identity_aliases, _worker_holdings,
    _worker_ready_teaser, _worker_flags, _desk_json, _law_stack,
    _contract_rules, _git_law_log, _workplaces,
    _platforms, _display_names, _sector_for_worker, _city_folder_name,
    _legacy_plist, _service_config, _queue_human_link,
    _desk_owner_of, OUTCOME_CLS, _IN_CITY, _KIND_LABELS, LAUNCH_AGENTS,
    _REPORT_WINDOW_DAYS, _REPORT_QUIET_HOURS, _REPORT_WINDOWS,
    _FAULT_OUTCOMES, RULE_HEADINGS, _WEDGE_SHIFTS,
)

from .paths import (  # noqa: F401
    API_ONLY,
    SUITE_URL,
    _days_param,
    _html_escape_requested,
    _limit_param,
    _map_roster_url,
    _out_path,
    _refuse_html_escape,
    _safe_worker_name,
)
from .handler import _Handler  # noqa: F401
from .server import make_server, serve  # noqa: F401


def _adopt(fn):
    """Rebind *fn* so name lookups use this package (monkeypatch-stable)."""
    if not isinstance(fn, types.FunctionType):
        return fn
    adopted = types.FunctionType(
        fn.__code__,
        globals(),
        name=fn.__name__,
        argdefs=fn.__defaults__,
        closure=fn.__closure__,
    )
    adopted.__kwdefaults__ = fn.__kwdefaults__
    adopted.__doc__ = fn.__doc__
    adopted.__annotations__ = getattr(fn, "__annotations__", {})
    adopted.__module__ = __name__
    adopted.__qualname__ = fn.__qualname__
    return adopted


for _name in (
    "_map_roster_url",
    "_html_escape_requested",
    "_refuse_html_escape",
    "_out_path",
    "_safe_worker_name",
    "_days_param",
    "_limit_param",
    "make_server",
    "serve",
):
    globals()[_name] = _adopt(globals()[_name])

for _name, _val in list(vars(_Handler).items()):
    if isinstance(_val, types.FunctionType):
        setattr(_Handler, _name, _adopt(_val))
_Handler.__module__ = __name__

del _name, _val, _adopt, types

__all__ = [
    "API_ONLY",
    "SUITE_URL",
    "DEFAULT_PORT",
    "_Handler",
    "make_server",
    "serve",
    "generation_token",
    "scene_model",
    "scene_tape",
    "report_model",
    "worker_model",
]

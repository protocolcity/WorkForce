"""Data models and API helpers.

Pure-Python side: constants, helpers, JSON data models. No HTML.
Glass is the suite Map; this module is the JSON roster.

Package peel of the former ``workforce/api/roster.py`` monolith. Existing
imports stay stable: ``from workforce.api.roster import scene_model`` and
``import workforce.api.roster as _api_roster`` keep working.

Functions are rebound onto this package's globals so tests that monkeypatch
``workforce.api.roster._load_roster`` (and friends) still reach the call
sites — the same binding they had when everything lived in one module.
"""

from __future__ import annotations

import concurrent.futures  # noqa: F401
import datetime  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import plistlib  # noqa: F401
import re  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401
import types
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401
from typing import Dict, List, Optional, Tuple  # noqa: F401

from ...daemon import heartbeat_status, read_heartbeat
from ...engine import _dig, _http_get_json, _is_timeout_exc, empty_run_streak
from ..._utils import (  # noqa: F401
    _DEFAULT_ENGINE_PORT,
    _ago,
    _fmt_fire,
    _parse_iso_z,
    _utc_iso_z,
    _utcnow,
    desk_base_url,
)
from ...ledger import Ledger, open_candidates, open_claims, parse_shifts  # noqa: F401
from ...roster import Roster, RosterError, Worker  # noqa: F401
from ...schedule import calendar_intervals_to_cron, maybe_cron, next_fire_utc  # noqa: F401
from ... import roster as roster_mod  # noqa: F401
from ... import runtimes as runtimes_mod  # noqa: F401

from .constants import (  # noqa: F401
    CITYHALL,
    DEFAULT_PORT,
    LAUNCH_AGENTS,
    OUTCOME_CLS,
    RULE_HEADINGS,
    _BOARD_DESK_TIMEOUT_SECS,
    _BOARD_PROBE_RETRY_BACKOFF_SECS,
    _BOARD_PROBE_TIMEOUT_SECS,
    _BRAND_MODE,
    _BRAND_TITLE,
    _FAILURE_OUTCOMES,
    _FAULT_OUTCOMES,
    _IN_CITY,
    _KIND_LABELS,
    _RECENT_FAILURE_WINDOW_SECS,
    _REPORT_QUIET_HOURS,
    _REPORT_WINDOW_DAYS,
    _REPORT_WINDOWS,
    _WEDGE_SHIFTS,
    _city_folder_name,
    _desk,
    _kind_label,
)
from .desk import (  # noqa: F401
    _desk_json,
    _desk_owner_of,
    _is_connection_exc,
    _queue_human_link,
    _worker_flags,
    _worker_holdings,
    _worker_queue,
    _worker_ready_teaser,
)
from .helpers import (  # noqa: F401
    _cli_label,
    _display_names,
    _load_roster,
    _platforms,
    _sector_for_worker,
    _worker_identity_aliases,
    _workplaces,
)
from .law import (  # noqa: F401
    _contract_rules,
    _git_law_log,
    _law_stack,
    _worker_health,
)
from .models import (  # noqa: F401
    _ledger_candidates,
    _worker_full_data,
    report_model,
    scene_model,
    scene_tape,
    worker_model,
)
from .pulse import (  # noqa: F401
    _launchctl_rota,
    _legacy_plist,
    _service_config,
    generation_token,
    recent_failures,
)


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


_ADOPT_NAMES = (
    "generation_token",
    "recent_failures",
    "_kind_label",
    "_desk",
    "_city_folder_name",
    "_service_config",
    "_legacy_plist",
    "_launchctl_rota",
    "_desk_json",
    "_queue_human_link",
    "_is_connection_exc",
    "_worker_queue",
    "_ledger_candidates",
    "_worker_full_data",
    "_platforms",
    "_load_roster",
    "_display_names",
    "_sector_for_worker",
    "_workplaces",
    "_cli_label",
    "scene_model",
    "scene_tape",
    "report_model",
    "_desk_owner_of",
    "_worker_identity_aliases",
    "_worker_holdings",
    "_worker_ready_teaser",
    "_worker_flags",
    "worker_model",
    "_law_stack",
    "_contract_rules",
    "_worker_health",
    "_git_law_log",
)

for _name in _ADOPT_NAMES:
    globals()[_name] = _adopt(globals()[_name])
del _name, _adopt, _ADOPT_NAMES, types

__all__ = [
    "DEFAULT_PORT",
    "CITYHALL",
    "OUTCOME_CLS",
    "RULE_HEADINGS",
    "generation_token",
    "recent_failures",
    "scene_model",
    "scene_tape",
    "report_model",
    "worker_model",
]

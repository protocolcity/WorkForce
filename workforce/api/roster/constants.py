"""Shared constants and tiny brand/desk helpers for the roster API peel."""

import os
import re

from ..._utils import _DEFAULT_ENGINE_PORT, desk_base_url

# Numeric fallback only. Bind path uses live ``engine_port()``;
# do not snapshot WORKFORCE_PORT at import.
DEFAULT_PORT = _DEFAULT_ENGINE_PORT

# pc-23: "lane" is retired vocabulary on rendered surfaces; roster data still
# says kind=lane until the schema migration lands.
_KIND_LABELS = {"lane": "worker"}


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")


def _desk() -> str:
    """Live desk base URL. Do not snapshot at import."""
    return desk_base_url()


CITYHALL = os.environ.get("WORKFORCE_CITYHALL", os.environ.get("WORKFORCE_CITYHALL", ""))

# ── Dashboard branding ──────
# In a founded city the room name leads: "ProtocolCity — Roster · Workers".
# A standalone WorkForce install fronts the engine brand and shows no doors
# to uninstalled rooms. Mirrors TP's TP_BRAND seam;
# this internal checkout IS the city instance, so "city" is the default and
# the public export must default to "standalone".
_BRAND_MODE = os.environ.get("WORKFORCE_BRAND", "city")
_IN_CITY = _BRAND_MODE == "city"
_BRAND_TITLE = ("ProtocolCity — Roster · Workers" if _IN_CITY
                else "WorkForce — Workers")



def _city_folder_name() -> str:
    """Basename of the city root for `[Folder] Roster` mast parity with Office."""
    root = (os.environ.get("CITY_ROOT") or os.environ.get("TP_CITY_ROOT") or "").strip()
    if not root:
        # Path pin: this lived at workforce/api/roster.py, so dirname(__file__)/../..
        # was the checkout root. After the package peel, anchor on workforce/.
        import workforce
        hood = os.path.abspath(os.path.join(os.path.dirname(workforce.__file__), ".."))
        top = ""
        cur = hood
        while True:
            if os.path.isfile(os.path.join(cur, "AGENTS.md")):
                top = cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        # Standalone: topmost AGENTS.md is this neighborhood — not a city.
        if top and os.path.abspath(top) != hood:
            root = top
    if not root or not os.path.isdir(root):
        return "City"
    return os.path.basename(root.rstrip(os.sep)) or "City"


# Outcomes that count as "failure" for wf-118 pulse visibility.
_FAILURE_OUTCOMES = frozenset(("vendor_limit", "error", "crashed"))
_RECENT_FAILURE_WINDOW_SECS = 1800  # 30 minutes — matches daemon cadence


# Board/scene display probe. Shorter than engine dispatch so
# a hung desk cannot freeze Map. Sockets always close via engine._http_get_json.
#
# wf-147: do **not** retry on timeout — with ~36 lanes fanned out, a 3s timeout
# + one retry made full /api/scene wall-clock ~6–16s when any product filter
# stalled (Map felt dead; clients hung up → BrokenPipe bursts on write).
# One short attempt: degrade to '?' and keep the glass under the 2s target.
# Retry only transient connection drops (reset / refused), not spent budget.
_BOARD_PROBE_TIMEOUT_SECS = 0.9
_BOARD_PROBE_RETRY_BACKOFF_SECS = 0.15
# Holdings / owner desk calls on the scene path share this tighter bound.
_BOARD_DESK_TIMEOUT_SECS = 1.0


_REPORT_WINDOW_DAYS = int(os.environ.get("WORKFORCE_REPORT_WINDOW_DAYS", "7"))
_REPORT_QUIET_HOURS = int(os.environ.get("WORKFORCE_REPORT_QUIET_HOURS", "72"))
_REPORT_WINDOWS = (7, 14, 30)

_FAULT_OUTCOMES = ("error", "crashed")


RULE_HEADINGS = re.compile(r"lane|never|stop|scope|gate|may not|boundar", re.I)


_WEDGE_SHIFTS = 3  # consecutive no-progress shifts on a nonempty queue = wedged


OUTCOME_CLS = {"ok": "ok", "error": "err", "skip": "dim", "warn": "amber",
               "running": "amber", "crashed": "err", "vendor_limit": "amber"}

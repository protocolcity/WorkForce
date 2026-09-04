"""Shared helpers — time, ownership markers, file I/O — imported across engine modules.

Public surface:
  _utcnow()                    → datetime (UTC)
  _day_str(when)               → "YYYY-MM-DD"
  _utc_iso_z(when)             → "YYYY-MM-DDTHH:MM:SSZ"
  _parse_iso_z(ts)             → datetime (UTC) | None
  _ago(ts)                     → human-readable age string ("3m ago")
  _fmt_fire(dt)                → human-readable next-fire string
  desk_base_url()              → WorkLane desk base from env
  engine_port(port=None)       → engine bind/API port int
  engine_api_url(port=None)    → WorkForce engine API base
  _atomic_write_json(path, raw) → None (atomic JSON write via tempfile)
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _day_str(when: Optional[datetime.datetime] = None) -> str:
    return (when or _utcnow()).strftime("%Y-%m-%d")


def _utc_iso_z(when: Optional[datetime.datetime] = None) -> str:
    return (when or _utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_z(ts: str) -> Optional[datetime.datetime]:
    """Inverse of ``_utc_iso_z`` — parse ISO-Z string to UTC datetime. None on any error."""
    if not ts:
        return None
    try:
        return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except (ValueError, TypeError):
        return None


def _ago(ts: str) -> str:
    dt = _parse_iso_z(ts)
    if dt is None:
        return ts
    secs = int((_utcnow() - dt).total_seconds())
    if secs < 90:
        return "%ds ago" % secs
    if secs < 5400:
        return "%dm ago" % (secs // 60)
    if secs < 172800:
        return "%dh ago" % (secs // 3600)
    return "%dd ago" % (secs // 86400)


def _fmt_fire(dt: Optional[datetime.datetime]) -> str:
    if not dt:
        return ""
    mins = int((dt - _utcnow()).total_seconds() // 60)
    if mins >= 2880:
        return "%s (in %dd)" % (dt.strftime("%b %d %H:%M"), mins // 1440)
    return "%s (in %dm)" % (dt.strftime("%H:%M"), max(mins, 0))


# Both historic desk-URL families. First non-empty wins.
# Do not rename keys — live machines have one family or the other.
_DESK_ENV_KEYS = (
    "WL_DESK_URL",
    "TP_DESK_URL",
    "WORKFORCE_DESK",
    "WORKFORCE_DESK",
)
_DEFAULT_DESK_FALLBACK = "http://127.0.0.1:8799"


def desk_base_url() -> str:
    """WorkLane desk base URL from env — both historic families.

    Precedence: ``WL_DESK_URL`` → ``TP_DESK_URL`` (desk-drop, wf-120+) →
    ``WORKFORCE_DESK`` → ``WORKFORCE_DESK`` (legacy board / retired
    product name) → loopback ``:8799``. Empty values skip to the next
    key. Does not rename env vars.
    """
    for key in _DESK_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return _DEFAULT_DESK_FALLBACK


# Engine board is loopback by construction (daemon bind). Port is the seam.
_DEFAULT_ENGINE_PORT = 8797


def engine_port(port: Optional[int] = None) -> int:
    """WorkForce engine bind/API port — own door, not a desk seam.

    Explicit *port*, else ``WORKFORCE_PORT``, else 8797. Call-time so a
    citizen-set env after import still binds the right door. Empty
    or non-int env values fall back. ``board.DEFAULT_PORT`` is the numeric
    fallback constant, not an env snapshot.
    """
    if port is not None:
        return int(port)
    raw = (os.environ.get("WORKFORCE_PORT") or "").strip()
    try:
        return int(raw) if raw else _DEFAULT_ENGINE_PORT
    except (TypeError, ValueError):
        return _DEFAULT_ENGINE_PORT


def engine_api_url(port: Optional[int] = None) -> str:
    """WorkForce engine API base URL — own door, not a desk/workplace seam.

    Host is always ``127.0.0.1`` (the daemon binds loopback). Port via
    ``engine_port``. No trailing slash.
    """
    return "http://127.0.0.1:%d" % engine_port(port)


# Ownership marker (PROCESS §5) — shared by engine and marshal_release.
_OWNER_MARKER_RE = re.compile(r"(?m)^Owner:\s*([^\s:(]+)")


def latest_owner_id(comments: Optional[List[dict]]) -> Optional[str]:
    """Latest ``Owner: <id>`` marker in comment bodies (PROCESS §5 claim)."""
    owner: Optional[str] = None
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        body = str(c.get("body") or "")
        matches = _OWNER_MARKER_RE.findall(body)
        if matches:
            owner = matches[-1].strip()
    return owner or None


def _atomic_write_json(
    path: str, raw: Dict[str, Any], *, prefix: str = ".roster-"
) -> None:
    """Atomically write *raw* as JSON to *path* via a same-directory tempfile."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

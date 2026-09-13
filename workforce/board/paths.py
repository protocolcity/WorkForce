"""Board URL helpers and request-path utilities."""

import os
import re
import sys
import urllib.parse
from typing import Optional


API_ONLY = True
SUITE_URL = (os.environ.get("SUITE_URL") or "http://127.0.0.1:8801").rstrip("/")


def _map_roster_url() -> str:
    return SUITE_URL + "/roster"


def _html_escape_requested() -> bool:
    raw = (os.environ.get("WORKFORCE_API_ONLY") or "1").strip().lower()
    return raw in ("0", "false", "no", "off")


def _refuse_html_escape() -> None:
    """Fail closed: a host still exporting API_ONLY=0 gets one line, no HTML."""
    if _html_escape_requested():
        print(
            "WORKFORCE_API_ONLY=0 is not supported — open the suite Map at %s"
            % _map_roster_url(),
            file=sys.stderr,
        )


def _out_path(local_root: str, name: str) -> str:
    return os.path.join(local_root, "run", "%s.out" % name)



def _safe_worker_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""))


def _days_param(path: str) -> Optional[int]:
    """?days= from a request path; None on absence or junk (model defaults)."""
    query = urllib.parse.urlsplit(path).query
    raw = urllib.parse.parse_qs(query).get("days", [""])[0]
    try:
        return int(raw)
    except ValueError:
        return None


def _limit_param(path: str, default: int = 20, max_limit: int = 100) -> int:
    """?limit= from a request path; invalid values fall back to ``default``."""
    query = urllib.parse.urlsplit(path).query
    raw = urllib.parse.parse_qs(query).get("limit", [""])[0]
    try:
        val = int(raw)
    except ValueError:
        return default
    if val <= 0:
        return default
    return min(val, max_limit)

"""Roster/data-dir resolution for WorkForce MCP tools."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from workforce import roster as roster_mod


def resolve_paths(
    roster_path: Optional[str] = None,
    data_dir: Optional[str] = None,
) -> Dict[str, str]:
    """Resolve WORKFORCE_ROSTER / WORKFORCE_DATA_DIR / cwd defaults."""
    env_roster = (os.environ.get("WORKFORCE_ROSTER") or "").strip()
    env_data = (os.environ.get("WORKFORCE_DATA_DIR") or "").strip()
    data = (data_dir or env_data or "").strip()
    roster = (roster_path or env_roster or "").strip()
    data_explicit = bool(data)

    if roster and not data:
        # roster.json lives in …/local/roster.json
        parent = os.path.dirname(os.path.realpath(roster))
        data = os.path.dirname(parent) if os.path.basename(parent) == "local" else parent
    if data and not roster:
        roster = os.path.join(data, "local", "roster.json")
    if not data:
        data = os.getcwd()
    if not roster:
        for cand in (
            os.path.join(data, "local", "roster.json"),
            os.path.join(data, "roster.json"),
            os.path.join(os.getcwd(), "local", "roster.json"),
        ):
            if os.path.isfile(cand):
                roster = cand
                break
        if not roster:
            roster = os.path.join(data, "local", "roster.json")
    if data_explicit:
        # An explicit data_dir (argument or WORKFORCE_DATA_DIR) always owns
        # local_root, even when the roster lives in an independent location.
        local_root = os.path.join(os.path.realpath(data), "local")
    else:
        local_root = os.path.dirname(os.path.realpath(roster))
        if os.path.basename(local_root) != "local":
            local_root = os.path.join(os.path.realpath(data), "local")
    return {
        "data_dir": os.path.realpath(data),
        "roster_path": os.path.realpath(roster),
        "local_root": os.path.realpath(local_root),
    }


def _load_roster_lenient(paths: Dict[str, str]):
    """Load roster; empty workers dict → empty Roster-like object."""
    path = paths["roster_path"]
    if not os.path.isfile(path):
        return None
    try:
        return roster_mod.load(path, base=paths["data_dir"])
    except roster_mod.RosterError as e:
        msg = str(e).lower()
        if "no workers" in msg:
            # Empty employment file is a valid first-user state
            class _Empty:
                workers: Dict[str, Any] = {}

            return _Empty()
        raise


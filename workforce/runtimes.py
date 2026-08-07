"""Runtime discovery — enumerate installed agent CLIs as the staffing pool.

The registry is the list of well-known vendor CLI names. Detection uses
shutil.which() — the same check the engine's §4 preflight runs per shift
(engine.py:162). No credentials are checked here; keychain is a per-worker
dispatch-time concern, not a discovery signal.
"""

import datetime
import os
import shutil
from typing import Dict, List, Optional

from .ledger import parse_shifts

# Ordered registry of known agent CLI names.
# Add new vendors here; policy (auto-staffing, display order) stays in config.
KNOWN_RUNTIMES: List[str] = ["claude", "codex", "grok", "cursor"]

# Lookback window for quota-hit telemetry.
_LIMIT_HIT_WINDOW_DAYS = 7


def detect(env_path: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Return {cli_name: resolved_path | None} for every known runtime.

    env_path: override the PATH search (forwarded to shutil.which as 'path').
    Runtimes not found are included with None so callers see the full registry.
    """
    return {name: shutil.which(name, path=env_path) for name in KNOWN_RUNTIMES}


def _count_limit_hits(local_root: str, worker_name: str) -> int:
    """Count vendor_limit shifts in the last _LIMIT_HIT_WINDOW_DAYS days for one worker.

    Best-effort read-model: any OSError returns 0 without raising.
    """
    try:
        ledger_path = os.path.join(local_root, "ledger", "%s.log" % worker_name)
        if not os.path.exists(ledger_path):
            return 0
        with open(ledger_path, "r", encoding="utf-8") as fh:
            text = "".join(fh.readlines()[-200:])
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=_LIMIT_HIT_WINDOW_DAYS)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        shifts = parse_shifts(text, limit=200)
        return sum(
            1 for s in shifts
            if s["outcome"] == "vendor_limit" and s["ts"] >= cutoff
        )
    except OSError:
        return 0


def staffing_pool(
    detected: Dict[str, Optional[str]],
    roster: Optional[object] = None,
    local_root: str = "",
) -> List[Dict]:
    """Map detected runtimes to their employment status.

    Returns one entry per KNOWN_RUNTIMES item:
      cli         str         — the CLI name
      path        str | None  — resolved path, or None if not installed
      workers     List[str]   — roster worker names whose command[0] resolves to this CLI
      employed    int         — number of workers on this runtime
      limit_hits  int         — vendor_limit shifts in last 7d (0 when local_root absent)
    """
    employed: Dict[str, List[str]] = {name: [] for name in KNOWN_RUNTIMES}
    if roster is not None:
        for wname, w in roster.workers.items():
            if w.command:
                basename = os.path.basename(w.command[0])
                if basename in employed:
                    employed[basename].append(wname)
    entries = []
    for name in KNOWN_RUNTIMES:
        workers = employed[name]
        limit_hits = 0
        if local_root:
            for wname in workers:
                limit_hits += _count_limit_hits(local_root, wname)
        entries.append({
            "cli": name,
            "path": detected.get(name),
            "workers": workers,
            "employed": len(workers),
            "limit_hits": limit_hits,
        })
    return entries

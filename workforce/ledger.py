"""Ledger — the append-only per-worker record of shifts (RUNNER_SPEC §8).

One file per worker under ``local/ledger/<worker>.log``. Events:
START / DONE / STOP / SKIP / ERROR / WARN / SCOPE_DENY, each UTC-timestamped,
with key=value pairs. The board reads this; nothing ever rewrites it.
"""

import datetime
import os
import re
from typing import List, Optional, Union


EVENTS = ("START", "DONE", "STOP", "SKIP", "ERROR", "WARN", "GHOST", "SCOPE_DENY")


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt(value: Union[str, int, float]) -> str:
    s = str(value)
    if any(c in s for c in (" ", "=", '"')):
        s = '"' + s.replace('"', "'") + '"'
    return s


class Ledger:
    def __init__(self, root: str, worker: str) -> None:
        self.path = os.path.join(root, "%s.log" % worker)
        os.makedirs(root, exist_ok=True)

    def append(self, event: str, **kv: Union[str, int, float]) -> str:
        if event not in EVENTS:
            raise ValueError("unknown ledger event %r (want one of %s)" % (event, "/".join(EVENTS)))
        parts = ["%s %s" % (_utcnow(), event)]
        parts.extend("%s=%s" % (k, _fmt(v)) for k, v in kv.items())
        line = " ".join(parts)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return line

    def tail(self, n: int = 20) -> str:
        if not os.path.exists(self.path):
            return ""
        with open(self.path, "r", encoding="utf-8") as fh:
            return "".join(fh.readlines()[-n:])


def _parse_line(line: str) -> Optional[dict]:
    parts = line.strip().split(" ")
    if len(parts) < 2 or parts[1] not in EVENTS:
        return None
    out = {"ts": parts[0], "event": parts[1]}
    rest = " ".join(parts[2:])
    for m in re.finditer(r'(\w+)=("(?:[^"]*)"|\S+)', rest):
        out[m.group(1)] = m.group(2).strip('"')
    return out


def parse_shifts(text: str, limit: int = 20) -> List[dict]:
    """Group raw ledger lines into shifts, newest first.

    A shift is START..(STOP|ERROR); a standalone SKIP/ERROR/WARN outside a
    shift is its own entry. Passes counted from DONE events. This is a read
    model only — the append-only file stays the record.
    """
    shifts: List[dict] = []
    current: Optional[dict] = None
    for line in text.splitlines():
        ev = _parse_line(line)
        if ev is None:
            continue
        kind = ev["event"]
        if kind == "START":
            current = {"ts": ev["ts"], "outcome": "running", "passes": 0,
                       "queue": ev.get("queue", "?"), "reason": "",
                       "budget_secs": int(ev.get("budget_secs", "0") or 0),
                       "dry_run": ev.get("dry_run") == "1", "end_ts": "",
                       "usage": {}}
            shifts.append(current)
        elif kind == "DONE" and current is not None:
            current["passes"] += 1
            # consumption telemetry: numeric DONE kvs beyond the
            # engine's own bookkeeping sum into the shift's usage read model
            for k, v in ev.items():
                if k in ("ts", "event", "rc", "on_pass", "dry_run"):
                    continue
                try:
                    num = float(v)
                except (TypeError, ValueError):
                    continue
                current["usage"][k] = current["usage"].get(k, 0) + num
            if current["dry_run"]:  # dry-runs end at DONE; no STOP follows
                current["outcome"], current["end_ts"] = "ok", ev["ts"]
                current["reason"] = "dry-run"
                current = None
        elif kind in ("STOP", "ERROR") and current is not None:
            reason = ev.get("reason", "")
            if kind == "STOP":
                current["outcome"] = "ok"
            elif reason.startswith("vendor limit:"):
                current["outcome"] = "vendor_limit"
            else:
                current["outcome"] = "error"
            current["reason"] = reason
            current["end_ts"] = ev["ts"]
            current = None
        elif kind in ("SKIP", "ERROR", "WARN", "SCOPE_DENY"):
            shifts.append({"ts": ev["ts"], "outcome": kind.lower(), "passes": 0,
                           "queue": "", "reason": ev.get("reason", ""),
                           "budget_secs": 0, "dry_run": False, "end_ts": ev["ts"],
                           "usage": {}})
            current = None
    # a START with no terminal event past budget+grace is a CRASHED shift,
    # not a running one — the runner died without logging (incident class:
    # a killed runaway shift that rendered as "on shift now" for 20 min)
    now = datetime.datetime.now(datetime.timezone.utc)
    for s in shifts:
        if s["outcome"] != "running":
            continue
        try:
            started = datetime.datetime.strptime(
                s["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if (now - started).total_seconds() > (s["budget_secs"] or 1500) + 600:
            s["outcome"] = "crashed"
            s["reason"] = "no terminal event past budget+grace"
    return list(reversed(shifts))[:limit]

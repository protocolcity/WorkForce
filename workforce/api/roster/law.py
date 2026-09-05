"""Law-stack walk, contract-rule extract, and worker health badge."""

import datetime
import hashlib
import os
import subprocess
from typing import Dict, List

from ...engine import empty_run_streak
from ...ledger import Ledger, parse_shifts
from ...roster import Worker
from .constants import RULE_HEADINGS, _WEDGE_SHIFTS


def _law_stack(worker: Worker) -> List[Dict[str, str]]:
    """The resolved law stack, by Charter convention — zero config.

    Standard filenames/placements ARE the API: the
    neighborhood's AGENTS.md sits in the workdir, city-level AGENTS.md files
    sit in ancestor directories. Contract and prompt come from the roster.
    Levels are labeled top-down: L0 city law ... L3 shift brief.
    """
    ancestors: List[str] = []
    d = os.path.dirname(os.path.abspath(worker.workdir))
    while True:
        cand = os.path.join(d, "AGENTS.md")
        if os.path.isfile(cand):
            ancestors.append(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    stack: List[Dict[str, str]] = []
    for path in reversed(ancestors):          # topmost first = city rules
        stack.append({"label": "city rules", "path": path})
    stack.append({"label": "neighborhood rules",
                  "path": os.path.join(os.path.abspath(worker.workdir), "AGENTS.md")})
    stack.append({"label": "contract", "path": worker.contract})
    stack.append({"label": "prompt", "path": worker.prompt})
    for i, entry in enumerate(stack):
        entry["level"] = "L%d" % min(i, 3)
        try:
            st = os.stat(entry["path"])
            with open(entry["path"], "rb") as fh:
                entry["sha"] = hashlib.sha256(fh.read()).hexdigest()[:16]
            entry["mtime"] = datetime.datetime.fromtimestamp(
                st.st_mtime, datetime.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        except OSError:
            entry["sha"], entry["mtime"] = "", "missing"
    return stack


def _contract_rules(path: str) -> List[Dict[str, str]]:
    """May/may-not summary from the contract's standardized headings."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    sections: List[Dict[str, str]] = []
    keep = False
    for line in lines:
        m = re.match(r"^#{2,4}\s+(.*)", line)
        if m:
            keep = bool(RULE_HEADINGS.search(m.group(1)))
            if keep:
                sections.append({"title": m.group(1).strip(), "body": ""})
            continue
        if keep and sections and line.strip():
            if len(sections[-1]["body"].splitlines()) < 8:
                sections[-1]["body"] += line.rstrip() + "\n"
    return sections


def _worker_health(local_root: str, w: Worker, queue: str) -> Dict[str, str]:
    """One dot per worker: ok | amber (starving) | err (last shift failed)
    | wedged (fires but never claims — the no-sitting law, oc-34)
    | idle (empty-queue streak at/past threshold — wf-125)."""
    shifts = parse_shifts(Ledger(os.path.join(local_root, "ledger"), w.name).tail(60), limit=6)
    real = [s for s in shifts if not s["dry_run"]]
    if not real:
        return {"cls": "dim", "why": "no shifts yet"}
    last = real[0]
    if last["outcome"] == "vendor_limit":
        return {"cls": "amber", "why": last["reason"] or "vendor limit"}
    if last["outcome"] in ("error", "crashed"):
        return {"cls": "err", "why": "last desk run %s: %s"
                                     % (last["outcome"].upper(), last["reason"] or "?")}
    # wf-125: empty-queue streak at/past threshold → idle (paused, not broken)
    _threshold = max(1, int(getattr(w, "empty_run_threshold", 3) or 3))
    _streak, _ = empty_run_streak(local_root, w.name)
    if _streak >= _threshold:
        return {
            "cls": "idle",
            "why": "empty-queue streak (%d consecutive queue empty)" % _streak,
        }
    try:
        q_n = int(queue)
    except ValueError:
        q_n = 0
    recent = real[:_WEDGE_SHIFTS]
    if (q_n > 0 and len(recent) == _WEDGE_SHIFTS
            and all(s["outcome"] == "ok" and s["reason"].startswith("no progress")
                    for s in recent)):
        # True sit only: reason is "no progress (N -> N)" (flat count).
        # "restocked (N -> M)" is productive-but-full (close + file follow-ups)
        # and must not ship-cut the default-lane coordinator.
        return {"cls": "wedged",
                "why": "queue %s but last %d shifts ended no-progress — "
                       "fires without claiming" % (queue, _WEDGE_SHIFTS)}
    try:
        starving = int(queue) > 0 and all(s["outcome"] == "skip" for s in real[:2]) and len(real) >= 2
    except ValueError:
        starving = False
    if starving:
        return {"cls": "amber", "why": "queue %s but last %d fires skipped (%s)"
                                        % (queue, len(real[:2]), real[0]["reason"])}
    return {"cls": "ok", "why": "last desk run %s" % last["outcome"]}


def _git_law_log(path: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", os.path.dirname(path), "log", "-3", "--format=%h %ad %s",
             "--date=short", "--", os.path.basename(path)],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""

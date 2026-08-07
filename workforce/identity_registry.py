"""PROCESS §5.2 identity registry — read-only helpers for hire / doctor.

Parses WorkLane PROCESS.md §5.2 table rows so hire can emit a paste-ready
row and doctor can flag roster identities that never got registered.

Host-neutral path resolution: explicit path → env → sibling worklane.
Never writes PROCESS.md (other-repo boundary; registration is citizen law).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import FrozenSet, Optional, Tuple

# Section starts at ### 5.2); end at the next ### heading (e.g. 5.2.1 is
# still inside 5.2's "Identity and attribution" parent — we keep reading
# until a ### that is not a 5.2.x subsection, or #### only stops at ###).
_SECTION_START = re.compile(r"^###\s+5\.2\b", re.MULTILINE)
_SECTION_END = re.compile(r"^###\s+(?!5\.2)", re.MULTILINE)
# Table row: | `agent-id` | Who |
_ROW_ID = re.compile(r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|", re.MULTILINE)

ENV_PROCESS = "WORKLANE_PROCESS"
ENV_PROCESS_ALT = "TP_PROCESS"


def _pkg_city_root() -> Path:
    """City root when installed as source (…/workforce/workforce/this.py → …/)."""
    # workforce/workforce/identity_registry.py → parents[2] = city (OneSeo)
    return Path(__file__).resolve().parents[2]


def resolve_process_md(path: Optional[str] = None) -> Optional[str]:
    """Locate PROCESS.md without reading it. None if not found."""
    if path:
        p = os.path.abspath(path)
        return p if os.path.isfile(p) else None
    for env_key in (ENV_PROCESS, ENV_PROCESS_ALT):
        env = (os.environ.get(env_key) or "").strip()
        if env:
            p = os.path.abspath(env)
            if os.path.isfile(p):
                return p
    candidates = (
        _pkg_city_root() / "worklane" / "PROCESS.md",
        Path.cwd().parent / "worklane" / "PROCESS.md",
        Path.cwd() / "worklane" / "PROCESS.md",
    )
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def parse_section_52_ids(text: str) -> FrozenSet[str]:
    """Extract agent ids from a PROCESS.md body (§5.2 table rows)."""
    if not text:
        return frozenset()
    m = _SECTION_START.search(text)
    if not m:
        return frozenset()
    start = m.end()
    end_m = _SECTION_END.search(text, start)
    chunk = text[start : end_m.start() if end_m else len(text)]
    return frozenset(_ROW_ID.findall(chunk))


def load_section_52_ids(path: Optional[str] = None) -> Tuple[FrozenSet[str], Optional[str]]:
    """Load §5.2 ids. Returns (ids, resolved_path). Empty ids if unreadable."""
    resolved = resolve_process_md(path)
    if not resolved:
        return frozenset(), None
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return frozenset(), resolved
    return parse_section_52_ids(text), resolved


def format_section_52_row(
    identity: str,
    *,
    display: str = "",
    role: str = "",
    papers_rel: str = "",
    feed: bool = True,
) -> str:
    """Ready-to-paste markdown table row for PROCESS §5.2."""
    identity = (identity or "").strip()
    who_bits = []
    label = (display or "").strip()
    if not label and role:
        label = role.strip()
    if label:
        who_bits.append(label + ".")
    else:
        who_bits.append("New hire.")
    if papers_rel:
        who_bits.append("Papers at `%s`." % papers_rel)
    if feed and identity:
        who_bits.append("Feed `worker:%s`." % identity)
    who = " ".join(who_bits)
    return "| `%s` | %s |" % (identity, who)

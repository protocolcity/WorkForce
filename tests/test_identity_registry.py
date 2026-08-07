"""PROCESS §5.2 identity registry helpers."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import identity_registry as reg  # noqa: E402


SAMPLE = """# PROCESS

### 5.1) Close-out

noise

### 5.2) Identity and attribution (all agents)

Canonical agent ids:

| Agent id | Who |
| --- | --- |
| `salem` | Salem · Systems Engineer. |
| `lili` | Lili · Desk Engineer. |
| `chief-of-staff` | Duchess · Chief of Staff. |

#### 5.2.1 Founder-present sessions

| Situation | Author |
| --- | --- |
| You present | `owner-terminal` |

### 5.3) Something else

| `not-in-52` | Should not parse |
"""


def test_parse_section_52_ids_table_rows():
    ids = reg.parse_section_52_ids(SAMPLE)
    assert "salem" in ids
    assert "lili" in ids
    assert "chief-of-staff" in ids
    # §5.2.1 situation table puts the id in column 2 — only Agent-id column
    # (col 1) rows are registry entries. owner-terminal is listed in §5.2
    # proper tables when registered; here it is not a col-1 id.
    assert "owner-terminal" not in ids
    assert "not-in-52" not in ids


def test_parse_section_52_empty_without_section():
    assert reg.parse_section_52_ids("# no section\n| `x` | y |\n") == frozenset()


def test_format_section_52_row():
    row = reg.format_section_52_row(
        "neo",
        display="Neo · Market Analyst",
        papers_rel="Demo/workers/neo/CONTRACT.md",
        feed=True,
    )
    assert row == (
        "| `neo` | Neo · Market Analyst. "
        "Papers at `Demo/workers/neo/CONTRACT.md`. "
        "Feed `worker:neo`. |"
    )


def test_load_section_52_ids_from_env(tmp_path, monkeypatch):
    p = tmp_path / "PROCESS.md"
    p.write_text(SAMPLE)
    monkeypatch.setenv("WORKLANE_PROCESS", str(p))
    ids, path = reg.load_section_52_ids()
    assert path == str(p)
    assert "salem" in ids

"""wf-225 — live worker papers teach project, not neighborhood-as-L1."""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKERS = os.path.join(ROOT, "workers")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(WORKERS),
    reason="live worker papers are not shipped on the public export tree",
)

# Teaching phrases that re-introduce neighborhood as the L1 unit.
# Wire ids (NEIGHBORHOOD_NAME, --neighborhood) live in engine/CLI, not papers.
_BANNED = (
    "neighborhood law",
    "in the workforce neighborhood",
    "house rules of this neighborhood",
    "workforce neighborhood itself",
    "workforce neighborhood job",
    "workforce/workforce neighborhood",
)

_LANE_PROMPTS = (
    "salem",
    "otto",
    "melanie",
    "claude-workforce",
)

_NAMED = (
    "salem",
    "otto",
    "melanie",
    "marshal",
    "clerk",
    "claude-workforce",
    "efficiency-workforce",
)


def _paper_paths():
    paths = []
    for name in sorted(os.listdir(WORKERS)):
        folder = os.path.join(WORKERS, name)
        if not os.path.isdir(folder):
            continue
        for paper in ("prompt.md", "CONTRACT.md"):
            path = os.path.join(folder, paper)
            if os.path.isfile(path):
                paths.append((name, paper, path))
    return paths


def test_live_worker_papers_named_in_wf225_exist():
    slugs = {name for name, _paper, _path in _paper_paths()}
    for required in _NAMED:
        assert required in slugs


def test_live_worker_papers_do_not_teach_neighborhood_as_l1():
    hits = []
    for name, paper, path in _paper_paths():
        body = open(path, encoding="utf-8").read()
        low = body.lower()
        for phrase in _BANNED:
            if phrase in low:
                hits.append("%s/%s: %r" % (name, paper, phrase))
    assert hits == []


def test_live_lane_prompts_teach_project_instructions():
    """Sitting lane prompts name AGENTS.md as project instructions."""
    for slug in _LANE_PROMPTS:
        path = os.path.join(WORKERS, slug, "prompt.md")
        body = open(path, encoding="utf-8").read().lower()
        assert "project instructions" in body, slug
        assert "neighborhood law" not in body, slug

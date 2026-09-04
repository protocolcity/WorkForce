"""wf-204 — skill-draft extractor: extract→draft SKILL.md, allowlist, dry-run."""

from __future__ import annotations

import os

import pytest

from workforce import skill_draft as sd
from workforce import cli


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------


def test_slugify_basic():
    assert sd._slugify("Design+spike: post-close promotion") == "design-spike-post-close-promotion"


def test_slugify_trims_hyphens():
    assert not sd._slugify("").startswith("-")


def test_slugify_max_length():
    long_title = "a" * 100
    assert len(sd._slugify(long_title)) <= 64


# ---------------------------------------------------------------------------
# extract_sections
# ---------------------------------------------------------------------------

SAMPLE_CLOSEOUT = """\
Owner: salem
Workdir: /tmp/repo
Start: 2026-08-12T14:00:00Z

Completed: skill-draft spike

Implemented draft_from_closeout() in workforce/skill_draft.py.
Added skill-draft CLI subcommand.
Wrote tests.

Verification:
- pytest tests/test_skill_draft.py -q → green (12 tests)
- skill-draft --dry-run emits content without writing
- workforce skill-draft salem wf-204 --title "Test" --dry-run exits 0

Links:
- commit abc123def456 on origin/main
- wf-204 closed

Follow-ups:
- wf-205: wire skill-draft into post-close job if founder decides
"""


def test_extract_sections_completed():
    secs = sd.extract_sections(SAMPLE_CLOSEOUT)
    assert "draft_from_closeout" in secs["completed"]


def test_extract_sections_verification():
    secs = sd.extract_sections(SAMPLE_CLOSEOUT)
    assert "pytest" in secs["verification"]


def test_extract_sections_links():
    secs = sd.extract_sections(SAMPLE_CLOSEOUT)
    assert "abc123def456" in secs["links"]


def test_extract_sections_followups():
    secs = sd.extract_sections(SAMPLE_CLOSEOUT)
    assert "wf-205" in secs["followups"]


def test_extract_sections_absent_key():
    secs = sd.extract_sections("Completed: just this\n\nsome text\n")
    assert secs["verification"] == ""
    assert secs["links"] == ""


def test_extract_sections_empty():
    secs = sd.extract_sections("")
    for v in secs.values():
        assert v == ""


# ---------------------------------------------------------------------------
# build_skill_md
# ---------------------------------------------------------------------------


def test_build_skill_md_frontmatter():
    md = sd.build_skill_md(
        title="My Skill",
        slug="my-skill",
        ticket_id="wf-204",
        worker="salem",
        sha="abc123",
        completed="Step 1\nStep 2",
        verification="pytest green",
    )
    assert md.startswith("---\n")
    assert "name: my-skill" in md
    assert "ticket: wf-204" in md
    assert "worker: salem" in md
    assert "status: draft" in md


def test_build_skill_md_procedure_section():
    md = sd.build_skill_md(
        title="My Skill",
        slug="my-skill",
        ticket_id="wf-204",
        worker="salem",
        sha="",
        completed="Do the thing",
        verification="",
    )
    assert "## Procedure" in md
    assert "Do the thing" in md


def test_build_skill_md_promote_gate_present():
    md = sd.build_skill_md(
        title="T",
        slug="t",
        ticket_id="wf-1",
        worker="salem",
        sha="",
        completed="",
        verification="",
    )
    assert "## Promote gate" in md
    assert "Never auto-promote" in md


def test_build_skill_md_verification_section_omitted_when_empty():
    md = sd.build_skill_md(
        title="T",
        slug="t",
        ticket_id="wf-1",
        worker="salem",
        sha="",
        completed="step",
        verification="",
    )
    assert "## Verification baseline" not in md


def test_build_skill_md_verification_section_present():
    md = sd.build_skill_md(
        title="T",
        slug="t",
        ticket_id="wf-1",
        worker="salem",
        sha="",
        completed="step",
        verification="pytest green",
    )
    assert "## Verification baseline" in md


# ---------------------------------------------------------------------------
# is_draft_allowed
# ---------------------------------------------------------------------------


def test_is_draft_allowed_bootstrap_seat():
    assert sd.is_draft_allowed("salem")
    assert sd.is_draft_allowed("blossom")


def test_is_draft_allowed_unknown_seat_no_roster():
    assert not sd.is_draft_allowed("mystery-worker")


def test_is_draft_allowed_roster_flag():
    class FakeWorker:
        skill_draft = True

    workers = {"custom-seat": FakeWorker()}
    assert sd.is_draft_allowed("custom-seat", workers)


def test_is_draft_allowed_roster_flag_false():
    class FakeWorker:
        skill_draft = False

    workers = {"custom-seat": FakeWorker()}
    assert not sd.is_draft_allowed("custom-seat", workers)


def test_is_draft_allowed_roster_missing_flag():
    class FakeWorker:
        pass  # no skill_draft attribute

    workers = {"other-seat": FakeWorker()}
    assert not sd.is_draft_allowed("other-seat", workers)


# ---------------------------------------------------------------------------
# draft_from_closeout — dry-run (no filesystem write)
# ---------------------------------------------------------------------------


def test_draft_from_closeout_dry_run_returns_content():
    result = sd.draft_from_closeout(
        close_out_text=SAMPLE_CLOSEOUT,
        worker="salem",
        ticket_id="wf-204",
        title="My Skill Title",
        dry_run=True,
    )
    assert result["allowed"] is True
    assert result["dry_run"] is True
    assert "My Skill Title" in result["content"]
    assert result["path"] == "<dry-run>"


def test_draft_from_closeout_dry_run_no_file_written(tmp_path):
    outdir = str(tmp_path / "drafts")
    result = sd.draft_from_closeout(
        close_out_text=SAMPLE_CLOSEOUT,
        worker="salem",
        ticket_id="wf-999",
        title="Test Skill",
        outdir=outdir,
        dry_run=True,
    )
    assert result["allowed"] is True
    # dry-run must not write anything
    assert not os.path.exists(os.path.join(outdir, "wf-999", "SKILL.md"))


def test_draft_from_closeout_denied_unknown_worker():
    result = sd.draft_from_closeout(
        close_out_text=SAMPLE_CLOSEOUT,
        worker="not-allowed",
        ticket_id="wf-204",
        title="Test",
        dry_run=True,
    )
    assert result["allowed"] is False
    assert "not in allowlist" in result["reason"]


# ---------------------------------------------------------------------------
# draft_from_closeout — live write
# ---------------------------------------------------------------------------


def test_draft_from_closeout_live_writes_file(tmp_path):
    outdir = str(tmp_path / "drafts")
    result = sd.draft_from_closeout(
        close_out_text=SAMPLE_CLOSEOUT,
        worker="salem",
        ticket_id="wf-204",
        title="Live Skill",
        sha="deadbeef1234",
        outdir=outdir,
        dry_run=False,
    )
    assert result["allowed"] is True
    assert result["dry_run"] is False
    written_path = os.path.join(outdir, "wf-204", "SKILL.md")
    assert os.path.exists(written_path)
    text = open(written_path).read()
    assert "Live Skill" in text
    assert "deadbeef1234" in text


def test_draft_from_closeout_live_requires_outdir():
    with pytest.raises(ValueError, match="outdir"):
        sd.draft_from_closeout(
            close_out_text="Completed:\nfoo",
            worker="salem",
            ticket_id="wf-204",
            title="T",
            outdir="",
            dry_run=False,
        )


# ---------------------------------------------------------------------------
# CLI — skill-draft subcommand
# ---------------------------------------------------------------------------


def test_cli_skill_draft_dry_run(capsys):
    rc = cli.main([
        "skill-draft", "salem", "wf-204",
        "--title", "My Skill",
        "--text", SAMPLE_CLOSEOUT,
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "my-skill" in out


def test_cli_skill_draft_denied_worker(capsys):
    rc = cli.main([
        "skill-draft", "nobody", "wf-204",
        "--title", "T",
        "--text", "Completed:\nfoo",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not in allowlist" in err


def test_cli_skill_draft_live_writes(tmp_path, capsys):
    outdir = str(tmp_path / "drafts")
    rc = cli.main([
        "skill-draft", "salem", "wf-204",
        "--title", "Live Skill",
        "--text", SAMPLE_CLOSEOUT,
        "--outdir", outdir,
        "--live",
    ])
    assert rc == 0
    assert os.path.exists(os.path.join(outdir, "wf-204", "SKILL.md"))
    out = capsys.readouterr().out
    assert "wrote" in out

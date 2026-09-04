"""Skill-draft extractor: closed shift evidence → draft SKILL.md (no auto-L0).

Core path (design wf-204 / research hermes-skill-accretion-bp-gap-2026-08.md §3.2):

    close-out comment (Completed: / Verification: / Links:)
        → extract_sections()
        → build_skill_md()
        → workers/<slug>/skills-drafts/<ticket-id>/SKILL.md
          (git-tracked staging; NEVER ~/.agents/skills/ or local/)

Allowlist: roster flag skill_draft=true per seat, or the SKILL_DRAFT_SEATS
bootstrap set. Promote is always citizen-gated: copy the staging directory to
the L0 skill shelf and run skills_sync.sh if applicable. Discard: delete the
directory.

Dry-run is the default: prints the draft without any filesystem write.
Pass dry_run=False + outdir to emit the file. The engine dispatch loop never
calls this automatically — it is an explicit post-close tool.
"""

import os
import re
from typing import Dict, Optional


# Bootstrap allowlist — seats that may propose drafts without a roster flag.
# Roster skill_draft=true extends this per deployment.
SKILL_DRAFT_SEATS = frozenset({"salem", "blossom"})


def _slugify(text: str) -> str:
    """WO title → kebab-case skill name slug (max 64 chars)."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:64]


def extract_sections(text: str) -> Dict[str, str]:
    """Pull Completed: / Verification: / Links: / Follow-ups: blocks.

    Each block extends until the next section header (or end of text).
    Returns a dict with keys 'completed', 'verification', 'links', 'followups';
    absent sections are empty strings.
    """
    headers = {
        "completed": re.compile(r"^Completed:\s*", re.MULTILINE),
        "verification": re.compile(r"^Verification:\s*", re.MULTILINE),
        "links": re.compile(r"^Links:\s*", re.MULTILINE),
        "followups": re.compile(r"^Follow-ups?:\s*", re.MULTILINE),
    }
    # Boundary: any section header from the §5 set
    any_header = re.compile(
        r"^(?:Completed|Verification|Links|Follow-ups?):\s*",
        re.MULTILINE,
    )
    out = {k: "" for k in headers}
    for key, pat in headers.items():
        m = pat.search(text)
        if not m:
            continue
        start = m.end()
        rest = text[start:]
        next_m = any_header.search(rest)
        block = rest[: next_m.start()] if next_m else rest
        out[key] = block.strip()
    return out


def build_skill_md(
    title: str,
    slug: str,
    ticket_id: str,
    worker: str,
    sha: str,
    completed: str,
    verification: str,
) -> str:
    """Compose the draft SKILL.md text in agentskills-compatible format.

    Output is a valid SKILL.md with YAML frontmatter (name, description,
    source block) followed by structured markdown sections. Compatible with
    the ~/.agents/skills/ shelf layout and agentskills.io conventions.
    """
    sha_note = " · %s" % sha[:12] if sha else ""
    desc_line = (completed.split("\n")[0] if completed else title).strip()
    lines = [
        "---",
        "name: %s" % slug,
        "description: >",
        "  %s" % desc_line,
        "source:",
        "  ticket: %s" % ticket_id,
        "  worker: %s" % worker,
        "  sha: %s" % (sha or "unset"),
        "  status: draft",
        "---",
        "",
        "# %s" % title,
        "",
        "**Status:** DRAFT — pending citizen review (not promoted to L0)",
        "**Source:** %s%s · signed by %s" % (ticket_id, sha_note, worker),
        "",
        "## Procedure",
        "",
        completed or "_(extracted from close-out Completed: section)_",
        "",
    ]
    if verification:
        lines += [
            "## Verification baseline",
            "",
            verification,
            "",
        ]
    lines += [
        "## Promote gate",
        "",
        "Citizen action required. Review the draft, then either:",
        "",
        "- **Promote:** copy this directory to `~/.agents/skills/%s/`" % slug,
        "  and run `scripts/skills_sync.sh` if applicable.",
        "  Never auto-promote — this gate exists by design.",
        "- **Discard:** delete this `skills-drafts/%s/` directory." % ticket_id,
        "",
    ]
    return "\n".join(lines)


def is_draft_allowed(worker: str, workers: Optional[Dict] = None) -> bool:
    """True when the worker seat may propose skill drafts.

    Allowlist priority:
      1. SKILL_DRAFT_SEATS bootstrap set (no roster needed).
      2. Roster flag skill_draft=true on the worker row.
    """
    if worker in SKILL_DRAFT_SEATS:
        return True
    if workers is None:
        return False
    w = workers.get(worker)
    if w is None:
        return False
    return bool(getattr(w, "skill_draft", False))


def draft_from_closeout(
    close_out_text: str,
    worker: str,
    ticket_id: str,
    title: str,
    sha: str = "",
    outdir: str = "",
    dry_run: bool = True,
    workers: Optional[Dict] = None,
) -> Dict:
    """Extract a draft SKILL.md from a close-out comment body.

    Args:
        close_out_text: §5 close-out comment text (Completed:/Verification:/…).
        worker:         Signing worker slug (checked against allowlist).
        ticket_id:      Work-order id (e.g. 'wf-204').
        title:          WO title (becomes skill heading and slug base).
        sha:            Landing commit SHA (optional; cite in Links: if known).
        outdir:         Base directory for staging output.
                        Required when dry_run=False.
                        Typical value: workers/<worker>/skills-drafts/
        dry_run:        If True (default), return content without writing.
        workers:        Roster workers dict for allowlist check. None = skip
                        roster check (bootstrap set only).

    Returns:
        dict with keys: allowed (bool), slug (str), path (str),
        content (str), dry_run (bool), reason (str, on deny).
    """
    allowed = is_draft_allowed(worker, workers)
    if not allowed:
        return {
            "allowed": False,
            "slug": "",
            "path": "",
            "content": "",
            "dry_run": dry_run,
            "reason": (
                "worker %r not in allowlist "
                "(roster skill_draft=true or SKILL_DRAFT_SEATS required)" % worker
            ),
        }

    slug = _slugify(title)
    secs = extract_sections(close_out_text)
    content = build_skill_md(
        title=title,
        slug=slug,
        ticket_id=ticket_id,
        worker=worker,
        sha=sha,
        completed=secs["completed"],
        verification=secs["verification"],
    )

    if not dry_run:
        if not outdir:
            raise ValueError("outdir is required when dry_run=False")
        draft_dir = os.path.join(outdir, ticket_id)
        os.makedirs(draft_dir, exist_ok=True)
        out_path = os.path.join(draft_dir, "SKILL.md")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    else:
        out_path = (
            os.path.join(outdir, ticket_id, "SKILL.md") if outdir else "<dry-run>"
        )

    return {
        "allowed": True,
        "slug": slug,
        "path": out_path,
        "content": content,
        "dry_run": dry_run,
    }

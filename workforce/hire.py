"""Hire — one employment write path (STAFFING §2 / workforce hire).

Produces papers (CONTRACT.md + prompt.md) and a live roster row together.
Office staff / You are never hireable here — those seats are permanent.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .roster import (
    DEFAULT_ROSTER_PATHS,
    RosterError,
    Worker,
    load,
)

# "you" is the synthetic citizen — never a roster entry, never hireable.
# Permanent Office staff (office-steward, daily-brief, …) are blocked by
# the roster duplicate-slug guard once their staff=True rows exist.
FORBIDDEN_HIRE_NAMES = frozenset({"you"})

DEFAULT_COMMAND = [
    "claude", "--model", "{model}", "-p", "{prompt_text}",
    "--dangerously-skip-permissions", "--no-session-persistence",
    "--output-format", "json",
]

DEFAULT_USAGE_FIELDS = {
    "tok_in": "usage.input_tokens",
    "tok_out": "usage.output_tokens",
    "cost_usd": "total_cost_usd",
}

_FALLBACK_CONTRACT = """# {slug} — Employment Contract (L2)

## Identity

- Signs all work as: `{slug}`
- Vendor CLI: claude
- Model/effort pin: vendor default

## Lane — what this worker may claim

- Tickets labeled `worker:{slug}` in store `{store}`, and nothing else.
  (Vocabulary law: routing label is worker:<id>, not lane: — pc-23 / STAFFING.)

## Never touch

- Anything behind a citizen gate (L0/L1) — prepare, never ship.
- `local/roster.json` and other employment records.

## Procedure

1. Claim — set the ticket in progress under your identity.
2. Work — smallest slice; stage only files your contract allows.
3. Verify — run the neighborhood's checks.
4. Close out — comment and hand back.
"""

_FALLBACK_PROMPT = """You are `{slug}`, a worker in the {neighborhood} neighborhood.

1. Read your contract: `workers/{slug}/CONTRACT.md`.
2. Read the neighborhood law: `AGENTS.md` at the repo root.
3. Check the queue: tickets labeled `worker:{slug}` in store `{store}` only.
4. Do ONE slice of ONE ticket. Sign everything as `{slug}`.
5. Queue empty or stop rule hit: stop cleanly and say why.
"""


def slugify(name: str) -> str:
    """Persona → kebab-case identity slug."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def _resolve_roster_path(path: Optional[str], base: str) -> str:
    if path:
        return path
    env = os.environ.get("WORKFORCE_ROSTER") or os.environ.get("WORKFORCE_ROSTER")
    if env:
        return env
    for candidate in DEFAULT_ROSTER_PATHS:
        p = os.path.join(base, candidate)
        if os.path.exists(p):
            return p
    # Prefer conventional local/ path even when creating the first hire.
    return os.path.join(base, "local", "roster.json")


def _read_raw(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"workers": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RosterError("cannot read roster %s: %s" % (path, exc))
    if not isinstance(raw, dict):
        raise RosterError("roster %s must be a JSON object" % path)
    workers = raw.get("workers")
    if workers is None:
        raw["workers"] = {}
    elif not isinstance(workers, dict):
        raise RosterError("roster %s workers must be an object" % path)
    return raw


def _atomic_write_json(path: str, raw: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".roster-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def worker_to_spec(w: Worker) -> Dict[str, Any]:
    """Serialize a Worker for roster.json (omit empty optionals that match defaults)."""
    spec = asdict(w)
    spec.pop("name", None)
    # Keep the file readable — drop empty strings / empty dicts that are defaults
    # except required paths already validated.
    #
    # scope_home / perimeter_grants: only write when set. Empty keys still break
    # older running daemons whose Worker dataclass rejects unknown fields
    # (2026-07-21: hire codex wrote empties → entire roster unreadable until
    # stripped). Non-empty values require a daemon that knows the fields.
    if not spec.get("scope_home"):
        spec.pop("scope_home", None)
    if not spec.get("perimeter_grants"):
        spec.pop("perimeter_grants", None)
    return spec


def _template_dir() -> Optional[Path]:
    env = os.environ.get("PROTOCOLCITY_TEMPLATES", "").strip()
    if env and os.path.isdir(env):
        return Path(env)
    # Developer/workforce/workforce/hire.py → Developer/ProtocolCity/templates
    sibling = Path(__file__).resolve().parents[2] / "ProtocolCity" / "templates"
    if sibling.is_dir():
        return sibling
    return None


def _fill_template(body: str, mapping: Dict[str, str]) -> str:
    out = body
    for key, val in mapping.items():
        out = out.replace("{{" + key + "}}", val)
        out = out.replace("{" + key + "}", val)
    # Soft-blank remaining {{PLACEHOLDERS}}
    out = re.sub(r"\{\{[A-Z0-9_/'\" —.-]+\}\}", "…", out)
    return out


def plant_papers(
    workdir: str,
    slug: str,
    *,
    role: str = "",
    store: str = "",
    neighborhood: str = "",
    force: bool = False,
) -> Tuple[str, str]:
    """Write CONTRACT.md + prompt.md under <workdir>/workers/<slug>/.

    Returns absolute (contract_path, prompt_path).
    """
    workdir = os.path.abspath(workdir)
    if not os.path.isdir(workdir):
        raise RosterError("workdir does not exist: %s" % workdir)
    workers_dir = os.path.join(workdir, "workers", slug)
    os.makedirs(workers_dir, exist_ok=True)
    contract = os.path.join(workers_dir, "CONTRACT.md")
    prompt = os.path.join(workers_dir, "prompt.md")
    store = store or os.path.basename(workdir).lower().replace(" ", "-")
    neighborhood = neighborhood or os.path.basename(workdir)
    mapping = {
        "WORKER_ID": slug,
        "slug": slug,
        "STORE_SLUG": store,
        "store": store,
        "NEIGHBORHOOD_NAME": neighborhood,
        "neighborhood": neighborhood,
        "CLI_COMMAND": "claude",
        'MODEL_OR_"vendor default"': "vendor default",
        "CLAIM_CRITERIA — e.g. \"single-file, verifiable by the test suite, no schema changes\"": (
            role or "work assigned to this cabinet"
        ),
        "FORBIDDEN_AREA_1": "local/ employment records (roster, ledger locks)",
        "FORBIDDEN_AREA_2": "other cabinets' workers/ trees",
    }
    tdir = _template_dir()
    for dest, src_name, fallback in (
        (contract, "worker-CONTRACT.md", _FALLBACK_CONTRACT),
        (prompt, "worker-prompt.md", _FALLBACK_PROMPT),
    ):
        if os.path.exists(dest) and not force:
            continue
        if tdir and (tdir / src_name).is_file():
            body = (tdir / src_name).read_text(encoding="utf-8")
            body = _fill_template(body, mapping)
        else:
            body = fallback.format(
                slug=slug, store=store, neighborhood=neighborhood
            )
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(body)
    return contract, prompt


def hire(
    *,
    name: str,
    workdir: str,
    display: str = "",
    role: str = "",
    kind: str = "lane",
    identity: str = "",
    schedule: str = "*/30 * * * *",
    model: str = "",
    command: Optional[List[str]] = None,
    queue_url: str = "",
    queue_count_key: str = "count",
    budget_secs: int = 1500,
    keychain_service: str = "claude-cli-oauth",
    keychain_env: str = "CLAUDE_CODE_OAUTH_TOKEN",
    env: Optional[Dict[str, str]] = None,
    plant: bool = True,
    force_papers: bool = False,
    project: str = "",
    roster_path: Optional[str] = None,
    base: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Arm a worker: papers + roster row. Returns a result dict."""
    base = base or os.getcwd()
    slug = slugify(name)
    if not slug:
        raise RosterError("hire needs a persona name (slug empty)")
    if slug in FORBIDDEN_HIRE_NAMES or name.strip().lower() in FORBIDDEN_HIRE_NAMES:
        raise RosterError(
            "cannot hire %r — permanent synthetic citizen seat" % name
        )
    workdir = os.path.abspath(workdir)
    if not os.path.isdir(workdir):
        raise RosterError("workdir does not exist: %s" % workdir)

    identity = slugify(identity) if identity else slug
    if identity in FORBIDDEN_HIRE_NAMES:
        raise RosterError("identity %r is reserved (synthetic citizen)" % identity)

    role_title = (role or "").strip()
    if display:
        disp = display.strip()
    elif role_title:
        # Board label "Reed · Role" — title-case raw slug personas
        raw = (name.strip() or slug)
        if raw.lower() == slug:
            persona = slug[:1].upper() + slug[1:] if slug else slug
        else:
            persona = raw
        disp = "%s · %s" % (persona, role_title)
    else:
        disp = name.strip() or slug

    store = project or os.path.basename(workdir).lower().replace(" ", "-")
    if not queue_url and kind == "lane":
        # Exclusive hand feed: never bare product ready —
        # unfiltered queues let one hire drain the whole neighborhood.
        queue_url = (
            "http://127.0.0.1:8799/api/admin/tasks/ready"
            "?product=%s&label=worker:%s" % (store, slug)
        )

    contract = os.path.join(workdir, "workers", slug, "CONTRACT.md")
    prompt = os.path.join(workdir, "workers", slug, "prompt.md")
    papers_planted = False
    if plant:
        contract, prompt = plant_papers(
            workdir, slug, role=role_title, store=store,
            neighborhood=os.path.basename(workdir), force=force_papers,
        )
        papers_planted = True
    else:
        if not os.path.isfile(contract) or not os.path.isfile(prompt):
            raise RosterError(
                "papers missing at %s — pass plant_papers=true or create them"
                % os.path.join(workdir, "workers", slug)
            )

    env_map = dict(env or {})
    env_map.setdefault("TP_AGENT_ID", identity)

    w = Worker(
        name=slug,
        workdir=workdir,
        contract=os.path.abspath(contract),
        prompt=os.path.abspath(prompt),
        identity=identity,
        command=list(command or DEFAULT_COMMAND),
        kind=kind,
        model=model,
        budget_secs=int(budget_secs),
        schedule=schedule,
        queue_url=queue_url,
        queue_count_key=queue_count_key or "count",
        keychain_service=keychain_service,
        keychain_env=keychain_env,
        env=env_map,
        display=disp,
        usage_fields=dict(DEFAULT_USAGE_FIELDS),
    )
    w.validate()

    path = _resolve_roster_path(roster_path, base)
    raw = _read_raw(path)
    workers = raw.setdefault("workers", {})
    if slug in workers:
        raise RosterError("worker %r already on the roster" % slug)
    for existing_name, spec in workers.items():
        if isinstance(spec, dict) and spec.get("identity") == identity:
            raise RosterError(
                "identity %r already used by %r" % (identity, existing_name)
            )

    next_steps = [
        "Fill blanks in %s (take-list / never-touch)." % contract,
        "Register signing id `%s` at Desk (PROCESS §5.2) if new." % identity,
        "Optional dry-run: workforce dispatch %s --dry-run" % slug,
    ]

    result = {
        "ok": True,
        "armed": not dry_run,
        "dry_run": dry_run,
        "worker": {
            "name": slug,
            "identity": identity,
            "display": disp,
            "kind": kind,
            "workdir": workdir,
            "contract": w.contract,
            "prompt": w.prompt,
            "schedule": schedule,
            "queue_url": queue_url,
        },
        "papers_planted": papers_planted,
        "roster_path": path,
        "next_steps": next_steps,
    }

    if dry_run:
        result["msg"] = "dry_run — papers %s; roster not written" % (
            "planted" if papers_planted else "checked"
        )
        return result

    workers[slug] = worker_to_spec(w)
    _atomic_write_json(path, raw)
    # Confirm the live roster still loads
    try:
        load(path=path, base=base)
    except RosterError:
        # Roll back this key if the merged file is somehow invalid
        workers.pop(slug, None)
        _atomic_write_json(path, raw)
        raise
    result["msg"] = "hired %s into %s" % (slug, os.path.basename(workdir))
    return result

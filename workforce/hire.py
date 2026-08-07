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
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from .roster import (
    DEFAULT_ROSTER_PATHS,
    WORKER_TYPES,
    RosterError,
    Worker,
    load,
)

# "you" is the synthetic citizen — never a roster entry, never hireable.
# Permanent Office staff (office-steward, daily-brief, …) are blocked by
# the roster duplicate-slug guard once their staff=True rows exist.
FORBIDDEN_HIRE_NAMES = frozenset({"you"})

_BAD_WORKER_PARAM = re.compile(r"[?&]worker=")

# Full payroll model pins known to the city (capacity rails + roster conventions).
# Shorthands like "claude-sonnet" are intentionally absent — hire must reject them
# so pin-pair matching and capacity policy stay exact-id.
CANONICAL_MODEL_IDS = frozenset({
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "grok-4.5",
    "cursor-grok-4.5-low",
})


def _check_queue_url(url: str, slug: str) -> None:
    """Reject ?worker= probe form — label=worker:<id> is the exclusive drain."""
    if url and _BAD_WORKER_PARAM.search(url) and "label=worker:" not in url:
        raise RosterError(
            "queue_url for %r uses ?worker= param — use label=worker:%s instead "
            "(worker= is unfiltered; label= is the exclusive drain)" % (slug, slug)
        )


def collect_known_model_ids(
    *,
    local_root: Optional[str] = None,
    policy_path: Optional[str] = None,
    roster_path: Optional[str] = None,
    base: Optional[str] = None,
) -> FrozenSet[str]:
    """Union of canonical pins, capacity-policy endpoints, and live roster models."""
    known: Set[str] = set(CANONICAL_MODEL_IDS)
    try:
        from . import capacity_policy as cp

        pol = cp.load_capacity_policy(
            path=policy_path, local_root=local_root, required=False
        )
        if pol is None:
            try:
                pol = cp.load_example_policy()
            except cp.CapacityPolicyError:
                pol = None
        if pol is not None:
            known.update(pol.known_models())
    except Exception:
        pass
    if roster_path and os.path.isfile(roster_path):
        try:
            r = load(path=roster_path, base=base or os.getcwd())
            for w in r.workers.values():
                m = (w.model or "").strip()
                if m and m != "default":
                    known.add(m)
        except RosterError:
            pass
    return frozenset(known)


def validate_model_pin(
    model: str,
    *,
    known: Optional[FrozenSet[str]] = None,
    local_root: Optional[str] = None,
    policy_path: Optional[str] = None,
    roster_path: Optional[str] = None,
    base: Optional[str] = None,
) -> str:
    """Return stripped model or raise RosterError for unknown / shorthand pins.

    Empty model = vendor default (explicit choice) and is always allowed.
    """
    pin = (model or "").strip()
    if not pin or pin == "default":
        return ""
    ids = known if known is not None else collect_known_model_ids(
        local_root=local_root,
        policy_path=policy_path,
        roster_path=roster_path,
        base=base,
    )
    if pin in ids:
        return pin
    sample = ", ".join(sorted(ids)[:8])
    more = " …" if len(ids) > 8 else ""
    raise RosterError(
        "unknown model pin %r — use a full id from capacity policy / roster "
        "conventions (e.g. %s%s); shorthands like 'claude-sonnet' are rejected"
        % (pin, sample, more)
    )

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
4. **Land it** — work is not done until it is on `origin/main`. From the
   shift tree: push FF-only (e.g. `git push origin HEAD:main` when FF-able).
   Resolve conflicts as a **union** — never wholesale-overwrite a shared file
   from a stale copy. Before editing a shared file, re-read it from main
   (rebase if your copy predates HEAD). Cite the landing commit SHA in
   close-out **Links**.
5. Close out — comment and hand back; include the landing SHA under Links.

## Shift worktree

When the roster sets `shift_worktree: true`, the engine spawns you with
`cwd=$WORKFORCE_SHIFT_WORKDIR` (linked worktree on `workforce/shift/{slug}`).
Primary checkout dirty is invisible to your index. Do not assume you are on
the primary checkout. Land via Procedure **Land it**.
"""

_FALLBACK_PROMPT = """You are `{slug}`, a worker in the {neighborhood} neighborhood.

1. Read your contract: `workers/{slug}/CONTRACT.md`.
2. Read the neighborhood law: `AGENTS.md` at the repo root.
3. Check the queue: tickets labeled `worker:{slug}` in store `{store}` only.
4. Do ONE slice of ONE ticket. Sign everything as `{slug}`.
5. Queue empty or stop rule hit: stop cleanly and say why.

If `$WORKFORCE_SHIFT_WORKDIR` is set, work and commit there (shift isolation).
**Land it** before close-out: FF-only push to `origin/main`, resolve conflicts
as a **union** (never wholesale overwrite), re-read shared files from main
before edit, cite the landing commit SHA in Links (PROCESS §5.1.3 / wf-172).
"""

# Appended when city templates lack the blurb — host-neutral;
# never requires editing ProtocolCity templates from this repo.
_SHIFT_CONTRACT_APPEND = """
## Shift worktree

When the roster sets `shift_worktree: true`, the engine spawns you with
`cwd=$WORKFORCE_SHIFT_WORKDIR` (linked worktree on `workforce/shift/{slug}`).
Primary checkout dirty is invisible to your index. Do not assume you are on
the primary checkout.

## Land it

Work is not done until it is on `origin/main`. Before close-out from the
shift tree:

1. Push FF-only to `origin/main` (e.g. `git push origin HEAD:main` when FF-able).
2. Resolve conflicts as a **union** — never wholesale-overwrite a shared file
   from a stale copy.
3. Before editing a shared file, re-read it from main; rebase if your copy
   predates HEAD.
4. Cite the landing commit SHA in close-out **Links**.
"""

_SHIFT_PROMPT_APPEND = """
If `$WORKFORCE_SHIFT_WORKDIR` is set, work and commit there (shift isolation).
**Land it** before close-out: FF-only push to `origin/main`, resolve conflicts
as a **union** (never wholesale overwrite), re-read shared files from main
before edit, cite the landing commit SHA in Links (PROCESS §5.1.3 / wf-172).
"""

# Soft shift one-liner present (older append / city copy) but no Land-it law.
_LAND_IT_CONTRACT_APPEND = """
## Land it

Work is not done until it is on `origin/main`. Before close-out from the
shift tree:

1. Push FF-only to `origin/main` (e.g. `git push origin HEAD:main` when FF-able).
2. Resolve conflicts as a **union** — never wholesale-overwrite a shared file
   from a stale copy.
3. Before editing a shared file, re-read it from main; rebase if your copy
   predates HEAD.
4. Cite the landing commit SHA in close-out **Links**.
"""

_LAND_IT_PROMPT_APPEND = """
**Land it** before close-out: FF-only push to `origin/main`, resolve conflicts
as a **union** (never wholesale overwrite), re-read shared files from main
before edit, cite the landing commit SHA in Links (PROCESS §5.1.3 / wf-172).
"""


def _has_shift_worktree_blurb(body: str) -> bool:
    return (
        "WORKFORCE_SHIFT_WORKDIR" in body
        or "shift worktree" in body.lower()
    )


def _has_land_it_blurb(body: str) -> bool:
    """True when papers already teach hard Land-it (not a soft 'land on main')."""
    lower = body.lower()
    if "land it" in lower:
        return True
    # Explicit landing-SHA close-out rule without the "Land it" heading.
    if "landing commit" in lower and "origin/main" in lower:
        return True
    return False


def _ensure_shift_worktree_blurb(body: str, *, dest: str, slug: str) -> str:
    """Guarantee planted papers mention shift isolation + Land-it.

    City templates may already soft-mention landing without the hard procedure;
    append only the missing piece. Never requires editing ProtocolCity templates
    from this repo.
    """
    base = os.path.basename(dest)
    is_contract = base == "CONTRACT.md"
    has_shift = _has_shift_worktree_blurb(body)
    has_land = _has_land_it_blurb(body)
    if has_shift and has_land:
        return body
    if not has_shift:
        # Full append already includes Land-it.
        if is_contract:
            append = _SHIFT_CONTRACT_APPEND.format(slug=slug)
        else:
            append = _SHIFT_PROMPT_APPEND
    else:
        # Soft shift blurb present; graft Land-it only.
        if is_contract:
            append = _LAND_IT_CONTRACT_APPEND
        else:
            append = _LAND_IT_PROMPT_APPEND
    return body.rstrip() + "\n" + append + "\n"


def slugify(name: str) -> str:
    """Persona → kebab-case identity slug."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def is_city_ops_workdir(workdir: str) -> bool:
    """True when *workdir* is (or lives under) city-ops: ``…/.protocolcity/ops``.

    City-ops seats share the Map "Office staff" bay via ``staff=true``.
    Path-part match keeps this host-neutral — no hard-coded city root.
    """
    if not workdir:
        return False
    try:
        parts = Path(os.path.abspath(workdir)).parts
    except (OSError, ValueError, TypeError):
        return False
    for i in range(len(parts) - 1):
        if parts[i] == ".protocolcity" and parts[i + 1] == "ops":
            return True
    return False


def resolve_staff_flag(workdir: str, staff: Optional[bool] = None) -> bool:
    """Decide staff= for a hire: explicit override, else city-ops auto."""
    if staff is not None:
        return bool(staff)
    return is_city_ops_workdir(workdir)


def resolve_type_alias(
    worker_type: str,
    kind: str,
    staff: Optional[bool],
) -> Tuple[str, Optional[bool]]:
    """Map citizen ``type=agent|staff|job`` onto wire kind/staff.

    ``type`` is authoritative when given; an explicitly conflicting kind or
    staff raises (kind=lane is the signature default, so only kind=job can
    conflict unambiguously). Returns (kind, staff) unchanged when no type.
    """
    t = (worker_type or "").strip().lower()
    if not t:
        return kind, staff
    if t not in WORKER_TYPES:
        raise RosterError(
            "type must be one of %s, got %r" % ("|".join(WORKER_TYPES), worker_type))
    want_kind = "lane" if t == "agent" else "job"
    want_staff = (t == "staff")
    kind_norm = (kind or "").strip().lower()
    if kind_norm == "job" and want_kind == "lane":
        raise RosterError("type=agent conflicts with kind=job")
    if staff is not None and bool(staff) != want_staff:
        raise RosterError(
            "type=%s conflicts with staff=%s" % (t, bool(staff)))
    return want_kind, want_staff


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
    # empty-run hygiene defaults — omit so older daemons stay loadable
    if int(spec.get("empty_run_threshold", 3) or 3) == 3:
        spec.pop("empty_run_threshold", None)
    if not int(spec.get("empty_run_backoff", 0) or 0):
        spec.pop("empty_run_backoff", None)
    # wf-166 — daily fire ceiling default 0 (unlimited); omit so older daemons load
    if not int(spec.get("max_fires_per_day", 0) or 0):
        spec.pop("max_fires_per_day", None)
    # staff=false is the default — only persist true (Map Office-staff bay, wf-143)
    if not spec.get("staff"):
        spec.pop("staff", None)
    # wf-153 slice 4 — load() defaults absent key by kind (lane→true, job→false).
    # Lanes: always persist so explicit false survives as opt-out (omitting
    # false would re-enable isolation on next load). Jobs: omit false (default);
    # persist true only when a job opts in.
    _kind = (spec.get("kind") or "lane")
    if isinstance(_kind, str):
        _kind = _kind.strip().lower()
    else:
        _kind = "lane"
    if _kind == "lane":
        spec["shift_worktree"] = bool(spec.get("shift_worktree"))
    elif not spec.get("shift_worktree"):
        spec.pop("shift_worktree", None)
    # wf-174 — persist max_passes for lanes (0 = budget drain is the hire
    # default; omit only when it would re-default wrongly). Jobs: omit 1
    # (single-pass default) so older daemons stay loadable.
    mp = int(spec.get("max_passes", 1) or 0)
    if _kind == "lane":
        spec["max_passes"] = mp
    elif mp == 1:
        spec.pop("max_passes", None)
    return spec


def _template_dir() -> Optional[Path]:
    env = os.environ.get("PROTOCOLCITY_TEMPLATES", "").strip()
    if env and os.path.isdir(env):
        return Path(env)
    # <city root>/workforce/workforce/hire.py → <city root>/ProtocolCity/templates
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
        body = _ensure_shift_worktree_blurb(body, dest=dest, slug=slug)
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
    staff: Optional[bool] = None,
    worker_type: str = "",
    shift_worktree: Optional[bool] = None,
    max_passes: Optional[int] = None,
) -> Dict[str, Any]:
    """Arm a worker: papers + roster row. Returns a result dict.

    ``staff`` — Map Office-staff bay. ``None`` auto-sets True when
    *workdir* is under ``.protocolcity/ops``; explicit True/False overrides.
    ``worker_type`` — citizen tier alias: agent|staff|job maps onto
    kind/staff; conflicting explicit kind=job or staff= raises.

    ``shift_worktree`` — per-shift git worktree isolation. ``None``
    defaults True for ``kind=lane`` (code hands share checkouts with founder
    sessions); False for jobs. Explicit True/False overrides.

    ``max_passes`` — multipass ceiling. ``None`` defaults 0 for
    lanes (budget-driven drain until empty/gated/budget/fault) and 1 for
    jobs (single-pass). Explicit int overrides.
    """
    base = base or os.getcwd()
    kind, staff = resolve_type_alias(worker_type, kind, staff)
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
    kind_norm = (kind or "lane").strip().lower()
    if display:
        disp = display.strip()
    elif kind_norm == "job" and role_title:
        # Scheduled jobs: public label is the function/role only (not
        # "Github-desk · Public Issues Intake"). Map OPS_TASK_LABELS + dig-in
        # then share one readable name across BP cities.
        disp = role_title
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
    _check_queue_url(queue_url, slug)

    path = _resolve_roster_path(roster_path, base)
    # Validate model before planting papers so a bad pin fails closed.
    local_root = os.path.join(base, "local") if base else None
    model = validate_model_pin(
        model,
        local_root=local_root if local_root and os.path.isdir(local_root) else None,
        roster_path=path if os.path.isfile(path) else None,
        base=base,
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

    staff_flag = resolve_staff_flag(workdir, staff)
    # Code lanes default on (shared-checkout incident class); jobs stay off.
    if shift_worktree is None:
        shift_wt = kind_norm == "lane"
    else:
        shift_wt = bool(shift_worktree)
    # wf-174 — lanes drain until budget/empty by default; jobs single-pass.
    if max_passes is None:
        mp = 0 if kind_norm == "lane" else 1
    else:
        mp = int(max_passes)

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
        max_passes=mp,
        schedule=schedule,
        queue_url=queue_url,
        queue_count_key=queue_count_key or "count",
        keychain_service=keychain_service,
        keychain_env=keychain_env,
        env=env_map,
        display=disp,
        usage_fields=dict(DEFAULT_USAGE_FIELDS),
        staff=staff_flag,
        shift_worktree=shift_wt,
    )
    w.validate()

    raw = _read_raw(path)
    workers = raw.setdefault("workers", {})
    if slug in workers:
        raise RosterError("worker %r already on the roster" % slug)
    for existing_name, spec in workers.items():
        if isinstance(spec, dict) and spec.get("identity") == identity:
            raise RosterError(
                "identity %r already used by %r" % (identity, existing_name)
            )

    # §5.2 registration assist: paste-ready row + registered flag.
    # Hire does not write PROCESS.md (other-repo / citizen law boundary).
    from .identity_registry import format_section_52_row, load_section_52_ids

    registered_ids, process_path = load_section_52_ids()
    identity_registered = identity in registered_ids if registered_ids else False
    papers_rel = os.path.join(
        os.path.basename(workdir), "workers", slug, "CONTRACT.md"
    )
    section_52_row = format_section_52_row(
        identity,
        display=disp,
        role=role_title,
        papers_rel=papers_rel,
        feed=(kind_norm == "lane"),
    )

    next_steps = [
        "Fill blanks in %s (take-list / never-touch)." % contract,
    ]
    if identity_registered:
        next_steps.append(
            "Signing id `%s` already in PROCESS §5.2 — no registry edit needed."
            % identity
        )
    else:
        dest = process_path or "worklane/PROCESS.md §5.2"
        next_steps.append(
            "Register signing id at Desk — paste this row into %s:" % dest
        )
        next_steps.append(section_52_row)
    next_steps.append("Optional dry-run: workforce dispatch %s --dry-run" % slug)

    result = {
        "ok": True,
        "armed": not dry_run,
        "dry_run": dry_run,
        "worker": {
            "name": slug,
            "identity": identity,
            "display": disp,
            "kind": kind,
            "model": model,
            "workdir": workdir,
            "contract": w.contract,
            "prompt": w.prompt,
            "schedule": schedule,
            "queue_url": queue_url,
            "staff": staff_flag,
            "type": w.worker_type,
            "shift_worktree": shift_wt,
            "max_passes": mp,
        },
        "papers_planted": papers_planted,
        "roster_path": path,
        "identity_registered": identity_registered,
        "section_52_row": section_52_row,
        "process_path": process_path,
        "next_steps": next_steps,
    }

    if dry_run:
        result["msg"] = "dry_run — papers %s; roster not written" % (
            "planted" if papers_planted else "checked"
        )
        return result

    spec = worker_to_spec(w)
    # Store paths relative to base so the roster survives workspace moves.
    # Resolve to absolute at load time (roster.load); existing absolute entries
    # in the JSON are unaffected — os.path.isabs guards the resolve step.
    for _f in ("workdir", "contract", "prompt"):
        if spec.get(_f):
            spec[_f] = os.path.relpath(spec[_f], base)
    workers[slug] = spec
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

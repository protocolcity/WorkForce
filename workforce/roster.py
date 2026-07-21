"""Roster — who is employed on this machine.

The roster is machine-local mutable config (gitignored ``local/roster.json``;
``roster.example.json`` is the versioned reference). Schedules are DATA here:
the daemon (slice 3) reads them; nothing is ever installed per worker.

Host-neutrality rule: this module knows nothing about any particular desk or
workplace. Desk URLs, queue probes, and env passthroughs are roster fields —
the seams live in config, never in code.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DEFAULT_ROSTER_PATHS = ("local/roster.json", "roster.json")


class RosterError(ValueError):
    """Roster file missing, malformed, or failing validation."""


@dataclass
class Worker:
    """One employed worker: identity + law paths + dispatch mechanics.

    ``kind`` is the Charter's lanes-vs-jobs split: a ``lane`` claims work
    orders; a ``job`` observes and never claims (its queue probe is its
    trigger condition, or absent for calendar jobs).
    """

    name: str
    workdir: str                       # neighborhood root; dispatch cwd
    contract: str                      # L2 law — canonical path, read at dispatch
    prompt: str                        # L3 shift brief — canonical path
    identity: str                      # registered signing identity (exactly one)
    command: List[str]                 # argv; {prompt_text} and {model} substituted
    kind: str = "lane"                 # lane | job
    model: str = ""                    # pin; empty = vendor default (explicit choice)
    budget_secs: int = 1500            # wall-clock hard-kill budget (whole shift)
    max_passes: int = 1                # §6 multi-pass ceiling; 1 = single-pass
    min_pass_secs: int = 600           # never start a pass you can't finish
    schedule: str = ""                 # five-field cron = daemon-owned; else informational
    predirty_env: str = ""             # env var name for the §7 snapshot path (host guards may expect their own name); empty = WORKFORCE_PREDIRTY
    queue_url: str = ""                # GET returning JSON; empty = no queue preflight (jobs)
    queue_count_key: str = "count"     # dot-path to the ready count in the probe JSON
    min_free_mb: int = 2048            # disk preflight floor
    keychain_service: str = ""         # secret fetched at dispatch (never persisted)
    keychain_env: str = ""             # env var the secret is exposed as
    env: Dict[str, str] = field(default_factory=dict)  # non-secret passthrough
    display: str = ""                  # "Persona · Role" board label; empty → name
    succeeds: str = ""                 # retired id this hire succeeds (audit only)
    owner: str = ""                    # accountable human or lane: owner-terminal | worker name
    skill: str = ""                    # optional capability id (matches .claude/skills/<id>)
    usage_fields: Dict[str, str] = field(default_factory=dict)  # ledger key -> dot-path into the pass's JSON output (consumption telemetry, oc-35); vendor specifics stay in config
    authority_chain: List[str] = field(default_factory=list)  # §6 explicit ordered law-file paths (L0 city, L1 business, ...); empty = no chain declared
    authority_chain_required: bool = False  # when True, dispatch fails closed (NO_AUTHORITY_CHAIN) if authority_chain is empty
    staff: bool = False                    # permanent Office seat — boards group separately; hire gate blocks re-hire
    ghost_audit: List[str] = field(default_factory=list)  # §8 pre-shift reconciler argv; empty = no audit
    scope_home: str = ""                # when set, realpath(workdir) must fall within realpath(scope_home) or a perimeter grant; empty = no enforcement
    perimeter_grants: List[str] = field(default_factory=list)  # additional allowed roots (PERIMETER row grants)

    def validate(self) -> None:
        if not self.name:
            raise RosterError("worker missing name")
        for f in ("workdir", "contract", "prompt", "identity"):
            if not getattr(self, f):
                raise RosterError("worker %r missing required field %r" % (self.name, f))
        if not self.command:
            raise RosterError("worker %r missing command" % self.name)
        if self.kind not in ("lane", "job"):
            raise RosterError("worker %r kind must be lane|job, got %r" % (self.name, self.kind))
        if self.max_passes < 1:
            raise RosterError("worker %r max_passes must be >= 1" % self.name)
        if self.max_passes > 1 and not self.queue_url:
            raise RosterError(
                "worker %r wants multi-pass but has no queue probe — the "
                "no-progress stop (§6) needs one" % self.name)
        if bool(self.keychain_service) != bool(self.keychain_env):
            raise RosterError(
                "worker %r must set keychain_service and keychain_env together" % self.name
            )
        for k, v in self.usage_fields.items():
            if not k or not v or not isinstance(v, str):
                raise RosterError(
                    "worker %r usage_fields must map ledger keys to dot-paths" % self.name)


_OWNER_SPECIAL = frozenset({"owner-terminal", "you", "founder"})


def _validate_owners(workers: Dict[str, Worker]) -> None:
    """Jobs may name an accountable owner: Office Manager or a roster lane/job."""
    for w in workers.values():
        own = (w.owner or "").strip()
        if not own:
            continue
        if own in _OWNER_SPECIAL or own in workers:
            continue
        raise RosterError(
            "worker %r owner %r is not owner-terminal/you and not on this roster"
            % (w.name, own)
        )


@dataclass
class Roster:
    workers: Dict[str, Worker]
    path: str

    def worker(self, name: str) -> Worker:
        try:
            return self.workers[name]
        except KeyError:
            raise RosterError(
                "no worker %r in roster %s (have: %s)"
                % (name, self.path, ", ".join(sorted(self.workers)) or "none")
            )


def _resolve_path(explicit: Optional[str], base: str) -> str:
    if explicit:
        return explicit
    for candidate in DEFAULT_ROSTER_PATHS:
        p = os.path.join(base, candidate)
        if os.path.exists(p):
            return p
    raise RosterError(
        "no roster found (looked for %s under %s); pass --file or set WORKFORCE_ROSTER"
        % (", ".join(DEFAULT_ROSTER_PATHS), base)
    )


def load(path: Optional[str] = None, base: Optional[str] = None) -> Roster:
    base = base or os.getcwd()
    path = _resolve_path(
        path or os.environ.get("WORKFORCE_ROSTER") or os.environ.get("WORKFORCE_ROSTER"),
        base,
    )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RosterError("cannot read roster %s: %s" % (path, exc))

    workers_raw = raw.get("workers")
    if not isinstance(workers_raw, dict) or not workers_raw:
        raise RosterError("roster %s has no workers" % path)

    identities = {}
    workers: Dict[str, Worker] = {}
    for name, spec in workers_raw.items():
        if not isinstance(spec, dict):
            raise RosterError("worker %r spec must be an object" % name)
        known = {f for f in Worker.__dataclass_fields__ if f != "name"}
        unknown = set(spec) - known
        if unknown:
            raise RosterError("worker %r has unknown fields: %s" % (name, ", ".join(sorted(unknown))))
        w = Worker(name=name, **spec)
        w.validate()
        if w.identity in identities:
            raise RosterError(
                "identity %r used by both %r and %r — one worker, one identity"
                % (w.identity, identities[w.identity], name)
            )
        identities[w.identity] = name
        workers[name] = w
    _validate_owners(workers)
    return Roster(workers=workers, path=path)

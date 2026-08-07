"""Roster — who is employed on this machine.

The roster is machine-local mutable config (gitignored ``local/roster.json``;
``roster.example.json`` is the versioned reference). Schedules are DATA here:
the daemon (slice 3) reads them; nothing is ever installed per worker.

Host-neutrality rule: this module knows nothing about any particular desk or
workplace. Desk URLs, queue probes, and env passthroughs are roster fields —
the seams live in config, never in code.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_log = logging.getLogger(__name__)

DEFAULT_ROSTER_PATHS = ("local/roster.json", "roster.json")

# wf-154 · citizen three-tier taxonomy: derived from kind + staff.
# Wire values stay kind=lane|job; type is never persisted, always derived.
WORKER_TYPES = ("agent", "staff", "job")


class RosterError(ValueError):
    """Roster file missing, malformed, or failing validation."""


@dataclass
class Worker:
    """One employed worker: identity + law paths + dispatch mechanics.

    ``kind`` is the Charter's lanes-vs-jobs split: a ``lane`` claims work
    orders; a ``job`` observes and never claims (its queue probe is its
    trigger condition, or absent for calendar jobs).  Wire values are
    strictly ``lane`` or ``job``; citizen UIs may label these "worker" or
    "Agent" — those surface labels are not valid roster values.
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
    # §6 multi-pass ceiling:
    #   0 = budget-driven drain (stop only on empty / no-progress / budget floor /
    #       fault; engine enforces MAX_PASSES_HARD safety rail)
    #   1 = single-pass (legacy default for jobs / explicit lane opt-out)
    #   N>1 = soft ceiling after N successful passes
    # Dataclass default 1 keeps Worker() tests single-pass; load() and hire
    # default 0 for kind=lane when the key is absent (founder drain-loop ruling).
    max_passes: int = 1
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
    fallback_runtime: str = ""          # CLI name to retry on quota hit (must be in KNOWN_RUNTIMES); empty = no fallback
    fallback_model: str = ""            # model pin for the fallback shift; empty = vendor default
    # ALWAYS_WORK §4 / wf-111 — empty-run hygiene (host may pin per seat)
    empty_run_threshold: int = 3        # N consecutive queue-empty SKIPs → one WARN health signal
    empty_run_backoff: int = 0          # seconds to suppress cron fires after threshold; 0 = signal only
    # wf-125 — pause until ready: probe queue on each tick after threshold; suppress if still empty
    empty_run_pause: bool = False       # requires queue_url; auto-resumes when queue returns ready
    # wf-149 — adaptive idle backoff: after threshold, cadence stretches 1h → 4h → daily
    # heartbeat (never stops — the daily probe is the desk-reachability canary). Any wake
    # or non-empty probe resets to base. Only engages when pause/backoff are unset.
    empty_run_adaptive: bool = True     # False = legacy signal-only when backoff=0
    # wf-126 — vendor-limit backoff: suppress cron fires after N consecutive vendor_limit shifts
    vendor_limit_threshold: int = 3    # N consecutive vendor_limit shifts to trigger (0 = off)
    vendor_limit_backoff: int = 0      # seconds to suppress after threshold; 0 = signal only
    # wf-166 — daily fire ceiling: suppress further scheduled fires after N START
    # events on the host-local calendar day. 0 = unlimited (host-neutral default).
    # Recommended pin for aggressive hourly seats (e.g. CoS): max_fires_per_day: 1.
    # Manual fire_now bypasses; only the daemon tick path enforces.
    max_fires_per_day: int = 0
    # wf-153 — shift worktree isolation: spawn cwd under local/worktrees/<name>
    # so founder/other dirty on the primary checkout cannot enter hand commits.
    # Dataclass default false (safe for direct Worker() in tests). load() and
    # hire default true for kind=lane when the key is absent (slice 4); jobs
    # stay false. Explicit false on a lane row is a permanent opt-out.
    shift_worktree: bool = False

    @property
    def worker_type(self) -> str:
        """Citizen tier: agent = claims from a ready feed;
        staff = shipped workspace seat (kind=job + staff); job = plumbing.
        Derived, never stored — kind stays the lane|job wire value."""
        if self.kind == "lane":
            return "agent"
        return "staff" if self.staff else "job"

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
        if self.max_passes < 0:
            raise RosterError(
                "worker %r max_passes must be >= 0 (0 = budget-driven drain)"
                % self.name
            )
        # Budget-driven drain (0) and multi-pass (N>1) both re-probe the ready
        # feed between passes — need a queue_url for the no-progress stop.
        if self.max_passes != 1 and not self.queue_url:
            raise RosterError(
                "worker %r wants multi-pass/drain (max_passes=%s) but has no "
                "queue probe — the no-progress stop (§6) needs one"
                % (self.name, self.max_passes)
            )
        if bool(self.keychain_service) != bool(self.keychain_env):
            raise RosterError(
                "worker %r must set keychain_service and keychain_env together" % self.name
            )
        for k, v in self.usage_fields.items():
            if not k or not v or not isinstance(v, str):
                raise RosterError(
                    "worker %r usage_fields must map ledger keys to dot-paths" % self.name)
        if self.fallback_runtime:
            from .runtimes import KNOWN_RUNTIMES
            if self.fallback_runtime not in KNOWN_RUNTIMES:
                raise RosterError(
                    "worker %r fallback_runtime %r not in KNOWN_RUNTIMES (%s)"
                    % (self.name, self.fallback_runtime, ", ".join(KNOWN_RUNTIMES))
                )
        if int(self.empty_run_threshold) < 1:
            raise RosterError(
                "worker %r empty_run_threshold must be >= 1" % self.name
            )
        if int(self.empty_run_backoff) < 0:
            raise RosterError(
                "worker %r empty_run_backoff must be >= 0" % self.name
            )
        if int(self.vendor_limit_threshold) < 0:
            raise RosterError(
                "worker %r vendor_limit_threshold must be >= 0" % self.name
            )
        if int(self.vendor_limit_backoff) < 0:
            raise RosterError(
                "worker %r vendor_limit_backoff must be >= 0" % self.name
            )
        if int(self.vendor_limit_backoff) > 0 and int(self.vendor_limit_threshold) < 1:
            raise RosterError(
                "worker %r vendor_limit_backoff > 0 requires vendor_limit_threshold >= 1"
                % self.name
            )
        if int(self.max_fires_per_day) < 0:
            raise RosterError(
                "worker %r max_fires_per_day must be >= 0 (0 = unlimited)" % self.name
            )


_OWNER_SPECIAL = frozenset({"owner-terminal", "you", "founder"})


def _validate_owners(workers: Dict[str, Worker]) -> None:
    """Jobs may name an accountable owner: Office Manager or a roster lane/job."""
    for w in workers.values():
        own = (w.owner or "").strip()
        if not own:
            continue
        if own in _OWNER_SPECIAL or own in workers:
            continue
        _log.error(
            "roster: worker %r owner %r is not on this roster — owner field ignored",
            w.name, own,
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
        try:
            if not isinstance(spec, dict):
                raise RosterError("worker %r spec must be an object" % name)
            known = {f for f in Worker.__dataclass_fields__ if f != "name"}
            unknown = set(spec) - known
            if unknown:
                raise RosterError("worker %r has unknown fields: %s" % (name, ", ".join(sorted(unknown))))
            # Resolve relative paths to absolute using base.
            # Absolute entries (legacy or hand-written) pass through unchanged.
            for _f in ("workdir", "contract", "prompt"):
                _v = spec.get(_f, "")
                if _v and not os.path.isabs(_v):
                    spec[_f] = os.path.normpath(os.path.join(base, _v))
            # wf-153 slice 4 — absent key inherits hire defaults: lanes on,
            # jobs off. Explicit false on a lane is an opt-out (must be
            # persisted; see worker_to_spec). Matches hire kind defaults so
            # pre-flag live rosters isolate without a citizen roster rewrite.
            _kind = spec.get("kind") or "lane"
            if isinstance(_kind, str):
                _kind = _kind.strip().lower()
            else:
                _kind = "lane"
            if "shift_worktree" not in spec:
                spec["shift_worktree"] = (_kind == "lane")
            # wf-174 — absent max_passes: lanes *with* a ready feed drain until
            # budget/empty/fault; jobs and queue-less rows stay single-pass
            # (drain needs a probe for the no-progress stop). Explicit 1 on a
            # lane remains a single-pass opt-out (doctor notes drain-stunted).
            if "max_passes" not in spec:
                has_queue = bool((spec.get("queue_url") or "").strip())
                if _kind == "lane" and has_queue:
                    spec["max_passes"] = 0
                else:
                    spec["max_passes"] = 1
            w = Worker(name=name, **spec)
            w.validate()
            if w.identity in identities:
                raise RosterError(
                    "identity %r used by both %r and %r — one worker, one identity"
                    % (w.identity, identities[w.identity], name)
                )
            identities[w.identity] = name
            workers[name] = w
        except (RosterError, TypeError) as exc:
            _log.error("roster: skipping worker %r — %s", name, exc)
    _validate_owners(workers)
    return Roster(workers=workers, path=path)

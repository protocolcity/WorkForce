"""Capacity policy file — user-editable pin rails.

Envelope for Mode B staging / ``workforce repin``: allowed pin
pairs, seats/day cap, cooldown, and which roster fields may change.

Host-neutral: resolves ``local/capacity_policy.json`` under the data dir
(or an explicit path / env). Never invents pairs when the file is missing
— consumers that need the envelope must load it; absence is an error.

Shipped reference: repo-root ``capacity_policy.example.json`` (citizen
copies into the data dir). Hands never write machine-local policy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_ID = "workforce.capacity_policy/v1"
DEFAULT_BASENAME = "capacity_policy.json"
EXAMPLE_BASENAME = "capacity_policy.example.json"
ENV_PATH = "WORKFORCE_CAPACITY_POLICY"

# Founder-encoded defaults mirrored in the shipped example (runbook rails).
DEFAULT_SEATS_PER_DAY = 10
DEFAULT_COOLDOWN_HOURS = 6
DEFAULT_ALLOWED_FIELDS = ("model", "command")


class CapacityPolicyError(ValueError):
    """Policy file missing, malformed, or failing validation."""


@dataclass(frozen=True)
class PinEndpoint:
    """One side of an allowed re-pin pair."""

    model: str
    runtime: str = ""  # CLI basename hint (claude, grok, …); empty = any


@dataclass(frozen=True)
class PinPair:
    """Bidirectional (default) or one-way allowed model transition."""

    id: str
    a: PinEndpoint
    b: PinEndpoint
    tier: str = ""
    description: str = ""
    bidirectional: bool = True


@dataclass(frozen=True)
class CapacityPolicy:
    """Validated capacity policy envelope."""

    seats_per_day: int
    cooldown_hours: int
    allowed_fields: Tuple[str, ...]
    pin_pairs: Tuple[PinPair, ...]
    stay_pinned_tiers: Tuple[str, ...] = ()
    source_path: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def allows_field(self, name: str) -> bool:
        return name in self.allowed_fields

    def find_pair_for_models(
        self, from_model: str, to_model: str
    ) -> Optional[PinPair]:
        """Return the first pin pair that authorizes from_model → to_model."""
        if not from_model or not to_model:
            return None
        if from_model == to_model:
            return None
        for pair in self.pin_pairs:
            if pair.a.model == from_model and pair.b.model == to_model:
                return pair
            if pair.bidirectional and pair.b.model == from_model and pair.a.model == to_model:
                return pair
        return None

    def is_allowed_transition(self, from_model: str, to_model: str) -> bool:
        return self.find_pair_for_models(from_model, to_model) is not None

    def known_models(self) -> Tuple[str, ...]:
        """Full model pins that appear as pin-pair endpoints (hire/repin rails)."""
        seen: List[str] = []
        for pair in self.pin_pairs:
            for m in (pair.a.model, pair.b.model):
                if m and m not in seen:
                    seen.append(m)
        return tuple(seen)


def _pkg_repo_root() -> str:
    """Repo root when installed as source tree (…/workforce/workforce/this.py)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def example_policy_path() -> str:
    """Absolute path to the shipped example template (may not exist in wheels)."""
    return os.path.join(_pkg_repo_root(), EXAMPLE_BASENAME)


def resolve_policy_path(
    path: Optional[str] = None,
    local_root: Optional[str] = None,
) -> str:
    """Resolve the policy file path without reading it.

    Order: explicit ``path`` → ``$WORKFORCE_CAPACITY_POLICY`` →
    ``{local_root}/capacity_policy.json`` → ``{cwd}/local/capacity_policy.json``.
    """
    if path:
        return os.path.abspath(path)
    env = (os.environ.get(ENV_PATH) or "").strip()
    if env:
        return os.path.abspath(env)
    if local_root:
        return os.path.abspath(os.path.join(local_root, DEFAULT_BASENAME))
    return os.path.abspath(os.path.join(os.getcwd(), "local", DEFAULT_BASENAME))


def _require_dict(obj: Any, label: str) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise CapacityPolicyError("%s must be a JSON object" % label)
    return obj


def _require_str(obj: Any, label: str) -> str:
    if not isinstance(obj, str) or not obj.strip():
        raise CapacityPolicyError("%s must be a non-empty string" % label)
    return obj.strip()


def _require_nonneg_int(obj: Any, label: str) -> int:
    if isinstance(obj, bool) or not isinstance(obj, int):
        raise CapacityPolicyError("%s must be an integer" % label)
    if obj < 0:
        raise CapacityPolicyError("%s must be >= 0" % label)
    return obj


def _parse_endpoint(raw: Any, label: str) -> PinEndpoint:
    d = _require_dict(raw, label)
    model = _require_str(d.get("model"), "%s.model" % label)
    runtime = d.get("runtime", "")
    if runtime is None:
        runtime = ""
    if not isinstance(runtime, str):
        raise CapacityPolicyError("%s.runtime must be a string" % label)
    return PinEndpoint(model=model, runtime=runtime.strip())


def _parse_pair(raw: Any, index: int) -> PinPair:
    label = "pin_pairs[%d]" % index
    d = _require_dict(raw, label)
    pair_id = _require_str(d.get("id"), "%s.id" % label)
    # Accept either a/b or primary/fallback (example uses a/b).
    if "a" in d and "b" in d:
        a = _parse_endpoint(d.get("a"), "%s.a" % label)
        b = _parse_endpoint(d.get("b"), "%s.b" % label)
    elif "primary" in d and "fallback" in d:
        a = _parse_endpoint(d.get("primary"), "%s.primary" % label)
        b = _parse_endpoint(d.get("fallback"), "%s.fallback" % label)
    else:
        raise CapacityPolicyError(
            "%s needs endpoints a+b (or primary+fallback)" % label
        )
    if a.model == b.model:
        raise CapacityPolicyError(
            "%s endpoints must differ (both model %r)" % (label, a.model)
        )
    tier = d.get("tier", "") or ""
    if not isinstance(tier, str):
        raise CapacityPolicyError("%s.tier must be a string" % label)
    description = d.get("description", "") or ""
    if not isinstance(description, str):
        raise CapacityPolicyError("%s.description must be a string" % label)
    bi = d.get("bidirectional", True)
    if not isinstance(bi, bool):
        raise CapacityPolicyError("%s.bidirectional must be a boolean" % label)
    return PinPair(
        id=pair_id,
        a=a,
        b=b,
        tier=tier.strip(),
        description=description.strip(),
        bidirectional=bi,
    )


def validate_policy_dict(data: Dict[str, Any]) -> CapacityPolicy:
    """Validate a raw dict into a CapacityPolicy (no I/O)."""
    data = _require_dict(data, "policy")
    schema = data.get("schema")
    if schema != SCHEMA_ID:
        raise CapacityPolicyError(
            "unsupported schema %r (want %r)" % (schema, SCHEMA_ID)
        )

    seats = data.get("seats_per_day", DEFAULT_SEATS_PER_DAY)
    seats_i = _require_nonneg_int(seats, "seats_per_day")
    if seats_i == 0:
        raise CapacityPolicyError("seats_per_day must be >= 1")

    cooldown = data.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS)
    cooldown_i = _require_nonneg_int(cooldown, "cooldown_hours")

    fields_raw = data.get("allowed_fields", list(DEFAULT_ALLOWED_FIELDS))
    if not isinstance(fields_raw, list) or not fields_raw:
        raise CapacityPolicyError("allowed_fields must be a non-empty list")
    allowed: List[str] = []
    for i, f in enumerate(fields_raw):
        if not isinstance(f, str) or not f.strip():
            raise CapacityPolicyError(
                "allowed_fields[%d] must be a non-empty string" % i
            )
        name = f.strip()
        if name in allowed:
            raise CapacityPolicyError("allowed_fields duplicate %r" % name)
        allowed.append(name)
    # Mode B envelope: only pin-related fields; reject schedule etc. by default list
    # but do not hard-forbid extra names here — stager enforces against this list.

    pairs_raw = data.get("pin_pairs")
    if not isinstance(pairs_raw, list) or not pairs_raw:
        raise CapacityPolicyError("pin_pairs must be a non-empty list")
    pairs: List[PinPair] = []
    seen_ids: set = set()
    for i, p in enumerate(pairs_raw):
        pair = _parse_pair(p, i)
        if pair.id in seen_ids:
            raise CapacityPolicyError("duplicate pin_pairs id %r" % pair.id)
        seen_ids.add(pair.id)
        pairs.append(pair)

    stay: List[str] = []
    stay_raw = data.get("stay_pinned_tiers") or data.get("stay_pinned") or []
    if isinstance(stay_raw, dict):
        # Allow {"tiers": ["heavy_multipass"], "note": "..."}
        stay_raw = stay_raw.get("tiers") or []
    if not isinstance(stay_raw, list):
        raise CapacityPolicyError("stay_pinned_tiers must be a list")
    for i, t in enumerate(stay_raw):
        if not isinstance(t, str) or not t.strip():
            raise CapacityPolicyError(
                "stay_pinned_tiers[%d] must be a non-empty string" % i
            )
        stay.append(t.strip())

    return CapacityPolicy(
        seats_per_day=seats_i,
        cooldown_hours=cooldown_i,
        allowed_fields=tuple(allowed),
        pin_pairs=tuple(pairs),
        stay_pinned_tiers=tuple(stay),
        source_path="",
        raw=data,
    )


def load_capacity_policy(
    path: Optional[str] = None,
    local_root: Optional[str] = None,
    *,
    required: bool = True,
) -> Optional[CapacityPolicy]:
    """Load and validate the capacity policy file.

    When ``required`` is False and the file is missing, returns None.
    Malformed files always raise CapacityPolicyError.
    """
    resolved = resolve_policy_path(path=path, local_root=local_root)
    if not os.path.isfile(resolved):
        if not required:
            return None
        example = example_policy_path()
        hint = (
            "capacity policy not found at %s — copy the shipped template "
            "(%s) to local/capacity_policy.json (citizen write)"
            % (resolved, example if os.path.isfile(example) else EXAMPLE_BASENAME)
        )
        raise CapacityPolicyError(hint)
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise CapacityPolicyError("cannot read %s: %s" % (resolved, exc)) from exc
    except json.JSONDecodeError as exc:
        raise CapacityPolicyError(
            "invalid JSON in %s: %s" % (resolved, exc)
        ) from exc
    if not isinstance(data, dict):
        raise CapacityPolicyError("%s root must be a JSON object" % resolved)
    policy = validate_policy_dict(data)
    # dataclasses.replace not needed — rebuild with source_path
    return CapacityPolicy(
        seats_per_day=policy.seats_per_day,
        cooldown_hours=policy.cooldown_hours,
        allowed_fields=policy.allowed_fields,
        pin_pairs=policy.pin_pairs,
        stay_pinned_tiers=policy.stay_pinned_tiers,
        source_path=resolved,
        raw=policy.raw,
    )


def load_example_policy() -> CapacityPolicy:
    """Load the shipped example template (for tests / docs tooling)."""
    path = example_policy_path()
    if not os.path.isfile(path):
        raise CapacityPolicyError("shipped example missing: %s" % path)
    return load_capacity_policy(path=path)

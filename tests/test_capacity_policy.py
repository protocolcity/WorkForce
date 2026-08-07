"""Capacity policy file loader + validation."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import capacity_policy as cp  # noqa: E402


def _minimal_policy(**overrides):
    base = {
        "schema": cp.SCHEMA_ID,
        "seats_per_day": 10,
        "cooldown_hours": 6,
        "allowed_fields": ["model", "command"],
        "pin_pairs": [
            {
                "id": "generalist-claude-grok",
                "tier": "generalist",
                "a": {"model": "claude-sonnet-4-6", "runtime": "claude"},
                "b": {"model": "grok-4.5", "runtime": "grok"},
                "bidirectional": True,
            }
        ],
        "stay_pinned_tiers": ["heavy_multipass"],
    }
    base.update(overrides)
    return base


def test_validate_minimal_ok():
    p = cp.validate_policy_dict(_minimal_policy())
    assert p.seats_per_day == 10
    assert p.cooldown_hours == 6
    assert p.allowed_fields == ("model", "command")
    assert len(p.pin_pairs) == 1
    assert p.pin_pairs[0].id == "generalist-claude-grok"
    assert p.stay_pinned_tiers == ("heavy_multipass",)


def test_allowed_transition_bidirectional():
    p = cp.validate_policy_dict(_minimal_policy())
    assert p.is_allowed_transition("claude-sonnet-4-6", "grok-4.5")
    assert p.is_allowed_transition("grok-4.5", "claude-sonnet-4-6")
    assert not p.is_allowed_transition("claude-sonnet-4-6", "claude-haiku-4-5-20251001")
    assert not p.is_allowed_transition("claude-sonnet-4-6", "claude-sonnet-4-6")


def test_known_models_from_pin_pairs():
    p = cp.validate_policy_dict(_minimal_policy())
    models = p.known_models()
    assert "claude-sonnet-4-6" in models
    assert "grok-4.5" in models
    assert len(models) == 2


def test_one_way_pair():
    data = _minimal_policy(
        pin_pairs=[
            {
                "id": "one-way",
                "a": {"model": "m-a"},
                "b": {"model": "m-b"},
                "bidirectional": False,
            }
        ]
    )
    p = cp.validate_policy_dict(data)
    assert p.is_allowed_transition("m-a", "m-b")
    assert not p.is_allowed_transition("m-b", "m-a")


def test_primary_fallback_alias():
    data = _minimal_policy(
        pin_pairs=[
            {
                "id": "alias",
                "primary": {"model": "p1", "runtime": "r1"},
                "fallback": {"model": "p2", "runtime": "r2"},
            }
        ]
    )
    p = cp.validate_policy_dict(data)
    assert p.is_allowed_transition("p1", "p2")


def test_rejects_wrong_schema():
    with pytest.raises(cp.CapacityPolicyError, match="unsupported schema"):
        cp.validate_policy_dict(_minimal_policy(schema="other/v0"))


def test_rejects_empty_pin_pairs():
    with pytest.raises(cp.CapacityPolicyError, match="pin_pairs"):
        cp.validate_policy_dict(_minimal_policy(pin_pairs=[]))


def test_rejects_duplicate_pair_id():
    pair = {
        "id": "dup",
        "a": {"model": "a1"},
        "b": {"model": "b1"},
    }
    with pytest.raises(cp.CapacityPolicyError, match="duplicate"):
        cp.validate_policy_dict(_minimal_policy(pin_pairs=[pair, dict(pair)]))


def test_rejects_same_endpoint_models():
    with pytest.raises(cp.CapacityPolicyError, match="differ"):
        cp.validate_policy_dict(
            _minimal_policy(
                pin_pairs=[
                    {
                        "id": "bad",
                        "a": {"model": "same"},
                        "b": {"model": "same"},
                    }
                ]
            )
        )


def test_rejects_zero_seats():
    with pytest.raises(cp.CapacityPolicyError, match="seats_per_day"):
        cp.validate_policy_dict(_minimal_policy(seats_per_day=0))


def test_rejects_negative_cooldown():
    with pytest.raises(cp.CapacityPolicyError, match="cooldown_hours"):
        cp.validate_policy_dict(_minimal_policy(cooldown_hours=-1))


def test_load_from_path(tmp_path):
    path = tmp_path / "capacity_policy.json"
    path.write_text(json.dumps(_minimal_policy()), encoding="utf-8")
    p = cp.load_capacity_policy(path=str(path))
    assert p is not None
    assert p.source_path == str(path.resolve()) or os.path.samefile(
        p.source_path, str(path)
    )
    assert p.seats_per_day == 10


def test_load_from_local_root(tmp_path):
    path = tmp_path / "capacity_policy.json"
    path.write_text(json.dumps(_minimal_policy()), encoding="utf-8")
    p = cp.load_capacity_policy(local_root=str(tmp_path))
    assert p is not None
    assert p.is_allowed_transition("claude-sonnet-4-6", "grok-4.5")


def test_missing_required_raises(tmp_path):
    with pytest.raises(cp.CapacityPolicyError, match="not found"):
        cp.load_capacity_policy(local_root=str(tmp_path), required=True)


def test_missing_optional_returns_none(tmp_path):
    assert cp.load_capacity_policy(local_root=str(tmp_path), required=False) is None


def test_invalid_json_raises(tmp_path):
    path = tmp_path / "capacity_policy.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(cp.CapacityPolicyError, match="invalid JSON"):
        cp.load_capacity_policy(path=str(path))


def test_env_path_override(tmp_path, monkeypatch):
    path = tmp_path / "custom.json"
    path.write_text(json.dumps(_minimal_policy(seats_per_day=3)), encoding="utf-8")
    monkeypatch.setenv(cp.ENV_PATH, str(path))
    p = cp.load_capacity_policy(local_root=str(tmp_path / "other"))
    assert p.seats_per_day == 3


def test_shipped_example_loads_and_encodes_founder_rails():
    """Founder rails live in capacity_policy.example.json (citizen copies to local/)."""
    p = cp.load_example_policy()
    assert p.raw.get("schema") == cp.SCHEMA_ID
    assert p.seats_per_day >= 1
    assert p.cooldown_hours >= 0
    assert "model" in p.allowed_fields
    assert "command" in p.allowed_fields
    # Generalist claude ↔ grok
    assert p.is_allowed_transition("claude-sonnet-4-6", "grok-4.5")
    assert p.is_allowed_transition("grok-4.5", "claude-sonnet-4-6")
    # Patrol haiku ↔ grok
    assert p.is_allowed_transition("claude-haiku-4-5-20251001", "grok-4.5")
    assert "heavy_multipass" in p.stay_pinned_tiers
    pair_ids = {pair.id for pair in p.pin_pairs}
    assert "generalist-claude-grok" in pair_ids
    assert "patrol-haiku-grok" in pair_ids


def test_allows_field():
    p = cp.validate_policy_dict(_minimal_policy())
    assert p.allows_field("model")
    assert p.allows_field("command")
    assert not p.allows_field("schedule")
    assert not p.allows_field("budget_secs")

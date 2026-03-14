"""Tests for persona profile, temporal weighting, and persona store CRUD."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from review_bot.persona.profile import PersonaProfile, Priority, SeverityPattern
from review_bot.persona.store import PersonaStore
from review_bot.persona.temporal import apply_weights, weight_comment


# ---------------------------------------------------------------------------
# PersonaProfile Serialization
# ---------------------------------------------------------------------------


class TestPersonaProfileSerialization:
    """Test YAML round-trip for PersonaProfile."""

    def test_to_yaml_and_back(self, sample_persona):
        yaml_str = sample_persona.to_yaml()
        restored = PersonaProfile.from_yaml(yaml_str)

        assert restored.name == sample_persona.name
        assert restored.github_user == sample_persona.github_user
        assert restored.tone == sample_persona.tone
        assert len(restored.priorities) == len(sample_persona.priorities)
        assert restored.priorities[0].category == "error_handling"
        assert restored.pet_peeves == sample_persona.pet_peeves
        assert restored.severity_pattern.blocks_on == sample_persona.severity_pattern.blocks_on

    def test_minimal_profile_round_trip(self, minimal_persona):
        yaml_str = minimal_persona.to_yaml()
        restored = PersonaProfile.from_yaml(yaml_str)
        assert restored.name == "bob"
        assert restored.priorities == []
        assert restored.pet_peeves == []
        assert restored.tone == ""

    def test_yaml_is_valid_yaml(self, sample_persona):
        yaml_str = sample_persona.to_yaml()
        data = yaml.safe_load(yaml_str)
        assert isinstance(data, dict)
        assert data["name"] == "alice"

    def test_from_yaml_with_extra_fields(self):
        """Extra fields should be ignored by Pydantic."""
        yaml_str = yaml.dump({
            "name": "test",
            "github_user": "testuser",
            "unknown_field": "should be ignored",
        })
        profile = PersonaProfile.from_yaml(yaml_str)
        assert profile.name == "test"

    def test_overrides_preserved(self, sample_persona):
        yaml_str = sample_persona.to_yaml()
        restored = PersonaProfile.from_yaml(yaml_str)
        assert restored.overrides == ["Always check for type hints"]


# ---------------------------------------------------------------------------
# Priority and SeverityPattern Models
# ---------------------------------------------------------------------------


class TestPriorityModel:
    def test_priority_fields(self):
        p = Priority(category="naming", severity="moderate", description="Use good names")
        assert p.category == "naming"
        assert p.severity == "moderate"

    def test_severity_pattern_defaults(self):
        sp = SeverityPattern()
        assert sp.blocks_on == []
        assert sp.nits_on == []
        assert sp.approves_when == ""


# ---------------------------------------------------------------------------
# Temporal Weighting
# ---------------------------------------------------------------------------


class TestTemporalWeighting:
    """Test weight_comment and apply_weights functions."""

    def test_recent_comment_gets_3x(self):
        date = datetime.now(UTC) - timedelta(days=10)
        assert weight_comment(date) == 3.0

    def test_medium_age_gets_1_5x(self):
        date = datetime.now(UTC) - timedelta(days=200)
        assert weight_comment(date) == 1.5

    def test_old_comment_gets_0_5x(self):
        date = datetime.now(UTC) - timedelta(days=400)
        assert weight_comment(date) == 0.5

    def test_boundary_90_days(self):
        date = datetime.now(UTC) - timedelta(days=90)
        assert weight_comment(date) == 3.0

    def test_boundary_91_days(self):
        date = datetime.now(UTC) - timedelta(days=91)
        assert weight_comment(date) == 1.5

    def test_boundary_365_days(self):
        date = datetime.now(UTC) - timedelta(days=365)
        assert weight_comment(date) == 1.5

    def test_boundary_366_days(self):
        date = datetime.now(UTC) - timedelta(days=366)
        assert weight_comment(date) == 0.5

    def test_naive_datetime_assumed_utc(self):
        """Naive datetimes should work (treated as UTC)."""
        date = datetime.now() - timedelta(days=10)
        # Should not raise
        weight = weight_comment(date)
        assert weight == 3.0

    def test_apply_weights_adds_weight_field(self):
        now = datetime.now(UTC)
        comments = [
            {"body": "Fix this", "created_at": (now - timedelta(days=5)).isoformat()},
            {"body": "Old nit", "created_at": (now - timedelta(days=200)).isoformat()},
        ]
        weighted = apply_weights(comments)
        assert len(weighted) == 2
        assert weighted[0]["weight"] == 3.0
        assert weighted[1]["weight"] == 1.5
        # Original should not be mutated
        assert "weight" not in comments[0]

    def test_apply_weights_with_datetime_objects(self):
        now = datetime.now(UTC)
        comments = [
            {"body": "Recent", "created_at": now - timedelta(days=1)},
        ]
        weighted = apply_weights(comments)
        assert weighted[0]["weight"] == 3.0


# ---------------------------------------------------------------------------
# PersonaStore CRUD
# ---------------------------------------------------------------------------


class TestPersonaStore:
    """Test disk-based persona CRUD operations."""

    def test_save_and_load(self, persona_store, sample_persona):
        persona_store.save(sample_persona)
        loaded = persona_store.load("alice")
        assert loaded.name == "alice"
        assert loaded.tone == sample_persona.tone

    def test_load_missing_raises(self, persona_store):
        with pytest.raises(FileNotFoundError, match="not found"):
            persona_store.load("nonexistent")

    def test_exists(self, persona_store, sample_persona):
        assert persona_store.exists("alice") is False
        persona_store.save(sample_persona)
        assert persona_store.exists("alice") is True

    def test_list_all(self, persona_store, sample_persona, minimal_persona):
        persona_store.save(sample_persona)
        persona_store.save(minimal_persona)
        profiles = persona_store.list_all()
        names = {p.name for p in profiles}
        assert names == {"alice", "bob"}

    def test_list_all_empty(self, persona_store):
        assert persona_store.list_all() == []

    def test_delete(self, persona_store, sample_persona):
        persona_store.save(sample_persona)
        assert persona_store.exists("alice") is True
        persona_store.delete("alice")
        assert persona_store.exists("alice") is False

    def test_delete_missing_raises(self, persona_store):
        with pytest.raises(FileNotFoundError, match="not found"):
            persona_store.delete("nonexistent")

    def test_save_overwrites(self, persona_store, sample_persona):
        persona_store.save(sample_persona)
        modified = sample_persona.model_copy(update={"tone": "Very strict"})
        persona_store.save(modified)
        loaded = persona_store.load("alice")
        assert loaded.tone == "Very strict"

    def test_list_all_skips_invalid_yaml(self, persona_store, sample_persona):
        persona_store.save(sample_persona)
        # Write an invalid YAML file
        bad_file = persona_store._dir / "broken.yaml"
        bad_file.write_text("}{not valid yaml at all", encoding="utf-8")
        profiles = persona_store.list_all()
        # Should still return the valid one
        assert len(profiles) == 1
        assert profiles[0].name == "alice"

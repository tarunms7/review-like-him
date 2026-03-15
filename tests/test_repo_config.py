"""Tests for per-repo review configuration (RepoConfig)."""

from __future__ import annotations

import pytest
import yaml

from review_bot.config.repo_config import (
    MAX_CONFIG_SIZE_BYTES,
    SUPPORTED_VERSIONS,
    PersonaOverride,
    RepoConfig,
)


# ---------------------------------------------------------------------------
# from_yaml: valid configs
# ---------------------------------------------------------------------------


class TestFromYamlValid:
    """Test RepoConfig.from_yaml with valid YAML."""

    def test_complete_yaml(self) -> None:
        """Parse a fully specified config."""
        yaml_str = yaml.dump({
            "version": 1,
            "persona": "alice",
            "min_severity": "high",
            "skip_patterns": ["*.lock", "vendor/**"],
            "custom_instructions": "Focus on security",
            "max_comments": 25,
            "persona_overrides": {
                "bob": {
                    "min_severity": "critical",
                    "custom_instructions": "Also check perf",
                    "skip_patterns": ["docs/**"],
                    "max_comments": 10,
                },
            },
        })

        config = RepoConfig.from_yaml(yaml_str)

        assert config.version == 1
        assert config.persona == "alice"
        assert config.min_severity == "high"
        assert config.skip_patterns == ["*.lock", "vendor/**"]
        assert config.custom_instructions == "Focus on security"
        assert config.max_comments == 25
        assert "bob" in config.persona_overrides
        assert config.persona_overrides["bob"].min_severity == "critical"
        assert config.persona_overrides["bob"].max_comments == 10

    def test_missing_fields_use_defaults(self) -> None:
        """Omitted fields get their defaults."""
        config = RepoConfig.from_yaml("version: 1\n")

        assert config.version == 1
        assert config.persona is None
        assert config.min_severity == "low"
        assert config.skip_patterns == []
        assert config.custom_instructions == ""
        assert config.persona_overrides == {}
        assert config.max_comments == 50

    def test_minimal_yaml(self) -> None:
        """Minimal YAML with just a mapping is valid."""
        config = RepoConfig.from_yaml("min_severity: medium\n")

        assert config.min_severity == "medium"
        assert config.version == 1  # default


# ---------------------------------------------------------------------------
# from_yaml: invalid configs
# ---------------------------------------------------------------------------


class TestFromYamlInvalid:
    """Test RepoConfig.from_yaml with invalid input."""

    def test_invalid_min_severity(self) -> None:
        """Invalid min_severity raises ValueError."""
        with pytest.raises(Exception, match="min_severity"):
            RepoConfig.from_yaml("min_severity: extreme\n")

    def test_unsupported_version(self) -> None:
        """Unsupported version raises ValueError."""
        with pytest.raises(Exception, match="Unsupported config version"):
            RepoConfig.from_yaml("version: 99\n")

    def test_oversized_yaml(self) -> None:
        """YAML exceeding MAX_CONFIG_SIZE_BYTES raises ValueError."""
        big_yaml = "key: " + "x" * (MAX_CONFIG_SIZE_BYTES + 1) + "\n"
        with pytest.raises(ValueError, match="maximum size"):
            RepoConfig.from_yaml(big_yaml)

    def test_malformed_yaml(self) -> None:
        """Malformed YAML raises an error."""
        with pytest.raises(Exception):
            RepoConfig.from_yaml(":::: [[ not valid yaml")

    def test_empty_yaml(self) -> None:
        """Empty YAML (None from safe_load) raises ValueError."""
        with pytest.raises(ValueError, match="empty"):
            RepoConfig.from_yaml("")

    def test_yaml_with_only_comments(self) -> None:
        """YAML with only comments (None from safe_load) raises ValueError."""
        with pytest.raises(ValueError, match="empty"):
            RepoConfig.from_yaml("# just a comment\n")

    def test_non_mapping_yaml(self) -> None:
        """YAML that parses to a non-dict raises ValueError."""
        with pytest.raises(ValueError, match="mapping"):
            RepoConfig.from_yaml("- item1\n- item2\n")

    def test_max_comments_below_min(self) -> None:
        """max_comments below 1 raises validation error."""
        with pytest.raises(Exception):
            RepoConfig.from_yaml("max_comments: 0\n")

    def test_max_comments_above_max(self) -> None:
        """max_comments above 100 raises validation error."""
        with pytest.raises(Exception):
            RepoConfig.from_yaml("max_comments: 200\n")


# ---------------------------------------------------------------------------
# resolve_for_persona
# ---------------------------------------------------------------------------


class TestResolveForPersona:
    """Test RepoConfig.resolve_for_persona merging."""

    def test_merge_scalar_overrides(self) -> None:
        """Scalar fields are replaced by override."""
        config = RepoConfig(
            min_severity="low",
            max_comments=50,
            persona_overrides={
                "alice": PersonaOverride(
                    min_severity="high",
                    max_comments=10,
                ),
            },
        )

        resolved = config.resolve_for_persona("alice")

        assert resolved.min_severity == "high"
        assert resolved.max_comments == 10

    def test_merge_custom_instructions_appends(self) -> None:
        """Custom instructions from override are appended to base."""
        config = RepoConfig(
            custom_instructions="Base instructions",
            persona_overrides={
                "alice": PersonaOverride(
                    custom_instructions="Extra for alice",
                ),
            },
        )

        resolved = config.resolve_for_persona("alice")

        assert "Base instructions" in resolved.custom_instructions
        assert "Extra for alice" in resolved.custom_instructions
        # Verify append order
        assert resolved.custom_instructions == "Base instructions\nExtra for alice"

    def test_merge_custom_instructions_empty_base(self) -> None:
        """When base custom_instructions is empty, override is used directly."""
        config = RepoConfig(
            custom_instructions="",
            persona_overrides={
                "alice": PersonaOverride(
                    custom_instructions="Alice only",
                ),
            },
        )

        resolved = config.resolve_for_persona("alice")

        assert resolved.custom_instructions == "Alice only"

    def test_merge_skip_patterns_replaces(self) -> None:
        """Skip patterns from override replace base entirely."""
        config = RepoConfig(
            skip_patterns=["*.lock", "vendor/**"],
            persona_overrides={
                "alice": PersonaOverride(
                    skip_patterns=["docs/**"],
                ),
            },
        )

        resolved = config.resolve_for_persona("alice")

        assert resolved.skip_patterns == ["docs/**"]

    def test_nonexistent_persona_returns_base(self) -> None:
        """Non-existent persona returns the base config unchanged."""
        config = RepoConfig(
            min_severity="high",
            max_comments=30,
            persona_overrides={
                "alice": PersonaOverride(min_severity="critical"),
            },
        )

        resolved = config.resolve_for_persona("bob")

        assert resolved.min_severity == "high"
        assert resolved.max_comments == 30
        # Should be the same object since no changes
        assert resolved is config

    def test_override_with_no_fields_set(self) -> None:
        """Override with all None fields returns base config."""
        config = RepoConfig(
            min_severity="medium",
            persona_overrides={
                "alice": PersonaOverride(),
            },
        )

        resolved = config.resolve_for_persona("alice")

        assert resolved.min_severity == "medium"
        assert resolved is config


# ---------------------------------------------------------------------------
# default() classmethod
# ---------------------------------------------------------------------------


class TestDefault:
    """Test RepoConfig.default() classmethod."""

    def test_default_returns_valid_config(self) -> None:
        """Default config has all expected defaults."""
        config = RepoConfig.default()

        assert config.version == 1
        assert config.persona is None
        assert config.min_severity == "low"
        assert config.skip_patterns == []
        assert config.custom_instructions == ""
        assert config.persona_overrides == {}
        assert config.max_comments == 50


# ---------------------------------------------------------------------------
# SUPPORTED_VERSIONS constant
# ---------------------------------------------------------------------------


class TestConstants:
    """Test module-level constants."""

    def test_supported_versions(self) -> None:
        """SUPPORTED_VERSIONS contains expected versions."""
        assert 1 in SUPPORTED_VERSIONS

    def test_max_config_size(self) -> None:
        """MAX_CONFIG_SIZE_BYTES is 64KB."""
        assert MAX_CONFIG_SIZE_BYTES == 64 * 1024

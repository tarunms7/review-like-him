"""Per-repo review configuration loaded from .review-like-him.yml."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("review-bot")

# Maximum config file size to prevent abuse
MAX_CONFIG_SIZE_BYTES = 64 * 1024

# Supported config versions
SUPPORTED_VERSIONS: set[int] = {1}

# Valid severity levels
_VALID_SEVERITIES: set[str] = {"low", "medium", "high", "critical"}

# Mapping from severity string to integer for filtering
SEVERITY_TO_INT: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


class PersonaOverride(BaseModel):
    """Persona-specific overrides within RepoConfig."""

    min_severity: str | None = Field(
        default=None,
        description="Override minimum severity for this persona",
    )
    custom_instructions: str | None = Field(
        default=None,
        description="Additional instructions to append for this persona",
    )
    skip_patterns: list[str] | None = Field(
        default=None,
        description="Override skip patterns for this persona",
    )
    max_comments: int | None = Field(
        default=None,
        description="Override max comments for this persona",
    )

    @field_validator("min_severity")
    @classmethod
    def _validate_min_severity(cls, v: str | None) -> str | None:
        """Validate min_severity is a valid level when set."""
        if v is not None and v not in _VALID_SEVERITIES:
            raise ValueError(
                f"min_severity must be one of {sorted(_VALID_SEVERITIES)}, got '{v}'"
            )
        return v


class RepoConfig(BaseModel):
    """Per-repo review configuration loaded from .review-like-him.yml."""

    version: int = Field(default=1, description="Config version")
    persona: str | None = Field(
        default=None,
        description="Default persona name for this repo",
    )
    min_severity: str = Field(
        default="low",
        description="Minimum severity threshold: 'low', 'medium', 'high', or 'critical'",
    )
    skip_patterns: list[str] = Field(
        default_factory=list,
        description="List of glob patterns for files to skip in review",
    )
    custom_instructions: str = Field(
        default="",
        description="Additional instructions appended to the review prompt",
    )
    persona_overrides: dict[str, PersonaOverride] = Field(
        default_factory=dict,
        description="Per-persona config overrides keyed by persona name",
    )
    max_comments: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Maximum inline comments to post",
    )

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: int) -> int:
        """Version must be in SUPPORTED_VERSIONS."""
        if v not in SUPPORTED_VERSIONS:
            raise ValueError(
                f"Unsupported config version {v}. Supported: {sorted(SUPPORTED_VERSIONS)}"
            )
        return v

    @field_validator("min_severity")
    @classmethod
    def _validate_min_severity(cls, v: str) -> str:
        """Validate min_severity is a valid level."""
        if v not in _VALID_SEVERITIES:
            raise ValueError(
                f"min_severity must be one of {sorted(_VALID_SEVERITIES)}, got '{v}'"
            )
        return v

    @classmethod
    def from_yaml(cls, yaml_str: str) -> RepoConfig:
        """Parse a YAML string into a RepoConfig.

        Args:
            yaml_str: Raw YAML config string.

        Returns:
            Validated RepoConfig instance.

        Raises:
            ValueError: If YAML is too large, empty, or invalid.
        """
        if len(yaml_str.encode("utf-8")) > MAX_CONFIG_SIZE_BYTES:
            raise ValueError(
                f"Config file exceeds maximum size of {MAX_CONFIG_SIZE_BYTES} bytes"
            )

        import yaml

        data = yaml.safe_load(yaml_str)
        if data is None:
            raise ValueError("Config file is empty or contains only comments")
        if not isinstance(data, dict):
            raise ValueError("Config file must be a YAML mapping")

        return cls(**data)

    def resolve_for_persona(self, persona_name: str) -> RepoConfig:
        """Merge persona-specific overrides into the base config.

        For custom_instructions, the override is appended to the base.
        For lists (skip_patterns), the override replaces the base.
        For scalars (min_severity, max_comments), the override replaces the base.

        Args:
            persona_name: Name of the persona to resolve for.

        Returns:
            A new RepoConfig with overrides applied.
        """
        override = self.persona_overrides.get(persona_name)
        if override is None:
            return self

        updates: dict = {}

        if override.min_severity is not None:
            updates["min_severity"] = override.min_severity

        if override.skip_patterns is not None:
            updates["skip_patterns"] = override.skip_patterns

        if override.max_comments is not None:
            updates["max_comments"] = override.max_comments

        if override.custom_instructions is not None:
            # Append override instructions to base
            base = self.custom_instructions
            if base:
                updates["custom_instructions"] = f"{base}\n{override.custom_instructions}"
            else:
                updates["custom_instructions"] = override.custom_instructions

        if updates:
            return self.model_copy(update=updates)
        return self

    @classmethod
    def default(cls) -> RepoConfig:
        """Return a default RepoConfig with all defaults.

        Returns:
            RepoConfig with default values.
        """
        return cls()

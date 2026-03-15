"""Pydantic models for persona profiles with YAML serialization."""

from __future__ import annotations

import yaml
from pydantic import BaseModel, Field


class Priority(BaseModel):
    """A single review priority with category, severity level, and description."""

    category: str = Field(description="Priority category slug (e.g. 'error_handling', 'naming')")
    severity: str = Field(
        description="Severity level: 'critical', 'strict', 'moderate', 'opinionated'",
    )
    description: str = Field(description="Human-readable description of the priority")


class SeverityPattern(BaseModel):
    """Defines what a persona blocks on, nits on, and when they approve."""

    blocks_on: list[str] = Field(
        default_factory=list,
        description="Issues that cause the reviewer to request changes",
    )
    nits_on: list[str] = Field(
        default_factory=list,
        description="Issues the reviewer only nits on",
    )
    approves_when: str = Field(
        default="",
        description="Condition description for when the reviewer approves",
    )


class PersonaProfile(BaseModel):
    """Complete persona profile representing a reviewer's style, priorities, and patterns."""

    name: str = Field(description="Persona name slug used as filename and bot reference")
    github_user: str = Field(description="GitHub username the persona was mined from")
    mined_from: str = Field(default="", description="Human-readable mining summary")
    last_updated: str = Field(default="", description="ISO 8601 date string of last mining run")
    priorities: list[Priority] = Field(
        default_factory=list, description="Ordered review priorities",
    )
    pet_peeves: list[str] = Field(default_factory=list, description="Things the reviewer dislikes")
    tone: str = Field(default="", description="Description of reviewer's communication style")
    severity_pattern: SeverityPattern = Field(
        default_factory=SeverityPattern,
        description="What triggers blocks, nits, and approvals",
    )
    overrides: list[str] = Field(
        default_factory=list,
        description="Manual override notes added by persona creator",
    )
    smoothed_category_rates: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "EMA-smoothed approval rates per priority category. "
            "Persisted across reanalysis runs to provide history for smoothing."
        ),
    )
    last_mined_at: str | None = Field(
        default=None,
        description=(
            "ISO 8601 timestamp of the last successful mining run. "
            "Used by incremental mining to skip already-processed reviews."
        ),
    )

    def to_yaml(self) -> str:
        """Serialize the profile to a YAML string."""
        return yaml.dump(
            self.model_dump(),
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    @classmethod
    def from_yaml(cls, yaml_str: str) -> PersonaProfile:
        """Deserialize a profile from a YAML string."""
        data = yaml.safe_load(yaml_str)
        return cls.model_validate(data)

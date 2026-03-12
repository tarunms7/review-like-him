"""CRUD operations for persona YAML profiles stored on disk."""

from __future__ import annotations

import logging
from pathlib import Path

from review_bot.config.paths import PERSONAS_DIR
from review_bot.persona.profile import PersonaProfile

logger = logging.getLogger(__name__)


class PersonaStore:
    """Manages persona profiles as YAML files in ~/.review-bot/personas/."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._dir = base_dir or PERSONAS_DIR

    def _ensure_dir(self) -> None:
        """Create the personas directory if it doesn't exist."""
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, name: str) -> Path:
        """Return the file path for a persona by name."""
        return self._dir / f"{name}.yaml"

    def save(self, profile: PersonaProfile) -> None:
        """Save a persona profile to disk as YAML."""
        self._ensure_dir()
        path = self._path_for(profile.name)
        path.write_text(profile.to_yaml(), encoding="utf-8")
        logger.info("Saved persona '%s' to %s", profile.name, path)

    def load(self, name: str) -> PersonaProfile:
        """Load a persona profile by name.

        Raises:
            FileNotFoundError: If the persona file does not exist.
        """
        path = self._path_for(name)
        if not path.exists():
            raise FileNotFoundError(f"Persona '{name}' not found at {path}")
        yaml_str = path.read_text(encoding="utf-8")
        return PersonaProfile.from_yaml(yaml_str)

    def list_all(self) -> list[PersonaProfile]:
        """Load and return all persona profiles from the store."""
        self._ensure_dir()
        profiles: list[PersonaProfile] = []
        for path in sorted(self._dir.glob("*.yaml")):
            try:
                yaml_str = path.read_text(encoding="utf-8")
                profiles.append(PersonaProfile.from_yaml(yaml_str))
            except Exception:
                logger.warning("Failed to load persona from %s", path, exc_info=True)
        return profiles

    def delete(self, name: str) -> None:
        """Delete a persona profile by name.

        Raises:
            FileNotFoundError: If the persona file does not exist.
        """
        path = self._path_for(name)
        if not path.exists():
            raise FileNotFoundError(f"Persona '{name}' not found at {path}")
        path.unlink()
        logger.info("Deleted persona '%s' from %s", name, path)

    def exists(self, name: str) -> bool:
        """Check if a persona profile exists."""
        return self._path_for(name).exists()

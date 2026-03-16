"""Tests for narrowed exception handling in PersonaStore.list_all()."""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

import pytest

from review_bot.persona.profile import PersonaProfile
from review_bot.persona.store import PersonaStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_persona_yaml(name: str = "alice", github_user: str = "alice-gh") -> str:
    """Return valid persona YAML content."""
    return (
        f"name: {name}\n"
        f"github_user: {github_user}\n"
        "priorities: []\n"
        "pet_peeves: []\n"
        "tone: friendly\n"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListAllNarrowExceptions:
    """Tests for narrowed exception handling in list_all()."""

    def test_list_all_yaml_error_skips_bad_file(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Corrupt YAML file is skipped, valid file is returned."""
        persona_dir = tmp_path / "personas"
        persona_dir.mkdir()

        # Write a corrupt YAML file
        corrupt = persona_dir / "bad.yaml"
        corrupt.write_text(": : : {invalid yaml [[\n", encoding="utf-8")

        # Write a valid persona file
        good = persona_dir / "alice.yaml"
        good.write_text(_valid_persona_yaml(), encoding="utf-8")

        store = PersonaStore(base_dir=persona_dir)

        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "alice"

        # Check warning was logged mentioning the bad file
        assert any(
            "bad.yaml" in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        )

    def test_list_all_validation_error_skips_bad_file(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Valid YAML with wrong schema is skipped with WARNING."""
        persona_dir = tmp_path / "personas"
        persona_dir.mkdir()

        # Valid YAML but missing required 'name' and 'github_user' fields
        invalid_schema = persona_dir / "invalid_schema.yaml"
        invalid_schema.write_text(
            "some_field: value\nanother: 42\n",
            encoding="utf-8",
        )

        # Valid persona
        good = persona_dir / "bob.yaml"
        good.write_text(
            _valid_persona_yaml(name="bob", github_user="bob-gh"),
            encoding="utf-8",
        )

        store = PersonaStore(base_dir=persona_dir)

        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "bob"

        assert any(
            "invalid_schema.yaml" in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        )

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="File permissions not reliable on Windows",
    )
    def test_list_all_os_error_skips_unreadable_file(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Unreadable file is skipped with OSError."""
        persona_dir = tmp_path / "personas"
        persona_dir.mkdir()

        # Create unreadable file
        unreadable = persona_dir / "noperm.yaml"
        unreadable.write_text(_valid_persona_yaml(name="noperm"), encoding="utf-8")
        os.chmod(unreadable, 0o000)

        # Valid persona
        good = persona_dir / "charlie.yaml"
        good.write_text(
            _valid_persona_yaml(name="charlie", github_user="charlie-gh"),
            encoding="utf-8",
        )

        store = PersonaStore(base_dir=persona_dir)

        try:
            with caplog.at_level(logging.WARNING):
                profiles = store.list_all()

            assert len(profiles) == 1
            assert profiles[0].name == "charlie"

            assert any(
                "noperm.yaml" in record.message
                for record in caplog.records
                if record.levelno >= logging.WARNING
            )
        finally:
            # Restore permissions for cleanup
            os.chmod(unreadable, 0o644)

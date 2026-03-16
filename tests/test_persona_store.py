"""Tests for narrow exception handling in PersonaStore.list_all."""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

import pytest

from review_bot.persona.store import PersonaStore


def _valid_persona_yaml() -> str:
    """Return valid YAML for a minimal PersonaProfile."""
    return (
        "name: alice\n"
        "github_user: alice-gh\n"
    )


class TestListAllNarrowExceptions:
    """Tests for narrowed exception handling in PersonaStore.list_all."""

    def test_list_all_yaml_error_skips_bad_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Corrupt YAML file is skipped with WARNING log."""
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir()

        # Write a corrupt YAML file
        bad_file = personas_dir / "bad.yaml"
        bad_file.write_text(": : : not valid yaml [[", encoding="utf-8")

        # Write a valid persona file
        good_file = personas_dir / "good.yaml"
        good_file.write_text(_valid_persona_yaml(), encoding="utf-8")

        store = PersonaStore(base_dir=personas_dir)
        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "alice"
        assert "bad.yaml" in caplog.text
        assert "Failed to load persona" in caplog.text

    def test_list_all_validation_error_skips_bad_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Valid YAML with wrong schema is skipped with WARNING log."""
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir()

        # Write valid YAML but with missing required 'name' field
        bad_schema_file = personas_dir / "bad_schema.yaml"
        bad_schema_file.write_text(
            "some_unknown_field: value\n",
            encoding="utf-8",
        )

        # Write a valid persona file
        good_file = personas_dir / "good.yaml"
        good_file.write_text(_valid_persona_yaml(), encoding="utf-8")

        store = PersonaStore(base_dir=personas_dir)
        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "alice"
        assert "Failed to load persona" in caplog.text

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="File permissions not reliable on Windows",
    )
    def test_list_all_os_error_skips_unreadable_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unreadable file is skipped with WARNING log."""
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir()

        # Write a file and remove read permissions
        unreadable = personas_dir / "noperm.yaml"
        unreadable.write_text(_valid_persona_yaml(), encoding="utf-8")
        os.chmod(unreadable, 0o000)

        # Write a valid persona file
        good_file = personas_dir / "good.yaml"
        good_file.write_text(_valid_persona_yaml(), encoding="utf-8")

        store = PersonaStore(base_dir=personas_dir)
        try:
            with caplog.at_level(logging.WARNING):
                profiles = store.list_all()

            assert len(profiles) == 1
            assert profiles[0].name == "alice"
            assert "Failed to load persona" in caplog.text
        finally:
            # Restore permissions for cleanup
            os.chmod(unreadable, 0o644)

"""Tests for narrow exception handling in PersonaStore."""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

import pytest

from review_bot.persona.store import PersonaStore


# A valid persona YAML for testing
_VALID_PERSONA_YAML = """\
name: alice
github_user: alice-gh
tone: Direct
"""

# Corrupt YAML that will cause yaml.YAMLError
_CORRUPT_YAML = ":\n  - [\n  invalid: yaml: [["

# Valid YAML but missing required 'github_user' field (Pydantic ValidationError)
_BAD_SCHEMA_YAML = """\
tone: Direct
pet_peeves:
  - magic numbers
"""


class TestListAllNarrowExceptions:
    """Tests for narrow exception handling in PersonaStore.list_all."""

    def test_list_all_yaml_error_skips_bad_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Corrupt YAML file is skipped; valid file is still loaded."""
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir()

        # Write a corrupt YAML file
        bad_path = personas_dir / "corrupt.yaml"
        bad_path.write_text(_CORRUPT_YAML, encoding="utf-8")

        # Write a valid persona file
        good_path = personas_dir / "alice.yaml"
        good_path.write_text(_VALID_PERSONA_YAML, encoding="utf-8")

        store = PersonaStore(base_dir=personas_dir)

        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "alice"

        # Verify warning was logged mentioning the corrupt file
        assert any("corrupt.yaml" in record.message for record in caplog.records)
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_list_all_validation_error_skips_bad_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Valid YAML with wrong schema is skipped with WARNING."""
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir()

        # Write valid YAML but with missing required fields
        bad_path = personas_dir / "bad_schema.yaml"
        bad_path.write_text(_BAD_SCHEMA_YAML, encoding="utf-8")

        # Write a valid persona file
        good_path = personas_dir / "alice.yaml"
        good_path.write_text(_VALID_PERSONA_YAML, encoding="utf-8")

        store = PersonaStore(base_dir=personas_dir)

        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "alice"

        # Verify warning was logged
        assert any("bad_schema.yaml" in record.message for record in caplog.records)

    @pytest.mark.skipif(platform.system() == "Windows", reason="chmod not reliable on Windows")
    def test_list_all_os_error_skips_unreadable_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unreadable file (OSError) is skipped with WARNING."""
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir()

        # Write a file and remove read permissions
        unreadable = personas_dir / "noperm.yaml"
        unreadable.write_text(_VALID_PERSONA_YAML, encoding="utf-8")
        os.chmod(unreadable, 0o000)

        # Write a valid persona file
        good_path = personas_dir / "alice.yaml"
        good_path.write_text(_VALID_PERSONA_YAML, encoding="utf-8")

        store = PersonaStore(base_dir=personas_dir)

        try:
            with caplog.at_level(logging.WARNING):
                profiles = store.list_all()

            assert len(profiles) == 1
            assert profiles[0].name == "alice"

            # Verify warning was logged
            assert any("noperm.yaml" in record.message for record in caplog.records)
        finally:
            # Restore permissions for cleanup
            os.chmod(unreadable, 0o644)

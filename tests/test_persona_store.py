"""Tests for narrow exception handling in PersonaStore.list_all."""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

import pytest

from review_bot.persona.store import PersonaStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PERSONA_YAML = """\
name: valid-persona
github_user: valid-gh
tone: friendly
"""

_CORRUPT_YAML = """\
: [invalid: yaml: {{{{
  - not: parseable
"""

_BAD_SCHEMA_YAML = """\
not_a_real_field: true
also_wrong: 42
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListAllNarrowExceptions:
    """Test narrow exception handling in PersonaStore.list_all."""

    def test_list_all_yaml_error_skips_bad_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Corrupt YAML file is skipped; valid persona still loaded."""
        persona_dir = tmp_path / "personas"
        persona_dir.mkdir()

        # Write one corrupt file and one valid file
        (persona_dir / "bad.yaml").write_text(_CORRUPT_YAML, encoding="utf-8")
        (persona_dir / "good.yaml").write_text(_VALID_PERSONA_YAML, encoding="utf-8")

        store = PersonaStore(base_dir=persona_dir)

        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "valid-persona"

        # Check warning was logged mentioning the bad file
        assert any("bad.yaml" in record.message for record in caplog.records)

    def test_list_all_validation_error_skips_bad_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Valid YAML with wrong schema is skipped with WARNING log."""
        persona_dir = tmp_path / "personas"
        persona_dir.mkdir()

        # YAML that parses fine but fails Pydantic validation (missing required fields)
        (persona_dir / "bad_schema.yaml").write_text(
            _BAD_SCHEMA_YAML, encoding="utf-8",
        )
        (persona_dir / "good.yaml").write_text(_VALID_PERSONA_YAML, encoding="utf-8")

        store = PersonaStore(base_dir=persona_dir)

        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "valid-persona"

        # Check warning was logged
        assert any("bad_schema.yaml" in record.message for record in caplog.records)

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="File permission tests not reliable on Windows",
    )
    def test_list_all_os_error_skips_unreadable_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unreadable file is skipped with WARNING log."""
        persona_dir = tmp_path / "personas"
        persona_dir.mkdir()

        # Write a valid file
        (persona_dir / "good.yaml").write_text(_VALID_PERSONA_YAML, encoding="utf-8")

        # Write an unreadable file
        unreadable = persona_dir / "unreadable.yaml"
        unreadable.write_text(_VALID_PERSONA_YAML, encoding="utf-8")
        os.chmod(unreadable, 0o000)

        store = PersonaStore(base_dir=persona_dir)

        try:
            with caplog.at_level(logging.WARNING):
                profiles = store.list_all()

            assert len(profiles) == 1
            assert profiles[0].name == "valid-persona"

            # Check warning was logged
            assert any(
                "unreadable.yaml" in record.message for record in caplog.records
            )
        finally:
            # Restore permissions for cleanup
            os.chmod(unreadable, 0o644)

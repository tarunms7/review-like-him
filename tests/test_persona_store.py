"""Tests for narrowed exception handling in PersonaStore.list_all."""

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
name: alice
github_user: alice-gh
tone: Friendly
"""

_CORRUPT_YAML = """\
: [invalid: yaml: {{{
"""

_WRONG_SCHEMA_YAML = """\
tone: Direct
pet_peeves:
  - Magic numbers
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListAllNarrowExceptions:
    """Verify narrowed exception handling in PersonaStore.list_all."""

    def test_list_all_yaml_error_skips_bad_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Corrupt YAML files are skipped with a WARNING log."""
        persona_dir = tmp_path / "personas"
        persona_dir.mkdir()

        # Write one corrupt and one valid file
        (persona_dir / "bad.yaml").write_text(_CORRUPT_YAML, encoding="utf-8")
        (persona_dir / "good.yaml").write_text(_VALID_PERSONA_YAML, encoding="utf-8")

        store = PersonaStore(base_dir=persona_dir)

        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "alice"

        # Check warning was logged for the corrupt file
        assert any("bad.yaml" in rec.message for rec in caplog.records)

    def test_list_all_validation_error_skips_bad_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Valid YAML with wrong schema (missing required fields) is skipped."""
        persona_dir = tmp_path / "personas"
        persona_dir.mkdir()

        # Missing required 'name' and 'github_user' fields
        (persona_dir / "invalid_schema.yaml").write_text(
            _WRONG_SCHEMA_YAML, encoding="utf-8",
        )
        (persona_dir / "valid.yaml").write_text(
            _VALID_PERSONA_YAML, encoding="utf-8",
        )

        store = PersonaStore(base_dir=persona_dir)

        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "alice"

        assert any(
            "invalid_schema.yaml" in rec.message for rec in caplog.records
        )

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="File permissions not reliable on Windows",
    )
    def test_list_all_os_error_skips_unreadable_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unreadable files (OSError) are skipped with a WARNING."""
        persona_dir = tmp_path / "personas"
        persona_dir.mkdir()

        unreadable = persona_dir / "noperm.yaml"
        unreadable.write_text(_VALID_PERSONA_YAML, encoding="utf-8")
        os.chmod(unreadable, 0o000)

        (persona_dir / "readable.yaml").write_text(
            _VALID_PERSONA_YAML, encoding="utf-8",
        )

        store = PersonaStore(base_dir=persona_dir)

        with caplog.at_level(logging.WARNING):
            profiles = store.list_all()

        # Restore permissions for cleanup
        os.chmod(unreadable, 0o644)

        assert len(profiles) == 1
        assert profiles[0].name == "alice"

        assert any("noperm.yaml" in rec.message for rec in caplog.records)

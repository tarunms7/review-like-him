"""Tests for narrow exception handling in PersonaStore.list_all()."""

from __future__ import annotations

import os
import platform
import sys

import pytest

from review_bot.persona.store import PersonaStore


class TestListAllNarrowExceptions:
    """Tests that list_all() skips bad files with specific exceptions."""

    def test_list_all_yaml_error_skips_bad_file(
        self, tmp_path, caplog,
    ) -> None:
        """Corrupt YAML file is skipped; valid persona still returned."""
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir()

        # Write a corrupt YAML file
        bad_file = personas_dir / "bad.yaml"
        bad_file.write_text(": : : not valid yaml [[", encoding="utf-8")

        # Write a valid persona file
        good_file = personas_dir / "good.yaml"
        good_file.write_text(
            "name: good\ngithub_user: good-gh\n", encoding="utf-8",
        )

        store = PersonaStore(base_dir=personas_dir)
        profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "good"
        assert "bad.yaml" in caplog.text

    def test_list_all_validation_error_skips_bad_file(
        self, tmp_path, caplog,
    ) -> None:
        """Valid YAML with wrong schema is skipped with WARNING."""
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir()

        # Valid YAML but missing required 'name' and 'github_user' fields
        bad_file = personas_dir / "invalid_schema.yaml"
        bad_file.write_text(
            "some_random_key: value\n", encoding="utf-8",
        )

        # Valid persona
        good_file = personas_dir / "valid.yaml"
        good_file.write_text(
            "name: valid\ngithub_user: valid-gh\n", encoding="utf-8",
        )

        store = PersonaStore(base_dir=personas_dir)
        profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "valid"
        assert "invalid_schema.yaml" in caplog.text

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="File permission tests not reliable on Windows",
    )
    def test_list_all_os_error_skips_unreadable_file(
        self, tmp_path, caplog,
    ) -> None:
        """Unreadable file is skipped with WARNING log."""
        personas_dir = tmp_path / "personas"
        personas_dir.mkdir()

        # Create an unreadable file
        unreadable = personas_dir / "noperm.yaml"
        unreadable.write_text(
            "name: noperm\ngithub_user: noperm-gh\n", encoding="utf-8",
        )
        os.chmod(unreadable, 0o000)

        # Valid persona
        good_file = personas_dir / "ok.yaml"
        good_file.write_text(
            "name: ok\ngithub_user: ok-gh\n", encoding="utf-8",
        )

        store = PersonaStore(base_dir=personas_dir)
        profiles = store.list_all()

        assert len(profiles) == 1
        assert profiles[0].name == "ok"
        assert "noperm.yaml" in caplog.text

        # Restore permissions for cleanup
        os.chmod(unreadable, 0o644)

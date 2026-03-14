"""Tests for review_bot.config — settings validation, env vars, path resolution."""

from __future__ import annotations

import os

import pytest

from review_bot.config.paths import ensure_directories, validate_path
from review_bot.config.settings import Settings

# ---------------------------------------------------------------------------
# Settings Validation
# ---------------------------------------------------------------------------


class TestSettingsValidation:
    """Test Pydantic settings model validation."""

    def test_default_settings(self, monkeypatch):
        """Settings should instantiate with defaults (no env vars needed)."""
        # Clear any env vars that might interfere
        for key in list(os.environ):
            if key.startswith("REVIEW_BOT_"):
                monkeypatch.delenv(key, raising=False)
        settings = Settings()
        assert settings.github_app_id == 0
        assert settings.port == 8000
        assert settings.host == "0.0.0.0"

    def test_invalid_port_below_range(self):
        with pytest.raises(ValueError, match="port must be between"):
            Settings(port=0)

    def test_invalid_port_above_range(self):
        with pytest.raises(ValueError, match="port must be between"):
            Settings(port=70000)

    def test_valid_port_boundaries(self):
        s1 = Settings(port=1)
        assert s1.port == 1
        s2 = Settings(port=65535)
        assert s2.port == 65535

    def test_negative_app_id(self):
        with pytest.raises(ValueError, match="github_app_id must be >= 0"):
            Settings(github_app_id=-1)

    def test_zero_app_id_is_valid(self):
        s = Settings(github_app_id=0)
        assert s.github_app_id == 0


# ---------------------------------------------------------------------------
# Environment Variable Loading
# ---------------------------------------------------------------------------


class TestEnvironmentVariableLoading:
    """Test that REVIEW_BOT_ env vars are picked up."""

    def test_env_app_id(self, monkeypatch):
        monkeypatch.setenv("REVIEW_BOT_GITHUB_APP_ID", "99999")
        settings = Settings()
        assert settings.github_app_id == 99999

    def test_env_port(self, monkeypatch):
        monkeypatch.setenv("REVIEW_BOT_PORT", "9090")
        settings = Settings()
        assert settings.port == 9090

    def test_env_webhook_secret(self, monkeypatch):
        monkeypatch.setenv("REVIEW_BOT_WEBHOOK_SECRET", "super-secret")
        settings = Settings()
        assert settings.webhook_secret == "super-secret"

    def test_env_host(self, monkeypatch):
        monkeypatch.setenv("REVIEW_BOT_HOST", "127.0.0.1")
        settings = Settings()
        assert settings.host == "127.0.0.1"


# ---------------------------------------------------------------------------
# Server Validation
# ---------------------------------------------------------------------------


class TestValidateForServer:
    """Test the validate_for_server method."""

    def test_unconfigured_settings_have_errors(self, monkeypatch):
        for key in list(os.environ):
            if key.startswith("REVIEW_BOT_"):
                monkeypatch.delenv(key, raising=False)
        settings = Settings()
        errors = settings.validate_for_server()
        assert len(errors) > 0
        assert any("github_app_id" in e for e in errors)
        assert any("webhook_secret" in e for e in errors)

    def test_configured_settings_pass(self, tmp_path):
        key_file = tmp_path / "key.pem"
        key_file.write_text("fake-key")
        key_file.chmod(0o600)
        settings = Settings(
            github_app_id=12345,
            webhook_secret="secret",
            private_key_path=key_file,
        )
        errors = settings.validate_for_server()
        assert errors == []

    def test_insecure_permissions_flagged(self, tmp_path):
        key_file = tmp_path / "key.pem"
        key_file.write_text("fake-key")
        key_file.chmod(0o644)  # too permissive
        settings = Settings(
            github_app_id=12345,
            webhook_secret="secret",
            private_key_path=key_file,
        )
        errors = settings.validate_for_server()
        assert any("insecure permissions" in e for e in errors)

    def test_missing_key_file_flagged(self, tmp_path):
        settings = Settings(
            github_app_id=12345,
            webhook_secret="secret",
            private_key_path=tmp_path / "nonexistent.pem",
        )
        errors = settings.validate_for_server()
        assert any("does not exist" in e for e in errors)


# ---------------------------------------------------------------------------
# Path Utilities
# ---------------------------------------------------------------------------


class TestPathUtilities:
    """Test path validation and directory creation."""

    def test_ensure_directories(self, tmp_path, monkeypatch):
        """Ensure directories creates the expected structure."""
        import review_bot.config.paths as paths_mod

        config_dir = tmp_path / ".review-bot"
        monkeypatch.setattr(paths_mod, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(paths_mod, "PERSONAS_DIR", config_dir / "personas")
        monkeypatch.setattr(paths_mod, "REPOS_DIR", config_dir / "repos")
        monkeypatch.setattr(paths_mod, "LOG_DIR", config_dir / "logs")
        monkeypatch.setattr(
            paths_mod,
            "_ALL_DIRS",
            [config_dir, config_dir / "personas", config_dir / "repos", config_dir / "logs"],
        )

        ensure_directories()

        assert config_dir.exists()
        assert (config_dir / "personas").exists()
        assert (config_dir / "repos").exists()
        assert (config_dir / "logs").exists()

    def test_validate_path_existing(self, tmp_path):
        f = tmp_path / "exists.txt"
        f.write_text("hello")
        validate_path(f, must_exist=True, must_be_file=True)  # should not raise

    def test_validate_path_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            validate_path(tmp_path / "nope.txt", must_exist=True)

    def test_validate_path_not_file_raises(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        with pytest.raises(ValueError, match="not a file"):
            validate_path(d, must_exist=True, must_be_file=True)

    def test_validate_path_must_exist_false(self, tmp_path):
        validate_path(tmp_path / "anything", must_exist=False)  # should not raise

"""Tests for review_bot.config.paths — directory creation and path validation."""

from __future__ import annotations

import stat

import pytest

from review_bot.config.paths import ensure_directories, validate_path


class TestEnsureDirectories:
    """Test ensure_directories() creates all expected dirs."""

    def test_creates_all_expected_dirs(self, tmp_path, monkeypatch):
        """ensure_directories() should create CONFIG_DIR, PERSONAS_DIR, REPOS_DIR, LOG_DIR."""
        import review_bot.config.paths as paths_mod

        config_dir = tmp_path / ".review-bot"
        personas_dir = config_dir / "personas"
        repos_dir = config_dir / "repos"
        log_dir = config_dir / "logs"

        monkeypatch.setattr(paths_mod, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(paths_mod, "PERSONAS_DIR", personas_dir)
        monkeypatch.setattr(paths_mod, "REPOS_DIR", repos_dir)
        monkeypatch.setattr(paths_mod, "LOG_DIR", log_dir)
        monkeypatch.setattr(
            paths_mod, "_ALL_DIRS", [config_dir, personas_dir, repos_dir, log_dir]
        )

        ensure_directories()

        assert config_dir.is_dir()
        assert personas_dir.is_dir()
        assert repos_dir.is_dir()
        assert log_dir.is_dir()

    def test_idempotent(self, tmp_path, monkeypatch):
        """Calling ensure_directories() twice should not raise."""
        import review_bot.config.paths as paths_mod

        config_dir = tmp_path / ".review-bot"
        all_dirs = [
            config_dir,
            config_dir / "personas",
            config_dir / "repos",
            config_dir / "logs",
        ]

        monkeypatch.setattr(paths_mod, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(paths_mod, "PERSONAS_DIR", config_dir / "personas")
        monkeypatch.setattr(paths_mod, "REPOS_DIR", config_dir / "repos")
        monkeypatch.setattr(paths_mod, "LOG_DIR", config_dir / "logs")
        monkeypatch.setattr(paths_mod, "_ALL_DIRS", all_dirs)

        ensure_directories()
        ensure_directories()  # second call should not error

        for d in all_dirs:
            assert d.is_dir()

    def test_raises_oserror_when_parent_not_writable(self, tmp_path, monkeypatch):
        """ensure_directories() should raise OSError when it cannot create dirs."""
        import review_bot.config.paths as paths_mod

        # Create a read-only parent directory
        readonly_parent = tmp_path / "readonly"
        readonly_parent.mkdir()
        readonly_parent.chmod(stat.S_IRUSR | stat.S_IXUSR)

        config_dir = readonly_parent / "sub" / ".review-bot"
        monkeypatch.setattr(paths_mod, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(paths_mod, "_ALL_DIRS", [config_dir])

        try:
            with pytest.raises(OSError):
                ensure_directories()
        finally:
            # Restore write permissions so pytest can clean up tmp_path
            readonly_parent.chmod(stat.S_IRWXU)


class TestValidatePath:
    """Test validate_path() with various flag combinations."""

    def test_must_exist_true_raises_for_missing(self, tmp_path):
        """must_exist=True should raise FileNotFoundError for non-existent path."""
        with pytest.raises(FileNotFoundError, match="does not exist"):
            validate_path(tmp_path / "missing.txt", must_exist=True)

    def test_must_exist_false_no_raise_for_missing(self, tmp_path):
        """must_exist=False should not raise for non-existent path."""
        validate_path(tmp_path / "anything.txt", must_exist=False)  # no error

    def test_must_be_file_true_raises_for_directory(self, tmp_path):
        """must_be_file=True should raise ValueError when path is a directory."""
        d = tmp_path / "somedir"
        d.mkdir()
        with pytest.raises(ValueError, match="not a file"):
            validate_path(d, must_exist=True, must_be_file=True)

    def test_must_be_file_true_passes_for_file(self, tmp_path):
        """must_be_file=True should not raise for an actual file."""
        f = tmp_path / "real.txt"
        f.write_text("content")
        validate_path(f, must_exist=True, must_be_file=True)  # no error

    def test_must_be_file_false_passes_for_directory(self, tmp_path):
        """must_be_file=False should not raise for a directory."""
        d = tmp_path / "adir"
        d.mkdir()
        validate_path(d, must_exist=True, must_be_file=False)  # no error

"""Tests for review_bot.utils.git — clone_repo and get_diff with mocked subprocess."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from review_bot.utils.git import clone_repo, get_diff


class TestCloneRepo:
    """Test clone_repo() command construction and error handling."""

    @patch("review_bot.utils.git.subprocess.run")
    def test_constructs_correct_command_with_depth(self, mock_run, tmp_path):
        """clone_repo() should include --depth flag when depth > 0."""
        dest = tmp_path / "repo"
        url = "https://github.com/user/repo.git"

        clone_repo(url, dest, depth=5)

        mock_run.assert_called_once_with(
            ["git", "clone", "--depth", "5", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )

    @patch("review_bot.utils.git.subprocess.run")
    def test_default_depth_is_1(self, mock_run, tmp_path):
        """clone_repo() should use --depth 1 by default."""
        dest = tmp_path / "repo"
        url = "https://github.com/user/repo.git"

        clone_repo(url, dest)

        mock_run.assert_called_once_with(
            ["git", "clone", "--depth", "1", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )

    @patch("review_bot.utils.git.subprocess.run")
    def test_depth_zero_omits_depth_flag(self, mock_run, tmp_path):
        """clone_repo() with depth=0 should omit --depth for a full clone."""
        dest = tmp_path / "repo"
        url = "https://github.com/user/repo.git"

        clone_repo(url, dest, depth=0)

        mock_run.assert_called_once_with(
            ["git", "clone", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )

    @patch("review_bot.utils.git.subprocess.run")
    def test_returns_dest_path(self, mock_run, tmp_path):
        """clone_repo() should return the destination path."""
        dest = tmp_path / "repo"
        result = clone_repo("https://github.com/user/repo.git", dest)
        assert result == dest

    @patch("review_bot.utils.git.subprocess.run")
    def test_raises_called_process_error_on_failure(self, mock_run, tmp_path):
        """clone_repo() should propagate CalledProcessError from git."""
        mock_run.side_effect = subprocess.CalledProcessError(
            128, ["git", "clone"], stderr="fatal: repository not found"
        )
        dest = tmp_path / "repo"

        with pytest.raises(subprocess.CalledProcessError):
            clone_repo("https://github.com/user/bad-repo.git", dest)


class TestGetDiff:
    """Test get_diff() command construction and output."""

    @patch("review_bot.utils.git.subprocess.run")
    def test_constructs_correct_command(self, mock_run, tmp_path):
        """get_diff() should build the correct git diff command."""
        mock_run.return_value = MagicMock(stdout="diff output")

        get_diff(tmp_path, "main", "feature")

        mock_run.assert_called_once_with(
            ["git", "diff", "main...feature"],
            check=True,
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

    @patch("review_bot.utils.git.subprocess.run")
    def test_returns_stdout(self, mock_run, tmp_path):
        """get_diff() should return the stdout from git diff."""
        expected = "diff --git a/foo.py b/foo.py\n+hello\n"
        mock_run.return_value = MagicMock(stdout=expected)

        result = get_diff(tmp_path, "v1.0", "v2.0")

        assert result == expected

    @patch("review_bot.utils.git.subprocess.run")
    def test_returns_empty_string_for_no_diff(self, mock_run, tmp_path):
        """get_diff() should return empty string when there are no changes."""
        mock_run.return_value = MagicMock(stdout="")

        result = get_diff(tmp_path, "same-ref", "same-ref")

        assert result == ""

    @patch("review_bot.utils.git.subprocess.run")
    def test_raises_called_process_error_on_failure(self, mock_run, tmp_path):
        """get_diff() should propagate CalledProcessError from git."""
        mock_run.side_effect = subprocess.CalledProcessError(
            128, ["git", "diff"], stderr="fatal: bad revision"
        )

        with pytest.raises(subprocess.CalledProcessError):
            get_diff(tmp_path, "bad-ref", "other-ref")

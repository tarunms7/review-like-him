"""Tests for the status CLI command."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from review_bot.cli.status_cmd import status_cmd


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_status_cmd_uses_default_port(runner: CliRunner) -> None:
    """Without --port, should hit localhost:8000 (Settings default)."""
    with patch("review_bot.cli.status_cmd.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "rate_limits": {}}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = runner.invoke(status_cmd)

        assert result.exit_code == 0
        mock_get.assert_called_once_with("http://localhost:8000/status", timeout=5.0)


def test_status_cmd_custom_port(runner: CliRunner) -> None:
    """--port 9090 should override the default."""
    with patch("review_bot.cli.status_cmd.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "rate_limits": {}}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = runner.invoke(status_cmd, ["--port", "9090"])

        assert result.exit_code == 0
        mock_get.assert_called_once_with("http://localhost:9090/status", timeout=5.0)


def test_status_cmd_connection_error_shows_port(runner: CliRunner) -> None:
    """Error message should display the actual port being used."""
    with patch("review_bot.cli.status_cmd.httpx.get", side_effect=httpx.ConnectError("fail")):
        result = runner.invoke(status_cmd, ["--port", "3000"])

        assert result.exit_code == 1
        assert "localhost:3000" in result.output


def test_status_cmd_respects_env_port(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """REVIEW_BOT_PORT env var should be picked up via Settings."""
    monkeypatch.setenv("REVIEW_BOT_PORT", "7777")
    with patch("review_bot.cli.status_cmd.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "rate_limits": {}}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = runner.invoke(status_cmd)

        assert result.exit_code == 0
        mock_get.assert_called_once_with("http://localhost:7777/status", timeout=5.0)

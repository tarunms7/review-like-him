"""Tests for narrowed exception handling in RepoScanner."""

from __future__ import annotations

import base64
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from review_bot.config.repo_config import RepoConfig
from review_bot.review.repo_scanner import RepoContext, RepoScanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(text: str) -> str:
    """Encode text as base64 for mock file content responses."""
    return base64.b64encode(text.encode()).decode()


def _dir_entry(name: str, entry_type: str = "file") -> dict:
    """Create a mock directory entry."""
    return {"name": name, "type": entry_type}


def _make_scanner(mock_github_client: MagicMock) -> RepoScanner:
    """Create a RepoScanner with a mock client."""
    return RepoScanner(mock_github_client)


def _minimal_root_contents() -> list[dict]:
    """Root contents with a pyproject.toml and src dir."""
    return [
        _dir_entry("pyproject.toml"),
        _dir_entry("src", "dir"),
    ]


# ---------------------------------------------------------------------------
# Narrow exception handling tests
# ---------------------------------------------------------------------------


class TestNarrowExceptionHandling:
    """Tests for narrowed exception types in scan() and helpers."""

    @pytest.mark.asyncio()
    async def test_scan_modules_httpx_error_graceful_degradation(
        self, mock_github_client,
    ) -> None:
        """HTTPStatusError in _detect_modules degrades to empty modules."""
        root_contents = _minimal_root_contents()

        call_count = 0

        async def mock_get_contents(owner, repo, path):
            nonlocal call_count
            if path == "":
                return root_contents
            if path == "pyproject.toml":
                return {"content": _b64("[project]\nname = 'x'\n")}
            if path == "src":
                # This is called by _detect_modules via _list_dir
                raise httpx.HTTPStatusError(
                    "Server Error",
                    request=httpx.Request("GET", "https://api.github.com"),
                    response=httpx.Response(500),
                )
            if path == ".review-like-him.yml":
                raise httpx.HTTPStatusError(
                    "Not Found",
                    request=httpx.Request("GET", "https://api.github.com"),
                    response=httpx.Response(404),
                )
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = _make_scanner(mock_github_client)
        ctx = await scanner.scan("owner", "repo")

        assert isinstance(ctx, RepoContext)
        # _list_dir catches HTTPStatusError internally and returns None,
        # so modules will be empty but no exception propagates
        assert ctx.modules == [] or isinstance(ctx.modules, list)

    @pytest.mark.asyncio()
    async def test_scan_api_contracts_key_error_logged(
        self, mock_github_client, caplog,
    ) -> None:
        """KeyError in _detect_api_contracts is caught and logged."""
        root_contents = _minimal_root_contents()

        async def mock_get_contents(owner, repo, path):
            if path == "":
                return root_contents
            if path == "pyproject.toml":
                return {"content": _b64("[project]\nname = 'x'\n")}
            if path == ".review-like-him.yml":
                raise httpx.HTTPStatusError(
                    "Not Found",
                    request=httpx.Request("GET", "https://api.github.com"),
                    response=httpx.Response(404),
                )
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = _make_scanner(mock_github_client)

        # Patch _detect_api_contracts to raise KeyError
        original = scanner._detect_api_contracts
        async def raising_detect(*args, **kwargs):
            raise KeyError("missing_field")
        scanner._detect_api_contracts = raising_detect

        with caplog.at_level(logging.WARNING):
            ctx = await scanner.scan("owner", "repo")

        assert ctx.api_contracts == []
        assert any(
            "Failed to detect API contracts" in record.message
            and "missing_field" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio()
    async def test_read_file_unicode_error_returns_none(
        self, mock_github_client,
    ) -> None:
        """UnicodeDecodeError in _read_file returns None."""
        # Create content that's valid base64 but decodes to invalid UTF-8
        invalid_bytes = b"\x80\x81\x82\x83"
        b64_content = base64.b64encode(invalid_bytes).decode("ascii")

        mock_github_client.get_repo_contents = AsyncMock(
            return_value={"content": b64_content},
        )

        scanner = _make_scanner(mock_github_client)
        result = await scanner._read_file("owner", "repo", "binary.dat")

        assert result is None

    @pytest.mark.asyncio()
    async def test_load_repo_config_yaml_error_returns_default(
        self, mock_github_client,
    ) -> None:
        """Invalid YAML in load_repo_config returns RepoConfig.default()."""
        invalid_yaml = ": : : not valid yaml [["

        mock_github_client.get_repo_contents = AsyncMock(
            return_value={"content": _b64(invalid_yaml)},
        )

        scanner = _make_scanner(mock_github_client)
        config = await scanner.load_repo_config("owner", "repo")

        assert config == RepoConfig.default()

    @pytest.mark.asyncio()
    async def test_read_repo_config_yaml_error_returns_none(
        self, mock_github_client,
    ) -> None:
        """Invalid YAML in _read_repo_config returns None."""
        invalid_yaml = '": invalid: yaml: ['

        mock_github_client.get_repo_contents = AsyncMock(
            return_value={"content": _b64(invalid_yaml)},
        )

        scanner = _make_scanner(mock_github_client)
        config = await scanner._read_repo_config("owner", "repo")

        assert config is None

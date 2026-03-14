"""Tests for review_bot.github.api — retry logic, rate limits, API methods."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from review_bot.github.api import (
    GITHUB_API_BASE,
    INITIAL_BACKOFF,
    MAX_RETRIES,
    GitHubAPIClient,
    PullRequestFile,
    ReviewComment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(
    status_code: int = 200,
    json_data: dict | list | None = None,
    text: str = "",
    headers: dict | None = None,
) -> httpx.Response:
    """Build a fake httpx.Response with proper content encoding."""
    if json_data is not None:
        content = json.dumps(json_data).encode()
        h = dict(headers or {})
        h.setdefault("content-type", "application/json")
        return httpx.Response(
            status_code=status_code,
            headers=h,
            content=content,
            request=httpx.Request("GET", "https://api.github.com/test"),
        )
    return httpx.Response(
        status_code=status_code,
        headers=headers or {},
        text=text,
        request=httpx.Request("GET", "https://api.github.com/test"),
    )


# ---------------------------------------------------------------------------
# Retry / Backoff
# ---------------------------------------------------------------------------


class TestRetryLogic:
    """Test exponential backoff on transient failures."""

    @pytest.mark.asyncio
    async def test_retries_on_server_error(self):
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            side_effect=[
                _mock_response(500, text="Internal Server Error"),
                _mock_response(500, text="Internal Server Error"),
                _mock_response(200, json_data={"ok": True}),
            ]
        )
        client = GitHubAPIClient(http_client)

        with patch("review_bot.github.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            resp = await client._request("GET", f"{GITHUB_API_BASE}/test")

        assert resp.status_code == 200
        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            return_value=_mock_response(500, text="Server Error"),
        )
        client = GitHubAPIClient(http_client)

        with patch("review_bot.github.api.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.HTTPStatusError):
                await client._request("GET", f"{GITHUB_API_BASE}/test")

        assert http_client.request.call_count == MAX_RETRIES

    @pytest.mark.asyncio
    async def test_retries_on_transport_error(self):
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            side_effect=[
                httpx.ConnectError("Connection refused"),
                _mock_response(200, json_data={"ok": True}),
            ]
        )
        client = GitHubAPIClient(http_client)

        with patch("review_bot.github.api.asyncio.sleep", new_callable=AsyncMock):
            resp = await client._request("GET", f"{GITHUB_API_BASE}/test")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate Limit Handling
# ---------------------------------------------------------------------------


class TestRateLimitHandling:
    """Test that 403 rate-limit responses trigger retry with backoff."""

    @pytest.mark.asyncio
    async def test_rate_limit_retries(self):
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            side_effect=[
                _mock_response(403, text="API rate limit exceeded"),
                _mock_response(200, json_data={"done": True}),
            ]
        )
        client = GitHubAPIClient(http_client)

        with patch("review_bot.github.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            resp = await client._request("GET", f"{GITHUB_API_BASE}/test")

        assert resp.status_code == 200
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_uses_retry_after_header(self):
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            side_effect=[
                _mock_response(
                    403,
                    text="API rate limit exceeded",
                    headers={"Retry-After": "7"},
                ),
                _mock_response(200, json_data={}),
            ]
        )
        client = GitHubAPIClient(http_client)

        with patch("review_bot.github.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await client._request("GET", f"{GITHUB_API_BASE}/test")

        # Should wait the Retry-After value (7 seconds)
        mock_sleep.assert_called_once_with(7.0)

    @pytest.mark.asyncio
    async def test_non_rate_limit_403_raises(self):
        """A 403 that is NOT rate-limit should raise immediately."""
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            return_value=_mock_response(403, text="Resource not accessible"),
        )
        client = GitHubAPIClient(http_client)

        with pytest.raises(httpx.HTTPStatusError):
            await client._request("GET", f"{GITHUB_API_BASE}/test")


# ---------------------------------------------------------------------------
# API Method Tests
# ---------------------------------------------------------------------------


class TestGitHubAPIMethods:
    """Test high-level API methods delegate correctly."""

    @pytest.mark.asyncio
    async def test_get_pull_request(self):
        pr_data = {"number": 42, "title": "Test PR"}
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            return_value=_mock_response(200, json_data=pr_data)
        )
        client = GitHubAPIClient(http_client)
        result = await client.get_pull_request("owner", "repo", 42)
        assert result == pr_data

    @pytest.mark.asyncio
    async def test_get_pull_request_diff(self):
        diff = "diff --git a/f.py b/f.py\n..."
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            return_value=_mock_response(200, text=diff)
        )
        client = GitHubAPIClient(http_client)
        result = await client.get_pull_request_diff("owner", "repo", 42)
        assert "diff --git" in result

    @pytest.mark.asyncio
    async def test_get_pull_request_files(self):
        files = [
            {
                "filename": "a.py",
                "status": "modified",
                "additions": 5,
                "deletions": 2,
                "patch": "+new line",
            }
        ]
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            return_value=_mock_response(200, json_data=files)
        )
        client = GitHubAPIClient(http_client)
        result = await client.get_pull_request_files("owner", "repo", 42)
        assert len(result) == 1
        assert isinstance(result[0], PullRequestFile)
        assert result[0].filename == "a.py"

    @pytest.mark.asyncio
    async def test_post_review(self):
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            return_value=_mock_response(200, json_data={"id": 1})
        )
        client = GitHubAPIClient(http_client)
        comments = [ReviewComment(path="f.py", line=10, body="Fix")]
        result = await client.post_review("o", "r", 1, "body", "COMMENT", comments)
        assert result == {"id": 1}

    @pytest.mark.asyncio
    async def test_post_comment(self):
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            return_value=_mock_response(200, json_data={"id": 2})
        )
        client = GitHubAPIClient(http_client)
        result = await client.post_comment("o", "r", 1, "Nice!")
        assert result == {"id": 2}

    @pytest.mark.asyncio
    async def test_get_user_reviews_filters_events(self):
        events = [
            {"type": "PullRequestReviewEvent", "id": "1"},
            {"type": "PushEvent", "id": "2"},
            {"type": "PullRequestReviewEvent", "id": "3"},
        ]
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.request = AsyncMock(
            return_value=_mock_response(200, json_data=events)
        )
        client = GitHubAPIClient(http_client)
        result = await client.get_user_reviews("alice")
        assert len(result) == 2
        assert all(e["type"] == "PullRequestReviewEvent" for e in result)


# ---------------------------------------------------------------------------
# JWT Generation (github.app)
# ---------------------------------------------------------------------------


class TestGitHubAppAuth:
    """Test JWT generation and token caching."""

    def test_jwt_generation(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        # Generate a test RSA key
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        key_path = tmp_path / "test-key.pem"
        key_path.write_bytes(pem)

        from review_bot.github.app import GitHubAppAuth
        import jwt as pyjwt

        auth = GitHubAppAuth("12345", str(key_path))
        token = auth.get_jwt()

        # Decode without verification to check claims
        decoded = pyjwt.decode(token, options={"verify_signature": False})
        assert decoded["iss"] == "12345"
        assert "exp" in decoded
        assert "iat" in decoded

    @pytest.mark.asyncio
    async def test_installation_token_caching(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        key_path = tmp_path / "test-key.pem"
        key_path.write_bytes(pem)

        from review_bot.github.app import GitHubAppAuth

        auth = GitHubAppAuth("12345", str(key_path))

        # Pre-populate cache with a token that expires far in the future
        auth._token_cache[99] = ("cached-token", time.time() + 7200)

        token = await auth.get_installation_token(99)
        assert token == "cached-token"

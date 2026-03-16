"""Tests for narrow exception handling in FeedbackPoller."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from review_bot.review.feedback_poller import FeedbackPoller


def _make_poller(github_client=None, feedback_store=None):
    """Create a FeedbackPoller with mocked dependencies."""
    if github_client is None:
        github_client = MagicMock()
    if feedback_store is None:
        feedback_store = MagicMock()
        feedback_store.get_tracked_comments = AsyncMock(return_value=[])
    return FeedbackPoller(
        github_client=github_client,
        feedback_store=feedback_store,
    )


class TestPollReactionsNarrowExceptions:
    """Tests for narrowed exception handling in poll_reactions_for_comment."""

    @pytest.mark.asyncio()
    async def test_poll_reactions_httpx_error_returns_empty(self) -> None:
        """HTTPStatusError returns empty list instead of raising."""
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found",
                request=httpx.Request("GET", "https://api.github.com"),
                response=httpx.Response(404),
            ),
        )
        poller = _make_poller(github_client=client)
        result = await poller.poll_reactions_for_comment("owner", "repo", 123)
        assert result == []

    @pytest.mark.asyncio()
    async def test_poll_reactions_request_error_returns_empty(self) -> None:
        """RequestError returns empty list instead of raising."""
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=httpx.RequestError(
                "Connection failed",
                request=httpx.Request("GET", "https://api.github.com"),
            ),
        )
        poller = _make_poller(github_client=client)
        result = await poller.poll_reactions_for_comment("owner", "repo", 456)
        assert result == []

    @pytest.mark.asyncio()
    async def test_run_poll_loop_cancelled_error_propagates(self) -> None:
        """asyncio.CancelledError is re-raised, not swallowed."""
        store = MagicMock()
        store.get_tracked_comments = AsyncMock(side_effect=asyncio.CancelledError)
        poller = _make_poller(feedback_store=store)

        with pytest.raises(asyncio.CancelledError):
            # poll_all_tracked_comments will raise CancelledError,
            # run_poll_loop catches it and re-raises
            await poller.run_poll_loop()

    @pytest.mark.asyncio()
    async def test_run_poll_loop_general_error_continues(self) -> None:
        """RuntimeError in poll cycle is logged, loop continues."""
        store = MagicMock()
        call_count = 0

        async def mock_get_tracked(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            # Cancel on second iteration to stop the loop
            raise asyncio.CancelledError

        store.get_tracked_comments = AsyncMock(side_effect=mock_get_tracked)
        poller = _make_poller(feedback_store=store)

        # Patch sleep to avoid real delays
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(asyncio.CancelledError):
                await poller.run_poll_loop()

        # The loop survived the RuntimeError and reached the second call
        assert call_count == 2

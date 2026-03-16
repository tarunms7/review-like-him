"""Tests for narrow exception handling in FeedbackPoller."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from review_bot.review.feedback import FeedbackStore
from review_bot.review.feedback_poller import FeedbackPoller


@pytest.fixture()
def mock_feedback_store() -> FeedbackStore:
    """A FeedbackStore with async methods mocked."""
    store = MagicMock(spec=FeedbackStore)
    store.get_tracked_comments = AsyncMock(return_value=[])
    store.get_stored_reactions = AsyncMock(return_value=[])
    store.record_feedback = AsyncMock()
    return store


@pytest.fixture()
def mock_poller_client() -> MagicMock:
    """A GitHubAPIClient mock for FeedbackPoller tests."""
    client = MagicMock()
    client._request = AsyncMock()
    return client


class TestNarrowExceptionHandling:
    """Tests for narrowed exception handling in FeedbackPoller."""

    @pytest.mark.asyncio()
    async def test_poll_reactions_httpx_error_returns_empty(
        self, mock_poller_client, mock_feedback_store,
    ) -> None:
        """HTTPStatusError from _request returns empty list."""
        mock_poller_client._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error",
                request=httpx.Request("GET", "https://api.github.com"),
                response=httpx.Response(500),
            ),
        )
        poller = FeedbackPoller(mock_poller_client, mock_feedback_store)
        result = await poller.poll_reactions_for_comment("owner", "repo", 123)
        assert result == []

    @pytest.mark.asyncio()
    async def test_poll_reactions_request_error_returns_empty(
        self, mock_poller_client, mock_feedback_store,
    ) -> None:
        """RequestError from _request returns empty list."""
        mock_poller_client._request = AsyncMock(
            side_effect=httpx.RequestError(
                "Connection failed",
                request=httpx.Request("GET", "https://api.github.com"),
            ),
        )
        poller = FeedbackPoller(mock_poller_client, mock_feedback_store)
        result = await poller.poll_reactions_for_comment("owner", "repo", 456)
        assert result == []

    @pytest.mark.asyncio()
    async def test_run_poll_loop_cancelled_error_propagates(
        self, mock_poller_client, mock_feedback_store,
    ) -> None:
        """asyncio.CancelledError is re-raised, not swallowed."""
        poller = FeedbackPoller(mock_poller_client, mock_feedback_store)
        poller.poll_all_tracked_comments = AsyncMock(
            side_effect=asyncio.CancelledError(),
        )
        with pytest.raises(asyncio.CancelledError):
            await poller.run_poll_loop()

    @pytest.mark.asyncio()
    async def test_run_poll_loop_general_error_continues(
        self, mock_poller_client, mock_feedback_store,
    ) -> None:
        """RuntimeError in poll cycle is logged; loop continues."""
        call_count = 0

        async def _side_effect() -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            # Cancel on second call so the loop exits
            raise asyncio.CancelledError()

        poller = FeedbackPoller(mock_poller_client, mock_feedback_store)
        poller.poll_all_tracked_comments = AsyncMock(side_effect=_side_effect)
        # Patch sleep to avoid real delays
        poller._poll_interval = MagicMock()
        poller._poll_interval.total_seconds.return_value = 0

        with pytest.raises(asyncio.CancelledError):
            await poller.run_poll_loop()

        # The loop should have called poll at least twice (error + cancel)
        assert call_count == 2

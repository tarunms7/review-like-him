"""Tests for narrow exception handling in FeedbackPoller."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from review_bot.review.feedback import FeedbackStore
from review_bot.review.feedback_poller import FeedbackPoller


@pytest.fixture()
def mock_feedback_store() -> FeedbackStore:
    """Create a mocked FeedbackStore."""
    store = MagicMock(spec=FeedbackStore)
    store.get_tracked_comments = AsyncMock(return_value=[])
    store.get_stored_reactions = AsyncMock(return_value=[])
    store.record_feedback = AsyncMock()
    return store


@pytest.fixture()
def mock_poller_github_client() -> MagicMock:
    """Create a mocked GitHubAPIClient for poller tests."""
    from review_bot.github.api import GitHubAPIClient

    client = MagicMock(spec=GitHubAPIClient)
    client._request = AsyncMock()
    return client


class TestPollReactionsNarrowExceptions:
    """Tests for narrow exception handling in poll_reactions_for_comment."""

    @pytest.mark.asyncio()
    async def test_poll_reactions_httpx_error_returns_empty(
        self, mock_poller_github_client, mock_feedback_store,
    ) -> None:
        """HTTPStatusError returns empty list instead of raising."""
        mock_poller_github_client._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found",
                request=httpx.Request("GET", "https://api.github.com"),
                response=httpx.Response(404),
            ),
        )

        poller = FeedbackPoller(mock_poller_github_client, mock_feedback_store)
        result = await poller.poll_reactions_for_comment("owner", "repo", 123)

        assert result == []

    @pytest.mark.asyncio()
    async def test_poll_reactions_request_error_returns_empty(
        self, mock_poller_github_client, mock_feedback_store,
    ) -> None:
        """RequestError returns empty list instead of raising."""
        mock_poller_github_client._request = AsyncMock(
            side_effect=httpx.RequestError(
                "Connection failed",
                request=httpx.Request("GET", "https://api.github.com"),
            ),
        )

        poller = FeedbackPoller(mock_poller_github_client, mock_feedback_store)
        result = await poller.poll_reactions_for_comment("owner", "repo", 456)

        assert result == []


class TestRunPollLoopExceptionHandling:
    """Tests for exception handling in run_poll_loop."""

    @pytest.mark.asyncio()
    async def test_run_poll_loop_cancelled_error_propagates(
        self, mock_poller_github_client, mock_feedback_store,
    ) -> None:
        """asyncio.CancelledError is re-raised, not swallowed."""
        poller = FeedbackPoller(mock_poller_github_client, mock_feedback_store)

        with patch.object(
            poller,
            "poll_all_tracked_comments",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError(),
        ):
            with pytest.raises(asyncio.CancelledError):
                await poller.run_poll_loop()

    @pytest.mark.asyncio()
    async def test_run_poll_loop_general_error_continues(
        self, mock_poller_github_client, mock_feedback_store,
    ) -> None:
        """RuntimeError in poll cycle is logged but loop continues."""
        call_count = 0

        async def mock_poll():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            # Cancel after second successful call to stop the loop
            raise asyncio.CancelledError()

        poller = FeedbackPoller(mock_poller_github_client, mock_feedback_store)

        with patch.object(poller, "poll_all_tracked_comments", side_effect=mock_poll):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(asyncio.CancelledError):
                    await poller.run_poll_loop()

        # The loop survived the RuntimeError and ran again
        assert call_count == 2

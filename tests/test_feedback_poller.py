"""Tests for narrow exception handling in FeedbackPoller."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from review_bot.review.feedback import FeedbackStore
from review_bot.review.feedback_poller import FeedbackPoller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_poller(
    github_client: MagicMock | None = None,
    feedback_store: MagicMock | None = None,
) -> FeedbackPoller:
    """Create a FeedbackPoller with mocked dependencies."""
    client = github_client or MagicMock()
    store = feedback_store or MagicMock(spec=FeedbackStore)
    return FeedbackPoller(github_client=client, feedback_store=store)


# ---------------------------------------------------------------------------
# poll_reactions_for_comment tests
# ---------------------------------------------------------------------------


class TestPollReactionsNarrowExceptions:
    """Test narrow exception handling in poll_reactions_for_comment."""

    @pytest.mark.asyncio()
    async def test_poll_reactions_httpx_error_returns_empty(self) -> None:
        """HTTPStatusError is caught and returns empty list."""
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
        """RequestError (connection issues) is caught and returns empty list."""
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


# ---------------------------------------------------------------------------
# run_poll_loop tests
# ---------------------------------------------------------------------------


class TestRunPollLoopExceptions:
    """Test exception handling in run_poll_loop."""

    @pytest.mark.asyncio()
    async def test_run_poll_loop_cancelled_error_propagates(self) -> None:
        """asyncio.CancelledError is re-raised, not swallowed."""
        store = MagicMock(spec=FeedbackStore)
        store.get_tracked_comments = AsyncMock(
            side_effect=asyncio.CancelledError(),
        )
        poller = _make_poller(feedback_store=store)

        with pytest.raises(asyncio.CancelledError):
            await poller.run_poll_loop()

    @pytest.mark.asyncio()
    async def test_run_poll_loop_general_error_continues(self) -> None:
        """RuntimeError in poll cycle is logged but loop continues."""
        store = MagicMock(spec=FeedbackStore)
        call_count = 0

        async def _mock_poll() -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            # Cancel on second call so the loop exits
            raise asyncio.CancelledError()

        poller = _make_poller(feedback_store=store)
        poller.poll_all_tracked_comments = _mock_poll  # type: ignore[assignment]

        with pytest.raises(asyncio.CancelledError):
            # Patch sleep to avoid actual waiting
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await poller.run_poll_loop()

        # The loop survived the RuntimeError and reached the second call
        assert call_count == 2

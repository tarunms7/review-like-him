"""Tests for narrowed exception handling in FeedbackPoller."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from review_bot.review.feedback_poller import FeedbackPoller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_poller(
    github_client: MagicMock | None = None,
    feedback_store: MagicMock | None = None,
) -> FeedbackPoller:
    """Create a FeedbackPoller with mock dependencies."""
    if github_client is None:
        github_client = MagicMock()
    if feedback_store is None:
        feedback_store = MagicMock()
    return FeedbackPoller(
        github_client=github_client,
        feedback_store=feedback_store,
        poll_interval=timedelta(seconds=1),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPollReactionsNarrowExceptions:
    """Tests for narrowed exception handling in poll_reactions_for_comment."""

    @pytest.mark.asyncio()
    async def test_poll_reactions_httpx_error_returns_empty(self) -> None:
        """HTTPStatusError returns empty list instead of crashing."""
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Forbidden",
                request=httpx.Request("GET", "https://api.github.com"),
                response=httpx.Response(403),
            ),
        )

        poller = _make_poller(github_client=client)
        result = await poller.poll_reactions_for_comment("owner", "repo", 123)

        assert result == []

    @pytest.mark.asyncio()
    async def test_poll_reactions_request_error_returns_empty(self) -> None:
        """RequestError (connection issue) returns empty list."""
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=httpx.RequestError(
                "Connection refused",
                request=httpx.Request("GET", "https://api.github.com"),
            ),
        )

        poller = _make_poller(github_client=client)
        result = await poller.poll_reactions_for_comment("owner", "repo", 456)

        assert result == []


class TestRunPollLoopExceptionHandling:
    """Tests for exception handling in run_poll_loop."""

    @pytest.mark.asyncio()
    async def test_run_poll_loop_cancelled_error_propagates(self) -> None:
        """asyncio.CancelledError is re-raised, not swallowed."""
        store = MagicMock()
        store.get_tracked_comments = AsyncMock(
            side_effect=asyncio.CancelledError(),
        )

        poller = _make_poller(feedback_store=store)

        with pytest.raises(asyncio.CancelledError):
            await poller.run_poll_loop()

    @pytest.mark.asyncio()
    async def test_run_poll_loop_general_error_continues(self) -> None:
        """RuntimeError in poll cycle doesn't crash the loop."""
        call_count = 0

        async def mock_poll(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            # On second call, cancel the loop to end the test
            raise asyncio.CancelledError()

        poller = _make_poller()
        poller.poll_all_tracked_comments = AsyncMock(side_effect=mock_poll)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(asyncio.CancelledError):
                await poller.run_poll_loop()

        # The loop should have continued past the RuntimeError
        assert call_count == 2

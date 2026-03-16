"""Tests for narrowed exception handling in FeedbackPoller."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from review_bot.review.feedback_poller import FeedbackPoller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_poller(github_client: MagicMock | None = None) -> FeedbackPoller:
    """Create a FeedbackPoller with mocked dependencies."""
    client = github_client or MagicMock()
    store = MagicMock()
    store.get_tracked_comments = AsyncMock(return_value=[])
    store.get_stored_reactions = AsyncMock(return_value=[])
    store.record_feedback = AsyncMock()
    return FeedbackPoller(
        github_client=client,
        feedback_store=store,
    )


# ---------------------------------------------------------------------------
# poll_reactions_for_comment — narrowed exception tests
# ---------------------------------------------------------------------------


class TestPollReactionsNarrowExceptions:
    """Verify narrowed exception handling in poll_reactions_for_comment."""

    @pytest.mark.asyncio()
    async def test_poll_reactions_httpx_error_returns_empty(self) -> None:
        """HTTPStatusError should be caught and return []."""
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error",
                request=httpx.Request("GET", "https://api.github.com"),
                response=httpx.Response(500),
            ),
        )
        poller = _make_poller(client)

        result = await poller.poll_reactions_for_comment("owner", "repo", 123)

        assert result == []

    @pytest.mark.asyncio()
    async def test_poll_reactions_request_error_returns_empty(self) -> None:
        """RequestError (connection issue) should be caught and return []."""
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=httpx.RequestError(
                "Connection failed",
                request=httpx.Request("GET", "https://api.github.com"),
            ),
        )
        poller = _make_poller(client)

        result = await poller.poll_reactions_for_comment("owner", "repo", 456)

        assert result == []


# ---------------------------------------------------------------------------
# run_poll_loop — exception handling tests
# ---------------------------------------------------------------------------


class TestRunPollLoopExceptions:
    """Verify run_poll_loop exception handling behavior."""

    @pytest.mark.asyncio()
    async def test_run_poll_loop_cancelled_error_propagates(self) -> None:
        """asyncio.CancelledError must propagate (not be swallowed)."""
        poller = _make_poller()
        poller._feedback_store.get_tracked_comments = AsyncMock(
            side_effect=asyncio.CancelledError(),
        )

        with pytest.raises(asyncio.CancelledError):
            await poller.run_poll_loop()

    @pytest.mark.asyncio()
    async def test_run_poll_loop_general_error_continues(self) -> None:
        """RuntimeError in poll cycle should be logged, not crash the loop."""
        poller = _make_poller()
        call_count = 0

        async def _mock_poll() -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            # On second call, cancel the loop so the test finishes
            raise asyncio.CancelledError()

        poller.poll_all_tracked_comments = AsyncMock(side_effect=_mock_poll)
        # Set a tiny sleep interval so the loop iterates quickly
        poller._poll_interval = __import__("datetime").timedelta(seconds=0)

        with pytest.raises(asyncio.CancelledError):
            await poller.run_poll_loop()

        # The loop must have survived the RuntimeError on the first call
        assert call_count == 2

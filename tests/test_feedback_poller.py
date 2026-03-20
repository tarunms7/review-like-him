"""Tests for review_bot.review.feedback_poller — reaction polling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from review_bot.review.feedback import FeedbackEvent
from review_bot.review.feedback_poller import (
    REACTION_FEEDBACK,
    FeedbackPoller,
    _is_bot_user,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_github_client() -> MagicMock:
    client = MagicMock()
    client._request = AsyncMock()
    client.get_comment_reactions = AsyncMock(return_value=[])
    return client


@pytest.fixture()
def mock_feedback_store() -> MagicMock:
    store = MagicMock()
    store.get_tracked_comments = AsyncMock(return_value=[])
    store.get_stored_reactions = AsyncMock(return_value=[])
    store.record_feedback = AsyncMock()
    return store


@pytest.fixture()
def poller(mock_github_client, mock_feedback_store) -> FeedbackPoller:
    return FeedbackPoller(mock_github_client, mock_feedback_store)


# ---------------------------------------------------------------------------
# _is_bot_user
# ---------------------------------------------------------------------------


class TestIsBotUser:
    """Test bot user detection."""

    def test_bot_suffix_bracket(self):
        assert _is_bot_user("renovate[bot]") is True

    def test_bot_suffix_dash(self):
        assert _is_bot_user("dependabot-bot") is True

    def test_normal_user(self):
        assert _is_bot_user("alice") is False

    def test_case_insensitive(self):
        assert _is_bot_user("MyApp[Bot]") is True
        assert _is_bot_user("HELPER-BOT") is True

    def test_empty_string(self):
        assert _is_bot_user("") is False


# ---------------------------------------------------------------------------
# poll_reactions_for_comment
# ---------------------------------------------------------------------------


class TestPollReactionsForComment:
    """Test fetching reactions for a single comment."""

    @pytest.mark.asyncio
    async def test_returns_parsed_reactions(self, poller, mock_github_client):
        """Successful API call returns reaction list."""
        reactions = [
            {"id": 1, "user": {"login": "alice"}, "content": "+1"},
            {"id": 2, "user": {"login": "bob"}, "content": "-1"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = reactions
        mock_github_client._request = AsyncMock(return_value=mock_response)

        result = await poller.poll_reactions_for_comment("owner", "repo", 123)

        assert result == reactions
        mock_github_client._request.assert_called_once()
        call_args = mock_github_client._request.call_args
        assert call_args[0][0] == "GET"
        assert "/pulls/comments/123/reactions" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_returns_empty_on_http_error(
        self, poller, mock_github_client
    ):
        """HTTP errors should return empty list, not raise."""
        mock_github_client._request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404", request=MagicMock(), response=MagicMock()
            )
        )

        result = await poller.poll_reactions_for_comment("owner", "repo", 123)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_request_error(
        self, poller, mock_github_client
    ):
        """Network errors should return empty list."""
        mock_github_client._request = AsyncMock(
            side_effect=httpx.RequestError("timeout")
        )

        result = await poller.poll_reactions_for_comment("owner", "repo", 123)
        assert result == []


# ---------------------------------------------------------------------------
# poll_all_tracked_comments
# ---------------------------------------------------------------------------


class TestPollAllTrackedComments:
    """Test the full poll cycle across all tracked comments."""

    @pytest.mark.asyncio
    async def test_records_new_feedback_events(
        self, poller, mock_github_client, mock_feedback_store
    ):
        """New reactions should be recorded as feedback events."""
        mock_feedback_store.get_tracked_comments = AsyncMock(
            return_value=[
                {
                    "comment_id": 100,
                    "repo": "owner/repo",
                    "pr_author": "pr-author",
                    "persona_name": "alice",
                },
            ]
        )
        mock_feedback_store.get_stored_reactions = AsyncMock(return_value=[])

        reactions = [
            {"id": 1, "user": {"login": "alice"}, "content": "+1"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = reactions
        mock_github_client._request = AsyncMock(return_value=mock_response)

        new_count = await poller.poll_all_tracked_comments()

        assert new_count == 1
        mock_feedback_store.record_feedback.assert_called_once()
        event = mock_feedback_store.record_feedback.call_args[0][0]
        assert isinstance(event, FeedbackEvent)
        assert event.comment_id == 100
        assert event.feedback_type == "positive"
        assert event.feedback_source == "reaction"
        assert event.reactor_username == "alice"
        assert event.is_pr_author is False

    @pytest.mark.asyncio
    async def test_skips_bot_users(
        self, poller, mock_github_client, mock_feedback_store
    ):
        """Reactions from bot users should be ignored."""
        mock_feedback_store.get_tracked_comments = AsyncMock(
            return_value=[
                {
                    "comment_id": 100,
                    "repo": "owner/repo",
                    "pr_author": "",
                },
            ]
        )
        mock_feedback_store.get_stored_reactions = AsyncMock(return_value=[])

        reactions = [
            {"id": 1, "user": {"login": "renovate[bot]"}, "content": "+1"},
            {"id": 2, "user": {"login": "dependabot-bot"}, "content": "heart"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = reactions
        mock_github_client._request = AsyncMock(return_value=mock_response)

        new_count = await poller.poll_all_tracked_comments()

        assert new_count == 0
        mock_feedback_store.record_feedback.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_already_stored_reactions(
        self, poller, mock_github_client, mock_feedback_store
    ):
        """Reactions that already exist in the store should be skipped."""
        mock_feedback_store.get_tracked_comments = AsyncMock(
            return_value=[
                {
                    "comment_id": 100,
                    "repo": "owner/repo",
                    "pr_author": "",
                },
            ]
        )
        mock_feedback_store.get_stored_reactions = AsyncMock(
            return_value=[
                {
                    "reactor_username": "alice",
                    "feedback_type": "positive",
                    "feedback_source": "reaction",
                },
            ]
        )

        reactions = [
            {"id": 1, "user": {"login": "alice"}, "content": "+1"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = reactions
        mock_github_client._request = AsyncMock(return_value=mock_response)

        new_count = await poller.poll_all_tracked_comments()

        assert new_count == 0
        mock_feedback_store.record_feedback.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_invalid_repo_format(
        self, poller, mock_github_client, mock_feedback_store
    ):
        """Comments with invalid repo format (no slash) should be skipped."""
        mock_feedback_store.get_tracked_comments = AsyncMock(
            return_value=[
                {
                    "comment_id": 100,
                    "repo": "invalid-repo-no-slash",
                    "pr_author": "",
                },
            ]
        )

        new_count = await poller.poll_all_tracked_comments()

        assert new_count == 0
        mock_github_client._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_feedback_mapping(
        self, poller, mock_github_client, mock_feedback_store
    ):
        """All REACTION_FEEDBACK mappings should be used correctly."""
        mock_feedback_store.get_tracked_comments = AsyncMock(
            return_value=[
                {
                    "comment_id": 100,
                    "repo": "owner/repo",
                    "pr_author": "pr-author",
                },
            ]
        )
        mock_feedback_store.get_stored_reactions = AsyncMock(return_value=[])

        # Create reactions for each mapped type
        reactions = [
            {"id": i, "user": {"login": f"user{i}"}, "content": content}
            for i, content in enumerate(REACTION_FEEDBACK.keys())
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = reactions
        mock_github_client._request = AsyncMock(return_value=mock_response)

        new_count = await poller.poll_all_tracked_comments()

        assert new_count == len(REACTION_FEEDBACK)
        recorded_types = [
            call[0][0].feedback_type
            for call in mock_feedback_store.record_feedback.call_args_list
        ]
        for content, expected_type in REACTION_FEEDBACK.items():
            assert expected_type in recorded_types

    @pytest.mark.asyncio
    async def test_pr_author_flag_set_correctly(
        self, poller, mock_github_client, mock_feedback_store
    ):
        """is_pr_author should be True when reactor matches pr_author."""
        mock_feedback_store.get_tracked_comments = AsyncMock(
            return_value=[
                {
                    "comment_id": 100,
                    "repo": "owner/repo",
                    "pr_author": "alice",
                },
            ]
        )
        mock_feedback_store.get_stored_reactions = AsyncMock(return_value=[])

        reactions = [
            {"id": 1, "user": {"login": "alice"}, "content": "+1"},
            {"id": 2, "user": {"login": "bob"}, "content": "+1"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = reactions
        mock_github_client._request = AsyncMock(return_value=mock_response)

        await poller.poll_all_tracked_comments()

        events = [
            call[0][0]
            for call in mock_feedback_store.record_feedback.call_args_list
        ]
        alice_event = next(e for e in events if e.reactor_username == "alice")
        bob_event = next(e for e in events if e.reactor_username == "bob")
        assert alice_event.is_pr_author is True
        assert bob_event.is_pr_author is False

    @pytest.mark.asyncio
    async def test_no_tracked_comments_returns_zero(
        self, poller, mock_feedback_store
    ):
        """When there are no tracked comments, return 0."""
        mock_feedback_store.get_tracked_comments = AsyncMock(return_value=[])

        result = await poller.poll_all_tracked_comments()

        assert result == 0

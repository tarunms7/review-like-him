"""Tests for feedback collection, storage, and persona re-weighting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from review_bot.persona.analyzer import _apply_ema_smoothing
from review_bot.review.feedback import (
    FeedbackEvent,
    FeedbackStore,
)
from review_bot.review.feedback_poller import (
    REACTION_FEEDBACK,
    FeedbackPoller,
    _is_bot_user,
)


@pytest.fixture()
async def db_engine():
    """Create an in-memory SQLite async engine with feedback tables."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    store = FeedbackStore(engine)
    await store.ensure_tables()
    yield engine
    await engine.dispose()


@pytest.fixture()
async def feedback_store(db_engine):
    """Create a FeedbackStore with initialized tables."""
    return FeedbackStore(db_engine)


class TestRecordFeedback:
    """Tests for recording feedback events."""

    @pytest.mark.asyncio()
    async def test_record_feedback_positive(self, feedback_store: FeedbackStore) -> None:
        """Record a positive feedback event and verify it's stored."""
        # First track a comment
        await feedback_store.track_posted_comment(
            comment_id=100,
            review_id="rev-1",
            persona_name="alice",
            repo="owner/repo",
            pr_number=42,
            file_path="src/auth.py",
            line_number=10,
            body="Check error handling here",
            category="error_handling",
        )

        event = FeedbackEvent(
            comment_id=100,
            feedback_type="positive",
            feedback_source="reaction",
            reactor_username="dev-user",
            is_pr_author=True,
        )
        await feedback_store.record_feedback(event)

        stored = await feedback_store.get_stored_reactions(100)
        assert len(stored) == 1
        assert stored[0]["feedback_type"] == "positive"
        assert stored[0]["reactor_username"] == "dev-user"

    @pytest.mark.asyncio()
    async def test_record_feedback_deduplicates(self, feedback_store: FeedbackStore) -> None:
        """Duplicate feedback events should be silently ignored."""
        event = FeedbackEvent(
            comment_id=200,
            feedback_type="positive",
            feedback_source="reaction",
            reactor_username="dev-user",
            is_pr_author=False,
        )
        await feedback_store.record_feedback(event)
        await feedback_store.record_feedback(event)  # duplicate

        stored = await feedback_store.get_stored_reactions(200)
        assert len(stored) == 1


class TestFeedbackSummary:
    """Tests for aggregated feedback summaries."""

    @pytest.mark.asyncio()
    async def test_feedback_summary_by_category(self, feedback_store: FeedbackStore) -> None:
        """Feedback should be grouped by category."""
        # Track comments in two categories
        await feedback_store.track_posted_comment(
            comment_id=300, review_id="rev-2", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path="src/auth.py",
            line_number=10, body="Security issue found", category="Security",
        )
        await feedback_store.track_posted_comment(
            comment_id=301, review_id="rev-2", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path="src/test.py",
            line_number=5, body="Missing test case", category="Testing",
        )

        # Add positive feedback to Security
        await feedback_store.record_feedback(FeedbackEvent(
            comment_id=300, feedback_type="positive", feedback_source="reaction",
            reactor_username="user1", is_pr_author=False,
        ))
        # Add negative feedback to Testing
        await feedback_store.record_feedback(FeedbackEvent(
            comment_id=301, feedback_type="negative", feedback_source="reaction",
            reactor_username="user1", is_pr_author=False,
        ))

        summaries = await feedback_store.get_persona_feedback_summary("alice")
        assert len(summaries) == 2

        by_cat = {s.category: s for s in summaries}
        assert by_cat["Security"].positive_count == 1
        assert by_cat["Security"].approval_rate == 1.0
        assert by_cat["Testing"].negative_count == 1
        assert by_cat["Testing"].approval_rate == 0.0

    @pytest.mark.asyncio()
    async def test_feedback_weights_pr_author_2x(self, feedback_store: FeedbackStore) -> None:
        """PR author feedback should be weighted 2x."""
        await feedback_store.track_posted_comment(
            comment_id=400, review_id="rev-3", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path=None,
            line_number=None, body="Good catch", category="Bugs",
        )

        # PR author gives positive feedback (weight 2)
        await feedback_store.record_feedback(FeedbackEvent(
            comment_id=400, feedback_type="positive", feedback_source="reaction",
            reactor_username="pr-author", is_pr_author=True,
        ))
        # Non-author gives negative feedback (weight 1)
        await feedback_store.record_feedback(FeedbackEvent(
            comment_id=400, feedback_type="negative", feedback_source="reaction",
            reactor_username="other-user", is_pr_author=False,
        ))

        summaries = await feedback_store.get_persona_feedback_summary("alice")
        assert len(summaries) == 1
        s = summaries[0]
        # PR author positive = 2, non-author negative = 1
        assert s.positive_count == 2
        assert s.negative_count == 1
        assert s.approval_rate == pytest.approx(2.0 / 3.0)

    @pytest.mark.asyncio()
    async def test_feedback_ignores_old_reviews(self, feedback_store: FeedbackStore) -> None:
        """Feedback older than since_days should be excluded."""
        # Insert a comment with old posted_at date
        old_date = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        async with feedback_store._engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO review_comment_tracking
                    (comment_id, review_id, persona_name, repo, pr_number,
                     body, category, posted_at)
                VALUES (500, 'rev-old', 'alice', 'owner/repo', 1,
                        'Old comment', 'Security', :posted_at)
            """), {"posted_at": old_date})

        await feedback_store.record_feedback(FeedbackEvent(
            comment_id=500, feedback_type="positive", feedback_source="reaction",
            reactor_username="user1", is_pr_author=False,
        ))

        summaries = await feedback_store.get_persona_feedback_summary("alice", since_days=90)
        # Old comment should be excluded
        assert len(summaries) == 0

    @pytest.mark.asyncio()
    async def test_feedback_minimum_sample_size(self, feedback_store: FeedbackStore) -> None:
        """Summary should work correctly even with minimal data."""
        await feedback_store.track_posted_comment(
            comment_id=600, review_id="rev-5", persona_name="bob",
            repo="owner/repo", pr_number=1, file_path=None,
            line_number=None, body="Single comment", category="Style",
        )

        summaries = await feedback_store.get_persona_feedback_summary("bob")
        assert len(summaries) == 1
        s = summaries[0]
        assert s.total_comments == 1
        assert s.positive_count == 0
        assert s.negative_count == 0
        assert s.approval_rate == 0.0


class TestCategoryApprovalRates:
    """Tests for category approval rate queries."""

    @pytest.mark.asyncio()
    async def test_category_approval_rates(self, feedback_store: FeedbackStore) -> None:
        """get_category_approval_rates should return a dict of rates."""
        await feedback_store.track_posted_comment(
            comment_id=700, review_id="rev-6", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path=None,
            line_number=None, body="Test body", category="Naming",
        )
        await feedback_store.record_feedback(FeedbackEvent(
            comment_id=700, feedback_type="positive", feedback_source="reaction",
            reactor_username="user1", is_pr_author=False,
        ))

        rates = await feedback_store.get_category_approval_rates("alice")
        assert "Naming" in rates
        assert rates["Naming"] == 1.0


class TestEMASmoothing:
    """Tests for exponential moving average smoothing."""

    def test_ema_smoothing_prevents_oscillation(self) -> None:
        """EMA should smooth out rapid rate changes."""
        # Start at 0.5, current is 1.0, alpha=0.3
        smoothed = _apply_ema_smoothing(1.0, 0.5, alpha=0.3)
        assert smoothed == pytest.approx(0.65)

        # Apply again with current=0.0
        smoothed2 = _apply_ema_smoothing(0.0, smoothed, alpha=0.3)
        assert smoothed2 == pytest.approx(0.455)

        # Should not jump directly to 0 or 1
        assert 0 < smoothed2 < 1

    def test_ema_smoothing_converges(self) -> None:
        """EMA should eventually converge to the sustained value."""
        rate = 0.5
        for _ in range(50):
            rate = _apply_ema_smoothing(1.0, rate, alpha=0.3)
        # After many iterations of constant 1.0 input, should be close to 1.0
        assert rate == pytest.approx(1.0, abs=0.01)


class TestIgnoreBotReactions:
    """Tests for bot reaction filtering."""

    def test_is_bot_user_detects_bots(self) -> None:
        """Bot usernames should be detected correctly."""
        assert _is_bot_user("github-actions[bot]") is True
        assert _is_bot_user("review-bot") is True
        assert _is_bot_user("dependabot[bot]") is True

    def test_is_bot_user_allows_humans(self) -> None:
        """Human usernames should not be flagged as bots."""
        assert _is_bot_user("alice") is False
        assert _is_bot_user("dev-user") is False
        assert _is_bot_user("bot-lover") is False


class TestFeedbackPoller:
    """Tests for the FeedbackPoller service."""

    @pytest.mark.asyncio()
    async def test_poll_all_tracked_comments(self, feedback_store: FeedbackStore) -> None:
        """Poller should fetch reactions and record new feedback events."""
        await feedback_store.track_posted_comment(
            comment_id=800, review_id="rev-7", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path="src/main.py",
            line_number=5, body="Check this", category="Bugs",
        )

        # Mock GitHub client to return reactions
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "content": "+1",
                "user": {"login": "dev-user"},
            },
            {
                "content": "heart",
                "user": {"login": "another-user"},
            },
            {
                "content": "+1",
                "user": {"login": "github-actions[bot]"},  # Bot, should be ignored
            },
        ]
        mock_client = MagicMock()
        mock_client._request = AsyncMock(return_value=mock_response)

        poller = FeedbackPoller(
            github_client=mock_client,
            feedback_store=feedback_store,
        )
        new_count = await poller.poll_all_tracked_comments()

        assert new_count == 2  # 2 human reactions, 1 bot ignored

        stored = await feedback_store.get_stored_reactions(800)
        assert len(stored) == 2
        usernames = {r["reactor_username"] for r in stored}
        assert "dev-user" in usernames
        assert "another-user" in usernames
        assert "github-actions[bot]" not in usernames

    @pytest.mark.asyncio()
    async def test_poll_skips_already_stored_reactions(
        self, feedback_store: FeedbackStore
    ) -> None:
        """Poller should not re-record already stored reactions."""
        await feedback_store.track_posted_comment(
            comment_id=900, review_id="rev-8", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path=None,
            line_number=None, body="Test", category="Style",
        )

        # Pre-store a reaction
        await feedback_store.record_feedback(FeedbackEvent(
            comment_id=900, feedback_type="positive", feedback_source="reaction",
            reactor_username="existing-user", is_pr_author=False,
        ))

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"content": "+1", "user": {"login": "existing-user"}},
            {"content": "-1", "user": {"login": "new-user"}},
        ]
        mock_client = MagicMock()
        mock_client._request = AsyncMock(return_value=mock_response)

        poller = FeedbackPoller(
            github_client=mock_client,
            feedback_store=feedback_store,
        )
        new_count = await poller.poll_all_tracked_comments()

        assert new_count == 1  # Only new-user's reaction

    @pytest.mark.asyncio()
    async def test_poll_empty_comments(self, feedback_store: FeedbackStore) -> None:
        """Poller should handle no tracked comments gracefully."""
        mock_client = MagicMock()
        poller = FeedbackPoller(
            github_client=mock_client,
            feedback_store=feedback_store,
        )
        new_count = await poller.poll_all_tracked_comments()
        assert new_count == 0


class TestReactionMapping:
    """Tests for the reaction-to-feedback type mapping."""

    def test_positive_reactions(self) -> None:
        """Positive GitHub reactions should map to 'positive'."""
        for reaction in ["+1", "heart", "hooray", "rocket"]:
            assert REACTION_FEEDBACK[reaction] == "positive"

    def test_negative_reactions(self) -> None:
        """Negative GitHub reactions should map to 'negative'."""
        assert REACTION_FEEDBACK["-1"] == "negative"

    def test_confused_reactions(self) -> None:
        """Confused reaction should map to 'confused'."""
        assert REACTION_FEEDBACK["confused"] == "confused"

    def test_neutral_reactions(self) -> None:
        """Eyes and laugh reactions should map to 'neutral'."""
        assert REACTION_FEEDBACK["eyes"] == "neutral"
        assert REACTION_FEEDBACK["laugh"] == "neutral"


class TestPrAuthorTracking:
    """Tests for PR author tracking and is_pr_author detection."""

    @pytest.mark.asyncio()
    async def test_track_comment_with_pr_author(self, feedback_store: FeedbackStore) -> None:
        """track_posted_comment should store pr_author."""
        await feedback_store.track_posted_comment(
            comment_id=1100, review_id="rev-11", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path=None,
            line_number=None, body="Nice code", category="Style",
            pr_author="pr-dev",
        )
        comments = await feedback_store.get_tracked_comments()
        assert len(comments) == 1
        assert comments[0]["pr_author"] == "pr-dev"

    @pytest.mark.asyncio()
    async def test_get_pr_author_for_comment(self, feedback_store: FeedbackStore) -> None:
        """get_pr_author_for_comment should return stored pr_author."""
        await feedback_store.track_posted_comment(
            comment_id=1200, review_id="rev-12", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path=None,
            line_number=None, body="Check this", category="Bugs",
            pr_author="the-author",
        )
        author = await feedback_store.get_pr_author_for_comment(1200)
        assert author == "the-author"

    @pytest.mark.asyncio()
    async def test_get_pr_author_for_missing_comment(self, feedback_store: FeedbackStore) -> None:
        """get_pr_author_for_comment should return None for unknown comment."""
        author = await feedback_store.get_pr_author_for_comment(9999)
        assert author is None

    @pytest.mark.asyncio()
    async def test_poller_sets_is_pr_author(self, feedback_store: FeedbackStore) -> None:
        """Poller should set is_pr_author=True when reactor matches pr_author."""
        await feedback_store.track_posted_comment(
            comment_id=1300, review_id="rev-13", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path="src/main.py",
            line_number=5, body="Check this", category="Bugs",
            pr_author="pr-dev",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"content": "+1", "user": {"login": "pr-dev"}},
            {"content": "heart", "user": {"login": "other-user"}},
        ]
        mock_client = MagicMock()
        mock_client._request = AsyncMock(return_value=mock_response)

        poller = FeedbackPoller(
            github_client=mock_client,
            feedback_store=feedback_store,
        )
        new_count = await poller.poll_all_tracked_comments()
        assert new_count == 2

        stored = await feedback_store.get_stored_reactions(1300)
        by_user = {r["reactor_username"]: r for r in stored}
        assert by_user["pr-dev"]["is_pr_author"] == 1
        assert by_user["other-user"]["is_pr_author"] == 0

    @pytest.mark.asyncio()
    async def test_track_comment_default_pr_author(self, feedback_store: FeedbackStore) -> None:
        """track_posted_comment without pr_author should default to empty string."""
        await feedback_store.track_posted_comment(
            comment_id=1400, review_id="rev-14", persona_name="alice",
            repo="owner/repo", pr_number=42, file_path=None,
            line_number=None, body="Test", category="Style",
        )
        comments = await feedback_store.get_tracked_comments()
        matching = [c for c in comments if c["comment_id"] == 1400]
        assert len(matching) == 1
        assert matching[0]["pr_author"] == ""


class TestWebhookFeedbackHandlers:
    """Tests for webhook feedback event handlers."""

    @pytest.mark.asyncio()
    async def test_reply_sentiment_analysis(self) -> None:
        """Reply sentiment analysis should classify common responses."""
        from review_bot.server.webhooks import _analyze_reply_sentiment

        assert _analyze_reply_sentiment("thanks, good catch!") == "positive"
        assert _analyze_reply_sentiment("i disagree with this") == "negative"
        assert _analyze_reply_sentiment("what do you mean by this?") == "confused"
        assert _analyze_reply_sentiment("acknowledged") == "neutral"

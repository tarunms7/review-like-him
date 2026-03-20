"""Tests for review_bot.review.github_poster — posting reviews to GitHub."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from review_bot.review.formatter import (
    CategorySection,
    Finding,
    InlineComment,
    ReviewResult,
)
from review_bot.review.github_poster import (
    _CONFIDENCE_LEGEND,
    _VERDICT_TO_EVENT,
    ReviewPoster,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_github_client() -> MagicMock:
    client = MagicMock()
    client.post_review = AsyncMock(return_value={"id": 999})
    client.post_comment = AsyncMock(return_value={"id": 1000})
    client.get_review_comments = AsyncMock(return_value=[])
    return client


@pytest.fixture()
def mock_feedback_store() -> MagicMock:
    store = MagicMock()
    store.track_posted_comment = AsyncMock()
    store.get_tracked_comments = AsyncMock(return_value=[])
    return store


@pytest.fixture()
def simple_result() -> ReviewResult:
    return ReviewResult(
        verdict="approve",
        summary_sections=[
            CategorySection(
                emoji="🐛",
                title="Bugs",
                findings=[
                    Finding(text="Null pointer risk", confidence="high"),
                ],
            ),
        ],
        inline_comments=[
            InlineComment(
                file="src/main.py",
                line=42,
                body="This nil check is missing",
                confidence="high",
            ),
        ],
        persona_name="alice",
        pr_url="https://github.com/owner/repo/pull/1",
    )


@pytest.fixture()
def empty_result() -> ReviewResult:
    return ReviewResult(
        verdict="approve",
        summary_sections=[],
        inline_comments=[],
        persona_name="bob",
        pr_url="https://github.com/owner/repo/pull/2",
    )


# ---------------------------------------------------------------------------
# _format_body
# ---------------------------------------------------------------------------


class TestFormatBody:
    """Test ReviewPoster._format_body() markdown generation."""

    def test_format_body_with_sections(
        self, mock_github_client, simple_result
    ):
        poster = ReviewPoster(mock_github_client)
        body = poster._format_body(simple_result)

        # Header with persona name
        assert "Reviewing as alice-bot" in body
        # Verdict badge
        assert "✅ **Approved**" in body
        # Section header
        assert "### 🐛 Bugs" in body
        # Finding with confidence prefix
        assert "🔴 Null pointer risk" in body
        # Confidence legend present when findings exist
        assert "**Confidence:**" in body
        assert _CONFIDENCE_LEGEND in body

    def test_format_body_no_sections(
        self, mock_github_client, empty_result
    ):
        poster = ReviewPoster(mock_github_client)
        body = poster._format_body(empty_result)

        assert "No issues found" in body
        assert "🎉" in body
        # No confidence legend when there are no findings
        assert _CONFIDENCE_LEGEND not in body

    def test_format_body_request_changes_verdict(self, mock_github_client):
        result = ReviewResult(
            verdict="request_changes",
            summary_sections=[
                CategorySection(
                    emoji="🔒",
                    title="Security",
                    findings=[Finding(text="SQL injection", confidence="high")],
                ),
            ],
            inline_comments=[],
            persona_name="carol",
            pr_url="https://github.com/owner/repo/pull/3",
        )
        poster = ReviewPoster(mock_github_client)
        body = poster._format_body(result)

        assert "🔴 **Changes Requested**" in body

    def test_format_body_comment_verdict(self, mock_github_client):
        result = ReviewResult(
            verdict="comment",
            summary_sections=[
                CategorySection(
                    emoji="💅",
                    title="Style",
                    findings=[Finding(text="Naming", confidence="low")],
                ),
            ],
            inline_comments=[],
            persona_name="dave",
            pr_url="https://github.com/owner/repo/pull/4",
        )
        poster = ReviewPoster(mock_github_client)
        body = poster._format_body(result)

        assert "💬 **Comments**" in body
        # Low confidence finding uses white circle
        assert "⚪ Naming" in body

    def test_format_body_medium_confidence(self, mock_github_client):
        result = ReviewResult(
            verdict="approve",
            summary_sections=[
                CategorySection(
                    emoji="⚡",
                    title="Performance",
                    findings=[Finding(text="Slow query", confidence="medium")],
                ),
            ],
            inline_comments=[],
            persona_name="eve",
            pr_url="https://github.com/owner/repo/pull/5",
        )
        poster = ReviewPoster(mock_github_client)
        body = poster._format_body(result)

        assert "🟡 Slow query" in body


# ---------------------------------------------------------------------------
# _format_inline_body
# ---------------------------------------------------------------------------


class TestFormatInlineBody:
    """Test ReviewPoster._format_inline_body() confidence prefix."""

    def test_high_confidence_prefix(self):
        ic = InlineComment(
            file="f.py", line=1, body="Bug here", confidence="high"
        )
        assert ReviewPoster._format_inline_body(ic) == "🔴 Bug here"

    def test_medium_confidence_prefix(self):
        ic = InlineComment(
            file="f.py", line=1, body="Maybe an issue", confidence="medium"
        )
        assert ReviewPoster._format_inline_body(ic) == "🟡 Maybe an issue"

    def test_low_confidence_prefix(self):
        ic = InlineComment(
            file="f.py", line=1, body="Nit", confidence="low"
        )
        assert ReviewPoster._format_inline_body(ic) == "⚪ Nit"

    def test_unknown_confidence_defaults_to_medium(self):
        ic = InlineComment(
            file="f.py", line=1, body="Something", confidence="unknown"
        )
        # Unknown confidence falls back to medium emoji (🟡)
        assert ReviewPoster._format_inline_body(ic) == "🟡 Something"


# ---------------------------------------------------------------------------
# post()
# ---------------------------------------------------------------------------


class TestPost:
    """Test ReviewPoster.post() GitHub API interaction."""

    @pytest.mark.asyncio
    async def test_post_calls_post_review(
        self, mock_github_client, simple_result
    ):
        poster = ReviewPoster(mock_github_client)
        response = await poster.post("owner", "repo", 1, simple_result)

        mock_github_client.post_review.assert_called_once()
        call_kwargs = mock_github_client.post_review.call_args
        assert call_kwargs.kwargs["owner"] == "owner"
        assert call_kwargs.kwargs["repo"] == "repo"
        assert call_kwargs.kwargs["pr_number"] == 1
        assert call_kwargs.kwargs["event"] == "APPROVE"
        assert call_kwargs.kwargs["comments"] is not None
        assert len(call_kwargs.kwargs["comments"]) == 1
        assert call_kwargs.kwargs["comments"][0].path == "src/main.py"
        assert call_kwargs.kwargs["comments"][0].line == 42
        assert response == {"id": 999}

    @pytest.mark.asyncio
    async def test_post_no_inline_comments(
        self, mock_github_client, empty_result
    ):
        poster = ReviewPoster(mock_github_client)
        await poster.post("owner", "repo", 2, empty_result)

        call_kwargs = mock_github_client.post_review.call_args
        assert call_kwargs.kwargs["comments"] is None

    @pytest.mark.asyncio
    async def test_post_falls_back_to_comment_on_review_failure(
        self, mock_github_client, simple_result
    ):
        mock_github_client.post_review = AsyncMock(
            side_effect=Exception("review failed")
        )
        poster = ReviewPoster(mock_github_client)
        response = await poster.post("owner", "repo", 1, simple_result)

        mock_github_client.post_comment.assert_called_once()
        assert response == {"id": 1000}

    @pytest.mark.asyncio
    async def test_post_raises_when_both_fail(
        self, mock_github_client, simple_result
    ):
        mock_github_client.post_review = AsyncMock(
            side_effect=Exception("review failed")
        )
        mock_github_client.post_comment = AsyncMock(
            side_effect=Exception("comment failed")
        )
        poster = ReviewPoster(mock_github_client)

        with pytest.raises(Exception, match="review failed"):
            await poster.post("owner", "repo", 1, simple_result)

    @pytest.mark.asyncio
    async def test_post_maps_verdict_to_event(self, mock_github_client):
        for verdict, expected_event in _VERDICT_TO_EVENT.items():
            mock_github_client.post_review.reset_mock()
            result = ReviewResult(
                verdict=verdict,
                summary_sections=[],
                inline_comments=[],
                persona_name="tester",
                pr_url="https://github.com/o/r/pull/1",
            )
            poster = ReviewPoster(mock_github_client)
            await poster.post("o", "r", 1, result)

            call_kwargs = mock_github_client.post_review.call_args
            assert call_kwargs.kwargs["event"] == expected_event


# ---------------------------------------------------------------------------
# _track_posted_comments
# ---------------------------------------------------------------------------


class TestTrackPostedComments:
    """Test ReviewPoster._track_posted_comments() feedback tracking."""

    @pytest.mark.asyncio
    async def test_track_calls_feedback_store(
        self, mock_github_client, mock_feedback_store
    ):
        """Each API comment should be tracked via feedback_store."""
        mock_github_client.get_review_comments = AsyncMock(
            return_value=[
                {
                    "id": 100,
                    "path": "src/main.py",
                    "line": 42,
                    "original_line": 42,
                    "body": "🔴 Bug here",
                },
            ]
        )
        result = ReviewResult(
            verdict="comment",
            summary_sections=[],
            inline_comments=[
                InlineComment(
                    file="src/main.py",
                    line=42,
                    body="Bug here",
                    confidence="high",
                ),
            ],
            persona_name="alice",
            pr_url="https://github.com/owner/repo/pull/1",
        )

        poster = ReviewPoster(mock_github_client, feedback_store=mock_feedback_store)
        await poster._track_posted_comments(
            owner="owner",
            repo="repo",
            pr_number=1,
            result=result,
            response={"id": 999},
            pr_author="bob",
        )

        mock_feedback_store.track_posted_comment.assert_called_once()
        call_kwargs = mock_feedback_store.track_posted_comment.call_args
        assert call_kwargs.kwargs["comment_id"] == 100
        assert call_kwargs.kwargs["review_id"] == "999"
        assert call_kwargs.kwargs["persona_name"] == "alice"
        assert call_kwargs.kwargs["repo"] == "owner/repo"
        assert call_kwargs.kwargs["pr_number"] == 1
        assert call_kwargs.kwargs["file_path"] == "src/main.py"
        assert call_kwargs.kwargs["line_number"] == 42
        assert call_kwargs.kwargs["pr_author"] == "bob"

    @pytest.mark.asyncio
    async def test_track_returns_early_without_feedback_store(
        self, mock_github_client
    ):
        """When no feedback_store is set, tracking should return immediately."""
        poster = ReviewPoster(mock_github_client, feedback_store=None)
        result = ReviewResult(
            verdict="approve",
            summary_sections=[],
            inline_comments=[
                InlineComment(file="f.py", line=1, body="x", confidence="high"),
            ],
            persona_name="alice",
            pr_url="https://github.com/o/r/pull/1",
        )
        # Should not raise
        await poster._track_posted_comments(
            owner="o",
            repo="r",
            pr_number=1,
            result=result,
            response={"id": 1},
            pr_author="",
        )
        # get_review_comments should NOT have been called
        mock_github_client.get_review_comments.assert_not_called()

    @pytest.mark.asyncio
    async def test_track_returns_early_without_inline_comments(
        self, mock_github_client, mock_feedback_store
    ):
        """When there are no inline comments, tracking should skip."""
        poster = ReviewPoster(mock_github_client, feedback_store=mock_feedback_store)
        result = ReviewResult(
            verdict="approve",
            summary_sections=[],
            inline_comments=[],
            persona_name="alice",
            pr_url="https://github.com/o/r/pull/1",
        )
        await poster._track_posted_comments(
            owner="o",
            repo="r",
            pr_number=1,
            result=result,
            response={"id": 1},
            pr_author="",
        )
        mock_github_client.get_review_comments.assert_not_called()
        mock_feedback_store.track_posted_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_track_returns_early_without_review_id(
        self, mock_github_client, mock_feedback_store
    ):
        """When response has no id, tracking should skip."""
        poster = ReviewPoster(mock_github_client, feedback_store=mock_feedback_store)
        result = ReviewResult(
            verdict="approve",
            summary_sections=[],
            inline_comments=[
                InlineComment(file="f.py", line=1, body="x"),
            ],
            persona_name="alice",
            pr_url="https://github.com/o/r/pull/1",
        )
        await poster._track_posted_comments(
            owner="o",
            repo="r",
            pr_number=1,
            result=result,
            response={},  # No id
            pr_author="",
        )
        mock_github_client.get_review_comments.assert_not_called()

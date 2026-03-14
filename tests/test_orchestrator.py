"""Tests for review_bot.review.orchestrator — end-to-end review pipeline."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from review_bot.github.api import PullRequestFile
from review_bot.review.formatter import ReviewResult
from review_bot.review.orchestrator import LARGE_PR_FILE_THRESHOLD, ReviewOrchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_json(verdict: str = "approve") -> str:
    return json.dumps({
        "verdict": verdict,
        "summary_sections": [
            {"emoji": "🧪", "title": "Testing", "findings": ["Looks good"]},
        ],
        "inline_comments": [],
    })


# ---------------------------------------------------------------------------
# End-to-end Review Flow
# ---------------------------------------------------------------------------


class TestRunReview:
    """Test the full orchestrated review pipeline with mocked dependencies."""

    @pytest.mark.asyncio
    async def test_standard_review_flow(
        self, mock_github_client, sample_persona, persona_store
    ):
        # Persist persona so store.load works
        persona_store.save(sample_persona)

        with (
            patch.object(
                ReviewOrchestrator, "__init__", lambda self, *a, **kw: None
            ),
        ):
            orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
            orch._github = mock_github_client
            orch._persona_store = persona_store
            orch._db_engine = None
            orch._scanner = MagicMock()
            orch._scanner.scan = AsyncMock(return_value=MagicMock(
                languages=["python"], frameworks=["fastapi"],
            ))
            orch._prompt_builder = MagicMock()
            orch._prompt_builder.build = MagicMock(return_value="prompt text")
            orch._reviewer = MagicMock()
            orch._reviewer.review = AsyncMock(return_value=_llm_json("approve"))
            orch._formatter = MagicMock()
            mock_result = ReviewResult(
                verdict="approve",
                summary_sections=[],
                inline_comments=[],
                persona_name="alice",
                pr_url="https://github.com/owner/repo/pull/42",
            )
            orch._formatter.format = MagicMock(return_value=mock_result)
            orch._poster = MagicMock()
            orch._poster.post = AsyncMock()

            result = await orch.run_review("owner", "repo", 42, "alice")

        assert result.verdict == "approve"
        assert result.persona_name == "alice"
        mock_github_client.get_pull_request.assert_called_once()
        mock_github_client.get_pull_request_files.assert_called_once()
        orch._reviewer.review.assert_called_once()
        orch._poster.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_poster_failure_does_not_raise(
        self, mock_github_client, sample_persona, persona_store
    ):
        """If posting to GitHub fails, the review should still return a result."""
        persona_store.save(sample_persona)

        with patch.object(
            ReviewOrchestrator, "__init__", lambda self, *a, **kw: None
        ):
            orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
            orch._github = mock_github_client
            orch._persona_store = persona_store
            orch._db_engine = None
            orch._scanner = MagicMock()
            orch._scanner.scan = AsyncMock(return_value=MagicMock(
                languages=[], frameworks=[],
            ))
            orch._prompt_builder = MagicMock()
            orch._prompt_builder.build = MagicMock(return_value="prompt")
            orch._reviewer = MagicMock()
            orch._reviewer.review = AsyncMock(return_value=_llm_json())
            orch._formatter = MagicMock()
            mock_result = ReviewResult(
                verdict="approve",
                summary_sections=[],
                inline_comments=[],
                persona_name="alice",
                pr_url="https://github.com/owner/repo/pull/42",
            )
            orch._formatter.format = MagicMock(return_value=mock_result)
            orch._poster = MagicMock()
            orch._poster.post = AsyncMock(side_effect=Exception("GitHub API error"))

            result = await orch.run_review("owner", "repo", 42, "alice")

        assert result.verdict == "approve"


# ---------------------------------------------------------------------------
# Large PR Handling
# ---------------------------------------------------------------------------


class TestLargePRHandling:
    """Test that PRs exceeding the file threshold get summary-only reviews."""

    @pytest.mark.asyncio
    async def test_large_pr_posts_summary_comment(
        self, mock_github_client, sample_persona, persona_store
    ):
        persona_store.save(sample_persona)

        # Generate file list exceeding threshold
        large_files = [
            PullRequestFile(
                filename=f"src/file_{i}.py",
                status="added" if i % 3 == 0 else "modified",
                additions=10,
                deletions=2,
            )
            for i in range(LARGE_PR_FILE_THRESHOLD + 50)
        ]
        mock_github_client.get_pull_request_files = AsyncMock(return_value=large_files)

        with patch.object(
            ReviewOrchestrator, "__init__", lambda self, *a, **kw: None
        ):
            orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
            orch._github = mock_github_client
            orch._persona_store = persona_store
            orch._db_engine = None
            orch._scanner = MagicMock()
            orch._prompt_builder = MagicMock()
            orch._reviewer = MagicMock()
            orch._formatter = MagicMock()
            orch._poster = MagicMock()

            result = await orch.run_review("owner", "repo", 42, "alice")

        assert result.verdict == "comment"
        assert result.persona_name == "alice"
        mock_github_client.post_comment.assert_called_once()
        # LLM reviewer should NOT have been called for large PRs
        orch._reviewer.review.assert_not_called()


# ---------------------------------------------------------------------------
# URL Parsing
# ---------------------------------------------------------------------------


class TestParsePRUrl:
    """Test PR URL parsing utility."""

    def test_valid_url(self):
        owner, repo, num = ReviewOrchestrator._parse_pr_url(
            "https://github.com/owner/repo/pull/42"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert num == 42

    def test_http_url(self):
        owner, repo, num = ReviewOrchestrator._parse_pr_url(
            "http://github.com/acme/lib/pull/7"
        )
        assert owner == "acme"
        assert repo == "lib"
        assert num == 7

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            ReviewOrchestrator._parse_pr_url("https://gitlab.com/x/y/merge_requests/1")

    def test_missing_number_raises(self):
        with pytest.raises(ValueError):
            ReviewOrchestrator._parse_pr_url("https://github.com/owner/repo/pull/")


# ---------------------------------------------------------------------------
# Persona Loading
# ---------------------------------------------------------------------------


class TestPersonaLoading:
    """Test that orchestrator correctly loads persona from store."""

    @pytest.mark.asyncio
    async def test_missing_persona_raises(
        self, mock_github_client, persona_store
    ):
        with patch.object(
            ReviewOrchestrator, "__init__", lambda self, *a, **kw: None
        ):
            orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
            orch._github = mock_github_client
            orch._persona_store = persona_store
            orch._db_engine = None
            orch._scanner = MagicMock()
            orch._prompt_builder = MagicMock()
            orch._reviewer = MagicMock()
            orch._formatter = MagicMock()
            orch._poster = MagicMock()

            with pytest.raises(FileNotFoundError):
                await orch.run_review("owner", "repo", 42, "nonexistent")

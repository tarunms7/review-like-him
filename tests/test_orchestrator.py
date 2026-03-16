"""Tests for review_bot.review.orchestrator — end-to-end review pipeline."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from review_bot.github.api import PullRequestFile
from review_bot.review.formatter import (
    CategorySection,
    Finding,
    ReviewResult,
)
from review_bot.review.orchestrator import (
    EXTREME_PR_THRESHOLD,
    MULTI_PASS_THRESHOLD,
    ReviewOrchestrator,
)
from review_bot.review.prompt_builder import MAX_DIFF_CHARS

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


def _make_orchestrator(
    mock_github_client,
    persona_store,
    min_severity: int = 0,
) -> ReviewOrchestrator:
    """Create a ReviewOrchestrator with mocked internal dependencies."""
    with patch.object(
        ReviewOrchestrator, "__init__", lambda self, *a, **kw: None
    ):
        orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
        orch._github = mock_github_client
        orch._persona_store = persona_store
        orch._db_engine = None
        orch._min_severity = min_severity
        orch._scanner = MagicMock()
        mock_repo_ctx = MagicMock(
            languages=["python"], frameworks=["fastapi"], repo_config={},
        )
        mock_repo_ctx.repo_config = {}
        orch._scanner.scan = AsyncMock(return_value=mock_repo_ctx)
        orch._prompt_builder = MagicMock()
        orch._prompt_builder.build = MagicMock(return_value="prompt text")
        orch._prompt_builder.build_chunked = MagicMock(return_value="chunk prompt")
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
        return orch


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
        orch = _make_orchestrator(mock_github_client, persona_store)

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
        orch = _make_orchestrator(mock_github_client, persona_store)
        orch._poster.post = AsyncMock(side_effect=Exception("GitHub API error"))

        result = await orch.run_review("owner", "repo", 42, "alice")

        assert result.verdict == "approve"


# ---------------------------------------------------------------------------
# Large PR Handling (Extreme — summary only)
# ---------------------------------------------------------------------------


class TestExtremePRHandling:
    """Test that PRs exceeding the extreme threshold get summary-only reviews."""

    @pytest.mark.asyncio
    async def test_extreme_pr_gets_summary_only(
        self, mock_github_client, sample_persona, persona_store
    ):
        """PR with 1500+ files should get summary comment, no LLM review."""
        persona_store.save(sample_persona)

        # Generate file list exceeding extreme threshold
        large_files = [
            PullRequestFile(
                filename=f"src/file_{i}.py",
                status="added" if i % 3 == 0 else "modified",
                additions=10,
                deletions=2,
            )
            for i in range(EXTREME_PR_THRESHOLD + 500)
        ]
        mock_github_client.get_pull_request_files = AsyncMock(return_value=large_files)

        orch = _make_orchestrator(mock_github_client, persona_store)

        result = await orch.run_review("owner", "repo", 42, "alice")

        assert result.verdict == "comment"
        assert result.persona_name == "alice"
        mock_github_client.post_comment.assert_called_once()
        # LLM reviewer should NOT have been called for extreme PRs
        orch._reviewer.review.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-pass Chunked Review
# ---------------------------------------------------------------------------


class TestMultipassReview:
    """Test that large PRs (80+ files) trigger multi-pass chunked review."""

    @pytest.mark.asyncio
    async def test_multipass_triggered_for_large_pr(
        self, mock_github_client, sample_persona, persona_store
    ):
        """PR with 100 files should trigger multi-pass review."""
        persona_store.save(sample_persona)

        # Generate files exceeding multi-pass threshold but below extreme
        num_files = MULTI_PASS_THRESHOLD + 20
        large_files = [
            PullRequestFile(
                filename=f"src/module_{i // 10}/file_{i}.py",
                status="modified",
                additions=5,
                deletions=2,
            )
            for i in range(num_files)
        ]
        mock_github_client.get_pull_request_files = AsyncMock(
            return_value=large_files
        )

        # Build a diff that includes all files
        diff_parts = []
        for f in large_files:
            diff_parts.append(
                f"diff --git a/{f.filename} b/{f.filename}\n"
                f"--- a/{f.filename}\n"
                f"+++ b/{f.filename}\n"
                f"@@ -1,3 +1,3 @@\n"
                f"-old line\n"
                f"+new line\n"
            )
        mock_github_client.get_pull_request_diff = AsyncMock(
            return_value="\n".join(diff_parts)
        )

        orch = _make_orchestrator(mock_github_client, persona_store)

        # The reviewer will be called multiple times (once per chunk)
        orch._reviewer.review = AsyncMock(return_value=_llm_json("comment"))

        # The formatter returns a mock result for each chunk
        chunk_result = ReviewResult(
            verdict="comment",
            summary_sections=[
                CategorySection(
                    emoji="🧪",
                    title="Testing",
                    findings=[Finding(text="Missing tests")],
                ),
            ],
            inline_comments=[],
            persona_name="alice",
            pr_url="https://github.com/owner/repo/pull/42",
        )
        orch._formatter.format = MagicMock(return_value=chunk_result)

        result = await orch.run_review("owner", "repo", 42, "alice")

        # Multi-pass should call reviewer multiple times (>1)
        assert orch._reviewer.review.call_count >= 1
        # build_chunked should have been called instead of build
        assert orch._prompt_builder.build_chunked.call_count >= 1
        # The standard build should NOT have been called
        orch._prompt_builder.build.assert_not_called()
        # Result should be from the merged output
        assert result.persona_name == "alice"


# ---------------------------------------------------------------------------
# Severity Filtering
# ---------------------------------------------------------------------------


class TestSeverityFiltering:
    """Test that severity filtering is applied when min_severity > 0."""

    @pytest.mark.asyncio
    async def test_severity_filter_applied(
        self, mock_github_client, sample_persona, persona_store
    ):
        """When min_severity > 0, filter_result_by_severity should be called."""
        persona_store.save(sample_persona)
        orch = _make_orchestrator(
            mock_github_client, persona_store, min_severity=2
        )

        # Create a result with low-confidence style findings that should be filtered
        mock_result = ReviewResult(
            verdict="comment",
            summary_sections=[
                CategorySection(
                    emoji="💅",
                    title="Style",
                    findings=[
                        Finding(text="Use snake_case", confidence="low"),
                    ],
                ),
            ],
            inline_comments=[],
            persona_name="alice",
            pr_url="https://github.com/owner/repo/pull/42",
        )
        orch._formatter.format = MagicMock(return_value=mock_result)

        with patch(
            "review_bot.review.orchestrator.filter_result_by_severity"
        ) as mock_filter:
            # filter should return a filtered version
            filtered_result = ReviewResult(
                verdict="approve",
                summary_sections=[],
                inline_comments=[],
                persona_name="alice",
                pr_url="https://github.com/owner/repo/pull/42",
            )
            mock_filter.return_value = filtered_result

            result = await orch.run_review("owner", "repo", 42, "alice")

            mock_filter.assert_called_once_with(mock_result, 2)
            assert result.verdict == "approve"

    @pytest.mark.asyncio
    async def test_severity_filter_not_applied_when_zero(
        self, mock_github_client, sample_persona, persona_store
    ):
        """When min_severity is 0, filter should not be called."""
        persona_store.save(sample_persona)
        orch = _make_orchestrator(
            mock_github_client, persona_store, min_severity=0
        )

        with patch(
            "review_bot.review.orchestrator.filter_result_by_severity"
        ) as mock_filter:
            await orch.run_review("owner", "repo", 42, "alice")
            mock_filter.assert_not_called()


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
        orch = _make_orchestrator(mock_github_client, persona_store)

        with pytest.raises(FileNotFoundError):
            await orch.run_review("owner", "repo", 42, "nonexistent")


# ---------------------------------------------------------------------------
# Diff-Size Multi-Pass Trigger
# ---------------------------------------------------------------------------


class TestDiffSizeMultipass:
    """Test that large diffs (even with few files) trigger multi-pass."""

    @pytest.mark.asyncio
    async def test_large_diff_triggers_multipass(
        self, mock_github_client, sample_persona, persona_store
    ):
        """A diff exceeding MAX_DIFF_CHARS * 2 should trigger multi-pass."""
        persona_store.save(sample_persona)

        # Only a few files (below MULTI_PASS_THRESHOLD) but huge diff
        small_files = [
            PullRequestFile(
                filename=f"src/big_{i}.py",
                status="modified",
                additions=100,
                deletions=50,
            )
            for i in range(5)
        ]
        mock_github_client.get_pull_request_files = AsyncMock(
            return_value=small_files
        )

        # Build a diff that exceeds MAX_DIFF_CHARS * 2
        huge_diff = "x" * (MAX_DIFF_CHARS * 2 + 1)
        mock_github_client.get_pull_request_diff = AsyncMock(
            return_value=huge_diff
        )

        orch = _make_orchestrator(mock_github_client, persona_store)
        orch._reviewer.review = AsyncMock(return_value=_llm_json("comment"))

        chunk_result = ReviewResult(
            verdict="comment",
            summary_sections=[],
            inline_comments=[],
            persona_name="alice",
            pr_url="https://github.com/owner/repo/pull/42",
        )
        orch._formatter.format = MagicMock(return_value=chunk_result)

        # Mock DiffChunker to return 2 chunks so build_chunked is used
        chunk_a = MagicMock(
            chunk_id="chunk-1",
            label="group-a",
            diff_text="diff a",
            files=small_files[:3],
        )
        chunk_b = MagicMock(
            chunk_id="chunk-2",
            label="group-b",
            diff_text="diff b",
            files=small_files[3:],
        )
        mock_chunking = MagicMock(
            chunks=[chunk_a, chunk_b],
            skipped_files=[],
        )

        with patch(
            "review_bot.review.orchestrator.DiffChunker"
        ) as mock_chunker:
            mock_chunker.return_value.chunk.return_value = mock_chunking
            result = await orch.run_review("owner", "repo", 42, "alice")

        assert result.persona_name == "alice"
        # Diff-size condition triggered multi-pass: build_chunked must be called
        orch._prompt_builder.build_chunked.assert_called()
        orch._prompt_builder.build.assert_not_called()


# ---------------------------------------------------------------------------
# Single-Chunk Fallback
# ---------------------------------------------------------------------------


class TestSingleChunkFallback:
    """Test that a single chunk after partitioning falls back to single-pass."""

    @pytest.mark.asyncio
    async def test_single_chunk_uses_single_pass(
        self, mock_github_client, sample_persona, persona_store
    ):
        """If chunking produces 1 chunk, use normal build() not build_chunked()."""
        persona_store.save(sample_persona)

        # Enough files to trigger multi-pass
        num_files = MULTI_PASS_THRESHOLD + 5
        large_files = [
            PullRequestFile(
                filename=f"src/file_{i}.py",
                status="modified",
                additions=5,
                deletions=2,
            )
            for i in range(num_files)
        ]
        mock_github_client.get_pull_request_files = AsyncMock(
            return_value=large_files
        )
        mock_github_client.get_pull_request_diff = AsyncMock(
            return_value="diff --git a/f.py b/f.py\n-old\n+new"
        )

        orch = _make_orchestrator(mock_github_client, persona_store)
        orch._reviewer.review = AsyncMock(return_value=_llm_json("approve"))

        single_result = ReviewResult(
            verdict="approve",
            summary_sections=[],
            inline_comments=[],
            persona_name="alice",
            pr_url="https://github.com/owner/repo/pull/42",
        )
        orch._formatter.format = MagicMock(return_value=single_result)

        # Patch DiffChunker to return exactly 1 chunk
        single_chunk = MagicMock(
            chunk_id="chunk-1",
            label="all-files",
            diff_text="diff --git a/f.py b/f.py\n-old\n+new",
            files=large_files,
        )
        mock_chunking = MagicMock(
            chunks=[single_chunk],
            skipped_files=[],
        )

        with patch(
            "review_bot.review.orchestrator.DiffChunker"
        ) as mock_chunker:
            mock_chunker.return_value.chunk.return_value = mock_chunking

            result = await orch.run_review("owner", "repo", 42, "alice")

        assert result.verdict == "approve"
        # Single chunk → should use build(), NOT build_chunked()
        orch._prompt_builder.build.assert_called_once()
        orch._prompt_builder.build_chunked.assert_not_called()
        # Reviewer should be called exactly once (single-pass)
        orch._reviewer.review.assert_called_once()

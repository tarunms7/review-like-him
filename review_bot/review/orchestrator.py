"""Main review pipeline orchestrating the full review lifecycle."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from review_bot.config.repo_config import RepoConfig, SEVERITY_TO_INT
from review_bot.github.api import GitHubAPIClient
from review_bot.persona.profile import PersonaProfile
from review_bot.persona.store import PersonaStore
from review_bot.review.chunker import DiffChunker
from review_bot.review.formatter import ReviewFormatter, ReviewResult
from review_bot.review.github_poster import ReviewPoster
from review_bot.review.merger import ChunkResultMerger
from review_bot.review.prompt_builder import MAX_DIFF_CHARS, PromptBuilder
from review_bot.review.repo_scanner import RepoContext, RepoScanner
from review_bot.review.reviewer import ClaudeReviewer
from review_bot.review.severity import filter_result_by_severity

logger = logging.getLogger("review-bot")

# PRs with more than this many files trigger multi-pass chunked review
MULTI_PASS_THRESHOLD = 80

# PRs with more than this many files get a summary-only review
EXTREME_PR_THRESHOLD = 1000

# Backward-compatible alias — kept for external consumers
LARGE_PR_FILE_THRESHOLD: int = 500


class ReviewOrchestrator:
    """Main review pipeline: persona → PR → scan → prompt → review → post.

    Ties together persona loading, PR fetching, repo scanning, prompt
    building, LLM review, formatting, and GitHub posting.
    """

    def __init__(
        self,
        github_client: GitHubAPIClient,
        persona_store: PersonaStore,
        db_engine: AsyncEngine | None = None,
        min_severity: int = 0,
    ) -> None:
        self._github = github_client
        self._persona_store = persona_store
        self._db_engine = db_engine
        self._min_severity = min_severity
        self._scanner = RepoScanner(github_client)
        self._prompt_builder = PromptBuilder()
        self._reviewer = ClaudeReviewer()
        self._formatter = ReviewFormatter()
        self._poster = ReviewPoster(github_client)

    async def run_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        persona_name: str,
    ) -> ReviewResult:
        """Run a full review pipeline for a pull request.

        Steps:
        1. Load persona profile
        2. Fetch PR data and diff
        3. Scan repo conventions
        4. Build prompt (or chunk for large PRs)
        5. Execute LLM review
        6. Format output and apply severity filter
        7. Post review to GitHub
        8. Log to database

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            persona_name: Name of the persona to review as.

        Returns:
            Structured ReviewResult.
        """
        start_time = time.monotonic()
        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"

        logger.info(
            "Starting review of %s/%s#%d as '%s'",
            owner,
            repo,
            pr_number,
            persona_name,
        )

        # 1. Load persona
        persona = self._persona_store.load(persona_name)
        logger.info("Loaded persona '%s'", persona.name)

        # 2. Fetch PR data, diff, and files concurrently
        pr_data = await self._github.get_pull_request(owner, repo, pr_number)
        files = await self._github.get_pull_request_files(
            owner,
            repo,
            pr_number,
        )
        diff = await self._github.get_pull_request_diff(
            owner,
            repo,
            pr_number,
        )

        logger.info(
            "Fetched PR #%d: %d files, %d additions, %d deletions",
            pr_number,
            len(files),
            pr_data.get("additions", 0),
            pr_data.get("deletions", 0),
        )

        # Handle extreme PRs (1000+ files) — summary only
        if len(files) > EXTREME_PR_THRESHOLD:
            logger.warning(
                "Extreme PR with %d files, posting summary comment",
                len(files),
            )
            result = await self._handle_extreme_pr(
                owner,
                repo,
                pr_number,
                persona,
                pr_data,
                files,
                pr_url,
            )
            await self._log_review(
                owner,
                repo,
                pr_number,
                pr_url,
                result,
                start_time,
            )
            return result

        # 3. Scan repo conventions
        repo_context = await self._scanner.scan(owner, repo)
        logger.info(
            "Repo context: languages=%s, frameworks=%s",
            repo_context.languages,
            repo_context.frameworks,
        )

        # 3b. Load and resolve per-repo config
        repo_config_loaded = False
        try:
            repo_config = await self._scanner.load_repo_config(owner, repo)
            repo_config = repo_config.resolve_for_persona(persona_name)
            repo_config_loaded = True
        except Exception:
            logger.warning(
                "Failed to load repo config for %s/%s, using defaults",
                owner, repo,
            )
            repo_config = RepoConfig.default()

        if repo_config_loaded:
            logger.info(
                "Repo config: min_severity=%s, max_comments=%d, skip_patterns=%s",
                repo_config.min_severity,
                repo_config.max_comments,
                repo_config.skip_patterns,
            )

        # 3c. Apply skip patterns to filter files and diff
        if repo_config.skip_patterns:
            files = self._filter_files(files, repo_config.skip_patterns)
            diff = self._filter_diff(diff, repo_config.skip_patterns)

        # Determine effective severity: use repo config if loaded, else global
        if repo_config_loaded:
            effective_severity = max(
                SEVERITY_TO_INT.get(repo_config.min_severity, 0),
                self._min_severity,
            )
        else:
            effective_severity = self._min_severity
        effective_severity = max(
            effective_severity,
            0,
        )

        # Handle large PRs (80+ files or huge diffs) — multi-pass chunked review
        if len(files) > MULTI_PASS_THRESHOLD or len(diff) > MAX_DIFF_CHARS * 2:
            logger.info(
                "Large PR with %d files (%d chars diff), using multi-pass review",
                len(files),
                len(diff),
            )
            result = await self._handle_large_pr_multipass(
                owner,
                repo,
                pr_number,
                persona,
                pr_data,
                files,
                diff,
                repo_context,
                pr_url,
                custom_instructions=repo_config.custom_instructions,
            )
        else:
            # 4. Build prompt
            prompt = self._prompt_builder.build(
                persona=persona,
                repo_context=repo_context,
                pr_data=pr_data,
                diff=diff,
                files=files,
                custom_instructions=repo_config.custom_instructions,
            )

            # 5. Execute LLM review
            logger.info("Executing Claude review...")
            raw_output = await self._reviewer.review(prompt)

            # 6. Format output
            result = self._formatter.format(
                raw_output=raw_output,
                persona_name=persona_name,
                pr_url=pr_url,
            )

        # 6b. Apply severity filter if configured
        if effective_severity > 0:
            result = filter_result_by_severity(result, effective_severity)

        # 6c. Apply max comments limit
        result = self._apply_comment_limit(result, repo_config.max_comments)

        # 7. Post review to GitHub
        try:
            await self._poster.post(owner, repo, pr_number, result)
        except Exception as exc:
            logger.error(
                "Failed to post review to GitHub: %s",
                exc,
            )

        # 8. Log to database
        await self._log_review(
            owner,
            repo,
            pr_number,
            pr_url,
            result,
            start_time,
        )

        logger.info(
            "Review complete: verdict=%s, sections=%d, comments=%d",
            result.verdict,
            len(result.summary_sections),
            len(result.inline_comments),
        )

        return result

    async def run_review_from_url(
        self,
        pr_url: str,
        persona_name: str,
    ) -> ReviewResult:
        """Run a review from a full GitHub PR URL.

        Parses the URL to extract owner, repo, and PR number,
        then delegates to run_review.

        Args:
            pr_url: Full GitHub PR URL.
            persona_name: Name of the persona to review as.

        Returns:
            Structured ReviewResult.
        """
        owner, repo, pr_number = self._parse_pr_url(pr_url)
        return await self.run_review(owner, repo, pr_number, persona_name)

    async def _handle_extreme_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        persona: PersonaProfile,
        pr_data: dict,
        files: list,
        pr_url: str,
    ) -> ReviewResult:
        """Handle PRs with 1000+ files by posting a persona-aware summary comment."""
        added = sum(1 for f in files if f.status == "added")
        modified = sum(1 for f in files if f.status == "modified")
        removed = sum(1 for f in files if f.status == "removed")

        tone_note = f" (tone: {persona.tone})" if persona.tone else ""
        summary = (
            f"**{persona.name}**{tone_note} here — this PR has **{len(files)} files**, "
            f"which is too large for a detailed line-by-line review.\n\n"
            f"### File breakdown\n"
            f"- **{added}** files added\n"
            f"- **{modified}** files modified\n"
            f"- **{removed}** files removed\n\n"
            f"Consider breaking this into smaller PRs for better review coverage."
        )

        result = ReviewResult(
            verdict="comment",
            summary_sections=[],
            inline_comments=[],
            persona_name=persona.name,
            pr_url=pr_url,
        )

        try:
            await self._github.post_comment(
                owner,
                repo,
                pr_number,
                summary,
            )
        except Exception as exc:
            logger.error("Failed to post large PR comment: %s", exc)

        return result

    async def _handle_large_pr_multipass(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        persona: PersonaProfile,
        pr_data: dict,
        files: list,
        diff: str,
        repo_context: RepoContext,
        pr_url: str,
        custom_instructions: str = "",
    ) -> ReviewResult:
        """Handle large PRs using multi-pass chunked review.

        Chunks the diff, builds a prompt per chunk, reviews concurrently,
        and merges results.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            persona: Loaded persona profile.
            pr_data: Raw PR data from GitHub API.
            files: List of changed PR files.
            diff: Full unified diff text.
            repo_context: Detected repo conventions.
            pr_url: Full GitHub PR URL.

        Returns:
            Merged ReviewResult from all chunks.
        """
        chunker = DiffChunker()
        chunking_result = chunker.chunk(diff, files)

        if not chunking_result.chunks:
            logger.warning("No reviewable chunks after filtering")
            return ReviewResult(
                verdict="comment",
                summary_sections=[],
                inline_comments=[],
                persona_name=persona.name,
                pr_url=pr_url,
            )

        logger.info(
            "Chunked PR into %d chunks (%d files skipped)",
            len(chunking_result.chunks),
            len(chunking_result.skipped_files),
        )

        # Single chunk after partitioning — skip cross-chunk context,
        # use normal single-pass flow instead.
        if len(chunking_result.chunks) == 1:
            logger.info("Single chunk after partitioning, using single-pass flow")
            prompt = self._prompt_builder.build(
                persona=persona,
                repo_context=repo_context,
                pr_data=pr_data,
                diff=chunking_result.chunks[0].diff_text,
                files=chunking_result.chunks[0].files,
                custom_instructions=custom_instructions,
            )
            raw_output = await self._reviewer.review(prompt)
            return self._formatter.format(
                raw_output=raw_output,
                persona_name=persona.name,
                pr_url=pr_url,
            )

        # Build prompts for each chunk
        prompts: list[str] = []
        for chunk in chunking_result.chunks:
            prompt = self._prompt_builder.build_chunked(
                persona=persona,
                repo_context=repo_context,
                pr_data=pr_data,
                chunk=chunk,
                all_chunks=chunking_result.chunks,
                custom_instructions=custom_instructions,
            )
            prompts.append(prompt)

        # Review all chunks concurrently (max 3 at a time)
        raw_outputs = await self._review_chunks_concurrent(prompts, max_concurrent=3)

        # Format each chunk's output
        chunk_results: list[ReviewResult] = []
        chunk_labels: list[str] = []
        for i, raw_output in enumerate(raw_outputs):
            formatted = self._formatter.format(
                raw_output=raw_output,
                persona_name=persona.name,
                pr_url=pr_url,
            )
            chunk_results.append(formatted)
            chunk_labels.append(chunking_result.chunks[i].label)

        # Merge all chunk results
        merger = ChunkResultMerger()
        return merger.merge(chunk_results, chunk_labels)

    @staticmethod
    def _filter_files(
        files: list,
        skip_patterns: list[str],
    ) -> list:
        """Filter out files matching any skip pattern using fnmatch.

        Args:
            files: List of PullRequestFile objects.
            skip_patterns: Glob patterns to skip.

        Returns:
            Filtered list of files.
        """
        from fnmatch import fnmatch

        return [
            f for f in files
            if not any(fnmatch(f.filename, pat) for pat in skip_patterns)
        ]

    @staticmethod
    def _filter_diff(diff: str, skip_patterns: list[str]) -> str:
        """Remove diff sections for files matching skip patterns.

        Args:
            diff: Full unified diff text.
            skip_patterns: Glob patterns to skip.

        Returns:
            Filtered diff text.
        """
        from fnmatch import fnmatch

        sections: list[str] = []
        current_lines: list[str] = []
        current_file: str | None = None

        for line in diff.split("\n"):
            if line.startswith("diff --git"):
                # Flush previous section
                if current_file is not None and current_lines:
                    if not any(fnmatch(current_file, pat) for pat in skip_patterns):
                        sections.append("\n".join(current_lines))
                # Extract filename from "diff --git a/path b/path"
                parts = line.split(" b/", 1)
                current_file = parts[1] if len(parts) > 1 else ""
                current_lines = [line]
            else:
                current_lines.append(line)

        # Flush last section
        if current_file is not None and current_lines:
            if not any(fnmatch(current_file, pat) for pat in skip_patterns):
                sections.append("\n".join(current_lines))

        return "\n".join(sections)

    @staticmethod
    def _apply_comment_limit(result: ReviewResult, max_comments: int) -> ReviewResult:
        """Truncate inline comments to respect the max_comments limit.

        Args:
            result: The ReviewResult to limit.
            max_comments: Maximum number of inline comments.

        Returns:
            A new ReviewResult with truncated inline_comments if needed.
        """
        if len(result.inline_comments) <= max_comments:
            return result

        return result.model_copy(
            update={"inline_comments": result.inline_comments[:max_comments]}
        )

    async def _review_chunks_concurrent(
        self,
        prompts: list[str],
        max_concurrent: int = 3,
    ) -> list[str]:
        """Review multiple chunks concurrently with a semaphore limit.

        Args:
            prompts: List of prompt strings, one per chunk.
            max_concurrent: Maximum number of concurrent reviews.

        Returns:
            List of raw LLM output strings, in the same order as prompts.
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _review_one(prompt: str) -> str:
            async with semaphore:
                return await self._reviewer.review(prompt)

        tasks = [_review_one(p) for p in prompts]
        return list(await asyncio.gather(*tasks))

    @staticmethod
    def _parse_pr_url(pr_url: str) -> tuple[str, str, int]:
        """Parse a GitHub PR URL into (owner, repo, pr_number).

        Raises:
            ValueError: If the URL format is invalid.
        """
        match = re.match(
            r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)",
            pr_url,
        )
        if not match:
            raise ValueError(f"Invalid GitHub PR URL: {pr_url}")
        return match.group(1), match.group(2), int(match.group(3))

    async def _log_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        pr_url: str,
        result: ReviewResult,
        start_time: float,
    ) -> None:
        """Log review metadata to SQLite if a database engine is configured."""
        if self._db_engine is None:
            return

        duration_ms = int((time.monotonic() - start_time) * 1000)
        comment_count = len(result.inline_comments) + sum(
            len(s.findings) for s in result.summary_sections
        )

        try:
            from sqlalchemy import text

            async with self._db_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO reviews "
                        "(persona_name, repo, pr_number, pr_url, "
                        "verdict, comment_count, created_at, duration_ms) "
                        "VALUES "
                        "(:persona, :repo, :pr, :url, "
                        ":verdict, :comments, :created, :duration)"
                    ),
                    {
                        "persona": result.persona_name,
                        "repo": f"{owner}/{repo}",
                        "pr": pr_number,
                        "url": pr_url,
                        "verdict": result.verdict,
                        "comments": comment_count,
                        "created": datetime.now(tz=UTC).isoformat(),
                        "duration": duration_ms,
                    },
                )
        except Exception as exc:
            logger.warning("Failed to log review to database: %s", exc)

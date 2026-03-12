"""Main review pipeline orchestrating the full review lifecycle."""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from review_bot.github.api import GitHubAPIClient
from review_bot.persona.store import PersonaStore
from review_bot.review.formatter import ReviewFormatter, ReviewResult
from review_bot.review.github_poster import ReviewPoster
from review_bot.review.prompt_builder import PromptBuilder
from review_bot.review.repo_scanner import RepoScanner
from review_bot.review.reviewer import ClaudeReviewer

logger = logging.getLogger("review-bot")

# PRs with more than this many files get a summary-only review
LARGE_PR_FILE_THRESHOLD = 500


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
    ) -> None:
        self._github = github_client
        self._persona_store = persona_store
        self._db_engine = db_engine
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
        4. Build prompt
        5. Execute LLM review
        6. Format output
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

        # Handle very large PRs
        if len(files) > LARGE_PR_FILE_THRESHOLD:
            logger.warning(
                "Large PR with %d files, posting summary comment",
                len(files),
            )
            result = await self._handle_large_pr(
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

        # 4. Build prompt
        prompt = self._prompt_builder.build(
            persona=persona,
            repo_context=repo_context,
            pr_data=pr_data,
            diff=diff,
            files=files,
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

    async def _handle_large_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        persona: object,
        pr_data: dict,
        files: list,
        pr_url: str,
    ) -> ReviewResult:
        """Handle PRs with 500+ files by posting a summary comment."""
        file_summary = (
            f"This PR has {len(files)} files — too large for a "
            f"detailed line-by-line review. Here's a high-level summary:"
        )

        added = sum(1 for f in files if f.status == "added")
        modified = sum(1 for f in files if f.status == "modified")
        removed = sum(1 for f in files if f.status == "removed")

        summary = (
            f"{file_summary}\n\n"
            f"- **{added}** files added\n"
            f"- **{modified}** files modified\n"
            f"- **{removed}** files removed\n\n"
            f"Consider breaking this into smaller PRs for better review."
        )

        result = ReviewResult(
            verdict="comment",
            summary_sections=[],
            inline_comments=[],
            persona_name=persona.name if hasattr(persona, "name") else "",
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

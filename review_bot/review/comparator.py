"""Multi-persona comparison: run a PR through several personas without posting."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from review_bot.github.api import GitHubAPIClient
from review_bot.persona.store import PersonaStore
from review_bot.review.formatter import ReviewFormatter, ReviewResult
from review_bot.review.prompt_builder import PromptBuilder
from review_bot.review.repo_scanner import RepoScanner
from review_bot.review.reviewer import ClaudeReviewer

logger = logging.getLogger("review-bot")

DEFAULT_PER_PERSONA_TIMEOUT: float = 120.0
MAX_CONCURRENT_PERSONAS: int = 3


@dataclass
class ComparisonEntry:
    """A single persona's result within a comparison."""

    persona_name: str
    result: ReviewResult
    duration_ms: int
    error: str | None = None


@dataclass
class ComparisonResult:
    """Aggregated results from comparing multiple personas on a single PR."""

    pr_url: str
    entries: list[ComparisonEntry] = field(default_factory=list)
    total_duration_ms: int = 0


class PersonaComparator:
    """Run a PR through multiple personas and collect results for comparison."""

    def __init__(
        self,
        github_client: GitHubAPIClient,
        persona_store: PersonaStore,
    ) -> None:
        self._github = github_client
        self._persona_store = persona_store
        self._scanner = RepoScanner(github_client)
        self._prompt_builder = PromptBuilder()
        self._reviewer = ClaudeReviewer()
        self._formatter = ReviewFormatter()

    async def compare(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        persona_names: list[str],
        *,
        timeout_per_persona: float = DEFAULT_PER_PERSONA_TIMEOUT,
    ) -> ComparisonResult:
        """Run a PR through multiple personas and return comparison results.

        Fetches PR data once and shares it across all persona reviews.
        Uses a semaphore to limit concurrency.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            persona_names: List of persona names to compare.
            timeout_per_persona: Max seconds per persona review.

        Returns:
            ComparisonResult with an entry per persona.
        """
        start = time.monotonic()
        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"

        # Fetch shared PR data once
        pr_data = await self._github.get_pull_request(owner, repo, pr_number)
        files = await self._github.get_pull_request_files(owner, repo, pr_number)
        diff = await self._github.get_pull_request_diff(owner, repo, pr_number)
        repo_context = await self._scanner.scan(owner, repo)

        sem = asyncio.Semaphore(MAX_CONCURRENT_PERSONAS)

        tasks = [
            self._review_with_persona(
                persona_name=name,
                pr_data=pr_data,
                files=files,
                diff=diff,
                repo_context=repo_context,
                pr_url=pr_url,
                sem=sem,
                timeout=timeout_per_persona,
            )
            for name in persona_names
        ]

        entries = list(await asyncio.gather(*tasks))
        total_ms = int((time.monotonic() - start) * 1000)

        return ComparisonResult(
            pr_url=pr_url,
            entries=entries,
            total_duration_ms=total_ms,
        )

    async def _review_with_persona(
        self,
        persona_name: str,
        pr_data: dict,
        files: list,
        diff: str,
        repo_context,
        pr_url: str,
        sem: asyncio.Semaphore,
        timeout: float,
    ) -> ComparisonEntry:
        """Review a PR as a single persona, with timeout and error handling."""
        start = time.monotonic()
        # Placeholder result for error cases
        empty_result = ReviewResult(
            verdict="comment",
            summary_sections=[],
            inline_comments=[],
            persona_name=persona_name,
            pr_url=pr_url,
        )

        try:
            async with sem:
                persona = self._persona_store.load(persona_name)

                prompt = self._prompt_builder.build(
                    persona=persona,
                    repo_context=repo_context,
                    pr_data=pr_data,
                    diff=diff,
                    files=files,
                )

                raw_output = await asyncio.wait_for(
                    self._reviewer.review(prompt),
                    timeout=timeout,
                )

                result = self._formatter.format(
                    raw_output=raw_output,
                    persona_name=persona_name,
                    pr_url=pr_url,
                )

            duration_ms = int((time.monotonic() - start) * 1000)
            return ComparisonEntry(
                persona_name=persona_name,
                result=result,
                duration_ms=duration_ms,
            )

        except TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning("Persona '%s' timed out after %.1fs", persona_name, timeout)
            return ComparisonEntry(
                persona_name=persona_name,
                result=empty_result,
                duration_ms=duration_ms,
                error=f"Timed out after {timeout}s",
            )

        except FileNotFoundError:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning("Persona '%s' not found", persona_name)
            return ComparisonEntry(
                persona_name=persona_name,
                result=empty_result,
                duration_ms=duration_ms,
                error=f"Persona '{persona_name}' not found",
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.exception("Error reviewing as '%s'", persona_name)
            return ComparisonEntry(
                persona_name=persona_name,
                result=empty_result,
                duration_ms=duration_ms,
                error=str(exc),
            )

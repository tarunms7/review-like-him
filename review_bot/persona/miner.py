"""GitHub review history miner that fetches review comments, verdicts, and threads."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Regex pattern for validating GitHub API repository URLs
_GITHUB_REPO_URL_RE = re.compile(
    r"^https://api\.github\.com/repos/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"
)

# GitHub API constants
GITHUB_API = "https://api.github.com"
PER_PAGE = 100
RATE_LIMIT_BUFFER = 5  # seconds buffer when sleeping for rate limits
MAX_RATE_LIMIT_SLEEP = 300  # maximum seconds to sleep for rate limit backoff


@dataclasses.dataclass
class MiningProgress:
    """Granular progress event emitted during review mining."""

    phase: str = "discovering_repos"
    repo: str | None = None
    repo_index: int | None = None
    repo_total: int | None = None
    detail: str = ""
    items_found: int = 0
    page: int | None = None
    pr_number: int | None = None
    pr_index: int | None = None
    pr_total: int | None = None


# Type alias for the progress callback
ProgressCallback = Callable[[MiningProgress], None] | None


class GitHubReviewMiner:
    """Mines GitHub review history for a given user across accessible repos."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client
        self._etags: dict[str, str] = {}

    async def _request(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Make a GitHub API request with ETag support, rate limit respect, and retry on 429."""
        headers: dict[str, str] = {}
        cache_key = f"{url}?{params}" if params else url

        if cache_key in self._etags:
            headers["If-None-Match"] = self._etags[cache_key]

        max_retries = 5
        backoff = 1.0

        for attempt in range(max_retries):
            response = await self._client.get(url, params=params, headers=headers)

            # Store ETag for conditional requests
            if "ETag" in response.headers:
                self._etags[cache_key] = response.headers["ETag"]

            # Handle 429 rate limit responses with exponential backoff
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else backoff
                except (ValueError, TypeError):
                    wait = backoff
                wait = min(wait, MAX_RATE_LIMIT_SLEEP)
                logger.warning(
                    "Rate limited (429) on attempt %d/%d, retrying in %.1fs",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, MAX_RATE_LIMIT_SLEEP)
                continue

            # Respect approaching rate limits
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining is not None:
                try:
                    remaining_int = int(remaining)
                except (ValueError, TypeError):
                    remaining_int = None
                    logger.warning(
                        "Non-numeric X-RateLimit-Remaining header: %s", remaining
                    )
                if remaining_int is not None and remaining_int <= 1:
                    try:
                        reset_at = int(response.headers.get("X-RateLimit-Reset", "0"))
                    except (ValueError, TypeError):
                        reset_at = 0
                    import time

                    sleep_seconds = min(
                        max(reset_at - int(time.time()) + RATE_LIMIT_BUFFER, 1),
                        MAX_RATE_LIMIT_SLEEP,
                    )
                    logger.warning("Rate limit near exhaustion, sleeping %d seconds", sleep_seconds)
                    await asyncio.sleep(sleep_seconds)

            try:
                from review_bot.github.rate_limits import RateLimitTracker

                RateLimitTracker().update_from_response(url, response.headers)
            except ImportError:
                pass
            return response

        # Final attempt exhausted — return last response and let caller handle
        logger.error("Rate limit retries exhausted after %d attempts for %s", max_retries, url)
        return response

    async def _paginate(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        on_page: Callable[[int, list[dict]], None] | None = None,
    ) -> list[dict]:
        """Fetch all pages from a paginated GitHub API endpoint.

        Args:
            url: GitHub API endpoint URL.
            params: Query parameters for the request.
            on_page: Optional callback called with (page_number, page_data) after
                each successful page fetch. The caller uses this to construct and
                emit MiningProgress events with the appropriate phase.
        """
        params = dict(params or {})
        params.setdefault("per_page", PER_PAGE)
        page = 1
        results: list[dict] = []

        while True:
            params["page"] = page
            response = await self._request(url, params)

            if response.status_code == 304:
                break
            response.raise_for_status()

            data = response.json()
            if not data:
                break

            results.extend(data)

            if on_page is not None:
                on_page(page, data)

            # Check for next page via Link header
            link = response.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            page += 1

        return results

    async def _discover_reviewed_prs(
        self,
        username: str,
        progress_callback: ProgressCallback = None,
    ) -> dict[str, list[int]]:
        """Discover repos and specific PR numbers the user has reviewed.

        Uses the GitHub Search API to find PRs reviewed by the user,
        then groups PR numbers by repo. This avoids fetching all PRs
        in a repo and only targets the ones the user actually reviewed.

        Returns:
            Dict mapping repo full_name to list of PR numbers reviewed.
        """
        if progress_callback:
            progress_callback(MiningProgress(
                phase="discovering_repos",
                detail="Searching for reviewed PRs...",
            ))

        # Paginate through all search results
        search_url = f"{GITHUB_API}/search/issues"
        params = {
            "q": f"type:pr reviewed-by:{username}",
            "sort": "updated",
            "order": "desc",
            "per_page": PER_PAGE,
        }

        repos_to_prs: dict[str, list[int]] = {}
        page = 1

        while True:
            params["page"] = page
            response = await self._request(search_url, params)
            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                repo_url = item.get("repository_url", "")
                pr_number = item.get("number")
                if repo_url and pr_number:
                    if not _GITHUB_REPO_URL_RE.match(repo_url):
                        logger.warning(
                            "Skipping unexpected repository URL format: %s", repo_url
                        )
                        continue
                    parts = repo_url.rstrip("/").split("/")
                    if len(parts) >= 2:
                        full_name = f"{parts[-2]}/{parts[-1]}"
                        if full_name not in repos_to_prs:
                            repos_to_prs[full_name] = []
                        if pr_number not in repos_to_prs[full_name]:
                            repos_to_prs[full_name].append(pr_number)

            # GitHub Search API caps at 1000 results total
            total_count = data.get("total_count", 0)
            if page * PER_PAGE >= min(total_count, 1000):
                break
            page += 1

        if progress_callback:
            total_prs = sum(len(prs) for prs in repos_to_prs.values())
            progress_callback(MiningProgress(
                phase="discovering_repos",
                detail=f"Found {total_prs} reviewed PRs across {len(repos_to_prs)} repos",
                repo_total=len(repos_to_prs),
            ))

        return repos_to_prs

    async def _fetch_reviews_for_repo(
        self,
        repo_full_name: str,
        username: str,
        pr_numbers: list[int],
        repo_index: int | None = None,
        repo_total: int | None = None,
        items_found: int = 0,
        progress_callback: ProgressCallback = None,
    ) -> list[dict]:
        """Fetch review comments and verdicts by a user for specific PRs.

        Only fetches data for the PR numbers the user actually reviewed,
        instead of scanning all PRs in the repo.
        """
        results: list[dict] = []
        pr_total = len(pr_numbers)

        for pr_idx, pr_number in enumerate(pr_numbers):
            if progress_callback:
                progress_callback(MiningProgress(
                    phase="fetching_pr_reviews",
                    repo=repo_full_name,
                    repo_index=repo_index,
                    repo_total=repo_total,
                    detail=f"Fetching reviews for PR #{pr_number} ({pr_idx + 1}/{pr_total})",
                    items_found=items_found + len(results),
                    pr_number=pr_number,
                    pr_index=pr_idx + 1,
                    pr_total=pr_total,
                ))

            # Fetch inline review comments for this PR
            comments_url = (
                f"{GITHUB_API}/repos/{repo_full_name}/pulls/{pr_number}/comments"
            )
            comments = await self._paginate(comments_url)

            for comment in comments:
                user = comment.get("user", {})
                if user and user.get("login", "").lower() == username.lower():
                    line = comment.get("original_line") or comment.get("line")
                    if line is None:
                        line = 0
                    results.append({
                        "repo": repo_full_name,
                        "pr_number": pr_number,
                        "comment_body": comment.get("body", ""),
                        "verdict": None,
                        "created_at": comment.get("created_at", ""),
                        "file_path": comment.get("path", ""),
                        "line": line,
                    })

            # Fetch review verdicts (APPROVE, REQUEST_CHANGES, etc.)
            reviews_url = (
                f"{GITHUB_API}/repos/{repo_full_name}/pulls/{pr_number}/reviews"
            )
            reviews = await self._paginate(reviews_url)

            for review in reviews:
                user = review.get("user", {})
                if user and user.get("login", "").lower() == username.lower():
                    state = review.get("state", "")
                    body = review.get("body", "")
                    if state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED") and body:
                        results.append({
                            "repo": repo_full_name,
                            "pr_number": pr_number,
                            "comment_body": body,
                            "verdict": state,
                            "created_at": review.get("submitted_at", ""),
                            "file_path": None,
                            "line": None,
                        })

        return results

    async def mine_user_reviews(
        self,
        username: str,
        progress_callback: Callable[[MiningProgress], None] | None = None,
    ) -> list[dict]:
        """Mine all accessible review data for a GitHub user.

        Args:
            username: GitHub username to mine reviews for.
            progress_callback: Optional callback invoked with a MiningProgress
                dataclass on every progress event. Defaults to None. When None,
                behavior is unchanged from before (no progress reporting).

        Returns:
            List of review comment dicts with repo, pr_number, comment_body,
            verdict, created_at, file_path, and line fields.
        """
        logger.info("Discovering repos with reviews from %s", username)

        repos_to_prs = await self._discover_reviewed_prs(username, progress_callback)
        total = len(repos_to_prs)
        total_prs = sum(len(prs) for prs in repos_to_prs.values())
        logger.info(
            "Found %d reviewed PRs across %d repos for %s", total_prs, total, username
        )

        all_reviews: list[dict] = []
        for idx, (repo_name, pr_numbers) in enumerate(repos_to_prs.items()):
            logger.info(
                "Mining %d reviewed PRs from %s (%d/%d)",
                len(pr_numbers), repo_name, idx + 1, total,
            )
            try:
                reviews = await self._fetch_reviews_for_repo(
                    repo_name,
                    username,
                    pr_numbers=pr_numbers,
                    repo_index=idx + 1,
                    repo_total=total,
                    items_found=len(all_reviews),
                    progress_callback=progress_callback,
                )
                all_reviews.extend(reviews)
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "Failed to fetch reviews from %s: %s",
                    repo_name,
                    exc.response.status_code,
                )

        if progress_callback:
            progress_callback(MiningProgress(
                phase="done",
                detail=f"Done: {len(all_reviews)} review comments across {total} repos",
                items_found=len(all_reviews),
                repo_total=total,
            ))

        logger.info("Mined %d total review comments for %s", len(all_reviews), username)
        return all_reviews

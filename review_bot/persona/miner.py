"""GitHub review history miner that fetches review comments, verdicts, and threads."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# GitHub API constants
GITHUB_API = "https://api.github.com"
PER_PAGE = 100
RATE_LIMIT_BUFFER = 5  # seconds buffer when sleeping for rate limits


class GitHubReviewMiner:
    """Mines GitHub review history for a given user across accessible repos."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client
        self._etags: dict[str, str] = {}

    async def _request(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Make a GitHub API request with ETag support and rate limit respect."""
        headers: dict[str, str] = {}
        cache_key = f"{url}?{params}" if params else url

        if cache_key in self._etags:
            headers["If-None-Match"] = self._etags[cache_key]

        response = await self._client.get(url, params=params, headers=headers)

        # Store ETag for conditional requests
        if "ETag" in response.headers:
            self._etags[cache_key] = response.headers["ETag"]

        # Respect rate limits
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) <= 1:
            reset_at = int(response.headers.get("X-RateLimit-Reset", "0"))
            import time

            sleep_seconds = max(reset_at - int(time.time()) + RATE_LIMIT_BUFFER, 1)
            logger.warning("Rate limit near exhaustion, sleeping %d seconds", sleep_seconds)
            await asyncio.sleep(sleep_seconds)

        return response

    async def _paginate(self, url: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Fetch all pages from a paginated GitHub API endpoint."""
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

            # Check for next page via Link header
            link = response.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            page += 1

        return results

    async def _fetch_user_repos(self, username: str) -> list[dict]:
        """Fetch repos a user has contributed to via their events."""
        # Search for PRs the user has reviewed
        search_url = f"{GITHUB_API}/search/issues"
        params = {
            "q": f"type:pr reviewed-by:{username}",
            "sort": "updated",
            "order": "desc",
            "per_page": PER_PAGE,
        }
        response = await self._request(search_url, params)
        response.raise_for_status()
        data = response.json()

        # Extract unique repos from search results
        repos: dict[str, dict] = {}
        for item in data.get("items", []):
            repo_url = item.get("repository_url", "")
            if repo_url and repo_url not in repos:
                # Extract owner/repo from URL
                parts = repo_url.rstrip("/").split("/")
                if len(parts) >= 2:
                    full_name = f"{parts[-2]}/{parts[-1]}"
                    repos[repo_url] = {"full_name": full_name, "url": repo_url}

        return list(repos.values())

    async def _fetch_reviews_for_repo(
        self,
        repo_full_name: str,
        username: str,
    ) -> list[dict]:
        """Fetch all review comments and verdicts by a user in a given repo."""
        results: list[dict] = []

        # Fetch pull request review comments by the user
        comments_url = f"{GITHUB_API}/repos/{repo_full_name}/pulls/comments"
        all_comments = await self._paginate(comments_url, {"sort": "updated", "direction": "desc"})

        for comment in all_comments:
            user = comment.get("user", {})
            if user and user.get("login", "").lower() == username.lower():
                results.append({
                    "repo": repo_full_name,
                    "pr_number": comment.get("pull_request_url", "").rstrip("/").split("/")[-1],
                    "comment_body": comment.get("body", ""),
                    "verdict": None,  # Comments don't carry verdicts
                    "created_at": comment.get("created_at", ""),
                    "file_path": comment.get("path", ""),
                    "line": comment.get("original_line") or comment.get("line"),
                })

        # Fetch PR reviews (for verdicts like APPROVE, REQUEST_CHANGES)
        prs_url = f"{GITHUB_API}/repos/{repo_full_name}/pulls"
        prs = await self._paginate(prs_url, {"state": "all", "sort": "updated", "direction": "desc"})

        for pr in prs:
            pr_number = pr.get("number")
            reviews_url = f"{GITHUB_API}/repos/{repo_full_name}/pulls/{pr_number}/reviews"
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
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> list[dict]:
        """Mine all accessible review data for a GitHub user.

        Args:
            username: GitHub username to mine reviews for.
            progress_callback: Optional callback(repo_name, current, total) for progress.

        Returns:
            List of review comment dicts with repo, pr_number, comment_body,
            verdict, created_at, file_path, and line fields.
        """
        logger.info("Discovering repos with reviews from %s", username)

        repos = await self._fetch_user_repos(username)
        total = len(repos)
        logger.info("Found %d repos with reviews from %s", total, username)

        all_reviews: list[dict] = []
        for idx, repo_info in enumerate(repos):
            repo_name = repo_info["full_name"]
            if progress_callback:
                progress_callback(repo_name, idx + 1, total)

            logger.info("Mining reviews from %s (%d/%d)", repo_name, idx + 1, total)
            try:
                reviews = await self._fetch_reviews_for_repo(repo_name, username)
                all_reviews.extend(reviews)
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "Failed to fetch reviews from %s: %s",
                    repo_name,
                    exc.response.status_code,
                )

        logger.info("Mined %d total review comments for %s", len(all_reviews), username)
        return all_reviews

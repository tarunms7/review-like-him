"""Async GitHub API v3 client for PR operations, reviews, and repo content."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("review-bot")

GITHUB_API_BASE = "https://api.github.com"

# Rate-limit / retry settings
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0  # seconds


@dataclass
class PullRequestFile:
    """A file changed in a pull request."""

    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None = None


@dataclass
class ReviewComment:
    """An inline comment to be posted as part of a GitHub PR review."""

    path: str
    line: int
    body: str


class GitHubAPIClient:
    """Async GitHub API v3 client with rate-limit handling.

    All methods use exponential backoff for retries on transient errors
    and rate limits.
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Make an HTTP request with exponential backoff on rate limits and errors."""
        backoff = INITIAL_BACKOFF
        last_exc: Exception | None = None
        resp: httpx.Response | None = None

        for attempt in range(MAX_RETRIES):
            try:
                resp = await self._client.request(
                    method, url, headers=headers, json=json, params=params
                )

                # Handle rate limiting
                if resp.status_code == 403 and "rate limit" in resp.text.lower():
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else backoff
                    logger.warning(
                        "Rate limited (attempt %d/%d), waiting %.1fs",
                        attempt + 1,
                        MAX_RETRIES,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    backoff *= 2
                    continue

                # Handle server errors with retry
                if resp.status_code >= 500:
                    logger.warning(
                        "Server error %d (attempt %d/%d), retrying in %.1fs",
                        resp.status_code,
                        attempt + 1,
                        MAX_RETRIES,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue

                resp.raise_for_status()
                try:
                    from review_bot.github.rate_limits import RateLimitTracker

                    RateLimitTracker().update_from_response(url, resp.headers)
                except ImportError:
                    pass
                return resp

            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning(
                    "Transport error (attempt %d/%d): %s",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2

        # resp is always defined after at least one loop iteration, but
        # initialize a fallback for type-safety in case of unexpected paths.
        fallback_resp = httpx.Response(500)
        raise httpx.HTTPStatusError(
            f"Request failed after {MAX_RETRIES} retries",
            request=httpx.Request(method, url),
            response=resp if resp is not None else fallback_resp,
        ) from last_exc

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        """Get pull request data."""
        resp = await self._request(
            "GET", f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        )
        return resp.json()

    async def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Get the unified diff for a pull request."""
        resp = await self._request(
            "GET",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return resp.text

    async def get_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> list[PullRequestFile]:
        """Get the list of files changed in a pull request."""
        resp = await self._request(
            "GET",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files",
        )
        return [
            PullRequestFile(
                filename=f["filename"],
                status=f["status"],
                additions=f["additions"],
                deletions=f["deletions"],
                patch=f.get("patch"),
            )
            for f in resp.json()
        ]

    async def post_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        event: str,
        comments: list[ReviewComment] | None = None,
    ) -> dict:
        """Create a pull request review with optional inline comments.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            body: Review body text.
            event: Review event: APPROVE, REQUEST_CHANGES, or COMMENT.
            comments: Optional inline review comments.
        """
        payload: dict[str, Any] = {"body": body, "event": event}
        if comments:
            payload["comments"] = [
                {"path": c.path, "line": c.line, "body": c.body} for c in comments
            ]

        resp = await self._request(
            "POST",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json=payload,
        )
        return resp.json()

    async def post_comment(self, owner: str, repo: str, pr_number: int, body: str) -> dict:
        """Post a general issue comment on a pull request."""
        resp = await self._request(
            "POST",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json={"body": body},
        )
        return resp.json()

    async def get_user_reviews(
        self, username: str, page: int = 1, per_page: int = 100
    ) -> list[dict]:
        """Get public review activity for a user (via events API).

        Used for persona mining — fetches PullRequestReviewEvent data.
        """
        resp = await self._request(
            "GET",
            f"{GITHUB_API_BASE}/users/{username}/events",
            headers={"Accept": "application/vnd.github+json"},
            params={"page": page, "per_page": per_page},
        )
        return [e for e in resp.json() if e.get("type") == "PullRequestReviewEvent"]

    async def update_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> dict:
        """Update an existing issue comment body.

        Args:
            owner: Repository owner.
            repo: Repository name.
            comment_id: ID of the comment to update.
            body: New comment body text.

        Returns:
            Parsed JSON response dict with id, body, user, created_at, updated_at fields.
        """
        resp = await self._request(
            "PATCH",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return resp.json()

    async def delete_comment(self, owner: str, repo: str, comment_id: int) -> None:
        """Delete an existing issue comment.

        Args:
            owner: Repository owner.
            repo: Repository name.
            comment_id: ID of the comment to delete.
        """
        await self._request(
            "DELETE",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/comments/{comment_id}",
        )

    async def get_comment_reactions(
        self, owner: str, repo: str, comment_id: int
    ) -> list[dict]:
        """Get reactions on a pull request review comment.

        Args:
            owner: Repository owner.
            repo: Repository name.
            comment_id: ID of the pull request review comment.

        Returns:
            List of reaction dicts with id, user, content, and created_at fields.
        """
        resp = await self._request(
            "GET",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions",
            headers={"Accept": "application/vnd.github+json"},
        )
        return resp.json()

    async def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: str
    ) -> list[dict]:
        """Get inline comments posted as part of a specific PR review.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            review_id: ID of the review (as a string).

        Returns:
            List of review comment dicts with id, path, line, original_line, and body fields.
        """
        resp = await self._request(
            "GET",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}/comments",
        )
        return resp.json()

    async def get_repo_contents(self, owner: str, repo: str, path: str) -> dict:
        """Get file contents from a repository."""
        resp = await self._request(
            "GET",
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}",
        )
        return resp.json()

"""Background poller for GitHub reactions on tracked review comments.

Periodically checks tracked comments for new reactions and replies,
converts them to feedback events, and stores them for persona refinement.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from review_bot.github.api import GitHubAPIClient
from review_bot.review.feedback import FeedbackEvent, FeedbackStore

logger = logging.getLogger("review-bot")

# Map GitHub reaction content to feedback signal type
REACTION_FEEDBACK: dict[str, str] = {
    "+1": "positive",
    "heart": "positive",
    "hooray": "positive",
    "rocket": "positive",
    "laugh": "positive",
    "-1": "negative",
    "confused": "confused",
    "eyes": "neutral",
}

# Bot suffixes to detect and ignore bot reactions
_BOT_SUFFIXES = ("[bot]", "-bot")


@dataclass
class ReactionPollResult:
    """Result of polling reactions for a single comment.

    Attributes:
        comment_id: GitHub comment ID.
        new_events: Number of new feedback events recorded.
        total_reactions: Total reactions found on the comment.
    """

    comment_id: int
    new_events: int
    total_reactions: int


def _is_bot_user(username: str) -> bool:
    """Check if a username appears to be a bot account.

    Args:
        username: GitHub username to check.

    Returns:
        True if the username looks like a bot.
    """
    lower = username.lower()
    return any(lower.endswith(suffix) for suffix in _BOT_SUFFIXES)


class FeedbackPoller:
    """Polls GitHub for reactions on tracked review comments.

    Periodically fetches reactions from the GitHub API, diffs them against
    stored reactions, and inserts new feedback events.

    Args:
        github_client: GitHubAPIClient for fetching reactions.
        feedback_store: FeedbackStore for recording feedback events.
        poll_interval: Time between poll cycles.
        max_comment_age: Maximum age of comments to poll.
    """

    def __init__(
        self,
        github_client: GitHubAPIClient,
        feedback_store: FeedbackStore,
        poll_interval: timedelta = timedelta(hours=6),
        max_comment_age: timedelta = timedelta(days=30),
    ) -> None:
        """Initialize with GitHub client and feedback store."""
        self._github_client = github_client
        self._feedback_store = feedback_store
        self._poll_interval = poll_interval
        self._max_comment_age = max_comment_age

    async def poll_reactions_for_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
    ) -> list[dict]:
        """Fetch reactions for a single PR review comment from GitHub.

        Args:
            owner: Repository owner.
            repo: Repository name.
            comment_id: GitHub comment ID.

        Returns:
            List of reaction dicts from the GitHub API.
        """
        try:
            resp = await self._github_client._request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}"
                f"/pulls/comments/{comment_id}/reactions",
                headers={"Accept": "application/vnd.github+json"},
            )
            return resp.json()
        except Exception:
            logger.warning(
                "Failed to fetch reactions for comment %d in %s/%s",
                comment_id, owner, repo,
            )
            return []

    async def poll_all_tracked_comments(self) -> int:
        """Poll all tracked comments, diff against stored, insert new events.

        Returns:
            Count of new feedback events recorded.
        """
        max_age_days = int(self._max_comment_age.total_seconds() / 86400)
        comments = await self._feedback_store.get_tracked_comments(
            max_age_days=max_age_days
        )

        if not comments:
            logger.debug("No tracked comments to poll")
            return 0

        total_new = 0
        for comment in comments:
            repo = comment["repo"]
            parts = repo.split("/", 1)
            if len(parts) != 2:
                logger.warning("Invalid repo format in tracking: %s", repo)
                continue

            owner, repo_name = parts
            comment_id = comment["comment_id"]

            reactions = await self.poll_reactions_for_comment(
                owner, repo_name, comment_id
            )

            # Get existing stored reactions for diffing
            stored = await self._feedback_store.get_stored_reactions(comment_id)
            stored_keys = {
                (r["reactor_username"], r["feedback_type"], r["feedback_source"])
                for r in stored
            }

            for reaction in reactions:
                user = reaction.get("user", {})
                username = user.get("login", "")

                # Skip bot reactions
                if _is_bot_user(username):
                    continue

                content = reaction.get("content", "")
                feedback_type = REACTION_FEEDBACK.get(content)
                if feedback_type is None:
                    continue

                key = (username, feedback_type, "reaction")
                if key in stored_keys:
                    continue

                event = FeedbackEvent(
                    comment_id=comment_id,
                    feedback_type=feedback_type,
                    feedback_source="reaction",
                    reactor_username=username,
                    is_pr_author=False,  # Cannot determine from reaction alone
                )
                await self._feedback_store.record_feedback(event)
                total_new += 1

        logger.info("Polled %d comments, recorded %d new events", len(comments), total_new)
        return total_new

    async def run_poll_loop(self) -> None:
        """Long-running loop that polls for reactions periodically.

        Suitable for asyncio.create_task(). Runs until cancelled.
        """
        logger.info(
            "Starting feedback poll loop (interval=%s)",
            self._poll_interval,
        )
        while True:
            try:
                new_count = await self.poll_all_tracked_comments()
                if new_count > 0:
                    logger.info("Feedback poll cycle: %d new events", new_count)
            except asyncio.CancelledError:
                logger.info("Feedback poll loop cancelled")
                raise
            except Exception:
                logger.exception("Error in feedback poll cycle")

            await asyncio.sleep(self._poll_interval.total_seconds())

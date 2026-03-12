"""Posts structured review results to GitHub as PR reviews."""

from __future__ import annotations

import logging

from review_bot.github.api import GitHubAPIClient, ReviewComment
from review_bot.review.formatter import ReviewResult

logger = logging.getLogger("review-bot")

# Map internal verdict to GitHub review event
_VERDICT_TO_EVENT: dict[str, str] = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}


class ReviewPoster:
    """Takes a ReviewResult and posts it to GitHub as a PR review."""

    def __init__(self, github_client: GitHubAPIClient) -> None:
        self._client = github_client

    async def post(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        result: ReviewResult,
    ) -> dict:
        """Post a ReviewResult to GitHub as a PR review.

        Posts the categorized summary as the review body and inline
        comments as review comments with the appropriate verdict.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            result: Structured review result to post.

        Returns:
            GitHub API response dict.
        """
        body = self._format_body(result)
        event = _VERDICT_TO_EVENT.get(result.verdict, "COMMENT")

        comments: list[ReviewComment] | None = None
        if result.inline_comments:
            comments = [
                ReviewComment(
                    path=ic.file,
                    line=ic.line,
                    body=ic.body,
                )
                for ic in result.inline_comments
            ]

        logger.info(
            "Posting review to %s/%s#%d: verdict=%s, sections=%d, inline_comments=%d",
            owner,
            repo,
            pr_number,
            result.verdict,
            len(result.summary_sections),
            len(result.inline_comments),
        )

        try:
            response = await self._client.post_review(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                body=body,
                event=event,
                comments=comments,
            )
            logger.info("Review posted successfully")
            return response
        except Exception as exc:
            logger.error("Failed to post review: %s", exc)
            # Try posting as a plain comment as fallback
            try:
                response = await self._client.post_comment(
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    body=body,
                )
                logger.info("Posted review as plain comment (fallback)")
                return response
            except Exception as fallback_exc:
                logger.error(
                    "Failed to post fallback comment: %s",
                    fallback_exc,
                )
                raise exc from fallback_exc

    def _format_body(self, result: ReviewResult) -> str:
        """Format the review body from a ReviewResult."""
        lines: list[str] = []

        # Header
        lines.append(f"## Reviewing as {result.persona_name}-bot 🤖")
        lines.append("")

        # Verdict badge
        verdict_labels = {
            "approve": "✅ **Approved**",
            "request_changes": "🔴 **Changes Requested**",
            "comment": "💬 **Comments**",
        }
        lines.append(verdict_labels.get(result.verdict, f"**{result.verdict}**"))
        lines.append("")

        # Category sections
        for section in result.summary_sections:
            lines.append(f"### {section.emoji} {section.title}")
            lines.append("")
            for finding in section.findings:
                lines.append(f"- {finding}")
            lines.append("")

        if not result.summary_sections:
            lines.append("No issues found. Looks good! 🎉")
            lines.append("")

        return "\n".join(lines)

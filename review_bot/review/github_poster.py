"""Posts structured review results to GitHub as PR reviews."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from review_bot.github.api import GitHubAPIClient, ReviewComment
from review_bot.review.formatter import CONFIDENCE_PREFIXES, ReviewResult
from review_bot.review.severity import _infer_comment_category

if TYPE_CHECKING:
    from review_bot.review.feedback import FeedbackStore
    from review_bot.review.formatter import InlineComment

logger = logging.getLogger("review-bot")

# Map internal verdict to GitHub review event
_VERDICT_TO_EVENT: dict[str, str] = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}

# Confidence legend for the review footer
_CONFIDENCE_LEGEND = (
    "\n---\n"
    "**Confidence:** 🔴 High · 🟡 Medium · ⚪ Low"
)


class ReviewPoster:
    """Takes a ReviewResult and posts it to GitHub as a PR review."""

    def __init__(
        self,
        github_client: GitHubAPIClient,
        feedback_store: FeedbackStore | None = None,
    ) -> None:
        self._client = github_client
        self._feedback_store = feedback_store

    async def post(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        result: ReviewResult,
        *,
        pr_author: str = "",
    ) -> dict:
        """Post a ReviewResult to GitHub as a PR review.

        Posts the categorized summary as the review body and inline
        comments as review comments with the appropriate verdict.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            result: Structured review result to post.
            pr_author: GitHub username of the PR author (for feedback tracking).

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
                    body=self._format_inline_body(ic),
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
            await self._track_posted_comments(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                result=result,
                response=response,
                pr_author=pr_author,
            )
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

    async def post_progress_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        persona_name: str,
        message: str,
    ) -> int:
        """Post a progress comment on a PR. Returns the comment ID for updates.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            persona_name: Name of the reviewing persona.
            message: Progress message to display.

        Returns:
            The GitHub comment ID (int) for subsequent updates.
        """
        body = f"⏳ **{persona_name}-bot** is reviewing this PR...\n\n{message}"
        response = await self._client.post_comment(owner, repo, pr_number, body)
        return response["id"]

    async def update_progress_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        message: str,
    ) -> None:
        """Update an existing progress comment on a PR.

        Args:
            owner: Repository owner.
            repo: Repository name.
            comment_id: The comment ID returned by post_progress_comment.
            message: Updated progress message.
        """
        await self._client.update_comment(owner, repo, comment_id, message)

    async def delete_progress_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
    ) -> None:
        """Delete a progress comment (e.g., after final review is posted)."""
        await self._client.delete_comment(owner, repo, comment_id)

    async def _track_posted_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        result: ReviewResult,
        response: dict,
        pr_author: str,
    ) -> None:
        """Track posted inline comments for feedback correlation.

        Fetches individual comment IDs from the GitHub API and records
        each one via the feedback store. Failures are logged but never
        break the review posting flow.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            result: The ReviewResult that was posted.
            response: GitHub API response from creating the review.
            pr_author: GitHub username of the PR author.
        """
        if not self._feedback_store:
            return

        if not result.inline_comments:
            return

        review_id = str(response.get("id", ""))
        if not review_id:
            logger.warning("No review ID in response, skipping comment tracking")
            return

        try:
            # Fetch the review's inline comments to get individual comment IDs
            api_comments = await self._fetch_review_comments(
                owner, repo, pr_number, review_id,
            )

            # Build a lookup from (path, line) to the original InlineComment
            inline_by_location: dict[tuple[str, int], InlineComment] = {}
            for ic in result.inline_comments:
                inline_by_location[(ic.file, ic.line)] = ic

            repo_full = f"{owner}/{repo}"

            for api_comment in api_comments:
                comment_id = api_comment.get("id")
                if not comment_id:
                    continue

                path = api_comment.get("path", "")
                line = api_comment.get("line") or api_comment.get("original_line", 0)
                comment_body = api_comment.get("body", "")

                # Infer category from the original inline comment body
                original_ic = inline_by_location.get((path, line))
                original_body = original_ic.body if original_ic else comment_body
                category = _infer_comment_category(original_body)

                try:
                    await self._feedback_store.track_posted_comment(
                        comment_id=comment_id,
                        review_id=review_id,
                        persona_name=result.persona_name,
                        repo=repo_full,
                        pr_number=pr_number,
                        file_path=path or None,
                        line_number=line or None,
                        body=comment_body,
                        category=category,
                        pr_author=pr_author,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to track comment %s: %s",
                        comment_id,
                        exc,
                    )

            logger.info(
                "Tracked %d posted comments for review %s",
                len(api_comments),
                review_id,
            )
        except Exception as exc:
            logger.warning("Failed to track posted comments: %s", exc)

    async def _fetch_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        review_id: str,
    ) -> list[dict]:
        """Fetch inline comments for a specific review from GitHub API.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            review_id: GitHub review ID.

        Returns:
            List of comment dicts from the GitHub API.
        """
        return await self._client.get_review_comments(
            owner, repo, pr_number, review_id,
        )

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

        # Category sections with confidence prefixes
        has_findings = False
        for section in result.summary_sections:
            lines.append(f"### {section.emoji} {section.title}")
            lines.append("")
            for finding in section.findings:
                prefix = CONFIDENCE_PREFIXES.get(finding.confidence, "🟡")
                lines.append(f"- {prefix} {finding.text}")
                has_findings = True
            lines.append("")

        if not result.summary_sections:
            lines.append("No issues found. Looks good! 🎉")
            lines.append("")

        # Add confidence legend if there are any findings
        if has_findings:
            lines.append(_CONFIDENCE_LEGEND)
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _format_inline_body(ic: InlineComment) -> str:
        """Format an inline comment body with confidence prefix.

        Args:
            ic: An InlineComment object with body and confidence fields.

        Returns:
            The formatted comment body string.
        """
        prefix = CONFIDENCE_PREFIXES.get(ic.confidence, "🟡")
        return f"{prefix} {ic.body}"

"""Core notification types: message model, channel protocol, and dispatcher."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

logger = logging.getLogger("review-bot")


class NotificationMessage(BaseModel):
    """Data object representing a notification to be sent after a review."""

    title: str = Field(description="Notification title, e.g. 'Review Complete: org/repo#42'")
    pr_url: str = Field(description="Full GitHub PR URL")
    persona_name: str = Field(description="Name of the persona that performed the review")
    repo: str = Field(description="Full repo name in 'owner/repo' format")
    pr_number: int = Field(description="Pull request number")
    verdict: str = Field(description="Review verdict: 'approve', 'request_changes', or 'comment'")
    summary: str = Field(description="Brief text summary of the review findings")
    comment_count: int = Field(description="Total number of inline comments + summary findings")
    success: bool = Field(default=True, description="True if review completed successfully")
    error_message: str | None = Field(
        default=None,
        description="Error message if success is False",
    )


@runtime_checkable
class NotificationChannel(Protocol):
    """Protocol that Slack and Discord notifiers implement."""

    @property
    def channel_type(self) -> str:
        """Return the channel type identifier, e.g. 'slack' or 'discord'."""
        ...

    async def send(self, message: NotificationMessage) -> bool:
        """Send a notification message. Returns True on success, False on failure.

        Must not raise exceptions.
        """
        ...


class NotificationDispatcher:
    """Manages notification channels and dispatches messages to all of them."""

    def __init__(self, channels: list[NotificationChannel] | None = None) -> None:
        self._channels: list[NotificationChannel] = list(channels) if channels else []

    def add_channel(self, channel: NotificationChannel) -> None:
        """Add a notification channel to the dispatcher."""
        self._channels.append(channel)

    async def notify(self, message: NotificationMessage) -> dict[str, bool]:
        """Send message to all channels.

        Returns dict mapping channel_type to success bool.
        Catches exceptions per channel so one failure doesn't block others.
        """
        results: dict[str, bool] = {}
        for channel in self._channels:
            try:
                results[channel.channel_type] = await channel.send(message)
            except Exception:
                logger.exception(
                    "Notification channel '%s' raised an exception",
                    channel.channel_type,
                )
                results[channel.channel_type] = False
        return results

    @staticmethod
    def build_message_from_result(
        result: object,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> NotificationMessage:
        """Construct a NotificationMessage from a ReviewResult.

        Args:
            result: A ReviewResult instance (imported lazily to avoid circular deps).
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
        """
        from review_bot.review.formatter import ReviewResult

        if not isinstance(result, ReviewResult):
            raise TypeError(f"Expected ReviewResult, got {type(result).__name__}")

        # Count total findings across summary sections + inline comments
        finding_count = sum(
            len(section.findings) for section in result.summary_sections
        )
        comment_count = finding_count + len(result.inline_comments)

        # Build a brief summary from section titles
        section_titles = [s.title for s in result.summary_sections if s.findings]
        summary = (
            f"{result.verdict.replace('_', ' ').title()}: "
            f"{', '.join(section_titles) if section_titles else 'No issues found'}"
        )

        full_repo = f"{owner}/{repo}"
        title = f"Review Complete: {full_repo}#{pr_number}"

        return NotificationMessage(
            title=title,
            pr_url=result.pr_url,
            persona_name=result.persona_name,
            repo=full_repo,
            pr_number=pr_number,
            verdict=result.verdict,
            summary=summary,
            comment_count=comment_count,
        )


def create_notifiers(settings: object) -> list[NotificationChannel]:
    """Create notifier instances from application settings.

    Args:
        settings: A Settings instance (accepts object to avoid circular import).

    Returns:
        List of configured NotificationChannel instances. Empty if notifications disabled.
    """
    from review_bot.notifications.discord import DiscordNotifier
    from review_bot.notifications.slack import SlackNotifier

    if not getattr(settings, "notifications_enabled", False):
        return []

    channels: list[NotificationChannel] = []

    slack_bot_token = getattr(settings, "slack_bot_token", None)
    slack_channel = getattr(settings, "slack_channel", None)
    if slack_bot_token and slack_channel:
        channels.append(SlackNotifier(bot_token=slack_bot_token, channel=slack_channel))

    discord_webhook_url = getattr(settings, "discord_webhook_url", None)
    if discord_webhook_url:
        channels.append(DiscordNotifier(webhook_url=discord_webhook_url))

    return channels

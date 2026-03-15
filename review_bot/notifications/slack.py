"""Slack notification channel using chat.postMessage with Block Kit formatting."""

from __future__ import annotations

import logging

import httpx

from review_bot.notifications.base import NotificationMessage

logger = logging.getLogger("review-bot")

MAX_MESSAGE_LENGTH = 3000

_VERDICT_EMOJI: dict[str, str] = {
    "approve": ":white_check_mark:",
    "request_changes": ":x:",
    "comment": ":speech_balloon:",
}


class SlackNotifier:
    """NotificationChannel implementation for Slack."""

    def __init__(
        self,
        *,
        bot_token: str,
        channel: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._channel = channel
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient()

    @property
    def channel_type(self) -> str:
        """Return 'slack'."""
        return "slack"

    async def send(self, message: NotificationMessage) -> bool:
        """POST to Slack chat.postMessage. Returns True if data['ok'] is True."""
        blocks = self._build_blocks(message)
        fallback_text = f"{message.title}: {message.summary}"

        try:
            response = await self._http_client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self._bot_token}"},
                json={
                    "channel": self._channel,
                    "text": fallback_text,
                    "blocks": blocks,
                },
            )
            data = response.json()

            if data.get("ok"):
                return True

            error = data.get("error", "unknown")
            if error == "channel_not_found":
                logger.error("Slack channel not found: %s", self._channel)
            elif error == "invalid_auth":
                logger.error("Slack authentication failed: invalid bot token")
            else:
                logger.error("Slack API error: %s", error)
            return False
        except Exception:
            logger.exception("Failed to send Slack notification")
            return False

    def _build_blocks(self, message: NotificationMessage) -> list[dict]:
        """Build Slack Block Kit blocks for the notification."""
        verdict_emoji = _VERDICT_EMOJI.get(message.verdict, ":question:")

        header_text = f"{verdict_emoji} {message.title}"

        body_parts = [
            f"*Persona:* {message.persona_name}",
            f"*PR:* <{message.pr_url}|{message.repo}#{message.pr_number}>",
            f"*Verdict:* {message.verdict.replace('_', ' ').title()}",
            f"*Comments:* {message.comment_count}",
            "",
            message.summary,
        ]

        if not message.success and message.error_message:
            body_parts.append(f"\n:warning: *Error:* {message.error_message}")

        body = "\n".join(body_parts)
        if len(body) > MAX_MESSAGE_LENGTH:
            body = body[: MAX_MESSAGE_LENGTH - 3] + "..."

        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header_text[:150], "emoji": True},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": body},
            },
        ]

    async def close(self) -> None:
        """Close the owned httpx client if one was created."""
        if self._owns_client:
            await self._http_client.aclose()

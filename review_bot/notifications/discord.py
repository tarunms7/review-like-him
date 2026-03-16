"""Discord notification channel using webhook URL with embed formatting."""

from __future__ import annotations

import logging

import httpx

from review_bot.notifications.base import NotificationMessage

logger = logging.getLogger("review-bot")

MAX_EMBED_DESCRIPTION = 4096

# Discord embed colors (decimal)
_VERDICT_COLORS: dict[str, int] = {
    "approve": 0x2ECC71,       # green
    "request_changes": 0xE74C3C,  # red
    "comment": 0x3498DB,       # blue
}


class DiscordNotifier:
    """NotificationChannel implementation for Discord webhooks."""

    def __init__(
        self,
        *,
        webhook_url: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient()

    @property
    def channel_type(self) -> str:
        """Return 'discord'."""
        return "discord"

    async def send(self, message: NotificationMessage) -> bool:
        """POST to Discord webhook URL with embed payload.

        Returns True for 200-204 status codes.
        """
        embed = self._build_embed(message)

        try:
            response = await self._http_client.post(
                self._webhook_url,
                json={"embeds": [embed]},
            )

            if 200 <= response.status_code <= 204:
                return True

            if response.status_code == 404:
                logger.error("Discord webhook not found: URL may be invalid")
            elif response.status_code == 429:
                logger.error("Discord rate limit exceeded")
            elif response.status_code >= 400:
                logger.error("Discord webhook error: HTTP %d", response.status_code)
            return False
        except httpx.HTTPError:
            logger.exception("Failed to send Discord notification")
            return False

    def _build_embed(self, message: NotificationMessage) -> dict:
        """Build a Discord embed dict for the notification."""
        color = _VERDICT_COLORS.get(message.verdict, 0x95A5A6)

        description_parts = [
            f"**Persona:** {message.persona_name}",
            f"**Verdict:** {message.verdict.replace('_', ' ').title()}",
            f"**Comments:** {message.comment_count}",
            "",
            message.summary,
        ]

        if not message.success and message.error_message:
            description_parts.append(f"\n⚠️ **Error:** {message.error_message}")

        description = "\n".join(description_parts)
        if len(description) > MAX_EMBED_DESCRIPTION:
            description = description[: MAX_EMBED_DESCRIPTION - 3] + "..."

        return {
            "title": message.title,
            "url": message.pr_url,
            "description": description,
            "color": color,
        }

    async def close(self) -> None:
        """Close the owned httpx client if one was created."""
        if self._owns_client:
            await self._http_client.aclose()

"""Tests for the notification system: base, Slack, and Discord channels."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from review_bot.notifications.base import (
    NotificationChannel,
    NotificationDispatcher,
    NotificationMessage,
)
from review_bot.notifications.discord import (
    _VERDICT_COLORS,
    MAX_EMBED_DESCRIPTION,
    DiscordNotifier,
)
from review_bot.notifications.slack import MAX_MESSAGE_LENGTH, SlackNotifier
from review_bot.review.formatter import (
    CategorySection,
    Finding,
    InlineComment,
    ReviewResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_message() -> NotificationMessage:
    """A standard notification message for testing."""
    return NotificationMessage(
        title="Review Complete: owner/repo#42",
        pr_url="https://github.com/owner/repo/pull/42",
        persona_name="alice",
        repo="owner/repo",
        pr_number=42,
        verdict="request_changes",
        summary="Request Changes: Security, Testing",
        comment_count=3,
    )


@pytest.fixture()
def failure_message() -> NotificationMessage:
    """A failure notification message."""
    return NotificationMessage(
        title="Review Failed: owner/repo#42",
        pr_url="https://github.com/owner/repo/pull/42",
        persona_name="alice",
        repo="owner/repo",
        pr_number=42,
        verdict="comment",
        summary="Review failed",
        comment_count=0,
        success=False,
        error_message="LLM timeout",
    )


@pytest.fixture()
def review_result() -> ReviewResult:
    """A ReviewResult for testing build_message_from_result."""
    return ReviewResult(
        verdict="request_changes",
        summary_sections=[
            CategorySection(
                emoji="🔒",
                title="Security",
                findings=[
                    Finding(text="Hard-coded secret", confidence="high"),
                ],
            ),
            CategorySection(
                emoji="🧪",
                title="Testing",
                findings=[
                    Finding(text="No tests for auth", confidence="medium"),
                ],
            ),
        ],
        inline_comments=[
            InlineComment(
                file="src/auth.py",
                line=4,
                body="Use env vars for secrets.",
                confidence="high",
            ),
        ],
        persona_name="alice",
        pr_url="https://github.com/owner/repo/pull/42",
    )


# ---------------------------------------------------------------------------
# NotificationMessage tests
# ---------------------------------------------------------------------------


class TestNotificationMessage:
    def test_defaults(self) -> None:
        msg = NotificationMessage(
            title="Test",
            pr_url="https://github.com/o/r/pull/1",
            persona_name="p",
            repo="o/r",
            pr_number=1,
            verdict="approve",
            summary="ok",
            comment_count=0,
        )
        assert msg.success is True
        assert msg.error_message is None

    def test_failure_fields(self, failure_message: NotificationMessage) -> None:
        assert failure_message.success is False
        assert failure_message.error_message == "LLM timeout"


# ---------------------------------------------------------------------------
# NotificationChannel protocol tests
# ---------------------------------------------------------------------------


class TestNotificationChannelProtocol:
    def test_slack_is_channel(self) -> None:
        notifier = SlackNotifier(bot_token="xoxb-test", channel="#general")
        assert isinstance(notifier, NotificationChannel)

    def test_discord_is_channel(self) -> None:
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")
        assert isinstance(notifier, NotificationChannel)


# ---------------------------------------------------------------------------
# NotificationDispatcher tests
# ---------------------------------------------------------------------------


class TestNotificationDispatcher:
    async def test_empty_dispatcher_is_noop(self, sample_message: NotificationMessage) -> None:
        dispatcher = NotificationDispatcher()
        results = await dispatcher.notify(sample_message)
        assert results == {}

    async def test_dispatch_to_multiple_channels(
        self, sample_message: NotificationMessage
    ) -> None:
        ch1 = MagicMock()
        ch1.channel_type = "slack"
        ch1.send = AsyncMock(return_value=True)

        ch2 = MagicMock()
        ch2.channel_type = "discord"
        ch2.send = AsyncMock(return_value=True)

        dispatcher = NotificationDispatcher(channels=[ch1, ch2])
        results = await dispatcher.notify(sample_message)

        assert results == {"slack": True, "discord": True}
        ch1.send.assert_awaited_once_with(sample_message)
        ch2.send.assert_awaited_once_with(sample_message)

    async def test_continues_when_one_channel_fails(
        self, sample_message: NotificationMessage
    ) -> None:
        ch_fail = MagicMock()
        ch_fail.channel_type = "slack"
        ch_fail.send = AsyncMock(side_effect=RuntimeError("connection error"))

        ch_ok = MagicMock()
        ch_ok.channel_type = "discord"
        ch_ok.send = AsyncMock(return_value=True)

        dispatcher = NotificationDispatcher(channels=[ch_fail, ch_ok])
        results = await dispatcher.notify(sample_message)

        assert results == {"slack": False, "discord": True}

    async def test_channel_returns_false(self, sample_message: NotificationMessage) -> None:
        ch = MagicMock()
        ch.channel_type = "slack"
        ch.send = AsyncMock(return_value=False)

        dispatcher = NotificationDispatcher(channels=[ch])
        results = await dispatcher.notify(sample_message)

        assert results == {"slack": False}

    async def test_add_channel(self, sample_message: NotificationMessage) -> None:
        dispatcher = NotificationDispatcher()

        ch = MagicMock()
        ch.channel_type = "test"
        ch.send = AsyncMock(return_value=True)

        dispatcher.add_channel(ch)
        results = await dispatcher.notify(sample_message)

        assert results == {"test": True}


# ---------------------------------------------------------------------------
# build_message_from_result tests
# ---------------------------------------------------------------------------


class TestBuildMessageFromResult:
    def test_produces_correct_fields(self, review_result: ReviewResult) -> None:
        msg = NotificationDispatcher.build_message_from_result(
            review_result, "owner", "repo", 42
        )

        assert msg.title == "Review Complete: owner/repo#42"
        assert msg.pr_url == "https://github.com/owner/repo/pull/42"
        assert msg.persona_name == "alice"
        assert msg.repo == "owner/repo"
        assert msg.pr_number == 42
        assert msg.verdict == "request_changes"
        # 2 findings + 1 inline = 3
        assert msg.comment_count == 3
        assert msg.success is True
        assert msg.error_message is None
        assert "Security" in msg.summary
        assert "Testing" in msg.summary

    def test_approve_verdict(self) -> None:
        result = ReviewResult(
            verdict="approve",
            summary_sections=[],
            inline_comments=[],
            persona_name="bob",
            pr_url="https://github.com/a/b/pull/1",
        )
        msg = NotificationDispatcher.build_message_from_result(result, "a", "b", 1)
        assert msg.verdict == "approve"
        assert msg.comment_count == 0
        assert "No issues found" in msg.summary

    def test_rejects_non_review_result(self) -> None:
        with pytest.raises(TypeError, match="Expected ReviewResult"):
            NotificationDispatcher.build_message_from_result(
                {"verdict": "approve"}, "a", "b", 1
            )


# ---------------------------------------------------------------------------
# SlackNotifier tests
# ---------------------------------------------------------------------------


class TestSlackNotifier:
    async def test_send_success(self, sample_message: NotificationMessage) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        mock_client.post.return_value = mock_response

        notifier = SlackNotifier(
            bot_token="xoxb-test", channel="#reviews", http_client=mock_client
        )
        result = await notifier.send(sample_message)

        assert result is True
        mock_client.post.assert_awaited_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.args[0] == "https://slack.com/api/chat.postMessage"
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer xoxb-test"
        assert call_kwargs.kwargs["json"]["channel"] == "#reviews"

    async def test_send_channel_not_found(
        self, sample_message: NotificationMessage
    ) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "error": "channel_not_found"}
        mock_client.post.return_value = mock_response

        notifier = SlackNotifier(
            bot_token="xoxb-test", channel="#missing", http_client=mock_client
        )
        result = await notifier.send(sample_message)

        assert result is False

    async def test_send_invalid_auth(self, sample_message: NotificationMessage) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "error": "invalid_auth"}
        mock_client.post.return_value = mock_response

        notifier = SlackNotifier(
            bot_token="bad-token", channel="#reviews", http_client=mock_client
        )
        result = await notifier.send(sample_message)

        assert result is False

    async def test_send_exception(self, sample_message: NotificationMessage) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        notifier = SlackNotifier(
            bot_token="xoxb-test", channel="#reviews", http_client=mock_client
        )
        result = await notifier.send(sample_message)

        assert result is False

    def test_build_blocks_structure(self, sample_message: NotificationMessage) -> None:
        notifier = SlackNotifier(bot_token="xoxb-test", channel="#reviews")
        blocks = notifier._build_blocks(sample_message)

        assert len(blocks) == 2
        assert blocks[0]["type"] == "header"
        assert blocks[0]["text"]["type"] == "plain_text"
        assert blocks[1]["type"] == "section"
        assert blocks[1]["text"]["type"] == "mrkdwn"
        assert "alice" in blocks[1]["text"]["text"]
        assert "owner/repo#42" in blocks[1]["text"]["text"]

    def test_build_blocks_truncation(self, sample_message: NotificationMessage) -> None:
        long_message = sample_message.model_copy(update={"summary": "x" * 5000})
        notifier = SlackNotifier(bot_token="xoxb-test", channel="#reviews")
        blocks = notifier._build_blocks(long_message)

        body = blocks[1]["text"]["text"]
        assert len(body) <= MAX_MESSAGE_LENGTH
        assert body.endswith("...")

    def test_build_blocks_failure_message(
        self, failure_message: NotificationMessage
    ) -> None:
        notifier = SlackNotifier(bot_token="xoxb-test", channel="#reviews")
        blocks = notifier._build_blocks(failure_message)

        body = blocks[1]["text"]["text"]
        assert "Error" in body
        assert "LLM timeout" in body

    async def test_close_owned_client(self) -> None:
        notifier = SlackNotifier(bot_token="xoxb-test", channel="#reviews")
        with patch.object(notifier._http_client, "aclose", new_callable=AsyncMock) as mock_close:
            await notifier.close()
            mock_close.assert_awaited_once()

    async def test_close_external_client(self) -> None:
        external_client = AsyncMock(spec=httpx.AsyncClient)
        notifier = SlackNotifier(
            bot_token="xoxb-test", channel="#reviews", http_client=external_client
        )
        await notifier.close()
        # Should not close external client
        external_client.aclose.assert_not_awaited()


# ---------------------------------------------------------------------------
# DiscordNotifier tests
# ---------------------------------------------------------------------------


class TestDiscordNotifier:
    async def test_send_success_200(self, sample_message: NotificationMessage) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response

        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            http_client=mock_client,
        )
        result = await notifier.send(sample_message)

        assert result is True
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.args[0] == "https://discord.com/api/webhooks/123/abc"
        assert "embeds" in call_kwargs.kwargs["json"]

    async def test_send_success_204(self, sample_message: NotificationMessage) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_client.post.return_value = mock_response

        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            http_client=mock_client,
        )
        result = await notifier.send(sample_message)

        assert result is True

    async def test_send_404(self, sample_message: NotificationMessage) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.post.return_value = mock_response

        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/bad/url",
            http_client=mock_client,
        )
        result = await notifier.send(sample_message)

        assert result is False

    async def test_send_429_rate_limit(self, sample_message: NotificationMessage) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_client.post.return_value = mock_response

        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            http_client=mock_client,
        )
        result = await notifier.send(sample_message)

        assert result is False

    async def test_send_exception(self, sample_message: NotificationMessage) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            http_client=mock_client,
        )
        result = await notifier.send(sample_message)

        assert result is False

    def test_build_embed_structure(self, sample_message: NotificationMessage) -> None:
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc"
        )
        embed = notifier._build_embed(sample_message)

        assert embed["title"] == sample_message.title
        assert embed["url"] == sample_message.pr_url
        assert embed["color"] == _VERDICT_COLORS["request_changes"]
        assert "alice" in embed["description"]

    def test_build_embed_colors(self) -> None:
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc"
        )
        for verdict, expected_color in _VERDICT_COLORS.items():
            msg = NotificationMessage(
                title="Test",
                pr_url="https://github.com/o/r/pull/1",
                persona_name="p",
                repo="o/r",
                pr_number=1,
                verdict=verdict,
                summary="s",
                comment_count=0,
            )
            embed = notifier._build_embed(msg)
            assert embed["color"] == expected_color

    def test_build_embed_truncation(self, sample_message: NotificationMessage) -> None:
        long_message = sample_message.model_copy(update={"summary": "x" * 6000})
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc"
        )
        embed = notifier._build_embed(long_message)

        assert len(embed["description"]) <= MAX_EMBED_DESCRIPTION
        assert embed["description"].endswith("...")

    def test_build_embed_failure_message(
        self, failure_message: NotificationMessage
    ) -> None:
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc"
        )
        embed = notifier._build_embed(failure_message)

        assert "Error" in embed["description"]
        assert "LLM timeout" in embed["description"]

    async def test_close_owned_client(self) -> None:
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc"
        )
        with patch.object(notifier._http_client, "aclose", new_callable=AsyncMock) as mock_close:
            await notifier.close()
            mock_close.assert_awaited_once()

    async def test_close_external_client(self) -> None:
        external_client = AsyncMock(spec=httpx.AsyncClient)
        notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            http_client=external_client,
        )
        await notifier.close()
        external_client.aclose.assert_not_awaited()

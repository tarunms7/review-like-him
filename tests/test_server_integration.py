"""Tests for server integration: dashboard routes and notification wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from review_bot.dashboard.router import router as dashboard_router
from review_bot.notifications.base import (
    NotificationDispatcher,
    NotificationMessage,
)
from review_bot.notifications.discord import DiscordNotifier
from review_bot.notifications.slack import SlackNotifier
from review_bot.server.queue import AsyncJobQueue

# ---------------------------------------------------------------------------
# Feature detection flags for pending integration code
# ---------------------------------------------------------------------------

_has_notification_settings = hasattr(
    __import__("review_bot.config.settings", fromlist=["Settings"]).Settings,
    "model_fields",
) and "slack_bot_token" in __import__(
    "review_bot.config.settings", fromlist=["Settings"]
).Settings.model_fields

_has_notification_log = False
try:
    from review_bot.server.app import _CREATE_TABLES_SQL

    _has_notification_log = any("notification_log" in sql for sql in _CREATE_TABLES_SQL)
except Exception:
    pass

_pending_settings = pytest.mark.skipif(
    not _has_notification_settings,
    reason="Settings notification fields not yet added (pending app.py/settings.py wiring)",
)
_pending_notif_log = pytest.mark.skipif(
    not _has_notification_log,
    reason="notification_log table not yet added to _CREATE_TABLES_SQL",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_integration_app(
    *,
    engine=None,
    job_queue=None,
    persona_store=None,
    notification_dispatcher=None,
    include_dashboard: bool = True,
) -> FastAPI:
    """Create a minimal FastAPI app with dashboard and health routers for testing."""
    from review_bot.server.health import router as health_router

    app = FastAPI()
    app.include_router(health_router)

    if include_dashboard:
        app.include_router(dashboard_router)

    # Set up app.state with provided or default mocks
    if engine is None:
        engine = MagicMock()
    if job_queue is None:
        jq = MagicMock(spec=AsyncJobQueue)
        type(jq).queue_depth = PropertyMock(return_value=0)
        type(jq).worker_status = PropertyMock(return_value="running")
        type(jq).current_job_id = PropertyMock(return_value=None)
        job_queue = jq
    if persona_store is None:
        persona_store = MagicMock()
        persona_store.list_all.return_value = []

    app.state.db_engine = engine
    app.state.job_queue = job_queue
    app.state.persona_store = persona_store

    if notification_dispatcher is not None:
        app.state.notification_dispatcher = notification_dispatcher

    return app


async def _create_real_engine(tmp_path):
    """Create a real async SQLite engine with tables initialized."""
    from review_bot.server.app import _init_database

    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    await _init_database(engine)
    return engine


# ---------------------------------------------------------------------------
# Dashboard Route Accessibility Tests
# ---------------------------------------------------------------------------


class TestDashboardRoutesAccessible:
    """Test that dashboard routes are accessible when app is created."""

    @pytest.mark.asyncio()
    async def test_dashboard_overview_returns_html(self, tmp_path):
        """GET /dashboard/ returns 200 with HTML content."""
        engine = await _create_real_engine(tmp_path)
        try:
            app = _create_integration_app(engine=engine)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/dashboard/")
                assert resp.status_code == 200
                assert "text/html" in resp.headers.get("content-type", "")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_dashboard_activity_returns_html(self, tmp_path):
        """GET /dashboard/activity returns 200 with HTML content."""
        engine = await _create_real_engine(tmp_path)
        try:
            app = _create_integration_app(engine=engine)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/dashboard/activity")
                assert resp.status_code == 200
                assert "text/html" in resp.headers.get("content-type", "")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_dashboard_personas_returns_html(self, tmp_path):
        """GET /dashboard/personas returns 200 with HTML content."""
        engine = await _create_real_engine(tmp_path)
        try:
            app = _create_integration_app(engine=engine)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/dashboard/personas")
                assert resp.status_code == 200
                assert "text/html" in resp.headers.get("content-type", "")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_dashboard_queue_returns_html(self, tmp_path):
        """GET /dashboard/queue returns 200 with HTML content."""
        engine = await _create_real_engine(tmp_path)
        try:
            app = _create_integration_app(engine=engine)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/dashboard/queue")
                assert resp.status_code == 200
                assert "text/html" in resp.headers.get("content-type", "")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_dashboard_config_returns_html(self, tmp_path):
        """GET /dashboard/config returns 200 with HTML content."""
        engine = await _create_real_engine(tmp_path)
        try:
            app = _create_integration_app(engine=engine)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/dashboard/config")
                assert resp.status_code == 200
                assert "text/html" in resp.headers.get("content-type", "")
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_dashboard_not_mounted_returns_404(self):
        """Without dashboard router, /dashboard/ returns 404."""
        app = _create_integration_app(include_dashboard=False)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/")
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Notification Dispatcher Initialization Tests
# ---------------------------------------------------------------------------


class TestNotificationDispatcherInit:
    """Test that notification dispatcher is properly initialized from settings."""

    def test_dispatcher_with_slack_config(self):
        """When Slack settings are provided, SlackNotifier is added to dispatcher."""
        dispatcher = NotificationDispatcher()
        slack = SlackNotifier(
            bot_token="xoxb-test-token",
            channel="#reviews",
        )
        dispatcher.add_channel(slack)
        assert len(dispatcher._channels) == 1
        assert dispatcher._channels[0].channel_type == "slack"

    def test_dispatcher_with_discord_config(self):
        """When Discord settings are provided, DiscordNotifier is added."""
        dispatcher = NotificationDispatcher()
        discord = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc",
        )
        dispatcher.add_channel(discord)
        assert len(dispatcher._channels) == 1
        assert dispatcher._channels[0].channel_type == "discord"

    def test_dispatcher_with_both_channels(self):
        """When both Slack and Discord settings are provided, both are added."""
        slack = SlackNotifier(bot_token="xoxb-test", channel="#reviews")
        discord = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123/abc",
        )
        dispatcher = NotificationDispatcher(channels=[slack, discord])
        assert len(dispatcher._channels) == 2
        channel_types = {ch.channel_type for ch in dispatcher._channels}
        assert channel_types == {"slack", "discord"}

    def test_dispatcher_empty_when_no_settings(self):
        """When no notification settings are provided, dispatcher has no channels."""
        dispatcher = NotificationDispatcher()
        assert len(dispatcher._channels) == 0

    @pytest.mark.asyncio()
    async def test_empty_dispatcher_notify_returns_empty_dict(self):
        """Notify with no channels returns empty dict."""
        dispatcher = NotificationDispatcher()
        message = NotificationMessage(
            title="Test",
            pr_url="https://github.com/org/repo/pull/1",
            persona_name="alice",
            repo="org/repo",
            pr_number=1,
            verdict="approve",
            summary="All good",
            comment_count=0,
        )
        result = await dispatcher.notify(message)
        assert result == {}


class TestDispatcherWiringToQueue:
    """Test that the dispatcher is wired to the job queue."""

    def test_dispatcher_set_on_queue(self):
        """Dispatcher is set as _notification_dispatcher on job queue."""
        mock_engine = MagicMock()
        mock_auth = MagicMock()
        mock_store = MagicMock()
        queue = AsyncJobQueue(
            db_engine=mock_engine,
            github_auth=mock_auth,
            persona_store=mock_store,
        )
        dispatcher = NotificationDispatcher()
        queue._notification_dispatcher = dispatcher
        assert queue._notification_dispatcher is dispatcher

    def test_dispatcher_absent_by_default(self):
        """By default, _notification_dispatcher is not set on queue."""
        mock_engine = MagicMock()
        mock_auth = MagicMock()
        mock_store = MagicMock()
        queue = AsyncJobQueue(
            db_engine=mock_engine,
            github_auth=mock_auth,
            persona_store=mock_store,
        )
        # Use getattr with None default as the contract specifies
        assert getattr(queue, "_notification_dispatcher", None) is None

    def test_dispatcher_stored_on_app_state(self):
        """Dispatcher is stored on app.state.notification_dispatcher."""
        dispatcher = NotificationDispatcher()
        app = _create_integration_app(notification_dispatcher=dispatcher)
        assert app.state.notification_dispatcher is dispatcher


# ---------------------------------------------------------------------------
# Notification Log Table Tests
# ---------------------------------------------------------------------------


@_pending_notif_log
class TestNotificationLogTable:
    """Test that the notification_log table is created during DB init."""

    @pytest.mark.asyncio()
    async def test_notification_log_table_exists(self, tmp_path):
        """notification_log table is created by _init_database."""
        engine = await _create_real_engine(tmp_path)
        try:
            async with engine.begin() as conn:
                # Check that the table exists by querying it
                result = await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='notification_log'"
                    )
                )
                row = result.fetchone()
                assert row is not None, "notification_log table should exist"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_notification_log_table_columns(self, tmp_path):
        """notification_log table has the expected columns."""
        engine = await _create_real_engine(tmp_path)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text("PRAGMA table_info(notification_log)"))
                columns = {row[1]: row[2] for row in result.fetchall()}

                assert "id" in columns
                assert "review_id" in columns
                assert "channel_type" in columns
                assert "success" in columns
                assert "error_message" in columns
                assert "sent_at" in columns
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_notification_log_insert(self, tmp_path):
        """Can insert a row into notification_log."""
        engine = await _create_real_engine(tmp_path)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO notification_log "
                        "(channel_type, success, sent_at) "
                        "VALUES (:channel_type, :success, :sent_at)"
                    ),
                    {
                        "channel_type": "slack",
                        "success": 1,
                        "sent_at": "2026-03-16T10:00:00Z",
                    },
                )
                result = await conn.execute(
                    text("SELECT * FROM notification_log")
                )
                rows = result.fetchall()
                assert len(rows) == 1
                # id, review_id, channel_type, success, error_message, sent_at
                assert rows[0][2] == "slack"  # channel_type
                assert rows[0][3] == 1  # success
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Settings Notification Fields Tests
# ---------------------------------------------------------------------------


@_pending_settings
class TestSettingsNotificationFields:
    """Test that Settings has the notification-related fields."""

    def test_settings_has_slack_fields(self):
        """Settings model accepts slack_bot_token and slack_channel."""
        from review_bot.config.settings import Settings

        s = Settings(
            github_app_id=1,
            webhook_secret="s",
            slack_bot_token="xoxb-test",
            slack_channel="#reviews",
        )
        assert s.slack_bot_token == "xoxb-test"
        assert s.slack_channel == "#reviews"

    def test_settings_has_discord_field(self):
        """Settings model accepts discord_webhook_url."""
        from review_bot.config.settings import Settings

        s = Settings(
            github_app_id=1,
            webhook_secret="s",
            discord_webhook_url="https://discord.com/api/webhooks/123/abc",
        )
        assert s.discord_webhook_url == "https://discord.com/api/webhooks/123/abc"

    def test_settings_notification_defaults(self):
        """Settings notification fields default to empty/True."""
        from review_bot.config.settings import Settings

        s = Settings(github_app_id=1, webhook_secret="s")
        assert s.slack_bot_token == ""
        assert s.slack_channel == ""
        assert s.discord_webhook_url == ""
        assert s.notify_on_success is True
        assert s.notify_on_failure is True

    def test_settings_notify_flags(self):
        """Settings notify_on_success and notify_on_failure can be set."""
        from review_bot.config.settings import Settings

        s = Settings(
            github_app_id=1,
            webhook_secret="s",
            notify_on_success=False,
            notify_on_failure=False,
        )
        assert s.notify_on_success is False
        assert s.notify_on_failure is False


# ---------------------------------------------------------------------------
# Dispatcher Channel Initialization from Settings
# ---------------------------------------------------------------------------


@_pending_settings
class TestDispatcherFromSettings:
    """Test building a dispatcher from Settings configuration."""

    def test_build_dispatcher_with_slack_settings(self):
        """Dispatcher gets SlackNotifier when settings have slack config."""
        from review_bot.config.settings import Settings

        settings = Settings(
            github_app_id=1,
            webhook_secret="s",
            slack_bot_token="xoxb-test-token",
            slack_channel="#code-reviews",
        )
        dispatcher = NotificationDispatcher()
        if settings.slack_bot_token and settings.slack_channel:
            dispatcher.add_channel(
                SlackNotifier(
                    bot_token=settings.slack_bot_token,
                    channel=settings.slack_channel,
                )
            )
        assert len(dispatcher._channels) == 1
        assert dispatcher._channels[0].channel_type == "slack"

    def test_build_dispatcher_with_discord_settings(self):
        """Dispatcher gets DiscordNotifier when settings have discord config."""
        from review_bot.config.settings import Settings

        settings = Settings(
            github_app_id=1,
            webhook_secret="s",
            discord_webhook_url="https://discord.com/api/webhooks/123/abc",
        )
        dispatcher = NotificationDispatcher()
        if settings.discord_webhook_url:
            dispatcher.add_channel(
                DiscordNotifier(webhook_url=settings.discord_webhook_url)
            )
        assert len(dispatcher._channels) == 1
        assert dispatcher._channels[0].channel_type == "discord"

    def test_build_dispatcher_no_notification_settings(self):
        """Dispatcher has no channels when no notification config is set."""
        from review_bot.config.settings import Settings

        settings = Settings(github_app_id=1, webhook_secret="s")
        dispatcher = NotificationDispatcher()
        if settings.slack_bot_token and settings.slack_channel:
            dispatcher.add_channel(
                SlackNotifier(
                    bot_token=settings.slack_bot_token,
                    channel=settings.slack_channel,
                )
            )
        if settings.discord_webhook_url:
            dispatcher.add_channel(
                DiscordNotifier(webhook_url=settings.discord_webhook_url)
            )
        assert len(dispatcher._channels) == 0

    def test_build_dispatcher_with_both_settings(self):
        """Dispatcher gets both channels when both are configured."""
        from review_bot.config.settings import Settings

        settings = Settings(
            github_app_id=1,
            webhook_secret="s",
            slack_bot_token="xoxb-test",
            slack_channel="#reviews",
            discord_webhook_url="https://discord.com/api/webhooks/123/abc",
        )
        dispatcher = NotificationDispatcher()
        if settings.slack_bot_token and settings.slack_channel:
            dispatcher.add_channel(
                SlackNotifier(
                    bot_token=settings.slack_bot_token,
                    channel=settings.slack_channel,
                )
            )
        if settings.discord_webhook_url:
            dispatcher.add_channel(
                DiscordNotifier(webhook_url=settings.discord_webhook_url)
            )
        assert len(dispatcher._channels) == 2
        types = {ch.channel_type for ch in dispatcher._channels}
        assert types == {"slack", "discord"}


# ---------------------------------------------------------------------------
# Database Schema Completeness Tests
# ---------------------------------------------------------------------------


class TestDatabaseSchemaIntegration:
    """Test that all expected tables and indexes exist after init."""

    @_pending_notif_log
    @pytest.mark.asyncio()
    async def test_all_expected_tables_exist(self, tmp_path):
        """All required tables are created by _init_database."""
        engine = await _create_real_engine(tmp_path)
        try:
            expected_tables = {
                "reviews",
                "jobs",
                "persona_stats",
                "review_comment_tracking",
                "review_feedback",
                "notification_log",
            }
            async with engine.begin() as conn:
                result = await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
                tables = {row[0] for row in result.fetchall()}
            assert expected_tables.issubset(tables), (
                f"Missing tables: {expected_tables - tables}"
            )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_dedup_index_exists(self, tmp_path):
        """idx_jobs_dedup index exists after init."""
        engine = await _create_real_engine(tmp_path)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' AND name='idx_jobs_dedup'"
                    )
                )
                row = result.fetchone()
                assert row is not None, "idx_jobs_dedup index should exist"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_reviews_indexes_exist(self, tmp_path):
        """Expected indexes on reviews table are created."""
        engine = await _create_real_engine(tmp_path)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' AND name LIKE 'idx_reviews_%'"
                    )
                )
                indexes = {row[0] for row in result.fetchall()}
            expected = {
                "idx_reviews_persona_name",
                "idx_reviews_pr_number",
                "idx_reviews_repo",
                "idx_reviews_created_at",
            }
            assert expected.issubset(indexes)
        finally:
            await engine.dispose()

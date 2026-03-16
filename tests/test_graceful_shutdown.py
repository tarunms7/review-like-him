"""Tests for graceful shutdown with job drain."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from review_bot.config.settings import Settings
from review_bot.server.queue import AsyncJobQueue, GitHubProgressCallback, ReviewJob
from review_bot.server.webhooks import configure, router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signature(payload: bytes, secret: str = "test-secret") -> str:
    """Create a valid HMAC-SHA256 signature for a webhook payload."""
    return "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()


def _make_mock_engine() -> MagicMock:
    """Create a mock async database engine."""
    engine = MagicMock()
    engine.dispose = AsyncMock()

    # Support async context manager for begin()
    conn = MagicMock()
    conn.execute = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    engine.begin = MagicMock(return_value=ctx)

    return engine


def _make_mock_queue(*, is_draining: bool = False) -> MagicMock:
    """Create a mock AsyncJobQueue with drain support."""
    queue = MagicMock(spec=AsyncJobQueue)
    queue.enqueue = AsyncMock()
    queue.is_draining = is_draining
    return queue


# ---------------------------------------------------------------------------
# Settings Tests
# ---------------------------------------------------------------------------


class TestShutdownDrainTimeoutSetting:
    """Tests for the shutdown_drain_timeout setting."""

    def test_default_value_is_30(self) -> None:
        """Verify setting loads with default of 30."""
        settings = Settings(
            github_app_id=0,
            webhook_secret="",
        )
        assert settings.shutdown_drain_timeout == 30

    def test_custom_value(self) -> None:
        """Verify setting can be customized."""
        settings = Settings(
            github_app_id=0,
            webhook_secret="",
            shutdown_drain_timeout=60,
        )
        assert settings.shutdown_drain_timeout == 60

    def test_zero_is_valid(self) -> None:
        """Zero timeout means immediate cancel."""
        settings = Settings(
            github_app_id=0,
            webhook_secret="",
            shutdown_drain_timeout=0,
        )
        assert settings.shutdown_drain_timeout == 0

    def test_negative_value_raises(self) -> None:
        """Negative timeout values are rejected."""
        with pytest.raises(ValueError, match="shutdown_drain_timeout must be >= 0"):
            Settings(
                github_app_id=0,
                webhook_secret="",
                shutdown_drain_timeout=-1,
            )

    def test_loads_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify setting loads from environment variable."""
        monkeypatch.setenv("REVIEW_BOT_SHUTDOWN_DRAIN_TIMEOUT", "45")
        settings = Settings(
            github_app_id=0,
            webhook_secret="",
        )
        assert settings.shutdown_drain_timeout == 45


# ---------------------------------------------------------------------------
# Queue Drain Tests
# ---------------------------------------------------------------------------


class TestDrainNoInflightReturnsImmediately:
    """Test drain with no in-flight jobs."""

    @pytest.mark.asyncio()
    async def test_drain_returns_true(self) -> None:
        """Empty queue, drain returns True instantly."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )
        await queue.start_worker()

        result = await queue.drain(timeout=5.0)
        assert result is True
        assert queue.is_draining is True

        await queue.stop_worker()


class TestDrainTimeoutReturnsFalse:
    """Test drain returns False when job is in-flight and timeout expires."""

    @pytest.mark.asyncio()
    async def test_drain_timeout(self) -> None:
        """Drain returns False when current job doesn't finish in time."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )
        # Simulate an in-flight job
        queue._current_job_id = "fake-job-id"

        result = await queue.drain(timeout=0.3)
        assert result is False
        assert queue.is_draining is True

    @pytest.mark.asyncio()
    async def test_drain_zero_timeout_returns_immediately(self) -> None:
        """Drain with timeout=0 returns immediately based on current state."""
        engine = _make_mock_engine()
        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=MagicMock(),
            persona_store=MagicMock(),
        )

        # No in-flight job → True
        result = await queue.drain(timeout=0)
        assert result is True

        # Reset draining state for next test
        queue._is_draining = False

        # With in-flight job → False
        queue._current_job_id = "fake-job-id"
        result = await queue.drain(timeout=0)
        assert result is False


class TestDrainDefaultTimeout:
    """Test drain uses constructor drain_timeout when timeout is None."""

    @pytest.mark.asyncio()
    async def test_drain_uses_default_timeout(self) -> None:
        """drain() with no argument uses self._drain_timeout."""
        engine = _make_mock_engine()
        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=MagicMock(),
            persona_store=MagicMock(),
            drain_timeout=0.2,
        )
        queue._current_job_id = "fake-job-id"

        result = await queue.drain()
        assert result is False
        assert queue.is_draining is True


class TestWebhookRejectsDuringDrain503:
    """Test webhook returns 503 during drain."""

    def test_webhook_returns_503(self) -> None:
        """POST /webhook during drain → 503 with detail."""
        mock_queue = _make_mock_queue(is_draining=True)
        mock_store = MagicMock()
        mock_store.exists.return_value = True

        app = FastAPI()
        app.include_router(router)
        configure(mock_queue, "test-secret", mock_store)

        client = TestClient(app)

        data = {
            "action": "review_requested",
            "pull_request": {"number": 1},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
            "requested_reviewer": {"login": "alice-bot[bot]"},
        }
        payload = json.dumps(data).encode()
        resp = client.post(
            "/webhook",
            content=payload,
            headers={
                "x-hub-signature-256": _make_signature(payload),
                "x-github-event": "pull_request",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 503
        assert "shutting down" in resp.json()["detail"].lower()



class TestEnqueueBeforeDrainSucceeds:
    """Test normal enqueue works when not draining."""

    @pytest.mark.asyncio()
    async def test_normal_enqueue(self) -> None:
        """Normal enqueue works when not draining."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        job = ReviewJob(
            owner="test",
            repo="repo",
            pr_number=1,
            persona_name="alice",
            installation_id=1,
        )

        # Should succeed without error
        job_id = await queue.enqueue(job)
        assert job_id == job.id


# ---------------------------------------------------------------------------
# GitHubProgressCallback Tests
# ---------------------------------------------------------------------------


class TestGitHubProgressCallback:
    """Tests for the live progress comment callback."""

    @pytest.mark.asyncio()
    async def test_on_progress_posts_new_comment(self) -> None:
        """First call to on_progress creates a new comment."""
        mock_client = MagicMock()
        mock_client.post_comment = AsyncMock(return_value={"id": 42})

        cb = GitHubProgressCallback(
            github_client=mock_client,
            owner="owner", repo="repo", pr_number=1,
            persona_name="alice",
        )
        await cb.on_progress("fetching_pr", "Loading PR data", percent=10)

        mock_client.post_comment.assert_called_once()
        call_body = mock_client.post_comment.call_args[0][3]
        assert "alice-bot" in call_body
        assert "10%" in call_body
        assert cb._comment_id == 42

    @pytest.mark.asyncio()
    async def test_on_progress_updates_existing_comment(self) -> None:
        """Subsequent calls PATCH the existing comment."""
        mock_client = MagicMock()
        mock_client.post_comment = AsyncMock(return_value={"id": 42})
        mock_client.update_comment = AsyncMock(return_value={"id": 42})

        cb = GitHubProgressCallback(
            github_client=mock_client,
            owner="owner", repo="repo", pr_number=1,
            persona_name="alice",
        )
        await cb.on_progress("fetching_pr", "Loading", percent=10)
        await cb.on_progress("reviewing", "Analyzing", percent=60)

        mock_client.update_comment.assert_called_once()
        call_body = mock_client.update_comment.call_args[0][2]
        assert "60%" in call_body

    @pytest.mark.asyncio()
    async def test_on_progress_without_percent(self) -> None:
        """on_progress works without a percent argument."""
        mock_client = MagicMock()
        mock_client.post_comment = AsyncMock(return_value={"id": 42})

        cb = GitHubProgressCallback(
            github_client=mock_client,
            owner="owner", repo="repo", pr_number=1,
            persona_name="alice",
        )
        await cb.on_progress("fetching_pr", "Loading PR data")

        mock_client.post_comment.assert_called_once()
        call_body = mock_client.post_comment.call_args[0][3]
        assert "alice-bot" in call_body
        # No progress bar when percent is None
        assert "%" not in call_body

    @pytest.mark.asyncio()
    async def test_on_progress_handles_post_error(self) -> None:
        """on_progress swallows errors without crashing."""
        mock_client = MagicMock()
        mock_client.post_comment = AsyncMock(side_effect=RuntimeError("network error"))

        cb = GitHubProgressCallback(
            github_client=mock_client,
            owner="owner", repo="repo", pr_number=1,
            persona_name="alice",
        )
        # Should not raise
        await cb.on_progress("fetching_pr", "Loading PR data", percent=10)
        assert cb._comment_id is None

    @pytest.mark.asyncio()
    async def test_delete_removes_comment(self) -> None:
        """delete() calls delete_comment with stored ID."""
        mock_client = MagicMock()
        mock_client.delete_comment = AsyncMock()

        cb = GitHubProgressCallback(
            github_client=mock_client,
            owner="owner", repo="repo", pr_number=1,
            persona_name="alice",
        )
        cb._comment_id = 42

        await cb.delete()
        mock_client.delete_comment.assert_called_once_with("owner", "repo", 42)

    @pytest.mark.asyncio()
    async def test_delete_noop_when_no_comment(self) -> None:
        """delete() does nothing if no comment was ever posted."""
        mock_client = MagicMock()
        mock_client.delete_comment = AsyncMock()

        cb = GitHubProgressCallback(
            github_client=mock_client,
            owner="owner", repo="repo", pr_number=1,
            persona_name="alice",
        )
        await cb.delete()
        mock_client.delete_comment.assert_not_called()

    @pytest.mark.asyncio()
    async def test_delete_handles_error(self) -> None:
        """delete() swallows errors without crashing."""
        mock_client = MagicMock()
        mock_client.delete_comment = AsyncMock(side_effect=RuntimeError("network error"))

        cb = GitHubProgressCallback(
            github_client=mock_client,
            owner="owner", repo="repo", pr_number=1,
            persona_name="alice",
        )
        cb._comment_id = 42

        # Should not raise
        await cb.delete()


# ---------------------------------------------------------------------------
# set_notification_dispatcher Tests
# ---------------------------------------------------------------------------


class TestSetNotificationDispatcher:
    """Tests for the set_notification_dispatcher method."""

    def test_setter_wires_dispatcher(self) -> None:
        """set_notification_dispatcher stores the dispatcher."""
        engine = _make_mock_engine()
        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=MagicMock(),
            persona_store=MagicMock(),
        )
        dispatcher = MagicMock()
        queue.set_notification_dispatcher(dispatcher)
        assert queue._notification_dispatcher is dispatcher


# ---------------------------------------------------------------------------
# Progress bar helper Tests
# ---------------------------------------------------------------------------


class TestBuildProgressBar:
    """Tests for the _build_progress_bar helper."""

    def test_zero_percent(self) -> None:
        from review_bot.server.queue import _build_progress_bar

        result = _build_progress_bar(0)
        assert "0%" in result
        assert "█" not in result

    def test_fifty_percent(self) -> None:
        from review_bot.server.queue import _build_progress_bar

        result = _build_progress_bar(50)
        assert "50%" in result

    def test_hundred_percent(self) -> None:
        from review_bot.server.queue import _build_progress_bar

        result = _build_progress_bar(100)
        assert "100%" in result
        assert "░" not in result

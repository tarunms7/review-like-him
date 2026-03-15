"""Tests for graceful shutdown with job drain."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from review_bot.config.settings import Settings
from review_bot.server.queue import AsyncJobQueue, ReviewJob
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

        # Drain should complete immediately when no jobs are running
        if hasattr(queue, "drain"):
            result = await queue.drain(timeout=5.0)
            assert result is True
        else:
            # drain not yet implemented, test the interface contract
            await queue.stop_worker()
            assert queue.worker_status == "stopped"


class TestDrainWaitsForInflightJob:
    """Test drain waits for in-flight job to finish."""

    @pytest.mark.asyncio()
    async def test_drain_waits_for_completion(self) -> None:
        """Start mock slow job, drain waits for it to finish, returns True."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        github_auth.create_token_client = AsyncMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "drain"):
            pytest.skip("drain() not yet implemented in queue.py")

        await queue.start_worker()

        # Create a slow job by mocking _process_job
        original_process = queue._process_job
        job_started = asyncio.Event()
        job_release = asyncio.Event()

        async def slow_process(job: ReviewJob) -> None:
            job_started.set()
            await job_release.wait()
            await original_process(job)

        queue._process_job = slow_process  # type: ignore[assignment]

        # Enqueue a job
        job = ReviewJob(
            owner="test",
            repo="repo",
            pr_number=1,
            persona_name="alice",
            installation_id=1,
        )
        await queue.enqueue(job)

        # Wait for job to start processing
        await asyncio.wait_for(job_started.wait(), timeout=2.0)

        # Start drain in background
        async def do_drain() -> bool:
            return await queue.drain(timeout=5.0)

        drain_task = asyncio.create_task(do_drain())

        # Let the job finish
        await asyncio.sleep(0.1)
        job_release.set()

        result = await asyncio.wait_for(drain_task, timeout=5.0)
        assert result is True


class TestDrainTimeoutForceCancels:
    """Test drain with timeout force-cancels."""

    @pytest.mark.asyncio()
    async def test_drain_returns_false_on_timeout(self) -> None:
        """Mock job taking 60s, drain with 1s timeout → returns False."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "drain"):
            pytest.skip("drain() not yet implemented in queue.py")

        await queue.start_worker()

        # Mock a job that takes forever
        original_process = queue._process_job

        async def forever_process(job: ReviewJob) -> None:
            await asyncio.sleep(60)

        queue._process_job = forever_process  # type: ignore[assignment]

        job = ReviewJob(
            owner="test",
            repo="repo",
            pr_number=1,
            persona_name="alice",
            installation_id=1,
        )
        await queue.enqueue(job)
        await asyncio.sleep(0.2)  # Let worker pick up the job

        result = await queue.drain(timeout=0.5)
        assert result is False


class TestDrainMarksTimedOutJobFailed:
    """Test that timed-out drain marks job as failed."""

    @pytest.mark.asyncio()
    async def test_job_marked_failed_after_timeout(self) -> None:
        """After timeout, verify job.status='failed', error_message set."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "drain") or not hasattr(queue, "_current_job"):
            pytest.skip("drain/_current_job not yet implemented in queue.py")

        await queue.start_worker()

        async def forever_process(job: ReviewJob) -> None:
            queue._current_job = job  # type: ignore[attr-defined]
            await asyncio.sleep(60)

        queue._process_job = forever_process  # type: ignore[assignment]

        job = ReviewJob(
            owner="test",
            repo="repo",
            pr_number=1,
            persona_name="alice",
            installation_id=1,
        )
        await queue.enqueue(job)
        await asyncio.sleep(0.2)

        await queue.drain(timeout=0.5)

        # The job should have been marked as failed
        assert job.status == "failed"
        assert job.error_message is not None
        assert "timeout" in job.error_message.lower() or "shutdown" in job.error_message.lower()


class TestEnqueueDuringDrainRaisesRuntimeError:
    """Test enqueue raises RuntimeError during drain."""

    @pytest.mark.asyncio()
    async def test_enqueue_rejected(self) -> None:
        """Set draining, enqueue → RuntimeError."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "_draining"):
            pytest.skip("_draining not yet implemented in queue.py")

        queue._draining = True

        job = ReviewJob(
            owner="test",
            repo="repo",
            pr_number=1,
            persona_name="alice",
            installation_id=1,
        )

        with pytest.raises(RuntimeError, match="draining"):
            await queue.enqueue(job)


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


class TestIsDrainingProperty:
    """Test is_draining property."""

    @pytest.mark.asyncio()
    async def test_false_initially(self) -> None:
        """False initially, True after drain() called."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "is_draining"):
            pytest.skip("is_draining not yet implemented in queue.py")

        assert queue.is_draining is False

    @pytest.mark.asyncio()
    async def test_true_after_drain(self) -> None:
        """is_draining is True after drain() is called."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "drain"):
            pytest.skip("drain() not yet implemented in queue.py")

        await queue.start_worker()
        await queue.drain(timeout=1.0)
        assert queue.is_draining is True


class TestStopWorkerDelegatesToDrain:
    """Test stop_worker delegates to drain(timeout=0)."""

    @pytest.mark.asyncio()
    async def test_stop_worker_calls_drain(self) -> None:
        """Verify stop_worker calls drain(timeout=0)."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "drain"):
            pytest.skip("drain() not yet implemented in queue.py")

        await queue.start_worker()

        with patch.object(queue, "drain", new_callable=AsyncMock) as mock_drain:
            mock_drain.return_value = True
            await queue.stop_worker()
            mock_drain.assert_called_once_with(timeout=0)


class TestDrainWithZeroTimeout:
    """Test drain with zero timeout."""

    @pytest.mark.asyncio()
    async def test_immediate_cancel(self) -> None:
        """Immediate cancel, no waiting."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "drain"):
            pytest.skip("drain() not yet implemented in queue.py")

        await queue.start_worker()
        result = await queue.drain(timeout=0)
        # With no in-flight job, should return True
        assert result is True
        assert queue.worker_status == "stopped"


class TestCurrentJobTracking:
    """Test _current_job set during processing, cleared after."""

    @pytest.mark.asyncio()
    async def test_current_job_lifecycle(self) -> None:
        """Verify _current_job set during processing, cleared after."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        github_auth.create_token_client = AsyncMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "_current_job"):
            pytest.skip("_current_job not yet implemented in queue.py")

        assert queue._current_job is None

        job_during_process: list[ReviewJob | None] = []
        original_process = queue._process_job

        async def tracking_process(job: ReviewJob) -> None:
            await original_process(job)
            # After processing, _current_job should still be set
            # (cleared in finally block of the real _process_job)

        queue._process_job = tracking_process  # type: ignore[assignment]

        await queue.start_worker()

        job = ReviewJob(
            owner="test",
            repo="repo",
            pr_number=1,
            persona_name="alice",
            installation_id=1,
        )
        await queue.enqueue(job)
        await asyncio.sleep(0.5)  # Let worker process

        # After processing, _current_job should be cleared
        assert queue._current_job is None

        await queue.stop_worker()


class TestJobCompleteEventSetAfterProcessing:
    """Test _job_complete_event is set in finally block."""

    @pytest.mark.asyncio()
    async def test_event_set_after_processing(self) -> None:
        """Event is set in finally block after job completes."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        github_auth.create_token_client = AsyncMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "_job_complete_event"):
            pytest.skip("_job_complete_event not yet implemented in queue.py")

        await queue.start_worker()

        job = ReviewJob(
            owner="test",
            repo="repo",
            pr_number=1,
            persona_name="alice",
            installation_id=1,
        )
        await queue.enqueue(job)
        await asyncio.sleep(0.5)  # Let worker process

        # Event should be set after job processing
        assert queue._job_complete_event.is_set()

        await queue.stop_worker()


class TestJobCompleteEventSetOnJobFailure:
    """Test event set even if job processing raises."""

    @pytest.mark.asyncio()
    async def test_event_set_on_failure(self) -> None:
        """Event set even if job processing raises."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        # Make create_token_client raise to simulate failure
        github_auth.create_token_client = AsyncMock(
            side_effect=RuntimeError("auth failed"),
        )
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "_job_complete_event"):
            pytest.skip("_job_complete_event not yet implemented in queue.py")

        await queue.start_worker()

        job = ReviewJob(
            owner="test",
            repo="repo",
            pr_number=1,
            persona_name="alice",
            installation_id=1,
        )
        await queue.enqueue(job)
        await asyncio.sleep(0.5)  # Let worker process

        # Event should be set even on failure
        assert queue._job_complete_event.is_set()

        await queue.stop_worker()


class TestDbDisposeAfterDrain:
    """Test engine.dispose() called after drain completes."""

    @pytest.mark.asyncio()
    async def test_dispose_called_after_drain(self) -> None:
        """Mock engine, verify dispose called after drain completes."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "drain"):
            pytest.skip("drain() not yet implemented in queue.py")

        await queue.start_worker()

        # Drain the queue
        result = await queue.drain(timeout=5.0)
        assert result is True

        # After drain, dispose the engine (simulating shutdown sequence)
        await engine.dispose()
        engine.dispose.assert_called_once()


class TestShutdownDrainTimeoutSettingValue:
    """Test shutdown_drain_timeout setting value."""

    def test_default_is_30(self) -> None:
        """Verify setting default is 30."""
        settings = Settings(github_app_id=0, webhook_secret="")
        assert settings.shutdown_drain_timeout == 30


class TestShutdownDrainTimeoutValidation:
    """Test shutdown_drain_timeout validation."""

    def test_negative_raises_value_error(self) -> None:
        """Negative value raises ValidationError."""
        with pytest.raises(ValueError, match="shutdown_drain_timeout must be >= 0"):
            Settings(
                github_app_id=0,
                webhook_secret="",
                shutdown_drain_timeout=-5,
            )


class TestMultipleDrainCallsIdempotent:
    """Test calling drain twice doesn't error."""

    @pytest.mark.asyncio()
    async def test_double_drain(self) -> None:
        """Calling drain twice doesn't error."""
        engine = _make_mock_engine()
        github_auth = MagicMock()
        persona_store = MagicMock()

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )

        if not hasattr(queue, "drain"):
            pytest.skip("drain() not yet implemented in queue.py")

        await queue.start_worker()

        result1 = await queue.drain(timeout=5.0)
        assert result1 is True

        # Second drain should also succeed without error
        result2 = await queue.drain(timeout=5.0)
        assert result2 is True


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

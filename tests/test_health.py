"""Tests for health check endpoints and queue properties."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from httpx import ASGITransport, AsyncClient

from review_bot.server.health import (
    CheckResult,
    _check_database,
    _check_github_app,
    _check_github_rate_limit,
    _check_queue,
    set_start_time,
)
from review_bot.server.queue import AsyncJobQueue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_engine():
    """Create a mock AsyncEngine that passes SELECT 1."""
    engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value=None)

    # engine.connect() returns an async context manager
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    engine.connect = MagicMock(return_value=cm)

    return engine


@pytest.fixture()
def mock_engine_failing():
    """Create a mock AsyncEngine that raises OperationalError."""
    from sqlalchemy.exc import OperationalError

    engine = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(
        side_effect=OperationalError("select", {}, Exception("connection refused")),
    )
    cm.__aexit__ = AsyncMock(return_value=False)
    engine.connect = MagicMock(return_value=cm)
    return engine


@pytest.fixture()
def mock_engine_timeout():
    """Create a mock AsyncEngine that times out."""
    engine = MagicMock()
    mock_conn = AsyncMock()

    async def slow_execute(*args, **kwargs):
        await asyncio.sleep(10)

    mock_conn.execute = slow_execute

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    engine.connect = MagicMock(return_value=cm)
    return engine


@pytest.fixture()
def mock_job_queue():
    """Create a mock AsyncJobQueue with healthy defaults."""
    queue = MagicMock(spec=AsyncJobQueue)
    type(queue).queue_depth = PropertyMock(return_value=0)
    type(queue).worker_status = PropertyMock(return_value="running")
    type(queue).current_job_id = PropertyMock(return_value=None)
    return queue


@pytest.fixture()
def mock_github_auth():
    """Create a mock GitHubAppAuth."""
    auth = MagicMock()
    auth._app_id = "123456"
    auth._token_cache = {1: ("token", 9999999999.0), 2: ("token2", 9999999999.0), 3: ("token3", 9999999999.0)}
    return auth


@pytest.fixture()
def mock_rate_limit_tracker():
    """Create a mock RateLimitTracker with sample data."""
    tracker = MagicMock()
    state = MagicMock()
    state.remaining = 4985
    state.limit = 5000
    state.resource = "core"
    tracker.snapshot.return_value = {"core": state}
    return tracker


def _create_test_app(
    engine=None,
    job_queue=None,
    github_auth=None,
    rate_limit_tracker=None,
):
    """Create a minimal FastAPI app with health router for testing."""
    from fastapi import FastAPI

    from review_bot.server.health import router

    app = FastAPI()
    app.include_router(router)

    # Set up app.state with provided or default mocks
    if engine is None:
        engine = MagicMock()
    if job_queue is None:
        jq = MagicMock(spec=AsyncJobQueue)
        type(jq).queue_depth = PropertyMock(return_value=0)
        type(jq).worker_status = PropertyMock(return_value="running")
        type(jq).current_job_id = PropertyMock(return_value=None)
        job_queue = jq
    if github_auth is None:
        github_auth = MagicMock()
        github_auth._app_id = "123456"
        github_auth._token_cache = {}

    app.state.db_engine = engine
    app.state.job_queue = job_queue
    app.state.github_auth = github_auth
    app.state.rate_limit_tracker = rate_limit_tracker

    return app


# ---------------------------------------------------------------------------
# CheckResult unit tests
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_to_dict_basic(self):
        result = CheckResult(status="pass", detail="OK")
        d = result.to_dict()
        assert d == {"status": "pass", "detail": "OK"}
        assert "duration_ms" not in d

    def test_to_dict_with_duration(self):
        result = CheckResult(status="fail", detail="Error", duration_ms=5.2)
        d = result.to_dict()
        assert d == {"status": "fail", "detail": "Error", "duration_ms": 5.2}


# ---------------------------------------------------------------------------
# Queue property tests
# ---------------------------------------------------------------------------


class TestQueueProperties:
    def test_queue_worker_status_running(self):
        """Verify property returns 'running' when task active."""
        queue = MagicMock(spec=AsyncJobQueue)
        queue._queue = asyncio.Queue()
        queue._worker_task = MagicMock()
        queue._worker_task.done.return_value = False

        # Call the real property
        status = AsyncJobQueue.worker_status.fget(queue)
        assert status == "running"

    def test_queue_worker_status_stopped(self):
        """Verify property returns 'stopped' when task is None."""
        queue = MagicMock(spec=AsyncJobQueue)
        queue._queue = asyncio.Queue()
        queue._worker_task = None

        status = AsyncJobQueue.worker_status.fget(queue)
        assert status == "stopped"

    def test_queue_worker_status_dead(self):
        """Verify property returns 'dead' when task.done() is True."""
        queue = MagicMock(spec=AsyncJobQueue)
        queue._queue = asyncio.Queue()
        queue._worker_task = MagicMock()
        queue._worker_task.done.return_value = True

        status = AsyncJobQueue.worker_status.fget(queue)
        assert status == "dead"

    def test_queue_depth_reporting(self):
        """Enqueue items and verify queue_depth reflects count."""
        q = asyncio.Queue()
        q.put_nowait("job1")
        q.put_nowait("job2")
        q.put_nowait("job3")

        queue = MagicMock(spec=AsyncJobQueue)
        queue._queue = q

        depth = AsyncJobQueue.queue_depth.fget(queue)
        assert depth == 3

    def test_current_job_id_property(self):
        """Verify current_job_id returns stored value."""
        queue = MagicMock(spec=AsyncJobQueue)
        queue._current_job_id = "abc-123"

        job_id = AsyncJobQueue.current_job_id.fget(queue)
        assert job_id == "abc-123"

    def test_current_job_id_none_when_idle(self):
        """Verify current_job_id returns None when no job processing."""
        queue = MagicMock(spec=AsyncJobQueue)
        queue._current_job_id = None

        job_id = AsyncJobQueue.current_job_id.fget(queue)
        assert job_id is None


# ---------------------------------------------------------------------------
# Individual check function tests
# ---------------------------------------------------------------------------


class TestCheckFunctions:
    @pytest.mark.asyncio()
    async def test_check_database_pass(self, mock_engine):
        result = await _check_database(mock_engine)
        assert result.status == "pass"
        assert "Connected" in result.detail
        assert result.duration_ms is not None

    @pytest.mark.asyncio()
    async def test_check_database_operational_error(self, mock_engine_failing):
        result = await _check_database(mock_engine_failing)
        assert result.status == "fail"
        assert "Database error" in result.detail

    @pytest.mark.asyncio()
    async def test_check_database_timeout(self, mock_engine_timeout):
        result = await _check_database(mock_engine_timeout)
        assert result.status == "fail"
        assert "timed out" in result.detail

    @pytest.mark.asyncio()
    async def test_check_queue_pass(self, mock_job_queue):
        result = await _check_queue(mock_job_queue)
        assert result.status == "pass"
        assert "queue_depth=0" in result.detail
        assert "worker_status=running" in result.detail

    @pytest.mark.asyncio()
    async def test_check_queue_dead_worker(self):
        queue = MagicMock(spec=AsyncJobQueue)
        type(queue).queue_depth = PropertyMock(return_value=0)
        type(queue).worker_status = PropertyMock(return_value="dead")
        type(queue).current_job_id = PropertyMock(return_value=None)

        result = await _check_queue(queue)
        assert result.status == "fail"

    @pytest.mark.asyncio()
    async def test_check_queue_warn_high_depth(self):
        queue = MagicMock(spec=AsyncJobQueue)
        type(queue).queue_depth = PropertyMock(return_value=15)
        type(queue).worker_status = PropertyMock(return_value="running")
        type(queue).current_job_id = PropertyMock(return_value="job-1")

        result = await _check_queue(queue)
        assert result.status == "warn"
        assert "queue_depth=15" in result.detail

    @pytest.mark.asyncio()
    async def test_check_github_rate_limit_pass(self, mock_rate_limit_tracker):
        result = await _check_github_rate_limit(mock_rate_limit_tracker)
        assert result.status == "pass"
        assert "4985/5000" in result.detail

    @pytest.mark.asyncio()
    async def test_check_github_rate_limit_warn(self):
        tracker = MagicMock()
        state = MagicMock()
        state.remaining = 5
        state.limit = 5000
        state.resource = "core"
        tracker.snapshot.return_value = {"core": state}

        result = await _check_github_rate_limit(tracker)
        assert result.status == "warn"

    @pytest.mark.asyncio()
    async def test_check_github_rate_limit_no_data(self):
        tracker = MagicMock()
        tracker.snapshot.return_value = {}

        result = await _check_github_rate_limit(tracker)
        assert result.status == "pass"
        assert "No rate limit data available yet" in result.detail

    @pytest.mark.asyncio()
    async def test_check_github_rate_limit_none_tracker(self):
        result = await _check_github_rate_limit(None)
        assert result.status == "pass"
        assert "No rate limit data available yet" in result.detail

    @pytest.mark.asyncio()
    async def test_check_github_app(self, mock_github_auth):
        result = await _check_github_app(mock_github_auth)
        assert result.status == "pass"
        assert "App ID: 123456" in result.detail
        assert "installations: 3" in result.detail


# ---------------------------------------------------------------------------
# Endpoint integration tests
# ---------------------------------------------------------------------------


class TestHealthzEndpoint:
    @pytest.mark.asyncio()
    async def test_healthz_always_200(self):
        """Liveness probe returns 200 regardless of state."""
        app = _create_test_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200
            assert resp.json() == {"status": "alive"}


class TestReadyzEndpoint:
    @pytest.mark.asyncio()
    async def test_readyz_healthy(self, mock_engine, mock_job_queue):
        """Readiness returns 200 when DB and worker are healthy."""
        app = _create_test_app(engine=mock_engine, job_queue=mock_job_queue)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert "database" in data["checks"]
            assert "worker" in data["checks"]

    @pytest.mark.asyncio()
    async def test_readyz_db_down_returns_503(self, mock_engine_failing):
        """Readiness probe with DB failure returns 503."""
        jq = MagicMock(spec=AsyncJobQueue)
        type(jq).worker_status = PropertyMock(return_value="running")

        app = _create_test_app(engine=mock_engine_failing, job_queue=jq)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "not_ready"

    @pytest.mark.asyncio()
    async def test_readyz_worker_dead_returns_503(self, mock_engine):
        """Readiness probe with dead worker returns 503."""
        jq = MagicMock(spec=AsyncJobQueue)
        type(jq).worker_status = PropertyMock(return_value="dead")

        app = _create_test_app(engine=mock_engine, job_queue=jq)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 503


class TestHealthEndpoint:
    @pytest.mark.asyncio()
    async def test_health_all_passing(self, mock_engine, mock_job_queue, mock_github_auth):
        """All dependencies healthy → 200 with full response schema."""
        set_start_time()
        app = _create_test_app(
            engine=mock_engine,
            job_queue=mock_job_queue,
            github_auth=mock_github_auth,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "healthy"
            assert data["version"] == "0.1.0"
            assert data["uptime_seconds"] >= 0
            checks = data["checks"]
            assert "database" in checks
            assert "queue" in checks
            assert "github_rate_limit" in checks
            assert "github_app" in checks

    @pytest.mark.asyncio()
    async def test_health_response_schema(self, mock_engine, mock_job_queue, mock_github_auth):
        """Validate all required fields present in health response."""
        set_start_time()
        app = _create_test_app(
            engine=mock_engine,
            job_queue=mock_job_queue,
            github_auth=mock_github_auth,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            data = resp.json()

            # Top-level fields
            assert "status" in data
            assert "version" in data
            assert "uptime_seconds" in data
            assert "checks" in data

            # Each check has status and detail
            for check_name in ["database", "queue", "github_rate_limit", "github_app"]:
                check = data["checks"][check_name]
                assert "status" in check
                assert "detail" in check

    @pytest.mark.asyncio()
    async def test_health_db_down_returns_503(self, mock_engine_failing, mock_job_queue, mock_github_auth):
        """DB check raising OperationalError → 503."""
        set_start_time()
        app = _create_test_app(
            engine=mock_engine_failing,
            job_queue=mock_job_queue,
            github_auth=mock_github_auth,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "unhealthy"
            assert data["checks"]["database"]["status"] == "fail"

    @pytest.mark.asyncio()
    async def test_health_db_timeout(self, mock_engine_timeout, mock_job_queue, mock_github_auth):
        """Slow DB query exceeding 5s timeout → fail status."""
        set_start_time()
        app = _create_test_app(
            engine=mock_engine_timeout,
            job_queue=mock_job_queue,
            github_auth=mock_github_auth,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 503
            data = resp.json()
            assert data["checks"]["database"]["status"] == "fail"
            assert "timed out" in data["checks"]["database"]["detail"]

    @pytest.mark.asyncio()
    async def test_health_worker_stopped(self, mock_engine, mock_github_auth):
        """Worker stopped → fail check, 503."""
        set_start_time()
        jq = MagicMock(spec=AsyncJobQueue)
        type(jq).queue_depth = PropertyMock(return_value=0)
        type(jq).worker_status = PropertyMock(return_value="stopped")
        type(jq).current_job_id = PropertyMock(return_value=None)

        app = _create_test_app(
            engine=mock_engine, job_queue=jq, github_auth=mock_github_auth
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 503
            assert resp.json()["checks"]["queue"]["status"] == "fail"

    @pytest.mark.asyncio()
    async def test_health_degraded_on_rate_limit_warn(
        self, mock_engine, mock_job_queue, mock_github_auth
    ):
        """Rate limit low but DB ok → status 'healthy' with warn check."""
        set_start_time()
        tracker = MagicMock()
        state = MagicMock()
        state.remaining = 5
        state.limit = 5000
        state.resource = "core"
        tracker.snapshot.return_value = {"core": state}

        app = _create_test_app(
            engine=mock_engine,
            job_queue=mock_job_queue,
            github_auth=mock_github_auth,
            rate_limit_tracker=tracker,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "healthy"
            assert data["checks"]["github_rate_limit"]["status"] == "warn"

    @pytest.mark.asyncio()
    async def test_health_no_rate_limit_data(
        self, mock_engine, mock_job_queue, mock_github_auth
    ):
        """Empty tracker → 'pass' with informative detail."""
        set_start_time()
        app = _create_test_app(
            engine=mock_engine,
            job_queue=mock_job_queue,
            github_auth=mock_github_auth,
            rate_limit_tracker=None,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            rl_check = resp.json()["checks"]["github_rate_limit"]
            assert rl_check["status"] == "pass"
            assert "No rate limit data" in rl_check["detail"]

    @pytest.mark.asyncio()
    async def test_uptime_calculation(self, mock_engine, mock_job_queue, mock_github_auth):
        """Uptime increases over time."""
        set_start_time()
        app = _create_test_app(
            engine=mock_engine,
            job_queue=mock_job_queue,
            github_auth=mock_github_auth,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp1 = await client.get("/health")
            uptime1 = resp1.json()["uptime_seconds"]
            assert uptime1 >= 0

            # Small delay then check again
            await asyncio.sleep(0.05)
            resp2 = await client.get("/health")
            uptime2 = resp2.json()["uptime_seconds"]
            assert uptime2 >= uptime1

    @pytest.mark.asyncio()
    async def test_queue_depth_reporting(self, mock_engine, mock_github_auth):
        """Health reports correct queue depth."""
        set_start_time()
        jq = MagicMock(spec=AsyncJobQueue)
        type(jq).queue_depth = PropertyMock(return_value=5)
        type(jq).worker_status = PropertyMock(return_value="running")
        type(jq).current_job_id = PropertyMock(return_value="job-42")

        app = _create_test_app(
            engine=mock_engine, job_queue=jq, github_auth=mock_github_auth
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            data = resp.json()
            assert "queue_depth=5" in data["checks"]["queue"]["detail"]
            assert "current_job_id=job-42" in data["checks"]["queue"]["detail"]

    @pytest.mark.asyncio()
    async def test_concurrent_health_checks(self, mock_engine, mock_job_queue, mock_github_auth):
        """Multiple simultaneous requests don't interfere."""
        set_start_time()
        app = _create_test_app(
            engine=mock_engine,
            job_queue=mock_job_queue,
            github_auth=mock_github_auth,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                client.get("/health"),
                client.get("/health"),
                client.get("/health"),
                client.get("/healthz"),
                client.get("/readyz"),
            )
            for resp in responses:
                assert resp.status_code == 200
                assert "status" in resp.json()

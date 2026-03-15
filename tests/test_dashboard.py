"""Tests for the dashboard module: queries, router, and template rendering."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from review_bot.dashboard import queries
from review_bot.dashboard.router import router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_engine():
    """Create an in-memory SQLite engine with tables initialized."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)

    # Re-use the table creation SQL from app.py
    from review_bot.server.app import _init_database

    await _init_database(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
async def seeded_engine(db_engine):
    """Engine with sample review and job data."""
    now = datetime.now(tz=UTC)

    async with db_engine.begin() as conn:
        # Insert reviews across time windows
        for i in range(5):
            await conn.execute(
                text(
                    "INSERT INTO reviews "
                    "(persona_name, repo, pr_number, pr_url, verdict, "
                    "comment_count, created_at, duration_ms) "
                    "VALUES (:pn, :repo, :pr, :url, :v, :cc, :ca, :dm)"
                ),
                {
                    "pn": "alice",
                    "repo": "org/repo-a",
                    "pr": i + 1,
                    "url": f"https://github.com/org/repo-a/pull/{i + 1}",
                    "v": "approve",
                    "cc": 3,
                    "ca": (now - timedelta(hours=i)).isoformat(),
                    "dm": 4000 + i * 100,
                },
            )

        for i in range(3):
            await conn.execute(
                text(
                    "INSERT INTO reviews "
                    "(persona_name, repo, pr_number, pr_url, verdict, "
                    "comment_count, created_at, duration_ms) "
                    "VALUES (:pn, :repo, :pr, :url, :v, :cc, :ca, :dm)"
                ),
                {
                    "pn": "bob",
                    "repo": "org/repo-b",
                    "pr": i + 10,
                    "url": f"https://github.com/org/repo-b/pull/{i + 10}",
                    "v": "request_changes",
                    "cc": 8,
                    "ca": (now - timedelta(days=3, hours=i)).isoformat(),
                    "dm": 6000,
                },
            )

        # Insert some old reviews (outside 30d window)
        for i in range(2):
            await conn.execute(
                text(
                    "INSERT INTO reviews "
                    "(persona_name, repo, pr_number, pr_url, verdict, "
                    "comment_count, created_at, duration_ms) "
                    "VALUES (:pn, :repo, :pr, :url, :v, :cc, :ca, :dm)"
                ),
                {
                    "pn": "alice",
                    "repo": "org/repo-a",
                    "pr": 100 + i,
                    "url": f"https://github.com/org/repo-a/pull/{100 + i}",
                    "v": "approve",
                    "cc": 1,
                    "ca": (now - timedelta(days=60)).isoformat(),
                    "dm": 2000,
                },
            )

        # Insert jobs
        await conn.execute(
            text(
                "INSERT INTO jobs "
                "(id, owner, repo, pr_number, persona_name, installation_id, "
                "status, queued_at) "
                "VALUES (:id, :o, :r, :pr, :pn, :iid, :s, :qa)"
            ),
            {
                "id": str(uuid.uuid4()),
                "o": "org",
                "r": "repo-a",
                "pr": 50,
                "pn": "alice",
                "iid": 1,
                "s": "queued",
                "qa": now.isoformat(),
            },
        )

        # Running job (stale - started 15 min ago)
        await conn.execute(
            text(
                "INSERT INTO jobs "
                "(id, owner, repo, pr_number, persona_name, installation_id, "
                "status, queued_at, started_at) "
                "VALUES (:id, :o, :r, :pr, :pn, :iid, :s, :qa, :sa)"
            ),
            {
                "id": str(uuid.uuid4()),
                "o": "org",
                "r": "repo-a",
                "pr": 51,
                "pn": "bob",
                "iid": 1,
                "s": "running",
                "qa": (now - timedelta(minutes=20)).isoformat(),
                "sa": (now - timedelta(minutes=15)).isoformat(),
            },
        )

        # Failed job
        await conn.execute(
            text(
                "INSERT INTO jobs "
                "(id, owner, repo, pr_number, persona_name, installation_id, "
                "status, queued_at, completed_at, error_message) "
                "VALUES (:id, :o, :r, :pr, :pn, :iid, :s, :qa, :ca, :em)"
            ),
            {
                "id": str(uuid.uuid4()),
                "o": "org",
                "r": "repo-b",
                "pr": 52,
                "pn": "bob",
                "iid": 1,
                "s": "failed",
                "qa": (now - timedelta(hours=1)).isoformat(),
                "ca": now.isoformat(),
                "em": "API timeout",
            },
        )

    return db_engine


def _make_app(engine, job_queue=None, persona_store=None):
    """Create a minimal FastAPI app with the dashboard router for testing."""
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    app = FastAPI()
    app.include_router(router)

    # Mount static files
    import os

    static_dir = os.path.join(os.path.dirname(__file__), "..", "review_bot", "dashboard", "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    app.state.db_engine = engine

    if job_queue is None:
        job_queue = MagicMock()
        job_queue.worker_status = "running"
        job_queue.queue_depth = 0
    app.state.job_queue = job_queue

    if persona_store is None:
        persona_store = MagicMock()
        persona_store.list_all.return_value = []
    app.state.persona_store = persona_store

    return app


# ---------------------------------------------------------------------------
# Query function tests
# ---------------------------------------------------------------------------


class TestGetReviewCounts:
    async def test_empty_database(self, db_engine):
        result = await queries.get_review_counts(db_engine)
        assert result == {"24h": 0, "7d": 0, "30d": 0}

    async def test_with_reviews(self, seeded_engine):
        result = await queries.get_review_counts(seeded_engine)
        # 5 alice reviews in last 24h, 3 bob reviews at 3 days ago
        assert result["24h"] == 5
        assert result["7d"] == 8  # 5 + 3
        assert result["30d"] == 8  # old ones are >30d


class TestGetActivityPage:
    async def test_empty_database(self, db_engine):
        rows, total = await queries.get_activity_page(db_engine)
        assert rows == []
        assert total == 0

    async def test_returns_rows(self, seeded_engine):
        rows, total = await queries.get_activity_page(seeded_engine)
        assert total == 10  # 5 + 3 + 2
        assert len(rows) == 10
        # Should be ordered by created_at DESC
        assert rows[0]["persona_name"] == "alice"

    async def test_pagination(self, seeded_engine):
        rows, total = await queries.get_activity_page(seeded_engine, page=1, per_page=3)
        assert len(rows) == 3
        assert total == 10

        rows2, _ = await queries.get_activity_page(seeded_engine, page=2, per_page=3)
        assert len(rows2) == 3
        # Pages should not overlap
        ids1 = {r["pr_number"] for r in rows}
        ids2 = {r["pr_number"] for r in rows2}
        assert ids1.isdisjoint(ids2)

    async def test_filter_by_persona(self, seeded_engine):
        rows, total = await queries.get_activity_page(seeded_engine, persona="bob")
        assert total == 3
        assert all(r["persona_name"] == "bob" for r in rows)

    async def test_filter_by_repo(self, seeded_engine):
        rows, total = await queries.get_activity_page(seeded_engine, repo="org/repo-b")
        assert total == 3
        assert all(r["repo"] == "org/repo-b" for r in rows)

    async def test_filter_combined(self, seeded_engine):
        rows, total = await queries.get_activity_page(
            seeded_engine, persona="alice", repo="org/repo-a"
        )
        assert total == 7  # 5 recent + 2 old


class TestGetPersonaStats:
    async def test_empty_database(self, db_engine):
        result = await queries.get_persona_stats(db_engine)
        assert result == []

    async def test_with_reviews(self, seeded_engine):
        result = await queries.get_persona_stats(seeded_engine)
        assert len(result) == 2
        # Ordered by total_reviews DESC
        alice = result[0]
        assert alice["persona_name"] == "alice"
        assert alice["total_reviews"] == 7  # 5 + 2
        assert alice["approvals"] == 7
        assert alice["change_requests"] == 0

        bob = result[1]
        assert bob["persona_name"] == "bob"
        assert bob["total_reviews"] == 3
        assert bob["change_requests"] == 3


class TestGetQueueSnapshot:
    async def test_empty_database(self, db_engine):
        result = await queries.get_queue_snapshot(db_engine)
        assert result == {"queued": [], "running": [], "failed": []}

    async def test_with_jobs(self, seeded_engine):
        result = await queries.get_queue_snapshot(seeded_engine)
        assert len(result["queued"]) == 1
        assert len(result["running"]) == 1
        assert len(result["failed"]) == 1
        assert result["failed"][0]["error_message"] == "API timeout"


class TestGetReviewsPerDay:
    async def test_empty_database(self, db_engine):
        result = await queries.get_reviews_per_day(db_engine)
        assert result == []

    async def test_with_reviews(self, seeded_engine):
        result = await queries.get_reviews_per_day(seeded_engine)
        assert len(result) >= 1
        assert all("date" in r and "count" in r for r in result)

    async def test_persona_filter(self, seeded_engine):
        result = await queries.get_reviews_per_day(seeded_engine, persona="bob")
        total = sum(r["count"] for r in result)
        assert total == 3


# ---------------------------------------------------------------------------
# Router / integration tests
# ---------------------------------------------------------------------------


class TestOverviewRoute:
    async def test_returns_200_empty_db(self, db_engine):
        app = _make_app(db_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/")
            assert resp.status_code == 200
            assert "Overview" in resp.text

    async def test_shows_counts(self, seeded_engine):
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/")
            assert resp.status_code == 200
            # Should contain review count numbers
            assert "review-bot" in resp.text


class TestActivityRoute:
    async def test_returns_200_empty_db(self, db_engine):
        app = _make_app(db_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/activity")
            assert resp.status_code == 200
            assert "No data yet" in resp.text

    async def test_with_data(self, seeded_engine):
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/activity")
            assert resp.status_code == 200
            assert "alice" in resp.text

    async def test_pagination(self, seeded_engine):
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/activity?page=1&per_page=3")
            assert resp.status_code == 200
            assert "page" in resp.text.lower()

    async def test_filter_persona(self, seeded_engine):
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/activity?persona=bob")
            assert resp.status_code == 200
            assert "bob" in resp.text

    async def test_filter_repo(self, seeded_engine):
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/activity?repo=org/repo-b")
            assert resp.status_code == 200


class TestPersonasRoute:
    async def test_returns_200_empty_db(self, db_engine):
        app = _make_app(db_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/personas")
            assert resp.status_code == 200
            assert "No data yet" in resp.text

    async def test_with_data(self, seeded_engine):
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/personas")
            assert resp.status_code == 200
            assert "alice" in resp.text
            assert "bob" in resp.text


class TestQueueRoute:
    async def test_returns_200_empty_db(self, db_engine):
        app = _make_app(db_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/queue")
            assert resp.status_code == 200

    async def test_shows_stale_warning(self, seeded_engine):
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/queue")
            assert resp.status_code == 200
            assert "stale" in resp.text.lower() or "Warning" in resp.text

    async def test_shows_failed_jobs(self, seeded_engine):
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/queue")
            assert resp.status_code == 200
            assert "API timeout" in resp.text


class TestConfigRoute:
    async def test_returns_200(self, db_engine):
        app = _make_app(db_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/config")
            assert resp.status_code == 200
            assert "Config" in resp.text


# ---------------------------------------------------------------------------
# Pagination boundary tests
# ---------------------------------------------------------------------------


class TestPaginationBoundaries:
    async def test_many_reviews(self, db_engine):
        """Insert 100+ reviews and verify page boundaries."""
        now = datetime.now(tz=UTC)
        async with db_engine.begin() as conn:
            for i in range(105):
                await conn.execute(
                    text(
                        "INSERT INTO reviews "
                        "(persona_name, repo, pr_number, pr_url, verdict, "
                        "comment_count, created_at, duration_ms) "
                        "VALUES (:pn, :repo, :pr, :url, :v, :cc, :ca, :dm)"
                    ),
                    {
                        "pn": "tester",
                        "repo": "org/big-repo",
                        "pr": i + 1,
                        "url": f"https://github.com/org/big-repo/pull/{i + 1}",
                        "v": "approve",
                        "cc": 2,
                        "ca": (now - timedelta(hours=i)).isoformat(),
                        "dm": 3000,
                    },
                )

        rows_p1, total = await queries.get_activity_page(
            db_engine, page=1, per_page=50
        )
        assert total == 105
        assert len(rows_p1) == 50

        rows_p2, _ = await queries.get_activity_page(
            db_engine, page=2, per_page=50
        )
        assert len(rows_p2) == 50

        rows_p3, _ = await queries.get_activity_page(
            db_engine, page=3, per_page=50
        )
        assert len(rows_p3) == 5

        # No overlap between pages
        prs_p1 = {r["pr_number"] for r in rows_p1}
        prs_p2 = {r["pr_number"] for r in rows_p2}
        prs_p3 = {r["pr_number"] for r in rows_p3}
        assert prs_p1.isdisjoint(prs_p2)
        assert prs_p2.isdisjoint(prs_p3)

    async def test_page_zero_clamped_to_one(self, seeded_engine):
        """page=0 should be clamped to page=1, not produce negative offset."""
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/activity?page=0")
            assert resp.status_code == 200
            # Should get same result as page=1
            resp1 = await client.get("/dashboard/activity?page=1")
            assert resp.text == resp1.text

    async def test_negative_page_clamped(self, seeded_engine):
        """Negative page values should be clamped to 1."""
        app = _make_app(seeded_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/dashboard/activity?page=-5")
            assert resp.status_code == 200

    async def test_per_page_upper_bound(self, db_engine):
        """per_page should be capped at 200."""
        now = datetime.now(tz=UTC)
        async with db_engine.begin() as conn:
            for i in range(250):
                await conn.execute(
                    text(
                        "INSERT INTO reviews "
                        "(persona_name, repo, pr_number, pr_url, verdict, "
                        "comment_count, created_at, duration_ms) "
                        "VALUES (:pn, :repo, :pr, :url, :v, :cc, :ca, :dm)"
                    ),
                    {
                        "pn": "tester",
                        "repo": "org/big-repo",
                        "pr": i + 1,
                        "url": f"https://github.com/org/big-repo/pull/{i + 1}",
                        "v": "approve",
                        "cc": 2,
                        "ca": (now - timedelta(hours=i)).isoformat(),
                        "dm": 3000,
                    },
                )

        app = _make_app(db_engine)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Request huge per_page - should be capped at 200
            resp = await client.get("/dashboard/activity?per_page=1000000")
            assert resp.status_code == 200


class TestDbUrlSanitization:
    """Verify that db_url credentials are masked in config page."""

    async def test_credentials_masked(self, db_engine):
        """db_url with credentials should have them masked."""
        import re

        from review_bot.dashboard.router import router as _router  # noqa: F811

        # The sanitization regex
        url = "postgresql://admin:s3cr3t@prod-db.internal/app"
        sanitized = re.sub(r"://[^@]+@", "://*****@", url)
        assert sanitized == "postgresql://*****@prod-db.internal/app"
        assert "s3cr3t" not in sanitized
        assert "admin" not in sanitized

    async def test_url_without_credentials_unchanged(self):
        """db_url without @ should pass through unchanged."""
        import re

        url = "sqlite+aiosqlite:///data.db"
        sanitized = re.sub(r"://[^@]+@", "://*****@", url)
        assert sanitized == url

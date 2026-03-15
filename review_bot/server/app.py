"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from review_bot.config.paths import ensure_directories
from review_bot.config.settings import Settings
from review_bot.github.app import GitHubAppAuth
from review_bot.persona.store import PersonaStore
from review_bot.server.health import router as health_router
from review_bot.server.health import set_start_time
from review_bot.server.queue import AsyncJobQueue
from review_bot.server.status import router as status_router
from review_bot.server.webhooks import configure, router

logger = logging.getLogger("review-bot")

# SQL for creating database tables
_CREATE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_name TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        pr_url TEXT NOT NULL,
        verdict TEXT NOT NULL,
        comment_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        duration_ms INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        persona_name TEXT NOT NULL,
        installation_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        error_message TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_stats (
        persona_name TEXT PRIMARY KEY,
        total_reviews INTEGER NOT NULL DEFAULT 0,
        repos_mined INTEGER NOT NULL DEFAULT 0,
        comments_mined INTEGER NOT NULL DEFAULT 0,
        last_mined_at TEXT,
        last_review_at TEXT
    )
    """,
]

# SQL for creating indexes on frequently queried columns
_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_reviews_persona_name ON reviews(persona_name)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_pr_number ON reviews(pr_number)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_repo ON reviews(repo)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_created_at ON reviews(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_persona_name ON jobs(persona_name)",
]


async def _init_database(engine: AsyncEngine) -> None:
    """Create database tables and indexes if they don't exist."""
    async with engine.begin() as conn:
        for sql in _CREATE_TABLES_SQL:
            await conn.execute(text(sql))
        for sql in _CREATE_INDEXES_SQL:
            await conn.execute(text(sql))
    logger.info("Database tables and indexes initialized")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: Application settings. If None, loaded from environment.

    Returns:
        Configured FastAPI application.
    """
    if settings is None:
        settings = Settings()

    # Validate server configuration before starting
    errors = settings.validate_for_server()
    if errors:
        for err in errors:
            logger.error("Config validation error: %s", err)
        raise RuntimeError(
            "Server configuration is invalid:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )

    # Ensure data directories exist
    ensure_directories()

    # Store settings and components on app state for access in lifespan
    app_settings = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Manage application startup and shutdown."""
        logger.info("Starting review-bot server")

        # Initialize database engine
        engine = create_async_engine(app_settings.db_url, echo=False)
        await _init_database(engine)

        # Initialize GitHub App auth
        github_auth = GitHubAppAuth(
            app_id=str(app_settings.github_app_id),
            private_key_path=str(app_settings.private_key_path),
        )

        # Initialize persona store
        persona_store = PersonaStore()

        # Initialize and start job queue
        job_queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=github_auth,
            persona_store=persona_store,
        )
        await job_queue.start_worker()

        # Initialize rate limit tracker placeholder (task-2 wires into API client)
        rate_limit_tracker = None
        try:
            from review_bot.github.rate_limits import RateLimitTracker

            rate_limit_tracker = RateLimitTracker()
        except ImportError:
            logger.debug("RateLimitTracker not yet available, using None placeholder")

        # Configure webhook module with runtime dependencies
        configure(
            job_queue=job_queue,
            webhook_secret=app_settings.webhook_secret,
            persona_store=persona_store,
        )

        # Record app start time for uptime calculation
        set_start_time()

        # Store on app state for access in tests/extensions
        app.state.db_engine = engine
        app.state.job_queue = job_queue
        app.state.github_auth = github_auth
        app.state.persona_store = persona_store
        app.state.rate_limit_tracker = rate_limit_tracker

        yield

        # Shutdown
        logger.info("Shutting down review-bot server")
        await job_queue.stop_worker()
        await engine.dispose()

    app = FastAPI(
        title="review-bot",
        description="AI-powered code review bot that mimics real reviewers",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(router)
    app.include_router(health_router)
    app.include_router(status_router)

    return app

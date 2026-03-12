"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from review_bot.config.settings import Settings
from review_bot.github.app import GitHubAppAuth
from review_bot.persona.store import PersonaStore
from review_bot.server.queue import AsyncJobQueue
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


async def _init_database(engine: AsyncEngine) -> None:
    """Create database tables if they don't exist."""
    async with engine.begin() as conn:
        for sql in _CREATE_TABLES_SQL:
            await conn.execute(text(sql))
    logger.info("Database tables initialized")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: Application settings. If None, loaded from environment.

    Returns:
        Configured FastAPI application.
    """
    if settings is None:
        settings = Settings()

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

        # Configure webhook module with runtime dependencies
        configure(
            job_queue=job_queue,
            webhook_secret=app_settings.webhook_secret,
            persona_store=persona_store,
        )

        # Store on app state for access in tests/extensions
        app.state.db_engine = engine
        app.state.job_queue = job_queue
        app.state.github_auth = github_auth
        app.state.persona_store = persona_store

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

    return app

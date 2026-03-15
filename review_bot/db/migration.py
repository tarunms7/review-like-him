"""Database migration utilities for SQLite-to-PostgreSQL migration.

Provides functions to export data from SQLite, import into PostgreSQL,
and orchestrate the full migration. Also includes dual-backend DDL
and engine creation helpers.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger("review-bot")

# ── Table names in migration order ──────────────────────────────────────
_TABLE_NAMES = ("reviews", "jobs", "persona_stats", "review_comment_tracking", "review_feedback")

# ── PostgreSQL-specific DDL ─────────────────────────────────────────────
_CREATE_TABLES_POSTGRESQL = [
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        persona_name TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        pr_url TEXT NOT NULL,
        verdict TEXT NOT NULL,
        comment_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL,
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
        installation_id BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        queued_at TIMESTAMPTZ NOT NULL,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        error_message TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_stats (
        persona_name TEXT PRIMARY KEY,
        total_reviews INTEGER NOT NULL DEFAULT 0,
        repos_mined INTEGER NOT NULL DEFAULT 0,
        comments_mined INTEGER NOT NULL DEFAULT 0,
        last_mined_at TIMESTAMPTZ,
        last_review_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_comment_tracking (
        comment_id BIGINT PRIMARY KEY,
        review_id TEXT NOT NULL,
        persona_name TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        file_path TEXT,
        line_number INTEGER,
        body TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        posted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_polled_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_feedback (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        comment_id BIGINT NOT NULL,
        feedback_type TEXT NOT NULL,
        feedback_source TEXT NOT NULL,
        reactor_username TEXT NOT NULL,
        is_pr_author BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(comment_id, feedback_type, feedback_source, reactor_username)
    )
    """,
]

# ── SQLite DDL (mirrors app.py _CREATE_TABLES_SQL) ──────────────────────
_CREATE_TABLES_SQLITE = [
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
    """
    CREATE TABLE IF NOT EXISTS review_comment_tracking (
        comment_id INTEGER PRIMARY KEY,
        review_id TEXT NOT NULL,
        persona_name TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        file_path TEXT,
        line_number INTEGER,
        body TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        posted_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_polled_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comment_id INTEGER NOT NULL,
        feedback_type TEXT NOT NULL,
        feedback_source TEXT NOT NULL,
        reactor_username TEXT NOT NULL,
        is_pr_author INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(comment_id, feedback_type, feedback_source, reactor_username)
    )
    """,
]

# ── Index DDL (backend-agnostic) ────────────────────────────────────────
_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_reviews_persona_name ON reviews(persona_name)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_pr_number ON reviews(pr_number)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_repo ON reviews(repo)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_created_at ON reviews(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_persona_name ON jobs(persona_name)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_persona ON review_comment_tracking(persona_name)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_category ON review_comment_tracking(category)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_created ON review_feedback(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_comment ON review_feedback(comment_id)",
    "CREATE INDEX IF NOT EXISTS idx_tracking_repo ON review_comment_tracking(repo)",
]


def get_db_backend(database_url: str) -> str:
    """Determine the database backend from a connection URL.

    Args:
        database_url: SQLAlchemy-style database connection URL.

    Returns:
        'sqlite' or 'postgresql'.

    Raises:
        ValueError: If the URL prefix is not supported.
    """
    if database_url.startswith("sqlite"):
        return "sqlite"
    if database_url.startswith("postgresql"):
        return "postgresql"
    raise ValueError(
        f"Unsupported database URL prefix: {database_url.split('://')[0]}. "
        "Only sqlite and postgresql are supported."
    )


async def create_engine(
    database_url: str,
    *,
    pool_max_size: int = 10,
    pool_max_overflow: int = 5,
    pool_recycle: int = 3600,
) -> AsyncEngine:
    """Create a SQLAlchemy async engine with backend-appropriate configuration.

    Args:
        database_url: SQLAlchemy-style database connection URL.
        pool_max_size: Maximum pool size (PostgreSQL only).
        pool_max_overflow: Maximum pool overflow (PostgreSQL only).
        pool_recycle: Pool connection recycle time in seconds (PostgreSQL only).

    Returns:
        Configured AsyncEngine instance.
    """
    backend = get_db_backend(database_url)

    if backend == "postgresql":
        return create_async_engine(
            database_url,
            echo=False,
            pool_size=pool_max_size,
            max_overflow=pool_max_overflow,
            pool_recycle=pool_recycle,
            pool_pre_ping=True,
        )
    # SQLite — no pool configuration
    return create_async_engine(database_url, echo=False)


async def init_database(engine: AsyncEngine, backend: str) -> None:
    """Create database tables and indexes if they don't exist.

    Args:
        engine: SQLAlchemy async engine to initialize tables on.
        backend: Database backend: 'sqlite' or 'postgresql'.
    """
    ddl = _CREATE_TABLES_SQLITE if backend == "sqlite" else _CREATE_TABLES_POSTGRESQL
    async with engine.begin() as conn:
        for sql in ddl:
            await conn.execute(text(sql))
        for sql in _CREATE_INDEXES_SQL:
            await conn.execute(text(sql))
    logger.info("Database tables and indexes initialized (backend=%s)", backend)


# ── Migration functions ─────────────────────────────────────────────────


async def export_sqlite_data(engine: AsyncEngine) -> dict[str, list[dict[str, Any]]]:
    """Export all rows from SQLite database tables.

    Args:
        engine: SQLAlchemy async engine connected to a SQLite database.

    Returns:
        Dictionary mapping table names to lists of row dicts.
        Empty tables produce empty lists.
    """
    data: dict[str, list[dict[str, Any]]] = {}
    async with engine.connect() as conn:
        for table in _TABLE_NAMES:
            result = await conn.execute(text(f"SELECT * FROM {table}"))  # noqa: S608
            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            data[table] = rows
            logger.info("Exported %d rows from %s", len(rows), table)
    return data


async def import_to_postgresql(
    engine: AsyncEngine,
    data: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    """Import data into PostgreSQL tables idempotently.

    Uses INSERT ... ON CONFLICT DO NOTHING so re-running is safe.
    All inserts are wrapped in a single transaction.

    Args:
        engine: SQLAlchemy async engine connected to a PostgreSQL database.
        data: Dictionary mapping table names to lists of row dicts
              (as returned by export_sqlite_data).

    Returns:
        Dictionary mapping table names to number of rows imported.
    """
    counts: dict[str, int] = {}

    async with engine.begin() as conn:
        for table in _TABLE_NAMES:
            rows = data.get(table, [])
            if not rows:
                counts[table] = 0
                continue

            imported = 0
            for row in rows:
                # Filter out auto-generated id for tables with identity columns
                _auto_id_tables = {"reviews", "review_feedback"}
                row_data = {
                    k: v
                    for k, v in row.items()
                    if k != "id" or table not in _auto_id_tables
                }

                columns = list(row_data.keys())
                placeholders = ", ".join(f":{c}" for c in columns)
                col_names = ", ".join(columns)

                # Determine conflict target
                if table in {"reviews", "review_feedback"}:
                    # Auto-generated id; use ON CONFLICT DO NOTHING without target
                    sql = (
                        f"INSERT INTO {table} ({col_names}) "  # noqa: S608
                        f"VALUES ({placeholders}) "
                        "ON CONFLICT DO NOTHING"
                    )
                elif table == "jobs":
                    sql = (
                        f"INSERT INTO {table} ({col_names}) "  # noqa: S608
                        f"VALUES ({placeholders}) "
                        "ON CONFLICT (id) DO NOTHING"
                    )
                elif table == "review_comment_tracking":
                    sql = (
                        f"INSERT INTO {table} ({col_names}) "  # noqa: S608
                        f"VALUES ({placeholders}) "
                        "ON CONFLICT (comment_id) DO NOTHING"
                    )
                else:  # persona_stats
                    sql = (
                        f"INSERT INTO {table} ({col_names}) "  # noqa: S608
                        f"VALUES ({placeholders}) "
                        "ON CONFLICT (persona_name) DO NOTHING"
                    )

                result = await conn.execute(text(sql), row_data)
                imported += result.rowcount

            counts[table] = imported
            logger.info("Imported %d rows into %s", imported, table)

    return counts


async def migrate_sqlite_to_postgresql(
    sqlite_engine: AsyncEngine,
    pg_engine: AsyncEngine,
) -> dict[str, int]:
    """Orchestrate a full migration from SQLite to PostgreSQL.

    Validates that the source engine is SQLite and the target is PostgreSQL,
    then exports all data and imports it into the target.

    Args:
        sqlite_engine: SQLAlchemy async engine connected to SQLite source.
        pg_engine: SQLAlchemy async engine connected to PostgreSQL target.

    Returns:
        Dictionary mapping table names to number of rows imported.

    Raises:
        ValueError: If source is not SQLite or target is not PostgreSQL.
    """
    source_url = str(sqlite_engine.url)
    target_url = str(pg_engine.url)

    if not source_url.startswith("sqlite"):
        raise ValueError(
            f"Source engine must be SQLite, got: {source_url.split('://')[0]}"
        )
    if not target_url.startswith("postgresql"):
        raise ValueError(
            f"Target engine must be PostgreSQL, got: {target_url.split('://')[0]}"
        )

    logger.info("Starting migration from SQLite to PostgreSQL")
    data = await export_sqlite_data(sqlite_engine)

    total_rows = sum(len(rows) for rows in data.values())
    logger.info("Exported %d total rows from SQLite", total_rows)

    counts = await import_to_postgresql(pg_engine, data)

    total_imported = sum(counts.values())
    logger.info("Migration complete: %d rows imported to PostgreSQL", total_imported)

    return counts

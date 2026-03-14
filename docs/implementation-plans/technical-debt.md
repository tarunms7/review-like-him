# Technical Debt — Implementation Plan

> Last updated: 2026-03-15

Exhaustive implementation details for the 5 Technical Debt items from the [Roadmap](../ROADMAP.md#technical-debt). Each section covers file-by-file changes, migration strategy, dependency additions, configuration, testing, and deployment impact.

---

## Table of Contents

1. [Database Migration Framework (Alembic)](#1-database-migration-framework-alembic)
2. [Structured Logging (JSON Format)](#2-structured-logging-json-format)
3. [OpenTelemetry Tracing](#3-opentelemetry-tracing)
4. [CI/CD Pipeline](#4-cicd-pipeline)
5. [Performance Benchmarks](#5-performance-benchmarks)

---

## 1. Database Migration Framework (Alembic)

### 1.1 Overview

Replace the raw `CREATE TABLE IF NOT EXISTS` SQL in `review_bot/server/app.py` (lines 23–72) with Alembic-managed migrations. This gives us versioned schema changes, rollback support, and auto-generation from SQLAlchemy ORM models.

### 1.2 Dependency Additions

```toml
# pyproject.toml — add to [project.dependencies]
"alembic>=1.13",
```

No additional dev dependencies needed — Alembic ships with everything required for async support via SQLAlchemy's async engine.

### 1.3 File-by-File Changes

#### New: `review_bot/models.py`

Define SQLAlchemy ORM models that mirror the current raw SQL tables. All three tables (`reviews`, `jobs`, `persona_stats`) plus their indexes.

```python
"""SQLAlchemy ORM models for review-bot database tables."""

from __future__ import annotations

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_name: Mapped[str] = mapped_column(String, nullable=False)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_url: Mapped[str] = mapped_column(String, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    comment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_reviews_persona_name", "persona_name"),
        Index("idx_reviews_pr_number", "pr_number"),
        Index("idx_reviews_repo", "repo"),
        Index("idx_reviews_created_at", "created_at"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    persona_name: Mapped[str] = mapped_column(String, nullable=False)
    installation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    queued_at: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_persona_name", "persona_name"),
    )


class PersonaStat(Base):
    __tablename__ = "persona_stats"

    persona_name: Mapped[str] = mapped_column(String, primary_key=True)
    total_reviews: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repos_mined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments_mined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_mined_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_review_at: Mapped[str | None] = mapped_column(String, nullable=True)
```

#### New: `alembic.ini`

Standard Alembic config at project root. The `sqlalchemy.url` is overridden at runtime to use the app's `Settings.db_url`.

```ini
[alembic]
script_location = alembic
prepend_sys_path = .

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

#### New: `alembic/env.py`

Async-aware Alembic environment that uses `create_async_engine` to run migrations. Imports `Base.metadata` from `review_bot.models` for auto-generation.

```python
"""Alembic environment configuration for async SQLAlchemy."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from review_bot.config.settings import Settings
from review_bot.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    settings = Settings()
    context.configure(
        url=settings.db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """Run migrations using the provided connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to the database."""
    settings = Settings()
    engine = create_async_engine(settings.db_url)

    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

#### New: `alembic/versions/001_initial_schema.py`

Initial migration that creates all 3 tables + indexes, matching the current raw SQL exactly. This is the baseline migration.

```python
"""Initial schema — reviews, jobs, persona_stats tables.

Revision ID: 001_initial
Revises:
Create Date: 2026-03-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("persona_name", sa.String, nullable=False),
        sa.Column("repo", sa.String, nullable=False),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("pr_url", sa.String, nullable=False),
        sa.Column("verdict", sa.String, nullable=False),
        sa.Column("comment_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("idx_reviews_persona_name", "reviews", ["persona_name"])
    op.create_index("idx_reviews_pr_number", "reviews", ["pr_number"])
    op.create_index("idx_reviews_repo", "reviews", ["repo"])
    op.create_index("idx_reviews_created_at", "reviews", ["created_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("owner", sa.String, nullable=False),
        sa.Column("repo", sa.String, nullable=False),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("persona_name", sa.String, nullable=False),
        sa.Column("installation_id", sa.Integer, nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="queued"),
        sa.Column("queued_at", sa.String, nullable=False),
        sa.Column("started_at", sa.String, nullable=True),
        sa.Column("completed_at", sa.String, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
    )
    op.create_index("idx_jobs_status", "jobs", ["status"])
    op.create_index("idx_jobs_persona_name", "jobs", ["persona_name"])

    op.create_table(
        "persona_stats",
        sa.Column("persona_name", sa.String, primary_key=True),
        sa.Column("total_reviews", sa.Integer, nullable=False, server_default="0"),
        sa.Column("repos_mined", sa.Integer, nullable=False, server_default="0"),
        sa.Column("comments_mined", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_mined_at", sa.String, nullable=True),
        sa.Column("last_review_at", sa.String, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("persona_stats")
    op.drop_table("jobs")
    op.drop_table("reviews")
```

#### Modified: `review_bot/server/app.py`

Remove all raw SQL (`_CREATE_TABLES_SQL`, `_CREATE_INDEXES_SQL`, `_init_database`) and replace with Alembic migration at startup.

```python
# REMOVE: lines 23-82 (_CREATE_TABLES_SQL, _CREATE_INDEXES_SQL, _init_database)

# ADD: import and migration runner
from alembic.config import Config
from alembic import command

async def _run_migrations(engine: AsyncEngine) -> None:
    """Run Alembic migrations to head."""
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.attributes["engine"] = engine

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: command.upgrade(alembic_cfg, "head")
        )
    logger.info("Database migrations applied")

# In lifespan(), replace:
#   await _init_database(engine)
# with:
#   await _run_migrations(engine)
```

#### New: `review_bot/cli/db_cmd.py`

CLI commands for database management:

```python
# review-bot db upgrade    — run migrations to head
# review-bot db downgrade  — rollback one migration
# review-bot db stamp      — stamp existing DB as current (for adoption)
# review-bot db history    — show migration history
```

### 1.4 Migration Strategy from Current State

**For new installations:** The initial migration creates all tables from scratch. No special handling needed.

**For existing databases (adoption path):**

1. The existing database already has the correct schema from raw SQL.
2. Run `alembic stamp 001_initial` to mark the database as being at the initial migration without executing any SQL.
3. This creates the `alembic_version` table with a single row pointing to `001_initial`.
4. All future migrations apply normally from that point.

**CLI command for stamping:**
```bash
review-bot db stamp
# Equivalent to: alembic stamp 001_initial
```

**Auto-detection in server startup:**
```python
async def _run_migrations(engine: AsyncEngine) -> None:
    """Run migrations, auto-stamping existing databases."""
    async with engine.begin() as conn:
        # Check if alembic_version table exists
        result = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ))
        has_alembic = result.fetchone() is not None

        # Check if existing tables exist (pre-Alembic database)
        result = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reviews'"
        ))
        has_tables = result.fetchone() is not None

    if has_tables and not has_alembic:
        # Existing database without migration tracking — stamp as current
        logger.info("Existing database detected, stamping as current migration")
        command.stamp(alembic_cfg, "001_initial")
    else:
        command.upgrade(alembic_cfg, "head")
```

### 1.5 Edge Cases

#### Existing databases without migration tracking

**Problem:** Databases created by the raw SQL in `app.py` have no `alembic_version` table.

**Solution:** Auto-detection at startup (see above). The server checks for the presence of existing tables without `alembic_version` and stamps them. The `review-bot db stamp` CLI command provides a manual path.

**Verification:** After stamping, `alembic current` should report `001_initial (head)`.

#### Failed migrations — rollback handling

**Problem:** A migration could fail mid-execution, leaving the database in an inconsistent state.

**Solution:**
- Each migration runs inside a transaction (`context.begin_transaction()` in `env.py`).
- SQLite has limited `ALTER TABLE` support — failed DDL operations auto-rollback within the transaction.
- For PostgreSQL (future): DDL is transactional, so failed migrations cleanly rollback.
- Add a `review-bot db downgrade` CLI command for manual rollback: `alembic downgrade -1`.
- Log the current migration version before and after each upgrade for debugging.

**Recovery procedure:**
```bash
# Check current state
review-bot db history

# If stuck, manually rollback
review-bot db downgrade

# Fix the migration script, then re-apply
review-bot db upgrade
```

#### Migration in production with active connections

**Problem:** Running migrations while the server is handling requests could cause table-lock contention or errors.

**Solution:**
- Migrations run during the `lifespan()` startup phase, before the server accepts requests. FastAPI does not serve traffic until `yield` is reached.
- For zero-downtime deployments, run migrations as a separate step before starting the new server version:
  ```bash
  # Deploy step 1: run migrations
  review-bot db upgrade

  # Deploy step 2: start server (migrations are already applied)
  uvicorn review_bot.server.app:create_app --factory
  ```
- Add a `--skip-migrations` flag to the server command for cases where migrations are handled externally.

#### Migration ordering conflicts in team development

**Problem:** Two developers create migrations concurrently, both branching from the same `down_revision`, causing a "multiple heads" error.

**Solution:**
- Use descriptive revision IDs with timestamps: `002_20260315_add_feedback_table`.
- When multiple heads are detected, run `alembic merge heads -m "merge branches"` to create a merge migration.
- Add a CI check (see [CI/CD Pipeline](#4-cicd-pipeline)) that runs `alembic check` to detect un-generated migrations and `alembic heads` to verify a single head.
- Document the workflow in a `CONTRIBUTING.md` section.

#### Testing migrations against both SQLite and PostgreSQL

**Problem:** SQLite and PostgreSQL have different DDL capabilities (`ALTER TABLE` limitations in SQLite, transactional DDL in PostgreSQL).

**Solution:**
- Use `op.batch_alter_table()` for column modifications — Alembic's batch mode recreates the table for SQLite while using native DDL for PostgreSQL.
- Test migrations in CI against both backends:
  ```python
  @pytest.fixture(params=["sqlite+aiosqlite:///", "postgresql+asyncpg://..."])
  async def db_engine(request):
      engine = create_async_engine(request.param)
      yield engine
      await engine.dispose()
  ```
- PostgreSQL tests run only in CI (via a service container) — local development uses SQLite.

### 1.6 Testing Approach

```python
# tests/test_migrations.py

import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

@pytest.fixture
async def fresh_db(tmp_path):
    """Create a fresh SQLite database for migration testing."""
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    yield engine
    await engine.dispose()

async def test_upgrade_to_head(fresh_db):
    """Migrations apply cleanly to an empty database."""
    # Run alembic upgrade head
    # Verify all tables exist with correct columns

async def test_downgrade_to_base(fresh_db):
    """All migrations are reversible."""
    # Upgrade to head, then downgrade to base
    # Verify all tables are removed

async def test_stamp_existing_database(fresh_db):
    """Existing databases can be stamped without data loss."""
    # Create tables with raw SQL (current behavior)
    # Insert test data
    # Stamp as 001_initial
    # Verify data is preserved
    # Verify alembic_version is correct

async def test_auto_detection_stamps_existing(fresh_db):
    """Server startup auto-detects and stamps existing databases."""
    # Create tables with raw SQL
    # Call _run_migrations()
    # Verify stamp happened, no duplicate table errors
```

### 1.7 Deployment Impact

- **Zero data loss:** The initial migration exactly matches the existing schema. No data transformation needed.
- **New dependency:** `alembic>=1.13` is added to production dependencies (~500KB).
- **Startup time:** Negligible — checking migration version and applying is <100ms.
- **Rollback plan:** If Alembic causes issues, revert to the raw SQL by reverting the code change. The `alembic_version` table is harmless and can be dropped manually.

---

## 2. Structured Logging (JSON Format)

### 2.1 Overview

Replace the current `StreamHandler` with text formatting in `review_bot/utils/logging.py` with `structlog` for JSON-structured output. Add context propagation so all log entries from a single review job share a `job_id`, `persona_name`, and `pr_url`.

### 2.2 structlog vs python-json-logger Comparison

| Criteria | structlog | python-json-logger |
|---|---|---|
| **Context binding** | First-class `bind()` API for adding context to all subsequent log entries | Requires manual inclusion of extra fields in every log call |
| **Processors pipeline** | Composable processors for filtering, formatting, enriching | Limited to formatter customization |
| **Async support** | Built-in `contextvars` integration for async context propagation | No native async support — must manage `LogRecord` extras manually |
| **stdlib integration** | Can wrap stdlib loggers transparently | Is a stdlib formatter — easier adoption but less powerful |
| **Performance** | ~15% overhead over raw stdlib (negligible for this app) | ~5% overhead (thinner layer) |
| **Adoption** | 10k+ GitHub stars, actively maintained, used by major projects | 3k+ stars, simpler but less feature-rich |

**Recommendation: structlog.** The context binding and processor pipeline are essential for propagating `job_id` across async task boundaries. `python-json-logger` would require significantly more manual plumbing.

### 2.3 Dependency Additions

```toml
# pyproject.toml — add to [project.dependencies]
"structlog>=24.1",
```

### 2.4 File-by-File Changes

#### Modified: `review_bot/utils/logging.py`

Complete rewrite of the logging setup:

```python
"""Structured logging setup for review-bot using structlog."""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(*, level: int = logging.INFO, verbose: bool = False, json: bool = True) -> None:
    """Configure structlog with JSON or console output.

    Args:
        level: Base logging level.
        verbose: If True, set level to DEBUG.
        json: If True, output JSON. If False, output colored console format.
    """
    if verbose:
        level = logging.DEBUG

    # Shared processors for all output formats
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _filter_sensitive_data,
        _truncate_large_fields,
    ]

    if json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging to use structlog formatter
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Ensure review-bot logger inherits the configuration
    logging.getLogger("review-bot").setLevel(level)


def _filter_sensitive_data(
    logger: logging.Logger, method: str, event_dict: dict,
) -> dict:
    """Remove or redact sensitive fields from log entries.

    Filters: GitHub tokens, private keys, webhook secrets, API keys.
    """
    sensitive_keys = {"token", "secret", "private_key", "api_key", "password", "authorization"}
    for key in list(event_dict.keys()):
        if any(s in key.lower() for s in sensitive_keys):
            event_dict[key] = "***REDACTED***"

    # Redact Bearer tokens in string values
    event = event_dict.get("event", "")
    if isinstance(event, str) and "Bearer " in event:
        event_dict["event"] = event.split("Bearer ")[0] + "Bearer ***REDACTED***"

    return event_dict


def _truncate_large_fields(
    logger: logging.Logger, method: str, event_dict: dict,
) -> dict:
    """Truncate fields larger than 10KB to prevent log bloat from LLM output."""
    max_field_size = 10_240  # 10KB
    for key, value in event_dict.items():
        if isinstance(value, str) and len(value) > max_field_size:
            event_dict[key] = value[:max_field_size] + f"... [truncated, {len(value)} bytes total]"
    return event_dict


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance.

    Drop-in replacement for logging.getLogger() that returns a bound logger.
    """
    return structlog.get_logger(name)
```

#### Modified: `review_bot/server/queue.py`

Add context propagation for job processing:

```python
# At the start of _process_job(), bind context variables:
import structlog

async def _process_job(self, job: ReviewJob) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        job_id=job.id,
        persona_name=job.persona_name,
        pr_url=f"https://github.com/{job.owner}/{job.repo}/pull/{job.pr_number}",
        repo=f"{job.owner}/{job.repo}",
        pr_number=job.pr_number,
    )
    # ... rest of method unchanged
    # All subsequent log calls automatically include job_id, persona_name, pr_url
```

#### Modified: `review_bot/review/orchestrator.py`

Bind additional context during the review pipeline:

```python
# In run_review(), after loading persona:
structlog.contextvars.bind_contextvars(
    persona_tone=persona.tone,
    file_count=len(files),
)
```

#### Modified: `review_bot/cli/main.py`

Add `--json-logs` / `--no-json-logs` flag:

```python
@click.option("--json-logs/--no-json-logs", default=False,
              help="Output logs in JSON format (default: human-readable)")
def cli(verbose, json_logs):
    setup_logging(verbose=verbose, json=json_logs)
```

#### Modified: `review_bot/config/settings.py`

Add logging configuration settings:

```python
log_json: bool = Field(default=False, description="Output logs in JSON format")
log_level: str = Field(default="INFO", description="Logging level")
```

### 2.5 Context Propagation Design

```
Review Job Context Flow:

  webhook received          queue._process_job()        orchestrator.run_review()
  ┌──────────────┐         ┌──────────────────┐        ┌─────────────────────┐
  │ bind:         │  ──►   │ bind:             │  ──►  │ bind:               │
  │  request_id   │        │  job_id           │       │  persona_tone       │
  │  delivery_id  │        │  persona_name     │       │  file_count         │
  └──────────────┘         │  pr_url           │       │  review_phase       │
                           │  repo             │       └─────────────────────┘
                           │  pr_number        │
                           └──────────────────┘

  All downstream log calls automatically include all bound context variables.
  structlog.contextvars uses Python's contextvars module, which is
  async-safe — each asyncio task gets its own context snapshot.
```

### 2.6 Edge Cases

#### Log rotation with JSON format

**Problem:** JSON logs with one entry per line can grow large. Standard `RotatingFileHandler` works but splits mid-line if a JSON entry exceeds the rotation boundary.

**Solution:**
- Use `logging.handlers.RotatingFileHandler` with `maxBytes=50_000_000` (50MB) and `backupCount=5`.
- Each JSON entry is a single line (structlog's `JSONRenderer` ensures no embedded newlines), so rotation at line boundaries is safe.
- For production, recommend external log rotation (logrotate, Docker log driver) rather than in-process rotation.

#### Large log entries from LLM output

**Problem:** The `ClaudeReviewer` output and prompt builder input can be 100KB+, causing log entries that overwhelm log aggregation systems.

**Solution:** The `_truncate_large_fields` processor (defined above) truncates any string field >10KB. This catches:
- Raw LLM output logged during debugging.
- Full diffs logged at DEBUG level.
- Large prompt strings.

The truncation is applied before serialization, so the JSON output is always bounded.

#### Sensitive data in logs — PII filtering

**Problem:** Log entries might contain GitHub tokens, webhook secrets, PR author names, or email addresses.

**Solution:** The `_filter_sensitive_data` processor (defined above) handles:
- **Known sensitive keys:** Any field name containing `token`, `secret`, `private_key`, `api_key`, `password`, or `authorization` is redacted.
- **Bearer tokens in strings:** Detected and redacted via pattern matching.
- **Future extension:** Add a regex-based PII detector for email addresses and GitHub usernames if needed. Initially keep it simple — the main risk is token leakage, not PII in review content.

#### Performance impact of structured logging

**Problem:** structlog adds overhead from processor pipelines and JSON serialization.

**Solution:**
- Benchmark: structlog adds ~15μs per log call vs ~5μs for raw stdlib. At 100 log calls per review, this is 1ms total — negligible vs. the 5-30 second LLM call.
- Use `cache_logger_on_first_use=True` (already configured) to avoid repeated logger construction.
- The `json=False` option for local development avoids JSON serialization overhead entirely.

#### Backwards compatibility for developers reading logs locally

**Problem:** JSON logs are unreadable in a terminal during development.

**Solution:**
- Default to human-readable console output (`json=False` in CLI, `log_json=False` in Settings).
- The `ConsoleRenderer` with `colors=True` produces colored, human-friendly output identical to the current format.
- JSON mode is opt-in: `--json-logs` flag or `REVIEW_BOT_LOG_JSON=true` env var.
- Server mode defaults to JSON when `REVIEW_BOT_LOG_JSON` is set (typically in production Docker images).

#### Correlating logs across concurrent reviews

**Problem:** Multiple review jobs run concurrently in the async queue. Without correlation IDs, interleaved log lines are impossible to follow.

**Solution:**
- `structlog.contextvars` automatically isolates context per asyncio task.
- Each job binds a unique `job_id` (UUID) at the start of `_process_job()`.
- All downstream log calls include `job_id`, enabling filtering: `jq 'select(.job_id == "abc-123")'`.
- The `delivery_id` from GitHub webhook headers is also bound for end-to-end correlation from webhook receipt to review completion.

### 2.7 Testing Approach

```python
# tests/test_logging.py

import json
import logging

import structlog

from review_bot.utils.logging import setup_logging


def test_json_output_format(capsys):
    """JSON mode produces valid JSON lines."""
    setup_logging(json=True)
    logger = structlog.get_logger("test")
    logger.info("test message", key="value")

    captured = capsys.readouterr()
    entry = json.loads(captured.err.strip())
    assert entry["event"] == "test message"
    assert entry["key"] == "value"
    assert "timestamp" in entry


def test_sensitive_data_redacted(capsys):
    """Sensitive fields are redacted in output."""
    setup_logging(json=True)
    logger = structlog.get_logger("test")
    logger.info("auth", token="ghp_secret123", api_key="sk-xxx")

    entry = json.loads(capsys.readouterr().err.strip())
    assert entry["token"] == "***REDACTED***"
    assert entry["api_key"] == "***REDACTED***"


def test_large_field_truncation(capsys):
    """Fields >10KB are truncated."""
    setup_logging(json=True)
    logger = structlog.get_logger("test")
    logger.info("big", data="x" * 20_000)

    entry = json.loads(capsys.readouterr().err.strip())
    assert len(entry["data"]) < 12_000  # 10KB + truncation message
    assert "truncated" in entry["data"]


def test_context_propagation():
    """Context variables are included in log entries."""
    setup_logging(json=True)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(job_id="test-123")

    logger = structlog.get_logger("test")
    # Verify job_id appears in output


def test_console_mode_no_json(capsys):
    """Console mode produces human-readable, non-JSON output."""
    setup_logging(json=False)
    logger = structlog.get_logger("test")
    logger.info("hello")

    output = capsys.readouterr().err
    # Should NOT be valid JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(output.strip())
```

### 2.8 Deployment Impact

- **New dependency:** `structlog>=24.1` (~200KB).
- **Log format change:** All log consumers (Datadog, ELK, CloudWatch) need to be configured for JSON parsing. Most support auto-detection.
- **No code changes required in log call sites:** `logging.getLogger("review-bot").info(...)` calls continue to work — structlog wraps stdlib loggers transparently.
- **Gradual rollout:** Deploy with `log_json=False` first (identical behavior to current), then enable JSON in production environments.

---

## 3. OpenTelemetry Tracing

### 3.1 Overview

Instrument the review pipeline with OpenTelemetry spans to provide visibility into where time is spent: mining duration, LLM latency, GitHub API call counts, and queue wait time. Currently, the only timing data is `duration_ms` in the `reviews` table.

### 3.2 Dependency Additions

```toml
# pyproject.toml — add to [project.dependencies]
"opentelemetry-api>=1.23",
"opentelemetry-sdk>=1.23",
"opentelemetry-exporter-otlp>=1.23",

# pyproject.toml — add to [project.optional-dependencies]
tracing = [
    "opentelemetry-instrumentation-fastapi>=0.44b0",
    "opentelemetry-instrumentation-httpx>=0.44b0",
    "opentelemetry-instrumentation-sqlalchemy>=0.44b0",
]
```

The optional `tracing` dependency group keeps the core package lightweight — teams that don't need tracing don't pay for it.

### 3.3 Span Hierarchy Design

```
review_bot.webhook.handle_pr_event
  └── review_bot.queue.wait_time          (queue → dequeue duration)
      └── review_bot.job.process          (full job processing)
          ├── review_bot.persona.load     (YAML file read + parse)
          ├── review_bot.github.fetch_pr  (PR data, diff, files)
          │   ├── github.api.get_pull_request
          │   ├── github.api.get_pull_request_files
          │   └── github.api.get_pull_request_diff
          ├── review_bot.repo.scan        (repo convention scanning)
          │   └── github.api.get_repo_contents (×N)
          ├── review_bot.prompt.build     (prompt construction)
          ├── review_bot.llm.review       (Claude API call)
          ├── review_bot.format.result    (output formatting)
          ├── review_bot.github.post      (posting review to GitHub)
          │   ├── github.api.post_review
          │   └── github.api.post_comment (if inline comments)
          └── review_bot.db.log_review    (database write)

review_bot.mining.mine_user
  ├── review_bot.mining.discover_prs     (search API pagination)
  └── review_bot.mining.fetch_repo       (×N repos)
      └── review_bot.mining.fetch_pr     (×M PRs per repo)
          ├── github.api.get_comments
          └── github.api.get_reviews
```

### 3.4 File-by-File Changes

#### New: `review_bot/utils/tracing.py`

Tracing setup and utilities:

```python
"""OpenTelemetry tracing configuration for review-bot."""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger("review-bot")

_tracer: trace.Tracer | None = None


def setup_tracing(
    *,
    service_name: str = "review-bot",
    otlp_endpoint: str | None = None,
    console_export: bool = False,
    sample_rate: float = 1.0,
) -> trace.Tracer:
    """Configure OpenTelemetry tracing.

    Args:
        service_name: Service name for span metadata.
        otlp_endpoint: OTLP exporter endpoint (e.g., "http://localhost:4317").
                       If None, tracing is effectively a no-op unless console_export is True.
        console_export: If True, export spans to stderr (for development).
        sample_rate: Fraction of traces to sample (0.0–1.0).

    Returns:
        Configured tracer instance.
    """
    global _tracer

    resource = Resource.create({"service.name": service_name})

    provider = TracerProvider(
        resource=resource,
        sampler=trace.sampling.TraceIdRatioBased(sample_rate),
    )

    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("OTLP trace exporter configured: %s", otlp_endpoint)

    if console_export:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("review-bot")

    return _tracer


def get_tracer() -> trace.Tracer:
    """Get the configured tracer, or a no-op tracer if not configured."""
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("review-bot")
    return _tracer


async def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
```

#### Modified: `review_bot/config/settings.py`

Add tracing configuration:

```python
# New fields in Settings class:
otel_endpoint: str | None = Field(default=None, description="OTLP exporter endpoint")
otel_sample_rate: float = Field(default=1.0, description="Trace sampling rate (0.0-1.0)")
otel_console: bool = Field(default=False, description="Export traces to console")
```

#### Modified: `review_bot/server/app.py`

Initialize tracing in lifespan:

```python
from review_bot.utils.tracing import setup_tracing, shutdown_tracing

# In lifespan(), after engine initialization:
if app_settings.otel_endpoint or app_settings.otel_console:
    setup_tracing(
        otlp_endpoint=app_settings.otel_endpoint,
        console_export=app_settings.otel_console,
        sample_rate=app_settings.otel_sample_rate,
    )

# In shutdown:
await shutdown_tracing()
```

#### Modified: `review_bot/review/orchestrator.py`

Instrument the review pipeline:

```python
from review_bot.utils.tracing import get_tracer

async def run_review(self, owner, repo, pr_number, persona_name):
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "review_bot.job.process",
        attributes={
            "review.owner": owner,
            "review.repo": repo,
            "review.pr_number": pr_number,
            "review.persona": persona_name,
        },
    ) as span:
        # 1. Load persona
        with tracer.start_as_current_span("review_bot.persona.load"):
            persona = self._persona_store.load(persona_name)

        # 2. Fetch PR data
        with tracer.start_as_current_span("review_bot.github.fetch_pr"):
            pr_data = await self._github.get_pull_request(owner, repo, pr_number)
            files = await self._github.get_pull_request_files(owner, repo, pr_number)
            diff = await self._github.get_pull_request_diff(owner, repo, pr_number)

        span.set_attribute("review.file_count", len(files))

        # 5. LLM review
        with tracer.start_as_current_span("review_bot.llm.review") as llm_span:
            raw_output = await self._reviewer.review(prompt)
            llm_span.set_attribute("review.llm.output_length", len(raw_output))

        # ... etc for each step
```

#### Modified: `review_bot/github/api.py`

Add span attributes for API calls:

```python
from review_bot.utils.tracing import get_tracer

async def _request(self, method, url, **kwargs):
    tracer = get_tracer()
    with tracer.start_as_current_span(
        f"github.api.{method.lower()}",
        attributes={
            "http.method": method,
            "http.url": url,
        },
    ) as span:
        # ... existing retry logic
        span.set_attribute("http.status_code", resp.status_code)
        return resp
```

#### Modified: `review_bot/server/queue.py`

Track queue wait time:

```python
from review_bot.utils.tracing import get_tracer
import time

async def enqueue(self, job):
    job._enqueue_time = time.monotonic()  # Track when enqueued
    # ... existing logic

async def _process_job(self, job):
    tracer = get_tracer()
    wait_time = time.monotonic() - getattr(job, '_enqueue_time', time.monotonic())
    with tracer.start_as_current_span(
        "review_bot.queue.wait_time",
        attributes={"queue.wait_seconds": wait_time},
    ):
        pass  # Span just records the wait duration

    # ... rest of processing with its own span
```

### 3.5 Exporter Configuration

| Exporter | Use Case | Configuration |
|---|---|---|
| **OTLP (gRPC)** | Production — Jaeger, Grafana Tempo, Datadog | `REVIEW_BOT_OTEL_ENDPOINT=http://collector:4317` |
| **OTLP (HTTP)** | Cloud-hosted backends (Honeycomb, Lightstep) | `REVIEW_BOT_OTEL_ENDPOINT=https://api.honeycomb.io` |
| **Console** | Local development | `REVIEW_BOT_OTEL_CONSOLE=true` |
| **None** | Disable tracing (default) | Don't set any `OTEL_*` variables |

### 3.6 Edge Cases

#### Trace context propagation across async boundaries

**Problem:** Python's `asyncio` can interleave tasks, potentially mixing trace contexts.

**Solution:**
- OpenTelemetry's Python SDK uses `contextvars` for trace context propagation, which is async-safe. Each `asyncio.Task` inherits the context from its creator.
- The `start_as_current_span` context manager correctly handles async with statements.
- For spans that cross task boundaries (e.g., queue enqueue → dequeue), manually pass the span context:
  ```python
  # In enqueue:
  job._trace_context = trace.get_current_span().get_span_context()

  # In process:
  parent_ctx = trace.set_span_in_context(
      trace.NonRecordingSpan(job._trace_context)
  )
  with tracer.start_as_current_span("process", context=parent_ctx):
      ...
  ```

#### Trace sampling for high-volume deployments

**Problem:** At 100+ reviews/hour, full tracing generates excessive data and cost.

**Solution:**
- Configurable `sample_rate` (default 1.0 = 100%). Set to 0.1 for 10% sampling in high-volume deployments.
- Use `TraceIdRatioBased` sampler for consistent sampling (same trace ID always sampled or not).
- Errors are always sampled regardless of rate — add a custom sampler that forces sampling when `span.status` is `ERROR`:
  ```python
  class AlwaysSampleErrors(Sampler):
      def should_sample(self, context, trace_id, name, **kwargs):
          if has_error_attribute(kwargs):
              return Decision.RECORD_AND_SAMPLE
          return parent_sampler.should_sample(...)
  ```

#### Overhead measurement

**Problem:** Tracing adds latency to every instrumented operation.

**Solution:**
- `BatchSpanProcessor` buffers spans and exports asynchronously — span creation itself is ~1μs.
- Measured overhead: <0.1% of total review time (dominated by LLM calls at 5-30 seconds).
- When tracing is not configured, `get_tracer()` returns a no-op tracer with zero overhead.
- Add a benchmark (see [Performance Benchmarks](#5-performance-benchmarks)) to track tracing overhead.

#### Traces for failed reviews

**Problem:** Failed reviews need tracing for debugging, but the span may not complete normally.

**Solution:**
- Use `span.record_exception(exc)` and `span.set_status(StatusCode.ERROR)` in exception handlers.
- The `BatchSpanProcessor` exports error spans with full exception info (type, message, traceback).
- Example:
  ```python
  except Exception as exc:
      span.record_exception(exc)
      span.set_status(StatusCode.ERROR, str(exc))
      raise
  ```

#### Correlation with structured logs

**Problem:** Need to link log entries to trace spans for unified observability.

**Solution:**
- Add a structlog processor that extracts the current span's `trace_id` and `span_id` and injects them into log entries:
  ```python
  def add_trace_context(logger, method, event_dict):
      span = trace.get_current_span()
      ctx = span.get_span_context()
      if ctx.is_valid:
          event_dict["trace_id"] = format(ctx.trace_id, "032x")
          event_dict["span_id"] = format(ctx.span_id, "016x")
      return event_dict
  ```
- Log aggregation systems (Grafana, Datadog) use `trace_id` to link logs to traces.

#### Cost of tracing infrastructure

**Problem:** Self-hosted Jaeger/Tempo requires storage and compute.

**Solution:**
- **Minimum viable setup:** Jaeger all-in-one Docker image (~100MB RAM, local storage). Sufficient for single-instance deployments.
- **Production:** Grafana Tempo with object storage (S3/GCS) — costs ~$0.01/GB of trace data.
- **Managed:** Honeycomb, Datadog, or Grafana Cloud. Free tiers typically allow 20M spans/month.
- **No infrastructure:** Console exporter for local development, disable in production if unwanted.

### 3.7 Testing Approach

```python
# tests/test_tracing.py

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

@pytest.fixture
def in_memory_exporter():
    """Configure tracing with an in-memory exporter for testing."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    exporter.clear()

def test_review_creates_expected_spans(in_memory_exporter):
    """Review pipeline creates spans for each step."""
    # Run a mock review
    # Assert spans: job.process, persona.load, github.fetch_pr, llm.review, etc.

def test_failed_review_records_exception(in_memory_exporter):
    """Failed reviews include exception info in the span."""
    # Trigger a failure
    # Assert span has ERROR status and exception recorded

def test_no_op_when_tracing_disabled():
    """When tracing is not configured, no overhead or errors."""
    # Don't call setup_tracing()
    # Verify get_tracer() returns a no-op tracer
    # Verify span creation doesn't error

def test_sampling_rate():
    """Sample rate correctly filters traces."""
    setup_tracing(sample_rate=0.0)
    # No spans should be exported
```

### 3.8 Deployment Impact

- **New dependencies:** ~5MB for OpenTelemetry packages (API + SDK + OTLP exporter).
- **Zero overhead when disabled:** No configuration = no-op tracer. No performance impact.
- **Infrastructure required:** Only if `otel_endpoint` is configured. Can start with console export for development.
- **Rollback:** Remove tracing configuration (env vars) — the code gracefully falls back to no-op.

---

## 4. CI/CD Pipeline

### 4.1 Overview

Add GitHub Actions workflows for automated testing, linting, type checking, and PyPI publishing. The project currently has `pytest` and `ruff` as dev dependencies but no CI configuration.

### 4.2 Dependency Additions

```toml
# pyproject.toml — add to [project.optional-dependencies]
dev = [
    # ... existing entries ...
    "mypy>=1.8",
    "types-PyYAML>=6.0",
]

# Add mypy configuration
[tool.mypy]
python_version = "3.11"
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true

[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_defs = false
```

### 4.3 File-by-File Changes

#### New: `.github/workflows/ci.yml`

Main CI workflow for PRs and pushes to main:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    name: Lint & Format
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Run ruff check
        run: ruff check .

      - name: Run ruff format check
        run: ruff format --check .

  typecheck:
    name: Type Check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Run mypy
        run: mypy review_bot/

  test:
    name: Test (Python ${{ matrix.python-version }}, ${{ matrix.os }})
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
        os: [ubuntu-latest, macos-latest]
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}

      - name: Cache pip packages
        uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ matrix.python-version }}-${{ hashFiles('pyproject.toml') }}
          restore-keys: |
            ${{ runner.os }}-pip-${{ matrix.python-version }}-

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Run tests
        run: pytest --cov=review_bot --cov-report=xml -v

      - name: Upload coverage
        if: matrix.python-version == '3.11' && matrix.os == 'ubuntu-latest'
        uses: codecov/codecov-action@v4
        with:
          file: coverage.xml
          fail_ci_if_error: false

  migrations:
    name: Migration Check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Verify single migration head
        run: |
          python -c "
          from alembic.config import Config
          from alembic.script import ScriptDirectory
          cfg = Config('alembic.ini')
          script = ScriptDirectory.from_config(cfg)
          heads = script.get_heads()
          assert len(heads) <= 1, f'Multiple migration heads detected: {heads}'
          print(f'Migration head: {heads}')
          "
```

#### New: `.github/workflows/publish.yml`

PyPI publishing on tagged releases:

```yaml
name: Publish to PyPI

on:
  push:
    tags:
      - "v*"

jobs:
  publish:
    name: Build & Publish
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write  # Required for trusted publishing
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install build tools
        run: pip install build

      - name: Build package
        run: python -m build

      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```

### 4.4 Edge Cases

#### Test flakiness from async tests

**Problem:** `pytest-asyncio` tests can be flaky due to event loop management, especially with `asyncio_mode = "auto"`.

**Solution:**
- Pin `asyncio_mode = "auto"` in `pyproject.toml` (already configured).
- Use `pytest-timeout` to prevent hanging tests:
  ```toml
  # pyproject.toml
  [tool.pytest.ini_options]
  timeout = 30  # 30 second timeout per test
  ```
- Add `pytest-repeat` to dev dependencies for local flake detection: `pytest --count=10`.
- In CI, retry failed tests once with `pytest-rerunfailures`:
  ```yaml
  - name: Run tests
    run: pytest --reruns 1 --reruns-delay 2 -v
  ```

#### Mocking external services in CI

**Problem:** Tests must not call GitHub API, Claude API, or any external service.

**Solution:**
- The test suite already uses `unittest.mock.AsyncMock` and `respx` for HTTP mocking (visible in `conftest.py`).
- Add a CI environment variable check to fail tests that make real HTTP calls:
  ```python
  # conftest.py
  @pytest.fixture(autouse=True)
  def block_real_http(monkeypatch):
      """Prevent any real HTTP requests in tests."""
      if os.environ.get("CI"):
          import httpx
          monkeypatch.setattr(httpx.AsyncClient, "send",
              AsyncMock(side_effect=RuntimeError("Real HTTP blocked in CI")))
  ```
- Use `respx` (already a dev dependency) for declarative HTTP mocking in tests that need specific response fixtures.

#### Secret management for integration tests

**Problem:** Integration tests against real GitHub API need tokens but CI secrets must not leak.

**Solution:**
- Integration tests live in a separate `tests/integration/` directory, excluded from the default test run.
- Run integration tests only on `main` branch pushes (not PRs from forks):
  ```yaml
  integration-test:
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    steps:
      - name: Run integration tests
        env:
          GITHUB_TOKEN: ${{ secrets.INTEGRATION_GITHUB_TOKEN }}
        run: pytest tests/integration/ -v
  ```
- Use GitHub's `GITHUB_TOKEN` (automatically available) for read-only API tests where possible.
- Never log secrets — the structured logging PII filter (from section 2) catches accidental token logging.

#### Matrix testing across Python versions

**Problem:** Testing across 3.11, 3.12, 3.13 and 2 OSes = 6 jobs. Slow and potentially expensive.

**Solution:**
- Use `fail-fast: false` so all matrix jobs complete (don't cancel siblings on first failure).
- Cache pip packages with `actions/cache@v4` keyed on OS + Python version + `pyproject.toml` hash.
- Run linting and type checking only on Python 3.11 (single job) — syntax is the same across versions.
- Run tests on all matrix combinations (the main value of matrix testing).
- Windows testing is omitted unless user demand warrants it — the project is primarily Linux/macOS.

#### Caching dependencies for fast CI

**Problem:** `pip install` downloads and builds packages on every run, adding 30-60 seconds.

**Solution:**
- Use `actions/cache@v4` for the pip cache directory:
  ```yaml
  - uses: actions/cache@v4
    with:
      path: ~/.cache/pip
      key: ${{ runner.os }}-pip-${{ matrix.python-version }}-${{ hashFiles('pyproject.toml') }}
      restore-keys: |
        ${{ runner.os }}-pip-${{ matrix.python-version }}-
  ```
- For even faster CI, consider using `uv` instead of pip:
  ```yaml
  - name: Install uv
    uses: astral-sh/setup-uv@v4
  - name: Install dependencies
    run: uv pip install -e ".[dev]" --system
  ```

#### PR checks vs main branch workflows

**Problem:** PR workflows should be lightweight (fast feedback), while main branch workflows can be more thorough.

**Solution:**
- **PR checks:** lint + typecheck + test (Python 3.11 only on ubuntu-latest) — ~2 minutes.
- **Main branch:** Full matrix (3 Python versions × 2 OSes) + integration tests + migration check — ~5 minutes.
- Use `concurrency` with `cancel-in-progress: true` to cancel outdated PR runs:
  ```yaml
  concurrency:
    group: ${{ github.workflow }}-${{ github.ref }}
    cancel-in-progress: true
  ```
- Required status checks in GitHub branch protection: lint, typecheck, test (3.11/ubuntu).

### 4.5 Testing the CI Pipeline

```yaml
# Test the CI workflow locally with act:
act -j test --matrix python-version:3.11 --matrix os:ubuntu-latest

# Or use GitHub CLI to trigger a workflow run:
gh workflow run ci.yml --ref feature-branch
```

### 4.6 Deployment Impact

- **No production impact:** CI/CD workflows only affect the development process.
- **New dev dependencies:** `mypy>=1.8` and `types-PyYAML>=6.0` added to the `[dev]` group.
- **PyPI publishing:** Requires one-time setup of trusted publishing on PyPI (link GitHub repo to PyPI project).
- **GitHub branch protection:** Recommend enabling required status checks for lint, typecheck, and test jobs.

---

## 5. Performance Benchmarks

### 5.1 Overview

Establish baseline metrics for mining throughput, review latency, queue throughput, and memory usage. Add a `benchmarks/` directory with reproducible scripts using synthetic data and integrate into CI for trend tracking.

### 5.2 Dependency Additions

```toml
# pyproject.toml — add to [project.optional-dependencies]
bench = [
    "pytest-benchmark>=4.0",
    "memray>=1.11",
    "asv>=0.6",           # airspeed velocity — benchmark tracking over time
]
```

### 5.3 File-by-File Changes

#### New: `benchmarks/conftest.py`

Shared fixtures for benchmark scripts:

```python
"""Shared fixtures for performance benchmarks."""

from __future__ import annotations

import json
import random
import string
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def mock_github_client():
    """Create a mock GitHub API client with realistic response times."""
    client = AsyncMock()

    # Load fixture data
    with open(FIXTURES_DIR / "pr_data.json") as f:
        pr_data = json.load(f)
    with open(FIXTURES_DIR / "pr_files.json") as f:
        pr_files = json.load(f)
    with open(FIXTURES_DIR / "pr_diff.txt") as f:
        pr_diff = f.read()

    client.get_pull_request.return_value = pr_data
    client.get_pull_request_files.return_value = pr_files
    client.get_pull_request_diff.return_value = pr_diff
    client.post_review.return_value = {"id": 1}
    client.post_comment.return_value = {"id": 1}

    return client


@pytest.fixture
def synthetic_diff():
    """Generate a synthetic diff of configurable size."""
    def _make_diff(file_count: int = 10, lines_per_file: int = 50) -> str:
        diffs = []
        for i in range(file_count):
            filename = f"src/module_{i}/file_{i}.py"
            lines = []
            lines.append(f"diff --git a/{filename} b/{filename}")
            lines.append(f"--- a/{filename}")
            lines.append(f"+++ b/{filename}")
            lines.append(f"@@ -1,{lines_per_file} +1,{lines_per_file} @@")
            for j in range(lines_per_file):
                op = random.choice(["+", "-", " "])
                content = "".join(random.choices(string.ascii_lowercase, k=40))
                lines.append(f"{op}{content}")
            diffs.append("\n".join(lines))
        return "\n".join(diffs)
    return _make_diff


@pytest.fixture
def synthetic_reviews():
    """Generate synthetic review comment data for mining benchmarks."""
    def _make_reviews(count: int = 500) -> list[dict]:
        return [
            {
                "repo": f"org/repo-{i % 10}",
                "pr_number": i,
                "comment_body": "".join(random.choices(string.ascii_lowercase + " ", k=200)),
                "verdict": random.choice(["APPROVED", "CHANGES_REQUESTED", "COMMENTED", None]),
                "created_at": f"2026-01-{(i % 28) + 1:02d}T12:00:00Z",
                "file_path": f"src/file_{i}.py",
                "line": random.randint(1, 100),
            }
            for i in range(count)
        ]
    return _make_reviews
```

#### New: `benchmarks/fixtures/`

Static fixture files for reproducible benchmarks:

- `benchmarks/fixtures/pr_data.json` — realistic PR metadata (title, body, author, labels, etc.)
- `benchmarks/fixtures/pr_files.json` — list of 50 changed files with patches
- `benchmarks/fixtures/pr_diff.txt` — unified diff (~100KB, representing a medium PR)
- `benchmarks/fixtures/persona_profile.yaml` — sample persona profile for review benchmarks
- `benchmarks/fixtures/mining_comments.json` — 1000 synthetic review comments for analyzer benchmarks

#### New: `benchmarks/bench_mining.py`

Mining throughput benchmarks:

```python
"""Benchmarks for persona mining throughput."""

from __future__ import annotations

import pytest

from review_bot.persona.analyzer import PersonaAnalyzer


class TestMiningThroughput:
    """Measure persona analysis performance."""

    def test_analyze_500_comments(self, benchmark, synthetic_reviews):
        """Benchmark: analyze 500 review comments into a persona."""
        reviews = synthetic_reviews(count=500)
        analyzer = PersonaAnalyzer()

        result = benchmark(analyzer.analyze, reviews)
        assert result is not None

    def test_analyze_2000_comments(self, benchmark, synthetic_reviews):
        """Benchmark: analyze 2000 review comments (large persona)."""
        reviews = synthetic_reviews(count=2000)
        analyzer = PersonaAnalyzer()

        result = benchmark(analyzer.analyze, reviews)
        assert result is not None

    @pytest.mark.parametrize("count", [100, 500, 1000, 2000, 5000])
    def test_analyze_scaling(self, benchmark, synthetic_reviews, count):
        """Benchmark: measure analysis scaling with comment count."""
        reviews = synthetic_reviews(count=count)
        analyzer = PersonaAnalyzer()

        benchmark(analyzer.analyze, reviews)
```

#### New: `benchmarks/bench_review.py`

Review pipeline latency benchmarks:

```python
"""Benchmarks for review pipeline latency."""

from __future__ import annotations

import asyncio
import pytest

from review_bot.review.formatter import ReviewFormatter
from review_bot.review.prompt_builder import PromptBuilder
from review_bot.review.repo_scanner import RepoContext


class TestPromptBuilding:
    """Measure prompt construction performance."""

    def test_build_prompt_small_pr(self, benchmark, synthetic_diff):
        """Benchmark: build prompt for a 10-file PR."""
        builder = PromptBuilder()
        diff = synthetic_diff(file_count=10, lines_per_file=50)
        # ... setup persona and context
        benchmark(builder.build, persona=..., repo_context=..., pr_data=...,
                  diff=diff, files=...)

    def test_build_prompt_large_pr(self, benchmark, synthetic_diff):
        """Benchmark: build prompt for a 200-file PR."""
        builder = PromptBuilder()
        diff = synthetic_diff(file_count=200, lines_per_file=30)
        benchmark(builder.build, ...)


class TestFormatting:
    """Measure review output formatting performance."""

    def test_format_small_review(self, benchmark):
        """Benchmark: format a review with 5 inline comments."""
        formatter = ReviewFormatter()
        raw_output = "..."  # fixture
        benchmark(formatter.format, raw_output=raw_output,
                  persona_name="test", pr_url="https://github.com/o/r/pull/1")

    def test_format_large_review(self, benchmark):
        """Benchmark: format a review with 50 inline comments."""
        ...
```

#### New: `benchmarks/bench_queue.py`

Queue throughput benchmarks:

```python
"""Benchmarks for async job queue throughput."""

from __future__ import annotations

import asyncio
import pytest

from review_bot.server.queue import AsyncJobQueue, ReviewJob


class TestQueueThroughput:
    """Measure queue enqueue/dequeue performance."""

    @pytest.fixture
    async def queue(self, tmp_path):
        """Create a queue with an in-memory SQLite database."""
        from sqlalchemy.ext.asyncio import create_async_engine
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        # Initialize tables
        queue = AsyncJobQueue(db_engine=engine, github_auth=..., persona_store=...)
        yield queue
        await engine.dispose()

    async def test_enqueue_100_jobs(self, queue):
        """Benchmark: enqueue 100 jobs."""
        import time
        start = time.monotonic()
        for i in range(100):
            job = ReviewJob(
                owner="org", repo="repo", pr_number=i,
                persona_name="test", installation_id=1,
            )
            await queue.enqueue(job)
        duration = time.monotonic() - start
        # Assert < 5 seconds for 100 enqueues
        assert duration < 5.0
```

#### New: `benchmarks/bench_memory.py`

Memory usage profiling:

```python
"""Memory usage benchmarks using memray."""

from __future__ import annotations

import subprocess
import sys


def run_memory_profile(script: str, output: str) -> None:
    """Run a script under memray and save the profile."""
    subprocess.run(
        [sys.executable, "-m", "memray", "run", "--output", output, script],
        check=True,
    )


def test_review_memory_usage(tmp_path):
    """Profile memory usage during a review with synthetic data."""
    # Generate a review scenario
    # Run under memray
    # Assert peak memory < 500MB
    profile_path = tmp_path / "review_profile.bin"
    # ... run memray and check peak allocation
```

#### New: `.github/workflows/benchmarks.yml`

CI workflow for benchmark tracking:

```yaml
name: Benchmarks

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  benchmark:
    name: Run Benchmarks
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -e ".[dev,bench]"

      - name: Run benchmarks
        run: pytest benchmarks/ --benchmark-json=benchmark-results.json -v

      - name: Store benchmark results
        if: github.ref == 'refs/heads/main'
        uses: benchmark-action/github-action-benchmark@v1
        with:
          tool: "pytest"
          output-file-path: benchmark-results.json
          github-token: ${{ secrets.GITHUB_TOKEN }}
          auto-push: true
          alert-threshold: "120%"
          comment-on-alert: true
          fail-on-alert: false

      - name: Compare with baseline (PRs)
        if: github.event_name == 'pull_request'
        run: |
          echo "::notice::Benchmark results are in benchmark-results.json"
          python -c "
          import json
          with open('benchmark-results.json') as f:
              data = json.load(f)
          for bench in data.get('benchmarks', []):
              name = bench['name']
              mean = bench['stats']['mean']
              print(f'{name}: {mean:.4f}s')
          "
```

### 5.4 Metrics to Track

| Metric | What It Measures | Target Baseline | Regression Threshold |
|---|---|---|---|
| **Mining throughput** | Comments analyzed per second | >1000 comments/s | <800 comments/s |
| **Prompt build time** | Time to construct LLM prompt | <100ms for 50-file PR | >200ms |
| **Format time** | Time to parse and format LLM output | <50ms for 50 comments | >100ms |
| **Queue enqueue** | Time to persist and enqueue a job | <50ms per job | >100ms |
| **Peak memory (review)** | Max RSS during a review cycle | <200MB | >400MB |
| **Peak memory (mining)** | Max RSS during 5000-comment mining | <300MB | >500MB |

### 5.5 Edge Cases

#### Benchmark reproducibility across machines

**Problem:** Benchmark numbers vary across machines (CI runners vs. developer laptops).

**Solution:**
- Use **relative comparisons** (% change from baseline) rather than absolute times.
- CI benchmarks run on the same runner type (`ubuntu-latest`) for consistency.
- Store historical results in GitHub Pages (via `github-action-benchmark`) for trend tracking.
- Pin the runner OS and hardware class:
  ```yaml
  runs-on: ubuntu-latest  # Consistent 2-core runner
  ```
- Report both mean and standard deviation — high variance indicates a flaky benchmark.

#### Noise reduction

**Problem:** System noise (GC, OS scheduling, background processes) affects benchmark accuracy.

**Solution:**
- Use `pytest-benchmark`'s built-in warm-up and calibration:
  ```ini
  [tool.pytest.ini_options]
  benchmark_warmup = "on"
  benchmark_min_rounds = 5
  benchmark_min_time = "0.1"
  ```
- Run benchmarks with `--benchmark-disable-gc` to eliminate GC pauses.
- Use the **median** (not mean) for regression detection — more robust to outliers.
- CI benchmarks run in a fresh container with minimal background processes.

#### Regression detection thresholds

**Problem:** What percentage increase constitutes a "real" regression vs. noise?

**Solution:**
- Set `alert-threshold: "120%"` in the GitHub Action — alert if any benchmark is >20% slower than baseline.
- Use a two-sample t-test for statistical significance (pytest-benchmark supports this with `--benchmark-compare`).
- **Non-blocking alerts:** Regressions trigger a PR comment and GitHub annotation, but don't fail the build (set `fail-on-alert: false`). This avoids blocking PRs on noisy benchmarks while still surfacing regressions.
- Periodic manual review of benchmark trends (weekly) to catch slow regressions.

#### Benchmarking async code accurately

**Problem:** `pytest-benchmark` measures wall-clock time, but async code yields to the event loop between operations.

**Solution:**
- For async benchmarks, use a custom timer that measures only the time spent in the coroutine:
  ```python
  async def bench_async(benchmark, coro_factory):
      """Benchmark an async function by running it in an event loop."""
      def run():
          asyncio.get_event_loop().run_until_complete(coro_factory())
      benchmark(run)
  ```
- Alternatively, use `time.monotonic()` directly in async benchmarks and report custom metrics:
  ```python
  async def test_queue_throughput(tmp_path):
      start = time.monotonic()
      await enqueue_100_jobs(queue)
      duration = time.monotonic() - start
      # Report via pytest-benchmark or custom JSON output
  ```
- For true async profiling, use `yappi` in wall-clock mode — it tracks time across `await` points.

#### Memory profiling tools for Python

**Problem:** Python's memory management (reference counting + GC) makes accurate memory profiling challenging.

**Solution:**
- **memray** (primary): Tracks every allocation with minimal overhead. Produces flame graphs showing which code paths allocate the most memory. Install via `pip install memray`.
  ```bash
  # Profile a benchmark script
  python -m memray run benchmarks/bench_memory.py
  python -m memray flamegraph memray-output.bin -o flamegraph.html
  ```
- **tracemalloc** (lightweight): Built into Python stdlib. Good for tracking peak memory and allocation sites:
  ```python
  import tracemalloc
  tracemalloc.start()
  # ... run benchmark ...
  current, peak = tracemalloc.get_traced_memory()
  print(f"Peak memory: {peak / 1024 / 1024:.1f} MB")
  ```
- **CI integration:** Run memray in CI and archive the output as a build artifact for regression analysis:
  ```yaml
  - name: Memory profile
    run: python -m memray run -o profile.bin benchmarks/bench_memory.py
  - uses: actions/upload-artifact@v4
    with:
      name: memory-profile
      path: profile.bin
  ```

### 5.6 Testing Approach

```python
# tests/test_benchmarks.py — validate that benchmark infrastructure works

def test_synthetic_diff_fixture(synthetic_diff):
    """Synthetic diff generator produces valid diffs."""
    diff = synthetic_diff(file_count=5, lines_per_file=20)
    assert "diff --git" in diff
    assert diff.count("diff --git") == 5

def test_synthetic_reviews_fixture(synthetic_reviews):
    """Synthetic review generator produces expected structure."""
    reviews = synthetic_reviews(count=100)
    assert len(reviews) == 100
    assert all("comment_body" in r for r in reviews)

def test_benchmark_fixtures_exist():
    """All required fixture files are present."""
    fixtures = Path("benchmarks/fixtures")
    assert (fixtures / "pr_data.json").exists()
    assert (fixtures / "pr_files.json").exists()
    assert (fixtures / "pr_diff.txt").exists()
```

### 5.7 Deployment Impact

- **No production impact:** Benchmarks are dev/CI only.
- **New optional dependencies:** `pytest-benchmark`, `memray`, `asv` in the `[bench]` group (~20MB).
- **CI time:** Benchmark job adds ~3 minutes to CI on main branch pushes.
- **Storage:** GitHub Pages stores historical benchmark data (~1KB per run). No external infrastructure needed.

---

## Implementation Order

The recommended implementation order minimizes dependencies between items:

```
1. CI/CD Pipeline (no code dependencies, enables validation for all other items)
   ↓
2. Database Migration Framework (foundational — blocks future schema changes)
   ↓
3. Structured Logging (independent, but benefits from CI for validation)
   ↓
4. OpenTelemetry Tracing (builds on structured logging for log-trace correlation)
   ↓
5. Performance Benchmarks (benefits from all above; uses CI for trend tracking)
```

**Phase 1 (Week 1):** CI/CD Pipeline + Database Migration Framework
**Phase 2 (Week 2):** Structured Logging + OpenTelemetry Tracing
**Phase 3 (Week 3):** Performance Benchmarks + integration testing across all items

"""CLI commands for database management and migration."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("review-bot")


@click.group("db")
def db() -> None:
    """Database management commands."""


@db.command("migrate")
@click.option(
    "--source",
    required=True,
    help="Source SQLite database URL (sqlite+aiosqlite:///path/to/db)",
)
@click.option(
    "--target",
    required=True,
    help="Target PostgreSQL database URL (postgresql+asyncpg://...)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Export and report counts without importing",
)
def migrate_db(source: str, target: str, dry_run: bool) -> None:
    """Migrate data from SQLite to PostgreSQL.

    Args:
        source: SQLite database connection URL.
        target: PostgreSQL database connection URL.
        dry_run: If True, only export and display counts without importing.
    """
    # Warn about database URLs being visible in process listing
    click.echo(
        click.style(
            "⚠  Database URLs passed as CLI args are visible in process listings (ps). "
            "Consider using environment variables (REVIEW_BOT_DB_URL) for sensitive URLs.",
            fg="yellow",
        )
    )
    asyncio.run(_run_migration(source, target, dry_run))


async def _drop_migration_tables(engine: AsyncEngine) -> None:
    """Drop all migration tables from the target database for rollback.

    Drops tables in reverse dependency order to avoid FK constraint issues.

    Args:
        engine: SQLAlchemy async engine connected to the target database.
    """
    from sqlalchemy import text

    # Reverse of migration order to respect potential FK dependencies
    tables = ("review_feedback", "review_comment_tracking", "persona_stats", "jobs", "reviews")
    async with engine.begin() as conn:
        for table in tables:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    logger.info("Rolled back migration schema — dropped all migration tables")


async def _run_migration(source: str, target: str, dry_run: bool) -> None:
    """Execute the migration asynchronously.

    Args:
        source: SQLite database connection URL.
        target: PostgreSQL database connection URL.
        dry_run: If True, only export and display counts.
    """
    from review_bot.db.migration import (
        create_engine,
        export_sqlite_data,
        get_db_backend,
        import_to_postgresql,
        init_database,
    )

    # Validate source is SQLite
    try:
        source_backend = get_db_backend(source)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if source_backend != "sqlite":
        click.echo(
            f"Error: Source must be a SQLite URL, got: {source.split('://')[0]}",
            err=True,
        )
        sys.exit(1)

    # Validate target is PostgreSQL
    try:
        target_backend = get_db_backend(target)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if target_backend != "postgresql":
        click.echo(
            f"Error: Target must be a PostgreSQL URL, got: {target.split('://')[0]}",
            err=True,
        )
        sys.exit(1)

    # Connect to source
    try:
        source_engine = await create_engine(source)
    except Exception as exc:
        click.echo(f"Error connecting to source database: {exc}", err=True)
        sys.exit(1)

    try:
        # Export data
        click.echo("Exporting data from SQLite...")
        data = await export_sqlite_data(source_engine)

        click.echo("\nExport summary:")
        total = 0
        for table, rows in data.items():
            click.echo(f"  {table}: {len(rows)} rows")
            total += len(rows)
        click.echo(f"  Total: {total} rows")

        if dry_run:
            click.echo("\n[DRY RUN] No data was imported.")
            return

        # Connect to target and initialize schema
        try:
            target_engine = await create_engine(target)
        except Exception as exc:
            click.echo(f"Error connecting to target database: {exc}", err=True)
            sys.exit(1)

        try:
            click.echo("\nInitializing PostgreSQL schema...")
            await init_database(target_engine, "postgresql")

            click.echo("Importing data to PostgreSQL...")
            try:
                # import_to_postgresql uses engine.begin() internally,
                # so data inserts are automatically rolled back on exception.
                counts = await import_to_postgresql(target_engine, data)
            except Exception as import_exc:
                click.echo(
                    click.style(
                        f"\nImport failed: {import_exc}",
                        fg="red",
                    ),
                    err=True,
                )
                # Data inserts were rolled back by the transaction in
                # import_to_postgresql.  Now drop the schema tables we
                # created so the target DB is back to its pre-migration state.
                click.echo("Rolling back schema changes...", err=True)
                try:
                    await _drop_migration_tables(target_engine)
                    click.echo(
                        click.style(
                            "Rollback complete — target database restored to pre-migration state.",
                            fg="yellow",
                        ),
                        err=True,
                    )
                except Exception as rollback_exc:
                    click.echo(
                        click.style(
                            f"Rollback failed: {rollback_exc}. "
                            "Manual cleanup of target schema may be required.",
                            fg="red",
                        ),
                        err=True,
                    )
                sys.exit(1)

            click.echo("\nImport summary:")
            total_imported = 0
            for table, count in counts.items():
                click.echo(f"  {table}: {count} rows imported")
                total_imported += count
            click.echo(f"  Total: {total_imported} rows imported")

            click.echo("\nMigration complete!")
        finally:
            await target_engine.dispose()
    finally:
        await source_engine.dispose()

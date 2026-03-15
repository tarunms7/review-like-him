"""CLI commands for database management and migration."""

from __future__ import annotations

import asyncio
import logging
import sys

import click

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
    asyncio.run(_run_migration(source, target, dry_run))


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
            counts = await import_to_postgresql(target_engine, data)

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

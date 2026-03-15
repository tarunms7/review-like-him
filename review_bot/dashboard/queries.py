"""Dashboard query functions for review analytics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def get_review_counts(engine: AsyncEngine) -> dict[str, int]:
    """Return review counts for 24h, 7d, and 30d windows.

    Args:
        engine: SQLAlchemy async engine.

    Returns:
        Dict with keys '24h', '7d', '30d' mapping to integer counts.
    """
    now = datetime.now(tz=UTC)
    windows = {
        "24h": (now - timedelta(hours=24)).isoformat(),
        "7d": (now - timedelta(days=7)).isoformat(),
        "30d": (now - timedelta(days=30)).isoformat(),
    }
    result: dict[str, int] = {}
    async with engine.connect() as conn:
        for label, since in windows.items():
            row = await conn.execute(
                text("SELECT COUNT(*) FROM reviews WHERE created_at >= :since"),
                {"since": since},
            )
            result[label] = row.scalar_one()
    return result


async def get_activity_page(
    engine: AsyncEngine,
    *,
    page: int = 1,
    per_page: int = 50,
    persona: str | None = None,
    repo: str | None = None,
) -> tuple[list[dict], int]:
    """Return paginated activity rows with optional filters.

    Args:
        engine: SQLAlchemy async engine.
        page: Page number (1-indexed).
        per_page: Number of rows per page.
        persona: Optional persona name filter.
        repo: Optional repo filter.

    Returns:
        Tuple of (rows, total_count). Each row dict has: persona_name, repo,
        pr_number, pr_url, verdict, comment_count, duration_ms, created_at.
    """
    where_clauses: list[str] = []
    params: dict = {}

    if persona:
        where_clauses.append("persona_name = :persona")
        params["persona"] = persona
    if repo:
        where_clauses.append("repo = :repo")
        params["repo"] = repo

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    # Get total count
    async with engine.connect() as conn:
        count_row = await conn.execute(
            text(f"SELECT COUNT(*) FROM reviews {where_sql}"),  # noqa: E501
            params,
        )
        total_count = count_row.scalar_one()

        # Get paginated rows
        offset = (page - 1) * per_page
        query_params = {**params, "limit": per_page, "offset": offset}
        result = await conn.execute(
            text(
                f"SELECT persona_name, repo, pr_number, pr_url, verdict, "  # noqa: E501
                f"comment_count, duration_ms, created_at "
                f"FROM reviews {where_sql} "
                f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
            ),
            query_params,
        )
        rows = [dict(row._mapping) for row in result.fetchall()]

    return rows, total_count


async def get_persona_stats(engine: AsyncEngine) -> list[dict]:
    """Return aggregate stats per persona from reviews table.

    Args:
        engine: SQLAlchemy async engine.

    Returns:
        List of dicts with: persona_name, total_reviews, avg_comments,
        avg_duration_ms, approvals, change_requests.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT "
                "persona_name, "
                "COUNT(*) AS total_reviews, "
                "ROUND(AVG(comment_count), 1) AS avg_comments, "
                "ROUND(AVG(duration_ms), 0) AS avg_duration_ms, "
                "SUM(CASE WHEN verdict = 'approve' THEN 1 ELSE 0 END) AS approvals, "
                "SUM(CASE WHEN verdict = 'request_changes' THEN 1 ELSE 0 END) "
                "AS change_requests "
                "FROM reviews "
                "GROUP BY persona_name "
                "ORDER BY total_reviews DESC"
            )
        )
        return [dict(row._mapping) for row in result.fetchall()]


async def get_queue_snapshot(engine: AsyncEngine) -> dict[str, list[dict]]:
    """Return current queue state grouped by status.

    Args:
        engine: SQLAlchemy async engine.

    Returns:
        Dict with keys 'queued', 'running', 'failed'. Each value is a list of
        job dicts with: id, owner, repo, pr_number, persona_name, status,
        queued_at, started_at, completed_at, error_message.
    """
    since_24h = (datetime.now(tz=UTC) - timedelta(hours=24)).isoformat()
    columns = (
        "id, owner, repo, pr_number, persona_name, status, "
        "queued_at, started_at, completed_at, error_message"
    )

    snapshot: dict[str, list[dict]] = {"queued": [], "running": [], "failed": []}

    async with engine.connect() as conn:
        # Queued jobs
        result = await conn.execute(
            text(f"SELECT {columns} FROM jobs WHERE status = 'queued' ORDER BY queued_at ASC"),
        )
        snapshot["queued"] = [dict(row._mapping) for row in result.fetchall()]

        # Running jobs
        result = await conn.execute(
            text(f"SELECT {columns} FROM jobs WHERE status = 'running' ORDER BY started_at ASC"),
        )
        snapshot["running"] = [dict(row._mapping) for row in result.fetchall()]

        # Failed jobs (last 24h only)
        result = await conn.execute(
            text(
                f"SELECT {columns} FROM jobs "
                f"WHERE status = 'failed' AND completed_at >= :since "
                f"ORDER BY completed_at DESC"
            ),
            {"since": since_24h},
        )
        snapshot["failed"] = [dict(row._mapping) for row in result.fetchall()]

    return snapshot


async def get_reviews_per_day(
    engine: AsyncEngine,
    persona: str | None = None,
    days: int = 30,
) -> list[dict]:
    """Return daily review counts for the specified window.

    Args:
        engine: SQLAlchemy async engine.
        persona: Optional persona name filter.
        days: Number of days to look back.

    Returns:
        List of dicts with 'date' (str) and 'count' (int).
    """
    since = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()
    params: dict = {"since": since}

    where = "WHERE created_at >= :since"
    if persona:
        where += " AND persona_name = :persona"
        params["persona"] = persona

    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                f"SELECT DATE(created_at) AS date, COUNT(*) AS count "
                f"FROM reviews {where} "
                f"GROUP BY DATE(created_at) "
                f"ORDER BY date ASC"
            ),
            params,
        )
        return [dict(row._mapping) for row in result.fetchall()]

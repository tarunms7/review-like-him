"""Status endpoints exposing system state, rate limits, and job history."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("review-bot")

router = APIRouter(tags=["status"])


async def _get_recent_jobs(request: Request, limit: int = 10) -> list[dict] | None:
    """Fetch recent jobs from the database."""
    db_engine = getattr(request.app.state, "db_engine", None)
    if db_engine is None:
        return None

    try:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, owner, repo, pr_number, persona_name, "
                    "status, queued_at, started_at, completed_at "
                    "FROM jobs ORDER BY queued_at DESC LIMIT :limit"
                ),
                {"limit": limit},
            )
            rows = result.fetchall()
            return [
                {
                    "id": row[0],
                    "owner": row[1],
                    "repo": row[2],
                    "pr_number": row[3],
                    "persona_name": row[4],
                    "status": row[5],
                    "queued_at": row[6],
                    "started_at": row[7],
                    "completed_at": row[8],
                }
                for row in rows
            ]
    except SQLAlchemyError:
        logger.exception("Failed to fetch recent jobs")
        return None


@router.get("/status")
async def status(request: Request) -> dict:
    """Return current system status: rate limits, queue depth, active jobs.

    Returns:
        Dict with status, reason, rate_limits, queue, and recent_jobs.
    """
    tracker = getattr(request.app.state, "rate_limit_tracker", None)
    job_queue = getattr(request.app.state, "job_queue", None)

    # Rate limits
    rate_limits = None
    if tracker is not None:
        snapshot = tracker.snapshot()
        rate_limits = {
            resource: {
                "remaining": snap.remaining,
                "limit": snap.limit,
                "used": snap.used,
                "reset": snap.reset,
                "last_updated": snap.last_updated,
            }
            for resource, snap in snapshot.items()
        }

    # Queue info
    queue_info = None
    if job_queue is not None:
        queue_info = {
            "depth": job_queue.queue_depth,
            "worker_status": job_queue.worker_status,
            "current_job_id": job_queue.current_job_id,
        }

    # Recent jobs from DB
    recent_jobs = await _get_recent_jobs(request)

    return {
        "status": "ok" if tracker is not None else "degraded",
        "reason": None if tracker is not None else "Rate limit tracker not initialized",
        "rate_limits": rate_limits,
        "queue": queue_info,
        "recent_jobs": recent_jobs,
    }


@router.get("/status/jobs/{pr_number}")
async def job_status_by_pr(request: Request, pr_number: int) -> dict:
    """Return job statuses for a specific PR number.

    Args:
        pr_number: The pull request number to look up.

    Returns:
        Dict with pr_number and list of job records for that PR.
    """
    db_engine = getattr(request.app.state, "db_engine", None)
    if db_engine is None:
        return {"pr_number": pr_number, "jobs": []}

    try:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, owner, repo, pr_number, persona_name, "
                    "status, queued_at, started_at, completed_at, error_message "
                    "FROM jobs WHERE pr_number = :pr_number "
                    "ORDER BY queued_at DESC LIMIT 20"
                ),
                {"pr_number": pr_number},
            )
            rows = result.fetchall()
            jobs = [
                {
                    "id": row[0],
                    "owner": row[1],
                    "repo": row[2],
                    "pr_number": row[3],
                    "persona_name": row[4],
                    "status": row[5],
                    "queued_at": row[6],
                    "started_at": row[7],
                    "completed_at": row[8],
                    "error_message": row[9],
                }
                for row in rows
            ]
    except SQLAlchemyError:
        logger.exception("Failed to fetch jobs for PR #%d", pr_number)
        return {"pr_number": pr_number, "jobs": [], "error": "Database error"}

    return {"pr_number": pr_number, "jobs": jobs}

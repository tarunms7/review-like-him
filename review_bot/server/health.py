"""Health check endpoints for monitoring and orchestration probes."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from review_bot.github.app import GitHubAppAuth
    from review_bot.github.rate_limits import RateLimitTracker
    from review_bot.server.queue import AsyncJobQueue

logger = logging.getLogger("review-bot")

# Module-level state for uptime tracking
_start_time: float | None = None

router = APIRouter(tags=["health"])


def set_start_time() -> None:
    """Record application start time for uptime calculation."""
    global _start_time  # noqa: PLW0603
    _start_time = time.monotonic()


def _uptime_seconds() -> float:
    """Return seconds since application start, or 0.0 if not started."""
    if _start_time is None:
        return 0.0
    return round(time.monotonic() - _start_time, 1)


@dataclass
class CheckResult:
    """Result of a single health check.

    Args:
        status: Check status — 'pass', 'fail', or 'warn'.
        detail: Human-readable detail about the check result.
        duration_ms: Time taken to run the check in milliseconds, or None.
    """

    status: str
    detail: str
    duration_ms: float | None = field(default=None)

    def to_dict(self) -> dict:
        """Convert to a plain dict for JSON serialization.

        Returns:
            Dict with status, detail, and optional duration_ms.
        """
        result: dict = {"status": self.status, "detail": self.detail}
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        return result


async def _check_database(engine: AsyncEngine) -> CheckResult:
    """Check database connectivity by running SELECT 1 with a 5s timeout.

    Args:
        engine: SQLAlchemy AsyncEngine instance.

    Returns:
        CheckResult with pass/fail status and latency info.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError, OperationalError

    t0 = time.monotonic()
    try:
        async with engine.connect() as conn:
            await asyncio.wait_for(conn.execute(text("SELECT 1")), timeout=5.0)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return CheckResult(
            status="pass",
            detail=f"Connected (latency: {int(elapsed_ms)}ms)",
            duration_ms=elapsed_ms,
        )
    except TimeoutError:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return CheckResult(
            status="fail",
            detail="Database query timed out (>5s)",
            duration_ms=elapsed_ms,
        )
    except OperationalError as exc:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return CheckResult(
            status="fail",
            detail=f"Database error: {exc}",
            duration_ms=elapsed_ms,
        )
    except (DatabaseError, OSError) as exc:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        logger.warning("Database health check failed: %s: %s", type(exc).__name__, exc)
        return CheckResult(
            status="fail",
            detail=f"Database error ({type(exc).__name__}): {exc}",
            duration_ms=elapsed_ms,
        )


async def _check_queue(job_queue: AsyncJobQueue) -> CheckResult:
    """Check job queue status including depth and worker state.

    Args:
        job_queue: AsyncJobQueue instance.

    Returns:
        CheckResult with queue depth and worker status details.
    """
    t0 = time.monotonic()
    try:
        depth = job_queue.queue_depth
        status = job_queue.worker_status
        job_id = job_queue.current_job_id

        detail = (
            f"queue_depth={depth}, "
            f"worker_status={status}, "
            f"current_job_id={job_id}"
        )
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

        if status == "dead":
            return CheckResult(status="fail", detail=detail, duration_ms=elapsed_ms)
        if status == "stopped":
            return CheckResult(status="fail", detail=detail, duration_ms=elapsed_ms)
        if depth > 10:
            return CheckResult(status="warn", detail=detail, duration_ms=elapsed_ms)
        return CheckResult(status="pass", detail=detail, duration_ms=elapsed_ms)
    except (AttributeError, RuntimeError) as exc:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        logger.warning("Queue health check failed: %s: %s", type(exc).__name__, exc)
        return CheckResult(
            status="fail",
            detail=f"Queue check error ({type(exc).__name__}): {exc}",
            duration_ms=elapsed_ms,
        )


async def _check_github_rate_limit(rate_limit_tracker: RateLimitTracker | None) -> CheckResult:
    """Check GitHub API rate limit status from cached tracker data.

    Args:
        rate_limit_tracker: RateLimitTracker instance or None.

    Returns:
        CheckResult with rate limit info or informative default.
    """
    t0 = time.monotonic()
    try:
        if rate_limit_tracker is None:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
            return CheckResult(
                status="pass",
                detail="No rate limit data available yet",
                duration_ms=elapsed_ms,
            )

        snapshot = rate_limit_tracker.snapshot()
        if not snapshot:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
            return CheckResult(
                status="pass",
                detail="No rate limit data available yet",
                duration_ms=elapsed_ms,
            )

        # Summarize core resource if available, otherwise first resource
        parts = []
        has_warning = False
        for resource_name, state in snapshot.items():
            parts.append(f"{resource_name}: {state.remaining}/{state.limit} remaining")
            if state.remaining <= 10:
                has_warning = True

        detail = ", ".join(parts)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        status = "warn" if has_warning else "pass"
        return CheckResult(status=status, detail=detail, duration_ms=elapsed_ms)
    except (AttributeError, KeyError, ValueError, TypeError) as exc:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        logger.warning("Rate limit health check failed: %s: %s", type(exc).__name__, exc)
        return CheckResult(
            status="pass",
            detail=f"Rate limit check unavailable ({type(exc).__name__}): {exc}",
            duration_ms=elapsed_ms,
        )


async def _check_github_app(github_auth: GitHubAppAuth) -> CheckResult:
    """Check GitHub App authentication status.

    Args:
        github_auth: GitHubAppAuth instance.

    Returns:
        CheckResult with App ID and installation count.
    """
    t0 = time.monotonic()
    try:
        app_id = github_auth._app_id
        token_count = len(github_auth._token_cache)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return CheckResult(
            status="pass",
            detail=f"App ID: {app_id}, installations: {token_count}",
            duration_ms=elapsed_ms,
        )
    except (AttributeError, TypeError) as exc:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        logger.warning("GitHub App health check failed: %s: %s", type(exc).__name__, exc)
        return CheckResult(
            status="fail",
            detail=f"GitHub App check error ({type(exc).__name__}): {exc}",
            duration_ms=elapsed_ms,
        )


@router.get("/healthz")
async def healthz() -> dict:
    """Kubernetes-style liveness probe. Always returns 200.

    Returns:
        Simple alive status dict.
    """
    return {"status": "alive"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict:
    """Kubernetes-style readiness probe.

    Returns 200 only if database is connected AND worker is running,
    else 503.

    Returns:
        Readiness status with check details.
    """
    db_check = await _check_database(request.app.state.db_engine)

    # Worker check: simplified from queue check
    job_queue = request.app.state.job_queue
    worker_st = job_queue.worker_status
    if worker_st == "running":
        worker_check = CheckResult(status="pass", detail=f"worker_status={worker_st}")
    else:
        worker_check = CheckResult(status="fail", detail=f"worker_status={worker_st}")

    checks = {
        "database": db_check.to_dict(),
        "worker": worker_check.to_dict(),
    }

    is_ready = db_check.status == "pass" and worker_check.status == "pass"
    if not is_ready:
        response.status_code = 503

    return {
        "status": "ready" if is_ready else "not_ready",
        "checks": checks,
    }


@router.get("/health")
async def health(request: Request, response: Response) -> dict:
    """Full health check returning status of all subsystems.

    Returns 200 if all critical checks pass/warn, 503 if any critical
    check (database, worker) fails.

    Returns:
        Health status with version, uptime, and all check results.
    """
    db_check, queue_check, rate_limit_check, app_check = await asyncio.gather(
        _check_database(request.app.state.db_engine),
        _check_queue(request.app.state.job_queue),
        _check_github_rate_limit(
            getattr(request.app.state, "rate_limit_tracker", None),
        ),
        _check_github_app(request.app.state.github_auth),
    )

    checks = {
        "database": db_check.to_dict(),
        "queue": queue_check.to_dict(),
        "github_rate_limit": rate_limit_check.to_dict(),
        "github_app": app_check.to_dict(),
    }

    # Critical checks: database and queue (which includes worker status)
    critical_failed = db_check.status == "fail" or queue_check.status == "fail"

    if critical_failed:
        response.status_code = 503
        status = "unhealthy"
    else:
        status = "healthy"

    return {
        "status": status,
        "version": "0.1.0",
        "uptime_seconds": _uptime_seconds(),
        "checks": checks,
    }

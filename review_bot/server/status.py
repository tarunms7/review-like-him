"""Status endpoint exposing GitHub API rate limit state."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

logger = logging.getLogger("review-bot")

router = APIRouter(tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict:
    """Return current GitHub API rate limit state per resource.

    Returns:
        Dict with status and rate_limits keyed by resource name,
        or degraded status if tracker is not initialized.
    """
    tracker = getattr(request.app.state, "rate_limit_tracker", None)

    if tracker is None:
        return {
            "status": "degraded",
            "reason": "Rate limit tracker not initialized",
        }

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

    return {
        "status": "ok",
        "rate_limits": rate_limits,
    }

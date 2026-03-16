"""Status endpoint exposing GitHub API rate limit state."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Header, HTTPException, Request

logger = logging.getLogger("review-bot")

router = APIRouter(tags=["status"])

# API key for status endpoint authentication (set via env var)
_STATUS_API_KEY: str = os.environ.get("REVIEW_BOT_STATUS_API_KEY", "")


def _is_localhost(request: Request) -> bool:
    """Check if the request originates from localhost."""
    if request.client is None:
        return False
    host = request.client.host
    return host in ("127.0.0.1", "::1", "localhost")


def _authenticate_status_request(
    request: Request,
    api_key: str | None,
) -> None:
    """Authenticate /status requests via API key or localhost origin.

    Raises HTTPException(401) if no credentials provided.
    Raises HTTPException(403) if API key is invalid.
    """
    # Allow localhost requests without API key
    if _is_localhost(request):
        return

    # Require API key for non-localhost requests
    if not api_key:
        raise HTTPException(status_code=401, detail="Authentication required")

    if not _STATUS_API_KEY:
        # No API key configured server-side; reject non-localhost
        raise HTTPException(status_code=403, detail="Status API key not configured")

    if api_key != _STATUS_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


@router.get("/status")
async def status(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Return current GitHub API rate limit state per resource.

    Requires either an API key header (X-API-Key) or localhost origin.

    Returns:
        Dict with status and rate_limits keyed by resource name,
        or degraded status if tracker is not initialized.
    """
    _authenticate_status_request(request, x_api_key)

    tracker = getattr(request.app.state, "rate_limit_tracker", None)

    if tracker is None:
        return {
            "status": "degraded",
            "reason": "Rate limit tracker not initialized",
            "rate_limits": None,
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
        "reason": None,
        "rate_limits": rate_limits,
    }

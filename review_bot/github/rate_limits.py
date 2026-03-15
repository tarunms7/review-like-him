from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResourceSnapshot:
    """Immutable snapshot of rate limit state for a GitHub API resource."""

    remaining: int
    limit: int
    used: int
    reset: int
    last_updated: str


class RateLimitTracker:
    """Thread-safe singleton that tracks GitHub API rate limit state."""

    _instance: RateLimitTracker | None = None
    _init_lock = threading.Lock()

    def __new__(cls) -> RateLimitTracker:
        with cls._init_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._lock = threading.Lock()
                inst._resources: dict[str, ResourceSnapshot] = {}
                cls._instance = inst
            return cls._instance

    @classmethod
    def infer_resource(cls, url: str) -> str:
        """Map URL path to resource type."""
        if "/search/" in url:
            return "search"
        if "/graphql" in url:
            return "graphql"
        return "core"

    def update_from_response(
        self,
        url: str,
        headers: dict[str, str],
    ) -> None:
        """Parse rate limit headers and update resource state."""
        remaining = headers.get("X-RateLimit-Remaining")
        limit = headers.get("X-RateLimit-Limit")
        reset = headers.get("X-RateLimit-Reset")
        used = headers.get("X-RateLimit-Used")

        if remaining is None or limit is None:
            return

        try:
            snap = ResourceSnapshot(
                remaining=int(remaining),
                limit=int(limit),
                used=int(used) if used is not None else 0,
                reset=int(reset) if reset is not None else 0,
                last_updated=datetime.now(
                    timezone.utc
                ).replace(microsecond=0).isoformat(),
            )
        except (ValueError, TypeError):
            logger.warning(
                "Failed to parse rate limit headers for %s", url
            )
            return

        resource = self.infer_resource(url)
        with self._lock:
            self._resources[resource] = snap

    def snapshot(self) -> dict[str, ResourceSnapshot]:
        """Return current rate limit state keyed by resource name."""
        with self._lock:
            return dict(self._resources)

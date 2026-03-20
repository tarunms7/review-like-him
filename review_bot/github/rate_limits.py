from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

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
                # threading.Lock is safe here: no awaits inside locked sections.
                inst._lock = threading.Lock()
                inst._resources: dict[str, ResourceSnapshot] = {}
                cls._instance = inst
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Destroy the singleton instance (useful for testing)."""
        with cls._init_lock:
            cls._instance = None

    @staticmethod
    def infer_resource(url: str) -> str:
        """Map URL path to GitHub rate-limit resource bucket.

        GitHub buckets: ``search``, ``graphql``, and ``core`` (everything
        else).  We further split ``core`` into granular internal names for
        issues/pulls comment endpoints so callers can observe per-resource
        pressure without relying on the GitHub ``/rate_limit`` endpoint.
        """
        if "/search/" in url:
            return "search"
        if "/graphql" in url:
            return "graphql"
        if "/issues/" in url:
            return "issues"
        if "/pulls/" in url:
            return "pulls"
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
                    UTC
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

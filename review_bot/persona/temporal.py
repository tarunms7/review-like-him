"""Temporal weighting functions for review comments based on age."""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def weight_comment(comment_date: datetime) -> float:
    """Return a weight based on comment age.

    Last 3 months: 3.0x, 3-12 months: 1.5x, 12+ months: 0.5x.
    """
    now = datetime.now(UTC)

    if comment_date.tzinfo is None:
        comment_date = comment_date.replace(tzinfo=UTC)

    age_days = (now - comment_date).days

    if age_days <= 90:
        return 3.0
    if age_days <= 365:
        return 1.5
    return 0.5


def apply_weights(comments: list[dict]) -> list[dict]:
    """Apply temporal weights to a collection of review comments.

    Each comment dict must have a 'created_at' key (ISO 8601 string or datetime).
    Returns a new list of comment dicts with an added 'weight' field.
    """
    weighted: list[dict] = []
    for comment in comments:
        entry = copy.deepcopy(comment)
        created_at = entry.get("created_at")
        if created_at is None:
            logger.warning(
                "Comment missing 'created_at', assigning weight 0.0: %s",
                str(entry.get("comment_body", ""))[:80],
            )
            entry["weight"] = 0.0
            weighted.append(entry)
            continue
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except ValueError:
                logger.warning(
                    "Unparseable 'created_at' value '%s', assigning weight 0.0",
                    created_at,
                )
                entry["weight"] = 0.0
                weighted.append(entry)
                continue
        temporal_w = weight_comment(created_at)
        dedup_w = entry.get("dedup_weight", 1.0)
        entry["weight"] = temporal_w * dedup_w
        weighted.append(entry)
    return weighted

"""Thread-aware deduplication and weighting for mined review comments."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Weight constants
ORIGINAL_COMMENT_WEIGHT: float = 1.0
REPLY_WEIGHT: float = 0.3
SELF_REPLY_WEIGHT: float = 0.2
SUBSTANTIVE_REPLY_WEIGHT: float = 0.7
SUBSTANTIVE_MIN_LENGTH: int = 100

# Trivial reply patterns (matched case-insensitively against stripped body)
_TRIVIAL_PATTERNS: list[str] = [
    "fixed",
    "done",
    "good point",
    "thanks",
    "will do",
    "updated",
    "addressed",
    "agreed",
    "ack",
    "lgtm",
    "+1",
    "nit",
    "sg",
    "sgtm",
]

# Regex for technical patterns (type annotations, function calls, variable refs)
_TECHNICAL_RE = re.compile(
    r"(`[^`]+`|->|::\w|def\s|class\s|import\s|return\s|\w+\.\w+\()",
)


def _find_thread_root(
    comment_id: int,
    parent_map: dict[int, int | None],
    visited: set[int] | None = None,
) -> int:
    """Walk the reply chain to find the thread root comment.

    Args:
        comment_id: The comment whose root we want.
        parent_map: Mapping of comment_id → in_reply_to_id.
        visited: Set of already-visited IDs for cycle detection.

    Returns:
        The comment_id of the thread root.
    """
    if visited is None:
        visited = set()

    if comment_id in visited:
        # Cycle detected — treat current as root
        return comment_id

    visited.add(comment_id)

    parent_id = parent_map.get(comment_id)
    if parent_id is None or parent_id not in parent_map:
        # No parent or parent was deleted — this is the root
        return comment_id

    return _find_thread_root(parent_id, parent_map, visited)


def _classify_reply(
    comment: dict,
    all_comments_by_id: dict[int, dict],
) -> float:
    """Classify a reply comment and return its dedup weight.

    Args:
        comment: The reply comment dict.
        all_comments_by_id: Lookup of all comments by their comment_id.

    Returns:
        A weight float from the dedup weight constants.
    """
    body = comment.get("comment_body", "")
    parent_id = comment.get("in_reply_to_id")

    # Check if self-reply (same user as parent)
    if parent_id is not None and parent_id in all_comments_by_id:
        parent = all_comments_by_id[parent_id]
        comment_user = comment.get("user", "")
        parent_user = parent.get("user", "")
        if comment_user and parent_user and comment_user == parent_user:
            return SELF_REPLY_WEIGHT

    # Check if trivial
    stripped = body.strip().lower()
    if stripped in _TRIVIAL_PATTERNS or len(body.strip()) < 20:
        return REPLY_WEIGHT

    # Check if substantive
    if (
        len(body) > SUBSTANTIVE_MIN_LENGTH
        or "```" in body
        or _TECHNICAL_RE.search(body)
    ):
        return SUBSTANTIVE_REPLY_WEIGHT

    return REPLY_WEIGHT


def resolve_threads(comments: list[dict]) -> list[dict]:
    """Annotate comments with thread resolution and dedup weights.

    Builds a parent map from comment_id → in_reply_to_id, resolves thread
    roots, and assigns dedup_weight to each comment.

    Args:
        comments: List of comment dicts (DedupComment shape).

    Returns:
        The same list of comment dicts, mutated in-place with is_reply,
        thread_root_id, and dedup_weight fields added.
    """
    # Build lookup structures
    parent_map: dict[int, int | None] = {}
    all_comments_by_id: dict[int, dict] = {}

    for comment in comments:
        cid = comment.get("comment_id")
        if cid is None:
            logger.warning(
                "Comment missing comment_id, treating as standalone: %s",
                comment.get("comment_body", "")[:80],
            )
            continue
        parent_map[cid] = comment.get("in_reply_to_id")
        all_comments_by_id[cid] = comment

    # Annotate each comment
    for comment in comments:
        cid = comment.get("comment_id")

        if cid is None:
            # Standalone — no thread info available
            comment["is_reply"] = False
            comment["thread_root_id"] = None
            comment["dedup_weight"] = ORIGINAL_COMMENT_WEIGHT
            continue

        reply_to = comment.get("in_reply_to_id")
        thread_root = _find_thread_root(cid, parent_map)

        if reply_to is not None:
            comment["is_reply"] = True
            comment["thread_root_id"] = thread_root
            comment["dedup_weight"] = _classify_reply(comment, all_comments_by_id)
        else:
            comment["is_reply"] = False
            comment["thread_root_id"] = thread_root
            comment["dedup_weight"] = ORIGINAL_COMMENT_WEIGHT

    return comments


def collapse_threads(comments: list[dict], username: str) -> list[dict]:
    """Resolve threads across all users, then filter to target user's comments.

    Calls resolve_threads on ALL comments (including other users for context),
    then filters to only the target username's comments.

    Args:
        comments: List of all comment dicts from all users.
        username: GitHub username to filter for.

    Returns:
        List of the target user's comments with dedup_weight applied.
    """
    resolve_threads(comments)

    return [
        c for c in comments
        if c.get("user", "").lower() == username.lower()
    ]

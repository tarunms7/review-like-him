"""Feedback collection and storage for review comment reactions.

Tracks reactions and replies on bot-posted review comments, stores them
in the database, and provides aggregated feedback summaries for persona
refinement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("review-bot")

# ── Table DDL (SQLite) ────────────────────────────────────────────────

CREATE_REVIEW_COMMENT_TRACKING_SQL = """
CREATE TABLE IF NOT EXISTS review_comment_tracking (
    comment_id INTEGER PRIMARY KEY,
    review_id TEXT NOT NULL,
    persona_name TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    file_path TEXT,
    line_number INTEGER,
    body TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    posted_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_polled_at TEXT
)
"""

CREATE_REVIEW_FEEDBACK_SQL = """
CREATE TABLE IF NOT EXISTS review_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL,
    feedback_type TEXT NOT NULL,
    feedback_source TEXT NOT NULL,
    reactor_username TEXT NOT NULL,
    is_pr_author INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(comment_id, feedback_type, feedback_source, reactor_username)
)
"""

CREATE_FEEDBACK_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_feedback_comment ON review_feedback(comment_id)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_created ON review_feedback(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tracking_persona ON review_comment_tracking(persona_name)",
    "CREATE INDEX IF NOT EXISTS idx_tracking_repo ON review_comment_tracking(repo)",
]


@dataclass
class FeedbackEvent:
    """A single feedback event from a reaction, reply, or dismissal.

    Attributes:
        comment_id: GitHub comment ID that received the feedback.
        feedback_type: Feedback signal: 'positive', 'negative', 'confused', 'neutral'.
        feedback_source: Source of feedback: 'reaction', 'reply', 'dismissed'.
        reactor_username: GitHub username of the person who gave feedback.
        is_pr_author: Whether the reactor is the PR author (weighted 2x).
    """

    comment_id: int
    feedback_type: str
    feedback_source: str
    reactor_username: str
    is_pr_author: bool


@dataclass
class FeedbackSummary:
    """Aggregated feedback for a review category.

    Attributes:
        category: Review category name (e.g. 'Security', 'Bugs').
        positive_count: Number of positive feedback events (weighted).
        negative_count: Number of negative feedback events (weighted).
        total_comments: Total comments in this category.
        approval_rate: Float [0.0, 1.0] representing positive / (positive + negative).
        sample_positive: Sample of positively-received comment bodies.
        sample_negative: Sample of negatively-received comment bodies.
    """

    category: str
    positive_count: int
    negative_count: int
    total_comments: int
    approval_rate: float
    sample_positive: list[str] = field(default_factory=list)
    sample_negative: list[str] = field(default_factory=list)


class FeedbackStore:
    """Service class for storing and querying review feedback.

    Provides methods to record feedback events, track posted comments,
    and generate aggregated feedback summaries per category.

    Args:
        engine: SQLAlchemy async engine for database operations.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Initialize with SQLAlchemy async engine."""
        self._engine = engine

    async def ensure_tables(self) -> None:
        """Create feedback tables if they don't exist."""
        async with self._engine.begin() as conn:
            await conn.execute(text(CREATE_REVIEW_COMMENT_TRACKING_SQL))
            await conn.execute(text(CREATE_REVIEW_FEEDBACK_SQL))
            for idx_sql in CREATE_FEEDBACK_INDEXES_SQL:
                await conn.execute(text(idx_sql))
        logger.info("Feedback tables initialized")

    async def record_feedback(self, event: FeedbackEvent) -> None:
        """Insert a feedback event with UNIQUE constraint dedup.

        If a duplicate event (same comment_id, feedback_type, feedback_source,
        reactor_username) already exists, the insert is silently skipped.

        Args:
            event: The feedback event to record.
        """
        sql = text("""
            INSERT OR IGNORE INTO review_feedback
                (comment_id, feedback_type, feedback_source,
                 reactor_username, is_pr_author, created_at)
            VALUES
                (:comment_id, :feedback_type, :feedback_source,
                 :reactor_username, :is_pr_author, :created_at)
        """)
        async with self._engine.begin() as conn:
            await conn.execute(sql, {
                "comment_id": event.comment_id,
                "feedback_type": event.feedback_type,
                "feedback_source": event.feedback_source,
                "reactor_username": event.reactor_username,
                "is_pr_author": 1 if event.is_pr_author else 0,
                "created_at": datetime.now(UTC).isoformat(),
            })

    async def track_posted_comment(
        self,
        comment_id: int,
        review_id: str,
        persona_name: str,
        repo: str,
        pr_number: int,
        file_path: str | None,
        line_number: int | None,
        body: str,
        category: str,
    ) -> None:
        """Insert a tracked comment into the review_comment_tracking table.

        Args:
            comment_id: GitHub comment ID.
            review_id: Internal review ID.
            persona_name: Name of the persona that posted the comment.
            repo: Repository full name (owner/repo).
            pr_number: Pull request number.
            file_path: File path for inline comments, or None.
            line_number: Line number for inline comments, or None.
            body: Comment body text.
            category: Review category (e.g. 'Security', 'Bugs').
        """
        sql = text("""
            INSERT OR IGNORE INTO review_comment_tracking
                (comment_id, review_id, persona_name, repo, pr_number,
                 file_path, line_number, body, category, posted_at)
            VALUES
                (:comment_id, :review_id, :persona_name, :repo, :pr_number,
                 :file_path, :line_number, :body, :category, :posted_at)
        """)
        async with self._engine.begin() as conn:
            await conn.execute(sql, {
                "comment_id": comment_id,
                "review_id": review_id,
                "persona_name": persona_name,
                "repo": repo,
                "pr_number": pr_number,
                "file_path": file_path,
                "line_number": line_number,
                "body": body,
                "category": category,
                "posted_at": datetime.now(UTC).isoformat(),
            })

    async def get_persona_feedback_summary(
        self,
        persona_name: str,
        since_days: int = 90,
    ) -> list[FeedbackSummary]:
        """Aggregate feedback per category, weighting PR author reactions 2x.

        Args:
            persona_name: Name of the persona to query feedback for.
            since_days: Only consider feedback from the last N days.

        Returns:
            List of FeedbackSummary, one per category with feedback.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()

        # Get all categories with their comments for this persona
        categories_sql = text("""
            SELECT DISTINCT t.category
            FROM review_comment_tracking t
            WHERE t.persona_name = :persona_name
              AND t.posted_at >= :cutoff
        """)

        async with self._engine.connect() as conn:
            cat_result = await conn.execute(
                categories_sql, {"persona_name": persona_name, "cutoff": cutoff}
            )
            categories = [row[0] for row in cat_result.fetchall()]

        summaries: list[FeedbackSummary] = []
        for category in categories:
            summary = await self._build_category_summary(
                persona_name, category, cutoff
            )
            summaries.append(summary)

        return summaries

    async def _build_category_summary(
        self,
        persona_name: str,
        category: str,
        cutoff: str,
    ) -> FeedbackSummary:
        """Build a FeedbackSummary for a single category.

        Args:
            persona_name: Persona name.
            category: Category to summarize.
            cutoff: ISO datetime cutoff string.

        Returns:
            FeedbackSummary for the category.
        """
        # Count total comments in category
        total_sql = text("""
            SELECT COUNT(*) FROM review_comment_tracking
            WHERE persona_name = :persona_name
              AND category = :category
              AND posted_at >= :cutoff
        """)

        # Get weighted feedback counts
        # PR author reactions count 2x (is_pr_author=1 contributes weight 2)
        feedback_sql = text("""
            SELECT
                f.feedback_type,
                SUM(CASE WHEN f.is_pr_author = 1 THEN 2 ELSE 1 END) as weighted_count
            FROM review_feedback f
            JOIN review_comment_tracking t ON f.comment_id = t.comment_id
            WHERE t.persona_name = :persona_name
              AND t.category = :category
              AND t.posted_at >= :cutoff
            GROUP BY f.feedback_type
        """)

        # Get sample positive comments
        sample_positive_sql = text("""
            SELECT DISTINCT t.body
            FROM review_comment_tracking t
            JOIN review_feedback f ON f.comment_id = t.comment_id
            WHERE t.persona_name = :persona_name
              AND t.category = :category
              AND t.posted_at >= :cutoff
              AND f.feedback_type = 'positive'
            LIMIT 3
        """)

        # Get sample negative comments
        sample_negative_sql = text("""
            SELECT DISTINCT t.body
            FROM review_comment_tracking t
            JOIN review_feedback f ON f.comment_id = t.comment_id
            WHERE t.persona_name = :persona_name
              AND t.category = :category
              AND t.posted_at >= :cutoff
              AND f.feedback_type = 'negative'
            LIMIT 3
        """)

        params = {
            "persona_name": persona_name,
            "category": category,
            "cutoff": cutoff,
        }

        async with self._engine.connect() as conn:
            total_result = await conn.execute(total_sql, params)
            total_comments = total_result.scalar() or 0

            fb_result = await conn.execute(feedback_sql, params)
            feedback_counts: dict[str, int] = {}
            for row in fb_result.fetchall():
                feedback_counts[row[0]] = int(row[1])

            pos_result = await conn.execute(sample_positive_sql, params)
            sample_positive = [row[0] for row in pos_result.fetchall()]

            neg_result = await conn.execute(sample_negative_sql, params)
            sample_negative = [row[0] for row in neg_result.fetchall()]

        positive_count = feedback_counts.get("positive", 0)
        negative_count = feedback_counts.get("negative", 0)
        total_feedback = positive_count + negative_count
        approval_rate = (
            positive_count / total_feedback if total_feedback > 0 else 0.0
        )

        return FeedbackSummary(
            category=category,
            positive_count=positive_count,
            negative_count=negative_count,
            total_comments=total_comments,
            approval_rate=approval_rate,
            sample_positive=sample_positive,
            sample_negative=sample_negative,
        )

    async def get_category_approval_rates(
        self,
        persona_name: str,
    ) -> dict[str, float]:
        """Return dict mapping category name to approval rate float [0.0, 1.0].

        Args:
            persona_name: Name of the persona to query.

        Returns:
            Dictionary mapping category names to approval rates.
        """
        summaries = await self.get_persona_feedback_summary(persona_name)
        return {s.category: s.approval_rate for s in summaries}

    async def get_tracked_comments(
        self,
        max_age_days: int = 30,
    ) -> list[dict]:
        """Get all tracked comments within max age for polling.

        Args:
            max_age_days: Maximum age of comments to poll.

        Returns:
            List of dicts with comment tracking info.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
        sql = text("""
            SELECT comment_id, review_id, persona_name, repo, pr_number,
                   file_path, line_number, body, category, posted_at
            FROM review_comment_tracking
            WHERE posted_at >= :cutoff
        """)
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"cutoff": cutoff})
            columns = list(result.keys())
            return [dict(zip(columns, row)) for row in result.fetchall()]

    async def get_stored_reactions(self, comment_id: int) -> list[dict]:
        """Get all stored feedback events for a comment.

        Args:
            comment_id: GitHub comment ID.

        Returns:
            List of dicts with feedback event info.
        """
        sql = text("""
            SELECT comment_id, feedback_type, feedback_source,
                   reactor_username, is_pr_author
            FROM review_feedback
            WHERE comment_id = :comment_id
        """)
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"comment_id": comment_id})
            columns = list(result.keys())
            return [dict(zip(columns, row)) for row in result.fetchall()]

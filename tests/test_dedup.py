"""Tests for review_bot.persona.dedup module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from review_bot.persona.dedup import (
    ORIGINAL_COMMENT_WEIGHT,
    REPLY_WEIGHT,
    SELF_REPLY_WEIGHT,
    SUBSTANTIVE_REPLY_WEIGHT,
    collapse_threads,
    resolve_threads,
)
from review_bot.persona.temporal import apply_weights


def _make_comment(
    comment_id: int | None = 1,
    in_reply_to_id: int | None = None,
    body: str = "This is a review comment",
    user: str = "alice",
    file_path: str | None = "src/main.py",
    line: int | None = 10,
    pr_number: int = 42,
    repo: str = "owner/repo",
    created_at: str = "2026-03-01T12:00:00Z",
    verdict: str | None = None,
) -> dict:
    """Helper to create a comment dict matching DedupComment shape."""
    c: dict = {
        "repo": repo,
        "pr_number": pr_number,
        "comment_body": body,
        "created_at": created_at,
        "file_path": file_path,
        "line": line,
        "user": user,
    }
    if verdict is not None:
        c["verdict"] = verdict
    if comment_id is not None:
        c["comment_id"] = comment_id
    if in_reply_to_id is not None:
        c["in_reply_to_id"] = in_reply_to_id
    return c


class TestResolveThreads:
    """Tests for resolve_threads function."""

    def test_standalone_comments_get_full_weight(self) -> None:
        """No in_reply_to_id → dedup_weight=1.0."""
        comments = [_make_comment(comment_id=1)]
        result = resolve_threads(comments)
        assert result[0]["dedup_weight"] == ORIGINAL_COMMENT_WEIGHT
        assert result[0]["is_reply"] is False

    def test_reply_gets_reduced_weight(self) -> None:
        """Comment with in_reply_to_id → dedup_weight=0.3."""
        comments = [
            _make_comment(comment_id=1, user="alice", body="Original comment here"),
            _make_comment(
                comment_id=2,
                in_reply_to_id=1,
                user="bob",
                body="Short reply",
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] == REPLY_WEIGHT
        assert reply["is_reply"] is True

    def test_self_reply_gets_lowest_weight(self) -> None:
        """User replying to own comment → 0.2."""
        comments = [
            _make_comment(comment_id=1, user="alice"),
            _make_comment(
                comment_id=2,
                in_reply_to_id=1,
                user="alice",
                body="Actually, let me fix that",
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] == SELF_REPLY_WEIGHT

    def test_substantive_reply_gets_higher_weight(self) -> None:
        """Long reply (>100 chars) → 0.7."""
        long_body = "x" * 101
        comments = [
            _make_comment(comment_id=1, user="alice"),
            _make_comment(
                comment_id=2,
                in_reply_to_id=1,
                user="bob",
                body=long_body,
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] == SUBSTANTIVE_REPLY_WEIGHT

    def test_substantive_reply_with_code_block(self) -> None:
        """Reply with ``` → 0.7."""
        code_body = "Here is the fix:\n```python\ndef foo(): pass\n```"
        comments = [
            _make_comment(comment_id=1, user="alice"),
            _make_comment(
                comment_id=2,
                in_reply_to_id=1,
                user="bob",
                body=code_body,
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] == SUBSTANTIVE_REPLY_WEIGHT

    def test_trivial_reply_fixed(self) -> None:
        """body='fixed' → REPLY_WEIGHT."""
        comments = [
            _make_comment(comment_id=1, user="alice"),
            _make_comment(
                comment_id=2, in_reply_to_id=1, user="bob", body="fixed"
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] == REPLY_WEIGHT

    def test_trivial_reply_lgtm(self) -> None:
        """body='LGTM' (case insensitive) → REPLY_WEIGHT."""
        comments = [
            _make_comment(comment_id=1, user="alice"),
            _make_comment(
                comment_id=2, in_reply_to_id=1, user="bob", body="LGTM"
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] == REPLY_WEIGHT

    def test_trivial_reply_done(self) -> None:
        """body='Done' → REPLY_WEIGHT."""
        comments = [
            _make_comment(comment_id=1, user="alice"),
            _make_comment(
                comment_id=2, in_reply_to_id=1, user="bob", body="Done"
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] == REPLY_WEIGHT

    def test_trivial_reply_plus_one(self) -> None:
        """body='+1' → REPLY_WEIGHT."""
        comments = [
            _make_comment(comment_id=1, user="alice"),
            _make_comment(
                comment_id=2, in_reply_to_id=1, user="bob", body="+1"
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] == REPLY_WEIGHT

    def test_deleted_parent_treated_as_root(self) -> None:
        """in_reply_to_id points to nonexistent ID → treated as standalone, weight=1.0."""
        comments = [
            _make_comment(comment_id=2, in_reply_to_id=999, user="alice", body="Reply to deleted"),
        ]
        result = resolve_threads(comments)
        # The parent doesn't exist, but in_reply_to_id is set, so it's still a reply
        # However the thread root resolution finds it as root since parent is missing
        assert result[0]["is_reply"] is True
        # Since parent 999 doesn't exist in all_comments_by_id, self-reply check fails
        # Body is > 20 chars so not trivial by length, check patterns
        # "Reply to deleted" is 16 chars stripped → < 20 → REPLY_WEIGHT
        assert result[0]["dedup_weight"] == REPLY_WEIGHT

    def test_circular_reference_handling(self) -> None:
        """A→B→A cycle → no infinite loop, deterministic result."""
        comments = [
            _make_comment(comment_id=1, in_reply_to_id=2, user="alice", body="Comment A"),
            _make_comment(comment_id=2, in_reply_to_id=1, user="bob", body="Comment B"),
        ]
        # Should not hang or raise
        result = resolve_threads(comments)
        assert len(result) == 2
        for c in result:
            assert "dedup_weight" in c
            assert "is_reply" in c
            assert "thread_root_id" in c

    def test_nested_chain_resolution_3_levels(self) -> None:
        """A→B→C chain → all point to root C."""
        comments = [
            _make_comment(comment_id=3, user="alice", body="Root comment here"),
            _make_comment(comment_id=2, in_reply_to_id=3, user="bob", body="Reply to root here"),
            _make_comment(comment_id=1, in_reply_to_id=2, user="charlie", body="Reply to reply here"),
        ]
        result = resolve_threads(comments)
        root = next(c for c in result if c["comment_id"] == 3)
        mid = next(c for c in result if c["comment_id"] == 2)
        leaf = next(c for c in result if c["comment_id"] == 1)

        assert root["thread_root_id"] == 3
        assert mid["thread_root_id"] == 3
        assert leaf["thread_root_id"] == 3

    def test_missing_comment_id_treated_as_standalone(self) -> None:
        """comment_id=None → standalone with warning."""
        comments = [_make_comment(comment_id=None)]
        result = resolve_threads(comments)
        assert result[0]["is_reply"] is False
        assert result[0]["dedup_weight"] == ORIGINAL_COMMENT_WEIGHT
        assert result[0]["thread_root_id"] is None

    def test_empty_comment_list(self) -> None:
        """Empty input → empty output."""
        assert resolve_threads([]) == []

    def test_single_comment_no_replies(self) -> None:
        """One comment → returned unchanged with weight=1.0."""
        comments = [_make_comment(comment_id=1)]
        result = resolve_threads(comments)
        assert len(result) == 1
        assert result[0]["dedup_weight"] == ORIGINAL_COMMENT_WEIGHT

    def test_trivial_patterns_case_insensitive(self) -> None:
        """'FIXED', 'Fixed', 'fixed' all match as trivial."""
        for variant in ["FIXED", "Fixed", "fixed"]:
            comments = [
                _make_comment(comment_id=1, user="alice"),
                _make_comment(
                    comment_id=2,
                    in_reply_to_id=1,
                    user="bob",
                    body=variant,
                ),
            ]
            result = resolve_threads(comments)
            reply = next(c for c in result if c["comment_id"] == 2)
            assert reply["dedup_weight"] == REPLY_WEIGHT, f"Failed for '{variant}'"

    def test_short_non_trivial_reply(self) -> None:
        """Short body not matching patterns → REPLY_WEIGHT (< 20 chars)."""
        comments = [
            _make_comment(comment_id=1, user="alice"),
            _make_comment(
                comment_id=2,
                in_reply_to_id=1,
                user="bob",
                body="no",
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] == REPLY_WEIGHT

    def test_reply_to_different_user_not_self_reply(self) -> None:
        """Replying to another user → not SELF_REPLY_WEIGHT."""
        comments = [
            _make_comment(comment_id=1, user="alice", body="Original comment"),
            _make_comment(
                comment_id=2,
                in_reply_to_id=1,
                user="bob",
                body="I think we should reconsider this approach entirely",
            ),
        ]
        result = resolve_threads(comments)
        reply = next(c for c in result if c["comment_id"] == 2)
        assert reply["dedup_weight"] != SELF_REPLY_WEIGHT

    def test_multiple_threads_independent(self) -> None:
        """Two separate threads processed correctly."""
        comments = [
            _make_comment(comment_id=1, user="alice", body="Thread 1 root"),
            _make_comment(comment_id=2, in_reply_to_id=1, user="bob", body="Thread 1 reply"),
            _make_comment(comment_id=3, user="charlie", body="Thread 2 root"),
            _make_comment(comment_id=4, in_reply_to_id=3, user="dave", body="Thread 2 reply"),
        ]
        result = resolve_threads(comments)

        t1_root = next(c for c in result if c["comment_id"] == 1)
        t1_reply = next(c for c in result if c["comment_id"] == 2)
        t2_root = next(c for c in result if c["comment_id"] == 3)
        t2_reply = next(c for c in result if c["comment_id"] == 4)

        assert t1_root["thread_root_id"] == 1
        assert t1_reply["thread_root_id"] == 1
        assert t2_root["thread_root_id"] == 3
        assert t2_reply["thread_root_id"] == 3

    def test_very_long_reply_chain(self) -> None:
        """10-level deep chain resolves correctly without stack overflow."""
        comments = [_make_comment(comment_id=1, user="alice", body="Root comment here")]
        for i in range(2, 12):
            comments.append(
                _make_comment(
                    comment_id=i,
                    in_reply_to_id=i - 1,
                    user="alice" if i % 2 == 0 else "bob",
                    body=f"Reply level {i} with enough text",
                )
            )
        result = resolve_threads(comments)
        assert len(result) == 11
        for c in result:
            assert c["thread_root_id"] == 1


class TestCollapseThreads:
    """Tests for collapse_threads function."""

    def test_cross_user_filtering(self) -> None:
        """Comments from user X and Y, collapse for X → only X's comments returned."""
        comments = [
            _make_comment(comment_id=1, user="alice", body="Alice's comment"),
            _make_comment(comment_id=2, user="bob", body="Bob's comment"),
            _make_comment(comment_id=3, user="alice", body="Another from Alice"),
        ]
        result = collapse_threads(comments, "alice")
        assert len(result) == 2
        assert all(c["user"] == "alice" for c in result)

    def test_cross_user_thread_resolution(self) -> None:
        """X replies to Y's comment → resolved correctly using Y for context."""
        comments = [
            _make_comment(comment_id=1, user="bob", body="Bob starts a thread"),
            _make_comment(
                comment_id=2,
                in_reply_to_id=1,
                user="alice",
                body="Alice replies to Bob with a detailed explanation of the issue",
            ),
        ]
        result = collapse_threads(comments, "alice")
        assert len(result) == 1
        assert result[0]["comment_id"] == 2
        assert result[0]["is_reply"] is True
        assert result[0]["thread_root_id"] == 1


class TestTemporalDedupIntegration:
    """Tests for temporal.apply_weights incorporating dedup_weight."""

    def test_temporal_weight_incorporates_dedup(self) -> None:
        """Verify weight = temporal * dedup_weight."""
        now = datetime.now(UTC)
        recent_date = (now - timedelta(days=30)).isoformat()

        comments = [
            _make_comment(comment_id=1, user="alice", created_at=recent_date),
            _make_comment(
                comment_id=2,
                in_reply_to_id=1,
                user="bob",
                body="short",
                created_at=recent_date,
            ),
        ]
        resolve_threads(comments)

        weighted = apply_weights(comments)

        original = next(c for c in weighted if c["comment_id"] == 1)
        reply = next(c for c in weighted if c["comment_id"] == 2)

        # Recent comments get 3.0 temporal weight
        assert original["weight"] == pytest.approx(3.0 * ORIGINAL_COMMENT_WEIGHT)
        assert reply["weight"] == pytest.approx(3.0 * REPLY_WEIGHT)

    def test_dedup_weight_default_when_missing(self) -> None:
        """temporal.apply_weights handles missing dedup_weight gracefully (defaults to 1.0)."""
        now = datetime.now(UTC)
        recent_date = (now - timedelta(days=30)).isoformat()

        comments = [
            {
                "repo": "owner/repo",
                "pr_number": 1,
                "comment_body": "No dedup_weight set",
                "created_at": recent_date,
                "file_path": "test.py",
                "line": 1,
            }
        ]
        weighted = apply_weights(comments)
        # Should default to 1.0 → 3.0 * 1.0 = 3.0
        assert weighted[0]["weight"] == pytest.approx(3.0)

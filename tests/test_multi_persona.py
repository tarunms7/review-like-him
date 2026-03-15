"""Tests for multi-persona assignment, deduplication, and per-PR limits."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from review_bot.server.queue import AsyncJobQueue, ReviewJob
from review_bot.server.webhooks import (
    MAX_PERSONAS_PER_PR,
    _deduplicated_enqueue,
    configure,
    router,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_persona_store():
    """PersonaStore that reports all personas as existing by default."""
    store = MagicMock()
    store.exists.return_value = True
    return store


@pytest.fixture()
def mock_job_queue():
    """Mock AsyncJobQueue with async enqueue."""
    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value="job-id-123")
    return queue


@pytest.fixture()
def webhook_client(mock_job_queue, mock_persona_store):
    """FastAPI TestClient with webhook router and mocks configured."""
    app = FastAPI()
    app.include_router(router)
    configure(mock_job_queue, "test-secret", mock_persona_store)
    return TestClient(app)


def _make_signature(payload: bytes, secret: str = "test-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _post_webhook(client, data: dict, event: str = "pull_request"):
    payload = json.dumps(data).encode()
    return client.post(
        "/webhook",
        content=payload,
        headers={
            "x-hub-signature-256": _make_signature(payload),
            "x-github-event": event,
            "content-type": "application/json",
        },
    )


# ---------------------------------------------------------------------------
# _deduplicated_enqueue tests
# ---------------------------------------------------------------------------


class TestDeduplicatedEnqueue:
    """Test the _deduplicated_enqueue helper function."""

    async def test_deduplicates_same_persona_name(self, mock_job_queue, mock_persona_store):
        """Same persona listed twice should only enqueue once."""
        configure(mock_job_queue, "", mock_persona_store)
        result = await _deduplicated_enqueue(
            "owner", "repo", 1, ["alice", "alice", "alice"], 99
        )
        assert result == ["alice"]
        assert mock_job_queue.enqueue.call_count == 1

    async def test_respects_max_personas_per_pr(self, mock_job_queue, mock_persona_store):
        """Should stop enqueuing after MAX_PERSONAS_PER_PR."""
        configure(mock_job_queue, "", mock_persona_store)
        personas = [f"persona{i}" for i in range(MAX_PERSONAS_PER_PR + 3)]
        result = await _deduplicated_enqueue("owner", "repo", 1, personas, 99)
        assert len(result) == MAX_PERSONAS_PER_PR
        assert mock_job_queue.enqueue.call_count == MAX_PERSONAS_PER_PR

    async def test_skips_missing_persona(self, mock_job_queue, mock_persona_store):
        """Non-existent personas should not be enqueued."""
        mock_persona_store.exists.side_effect = lambda name: name != "missing"
        configure(mock_job_queue, "", mock_persona_store)
        result = await _deduplicated_enqueue(
            "owner", "repo", 1, ["alice", "missing", "bob"], 99
        )
        assert result == ["alice", "bob"]
        assert mock_job_queue.enqueue.call_count == 2

    async def test_returns_empty_for_all_duplicates(self, mock_job_queue, mock_persona_store):
        """All duplicate names should yield a single enqueue."""
        configure(mock_job_queue, "", mock_persona_store)
        result = await _deduplicated_enqueue(
            "owner", "repo", 1, ["x", "x", "x"], 99
        )
        assert result == ["x"]

    async def test_multiple_unique_personas(self, mock_job_queue, mock_persona_store):
        """Multiple unique personas should all be enqueued."""
        configure(mock_job_queue, "", mock_persona_store)
        result = await _deduplicated_enqueue(
            "owner", "repo", 1, ["alice", "bob", "charlie"], 99
        )
        assert result == ["alice", "bob", "charlie"]
        assert mock_job_queue.enqueue.call_count == 3


# ---------------------------------------------------------------------------
# _is_duplicate tests
# ---------------------------------------------------------------------------


class TestIsDuplicate:
    """Test the AsyncJobQueue._is_duplicate method."""

    async def test_returns_true_for_queued_job(self):
        """A job matching an existing queued job should be a duplicate."""
        engine = MagicMock()
        conn = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar.return_value = 1
        conn.execute = AsyncMock(return_value=result_mock)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        engine.begin.return_value = ctx

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=MagicMock(),
            persona_store=MagicMock(),
        )
        job = ReviewJob("owner", "repo", 1, "alice", 99)
        assert await queue._is_duplicate(job) is True

    async def test_returns_false_for_no_match(self):
        """No matching active jobs means not a duplicate."""
        engine = MagicMock()
        conn = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar.return_value = 0
        conn.execute = AsyncMock(return_value=result_mock)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        engine.begin.return_value = ctx

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=MagicMock(),
            persona_store=MagicMock(),
        )
        job = ReviewJob("owner", "repo", 1, "alice", 99)
        assert await queue._is_duplicate(job) is False

    async def test_returns_false_on_db_error(self):
        """DB errors should not block enqueue (returns False)."""
        engine = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(side_effect=Exception("db error"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        engine.begin.return_value = ctx

        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=MagicMock(),
            persona_store=MagicMock(),
        )
        job = ReviewJob("owner", "repo", 1, "alice", 99)
        assert await queue._is_duplicate(job) is False


# ---------------------------------------------------------------------------
# enqueue dedup integration
# ---------------------------------------------------------------------------


class TestEnqueueDedup:
    """Test that enqueue() returns None for duplicate jobs."""

    async def test_enqueue_returns_none_for_duplicate(self):
        """enqueue() should return None when _is_duplicate returns True."""
        engine = MagicMock()
        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=MagicMock(),
            persona_store=MagicMock(),
        )
        queue._is_duplicate = AsyncMock(return_value=True)
        job = ReviewJob("owner", "repo", 1, "alice", 99)
        result = await queue.enqueue(job)
        assert result is None

    async def test_enqueue_returns_id_for_new_job(self):
        """enqueue() should return job ID when not a duplicate."""
        engine = MagicMock()
        queue = AsyncJobQueue(
            db_engine=engine,
            github_auth=MagicMock(),
            persona_store=MagicMock(),
        )
        queue._is_duplicate = AsyncMock(return_value=False)
        queue._persist_job = AsyncMock()
        job = ReviewJob("owner", "repo", 1, "alice", 99)
        result = await queue.enqueue(job)
        assert result == job.id
        queue._persist_job.assert_called_once_with(job)


# ---------------------------------------------------------------------------
# Webhook handler integration tests
# ---------------------------------------------------------------------------


class TestHandleReviewRequestedMultiPersona:
    """Test _handle_review_requested with multiple requested_reviewers."""

    def test_multiple_requested_reviewers(self, webhook_client, mock_job_queue, mock_persona_store):
        """Multiple bot reviewers on the PR should all be enqueued."""
        data = {
            "action": "review_requested",
            "pull_request": {
                "number": 1,
                "requested_reviewers": [
                    {"login": "alice-bot[bot]"},
                    {"login": "bob-bot[bot]"},
                ],
            },
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
            "requested_reviewer": {"login": "alice-bot[bot]"},
        }
        resp = _post_webhook(webhook_client, data)
        assert resp.status_code == 200
        # Should enqueue both alice and bob
        assert mock_job_queue.enqueue.call_count == 2

    def test_single_reviewer_still_works(self, webhook_client, mock_job_queue, mock_persona_store):
        """Single reviewer event should still enqueue exactly once."""
        data = {
            "action": "review_requested",
            "pull_request": {"number": 1},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
            "requested_reviewer": {"login": "alice-bot[bot]"},
        }
        resp = _post_webhook(webhook_client, data)
        assert resp.status_code == 200
        mock_job_queue.enqueue.assert_called_once()


class TestHandleLabelEventFanOut:
    """Test _handle_label_event fan-out across existing labels."""

    def test_multiple_review_labels(self, webhook_client, mock_job_queue, mock_persona_store):
        """All review: labels on the PR should trigger enqueues."""
        data = {
            "action": "labeled",
            "label": {"name": "review:alice"},
            "pull_request": {
                "number": 7,
                "labels": [
                    {"name": "review:alice"},
                    {"name": "review:bob"},
                    {"name": "bug"},
                ],
            },
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
        }
        resp = _post_webhook(webhook_client, data)
        assert resp.status_code == 200
        # Should enqueue both alice and bob (bug label is ignored)
        assert mock_job_queue.enqueue.call_count == 2

    def test_non_review_label_ignored(self, webhook_client, mock_job_queue):
        """Non-review: labels should not trigger any enqueue."""
        data = {
            "action": "labeled",
            "label": {"name": "enhancement"},
            "pull_request": {
                "number": 7,
                "labels": [{"name": "enhancement"}],
            },
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
        }
        resp = _post_webhook(webhook_client, data)
        assert resp.status_code == 200
        mock_job_queue.enqueue.assert_not_called()

    def test_duplicate_labels_deduplicated(self, webhook_client, mock_job_queue, mock_persona_store):
        """Duplicate review: labels should only enqueue once."""
        data = {
            "action": "labeled",
            "label": {"name": "review:alice"},
            "pull_request": {
                "number": 7,
                "labels": [
                    {"name": "review:alice"},
                    {"name": "review:alice"},
                ],
            },
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
        }
        resp = _post_webhook(webhook_client, data)
        assert resp.status_code == 200
        assert mock_job_queue.enqueue.call_count == 1


class TestIssueCommentDedup:
    """Test _handle_issue_comment uses deduplicated enqueue."""

    def test_duplicate_personas_in_comment(self, webhook_client, mock_job_queue, mock_persona_store):
        """Duplicate persona names in /review-as should be deduplicated."""
        data = {
            "action": "created",
            "comment": {"body": "/review-as alice alice bob"},
            "issue": {"number": 5, "pull_request": {"url": "..."}},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
        }
        resp = _post_webhook(webhook_client, data, event="issue_comment")
        assert resp.status_code == 200
        # alice should only be enqueued once, plus bob
        assert mock_job_queue.enqueue.call_count == 2


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


class TestMaxPersonasPerPrSetting:
    """Test the max_personas_per_pr field on Settings."""

    def test_default_value(self):
        from review_bot.config.settings import Settings

        s = Settings(
            github_app_id=0,
            webhook_secret="",
            private_key_path="/dev/null",
        )
        assert s.max_personas_per_pr == 5

    def test_valid_value(self):
        from review_bot.config.settings import Settings

        s = Settings(
            github_app_id=0,
            webhook_secret="",
            private_key_path="/dev/null",
            max_personas_per_pr=10,
        )
        assert s.max_personas_per_pr == 10

    def test_rejects_zero(self):
        from pydantic import ValidationError

        from review_bot.config.settings import Settings

        with pytest.raises(ValidationError):
            Settings(
                github_app_id=0,
                webhook_secret="",
                private_key_path="/dev/null",
                max_personas_per_pr=0,
            )

    def test_rejects_over_20(self):
        from pydantic import ValidationError

        from review_bot.config.settings import Settings

        with pytest.raises(ValidationError):
            Settings(
                github_app_id=0,
                webhook_secret="",
                private_key_path="/dev/null",
                max_personas_per_pr=21,
            )

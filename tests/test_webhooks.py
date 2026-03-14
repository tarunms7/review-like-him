"""Tests for review_bot.server.webhooks — HMAC validation, event routing, persona extraction."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from review_bot.server.webhooks import (
    _bot_username_to_persona,
    _parse_review_command,
    _verify_signature,
    configure,
    router,
)

# ---------------------------------------------------------------------------
# HMAC Signature Validation
# ---------------------------------------------------------------------------


class TestVerifySignature:
    """Test HMAC-SHA256 signature verification."""

    def test_valid_signature_accepted(self):
        payload = b'{"action": "opened"}'
        secret = "my-secret"
        sig = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        assert _verify_signature(payload, sig, secret) is True

    def test_invalid_signature_rejected(self):
        payload = b'{"action": "opened"}'
        assert _verify_signature(payload, "sha256=bad", "my-secret") is False

    def test_missing_signature_rejected(self):
        payload = b'{"action": "opened"}'
        assert _verify_signature(payload, "", "my-secret") is False

    def test_empty_secret_skips_validation(self):
        """When no secret is configured, validation is skipped (returns True)."""
        payload = b'{"action": "opened"}'
        assert _verify_signature(payload, "", "") is True

    def test_empty_secret_ignores_any_signature(self):
        payload = b'{"action": "opened"}'
        assert _verify_signature(payload, "sha256=anything", "") is True


# ---------------------------------------------------------------------------
# Bot Username → Persona Name
# ---------------------------------------------------------------------------


class TestBotUsernameToPersona:
    """Test mapping GitHub bot usernames to persona names."""

    def test_strips_bot_suffix(self):
        assert _bot_username_to_persona("alice-bot[bot]") == "alice"

    def test_strips_bot_without_bracket(self):
        assert _bot_username_to_persona("alice-bot") == "alice"

    def test_plain_username(self):
        assert _bot_username_to_persona("alice") == "alice"

    def test_empty_username_returns_none(self):
        assert _bot_username_to_persona("") is None

    def test_only_bot_suffix_returns_none(self):
        assert _bot_username_to_persona("-bot[bot]") is None

    def test_compound_name(self):
        assert _bot_username_to_persona("deep-am-bot[bot]") == "deep-am"


# ---------------------------------------------------------------------------
# /review-as Command Parsing
# ---------------------------------------------------------------------------


class TestParseReviewCommand:
    """Test extraction of persona names from /review-as comment commands."""

    def test_single_persona(self):
        assert _parse_review_command("/review-as alice") == ["alice"]

    def test_comma_separated(self):
        assert _parse_review_command("/review-as alice,bob") == ["alice", "bob"]

    def test_space_separated(self):
        assert _parse_review_command("/review-as alice bob") == ["alice", "bob"]

    def test_mixed_separator(self):
        result = _parse_review_command("/review-as alice, bob charlie")
        assert result == ["alice", "bob", "charlie"]

    def test_no_command_returns_empty(self):
        assert _parse_review_command("Just a regular comment") == []

    def test_case_insensitive(self):
        assert _parse_review_command("/Review-As alice") == ["alice"]

    def test_multiple_commands(self):
        body = "/review-as alice\nSome text\n/review-as bob"
        result = _parse_review_command(body)
        assert "alice" in result
        assert "bob" in result


# ---------------------------------------------------------------------------
# Webhook Endpoint Integration Tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def _mock_persona_store():
    """Provide a mock PersonaStore that always says persona exists."""
    store = MagicMock()
    store.exists.return_value = True
    return store


@pytest.fixture()
def _mock_job_queue():
    """Provide a mock AsyncJobQueue."""
    queue = MagicMock()
    queue.enqueue = AsyncMock()
    return queue


@pytest.fixture()
def webhook_client(_mock_job_queue, _mock_persona_store):
    """FastAPI TestClient with webhook router and mocks configured."""
    app = FastAPI()
    app.include_router(router)
    configure(_mock_job_queue, "test-secret", _mock_persona_store)
    return TestClient(app)


def _make_signature(payload: bytes, secret: str = "test-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class TestWebhookEndpoint:
    """Test the /webhook POST endpoint with event routing."""

    def test_valid_review_requested(self, webhook_client, _mock_job_queue):
        data = {
            "action": "review_requested",
            "pull_request": {"number": 1},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
            "requested_reviewer": {"login": "alice-bot[bot]"},
        }
        payload = json.dumps(data).encode()
        resp = webhook_client.post(
            "/webhook",
            content=payload,
            headers={
                "x-hub-signature-256": _make_signature(payload),
                "x-github-event": "pull_request",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        _mock_job_queue.enqueue.assert_called_once()

    def test_invalid_signature_returns_401(self, webhook_client):
        data = {"action": "review_requested"}
        payload = json.dumps(data).encode()
        resp = webhook_client.post(
            "/webhook",
            content=payload,
            headers={
                "x-hub-signature-256": "sha256=invalid",
                "x-github-event": "pull_request",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_issue_comment_review_as(self, webhook_client, _mock_job_queue):
        data = {
            "action": "created",
            "comment": {"body": "/review-as alice"},
            "issue": {"number": 5, "pull_request": {"url": "..."}},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
        }
        payload = json.dumps(data).encode()
        resp = webhook_client.post(
            "/webhook",
            content=payload,
            headers={
                "x-hub-signature-256": _make_signature(payload),
                "x-github-event": "issue_comment",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200
        _mock_job_queue.enqueue.assert_called_once()

    def test_label_event_review_prefix(self, webhook_client, _mock_job_queue):
        data = {
            "action": "labeled",
            "label": {"name": "review:alice"},
            "pull_request": {"number": 7},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
        }
        payload = json.dumps(data).encode()
        resp = webhook_client.post(
            "/webhook",
            content=payload,
            headers={
                "x-hub-signature-256": _make_signature(payload),
                "x-github-event": "pull_request",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200
        _mock_job_queue.enqueue.assert_called_once()

    def test_irrelevant_label_ignored(self, webhook_client, _mock_job_queue):
        data = {
            "action": "labeled",
            "label": {"name": "bug"},
            "pull_request": {"number": 7},
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
        }
        payload = json.dumps(data).encode()
        resp = webhook_client.post(
            "/webhook",
            content=payload,
            headers={
                "x-hub-signature-256": _make_signature(payload),
                "x-github-event": "pull_request",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200
        _mock_job_queue.enqueue.assert_not_called()

    def test_non_pr_comment_ignored(self, webhook_client, _mock_job_queue):
        """Comments on issues (not PRs) should be ignored."""
        data = {
            "action": "created",
            "comment": {"body": "/review-as alice"},
            "issue": {"number": 5},  # No pull_request key
            "repository": {"full_name": "owner/repo"},
            "installation": {"id": 99},
        }
        payload = json.dumps(data).encode()
        resp = webhook_client.post(
            "/webhook",
            content=payload,
            headers={
                "x-hub-signature-256": _make_signature(payload),
                "x-github-event": "issue_comment",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200
        _mock_job_queue.enqueue.assert_not_called()

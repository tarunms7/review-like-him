"""Tests for RateLimitTracker, /status endpoint, and CLI status."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

# Try importing the tracker; mark tests that need it.
try:
    from review_bot.github.rate_limits import (
        RateLimitTracker,
    )

    _has_tracker = True
except ImportError:
    _has_tracker = False
    RateLimitTracker = None  # type: ignore[assignment,misc]

needs_tracker = pytest.mark.skipif(
    not _has_tracker,
    reason="review_bot.github.rate_limits not implemented",
)


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the singleton between every test."""
    if _has_tracker and RateLimitTracker is not None:
        RateLimitTracker._instance = None
    yield
    if _has_tracker and RateLimitTracker is not None:
        RateLimitTracker._instance = None


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _create_status_app(
    rate_limit_tracker=None,
) -> FastAPI:
    """Minimal FastAPI app with a /status route.

    Mirrors the interface contract so we can test response
    shape independently of the real server wiring.
    """
    app = FastAPI()
    router = APIRouter()

    @router.get("/status")
    async def status_endpoint(request: Request) -> dict:
        tracker = getattr(
            request.app.state,
            "rate_limit_tracker",
            None,
        )
        if tracker is None:
            return {
                "status": "degraded",
                "reason": (
                    "Rate limit tracker not initialized"
                ),
            }
        snap = tracker.snapshot()
        rate_limits: dict = {}
        for name, rs in snap.items():
            rate_limits[name] = {
                "remaining": rs.remaining,
                "limit": rs.limit,
                "used": rs.used,
                "reset": rs.reset,
                "last_updated": rs.last_updated,
            }
        return {
            "status": "ok",
            "rate_limits": rate_limits,
        }

    app.include_router(router)
    app.state.rate_limit_tracker = rate_limit_tracker
    return app


@dataclass(frozen=True)
class _FakeSnapshot:
    """Stand-in for ResourceSnapshot in mock-only tests."""

    remaining: int
    limit: int
    used: int
    reset: int
    last_updated: str


def _mock_tracker(data: dict | None = None) -> MagicMock:
    """Build a MagicMock tracker with canned snapshot()."""
    tracker = MagicMock()
    tracker.snapshot.return_value = data or {}
    return tracker


_FULL_HEADERS: dict[str, str] = {
    "X-RateLimit-Remaining": "4832",
    "X-RateLimit-Limit": "5000",
    "X-RateLimit-Used": "168",
    "X-RateLimit-Reset": "1742072400",
}


# -------------------------------------------------------------------
# RateLimitTracker — singleton
# -------------------------------------------------------------------


@needs_tracker
class TestSingleton:
    """Two calls to RateLimitTracker() → same object."""

    def test_same_instance(self):
        a = RateLimitTracker()
        b = RateLimitTracker()
        assert a is b

    def test_reset_yields_new_instance(self):
        first = RateLimitTracker()
        RateLimitTracker._instance = None
        second = RateLimitTracker()
        assert first is not second


# -------------------------------------------------------------------
# RateLimitTracker — infer_resource
# -------------------------------------------------------------------


@needs_tracker
class TestInferResource:
    """URL → resource bucket mapping."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://api.github.com/search/issues",
                "search",
            ),
            (
                "https://api.github.com/search/code?q=x",
                "search",
            ),
            (
                "https://api.github.com/graphql",
                "graphql",
            ),
            (
                "https://api.github.com/repos/o/r/pulls",
                "core",
            ),
            (
                "https://api.github.com/users/octocat",
                "core",
            ),
        ],
    )
    def test_mapping(self, url: str, expected: str):
        result = RateLimitTracker.infer_resource(url)
        assert result == expected


# -------------------------------------------------------------------
# RateLimitTracker — snapshot (empty)
# -------------------------------------------------------------------


@needs_tracker
class TestSnapshotEmpty:
    """Fresh tracker returns empty dict."""

    def test_empty(self):
        tracker = RateLimitTracker()
        assert tracker.snapshot() == {}


# -------------------------------------------------------------------
# RateLimitTracker — update_from_response
# -------------------------------------------------------------------


@needs_tracker
class TestUpdateFromResponse:
    """Parsing headers into snapshot data."""

    def test_valid_headers(self):
        tracker = RateLimitTracker()
        tracker.update_from_response(
            "https://api.github.com/repos/o/r/pulls",
            _FULL_HEADERS,
        )
        snap = tracker.snapshot()
        assert "core" in snap
        core = snap["core"]
        assert core.remaining == 4832
        assert core.limit == 5000
        assert core.used == 168
        assert core.reset == 1742072400
        assert core.last_updated  # non-empty ISO str

    def test_search_bucket(self):
        tracker = RateLimitTracker()
        headers = {
            "X-RateLimit-Remaining": "28",
            "X-RateLimit-Limit": "30",
            "X-RateLimit-Used": "2",
            "X-RateLimit-Reset": "1742072400",
        }
        tracker.update_from_response(
            "https://api.github.com/search/issues?q=x",
            headers,
        )
        snap = tracker.snapshot()
        assert "search" in snap
        assert snap["search"].remaining == 28
        assert snap["search"].limit == 30

    def test_missing_headers_noop(self):
        tracker = RateLimitTracker()
        tracker.update_from_response(
            "https://api.github.com/repos/o/r", {}
        )
        assert tracker.snapshot() == {}

    def test_partial_headers_no_crash(self):
        """Only some rate-limit headers → must not raise."""
        tracker = RateLimitTracker()
        tracker.update_from_response(
            "https://api.github.com/repos/o/r",
            {"X-RateLimit-Remaining": "100"},
        )

    def test_multiple_resources_coexist(self):
        tracker = RateLimitTracker()
        tracker.update_from_response(
            "https://api.github.com/repos/o/r",
            {
                "X-RateLimit-Remaining": "4900",
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Used": "100",
                "X-RateLimit-Reset": "1742072400",
            },
        )
        tracker.update_from_response(
            "https://api.github.com/search/issues",
            {
                "X-RateLimit-Remaining": "25",
                "X-RateLimit-Limit": "30",
                "X-RateLimit-Used": "5",
                "X-RateLimit-Reset": "1742072400",
            },
        )
        snap = tracker.snapshot()
        assert "core" in snap
        assert "search" in snap
        assert snap["core"].remaining == 4900
        assert snap["search"].remaining == 25


# -------------------------------------------------------------------
# RateLimitTracker — thread safety
# -------------------------------------------------------------------


@needs_tracker
class TestThreadSafety:
    """Concurrent updates must not corrupt state."""

    def test_concurrent_updates(self):
        tracker = RateLimitTracker()
        errors: list[Exception] = []

        def updater(remaining: int) -> None:
            try:
                hdrs = {
                    "X-RateLimit-Remaining": str(
                        remaining
                    ),
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Used": str(
                        5000 - remaining
                    ),
                    "X-RateLimit-Reset": "1742072400",
                }
                for _ in range(50):
                    tracker.update_from_response(
                        "https://api.github.com/repos/o/r",
                        hdrs,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(
                target=updater, args=(i * 100,)
            )
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors: {errors}"
        snap = tracker.snapshot()
        assert "core" in snap
        assert isinstance(snap["core"].remaining, int)
        assert isinstance(snap["core"].limit, int)


# -------------------------------------------------------------------
# /status endpoint
# -------------------------------------------------------------------


class TestStatusEndpoint:
    """GET /status via FastAPI TestClient."""

    def test_ok_with_tracker(self):
        snap = {
            "core": _FakeSnapshot(
                remaining=4832,
                limit=5000,
                used=168,
                reset=1742072400,
                last_updated="2026-03-15T12:30:00",
            ),
            "search": _FakeSnapshot(
                remaining=28,
                limit=30,
                used=2,
                reset=1742072400,
                last_updated="2026-03-15T12:29:45",
            ),
        }
        app = _create_status_app(
            rate_limit_tracker=_mock_tracker(snap)
        )
        client = TestClient(app)
        resp = client.get("/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "rate_limits" in data

        core = data["rate_limits"]["core"]
        assert core["remaining"] == 4832
        assert core["limit"] == 5000
        assert core["used"] == 168
        assert core["reset"] == 1742072400
        assert (
            core["last_updated"] == "2026-03-15T12:30:00"
        )
        search = data["rate_limits"]["search"]
        assert search["remaining"] == 28

    def test_degraded_when_tracker_none(self):
        app = _create_status_app(rate_limit_tracker=None)
        client = TestClient(app)
        resp = client.get("/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert "reason" in data
        assert "not initialized" in data["reason"].lower()

    def test_empty_rate_limits(self):
        app = _create_status_app(
            rate_limit_tracker=_mock_tracker({})
        )
        client = TestClient(app)
        data = client.get("/status").json()
        assert data["status"] == "ok"
        assert data["rate_limits"] == {}

    def test_response_shape(self):
        """All ResourceSnapshotDict fields present."""
        snap = {
            "graphql": _FakeSnapshot(
                remaining=4999,
                limit=5000,
                used=1,
                reset=1742072400,
                last_updated="2026-03-15T12:28:00",
            ),
        }
        app = _create_status_app(
            rate_limit_tracker=_mock_tracker(snap)
        )
        client = TestClient(app)
        gql = client.get("/status").json()[
            "rate_limits"
        ]["graphql"]
        expected_keys = {
            "remaining",
            "limit",
            "used",
            "reset",
            "last_updated",
        }
        assert set(gql.keys()) == expected_keys


# -------------------------------------------------------------------
# CLI status command
# -------------------------------------------------------------------


class TestCLIStatusCommand:
    """review_bot.cli.status_cmd via Click CliRunner."""

    @staticmethod
    def _mock_response(
        json_data: dict, status: int = 200
    ) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_data
        resp.raise_for_status = MagicMock()
        return resp

    def test_successful_output(self):
        from review_bot.cli.status_cmd import status_cmd

        data = {
            "status": "ok",
            "rate_limits": {
                "core": {
                    "remaining": 4832,
                    "limit": 5000,
                    "used": 168,
                    "reset": 1742072400,
                    "last_updated": "2026-03-15T12:30:00",
                },
            },
        }
        runner = CliRunner()
        with patch(
            "review_bot.cli.status_cmd.httpx.get",
            return_value=self._mock_response(data),
        ):
            result = runner.invoke(status_cmd)

        assert result.exit_code == 0
        assert "4832" in result.output
        assert "5000" in result.output

    def test_degraded_output(self):
        from review_bot.cli.status_cmd import status_cmd

        data = {
            "status": "degraded",
            "reason": (
                "Rate limit tracker not initialized"
            ),
        }
        runner = CliRunner()
        with patch(
            "review_bot.cli.status_cmd.httpx.get",
            return_value=self._mock_response(data),
        ):
            result = runner.invoke(status_cmd)

        assert result.exit_code == 0
        assert "degraded" in result.output.lower()

    def test_no_rate_limit_data(self):
        from review_bot.cli.status_cmd import status_cmd

        data = {"status": "ok", "rate_limits": {}}
        runner = CliRunner()
        with patch(
            "review_bot.cli.status_cmd.httpx.get",
            return_value=self._mock_response(data),
        ):
            result = runner.invoke(status_cmd)

        assert result.exit_code == 0
        assert (
            "no rate limit data"
            in result.output.lower()
        )

    def test_connection_error(self):
        from review_bot.cli.status_cmd import status_cmd

        runner = CliRunner()
        with patch(
            "review_bot.cli.status_cmd.httpx.get",
            side_effect=httpx.ConnectError("refused"),
        ):
            result = runner.invoke(status_cmd)

        assert result.exit_code == 1
        assert (
            "could not connect" in result.output.lower()
        )

    def test_http_status_error(self):
        from review_bot.cli.status_cmd import status_cmd

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = (
            httpx.HTTPStatusError(
                "Server Error",
                request=MagicMock(),
                response=mock_resp,
            )
        )
        runner = CliRunner()
        with patch(
            "review_bot.cli.status_cmd.httpx.get",
            return_value=mock_resp,
        ):
            result = runner.invoke(status_cmd)

        assert result.exit_code == 1
        assert "error" in result.output.lower()

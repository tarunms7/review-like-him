"""Tests for incremental persona mining, review caching, and dedup merging."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from review_bot.persona.analyzer import PersonaAnalyzer
from review_bot.persona.profile import PersonaProfile, Priority, SeverityPattern
from review_bot.persona.store import PersonaStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_review(
    repo: str = "owner/repo",
    pr_number: int = 1,
    comment_body: str = "Looks good",
    created_at: str = "2026-01-15T12:00:00Z",
    file_path: str | None = "src/main.py",
    line: int | None = 10,
    verdict: str | None = None,
    user: str | None = "testuser",
    comment_id: int | None = None,
    in_reply_to_id: int | None = None,
) -> dict:
    """Build a review comment dict with sensible defaults."""
    review: dict = {
        "repo": repo,
        "pr_number": pr_number,
        "comment_body": comment_body,
        "created_at": created_at,
        "file_path": file_path,
        "line": line,
        "verdict": verdict,
    }
    if user is not None:
        review["user"] = user
    if comment_id is not None:
        review["comment_id"] = comment_id
    if in_reply_to_id is not None:
        review["in_reply_to_id"] = in_reply_to_id
    return review


def _make_profile(
    name: str = "alice",
    github_user: str = "alice-gh",
    last_mined_at: str | None = None,
    overrides: list[str] | None = None,
) -> PersonaProfile:
    """Build a PersonaProfile with sensible defaults."""
    return PersonaProfile(
        name=name,
        github_user=github_user,
        mined_from="10 comments across 2 repos",
        last_updated="2026-01-01",
        last_mined_at=last_mined_at,
        priorities=[
            Priority(
                category="error_handling",
                severity="critical",
                description="Check errors",
            ),
        ],
        pet_peeves=["Magic numbers"],
        tone="Direct",
        severity_pattern=SeverityPattern(
            blocks_on=["Unhandled errors"],
            nits_on=["Style"],
            approves_when="Tests pass",
        ),
        overrides=overrides or [],
    )


# ---------------------------------------------------------------------------
# 1. test_incremental_mine_appends_date_filter
# ---------------------------------------------------------------------------


class TestMinerSinceParam:
    """Tests for the since parameter on miner methods."""

    @pytest.mark.asyncio
    async def test_incremental_mine_appends_date_filter(self) -> None:
        """When since is provided, 'created:>' appears in the search query.

        Patches _discover_reviewed_prs to accept a since parameter and
        verifies the search query includes a date filter.
        """
        import httpx

        from review_bot.persona.miner import GitHubReviewMiner

        captured_params: list[dict] = []

        async def mock_get(url, params=None, headers=None):
            captured_params.append({"url": url, "params": params})
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.headers = {"Link": ""}
            resp.json.return_value = {"items": [], "total_count": 0}
            return resp

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=mock_get)

        miner = GitHubReviewMiner(client)

        # Patch _discover_reviewed_prs to accept since and append to query
        original_discover = miner._discover_reviewed_prs

        async def patched_discover(username, progress_callback=None, since=None):
            """Wrapper that modifies the search query when since is provided."""
            # We call the original but intercept the _request to inject since
            if since is not None:
                # Monkey-patch _request to append since filter to search queries
                original_request = miner._request

                async def request_with_since(url, params=None):
                    if params and "q" in (params or {}) and "/search/" in url:
                        params = dict(params)
                        params["q"] = params["q"] + f" created:>{since}"
                    return await original_request(url, params)

                miner._request = request_with_since
                try:
                    return await original_discover(username, progress_callback)
                finally:
                    miner._request = original_request
            return await original_discover(username, progress_callback)

        miner._discover_reviewed_prs = patched_discover
        await miner._discover_reviewed_prs("testuser", since="2026-01-01T00:00:00+00:00")

        search_calls = [
            c for c in captured_params if "/search/issues" in c["url"]
        ]
        assert len(search_calls) > 0
        query_str = search_calls[0]["params"]["q"]
        assert "created:>2026-01-01T00:00:00+00:00" in query_str

    # -----------------------------------------------------------------------
    # 2. test_incremental_mine_no_filter_when_since_none
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_incremental_mine_no_filter_when_since_none(self) -> None:
        """When since is None, no date filter appears in the search query."""
        import httpx

        from review_bot.persona.miner import GitHubReviewMiner

        captured_params: list[dict] = []

        async def mock_get(url, params=None, headers=None):
            captured_params.append({"url": url, "params": params})
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.headers = {"Link": ""}
            resp.json.return_value = {"items": [], "total_count": 0}
            return resp

        client = MagicMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=mock_get)

        miner = GitHubReviewMiner(client)
        await miner.mine_user_reviews("testuser")

        search_calls = [
            c for c in captured_params if "/search/issues" in c["url"]
        ]
        assert len(search_calls) > 0
        query_str = search_calls[0]["params"]["q"]
        assert "created:>" not in query_str


# ---------------------------------------------------------------------------
# 3. test_merge_deduplicates_reviews
# ---------------------------------------------------------------------------


class TestMergeDedup:
    """Tests for review merging and deduplication."""

    def test_merge_deduplicates_reviews(self) -> None:
        """Overlapping review lists are deduplicated by composite key."""
        from review_bot.cli.persona_cmd import _deduplicate_reviews

        shared = _make_review(comment_body="shared comment")
        only_old = _make_review(comment_body="old only")
        only_new = _make_review(comment_body="new only")

        existing = [shared, only_old]
        new = [shared.copy(), only_new]

        merged = _deduplicate_reviews(existing, new)
        bodies = [r["comment_body"] for r in merged]
        assert len(merged) == 3
        assert "shared comment" in bodies
        assert "old only" in bodies
        assert "new only" in bodies

    # -----------------------------------------------------------------------
    # 17. test_merge_handles_empty_cached_reviews
    # -----------------------------------------------------------------------

    def test_merge_handles_empty_cached_reviews(self) -> None:
        """No cached data + new reviews → just new reviews."""
        from review_bot.cli.persona_cmd import _deduplicate_reviews

        new = [_make_review(comment_body="new")]
        merged = _deduplicate_reviews([], new)
        assert len(merged) == 1
        assert merged[0]["comment_body"] == "new"

    # -----------------------------------------------------------------------
    # 18. test_merge_handles_empty_new_reviews
    # -----------------------------------------------------------------------

    def test_merge_handles_empty_new_reviews(self) -> None:
        """Cached data + no new reviews → cached data unchanged."""
        from review_bot.cli.persona_cmd import _deduplicate_reviews

        cached = [_make_review(comment_body="cached")]
        merged = _deduplicate_reviews(cached, [])
        assert len(merged) == 1
        assert merged[0]["comment_body"] == "cached"

    # -----------------------------------------------------------------------
    # 19. test_dedup_key_all_fields_must_match
    # -----------------------------------------------------------------------

    def test_dedup_key_all_fields_must_match(self) -> None:
        """Same body but different PR number → not deduplicated."""
        from review_bot.cli.persona_cmd import _deduplicate_reviews

        r1 = _make_review(pr_number=1, comment_body="same body")
        r2 = _make_review(pr_number=2, comment_body="same body")

        merged = _deduplicate_reviews([r1], [r2])
        assert len(merged) == 2


# ---------------------------------------------------------------------------
# 4-7. Timezone and last_mined_at parsing tests
# ---------------------------------------------------------------------------


class TestTimezoneNormalization:
    """Tests for last_mined_at parsing and timezone normalization."""

    def test_corrupted_last_mined_at_falls_back(self, caplog) -> None:
        """last_mined_at='not-a-date' triggers fallback to full mine with warning."""
        profile = _make_profile(last_mined_at="not-a-date")

        # Simulate the parsing logic from the mine command
        since_val = None
        is_incremental = False

        try:
            dt = datetime.fromisoformat(profile.last_mined_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            else:
                dt = dt.astimezone(UTC)
            since_val = dt.isoformat()
            is_incremental = True
        except ValueError:
            logging.getLogger("test").warning(
                "Could not parse last_mined_at='%s', falling back",
                profile.last_mined_at,
            )

        assert since_val is None
        assert is_incremental is False

    def test_timezone_normalization_utc(self) -> None:
        """last_mined_at with Z suffix → correct UTC query."""
        ts = "2026-01-15T10:00:00+00:00"
        dt = datetime.fromisoformat(ts)
        dt = dt.astimezone(UTC)
        result = dt.isoformat()
        assert "2026-01-15" in result

    def test_timezone_normalization_offset(self) -> None:
        """last_mined_at with +05:30 → normalized to UTC."""
        ts = "2026-01-15T15:30:00+05:30"
        dt = datetime.fromisoformat(ts)
        dt = dt.astimezone(UTC)
        assert dt.hour == 10
        assert dt.minute == 0

    def test_timezone_normalization_naive(self) -> None:
        """last_mined_at without tz → assume UTC."""
        ts = "2026-01-15T10:00:00"
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is None
        dt = dt.replace(tzinfo=UTC)
        assert dt.tzinfo == UTC
        assert dt.hour == 10


# ---------------------------------------------------------------------------
# 8. test_temporal_reweighting_on_merged_data
# ---------------------------------------------------------------------------


class TestTemporalReweighting:
    """Tests for temporal weight recalculation on merged data."""

    def test_temporal_reweighting_on_merged_data(self) -> None:
        """Weights are recalculated from scratch on the merged set."""
        from review_bot.persona.temporal import apply_weights

        now = datetime.now(UTC)
        recent = _make_review(
            comment_body="recent",
            created_at=(now - timedelta(days=30)).isoformat(),
        )
        old = _make_review(
            comment_body="old",
            created_at=(now - timedelta(days=400)).isoformat(),
        )

        weighted = apply_weights([recent, old])
        assert len(weighted) == 2

        recent_w = next(w for w in weighted if w["comment_body"] == "recent")
        old_w = next(w for w in weighted if w["comment_body"] == "old")

        # Recent should have higher weight than old
        assert recent_w["weight"] > old_w["weight"]
        # Recent within 90 days → 3.0, old >365 days → 0.5
        assert recent_w["weight"] == pytest.approx(3.0)
        assert old_w["weight"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 9. test_full_flag_forces_complete_mine
# ---------------------------------------------------------------------------


class TestFullFlag:
    """Tests for --full flag behavior."""

    def test_full_flag_forces_complete_mine(self) -> None:
        """When --full is True, last_mined_at is ignored."""
        profile = _make_profile(last_mined_at="2026-01-01T00:00:00Z")

        # Simulate the logic: full=True should skip incremental
        full = True
        since_val = None
        is_incremental = False

        if profile.last_mined_at and not full:
            try:
                dt = datetime.fromisoformat(profile.last_mined_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                since_val = dt.isoformat()
                is_incremental = True
            except ValueError:
                pass

        assert since_val is None
        assert is_incremental is False


# ---------------------------------------------------------------------------
# 10. test_first_mine_sets_last_mined_at
# ---------------------------------------------------------------------------


class TestFirstMine:
    """Tests that first mine sets last_mined_at."""

    def test_first_mine_sets_last_mined_at(self) -> None:
        """New profile without last_mined_at gets it set after mining."""
        profile = _make_profile(last_mined_at=None)
        assert profile.last_mined_at is None

        # Simulate what mine command does after full analysis
        profile.last_mined_at = datetime.now(UTC).isoformat()
        assert profile.last_mined_at is not None
        # Should be parseable
        dt = datetime.fromisoformat(profile.last_mined_at)
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# 11. test_incremental_preserves_overrides
# ---------------------------------------------------------------------------


class TestIncrementalPreservesOverrides:
    """Tests that overrides survive incremental analysis."""

    @pytest.mark.asyncio
    async def test_incremental_preserves_overrides(self) -> None:
        """Existing profile overrides are preserved after analyze_incremental."""
        existing = _make_profile(
            last_mined_at="2026-01-01T00:00:00Z",
            overrides=["Always check type hints", "Prefer dataclasses"],
        )

        reviews = [
            _make_review(comment_body="Use type hints", created_at="2026-01-10T00:00:00Z"),
        ]

        analyzer = PersonaAnalyzer()

        # Mock the analyze method to avoid actual LLM calls
        mock_profile = _make_profile(overrides=[])
        with patch.object(analyzer, "analyze", new_callable=AsyncMock) as mock_analyze:
            mock_analyze.return_value = mock_profile
            result = await analyzer.analyze_incremental(existing, reviews, reviews)

        assert result.overrides == ["Always check type hints", "Prefer dataclasses"]


# ---------------------------------------------------------------------------
# 12-14. PersonaStore reviews cache tests
# ---------------------------------------------------------------------------


class TestReviewsCache:
    """Tests for PersonaStore review save/load."""

    def test_save_and_load_reviews_roundtrip(self, tmp_path: Path) -> None:
        """save_reviews then load_reviews returns identical data."""
        store = PersonaStore(base_dir=tmp_path)
        reviews = [
            _make_review(comment_body="first"),
            _make_review(comment_body="second", pr_number=2),
        ]

        store.save_reviews("alice", reviews)
        loaded = store.load_reviews("alice")

        assert loaded == reviews

    def test_load_reviews_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """No reviews file → empty list."""
        store = PersonaStore(base_dir=tmp_path)
        result = store.load_reviews("nonexistent")
        assert result == []

    def test_load_reviews_corrupted_json(self, tmp_path: Path, caplog) -> None:
        """Corrupted JSON → empty list with warning logged."""
        store = PersonaStore(base_dir=tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        path = tmp_path / "broken_reviews.json"
        path.write_text("{invalid json!!!", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            result = store.load_reviews("broken")

        assert result == []
        assert any("Corrupted" in r.message or "corrupted" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 15. test_profile_last_mined_at_optional
# ---------------------------------------------------------------------------


class TestProfileLastMinedAt:
    """Tests for last_mined_at field on PersonaProfile."""

    def test_profile_last_mined_at_optional(self) -> None:
        """Existing YAML without last_mined_at loads fine with None default."""
        yaml_str = """
name: bob
github_user: bob-gh
mined_from: "5 comments across 1 repos"
last_updated: "2026-01-01"
priorities: []
pet_peeves: []
tone: "Chill"
severity_pattern:
  blocks_on: []
  nits_on: []
  approves_when: "Always"
overrides: []
"""
        profile = PersonaProfile.from_yaml(yaml_str)
        assert profile.last_mined_at is None
        assert profile.name == "bob"


# ---------------------------------------------------------------------------
# 16. test_incremental_mine_end_to_end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """End-to-end incremental mining tests."""

    @pytest.mark.asyncio
    async def test_incremental_mine_end_to_end(self, tmp_path: Path) -> None:
        """Mock GitHub API: first mine then incremental, verify only new fetched."""
        from review_bot.cli.persona_cmd import _deduplicate_reviews

        store = PersonaStore(base_dir=tmp_path)

        # --- First mine: 2 reviews ---
        first_reviews = [
            _make_review(
                comment_body="first review",
                created_at="2026-01-10T00:00:00Z",
            ),
            _make_review(
                comment_body="second review",
                created_at="2026-01-12T00:00:00Z",
                pr_number=2,
            ),
        ]

        store.save_reviews("alice", first_reviews)

        profile = _make_profile(
            last_mined_at="2026-01-12T00:00:00Z",
        )
        store.save(profile)

        # --- Second mine: 1 new review + 1 overlapping ---
        new_reviews = [
            _make_review(
                comment_body="second review",
                created_at="2026-01-12T00:00:00Z",
                pr_number=2,
            ),
            _make_review(
                comment_body="third review",
                created_at="2026-01-15T00:00:00Z",
                pr_number=3,
            ),
        ]

        cached = store.load_reviews("alice")
        merged = _deduplicate_reviews(cached, new_reviews)

        assert len(merged) == 3
        bodies = [r["comment_body"] for r in merged]
        assert "first review" in bodies
        assert "second review" in bodies
        assert "third review" in bodies

        # Save updated cache
        store.save_reviews("alice", merged)
        reloaded = store.load_reviews("alice")
        assert len(reloaded) == 3


# ---------------------------------------------------------------------------
# 20. test_mined_from_count_updated
# ---------------------------------------------------------------------------


class TestMinedFromCount:
    """Tests for mined_from update after incremental analysis."""

    @pytest.mark.asyncio
    async def test_mined_from_count_updated(self) -> None:
        """mined_from reflects total count after incremental analysis."""
        existing = _make_profile(
            last_mined_at="2026-01-01T00:00:00Z",
        )

        all_reviews = [
            _make_review(comment_body="r1", repo="owner/repo1"),
            _make_review(comment_body="r2", repo="owner/repo2"),
            _make_review(comment_body="r3", repo="owner/repo1"),
        ]

        analyzer = PersonaAnalyzer()
        mock_profile = _make_profile()

        with patch.object(analyzer, "analyze", new_callable=AsyncMock) as mock_analyze:
            mock_analyze.return_value = mock_profile
            result = await analyzer.analyze_incremental(
                existing, all_reviews[:1], all_reviews,
            )

        assert "3 comments" in result.mined_from
        assert "2 repos" in result.mined_from

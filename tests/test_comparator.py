"""Tests for persona comparison: comparator, formatters, and CLI command."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from review_bot.cli.compare_cmd import compare_cmd
from review_bot.review.comparator import (
    ComparisonEntry,
    ComparisonResult,
    PersonaComparator,
)
from review_bot.review.comparison_formatter import (
    format_comparison_api,
    format_comparison_cli,
)
from review_bot.review.formatter import (
    CategorySection,
    Finding,
    InlineComment,
    ReviewResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_review_result(persona_name: str, verdict: str = "approve") -> ReviewResult:
    """Build a ReviewResult for testing."""
    return ReviewResult(
        verdict=verdict,
        summary_sections=[
            CategorySection(
                emoji="🔒",
                title="Security",
                findings=[Finding(text=f"Finding from {persona_name}")],
            ),
        ],
        inline_comments=[
            InlineComment(
                file="src/app.py",
                line=10,
                body=f"Comment from {persona_name}",
            ),
        ],
        persona_name=persona_name,
        pr_url="https://github.com/owner/repo/pull/1",
    )


def _make_canned_json(persona_name: str, verdict: str = "approve") -> str:
    """Return canned JSON that ReviewFormatter can parse."""
    return json.dumps({
        "verdict": verdict,
        "summary_sections": [
            {
                "emoji": "🔒",
                "title": "Security",
                "findings": [{"text": f"Finding from {persona_name}"}],
            },
        ],
        "inline_comments": [
            {
                "file": "src/app.py",
                "line": 10,
                "body": f"Comment from {persona_name}",
            },
        ],
    })


@pytest.fixture()
def _mock_comparator_deps(mock_github_client, persona_store, sample_persona):
    """Set up a PersonaComparator with mocked dependencies."""
    # Save two personas
    from review_bot.persona.profile import PersonaProfile

    persona_store.save(sample_persona)
    bob = PersonaProfile(name="bob", github_user="bob-gh")
    persona_store.save(bob)

    return mock_github_client, persona_store


# ---------------------------------------------------------------------------
# PersonaComparator tests
# ---------------------------------------------------------------------------


class TestPersonaComparator:
    """Tests for PersonaComparator.compare()."""

    async def test_two_persona_comparison(self, _mock_comparator_deps):
        """Two personas should each produce a ComparisonEntry."""
        github_client, store = _mock_comparator_deps

        with patch("review_bot.review.comparator.ClaudeReviewer") as mock_cls, \
             patch("review_bot.review.comparator.RepoScanner") as mock_scanner_cls:
            mock_reviewer = MagicMock()
            # Return different canned JSON per call
            mock_reviewer.review = AsyncMock(
                side_effect=[
                    _make_canned_json("alice", "approve"),
                    _make_canned_json("bob", "request_changes"),
                ]
            )
            mock_cls.return_value = mock_reviewer

            mock_scanner = MagicMock()
            mock_scanner.scan = AsyncMock(return_value=MagicMock(
                languages=["python"], frameworks=[], repo_config={},
            ))
            mock_scanner_cls.return_value = mock_scanner

            comparator = PersonaComparator(github_client, store)
            result = await comparator.compare("owner", "repo", 1, ["alice", "bob"])

        assert isinstance(result, ComparisonResult)
        assert result.pr_url == "https://github.com/owner/repo/pull/1"
        assert len(result.entries) == 2
        assert result.total_duration_ms >= 0

        names = {e.persona_name for e in result.entries}
        assert names == {"alice", "bob"}

        for entry in result.entries:
            assert entry.error is None
            assert entry.duration_ms >= 0
            assert entry.result.persona_name == entry.persona_name

    async def test_persona_not_found(self, _mock_comparator_deps):
        """A missing persona should produce an entry with an error."""
        github_client, store = _mock_comparator_deps

        with patch("review_bot.review.comparator.ClaudeReviewer") as mock_cls, \
             patch("review_bot.review.comparator.RepoScanner") as mock_scanner_cls:
            mock_reviewer = MagicMock()
            mock_reviewer.review = AsyncMock(return_value=_make_canned_json("alice"))
            mock_cls.return_value = mock_reviewer

            mock_scanner = MagicMock()
            mock_scanner.scan = AsyncMock(return_value=MagicMock(
                languages=["python"], frameworks=[], repo_config={},
            ))
            mock_scanner_cls.return_value = mock_scanner

            comparator = PersonaComparator(github_client, store)
            result = await comparator.compare(
                "owner", "repo", 1, ["alice", "nonexistent"],
            )

        assert len(result.entries) == 2

        error_entry = next(e for e in result.entries if e.persona_name == "nonexistent")
        assert error_entry.error is not None
        assert "not found" in error_entry.error.lower()

        ok_entry = next(e for e in result.entries if e.persona_name == "alice")
        assert ok_entry.error is None

    async def test_timeout_handling(self, _mock_comparator_deps):
        """A persona that exceeds the timeout should produce an error entry."""
        github_client, store = _mock_comparator_deps

        async def _slow_review(_prompt: str) -> str:
            await asyncio.sleep(10)
            return "{}"

        with patch("review_bot.review.comparator.ClaudeReviewer") as mock_cls, \
             patch("review_bot.review.comparator.RepoScanner") as mock_scanner_cls:
            mock_reviewer = MagicMock()
            mock_reviewer.review = _slow_review
            mock_cls.return_value = mock_reviewer

            mock_scanner = MagicMock()
            mock_scanner.scan = AsyncMock(return_value=MagicMock(
                languages=["python"], frameworks=[], repo_config={},
            ))
            mock_scanner_cls.return_value = mock_scanner

            comparator = PersonaComparator(github_client, store)
            result = await comparator.compare(
                "owner", "repo", 1, ["alice"],
                timeout_per_persona=0.01,
            )

        assert len(result.entries) == 1
        entry = result.entries[0]
        assert entry.error is not None
        assert "timed out" in entry.error.lower()


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------


class TestComparisonFormatters:
    """Tests for CLI and API comparison formatters."""

    def _make_result(self, include_error: bool = False) -> ComparisonResult:
        entries = [
            ComparisonEntry(
                persona_name="alice",
                result=_make_review_result("alice", "approve"),
                duration_ms=500,
            ),
            ComparisonEntry(
                persona_name="bob",
                result=_make_review_result("bob", "request_changes"),
                duration_ms=800,
            ),
        ]
        if include_error:
            entries.append(
                ComparisonEntry(
                    persona_name="charlie",
                    result=ReviewResult(
                        verdict="comment",
                        summary_sections=[],
                        inline_comments=[],
                        persona_name="charlie",
                        pr_url="https://github.com/owner/repo/pull/1",
                    ),
                    duration_ms=100,
                    error="Persona 'charlie' not found",
                )
            )
        return ComparisonResult(
            pr_url="https://github.com/owner/repo/pull/1",
            entries=entries,
            total_duration_ms=1300,
        )

    def test_format_cli_contains_persona_names(self):
        result = self._make_result()
        output = format_comparison_cli(result)
        assert "alice" in output
        assert "bob" in output

    def test_format_cli_contains_verdicts(self):
        result = self._make_result()
        output = format_comparison_cli(result)
        assert "approve" in output
        assert "request_changes" in output

    def test_format_cli_error_entry(self):
        result = self._make_result(include_error=True)
        output = format_comparison_cli(result)
        assert "charlie" in output
        assert "ERROR" in output
        assert "not found" in output

    def test_format_api_structure(self):
        result = self._make_result()
        data = format_comparison_api(result)
        assert data["pr_url"] == "https://github.com/owner/repo/pull/1"
        assert data["total_duration_ms"] == 1300
        assert len(data["entries"]) == 2
        for entry in data["entries"]:
            assert "persona_name" in entry
            assert "duration_ms" in entry
            assert "error" in entry
            assert "result" in entry
            assert "verdict" in entry["result"]
            assert "summary_sections" in entry["result"]
            assert "inline_comments" in entry["result"]

    def test_format_api_error_entry(self):
        result = self._make_result(include_error=True)
        data = format_comparison_api(result)
        error_entry = next(e for e in data["entries"] if e["persona_name"] == "charlie")
        assert error_entry["error"] is not None
        assert "not found" in error_entry["error"]


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCompareCLI:
    """Tests for the compare Click command."""

    def test_invalid_url(self):
        runner = CliRunner()
        result = runner.invoke(compare_cmd, ["not-a-url", "-p", "alice,bob"])
        assert result.exit_code != 0
        assert "Invalid" in result.output

    def test_single_persona_error(self):
        runner = CliRunner()
        result = runner.invoke(
            compare_cmd,
            ["https://github.com/owner/repo/pull/1", "-p", "alice"],
        )
        assert result.exit_code != 0
        assert "At least 2" in result.output

    def test_json_output_flag(self):
        """The --json-output flag should produce JSON output."""
        runner = CliRunner()

        comparison_result = ComparisonResult(
            pr_url="https://github.com/owner/repo/pull/1",
            entries=[
                ComparisonEntry(
                    persona_name="alice",
                    result=_make_review_result("alice"),
                    duration_ms=100,
                ),
                ComparisonEntry(
                    persona_name="bob",
                    result=_make_review_result("bob"),
                    duration_ms=200,
                ),
            ],
            total_duration_ms=300,
        )

        with patch("review_bot.cli.compare_cmd._run_async", return_value=comparison_result):
            result = runner.invoke(
                compare_cmd,
                [
                    "https://github.com/owner/repo/pull/1",
                    "-p", "alice,bob",
                    "--json-output",
                ],
            )

        assert result.exit_code == 0
        # The output includes a header line before the JSON; extract the JSON portion
        json_start = result.output.index("{")
        data = json.loads(result.output[json_start:])
        assert data["pr_url"] == "https://github.com/owner/repo/pull/1"
        assert len(data["entries"]) == 2

    def test_text_output(self):
        """Default output should be text format with persona names."""
        runner = CliRunner()

        comparison_result = ComparisonResult(
            pr_url="https://github.com/owner/repo/pull/1",
            entries=[
                ComparisonEntry(
                    persona_name="alice",
                    result=_make_review_result("alice"),
                    duration_ms=100,
                ),
                ComparisonEntry(
                    persona_name="bob",
                    result=_make_review_result("bob"),
                    duration_ms=200,
                ),
            ],
            total_duration_ms=300,
        )

        with patch("review_bot.cli.compare_cmd._run_async", return_value=comparison_result):
            result = runner.invoke(
                compare_cmd,
                ["https://github.com/owner/repo/pull/1", "-p", "alice,bob"],
            )

        assert result.exit_code == 0
        assert "alice" in result.output
        assert "bob" in result.output

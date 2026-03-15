"""Tests for severity-based filtering module."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from review_bot.review.formatter import (
    CategorySection,
    Finding,
    InlineComment,
    ReviewResult,
)
from review_bot.review.severity import (
    _infer_comment_category,
    _is_critical_security,
    compute_finding_severity,
    filter_result_by_severity,
)


def _make_result(
    verdict: str = "request_changes",
    sections: list[CategorySection] | None = None,
    inline_comments: list[InlineComment] | None = None,
) -> ReviewResult:
    """Helper to create a ReviewResult for tests."""
    return ReviewResult(
        verdict=verdict,
        summary_sections=sections or [],
        inline_comments=inline_comments or [],
        persona_name="TestBot",
        pr_url="https://github.com/org/repo/pull/1",
    )


class TestComputeFindingSeverity:
    """Tests for compute_finding_severity function."""

    def test_compute_severity_security_high_confidence(self) -> None:
        """Security + high confidence = 4+1 = 5, clamped to 4."""
        result = compute_finding_severity("Security", confidence="high")
        assert result == 4  # clamped from 5

    def test_compute_severity_style_low_confidence(self) -> None:
        """Style + low confidence = 1-1 = 0."""
        result = compute_finding_severity("Style", confidence="low")
        assert result == 0

    def test_compute_severity_bugs_medium_confidence(self) -> None:
        """Bugs + medium confidence = 3+0 = 3."""
        result = compute_finding_severity("Bugs", confidence="medium")
        assert result == 3

    def test_compute_severity_with_file_type_boost(self) -> None:
        """File type severity_boost is added to the total."""
        with patch(
            "review_bot.review.severity._get_file_type_severity_boost",
            return_value=1,
        ):
            result = compute_finding_severity("Testing", confidence="medium", file_type="migration")
            assert result == 2  # 1 + 0 + 1

    def test_compute_severity_clamped_to_zero(self) -> None:
        """Result is clamped to minimum 0."""
        with patch(
            "review_bot.review.severity._get_file_type_severity_boost",
            return_value=-1,
        ):
            result = compute_finding_severity("Style", confidence="low", file_type="documentation")
            assert result == 0  # 1 - 1 - 1 = -1, clamped to 0

    def test_compute_severity_unknown_category_defaults_to_one(self) -> None:
        """Unknown categories get base severity of 1."""
        result = compute_finding_severity("UnknownCategory", confidence="medium")
        assert result == 1

    def test_compute_severity_default_params(self) -> None:
        """Default confidence=medium, file_type=unknown."""
        result = compute_finding_severity("Bugs")
        assert result == 3


class TestFilterResultBySeverity:
    """Tests for filter_result_by_severity function."""

    def test_filter_keeps_all_at_zero(self) -> None:
        """min_severity=0 keeps all findings unchanged."""
        section = CategorySection(
            emoji="💅",
            title="Style",
            findings=[Finding(text="Use consistent naming", confidence="low")],
        )
        result = _make_result(sections=[section])
        filtered = filter_result_by_severity(result, min_severity=0)
        assert len(filtered.summary_sections) == 1
        assert len(filtered.summary_sections[0].findings) == 1

    def test_filter_removes_low_severity(self) -> None:
        """Low-severity Style findings are removed at min_severity=2."""
        style_section = CategorySection(
            emoji="💅",
            title="Style",
            findings=[Finding(text="Naming inconsistency", confidence="low")],
        )
        security_section = CategorySection(
            emoji="🔒",
            title="Security",
            findings=[Finding(text="Missing auth check", confidence="high")],
        )
        result = _make_result(sections=[style_section, security_section])
        filtered = filter_result_by_severity(result, min_severity=2)

        # Style(1) + low(-1) = 0 < 2, removed
        # Security(4) + high(1) = 5, clamped to 4 >= 2, kept
        assert len(filtered.summary_sections) == 1
        assert filtered.summary_sections[0].title == "Security"

    def test_filter_all_removed_creates_lgtm(self) -> None:
        """When all findings are filtered, result is LGTM approve."""
        section = CategorySection(
            emoji="💅",
            title="Style",
            findings=[Finding(text="Minor style issue", confidence="low")],
        )
        result = _make_result(sections=[section])
        filtered = filter_result_by_severity(result, min_severity=4)

        assert filtered.verdict == "approve"
        assert len(filtered.summary_sections) == 1
        assert filtered.summary_sections[0].title == "LGTM"
        assert "below severity threshold" in filtered.summary_sections[0].findings[0].text
        assert filtered.inline_comments == []

    def test_filter_recomputes_verdict_downgrade(self) -> None:
        """Verdict downgrades from request_changes to comment after filtering."""
        # Keep one finding but remove the severity-driving ones
        style_section = CategorySection(
            emoji="💅",
            title="Style",
            findings=[Finding(text="Minor issue", confidence="low")],
        )
        arch_section = CategorySection(
            emoji="🏗️",
            title="Architecture",
            findings=[Finding(text="Design concern", confidence="medium")],
        )
        result = _make_result(
            verdict="request_changes",
            sections=[style_section, arch_section],
        )
        filtered = filter_result_by_severity(result, min_severity=2)

        # Style(1) + low(-1) = 0 < 2, removed
        # Architecture(2) + medium(0) = 2 >= 2, kept
        assert len(filtered.summary_sections) == 1
        assert filtered.summary_sections[0].title == "Architecture"
        # Verdict stays request_changes since we still have findings
        # and request_changes(2) > comment(1), so it downgrades to comment
        assert filtered.verdict == "comment"

    def test_filter_recomputes_verdict_never_upgrades(self) -> None:
        """Verdict never upgrades from comment to request_changes."""
        section = CategorySection(
            emoji="🔒",
            title="Security",
            findings=[Finding(text="Auth issue", confidence="high")],
        )
        result = _make_result(verdict="comment", sections=[section])
        filtered = filter_result_by_severity(result, min_severity=1)

        # Security findings kept, but verdict stays 'comment' (no upgrade)
        assert filtered.verdict == "comment"

    def test_security_override_bypasses_filter(self) -> None:
        """Critical security keywords bypass severity filtering."""
        section = CategorySection(
            emoji="💅",
            title="Style",
            findings=[
                Finding(
                    text="Possible SQL injection in user input handling",
                    confidence="low",
                ),
            ],
        )
        result = _make_result(sections=[section])
        filtered = filter_result_by_severity(result, min_severity=4)

        # Style + low = 0, normally filtered at 4, but SQL injection keyword overrides
        assert len(filtered.summary_sections) == 1
        assert "SQL injection" in filtered.summary_sections[0].findings[0].text

    def test_security_override_inline_comment(self) -> None:
        """Critical security keywords bypass filter on inline comments too."""
        comment = InlineComment(
            file="app.py",
            line=10,
            body="This is vulnerable to path traversal attacks",
            confidence="low",
        )
        result = _make_result(inline_comments=[comment])
        filtered = filter_result_by_severity(result, min_severity=4)

        assert len(filtered.inline_comments) == 1

    def test_filter_inline_comments_by_severity(self) -> None:
        """Inline comments are filtered based on inferred category."""
        comments = [
            InlineComment(
                file="app.py",
                line=5,
                body="This naming is inconsistent with the rest of the codebase",
                confidence="low",
            ),
            InlineComment(
                file="auth.py",
                line=20,
                body="Missing authentication check on this endpoint",
                confidence="high",
            ),
        ]
        result = _make_result(inline_comments=comments)
        filtered = filter_result_by_severity(result, min_severity=3)

        # Style + low = 0, filtered
        # Security + high = 5 clamped to 4, kept
        assert len(filtered.inline_comments) == 1
        assert "authentication" in filtered.inline_comments[0].body


class TestInferCommentCategory:
    """Tests for _infer_comment_category function."""

    def test_infer_category_keywords(self) -> None:
        """Category keywords are correctly identified."""
        assert _infer_comment_category("This is a security vulnerability") == "Security"
        assert _infer_comment_category("Potential null pointer bug here") == "Bugs"
        assert _infer_comment_category("Architecture coupling issue") == "Architecture"
        assert _infer_comment_category("Performance is slow here") == "Performance"
        assert _infer_comment_category("Missing test coverage") == "Testing"
        assert _infer_comment_category("Use better variable names") == "Style"

    def test_inline_comment_category_inference(self) -> None:
        """Inline comment body is used for category inference."""
        assert _infer_comment_category("SQL injection risk") == "Security"
        assert _infer_comment_category("This could crash with a NullPointerException") == "Bugs"
        assert _infer_comment_category("Consider using a cache for this result") == "Performance"
        assert _infer_comment_category("Refactor to reduce coupling") == "Architecture"

    def test_infer_defaults_to_style(self) -> None:
        """Unknown text defaults to Style category."""
        assert _infer_comment_category("This looks fine") == "Style"


class TestIsCriticalSecurity:
    """Tests for _is_critical_security function."""

    def test_detects_sql_injection(self) -> None:
        assert _is_critical_security("Possible SQL injection in query builder") is True

    def test_detects_rce(self) -> None:
        assert _is_critical_security("This allows RCE via user input") is True

    def test_detects_path_traversal(self) -> None:
        assert _is_critical_security("Path traversal in file handler") is True

    def test_no_match_returns_false(self) -> None:
        assert _is_critical_security("Use better variable names") is False

    def test_case_insensitive(self) -> None:
        assert _is_critical_security("SQL INJECTION vulnerability") is True


class TestMinSeverityEnum:
    """Tests for MinSeverity IntEnum in settings."""

    def test_min_severity_values(self) -> None:
        from review_bot.config.settings import MinSeverity

        assert MinSeverity.ALL == 0
        assert MinSeverity.LOW == 1
        assert MinSeverity.MEDIUM == 2
        assert MinSeverity.HIGH == 3
        assert MinSeverity.CRITICAL == 4

    def test_min_severity_is_int(self) -> None:
        from review_bot.config.settings import MinSeverity

        assert isinstance(MinSeverity.MEDIUM, int)
        assert MinSeverity.HIGH + 1 == 4


class TestSettingsMinSeverity:
    """Tests for min_severity field on Settings."""

    def test_default_min_severity(self) -> None:
        from review_bot.config.settings import Settings

        settings = Settings()
        assert settings.min_severity == 0

    def test_valid_min_severity(self) -> None:
        from review_bot.config.settings import Settings

        settings = Settings(min_severity=3)
        assert settings.min_severity == 3

    def test_invalid_min_severity_too_high(self) -> None:
        from review_bot.config.settings import Settings

        with pytest.raises(Exception):
            Settings(min_severity=5)

    def test_invalid_min_severity_negative(self) -> None:
        from review_bot.config.settings import Settings

        with pytest.raises(Exception):
            Settings(min_severity=-1)

    def test_default_feedback_poll_interval(self) -> None:
        from review_bot.config.settings import Settings

        settings = Settings()
        assert settings.feedback_poll_interval_hours == 6

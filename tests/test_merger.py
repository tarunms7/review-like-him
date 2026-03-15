"""Tests for the ChunkResultMerger."""

from __future__ import annotations

from review_bot.review.formatter import (
    CategorySection,
    Finding,
    InlineComment,
    ReviewResult,
)
from review_bot.review.merger import ChunkResultMerger, MergeConflict


def _make_result(
    verdict: str = "comment",
    sections: list[CategorySection] | None = None,
    comments: list[InlineComment] | None = None,
    persona_name: str = "alice",
    pr_url: str = "https://github.com/org/repo/pull/1",
) -> ReviewResult:
    """Create a ReviewResult for testing."""
    return ReviewResult(
        verdict=verdict,
        summary_sections=sections or [],
        inline_comments=comments or [],
        persona_name=persona_name,
        pr_url=pr_url,
    )


def _make_section(
    title: str,
    findings: list[str],
    emoji: str = "🐛",
) -> CategorySection:
    """Create a CategorySection with string findings."""
    return CategorySection(
        emoji=emoji,
        title=title,
        findings=[Finding(text=f) for f in findings],
    )


class TestMergeDeduplicatesSimilarFindings:
    """Findings with Jaccard similarity > 0.8 should be deduplicated."""

    def test_merge_deduplicates_similar_findings(self) -> None:
        result_a = _make_result(
            sections=[
                _make_section("Bugs", [
                    "Missing null check on user input parameter",
                ]),
            ],
        )
        result_b = _make_result(
            sections=[
                _make_section("Bugs", [
                    "Missing null check on user input parameter",  # exact dup
                ]),
            ],
        )

        merger = ChunkResultMerger()
        merged = merger.merge([result_a, result_b], ["chunk-1", "chunk-2"])

        bug_section = next(s for s in merged.summary_sections if s.title == "Bugs")
        assert len(bug_section.findings) == 1

    def test_near_duplicate_findings_removed(self) -> None:
        # Near-identical findings (>80% Jaccard) should be deduplicated
        result_a = _make_result(
            sections=[
                _make_section("Bugs", [
                    "the function is missing error handling for null input values",
                ]),
            ],
        )
        result_b = _make_result(
            sections=[
                _make_section("Bugs", [
                    "the function is missing error handling for null input",
                ]),
            ],
        )

        merger = ChunkResultMerger()
        merged = merger.merge([result_a, result_b], ["chunk-1", "chunk-2"])

        bug_section = next(s for s in merged.summary_sections if s.title == "Bugs")
        assert len(bug_section.findings) == 1


class TestMergeKeepsConflictingFindings:
    """Distinct findings should be preserved."""

    def test_merge_keeps_conflicting_findings(self) -> None:
        result_a = _make_result(
            sections=[
                _make_section("Bugs", [
                    "Missing null check on user input parameter",
                ]),
            ],
        )
        result_b = _make_result(
            sections=[
                _make_section("Bugs", [
                    "SQL injection vulnerability in query builder",
                ]),
            ],
        )

        merger = ChunkResultMerger()
        merged = merger.merge([result_a, result_b], ["chunk-1", "chunk-2"])

        bug_section = next(s for s in merged.summary_sections if s.title == "Bugs")
        assert len(bug_section.findings) == 2

    def test_merge_preserves_different_categories(self) -> None:
        result_a = _make_result(
            sections=[_make_section("Bugs", ["Bug finding"])],
        )
        result_b = _make_result(
            sections=[_make_section("Security", ["Security finding"], emoji="🔒")],
        )

        merger = ChunkResultMerger()
        merged = merger.merge([result_a, result_b], ["chunk-1", "chunk-2"])

        assert len(merged.summary_sections) == 2
        titles = {s.title for s in merged.summary_sections}
        assert titles == {"Bugs", "Security"}


class TestMergeVerdictMostSevereWins:
    """The most severe verdict should win."""

    def test_merge_verdict_most_severe_wins(self) -> None:
        results = [
            _make_result(verdict="approve"),
            _make_result(verdict="comment"),
            _make_result(verdict="request_changes"),
        ]

        merger = ChunkResultMerger()
        merged = merger.merge(results, ["c1", "c2", "c3"])

        assert merged.verdict == "request_changes"

    def test_merge_verdict_comment_beats_approve(self) -> None:
        results = [
            _make_result(verdict="approve"),
            _make_result(verdict="comment"),
        ]

        merger = ChunkResultMerger()
        merged = merger.merge(results, ["c1", "c2"])

        assert merged.verdict == "comment"

    def test_merge_verdict_all_approve(self) -> None:
        results = [
            _make_result(verdict="approve"),
            _make_result(verdict="approve"),
        ]

        merger = ChunkResultMerger()
        merged = merger.merge(results, ["c1", "c2"])

        assert merged.verdict == "approve"

    def test_merge_empty_results(self) -> None:
        merger = ChunkResultMerger()
        merged = merger.merge([], [])

        assert merged.verdict == "comment"
        assert merged.summary_sections == []
        assert merged.inline_comments == []


class TestMergeInlineCommentsDeduplicated:
    """Inline comments at the same (file, line) should be deduplicated."""

    def test_merge_inline_comments_deduplicated_by_file_line(self) -> None:
        comment_a = InlineComment(
            file="src/app.py",
            line=42,
            body="Missing error handling here",
        )
        comment_b = InlineComment(
            file="src/app.py",
            line=42,
            body="Add try/except block",
        )
        comment_c = InlineComment(
            file="src/app.py",
            line=100,
            body="Different line comment",
        )

        result_a = _make_result(comments=[comment_a])
        result_b = _make_result(comments=[comment_b, comment_c])

        merger = ChunkResultMerger()
        merged = merger.merge([result_a, result_b], ["c1", "c2"])

        assert len(merged.inline_comments) == 2
        # First comment at line 42 wins
        line_42 = next(c for c in merged.inline_comments if c.line == 42)
        assert line_42.body == "Missing error handling here"

    def test_different_files_same_line_kept(self) -> None:
        comment_a = InlineComment(file="src/a.py", line=10, body="Comment A")
        comment_b = InlineComment(file="src/b.py", line=10, body="Comment B")

        result = _make_result(comments=[comment_a, comment_b])
        merger = ChunkResultMerger()
        merged = merger.merge([result], ["c1"])

        assert len(merged.inline_comments) == 2


class TestMergeRanksSectionsBySeverity:
    """Sections should be ordered by severity."""

    def test_merge_ranks_sections_by_severity(self) -> None:
        result = _make_result(
            sections=[
                _make_section("Style", ["Style issue"], emoji="💅"),
                _make_section("Bugs", ["Bug issue"], emoji="🐛"),
                _make_section("Security", ["Security issue"], emoji="🔒"),
                _make_section("Performance", ["Perf issue"], emoji="⚡"),
                _make_section("Testing", ["Test issue"], emoji="🧪"),
                _make_section("Architecture", ["Arch issue"], emoji="🏗️"),
            ],
        )

        merger = ChunkResultMerger()
        merged = merger.merge([result], ["c1"])

        titles = [s.title for s in merged.summary_sections]
        expected = [
            "Security",
            "Bugs",
            "Architecture",
            "Performance",
            "Testing",
            "Style",
        ]
        assert titles == expected

    def test_unknown_categories_at_end(self) -> None:
        result = _make_result(
            sections=[
                _make_section("Custom", ["Custom finding"]),
                _make_section("Security", ["Security issue"], emoji="🔒"),
            ],
        )

        merger = ChunkResultMerger()
        merged = merger.merge([result], ["c1"])

        titles = [s.title for s in merged.summary_sections]
        assert titles == ["Security", "Custom"]


class TestMergePreservesMetadata:
    """Merged result should carry persona and PR metadata."""

    def test_merge_preserves_persona_and_pr_url(self) -> None:
        results = [
            _make_result(persona_name="alice", pr_url="https://github.com/org/repo/pull/1"),
            _make_result(persona_name="alice", pr_url="https://github.com/org/repo/pull/1"),
        ]

        merger = ChunkResultMerger()
        merged = merger.merge(results, ["c1", "c2"])

        assert merged.persona_name == "alice"
        assert merged.pr_url == "https://github.com/org/repo/pull/1"

    def test_single_result_preserves_content(self) -> None:
        result = _make_result(
            verdict="approve",
            sections=[_make_section("Bugs", ["A bug"])],
            persona_name="bob",
        )

        merger = ChunkResultMerger()
        merged = merger.merge([result], ["c1"])

        assert merged.verdict == "approve"
        assert merged.persona_name == "bob"
        assert len(merged.summary_sections) == 1
        assert merged.summary_sections[0].title == "Bugs"


class TestMergeConflictDataclass:
    """MergeConflict dataclass should be constructable."""

    def test_merge_conflict_creation(self) -> None:
        conflict = MergeConflict(
            chunk_ids=["chunk-1", "chunk-2"],
            finding_a="Finding A",
            finding_b="Finding B",
            resolution="Kept Finding A (appeared first)",
        )
        assert conflict.chunk_ids == ["chunk-1", "chunk-2"]
        assert conflict.finding_a == "Finding A"


class TestJaccardSimilarity:
    """Test the Jaccard similarity helper."""

    def test_identical_sets(self) -> None:
        merger = ChunkResultMerger()
        assert merger._jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self) -> None:
        merger = ChunkResultMerger()
        assert merger._jaccard_similarity({"a", "b"}, {"c", "d"}) == 0.0

    def test_empty_sets(self) -> None:
        merger = ChunkResultMerger()
        assert merger._jaccard_similarity(set(), set()) == 1.0

    def test_partial_overlap(self) -> None:
        merger = ChunkResultMerger()
        # {a, b, c} ∩ {b, c, d} = {b, c}, union = {a, b, c, d}
        sim = merger._jaccard_similarity({"a", "b", "c"}, {"b", "c", "d"})
        assert abs(sim - 0.5) < 0.01


class TestFindingConfidencePreserved:
    """Confidence metadata should survive merge."""

    def test_confidence_preserved_through_merge(self) -> None:
        finding = Finding(
            text="Critical bug",
            confidence="high",
            confidence_reason="Clear null pointer",
        )
        section = CategorySection(emoji="🐛", title="Bugs", findings=[finding])
        result = _make_result(sections=[section])

        merger = ChunkResultMerger()
        merged = merger.merge([result], ["c1"])

        merged_finding = merged.summary_sections[0].findings[0]
        assert merged_finding.confidence == "high"
        assert merged_finding.confidence_reason == "Clear null pointer"

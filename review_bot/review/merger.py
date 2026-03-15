"""Merges multiple chunk review results into a single ReviewResult."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from review_bot.review.formatter import (
    CategorySection,
    Finding,
    InlineComment,
    ReviewResult,
)

logger = logging.getLogger("review-bot")

# Verdict severity ordering (higher index = more severe)
_VERDICT_SEVERITY: dict[str, int] = {
    "approve": 0,
    "comment": 1,
    "request_changes": 2,
}

# Section ordering by severity (higher priority first)
_SECTION_SEVERITY_ORDER: list[str] = [
    "Security",
    "Bugs",
    "Architecture",
    "Performance",
    "Testing",
    "Style",
]


@dataclass
class MergeConflict:
    """Record of a deduplication decision between overlapping findings.

    Args:
        chunk_ids: The chunk IDs that produced the overlapping findings.
        finding_a: The first finding text.
        finding_b: The second finding text (duplicate that was removed).
        resolution: Description of how the conflict was resolved.
    """

    chunk_ids: list[str]
    finding_a: str
    finding_b: str
    resolution: str


class ChunkResultMerger:
    """Merges multiple chunk ReviewResults into a single ReviewResult.

    Deduplicates findings using Jaccard similarity, deduplicates inline
    comments by (file, line), resolves verdicts by severity, and ranks
    sections by importance.
    """

    def merge(
        self,
        chunk_results: list[ReviewResult],
        chunk_labels: list[str],
    ) -> ReviewResult:
        """Merge multiple ReviewResults into one.

        Args:
            chunk_results: List of ReviewResult objects from chunk reviews.
            chunk_labels: Corresponding labels for each chunk.

        Returns:
            A single merged ReviewResult.
        """
        if not chunk_results:
            return ReviewResult(
                verdict="comment",
                summary_sections=[],
                inline_comments=[],
                persona_name="",
                pr_url="",
            )

        if len(chunk_results) == 1:
            # Still rank sections by severity for consistent output
            result = chunk_results[0]
            ranked = self._rank_by_severity(result.summary_sections)
            return ReviewResult(
                verdict=result.verdict,
                summary_sections=ranked,
                inline_comments=result.inline_comments,
                persona_name=result.persona_name,
                pr_url=result.pr_url,
            )

        # Use persona_name and pr_url from the first result
        persona_name = chunk_results[0].persona_name
        pr_url = chunk_results[0].pr_url

        # Collect all sections and comments
        all_sections: list[CategorySection] = []
        all_comments: list[InlineComment] = []
        all_verdicts: list[str] = []

        for result in chunk_results:
            all_sections.extend(result.summary_sections)
            all_comments.extend(result.inline_comments)
            all_verdicts.append(result.verdict)

        merged_sections = self._merge_sections(all_sections)
        merged_sections = self._rank_by_severity(merged_sections)
        merged_comments = self._merge_inline_comments(all_comments)
        verdict = self._resolve_verdict(all_verdicts)

        return ReviewResult(
            verdict=verdict,
            summary_sections=merged_sections,
            inline_comments=merged_comments,
            persona_name=persona_name,
            pr_url=pr_url,
        )

    def _merge_sections(
        self, all_sections: list[CategorySection]
    ) -> list[CategorySection]:
        """Merge sections by grouping on category title and deduplicating findings.

        Args:
            all_sections: All CategorySection objects from all chunks.

        Returns:
            Merged list of CategorySection objects with deduplicated findings.
        """
        grouped: dict[str, list[CategorySection]] = {}
        for section in all_sections:
            grouped.setdefault(section.title, []).append(section)

        merged: list[CategorySection] = []
        for title, sections in grouped.items():
            # Use the emoji from the first section for this title
            emoji = sections[0].emoji

            # Collect all findings across chunks for this category
            all_findings: list[Finding] = []
            for s in sections:
                all_findings.extend(s.findings)

            deduped = self._deduplicate_findings(all_findings)
            if deduped:
                merged.append(
                    CategorySection(
                        emoji=emoji,
                        title=title,
                        findings=deduped,
                    )
                )

        return merged

    def _deduplicate_findings(
        self, findings: list[Finding]
    ) -> list[Finding]:
        """Remove duplicate findings using Jaccard similarity > 0.8.

        Args:
            findings: List of Finding objects to deduplicate.

        Returns:
            Deduplicated list of Finding objects.
        """
        if not findings:
            return []

        kept: list[Finding] = []
        for finding in findings:
            is_dup = False
            tokens_a = self._tokenize(finding.text)
            for existing in kept:
                tokens_b = self._tokenize(existing.text)
                similarity = self._jaccard_similarity(tokens_a, tokens_b)
                if similarity > 0.8:
                    is_dup = True
                    logger.debug(
                        "Deduplicated finding (Jaccard=%.2f): '%s' ~ '%s'",
                        similarity,
                        finding.text[:50],
                        existing.text[:50],
                    )
                    break
            if not is_dup:
                kept.append(finding)

        return kept

    def _merge_inline_comments(
        self, all_comments: list[InlineComment]
    ) -> list[InlineComment]:
        """Deduplicate inline comments by (file, line).

        When multiple comments target the same file and line, keep the
        first one encountered (from earlier chunks).

        Args:
            all_comments: All InlineComment objects from all chunks.

        Returns:
            Deduplicated list of InlineComment objects.
        """
        seen: set[tuple[str, int]] = set()
        deduped: list[InlineComment] = []

        for comment in all_comments:
            key = (comment.file, comment.line)
            if key not in seen:
                seen.add(key)
                deduped.append(comment)

        return deduped

    def _resolve_verdict(self, verdicts: list[str]) -> str:
        """Resolve multiple verdicts — most severe wins.

        Args:
            verdicts: List of verdict strings from chunk reviews.

        Returns:
            The most severe verdict string.
        """
        if not verdicts:
            return "comment"

        return max(verdicts, key=lambda v: _VERDICT_SEVERITY.get(v, 1))

    def _rank_by_severity(
        self, sections: list[CategorySection]
    ) -> list[CategorySection]:
        """Rank sections by severity order.

        Order: Security > Bugs > Architecture > Performance > Testing > Style.
        Unknown categories are placed at the end.

        Args:
            sections: List of CategorySection objects.

        Returns:
            Sorted list of CategorySection objects.
        """
        def sort_key(section: CategorySection) -> int:
            try:
                return _SECTION_SEVERITY_ORDER.index(section.title)
            except ValueError:
                return len(_SECTION_SEVERITY_ORDER)

        return sorted(sections, key=sort_key)

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Tokenize text into a set of lowercase words.

        Args:
            text: The text to tokenize.

        Returns:
            Set of lowercase word tokens.
        """
        return set(text.lower().split())

    @staticmethod
    def _jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
        """Calculate Jaccard similarity between two sets.

        Args:
            set_a: First set of tokens.
            set_b: Second set of tokens.

        Returns:
            Jaccard similarity coefficient (0.0 to 1.0).
        """
        if not set_a and not set_b:
            return 1.0
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union)

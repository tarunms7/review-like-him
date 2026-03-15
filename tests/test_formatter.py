"""Tests for review_bot.review.formatter — JSON parsing, fence stripping, fallback."""

from __future__ import annotations

import json

from review_bot.review.formatter import (
    CATEGORY_EMOJIS,
    CONFIDENCE_PREFIXES,
    CategorySection,
    Finding,
    InlineComment,
    ReviewFormatter,
    ReviewResult,
)


class TestFindingModel:
    """Test Finding Pydantic model defaults and fields."""

    def test_defaults(self):
        finding = Finding(text="Something wrong")
        assert finding.text == "Something wrong"
        assert finding.confidence == "medium"
        assert finding.confidence_reason == ""

    def test_custom_confidence(self):
        finding = Finding(text="Bug", confidence="high", confidence_reason="obvious")
        assert finding.confidence == "high"
        assert finding.confidence_reason == "obvious"

    def test_all_fields(self):
        finding = Finding(text="Issue", confidence="low", confidence_reason="unsure")
        assert finding.text == "Issue"
        assert finding.confidence == "low"
        assert finding.confidence_reason == "unsure"


class TestConfidenceNormalization:
    """Test _normalize_confidence static method."""

    def setup_method(self):
        self.normalize = ReviewFormatter._normalize_confidence

    def test_valid_values_pass_through(self):
        assert self.normalize("high") == "high"
        assert self.normalize("medium") == "medium"
        assert self.normalize("low") == "low"

    def test_case_insensitive(self):
        assert self.normalize("HIGH") == "high"
        assert self.normalize("Medium") == "medium"
        assert self.normalize("LOW") == "low"

    def test_llm_variations_very_high(self):
        assert self.normalize("very high") == "high"
        assert self.normalize("very_high") == "high"

    def test_llm_variations_critical(self):
        assert self.normalize("critical") == "high"
        assert self.normalize("certain") == "high"
        assert self.normalize("confident") == "high"

    def test_llm_variations_moderate(self):
        assert self.normalize("moderate") == "medium"
        assert self.normalize("normal") == "medium"

    def test_llm_variations_uncertain(self):
        assert self.normalize("uncertain") == "low"
        assert self.normalize("unsure") == "low"
        assert self.normalize("guess") == "low"
        assert self.normalize("very low") == "low"
        assert self.normalize("very_low") == "low"
        assert self.normalize("none") == "low"

    def test_garbage_defaults_to_medium(self):
        assert self.normalize("asdfghjkl") == "medium"
        assert self.normalize("42") == "medium"
        assert self.normalize("maybe") == "medium"

    def test_empty_string_defaults_to_medium(self):
        assert self.normalize("") == "medium"

    def test_whitespace_stripped(self):
        assert self.normalize("  high  ") == "high"
        assert self.normalize(" low ") == "low"

    def test_non_string_defaults_to_medium(self):
        # Edge case: LLM might return a number
        assert self.normalize(42) == "medium"  # type: ignore[arg-type]
        assert self.normalize(None) == "medium"  # type: ignore[arg-type]


class TestBackwardCompatibility:
    """Test backward compat: old format (strings) and new format (dicts with confidence)."""

    def setup_method(self):
        self.formatter = ReviewFormatter()

    def test_old_format_strings_become_findings(self):
        raw = json.dumps({
            "verdict": "approve",
            "summary_sections": [
                {"emoji": "🐛", "title": "Bugs", "findings": ["Bug one", "Bug two"]},
            ],
            "inline_comments": [],
        })
        result = self.formatter.format(raw, "alice", "http://x")
        findings = result.summary_sections[0].findings
        assert len(findings) == 2
        assert isinstance(findings[0], Finding)
        assert findings[0].text == "Bug one"
        assert findings[0].confidence == "medium"
        assert findings[1].text == "Bug two"

    def test_new_format_dicts_with_confidence(self):
        raw = json.dumps({
            "verdict": "approve",
            "summary_sections": [
                {
                    "emoji": "🔒",
                    "title": "Security",
                    "findings": [
                        {
                            "text": "SQL injection risk",
                            "confidence": "high",
                            "confidence_reason": "Direct string concatenation in query",
                        },
                    ],
                },
            ],
            "inline_comments": [],
        })
        result = self.formatter.format(raw, "alice", "http://x")
        finding = result.summary_sections[0].findings[0]
        assert finding.text == "SQL injection risk"
        assert finding.confidence == "high"
        assert finding.confidence_reason == "Direct string concatenation in query"

    def test_mixed_format_findings(self):
        raw = json.dumps({
            "verdict": "comment",
            "summary_sections": [
                {
                    "emoji": "🐛",
                    "title": "Bugs",
                    "findings": [
                        "Plain string finding",
                        {"text": "Dict finding", "confidence": "low"},
                    ],
                },
            ],
            "inline_comments": [],
        })
        result = self.formatter.format(raw, "alice", "http://x")
        findings = result.summary_sections[0].findings
        assert findings[0].text == "Plain string finding"
        assert findings[0].confidence == "medium"
        assert findings[1].text == "Dict finding"
        assert findings[1].confidence == "low"

    def test_inline_comments_old_format(self):
        raw = json.dumps({
            "verdict": "comment",
            "summary_sections": [],
            "inline_comments": [
                {"file": "a.py", "line": 1, "body": "Fix this"},
            ],
        })
        result = self.formatter.format(raw, "a", "http://x")
        comment = result.inline_comments[0]
        assert comment.confidence == "medium"
        assert comment.confidence_reason == ""

    def test_inline_comments_new_format(self):
        raw = json.dumps({
            "verdict": "comment",
            "summary_sections": [],
            "inline_comments": [
                {
                    "file": "a.py",
                    "line": 5,
                    "body": "Dangerous pattern",
                    "confidence": "high",
                    "confidence_reason": "Known vulnerability",
                },
            ],
        })
        result = self.formatter.format(raw, "a", "http://x")
        comment = result.inline_comments[0]
        assert comment.confidence == "high"
        assert comment.confidence_reason == "Known vulnerability"

    def test_llm_variation_normalized_in_finding(self):
        raw = json.dumps({
            "verdict": "comment",
            "summary_sections": [
                {
                    "emoji": "🐛",
                    "title": "Bugs",
                    "findings": [
                        {"text": "Maybe a bug", "confidence": "uncertain"},
                    ],
                },
            ],
            "inline_comments": [],
        })
        result = self.formatter.format(raw, "a", "http://x")
        assert result.summary_sections[0].findings[0].confidence == "low"

    def test_llm_variation_normalized_in_inline_comment(self):
        raw = json.dumps({
            "verdict": "comment",
            "summary_sections": [],
            "inline_comments": [
                {
                    "file": "a.py",
                    "line": 1,
                    "body": "Issue",
                    "confidence": "very high",
                },
            ],
        })
        result = self.formatter.format(raw, "a", "http://x")
        assert result.inline_comments[0].confidence == "high"


class TestConfidencePrefixes:
    """Test CONFIDENCE_PREFIXES module-level constant."""

    def test_all_levels_present(self):
        assert "high" in CONFIDENCE_PREFIXES
        assert "medium" in CONFIDENCE_PREFIXES
        assert "low" in CONFIDENCE_PREFIXES

    def test_values(self):
        assert CONFIDENCE_PREFIXES["high"] == "🔴"
        assert CONFIDENCE_PREFIXES["medium"] == "🟡"
        assert CONFIDENCE_PREFIXES["low"] == "⚪"


class TestReviewFormatterJsonParsing:
    """Test direct JSON parsing from LLM output."""

    def setup_method(self):
        self.formatter = ReviewFormatter()

    def test_valid_json_parsed_correctly(self):
        raw = json.dumps({
            "verdict": "approve",
            "summary_sections": [
                {"emoji": "🧪", "title": "Testing", "findings": ["All tests pass"]},
            ],
            "inline_comments": [
                {"file": "src/main.py", "line": 10, "body": "Looks good"},
            ],
        })
        result = self.formatter.format(raw, "alice", "https://github.com/o/r/pull/1")

        assert result.verdict == "approve"
        assert result.persona_name == "alice"
        assert result.pr_url == "https://github.com/o/r/pull/1"
        assert len(result.summary_sections) == 1
        assert result.summary_sections[0].title == "Testing"
        assert result.summary_sections[0].findings[0].text == "All tests pass"
        assert len(result.inline_comments) == 1
        assert result.inline_comments[0].file == "src/main.py"

    def test_invalid_verdict_defaults_to_comment(self):
        raw = json.dumps({"verdict": "looks_fine", "summary_sections": [], "inline_comments": []})
        result = self.formatter.format(raw, "bob", "http://x")
        assert result.verdict == "comment"

    def test_missing_verdict_defaults_to_comment(self):
        raw = json.dumps({"summary_sections": [], "inline_comments": []})
        result = self.formatter.format(raw, "bob", "http://x")
        assert result.verdict == "comment"

    def test_empty_findings_section_omitted(self):
        raw = json.dumps({
            "verdict": "approve",
            "summary_sections": [
                {"emoji": "🐛", "title": "Bugs", "findings": []},
                {"emoji": "🧪", "title": "Testing", "findings": ["Has tests"]},
            ],
            "inline_comments": [],
        })
        result = self.formatter.format(raw, "alice", "http://x")
        assert len(result.summary_sections) == 1
        assert result.summary_sections[0].title == "Testing"

    def test_missing_emoji_uses_category_default(self):
        raw = json.dumps({
            "verdict": "approve",
            "summary_sections": [
                {"title": "Bugs", "findings": ["Found one"]},
            ],
            "inline_comments": [],
        })
        result = self.formatter.format(raw, "a", "http://x")
        assert result.summary_sections[0].emoji == CATEGORY_EMOJIS["Bugs"]

    def test_unknown_category_gets_fallback_emoji(self):
        raw = json.dumps({
            "verdict": "approve",
            "summary_sections": [
                {"title": "CustomCategory", "findings": ["Something"]},
            ],
            "inline_comments": [],
        })
        result = self.formatter.format(raw, "a", "http://x")
        assert result.summary_sections[0].emoji == "📝"


class TestMarkdownFenceStripping:
    """Test JSON extraction from markdown code fences."""

    def setup_method(self):
        self.formatter = ReviewFormatter()

    def test_json_in_fenced_block(self):
        raw = '```json\n{"verdict": "approve", "summary_sections": [], "inline_comments": []}\n```'
        result = self.formatter.format(raw, "a", "http://x")
        assert result.verdict == "approve"

    def test_json_in_plain_fence(self):
        raw = (
            '```\n{"verdict": "request_changes",'
            ' "summary_sections": [], "inline_comments": []}\n```'
        )
        result = self.formatter.format(raw, "a", "http://x")
        assert result.verdict == "request_changes"

    def test_json_with_surrounding_text(self):
        raw = (
            "Here's my review:\n\n"
            '{"verdict": "comment", "summary_sections": [], "inline_comments": []}\n\n'
            "Hope this helps!"
        )
        result = self.formatter.format(raw, "a", "http://x")
        assert result.verdict == "comment"


class TestFallbackBehavior:
    """Test fallback when JSON extraction fails entirely."""

    def setup_method(self):
        self.formatter = ReviewFormatter()

    def test_plain_text_falls_back(self):
        raw = "This code looks problematic because of X, Y, Z."
        result = self.formatter.format(raw, "alice", "http://x")

        assert result.verdict == "comment"
        assert result.persona_name == "alice"
        assert len(result.summary_sections) == 1
        assert result.summary_sections[0].title == "Bugs"
        assert result.summary_sections[0].emoji == "🐛"
        assert raw.strip() in result.summary_sections[0].findings[0].text

    def test_empty_string_falls_back(self):
        result = self.formatter.format("", "bob", "http://x")
        assert result.verdict == "comment"

    def test_malformed_json_falls_back(self):
        raw = '{"verdict": "approve", "summary_sections": [BROKEN'
        result = self.formatter.format(raw, "a", "http://x")
        assert result.verdict == "comment"
        assert len(result.summary_sections) == 1


class TestInlineCommentExtraction:
    """Test inline comment parsing from JSON."""

    def setup_method(self):
        self.formatter = ReviewFormatter()

    def test_multiple_inline_comments(self):
        raw = json.dumps({
            "verdict": "comment",
            "summary_sections": [],
            "inline_comments": [
                {"file": "a.py", "line": 1, "body": "Fix this"},
                {"file": "b.py", "line": 42, "body": "And this"},
            ],
        })
        result = self.formatter.format(raw, "a", "http://x")
        assert len(result.inline_comments) == 2
        assert result.inline_comments[0].file == "a.py"
        assert result.inline_comments[1].line == 42

    def test_inline_comment_defaults(self):
        raw = json.dumps({
            "verdict": "comment",
            "summary_sections": [],
            "inline_comments": [{}],
        })
        result = self.formatter.format(raw, "a", "http://x")
        assert result.inline_comments[0].file == ""
        assert result.inline_comments[0].line == 0
        assert result.inline_comments[0].body == ""
        assert result.inline_comments[0].confidence == "medium"
        assert result.inline_comments[0].confidence_reason == ""


class TestReviewResultModel:
    """Test ReviewResult and sub-model validation."""

    def test_review_result_fields(self):
        result = ReviewResult(
            verdict="approve",
            summary_sections=[],
            inline_comments=[],
            persona_name="test",
            pr_url="http://x",
        )
        assert result.verdict == "approve"
        assert result.summary_sections == []

    def test_category_section_defaults(self):
        section = CategorySection(emoji="🐛", title="Bugs")
        assert section.findings == []

    def test_inline_comment_model(self):
        comment = InlineComment(file="x.py", line=10, body="Nit")
        assert comment.file == "x.py"
        assert comment.line == 10
        assert comment.confidence == "medium"
        assert comment.confidence_reason == ""

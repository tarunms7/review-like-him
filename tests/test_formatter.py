"""Tests for review_bot.review.formatter — JSON parsing, fence stripping, fallback."""

from __future__ import annotations

import json

from review_bot.review.formatter import (
    CATEGORY_EMOJIS,
    CategorySection,
    InlineComment,
    ReviewFormatter,
    ReviewResult,
)


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
        raw = '```\n{"verdict": "request_changes", "summary_sections": [], "inline_comments": []}\n```'
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
        assert raw.strip() in result.summary_sections[0].findings[0]

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

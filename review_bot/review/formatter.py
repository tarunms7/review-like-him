"""Pydantic models for structured review output and LLM response parsing."""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

logger = logging.getLogger("review-bot")

# Valid review categories with their emoji prefixes
CATEGORY_EMOJIS: dict[str, str] = {
    "Bugs": "🐛",
    "Architecture": "🏗️",
    "Testing": "🧪",
    "Style": "💅",
    "Security": "🔒",
    "Performance": "⚡",
}


class CategorySection(BaseModel):
    """A categorized section of review findings with emoji prefix."""

    emoji: str = Field(description="Section emoji prefix")
    title: str = Field(description="Section title")
    findings: list[str] = Field(
        default_factory=list,
        description="List of finding descriptions",
    )


class InlineComment(BaseModel):
    """A review comment attached to a specific file and line."""

    file: str = Field(description="File path relative to repo root")
    line: int = Field(description="Line number in the diff")
    body: str = Field(description="Comment text")


class ReviewResult(BaseModel):
    """Structured output of a complete code review by a persona."""

    verdict: str = Field(
        description="Review verdict: 'approve', 'request_changes', or 'comment'",
    )
    summary_sections: list[CategorySection] = Field(
        default_factory=list,
        description="Categorized review sections",
    )
    inline_comments: list[InlineComment] = Field(
        default_factory=list,
        description="File-specific inline review comments",
    )
    persona_name: str = Field(description="Name of the persona")
    pr_url: str = Field(description="Full GitHub URL of the PR")


class ReviewFormatter:
    """Parses raw LLM output into a structured ReviewResult."""

    def format(
        self,
        raw_output: str,
        persona_name: str,
        pr_url: str,
    ) -> ReviewResult:
        """Parse LLM output into a ReviewResult.

        The LLM is prompted to return JSON. Falls back to
        heuristic parsing if JSON extraction fails.
        """
        parsed = self._extract_json(raw_output)

        if parsed:
            return self._from_json(parsed, persona_name, pr_url)

        # Fallback: treat the whole output as a comment
        logger.warning("Could not parse structured review, using fallback")
        return ReviewResult(
            verdict="comment",
            summary_sections=[
                CategorySection(
                    emoji="🐛",
                    title="Bugs",
                    findings=[raw_output.strip()],
                ),
            ],
            inline_comments=[],
            persona_name=persona_name,
            pr_url=pr_url,
        )

    def _extract_json(self, text: str) -> dict | None:
        """Try to extract JSON from LLM output."""
        text = text.strip()

        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code fences
        fence_match = re.search(
            r"```(?:json)?\s*\n(.*?)\n\s*```",
            text,
            re.DOTALL,
        )
        if fence_match:
            try:
                return json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try finding a JSON object anywhere in the text
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def _from_json(
        self,
        data: dict,
        persona_name: str,
        pr_url: str,
    ) -> ReviewResult:
        """Build ReviewResult from parsed JSON dict."""
        verdict = data.get("verdict", "comment")
        if verdict not in ("approve", "request_changes", "comment"):
            verdict = "comment"

        sections: list[CategorySection] = []
        for section_data in data.get("summary_sections", []):
            title = section_data.get("title", "")
            emoji = section_data.get(
                "emoji",
                CATEGORY_EMOJIS.get(title, "📝"),
            )
            findings = section_data.get("findings", [])
            if findings:
                sections.append(
                    CategorySection(
                        emoji=emoji,
                        title=title,
                        findings=findings,
                    )
                )

        inline_comments: list[InlineComment] = []
        for comment_data in data.get("inline_comments", []):
            inline_comments.append(
                InlineComment(
                    file=comment_data.get("file", ""),
                    line=comment_data.get("line", 0),
                    body=comment_data.get("body", ""),
                )
            )

        return ReviewResult(
            verdict=verdict,
            summary_sections=sections,
            inline_comments=inline_comments,
            persona_name=persona_name,
            pr_url=pr_url,
        )

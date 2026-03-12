"""LLM-powered analyzer that converts weighted review data into a PersonaProfile."""

from __future__ import annotations

import json
import logging
from datetime import date

from claude_code_sdk import ClaudeCodeOptions, Message, query

from review_bot.persona.profile import PersonaProfile, Priority, SeverityPattern

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT_TEMPLATE = """\
Analyze these {count} review comments from GitHub user '{github_user}'.
Extract the following and return them as a JSON object:

1. "priorities": array of objects with "category" (slug like error_handling, test_coverage, \
naming, architecture), "severity" (critical/strict/moderate/opinionated), \
and "description" (human-readable).
2. "pet_peeves": array of strings describing things this reviewer particularly dislikes.
3. "tone": string describing the reviewer's communication style and voice.
4. "severity_pattern": object with:
   - "blocks_on": array of strings (issues causing request changes)
   - "nits_on": array of strings (issues only nit-picked)
   - "approves_when": string (condition for approval)

The review comments (with weights indicating recency — higher = more recent):
{reviews_json}

Return ONLY valid JSON, no markdown fences or extra text.
"""


class PersonaAnalyzer:
    """Analyzes weighted review data using Claude to build a PersonaProfile."""

    def __init__(self) -> None:
        pass

    async def analyze(
        self,
        weighted_reviews: list[dict],
        github_user: str,
        persona_name: str,
    ) -> PersonaProfile:
        """Analyze weighted reviews and produce a structured PersonaProfile.

        Args:
            weighted_reviews: Review dicts with weight field from temporal weighting.
            github_user: GitHub username the reviews were mined from.
            persona_name: Name slug for the persona.

        Returns:
            A fully populated PersonaProfile.
        """
        # Build summary for the LLM — include body, verdict, weight
        summaries = []
        for r in weighted_reviews:
            entry = {
                "body": r.get("comment_body", ""),
                "verdict": r.get("verdict"),
                "weight": r.get("weight", 1.0),
                "repo": r.get("repo", ""),
                "file": r.get("file_path"),
            }
            summaries.append(entry)

        prompt = ANALYSIS_PROMPT_TEMPLATE.format(
            count=len(summaries),
            github_user=github_user,
            reviews_json=json.dumps(summaries, indent=2),
        )

        logger.info("Sending %d review summaries to Claude for analysis", len(summaries))

        # Call Claude Code SDK
        result_text = ""
        async for message in query(
            prompt=prompt,
            options=ClaudeCodeOptions(max_turns=1),
        ):
            if isinstance(message, Message):
                for block in message.content:
                    if hasattr(block, "text"):
                        result_text += block.text

        # Parse the JSON response
        parsed = self._parse_llm_response(result_text)

        # Build the mining summary
        repos = {r.get("repo", "") for r in weighted_reviews if r.get("repo")}
        mined_from = f"{len(weighted_reviews)} comments across {len(repos)} repos"

        return PersonaProfile(
            name=persona_name,
            github_user=github_user,
            mined_from=mined_from,
            last_updated=date.today().isoformat(),
            priorities=[Priority.model_validate(p) for p in parsed.get("priorities", [])],
            pet_peeves=parsed.get("pet_peeves", []),
            tone=parsed.get("tone", ""),
            severity_pattern=SeverityPattern.model_validate(
                parsed.get("severity_pattern", {})
            ),
            overrides=[],
        )

    def _parse_llm_response(self, text: str) -> dict:
        """Extract JSON from LLM response text, handling markdown fences."""
        text = text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last fence lines
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error("Failed to parse LLM response as JSON: %s", text[:200])
            return {}

"""LLM-powered analyzer that converts weighted review data into a PersonaProfile."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

from review_bot.persona.profile import PersonaProfile, Priority, SeverityPattern

logger = logging.getLogger(__name__)


@dataclass
class CategoryWeight:
    """Tracks smoothed weight for a review category.

    Attributes:
        category: Category name slug.
        current_rate: Current approval rate from feedback.
        smoothed_rate: EMA-smoothed rate to prevent oscillation.
    """

    category: str
    current_rate: float
    smoothed_rate: float

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

        # Call Claude Agent SDK
        result_text = ""
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(max_turns=1),
        ):
            if isinstance(message, ResultMessage):
                if message.result:
                    result_text = message.result
                if message.is_error:
                    raise RuntimeError(
                        f"Claude returned an error (stop_reason={message.stop_reason})"
                    )
            elif isinstance(message, AssistantMessage):
                if message.error:
                    raise RuntimeError(f"Claude error: {message.error}")
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

    async def analyze_incremental(
        self,
        existing_profile: PersonaProfile,
        all_weighted_reviews: list[dict],
    ) -> PersonaProfile:
        """Re-analyze all reviews and produce an updated PersonaProfile.

        Calls analyze() on the full set of weighted reviews, then preserves
        manual overrides from the existing profile.

        Args:
            existing_profile: The current persona profile with overrides to keep.
            all_weighted_reviews: Full merged set of weighted reviews to analyze.

        Returns:
            Updated PersonaProfile with preserved overrides and new timestamps.
        """
        profile = await self.analyze(
            all_weighted_reviews,
            existing_profile.github_user,
            existing_profile.name,
        )
        profile.overrides = existing_profile.overrides
        profile.smoothed_category_rates = existing_profile.smoothed_category_rates
        profile.last_mined_at = datetime.now(UTC).isoformat()
        repos = {r.get("repo", "") for r in all_weighted_reviews if r.get("repo")}
        profile.mined_from = (
            f"{len(all_weighted_reviews)} comments across {len(repos)} repos"
        )
        return profile

    async def reanalyze_with_feedback(
        self,
        persona_name: str,
        feedback_store,
    ) -> PersonaProfile | None:
        """Load profile, get feedback summary, adjust priorities based on approval rates.

        Uses EMA smoothing to prevent oscillation in priority adjustments.

        Args:
            persona_name: Name of the persona to reanalyze.
            feedback_store: FeedbackStore instance with feedback data.

        Returns:
            Updated PersonaProfile with adjusted priorities, or None if
            the persona profile cannot be loaded.
        """
        from review_bot.persona.store import PersonaStore

        store = PersonaStore()
        try:
            profile = store.load(persona_name)
        except FileNotFoundError:
            logger.warning("Cannot reanalyze: persona '%s' not found", persona_name)
            return None

        summaries = await feedback_store.get_persona_feedback_summary(persona_name)
        if not summaries:
            logger.info("No feedback data for persona '%s', skipping reanalysis", persona_name)
            return profile

        # Build approval rates and total feedback counts by category
        approval_rates: dict[str, float] = {
            s.category: s.approval_rate for s in summaries
        }
        feedback_counts: dict[str, int] = {
            s.category: s.positive_count + s.negative_count for s in summaries
        }

        # Minimum sample size per category before adjustments are applied
        # (per plan sections 3.4-3.5)
        min_sample_size = 10

        # Adjust priority severities based on feedback
        adjusted_priorities: list[Priority] = []
        for priority in profile.priorities:
            rate = approval_rates.get(priority.category)
            if rate is None:
                adjusted_priorities.append(priority)
                continue

            # Skip adjustment if insufficient feedback data
            total_feedback = feedback_counts.get(priority.category, 0)
            if total_feedback < min_sample_size:
                logger.info(
                    "Skipping adjustment for %s: only %d feedback events (need %d)",
                    priority.category, total_feedback, min_sample_size,
                )
                adjusted_priorities.append(priority)
                continue

            # Use stored previous smoothed rate, or 0.5 as initial value
            previous_rate = profile.smoothed_category_rates.get(
                priority.category, 0.5,
            )

            # Apply EMA smoothing
            smoothed = _apply_ema_smoothing(rate, previous_rate, alpha=0.3)

            # Map smoothed rate to severity adjustment
            new_severity = priority.severity
            if smoothed < 0.3:
                # Low approval → demote severity
                severity_demotion = {
                    "critical": "strict",
                    "strict": "moderate",
                    "moderate": "opinionated",
                    "opinionated": "opinionated",
                }
                new_severity = severity_demotion.get(
                    priority.severity, priority.severity
                )
                logger.info(
                    "Demoting %s severity from %s to %s (approval=%.2f)",
                    priority.category, priority.severity, new_severity, smoothed,
                )
            elif smoothed > 0.8:
                # High approval → promote severity
                severity_promotion = {
                    "opinionated": "moderate",
                    "moderate": "strict",
                    "strict": "critical",
                    "critical": "critical",
                }
                new_severity = severity_promotion.get(
                    priority.severity, priority.severity
                )
                logger.info(
                    "Promoting %s severity from %s to %s (approval=%.2f)",
                    priority.category, priority.severity, new_severity, smoothed,
                )

            # Persist the smoothed rate for next run
            profile.smoothed_category_rates[priority.category] = smoothed

            adjusted_priorities.append(
                Priority(
                    category=priority.category,
                    severity=new_severity,
                    description=priority.description,
                )
            )

        profile.priorities = adjusted_priorities
        profile.last_updated = date.today().isoformat()
        return profile

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


def _apply_ema_smoothing(
    current_rate: float,
    previous_rate: float,
    alpha: float = 0.3,
) -> float:
    """Apply Exponential Moving Average smoothing to prevent oscillation.

    Args:
        current_rate: Current period's approval rate.
        previous_rate: Previous period's smoothed rate.
        alpha: Smoothing factor (0-1). Higher = more weight on current.

    Returns:
        Smoothed rate value.
    """
    return alpha * current_rate + (1 - alpha) * previous_rate

"""Claude Agent SDK wrapper for executing review prompts."""

from __future__ import annotations

import asyncio
import logging

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, query

logger = logging.getLogger("review-bot")

# Backoff settings for rate limit retries
_INITIAL_BACKOFF = 2.0  # seconds
_MAX_BACKOFF = 60.0  # seconds


class ClaudeReviewer:
    """Executes review prompts via Claude Agent SDK with retry logic."""

    def __init__(self, max_retries: int = 3) -> None:
        self._max_retries = max_retries

    async def review(self, prompt: str) -> str:
        """Execute the review prompt and return raw LLM output.

        Implements retries with exponential backoff on rate limits (429).
        Detects auth/session issues and logs clear messages.

        Args:
            prompt: The full review prompt.

        Returns:
            Raw text output from the LLM.

        Raises:
            RuntimeError: If review fails after all retry attempts.
        """
        last_error: Exception | None = None
        backoff = _INITIAL_BACKOFF

        for attempt in range(self._max_retries + 1):
            try:
                result = await self._execute(prompt)
                if result.strip():
                    return result
                logger.warning(
                    "Empty response from Claude (attempt %d/%d)",
                    attempt + 1,
                    self._max_retries + 1,
                )
            except Exception as exc:
                last_error = exc
                self._log_error(exc, attempt)

                if attempt < self._max_retries:
                    if self._is_rate_limit(exc):
                        logger.info(
                            "Rate limited, backing off %.1fs before retry %d...",
                            backoff,
                            attempt + 2,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)
                    else:
                        logger.info("Retrying review (attempt %d)...", attempt + 2)

        raise RuntimeError(f"Review failed after {self._max_retries + 1} attempts: {last_error}")

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        """Check if an exception indicates a rate limit (429) error."""
        exc_str = str(exc).lower()
        return "rate" in exc_str or "429" in exc_str

    async def _execute(self, prompt: str) -> str:
        """Execute a single prompt via Claude Agent SDK."""
        result_text = ""
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(max_turns=1),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text"):
                        result_text += block.text
        return result_text

    def _log_error(self, exc: Exception, attempt: int) -> None:
        """Log an appropriate error message based on exception type."""
        exc_str = str(exc).lower()

        if any(keyword in exc_str for keyword in ("auth", "token", "session", "expired", "401")):
            logger.error(
                "Authentication error on attempt %d: %s. "
                "Check your Claude session is active and valid.",
                attempt + 1,
                exc,
            )
        elif "rate" in exc_str or "429" in exc_str:
            logger.error(
                "Rate limit hit on attempt %d: %s",
                attempt + 1,
                exc,
            )
        else:
            logger.error(
                "Review execution failed on attempt %d: %s",
                attempt + 1,
                exc,
            )

"""Structured logging setup for review-bot."""

import logging
import sys


def setup_logging(*, level: int = logging.INFO, verbose: bool = False) -> logging.Logger:
    """Configure the review-bot logger with structured output.

    Args:
        level: Base logging level.
        verbose: If True, set level to DEBUG.

    Returns:
        The configured 'review-bot' logger.
    """
    if verbose:
        level = logging.DEBUG

    logger = logging.getLogger("review-bot")
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(level)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger

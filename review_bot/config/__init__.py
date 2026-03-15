"""Configuration management for review-bot."""

from review_bot.config.paths import (
    CONFIG_DIR,
    CONFIG_FILE,
    DB_PATH,
    LOG_DIR,
    PERSONAS_DIR,
    PID_FILE,
    REPOS_DIR,
    ensure_directories,
    validate_path,
)
from review_bot.config.settings import Settings

__all__ = [
    "CONFIG_DIR",
    "CONFIG_FILE",
    "DB_PATH",
    "LOG_DIR",
    "PERSONAS_DIR",
    "PID_FILE",
    "REPOS_DIR",
    "Settings",
    "ensure_directories",
    "validate_path",
]

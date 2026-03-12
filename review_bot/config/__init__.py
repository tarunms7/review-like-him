"""Configuration management for review-bot."""

from review_bot.config.paths import CONFIG_DIR, CONFIG_FILE, DB_PATH, PERSONAS_DIR, REPOS_DIR
from review_bot.config.settings import Settings

__all__ = [
    "CONFIG_DIR",
    "CONFIG_FILE",
    "DB_PATH",
    "PERSONAS_DIR",
    "REPOS_DIR",
    "Settings",
]

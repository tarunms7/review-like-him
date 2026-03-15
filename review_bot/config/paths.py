"""Standard filesystem paths for review-bot data and configuration."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("review-bot")

CONFIG_DIR: Path = Path.home() / ".review-bot"
PERSONAS_DIR: Path = CONFIG_DIR / "personas"
REPOS_DIR: Path = CONFIG_DIR / "repos"
DB_PATH: Path = CONFIG_DIR / "review-bot.db"
CONFIG_FILE: Path = CONFIG_DIR / "config.yaml"
LOG_DIR: Path = CONFIG_DIR / "logs"
PID_FILE: Path = CONFIG_DIR / "server.pid"

# All directories that should exist for the application to run
_ALL_DIRS: list[Path] = [CONFIG_DIR, PERSONAS_DIR, REPOS_DIR, LOG_DIR]


def ensure_directories() -> None:
    """Create ~/.review-bot/ and all subdirectories if missing.

    Safe to call multiple times — uses exist_ok=True.
    """
    for directory in _ALL_DIRS:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Failed to create directory %s: %s", directory, exc)
            raise


def validate_path(path: Path, *, must_exist: bool = True, must_be_file: bool = False) -> None:
    """Validate a filesystem path.

    Args:
        path: The path to validate.
        must_exist: If True, raise if path does not exist.
        must_be_file: If True, raise if path is not a file.

    Raises:
        FileNotFoundError: If path doesn't exist and must_exist is True.
        ValueError: If path is not a file and must_be_file is True.
    """
    if must_exist and not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    if must_be_file and path.exists() and not path.is_file():
        raise ValueError(f"Path is not a file: {path}")

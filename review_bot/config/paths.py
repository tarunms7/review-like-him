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
        ValueError: If path is not a file and must_be_file is True,
            or if path traversal is detected.
    """
    # Check for path traversal attempts
    _check_path_traversal(path)

    if must_exist and not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    if must_be_file and path.exists() and not path.is_file():
        raise ValueError(f"Path is not a file: {path}")


def _check_path_traversal(path: Path) -> None:
    """Verify a path does not escape its expected base directory.

    Checks for '..' components and ensures resolved persona/config paths
    stay within CONFIG_DIR.

    Args:
        path: The path to check.

    Raises:
        ValueError: If path traversal is detected.
    """
    # Reject any path with '..' components
    if ".." in path.parts:
        raise ValueError(f"Path traversal detected: {path}")

    # If the path is under CONFIG_DIR, ensure it stays there after resolution
    try:
        resolved = path.resolve()
        config_resolved = CONFIG_DIR.resolve()
        if str(path).startswith(str(CONFIG_DIR)) and not str(resolved).startswith(
            str(config_resolved)
        ):
            raise ValueError(
                f"Path traversal detected: {path} resolves outside config directory"
            )
    except OSError:
        pass  # Path may not exist yet; '..' check above is sufficient


def safe_persona_path(persona_name: str) -> Path:
    """Build a safe path for a persona profile file.

    Args:
        persona_name: Name of the persona (used as directory/file name).

    Returns:
        Path within PERSONAS_DIR for the persona.

    Raises:
        ValueError: If the persona name contains path traversal characters.
    """
    # Reject names with path separators or traversal
    if "/" in persona_name or "\\" in persona_name or ".." in persona_name:
        raise ValueError(f"Invalid persona name: {persona_name!r}")

    result = PERSONAS_DIR / persona_name
    # Verify the resolved path is still under PERSONAS_DIR
    if not result.resolve().parent == PERSONAS_DIR.resolve() and \
       not str(result.resolve()).startswith(str(PERSONAS_DIR.resolve())):
        raise ValueError(f"Persona path escapes personas directory: {persona_name!r}")

    return result

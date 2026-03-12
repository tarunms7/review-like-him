"""Standard filesystem paths for review-bot data and configuration."""

from pathlib import Path

CONFIG_DIR: Path = Path.home() / ".review-bot"
PERSONAS_DIR: Path = CONFIG_DIR / "personas"
REPOS_DIR: Path = CONFIG_DIR / "repos"
DB_PATH: Path = CONFIG_DIR / "review-bot.db"
CONFIG_FILE: Path = CONFIG_DIR / "config.yaml"
